"""Contracts for the read-only legacy long-term memory boundary."""

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.agent.memory import MemoryManager


def _manager(tmp_path: Path) -> "MemoryManager":
    from src.agent.memory import MemoryManager
    from src.infra.session_store import SessionStore

    return MemoryManager(
        str(tmp_path / "memory"),
        store=SessionStore(str(tmp_path / "sessions.db")),
    )


def test_placeholder_long_term_memory_is_not_injected(tmp_path: Path) -> None:
    manager = _manager(tmp_path)

    assert manager.long_term_memory == ""
    messages = manager.build_messages("session", "base prompt", "question")
    assert messages[0] == {"role": "system", "content": "base prompt"}


def test_real_long_term_memory_remains_readable_and_injected(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    memory_path = manager.memory_dir / "long_term.md"
    memory_path.write_text(
        "# Long-term Memory\n\n"
        "<!-- internal note -->\n"
        "- Prefers concise answers\n",
        encoding="utf-8",
    )

    assert "Prefers concise answers" in manager.long_term_memory
    assert "internal note" not in manager.long_term_memory
    messages = manager.build_messages("session", "base prompt", "question")
    assert "## User Profile (Long-term Memory)" in messages[0]["content"]
    assert "Prefers concise answers" in messages[0]["content"]
    assert "internal note" not in messages[0]["content"]
