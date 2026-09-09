"""Regression tests for atomic, ordered, shared session history."""

import asyncio
import json
import sqlite3
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
    monkeypatch.setattr(harness, "_plan", lambda messages: next(plans))

    response = harness.run("agent-session", "original user message")

    assert response.answer == "final answer"
    turns = memory.load_history("agent-session")
    assert [turn.role for turn in turns] == ["user", "tool", "assistant"]
    assert turns[0].content == "original user message"
    assert turns[1].metadata == {"tool_name": "lookup", "success": True}
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

    async def final_plan(messages: list[dict[str, str]]) -> dict[str, str]:
        return {"action": "final_answer", "answer": "stream answer"}

    monkeypatch.setattr(harness, "_plan_async", final_plan)

    async def consume() -> list[str]:
        return [
            event
            async for event in harness.run_async_stream(
                "stream-session", "stream user"
            )
        ]

    events = asyncio.run(consume())
    assert any('"done": true' in event for event in events)
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
