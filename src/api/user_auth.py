"""Opt-in account endpoints. No anonymous-cookie promotion or business ACLs."""

import json
import secrets
import sqlite3
from dataclasses import asdict
from typing import Any

from argon2.exceptions import Argon2Error
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
from starlette._utils import get_route_path
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from config.settings import project_path, settings
from src.api.public_errors import public_error
from src.security.auth_origin import is_loopback
from src.security.user_auth import (
    SESSION_SECONDS,
    AuthError,
    AuthService,
    User,
    csrf_token,
)

LOGIN_COOKIE: str = "ragflow_login"
MAX_AUTH_BODY_BYTES: int = 4096
router: APIRouter = APIRouter(prefix="/auth", tags=["accounts"])
_ERROR_STATUS: dict[str, int] = {
    "auth_required": 401,
    "auth_invalid_credentials": 401,
    "auth_invalid_request": 422,
    "auth_password_invalid": 422,
    "auth_csrf_rejected": 403,
    "auth_password_change_required": 403,
    "auth_role_required": 403,
    "auth_transport_required": 403,
    "auth_rate_limited": 429,
    "auth_body_too_large": 413,
    "auth_busy": 503,
    "auth_unavailable": 503,
}


def get_auth_service(request: Request) -> AuthService:
    service: AuthService | None = getattr(request.app.state, "user_auth", None)
    if not settings.auth_enabled or service is None:
        raise AuthError("auth_unavailable")
    return service


async def open_user_auth(application: FastAPI) -> None:
    """Explicit startup only; importing app never creates an account database."""
    application.state.user_auth = None
    if settings.auth_enabled:
        try:
            application.state.user_auth = await run_in_threadpool(
                AuthService, project_path(settings.auth_db_path)
            )
            from src.infra.session_store import session_store

            await run_in_threadpool(session_store.initialize_ownership)
        except (AuthError, sqlite3.Error, Argon2Error, OSError):
            raise RuntimeError("Account storage initialization failed") from None


def _token(request: Request) -> str:
    return request.cookies.get(LOGIN_COOKIE, "")


async def require_user(request: Request, response: Response) -> User:
    try:
        _check_transport(request.scope)
        service: AuthService = get_auth_service(request)
        user: User = await run_in_threadpool(service.authenticate, _token(request))
        if user.must_change_password:
            raise AuthError("auth_password_change_required")
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            _check_origin(request)
            _check_csrf(request)
        elif (
            request.method == "GET"
            and get_route_path(request.scope).rstrip("/") == "/query/stream"
        ):
            # The legacy SSE GET spends model budget and saves history. It is
            # NOT a passive read. Same-origin fetch may omit Origin, but a
            # session-bound custom header still prevents simple-request CSRF.
            _check_csrf(request)
            if request.headers.get("origin") is not None:
                _check_origin(request)
            elif request.headers.get("sec-fetch-site") in {"cross-site", "same-site"}:
                raise AuthError("auth_csrf_rejected")
    except AuthError as exc:
        code: str = str(exc) if str(exc) in _ERROR_STATUS else "auth_unavailable"
        raise HTTPException(
            _ERROR_STATUS[code],
            detail=public_error(code),
            headers={"Cache-Control": "no-store"},
        ) from None
    except sqlite3.Error:
        raise HTTPException(
            503,
            detail=public_error("auth_unavailable"),
            headers={"Cache-Control": "no-store"},
        ) from None
    response.headers["Cache-Control"] = "no-store"
    return user


async def require_admin(user: User = Depends(require_user)) -> User:
    if user.role != "admin":
        raise HTTPException(
            403,
            detail=public_error("auth_role_required"),
            headers={"Cache-Control": "no-store"},
        )
    return user


def _check_transport(scope: Scope) -> None:
    if scope.get("scheme") == "https":
        return
    client: tuple[str, int] | None = scope.get("client")
    server: tuple[str, int] | None = scope.get("server")
    if (
        settings.auth_allow_loopback_http
        and client
        and server
        and is_loopback(client[0])
        and is_loopback(server[0])
    ):
        return
    raise AuthError("auth_transport_required")


def _check_origin(request: Request) -> None:
    origins: list[str] = request.headers.getlist("origin")
    if (
        len(origins) != 1
        or origins[0] not in settings.auth_allowed_origins
        or request.headers.get("sec-fetch-site") == "cross-site"
    ):
        raise AuthError("auth_csrf_rejected")


def _check_csrf(request: Request) -> None:
    token: str = _token(request)
    supplied: str = request.headers.get("x-csrf-token", "")
    # ASCII validation prevents compare_digest from exposing malformed input.
    if not token.isascii() or not supplied.isascii() or len(supplied) != 64:
        raise AuthError("auth_csrf_rejected")
    if not secrets.compare_digest(csrf_token(token), supplied):
        raise AuthError("auth_csrf_rejected")


def _unique_fields(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


async def _fields(request: Request, expected: dict[str, int]) -> dict[str, str]:
    if (
        request.headers.get("content-type", "").split(";", 1)[0].strip()
        != "application/json"
    ):
        raise AuthError("auth_invalid_request")
    body: bytearray = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_AUTH_BODY_BYTES:
            raise AuthError("auth_body_too_large")
        body.extend(chunk)
    try:
        value = json.loads(body, object_pairs_hook=_unique_fields)
    except (ValueError, UnicodeError, RecursionError):
        raise AuthError("auth_invalid_request") from None
    if (
        not isinstance(value, dict)
        or value.keys() != expected.keys()
        or any(
            not isinstance(value[key], str) or len(value[key]) > limit
            for key, limit in expected.items()
        )
    ):
        raise AuthError("auth_invalid_request")
    try:
        for field in value.values():
            field.encode("utf-8")
    except UnicodeError:
        raise AuthError("auth_invalid_request") from None
    return value


@router.post("/login")
async def login(
    request: Request,
    response: Response,
    service: AuthService = Depends(get_auth_service),
) -> dict[str, Any]:
    _check_origin(request)
    fields: dict[str, str] = await _fields(request, {"username": 64, "password": 128})
    source: str = request.client.host if request.client else "unknown"
    result = await run_in_threadpool(
        service.login, fields["username"], fields["password"], source
    )
    # Replace, never adopt a supplied login token (session-fixation prevention).
    old_token: str = _token(request)
    if old_token:
        await run_in_threadpool(service.logout, old_token)
    response.set_cookie(
        LOGIN_COOKIE,
        result.token,
        max_age=SESSION_SECONDS,
        path="/",
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="strict",
    )
    return {"user": asdict(result.user), "csrf_token": csrf_token(result.token)}


@router.get("/me")
async def me(
    request: Request, service: AuthService = Depends(get_auth_service)
) -> dict[str, Any]:
    user: User = await run_in_threadpool(service.authenticate, _token(request))
    return {"user": asdict(user), "csrf_token": csrf_token(_token(request))}


@router.post("/logout", status_code=204)
async def logout(
    request: Request, service: AuthService = Depends(get_auth_service)
) -> Response:
    _check_origin(request)
    _check_csrf(request)
    await run_in_threadpool(service.logout, _token(request))
    response: Response = Response(status_code=204)
    response.delete_cookie(
        LOGIN_COOKIE,
        path="/",
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="strict",
    )
    return response


@router.post("/password", status_code=204)
async def change_password(
    request: Request, service: AuthService = Depends(get_auth_service)
) -> Response:
    _check_origin(request)
    _check_csrf(request)
    fields: dict[str, str] = await _fields(
        request, {"current_password": 128, "new_password": 128}
    )
    await run_in_threadpool(
        service.change_password,
        _token(request),
        fields["current_password"],
        fields["new_password"],
    )
    response: Response = Response(status_code=204)
    response.delete_cookie(
        LOGIN_COOKIE,
        path="/",
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="strict",
    )
    return response


class UserAuthBoundaryMiddleware:
    """Redact all auth failures, bound transport, and disallow caching even errors."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = get_route_path(scope)
        if not (path == "/auth" or path.startswith("/auth/")):
            await self.app(scope, receive, send)
            return

        async def private_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Cache-Control"] = "no-store"
                headers["Pragma"] = "no-cache"
                headers["X-Content-Type-Options"] = "nosniff"
            await send(message)

        try:
            if not settings.auth_enabled:
                raise AuthError("auth_unavailable")
            _check_transport(scope)
            if scope.get("query_string"):
                raise AuthError("auth_invalid_request")
            await self.app(scope, receive, private_send)
        except (AuthError, sqlite3.Error, Argon2Error) as exc:
            code: str = str(exc) if isinstance(exc, AuthError) else "auth_unavailable"
            status: int = _ERROR_STATUS.get(code, 503)
            if code not in _ERROR_STATUS:
                code = "auth_unavailable"
            response = JSONResponse({"detail": public_error(code)}, status_code=status)
            if status in {429, 503}:
                response.headers["Retry-After"] = "300" if status == 429 else "1"
            await response(scope, receive, private_send)
