"""Authenticate index mutations before request bodies are parsed."""

from ipaddress import ip_address
from secrets import compare_digest

from starlette._utils import get_route_path
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from config.settings import settings
from src.api.public_errors import public_error

ADMIN_KEY_HEADER: str = "X-Admin-Key"
_PROTECTED_INDEX_MUTATIONS: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/upload"),
        ("POST", "/index/rebuild"),
        ("POST", "/index/rollback"),
    }
)


def _requires_admin_access(scope: Scope) -> bool:
    method: str = scope["method"]
    path: str = get_route_path(scope).rstrip("/") or "/"
    return (method, path) in _PROTECTED_INDEX_MUTATIONS or (
        method == "DELETE" and path.startswith("/documents/")
    )


def _is_loopback_endpoint(endpoint: object) -> bool:
    if not isinstance(endpoint, (list, tuple)) or not endpoint:
        return False
    host: object = endpoint[0]
    if not isinstance(host, str):
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def _has_valid_admin_key(scope: Scope, supplied: str | None) -> bool:
    if (
        settings.allow_unauthenticated_admin
        and _is_loopback_endpoint(scope.get("client"))
        and _is_loopback_endpoint(scope.get("server"))
    ):
        return True
    expected: str = (
        settings.admin_api_key.get_secret_value()
        if settings.admin_api_key is not None
        else ""
    )
    return bool(
        expected
        and compare_digest(
            (supplied or "").encode("utf-8"),
            expected.encode("utf-8"),
        )
    )


class AdminAuthMiddleware:
    """Reject index mutations before multipart or other request body parsing."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "http" or not _requires_admin_access(scope):
            await self.app(scope, receive, send)
            return

        supplied: str | None = Headers(scope=scope).get(ADMIN_KEY_HEADER)
        if _has_valid_admin_key(scope, supplied):
            await self.app(scope, receive, send)
            return

        response: JSONResponse = JSONResponse(
            status_code=401,
            content={"detail": public_error("admin_auth_required")},
        )
        await response(scope, receive, send)
