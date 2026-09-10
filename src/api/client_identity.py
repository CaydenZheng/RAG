"""ASGI client identity and FastAPI session scoping helpers."""

import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from http.cookies import SimpleCookie
from typing import Any

from fastapi import HTTPException, Request
from starlette.datastructures import MutableHeaders

from src.llm.cache_context import scoped_cache_identity
from src.security.session_ids import (
    SessionNamespace,
    scoped_session_id,
    validate_client_id,
    validate_public_session_id,
)

CLIENT_ID_COOKIE = "ragflow_client"
CLIENT_ID_MAX_AGE = 60 * 60 * 24 * 365


@dataclass(frozen=True)
class RequestSession:
    """Validated public session ID and its client-scoped storage key."""

    public_id: str
    storage_id: str


class ClientIdentityMiddleware:
    """Issue an opaque HttpOnly cookie through request.state.client_id."""

    def __init__(self, app: Callable[..., Awaitable[None]]) -> None:
        self.app = app

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[..., Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
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
) -> RequestSession:
    """Return distinct public and storage IDs or raise an HTTP 422 response."""
    try:
        public_id = validate_public_session_id(session_id, allow_empty=allow_empty)
        if public_id == "":
            return RequestSession(public_id="", storage_id="")
        return RequestSession(
            public_id=public_id,
            storage_id=scoped_session_id(
                request.state.client_id, public_id, namespace
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
