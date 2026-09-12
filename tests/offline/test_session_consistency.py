"""Regression tests for atomic, ordered, shared session history."""

import asyncio
import json
import sqlite3
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


def _assert_contiguous_exchanges(history: list[dict[str, str]], count: int) -> None:
    assert len(history) == count * 2
    seen: set[str] = set()
    for offset in range(0, len(history), 2):
        user_turn, assistant_turn = history[offset : offset + 2]
        exchange_id = user_turn["content"].removeprefix("question-")
        assert user_turn == {"role": "user", "content": f"question-{exchange_id}"}
        assert assistant_turn == {
            "role": "assistant",
            "content": f"answer-{exchange_id}",
        }
        seen.add(exchange_id)
    assert seen == {str(index) for index in range(count)}


def test_concurrent_rag_and_agent_exchanges_are_complete_and_ordered(
    tmp_path: Path,
) -> None:
    from src.agent.memory import MemoryConfig, MemoryManager, MemoryTurn
    from src.infra.session_store import SessionStore

    store = SessionStore(str(tmp_path / "sessions.db"))
    agent_stores = [
        SessionStore(str(tmp_path / "sessions.db")) for _ in range(4)
    ]
    memories = [
        MemoryManager(
            str(tmp_path / f"memory-{index}"),
            config=MemoryConfig(compress_trigger_turns=10_000),
            store=agent_store,
        )
        for index, agent_store in enumerate(agent_stores)
    ]
    count = 20

    def append_rag(index: int) -> None:
        store.append_exchange(
            "rag-session", f"question-{index}", f"answer-{index}"
        )

    def append_agent(index: int) -> None:
        memories[index % len(memories)].add_turns(
            "agent-session",
            [
                MemoryTurn(role="user", content=f"question-{index}"),
                MemoryTurn(role="assistant", content=f"answer-{index}"),
            ],
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(append_rag, range(count)))
        list(executor.map(append_agent, range(count)))

    _assert_contiguous_exchanges(store.get_history("rag-session", limit=100), count)
    _assert_contiguous_exchanges(
        store.get_history("agent-session", limit=100), count
    )


def test_exchange_rolls_back_when_second_insert_fails(tmp_path: Path) -> None:
    from src.infra.session_store import SessionStore

    database = tmp_path / "sessions.db"
    store = SessionStore(str(database))
    with sqlite3.connect(database) as connection:
        connection.execute("""
            CREATE TRIGGER reject_broken_answer
            BEFORE INSERT ON sessions
            WHEN NEW.role = 'assistant' AND NEW.content = 'broken'
            BEGIN
                SELECT RAISE(ABORT, 'forced failure');
            END
        """)

    with pytest.raises(sqlite3.IntegrityError, match="forced failure"):
        store.append_exchange("atomic-session", "must-not-remain", "broken")

    assert store.get_history("atomic-session") == []


class _ToolStub:
    def get_tool_descriptions(self) -> str:
        return "lookup: deterministic test tool"

    def execute(
        self, tool_name: str, params: dict, session_id: str
    ):
        from src.agent.tools import ToolResult

        assert tool_name == "lookup"
        assert params == {"key": "value"}
        return ToolResult(
            success=True,
            data={"fact": "tool observation"},
            tool_name=tool_name,
        )


def test_agent_preserves_original_user_and_tool_roles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.agent.harness import AgentConfig, AgentHarness
    from src.agent.hooks import HookPipeline
    from src.agent.memory import MemoryConfig, MemoryManager
    from src.infra.session_store import SessionStore

    store = SessionStore(str(tmp_path / "sessions.db"))
    memory = MemoryManager(
        str(tmp_path / "memory"),
        config=MemoryConfig(compress_trigger_turns=100),
        store=store,
    )
    harness = AgentHarness(
        config=AgentConfig(max_iterations=2, verbose=False),
        memory=memory,
        tools=_ToolStub(),
        hooks=HookPipeline(),
    )
    plans = iter(
        [
            {
                "action": "tool_call",
                "tool_name": "lookup",
                "tool_params": {"key": "value"},
            },
            {"action": "final_answer", "answer": "final answer"},
        ]
    )
    async def next_plan(messages, max_tokens=None):
        return next(plans)

    monkeypatch.setattr(harness, "_plan_async", next_plan)

    response = harness.run("agent-session", "original user message")

    assert response.answer == "final answer"
    turns = memory.load_history("agent-session")
    assert [turn.role for turn in turns] == ["user", "tool", "assistant"]
    assert turns[0].content == "original user message"
    assert turns[1].metadata["tool_name"] == "lookup"
    assert turns[1].metadata["success"] is True
    assert len(turns[1].metadata["call_id"]) == 32
    assert turns[2].content == "final answer"

    next_messages = memory.build_messages(
        "agent-session", "system", "next user message"
    )
    assert [message["role"] for message in next_messages] == [
        "system",
        "user",
        "user",
        "assistant",
        "user",
    ]
    assert next_messages[1]["content"] == "original user message"
    assert next_messages[-1]["content"] == "next user message"


def test_streaming_agent_saves_one_complete_exchange(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.agent.harness import AgentConfig, AgentHarness
    from src.agent.hooks import HookPipeline
    from src.agent.memory import MemoryConfig, MemoryManager
    from src.infra.session_store import SessionStore

    store = SessionStore(str(tmp_path / "sessions.db"))
    memory = MemoryManager(
        str(tmp_path / "memory"),
        config=MemoryConfig(compress_trigger_turns=100),
        store=store,
    )
    harness = AgentHarness(
        config=AgentConfig(max_iterations=1, verbose=False),
        memory=memory,
        tools=_ToolStub(),
        hooks=HookPipeline(),
    )

    async def final_plan(
        messages: list[dict[str, str]], max_tokens=None
    ) -> dict[str, str]:
        return {"action": "final_answer", "answer": "stream answer"}

    monkeypatch.setattr(harness, "_plan_async", final_plan)

    async def consume():
        return [
            event
            async for event in harness.events(
                "stream-session", "stream user"
            )
        ]

    events = asyncio.run(consume())
    assert events[-1].to_dict()["done"] is True
    assert [
        (turn.role, turn.content)
        for turn in memory.load_history("stream-session")
    ] == [("user", "stream user"), ("assistant", "stream answer")]


def test_legacy_agent_history_is_migrated_once(tmp_path: Path) -> None:
    from src.agent.memory import MemoryManager
    from src.infra.session_store import SessionStore

    memory_root = tmp_path / "memory"
    legacy_path = memory_root / "sessions" / "legacy" / "history.json"
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_text(
        json.dumps(
            [
                {
                    "role": "user",
                    "content": "old question",
                    "timestamp": 1.0,
                    "token_count": 2,
                    "metadata": {"source": "legacy"},
                },
                {
                    "role": "assistant",
                    "content": "old answer",
                    "timestamp": 2.0,
                    "token_count": 3,
                    "metadata": {},
                },
            ]
        ),
        encoding="utf-8",
    )
    store = SessionStore(str(tmp_path / "sessions.db"))
    memory = MemoryManager(str(memory_root), store=store)

    first_load = memory.load_history("legacy")
    second_load = memory.load_history("legacy")

    assert [(turn.role, turn.content) for turn in first_load] == [
        ("user", "old question"),
        ("assistant", "old answer"),
    ]
    assert second_load == first_load
    assert not legacy_path.exists()
    assert legacy_path.with_suffix(".json.migrated").exists()
    assert store.history_count("legacy") == 2


def test_clear_during_legacy_migration_does_not_restore_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.agent.memory import MemoryManager, MemoryTurn
    from src.infra.session_store import SessionStore

    memory_root: Path = tmp_path / "memory"
    legacy_path: Path = (
        memory_root / "sessions" / "legacy-clear-race" / "history.json"
    )
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_text(
        json.dumps(
            [
                {
                    "role": "user",
                    "content": "legacy question",
                    "timestamp": 1.0,
                    "token_count": 2,
                    "metadata": {},
                }
            ]
        ),
        encoding="utf-8",
    )
    store: SessionStore = SessionStore(str(tmp_path / "sessions.db"))
    memory: MemoryManager = MemoryManager(str(memory_root), store=store)
    migration_read: threading.Event = threading.Event()
    migration_release: threading.Event = threading.Event()
    original_read_text: Callable[..., str] = Path.read_text

    def controlled_read_text(
        path: Path,
        encoding: str | None = None,
        errors: str | None = None,
    ) -> str:
        content: str = original_read_text(
            path,
            encoding=encoding,
            errors=errors,
        )
        if path == legacy_path:
            migration_read.set()
            migration_release.wait(2)
        return content

    monkeypatch.setattr(Path, "read_text", controlled_read_text)

    async def exercise() -> tuple[list[MemoryTurn], bool]:
        load_task: asyncio.Task[list[MemoryTurn]] = asyncio.create_task(
            asyncio.to_thread(memory.load_history, "legacy-clear-race")
        )
        assert await asyncio.to_thread(migration_read.wait, 1)
        existed: bool = memory.clear_session("legacy-clear-race")
        migration_release.set()
        return await load_task, existed

    loaded: list[MemoryTurn]
    existed: bool
    loaded, existed = asyncio.run(exercise())

    assert existed
    assert loaded == []
    assert memory.load_history("legacy-clear-race") == []


def test_clear_before_legacy_snapshot_does_not_restore_session(
    tmp_path: Path,
) -> None:
    from src.agent.memory import MemoryManager, MemoryTurn
    from src.infra.session_store import HistorySnapshot, SessionStore

    class ClearWindowStore(SessionStore):
        def __init__(self, db_path: str) -> None:
            super().__init__(db_path)
            self.snapshot_started: threading.Event = threading.Event()
            self.snapshot_release: threading.Event = threading.Event()
            self.clear_completed: threading.Event = threading.Event()
            self.clear_release: threading.Event = threading.Event()
            self.pause_snapshot: bool = True

        def read_snapshot(self, session_id: str) -> HistorySnapshot:
            if self.pause_snapshot:
                self.pause_snapshot = False
                self.snapshot_started.set()
                self.snapshot_release.wait(2)
            return super().read_snapshot(session_id)

        def clear(self, session_id: str) -> bool:
            existed: bool = super().clear(session_id)
            self.clear_completed.set()
            self.clear_release.wait(2)
            return existed

    memory_root: Path = tmp_path / "memory"
    legacy_path: Path = (
        memory_root / "sessions" / "legacy-clear-window" / "history.json"
    )
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_text(
        json.dumps(
            [
                {
                    "role": "user",
                    "content": "must stay cleared",
                    "timestamp": 1.0,
                    "token_count": 2,
                    "metadata": {},
                }
            ]
        ),
        encoding="utf-8",
    )
    store: ClearWindowStore = ClearWindowStore(str(tmp_path / "sessions.db"))
    memory: MemoryManager = MemoryManager(str(memory_root), store=store)

    async def exercise() -> tuple[list[MemoryTurn], bool]:
        load_task: asyncio.Task[list[MemoryTurn]] = asyncio.create_task(
            asyncio.to_thread(memory.load_history, "legacy-clear-window")
        )
        assert await asyncio.to_thread(store.snapshot_started.wait, 1)
        clear_task: asyncio.Task[bool] = asyncio.create_task(
            asyncio.to_thread(memory.clear_session, "legacy-clear-window")
        )
        try:
            assert await asyncio.to_thread(store.clear_completed.wait, 1)
            store.snapshot_release.set()
            loaded: list[MemoryTurn] = await load_task
        finally:
            store.snapshot_release.set()
            store.clear_release.set()
        return loaded, await clear_task

    loaded: list[MemoryTurn]
    existed: bool
    loaded, existed = asyncio.run(exercise())

    assert loaded == []
    assert memory.load_history("legacy-clear-window") == []
    assert existed


def test_compression_claim_is_shared_and_owner_scoped(tmp_path: Path) -> None:
    from src.infra.session_store import SessionStore

    database: Path = tmp_path / "sessions.db"
    first_store: SessionStore = SessionStore(str(database))
    second_store: SessionStore = SessionStore(str(database))
    epoch: int = first_store.read_snapshot("shared-claim").revision[0]

    assert first_store.try_claim_compression(
        "shared-claim", epoch, "owner-a", ttl_seconds=300.0
    )
    assert not second_store.try_claim_compression(
        "shared-claim", epoch, "owner-b", ttl_seconds=300.0
    )

    second_store.release_compression_claim("shared-claim", "owner-b")
    assert not second_store.try_claim_compression(
        "shared-claim", epoch, "owner-b", ttl_seconds=300.0
    )

    assert second_store.try_claim_compression(
        "shared-claim", epoch, "owner-b", ttl_seconds=0.0
    )
    first_store.release_compression_claim("shared-claim", "owner-a")
    assert not first_store.try_claim_compression(
        "shared-claim", epoch, "owner-c", ttl_seconds=300.0
    )

    second_store.release_compression_claim("shared-claim", "owner-b")
    assert first_store.try_claim_compression(
        "shared-claim", epoch, "owner-c", ttl_seconds=300.0
    )


def test_async_memory_update_does_not_wait_for_compression_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.agent.memory import (
        MemoryConfig,
        MemoryManager,
        MemoryTurn,
        MemoryUpdateStatus,
    )
    from src.infra.session_store import SessionStore, SessionTurn
    from src.llm import llm_client

    database: Path = tmp_path / "sessions.db"
    first_store: SessionStore = SessionStore(str(database))
    second_store: SessionStore = SessionStore(str(database))
    config: MemoryConfig = MemoryConfig(
        compress_trigger_turns=3,
        compress_keep_recent=1,
    )
    first_memory: MemoryManager = MemoryManager(
        str(tmp_path / "memory-a"),
        config=config,
        store=first_store,
    )
    second_memory: MemoryManager = MemoryManager(
        str(tmp_path / "memory-b"),
        config=config,
        store=second_store,
    )
    first_store.append_turns(
        "nonblocking-memory-update",
        [
            SessionTurn(role="user", content="one"),
            SessionTurn(role="assistant", content="two"),
        ],
    )
    compression_started: threading.Event = threading.Event()
    compression_release: threading.Event = threading.Event()
    call_lock: threading.Lock = threading.Lock()
    compression_calls: int = 0

    def chat(*_args: object, **_kwargs: object) -> str:
        nonlocal compression_calls
        with call_lock:
            compression_calls += 1
        compression_started.set()
        compression_release.wait(2)
        return "summary"

    monkeypatch.setattr(llm_client, "chat", chat)

    async def exercise(
    ) -> tuple[bool, MemoryUpdateStatus, MemoryUpdateStatus]:
        first_task: asyncio.Task[MemoryUpdateStatus] = asyncio.create_task(
            first_memory.add_turns_async(
                "nonblocking-memory-update",
                [MemoryTurn(role="assistant", content="pending-a")],
            )
        )
        assert await asyncio.to_thread(compression_started.wait, 1)
        second_task: asyncio.Task[MemoryUpdateStatus] = asyncio.create_task(
            second_memory.add_turns_async(
                "nonblocking-memory-update",
                [MemoryTurn(role="assistant", content="pending-b")],
            )
        )
        completed_while_compressing: bool = True
        try:
            second_status: MemoryUpdateStatus = await asyncio.wait_for(
                asyncio.shield(second_task), timeout=0.5
            )
        except TimeoutError:
            completed_while_compressing = False
            second_status = await second_task
        finally:
            compression_release.set()
        first_status: MemoryUpdateStatus = await first_task
        return completed_while_compressing, second_status, first_status

    result: tuple[bool, MemoryUpdateStatus, MemoryUpdateStatus] = asyncio.run(
        exercise()
    )
    history: list[MemoryTurn] = first_memory.load_history(
        "nonblocking-memory-update"
    )

    assert result == (
        True,
        MemoryUpdateStatus.APPENDED,
        MemoryUpdateStatus.COMPRESSED,
    )
    assert compression_calls == 1
    assert [turn.content for turn in history][-2:] == [
        "pending-b",
        "pending-a",
    ]


def test_clear_during_async_compression_does_not_restore_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.agent.memory import (
        MemoryConfig,
        MemoryManager,
        MemoryTurn,
        MemoryUpdateStatus,
    )
    from src.infra.session_store import SessionStore, SessionTurn

    store: SessionStore = SessionStore(str(tmp_path / "sessions.db"))
    memory: MemoryManager = MemoryManager(
        str(tmp_path / "memory"),
        config=MemoryConfig(compress_trigger_turns=3, compress_keep_recent=1),
        store=store,
    )
    store.append_turns(
        "clear-during-compression",
        [
            SessionTurn(role="user", content="one"),
            SessionTurn(role="assistant", content="two"),
        ],
    )
    compression_started: threading.Event = threading.Event()
    compression_release: threading.Event = threading.Event()

    def compress(_old_turns: list[MemoryTurn]) -> str:
        compression_started.set()
        compression_release.wait(2)
        return "summary"

    monkeypatch.setattr(memory, "_llm_compress", compress)

    async def exercise() -> tuple[MemoryUpdateStatus, bool]:
        update_task: asyncio.Task[MemoryUpdateStatus] = asyncio.create_task(
            memory.add_turns_async(
                "clear-during-compression",
                [MemoryTurn(role="user", content="pending")],
            )
        )
        assert await asyncio.to_thread(compression_started.wait, 1)
        existed: bool = memory.clear_session("clear-during-compression")
        compression_release.set()
        return await update_task, existed

    status: MemoryUpdateStatus
    existed: bool
    status, existed = asyncio.run(exercise())

    assert existed
    assert status.value == "stale_discarded"
    assert memory.load_history("clear-during-compression") == []


def test_compression_keeps_the_current_interaction_uncompressed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.agent.memory import (
        MemoryConfig,
        MemoryManager,
        MemoryTurn,
        MemoryUpdateStatus,
    )
    from src.infra.session_store import SessionStore, SessionTurn
    from src.llm import llm_client

    store: SessionStore = SessionStore(str(tmp_path / "sessions.db"))
    memory: MemoryManager = MemoryManager(
        str(tmp_path / "memory"),
        config=MemoryConfig(compress_trigger_turns=3, compress_keep_recent=1),
        store=store,
    )
    store.append_turns(
        "protected-current-interaction",
        [
            SessionTurn(role="user", content="old-question"),
            SessionTurn(role="assistant", content="old-answer"),
        ],
    )

    def chat(*_args: object, **_kwargs: object) -> str:
        return "summary"

    monkeypatch.setattr(llm_client, "chat", chat)

    status: MemoryUpdateStatus = memory.add_turns(
        "protected-current-interaction",
        [
            MemoryTurn(role="user", content="current-question"),
            MemoryTurn(role="assistant", content="current-answer"),
        ],
    )
    history: list[MemoryTurn] = memory.load_history(
        "protected-current-interaction"
    )

    assert status is MemoryUpdateStatus.COMPRESSED
    assert [(turn.role, turn.content) for turn in history][-2:] == [
        ("user", "current-question"),
        ("assistant", "current-answer"),
    ]


def test_concurrent_async_memory_updates_share_one_compression(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.agent.memory import MemoryConfig, MemoryManager, MemoryTurn
    from src.infra.session_store import SessionStore, SessionTurn

    database: Path = tmp_path / "sessions.db"
    store: SessionStore = SessionStore(str(database))
    second_store: SessionStore = SessionStore(str(database))
    first_memory: MemoryManager = MemoryManager(
        str(tmp_path / "memory-a"),
        config=MemoryConfig(compress_trigger_turns=6, compress_keep_recent=2),
        store=store,
    )
    second_memory: MemoryManager = MemoryManager(
        str(tmp_path / "memory-b"),
        config=MemoryConfig(compress_trigger_turns=6, compress_keep_recent=2),
        store=second_store,
    )
    store.append_turns(
        "concurrent-compression",
        [
            SessionTurn(role="user", content="base-0"),
            SessionTurn(role="assistant", content="base-1"),
            SessionTurn(role="user", content="base-2"),
            SessionTurn(role="assistant", content="base-3"),
            SessionTurn(role="user", content="base-4"),
        ],
    )
    call_lock: threading.Lock = threading.Lock()
    call_count: int = 0

    def compress(_old_turns: list[MemoryTurn]) -> str:
        nonlocal call_count
        with call_lock:
            call_count += 1
        time.sleep(0.1)
        return "summary"

    monkeypatch.setattr(first_memory, "_llm_compress", compress)
    monkeypatch.setattr(second_memory, "_llm_compress", compress)

    async def update_concurrently() -> None:
        await asyncio.gather(
            first_memory.add_turns_async(
                "concurrent-compression",
                [MemoryTurn(role="assistant", content="pending-a")],
            ),
            second_memory.add_turns_async(
                "concurrent-compression",
                [MemoryTurn(role="assistant", content="pending-b")],
            ),
        )

    asyncio.run(update_concurrently())
    history: list[MemoryTurn] = first_memory.load_history(
        "concurrent-compression"
    )
    contents: list[str] = [turn.content for turn in history]
    summary_count: int = sum(turn.role == "summary" for turn in history)

    assert call_count == 1
    assert summary_count == 1
    assert "pending-a" in contents
    assert "pending-b" in contents


def test_compression_reuses_summary_after_cas_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.agent.memory import (
        MemoryConfig,
        MemoryManager,
        MemoryTurn,
        MemoryUpdateStatus,
    )
    from src.infra.session_store import SessionStore, SessionTurn
    from src.llm import llm_client

    class ConflictInjectingStore(SessionStore):
        def __init__(self, db_path: str) -> None:
            super().__init__(db_path)
            self.cas_attempts: int = 0

        def replace_history_if_revision(
            self,
            session_id: str,
            expected_revision: tuple[int, int, int],
            turns: list[SessionTurn],
            *,
            compression_owner: str | None = None,
        ) -> bool:
            self.cas_attempts += 1
            if self.cas_attempts == 1:
                self.append_turns(
                    session_id,
                    [SessionTurn(role="assistant", content="concurrent")],
                )
            return super().replace_history_if_revision(
                session_id,
                expected_revision,
                turns,
                compression_owner=compression_owner,
            )

    store: ConflictInjectingStore = ConflictInjectingStore(
        str(tmp_path / "sessions.db")
    )
    memory: MemoryManager = MemoryManager(
        str(tmp_path / "memory"),
        config=MemoryConfig(compress_trigger_turns=6, compress_keep_recent=2),
        store=store,
    )
    store.append_turns(
        "compression-cas-conflict",
        [
            SessionTurn(role="user", content="base-0"),
            SessionTurn(role="assistant", content="base-1"),
            SessionTurn(role="user", content="base-2"),
            SessionTurn(role="assistant", content="base-3"),
            SessionTurn(role="user", content="base-4"),
        ],
    )
    compression_calls: int = 0

    def chat(*_args: object, **_kwargs: object) -> str:
        nonlocal compression_calls
        compression_calls += 1
        return "reused summary"

    monkeypatch.setattr(llm_client, "chat", chat)

    status: MemoryUpdateStatus = memory.add_turns(
        "compression-cas-conflict",
        [MemoryTurn(role="assistant", content="pending")],
    )
    history: list[MemoryTurn] = memory.load_history("compression-cas-conflict")

    assert status is MemoryUpdateStatus.COMPRESSED
    assert compression_calls == 1
    assert store.cas_attempts == 2
    assert [(turn.role, turn.content) for turn in history] == [
        ("summary", "[历史对话摘要，4 轮已压缩]\nreused summary"),
        ("user", "base-4"),
        ("assistant", "concurrent"),
        ("assistant", "pending"),
    ]


def test_failed_async_memory_compression_returns_truncated_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.agent.memory import (
        MemoryConfig,
        MemoryManager,
        MemoryTurn,
        MemoryUpdateStatus,
    )
    from src.infra.session_store import SessionStore, SessionTurn
    from src.llm import llm_client

    store: SessionStore = SessionStore(str(tmp_path / "sessions.db"))
    memory: MemoryManager = MemoryManager(
        str(tmp_path / "memory"),
        config=MemoryConfig(compress_trigger_turns=5, compress_keep_recent=2),
        store=store,
    )
    store.append_turns(
        "compression-timeout",
        [
            SessionTurn(role="user", content="base-0"),
            SessionTurn(role="assistant", content="base-1"),
            SessionTurn(role="user", content="base-2"),
            SessionTurn(role="assistant", content="base-3"),
        ],
    )

    def chat(*_args: object, **_kwargs: object) -> str:
        raise TimeoutError("provider timeout")

    monkeypatch.setattr(llm_client, "chat", chat)

    status: MemoryUpdateStatus = asyncio.run(
        memory.add_turns_async(
            "compression-timeout",
            [MemoryTurn(role="user", content="pending")],
        )
    )
    history: list[MemoryTurn] = memory.load_history("compression-timeout")
    contents: list[str] = [turn.content for turn in history]

    assert status is MemoryUpdateStatus.TRUNCATED
    assert contents == ["base-1", "base-2", "base-3", "pending"]
