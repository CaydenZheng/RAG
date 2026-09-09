"""Load browser resources relative to this module."""

from pathlib import Path

_WEB_DIR = Path(__file__).parent
_PAGES_DIR = _WEB_DIR / "pages"
STATIC_DIR = _WEB_DIR / "static"

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
}


def _load_page(filename: str) -> str:
    return (_PAGES_DIR / filename).read_text(encoding="utf-8")


SEARCH_PAGE_HTML = _load_page("search.html")
AGENT_PAGE_HTML = _load_page("agent.html")
