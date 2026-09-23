"""Behavioral coverage for bounded runtime state."""

import asyncio
import json
import os
from pathlib import Path

import pytest


def _register_gray_tool(registry) -> None:
    from src.agent.tools import SafetyLevel, ToolDef, ToolParam
    from src.core.agent_runtime import ToolResult

    registry.register(
        ToolDef(
            name="read_file",
            description="test audit tool",
            params=[ToolParam("name", "str", "name")],
            safety_level=SafetyLevel.GRAYLIST,
            execute_fn=lambda params: ToolResult(success=True, data=params),
        )
    )


def test_tool_dedup_evicts_the_oldest_session_at_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.agent import tools as tools_module
    from src.agent.tools import ToolRegistry

    monkeypatch.setattr(tools_module.time, "time", lambda: 100.0)
    registry = ToolRegistry(dedup_window=30.0, max_sessions=2)
    _register_gray_tool(registry)

    assert registry.execute("read_file", {"name": "same"}, "session-a").success
    assert registry.execute("read_file", {"name": "same"}, "session-b").success
    assert registry.execute("read_file", {"name": "same"}, "session-c").success

    result = registry.execute("read_file", {"name": "same"}, "session-a")
    assert result.success


def test_llm_cache_evicts_oldest_entries_without_manual_clear() -> None:
    from src.llm.cache import LLMCache

    cache = LLMCache(max_entries=2)
    first = [{"role": "user", "content": "first"}]
    second = [{"role": "user", "content": "second"}]
    third = [{"role": "user", "content": "third"}]

    cache.set("model", first, 0, "one")
    cache.set("model", second, 0, "two")
    cache.set("model", third, 0, "three")

    assert cache.get("model", first, 0) is None
    assert cache.get("model", second, 0) == "two"
    assert cache.get("model", third, 0) == "three"



def _assert_bounded_jsonl_files(
    path: Path, backup_count: int, max_bytes: int
) -> None:
    files = sorted(path.parent.glob(f"{path.name}*"))
    assert 1 <= len(files) <= backup_count + 1
    for file_path in files:
        assert file_path.stat().st_size <= max_bytes
        for line in file_path.read_text(encoding="utf-8").splitlines():
            json.loads(line)


def test_agent_hook_logs_rotate_and_apply_backup_retention(tmp_path: Path) -> None:
    from src.agent.hooks import HookContext, HookEvent, create_logging_hook

    hook = create_logging_hook(
        str(tmp_path),
        max_bytes=500,
        backup_count=2,
        retention_seconds=60,
    )
    for index in range(10):
        hook(
            HookContext(
                event=HookEvent.POST_PLANNING,
                session_id="session",
                data={"sequence": index, "detail": "x" * 80},
            )
        )

    path = tmp_path / "agent_events.jsonl"
    _assert_bounded_jsonl_files(path, backup_count=2, max_bytes=500)
    assert not (tmp_path / "agent_events.jsonl.3").exists()

    expired_backup = tmp_path / "agent_events.jsonl.1"
    assert expired_backup.exists()
    os.utime(expired_backup, (0, 0))
    cleanup_hook = create_logging_hook(
        str(tmp_path),
        max_bytes=10_000,
        backup_count=2,
        retention_seconds=1,
    )
    cleanup_hook(HookContext(event=HookEvent.SESSION_END, session_id="session"))
    assert not expired_backup.exists()


def test_tool_audit_log_uses_the_same_rotation_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from config.settings import settings
    from src.agent.tools import ToolRegistry

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "agent_log_max_bytes", 500)
    monkeypatch.setattr(settings, "agent_log_backup_count", 1)
    monkeypatch.setattr(settings, "agent_log_retention_seconds", 60)
    registry = ToolRegistry(dedup_window=0)
    _register_gray_tool(registry)

    for index in range(10):
        assert registry.execute(
            "read_file", {"name": f"file-{index}"}, "session"
        ).success

    path = tmp_path / "logs" / "audit.jsonl"
    _assert_bounded_jsonl_files(path, backup_count=1, max_bytes=500)
    assert not (path.parent / "audit.jsonl.2").exists()


@pytest.mark.parametrize("run_async", [False, True])
def test_tool_audit_failure_does_not_change_success_or_retry(
    monkeypatch: pytest.MonkeyPatch,
    run_async: bool,
) -> None:
    from src.agent import tools as tools_module
    from src.agent.tools import ToolRegistry

    registry = ToolRegistry(dedup_window=0)
    _register_gray_tool(registry)
    calls: list[dict] = []
    tool = registry.get_tool("read_file")
    assert tool is not None
    original_execute = tool.execute_fn

    def execute(params: dict):
        calls.append(params)
        return original_execute(params)

    def fail_audit(*_args, **_kwargs) -> None:
        raise OSError("audit unavailable")

    warnings: list[tuple[str, tuple]] = []
    tool.execute_fn = execute
    monkeypatch.setattr(tools_module, "append_jsonl", fail_audit)
    monkeypatch.setattr(
        tools_module.logger,
        "warning",
        lambda message, *args: warnings.append((message, args)),
    )

    if run_async:
        result = asyncio.run(
            registry.execute_async("read_file", {"name": "one"}, "session")
        )
    else:
        result = registry.execute("read_file", {"name": "one"}, "session")

    assert result.success
    assert calls == [{"name": "one"}]
    assert warnings == [
        (
            "Tool audit log write failed: tool={} type={}",
            ("read_file", "OSError"),
        )
    ]


def test_jsonl_pruning_tolerates_backup_disappearing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.infra.jsonl import append_jsonl

    path = tmp_path / "audit.jsonl"
    backup = tmp_path / "audit.jsonl.1"
    backup.write_text('{"old": true}\n', encoding="utf-8")
    original_stat = Path.stat
    disappeared = False

    def stat_with_concurrent_delete(self: Path, *args, **kwargs):
        nonlocal disappeared
        if self == backup and not disappeared:
            disappeared = True
            backup.unlink()
            raise FileNotFoundError(backup)
        return original_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat_with_concurrent_delete)

    append_jsonl(
        path,
        {"new": True},
        max_bytes=1_000,
        backup_count=1,
        retention_seconds=60,
    )

    assert disappeared
    assert json.loads(path.read_text(encoding="utf-8")) == {"new": True}


def test_jsonl_rejects_one_record_larger_than_the_file_limit(
    tmp_path: Path,
) -> None:
    from src.infra.jsonl import append_jsonl

    path = tmp_path / "bounded.jsonl"
    original = b'{"existing": true}\n'
    path.write_bytes(original)

    written = append_jsonl(
        path,
        {"oversized": "x" * 10_000},
        max_bytes=1_024,
        backup_count=1,
        retention_seconds=60,
    )

    assert written is False
    assert path.read_bytes() == original
    assert not path.with_name("bounded.jsonl.1").exists()


def test_default_pipeline_delegates_graylist_audit_to_registry(
    isolated_runtime: Path,
) -> None:
    from src.agent.hooks import (
        HookContext,
        HookEvent,
        create_default_pipeline,
    )
    from src.agent.tools import ToolRegistry

    pipeline = create_default_pipeline()
    registry = ToolRegistry(dedup_window=0)
    _register_gray_tool(registry)
    context = HookContext(
        event=HookEvent.PRE_TOOL_USE,
        session_id="session",
        data={"tool_name": "read_file", "tool_params": {"name": "one"}},
    )

    pipeline.fire(context)

    audit_path = isolated_runtime / "logs" / "audit.jsonl"
    assert not audit_path.exists()
    assert registry.execute(
        "read_file", {"name": "one"}, "session"
    ).success

    records = audit_path.read_text(encoding="utf-8").splitlines()
    assert len(records) == 1
    assert json.loads(records[0])["tool_name"] == "read_file"


def test_external_tools_that_send_user_input_are_graylisted() -> None:
    from src.agent.tools import SafetyLevel, create_default_registry

    registry = create_default_registry()

    for tool_name in ("get_weather", "search_web"):
        tool = registry.get_tool(tool_name)
        assert tool is not None
        assert tool.safety_level == SafetyLevel.GRAYLIST
