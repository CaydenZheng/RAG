"""Serve the built browser application without making it a startup dependency."""

from pathlib import Path

from fastapi.responses import HTMLResponse, PlainTextResponse, Response

_WEB_DIR = Path(__file__).parent
APP_DIST_DIR = _WEB_DIR / "dist"
APP_INDEX_PATH = APP_DIST_DIR / "index.html"

PAGE_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "base-uri 'none'; "
        "frame-ancestors 'none'; "
        "form-action 'self'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-store",
}

WEB_BUILD_INSTRUCTIONS = (
    "Web application is not built. Run `cd frontend`, `npm ci`, and "
    "`npm run build`, then refresh this page."
)


def web_entrypoint_response() -> Response:
    """Return the Vite entrypoint or a recoverable build instruction."""
    try:
        html = APP_INDEX_PATH.read_text(encoding="utf-8")
    except OSError:
        return PlainTextResponse(
            WEB_BUILD_INSTRUCTIONS,
            status_code=503,
            headers=PAGE_SECURITY_HEADERS,
        )
    return HTMLResponse(html, headers=PAGE_SECURITY_HEADERS)
