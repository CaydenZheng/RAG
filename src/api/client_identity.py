"""ASGI client identity and FastAPI session scoping helpers."""

import asyncio
import json
import secrets
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from http.cookies import SimpleCookie
from typing import Any

from fastapi import HTTPException, Request
from starlette._utils import get_route_path
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import MutableHeaders
from starlette.responses import JSONResponse, Response

from config.settings import settings
from src.api.public_errors import public_error
from src.core.run_options import WorkspaceError, scoped_run_options
from src.llm.cache_context import scoped_cache_identity
from src.security.account_limits import AccountRequestLimits, source_scope
from src.security.request_authorization import (
    AccountLimitExceeded,
    AuthorizationRevoked,
    ConversationBusy,
    ConversationConflict,
    PrivateSessionError,
    RequestAuthorization,
    bind_authorization,
    register_generation,
)
from src.security.session_ids import (
    SessionNamespace,
    account_scope,
    scoped_session_id,
    validate_client_id,
    validate_public_session_id,
)

CLIENT_ID_COOKIE = "ragflow_client"
CLIENT_ID_MAX_AGE = 60 * 60 * 24 * 365
AUTH_RECHECK_SECONDS: float = 0.25
_GENERATION_PATHS: frozenset[str] = frozenset(
    {"/query", "/query/stream", "/agent/chat", "/agent/chat/stream", "/skills/creator/stream"}
)


def _account_path(path: str) -> bool:
    return (
        path in _GENERATION_PATHS
        or (path.startswith("/workspace/") and path != "/workspace/config")
        or path in {"/ready", "/agent/tools"}
        or path == "/agent/reset"
        or path.startswith(
            (
                "/session/",
                "/agent/memory/",
                "/agent/approvals/",
                "/agent/elicitation/",
                "/conversations",
                "/skills",
                "/knowledge/",
            )
        )
    )


@dataclass(frozen=True)
class RequestSession:
    """Validated public session ID and its client-scoped storage key."""

    public_id: str
    storage_id: str


class ClientIdentityMiddleware:
    """Issue an opaque HttpOnly cookie through request.state.client_id."""

    def __init__(self, app: Callable[..., Awaitable[None]]) -> None:
        self.app = app
        self._limits: AccountRequestLimits = AccountRequestLimits()

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[..., Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if settings.auth_enabled:
            path: str = get_route_path(scope).rstrip("/") or "/"
            if _account_path(path):
                await self._account_request(scope, receive, send, path)
            else:
                # Account mode never issues/promotes an anonymous identity.
                await self.app(scope, receive, send)
            return

        if get_route_path(scope).startswith(("/conversations", "/skills", "/knowledge/")) or (
            get_route_path(scope).startswith("/workspace/") and get_route_path(scope) != "/workspace/config"
        ):
            await JSONResponse({"detail": public_error("auth_required")},status_code=401,
                               headers={"Cache-Control":"no-store"})(scope,receive,send)
            return

        supplied_id = Request(scope).cookies.get(CLIENT_ID_COOKIE, "")
        try:
            client_id = validate_client_id(supplied_id)
            issue_cookie = False
        except ValueError:
            client_id = secrets.token_hex(16)
            issue_cookie = True

        scope.setdefault("state", {})["client_id"] = client_id

        async def send_with_identity(message: dict[str, Any]) -> None:
            if issue_cookie and message["type"] == "http.response.start":
                MutableHeaders(scope=message).append(
                    "set-cookie",
                    _client_cookie_header(
                        client_id, secure=scope.get("scheme") == "https"
                    ),
                )
            await send(message)

        with scoped_cache_identity(client_id):
            await self.app(scope, receive, send_with_identity)

    async def _account_request(
        self,
        scope: dict[str, Any],
        receive: Callable[..., Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
        path: str,
    ) -> None:
        from src.api.user_auth import LOGIN_COOKIE, get_auth_service, require_user
        from src.infra.tracer import tracer
        from src.security.user_auth import AuthError

        request: Request = Request(scope, receive)
        try:
            user = await require_user(request, Response())
            service = get_auth_service(request)
        except HTTPException as exc:
            await JSONResponse(
                {"detail": exc.detail},
                status_code=exc.status_code,
                headers={"Cache-Control": "no-store"},
            )(scope, receive, send)
            return
        except (AuthError, sqlite3.Error):
            await JSONResponse(
                {"detail": public_error("auth_unavailable")},
                status_code=503,
                headers={"Cache-Control": "no-store"},
            )(scope, receive, send)
            return
        scope.setdefault("state", {})["user"] = user
        if tracer.current is not None:
            tracer.current["actor_scope"] = account_scope(user.id)
        credential: str = request.cookies.get(LOGIN_COOKIE, "")
        loop: asyncio.AbstractEventLoop = asyncio.get_running_loop()
        execution: asyncio.Task[None] | None = None

        def cancel_execution() -> None:
            if execution is not None:
                loop.call_soon_threadsafe(execution.cancel)

        authorization: RequestAuthorization = RequestAuthorization(
            user.id,
            lambda: service.session_active(credential),
            lambda: service.guard_session(credential),
            generation=path in _GENERATION_PATHS,
            cancel_task=cancel_execution,
            role=user.role,
            source_scope=source_scope(request.client.host if request.client else "unknown"),
        )
        limited: bool = path in _GENERATION_PATHS
        if limited and not self._limits.acquire(user.id, authorization.source_scope):
            await JSONResponse({"detail":public_error("account_rate_limited")},status_code=429,
                               headers={"Cache-Control":"no-store","Retry-After":"60"})(scope,receive,send)
            return
        started: bool = False
        finished: bool = False
        stream: bool = False

        async def private_send(message: dict[str, Any]) -> None:
            nonlocal started, finished, stream
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Cache-Control"] = "no-store"
            await send(message)
            # Track accepted sends, not attempts that cancellation can interrupt.
            if message["type"] == "http.response.start":
                started = True
                stream = headers.get("content-type", "").startswith("text/event-stream")
            elif message["type"] == "http.response.body" and not message.get(
                "more_body", False
            ):
                finished = True

        async def watch() -> None:
            while True:
                await asyncio.sleep(AUTH_RECHECK_SECONDS)
                if not await run_in_threadpool(authorization.active):
                    return

        async def failure(code: str, status: int) -> None:
            if finished:
                return
            if not started:
                await JSONResponse(
                    {"detail": public_error(code)},
                    status_code=status,
                    headers={"Cache-Control": "no-store"},
                )(scope, receive, send)
            elif stream:
                body: bytes = b""
                if not authorization.terminal.is_set():
                    if path == "/query/stream":
                        event: dict[str, Any] = {
                            "event": "error",
                            "done": True,
                            "error": public_error(code),
                            "query_id": scope["state"].get("request_id", "unavailable"),
                        }
                    else:
                        event = {"code": code, "error": public_error(code)["message"]}
                    body = (
                        "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
                    ).encode()
                await send(
                    {"type": "http.response.body", "body": body, "more_body": False}
                )
            else:
                # Headers (including Content-Length) are already accepted. Raising
                # aborts the response at the ASGI server; no second status or fake
                # empty JSON body may make this incomplete response look successful.
                raise RuntimeError("Private response interrupted") from None

        with (
            bind_authorization(authorization),
            scoped_cache_identity("user\0" + user.id),
            scoped_run_options(None),
        ):
            execution = asyncio.create_task(self.app(scope, receive, private_send))
            watcher: asyncio.Task[None] = asyncio.create_task(watch())
            try:
                done, _ = await asyncio.wait(
                    (execution, watcher), return_when=asyncio.FIRST_COMPLETED
                )
                if execution in done:
                    await execution
                else:
                    authorization.revoked.set()
                    execution.cancel()
                    await asyncio.gather(execution, return_exceptions=True)
                    await failure("auth_required", 401)
            except asyncio.CancelledError:
                authorization.revoked.set()
                if execution.cancelled() and asyncio.current_task().cancelling() == 0:
                    await failure("auth_required", 401)
                else:
                    raise
            except PrivateSessionError:
                await failure("private_session_not_found", 404)
            except ConversationBusy:
                await failure("conversation_busy", 409)
            except ConversationConflict:
                await failure("conversation_conflict", 409)
            except AuthorizationRevoked:
                authorization.revoked.set()
                await failure("auth_required", 401)
            except AccountLimitExceeded:
                await failure("account_quota_exceeded", 429)
            except sqlite3.Error:
                await failure("auth_unavailable", 503)
            except WorkspaceError as error:
                await failure(error.code, error.status)
            except ValueError:
                await failure("auth_invalid_request", 422)
            finally:
                watcher.cancel()
                if not execution.done():
                    authorization.revoked.set()
                    execution.cancel()
                await asyncio.gather(execution, watcher, return_exceptions=True)
                if limited:
                    self._limits.release(user.id, authorization.source_scope)


def _client_cookie_header(client_id: str, *, secure: bool) -> str:
    cookie = SimpleCookie()
    cookie[CLIENT_ID_COOKIE] = client_id
    cookie[CLIENT_ID_COOKIE]["httponly"] = True
    cookie[CLIENT_ID_COOKIE]["max-age"] = CLIENT_ID_MAX_AGE
    cookie[CLIENT_ID_COOKIE]["path"] = "/"
    cookie[CLIENT_ID_COOKIE]["samesite"] = "strict"
    if secure:
        cookie[CLIENT_ID_COOKIE]["secure"] = True
    return cookie.output(header="").strip()


def scope_request_session(
    request: Request,
    session_id: str,
    namespace: SessionNamespace,
    *,
    allow_empty: bool = False,
    create: bool = False,
) -> RequestSession:
    """Return distinct public and storage IDs or raise an HTTP 422 response."""
    try:
        public_id = validate_public_session_id(session_id, allow_empty=allow_empty)
        if public_id == "":
            return RequestSession(public_id="", storage_id="")
        if settings.auth_enabled:
            from src.infra.session_store import session_store

            user = getattr(request.state, "user", None)
            if user is None:
                raise HTTPException(401, detail=public_error("auth_required"))
            storage_id: str = session_store.owned_session(
                user.id, public_id, namespace, create=create
            )
            register_generation(storage_id)
            return RequestSession(public_id=public_id, storage_id=storage_id)
        return RequestSession(
            public_id=public_id,
            storage_id=scoped_session_id(request.state.client_id, public_id, namespace),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
