"""Load the browser pages relative to this module."""

from pathlib import Path

_PAGES_DIR = Path(__file__).with_name("pages")


def _load_page(filename: str) -> str:
    return (_PAGES_DIR / filename).read_text(encoding="utf-8")


SEARCH_PAGE_HTML = _load_page("search.html")
AGENT_PAGE_HTML = _load_page("agent.html")
