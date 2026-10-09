"""Permanent security contracts for native tool execution boundaries."""

from __future__ import annotations

import asyncio
import json
import os
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from loguru import logger

_CALCULATOR_ESCAPE = (
    "(lambda: [c for c in ().__class__.__base__.__subclasses__() "
    "if c.__name__ == 'catch_warnings'][0]()._module.__builtins__"
    "['__import__']('os').getpid())()"
)


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("2 + 3 * 4", 14),
        ("sqrt(16) + 2*pi", 4 + 2 * 3.141592653589793),
        ("sum([1, 2, 3]) + max(4, 5)", 11),
        ("round(sin(pi / 2), 5)", 1),
    ],
)
def test_default_calculator_allows_only_bounded_arithmetic(
    expression: str,
    expected: float,
) -> None:
    from src.agent.tools import create_default_registry

    result = asyncio.run(
        create_default_registry().execute_async(
            "calculator",
            {"expression": expression},
            session_id=f"allowed-{expression}",
        )
    )

    assert result.success is True
    assert result.data["result"] == pytest.approx(expected)


@pytest.mark.parametrize(
    "expression",
    [
        _CALCULATOR_ESCAPE,
        "(1).__class__",
        "[1, 2][0]",
        "(lambda: 1)()",
        "sum(value for value in [1, 2])",
        "10 ** 101",
        "sum([1e100, 1e100, -1e100])",
        "'not arithmetic'",
    ],
)
def test_default_calculator_rejects_unsafe_or_unbounded_ast(
    expression: str,
) -> None:
    from src.agent.tools import create_default_registry

    result = asyncio.run(
        create_default_registry().execute_async(
            "calculator",
            {"expression": expression},
            session_id=f"rejected-{len(expression)}",
        )
    )

    assert result.success is False
    assert result.error == "Calculator expression was rejected"
    assert result.error_code == "calculator_expression_rejected"
    assert result.data is None


def test_default_calculator_rejects_oversized_expression_before_execution() -> None:
    from src.agent.tools import create_default_registry

    result = asyncio.run(
        create_default_registry().execute_async(
            "calculator",
            {"expression": "+".join("1" for _ in range(300))},
            session_id="oversized-expression",
        )
    )

    assert result.success is False
    assert result.error_code == "invalid_tool_parameters"


@pytest.mark.parametrize("planner_mode", ["json", "native"])
def test_harness_planners_cannot_escape_default_calculator(
    planner_mode: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.agent.harness import AgentConfig, AgentHarness
    from src.agent.tools import create_default_registry
    from src.core.agent_runtime import AgentEventKind
    from src.llm import llm_client
    from src.llm.client import NativeChatResponse, NativeToolCall

    if planner_mode == "json":
        responses: Any = iter(
            [
                json.dumps(
                    {
                        "action": "tool_call",
                        "tool_name": "calculator",
                        "tool_params": {"expression": _CALCULATOR_ESCAPE},
                    }
                ),
                json.dumps({"action": "final_answer"}),
            ]
        )
        method = "chat_async"
    else:
        responses = iter(
            [
                NativeChatResponse(
                    content="",
                    tool_calls=(
                        NativeToolCall(
                            call_id="provider-call",
                            name="calculator",
                            arguments=json.dumps(
                                {"expression": _CALCULATOR_ESCAPE}
                            ),
                        ),
                    ),
                ),
                NativeChatResponse(content="READY_TO_ANSWER"),
            ]
        )
        method = "chat_with_tools_async"
    requests: list[list[dict[str, Any]]] = []

    async def chat(
        messages: list[dict[str, Any]],
        *_args: object,
        **_kwargs: object,
    ) -> Any:
        requests.append(json.loads(json.dumps(messages)))
        return next(responses)

    monkeypatch.setattr(llm_client, method, chat)

    async def chat_stream_async(*_args: object, **_kwargs: object):
        yield "safe result"

    monkeypatch.setattr(llm_client, "chat_stream_async", chat_stream_async)
    harness = AgentHarness(
        config=AgentConfig(planner_mode=planner_mode, verbose=False),
        tools=create_default_registry(),
    )

    async def exercise() -> list[Any]:
        return [
            event
            async for event in harness.events(
                f"calculator-{planner_mode}",
                "calculate the supplied expression",
            )
        ]

    events = asyncio.run(exercise())

    assert events[-1].kind is AgentEventKind.DONE
    assert events[-1].response is not None
    assert events[-1].response.tool_calls[0].success is False
    assert (
        events[-1].response.tool_calls[0].error_code
        == "calculator_expression_rejected"
    )
    assert str(os.getpid()) not in json.dumps(requests[-1])


def test_harness_passes_its_tool_call_identity_into_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.agent.harness import AgentConfig, AgentHarness
    from src.agent.tools import (
        SafetyLevel,
        ToolDef,
        ToolRegistry,
        current_tool_call_id,
    )
    from src.core.agent_runtime import AgentEventKind, ToolResult
    from src.llm import llm_client

    observed_call_ids: list[str | None] = []

    async def execute(params: dict[str, object]) -> ToolResult:
        del params
        observed_call_ids.append(current_tool_call_id())
        return ToolResult(success=True, data={"ok": True})

    registry = ToolRegistry(dedup_window=0)
    registry.register(
        ToolDef(
            "observe_identity",
            "observe identity",
            [],
            SafetyLevel.WHITELIST,
            lambda params: ToolResult(success=True),
            execute_async_fn=execute,
            max_retries=0,
        )
    )
    responses = iter(
        [
            json.dumps(
                {
                    "action": "tool_call",
                    "tool_name": "observe_identity",
                    "tool_params": {},
                }
            ),
            json.dumps({"action": "final_answer"}),
        ]
    )

    async def chat(*_args: object, **_kwargs: object) -> str:
        return next(responses)

    monkeypatch.setattr(llm_client, "chat_async", chat)

    async def chat_stream_async(*_args: object, **_kwargs: object):
        yield "done"

    monkeypatch.setattr(llm_client, "chat_stream_async", chat_stream_async)
    harness = AgentHarness(
        config=AgentConfig(planner_mode="json", verbose=False),
        tools=registry,
    )

    async def exercise() -> list[Any]:
        return [
            event
            async for event in harness.events("identity-session", "observe")
        ]

    events = asyncio.run(exercise())
    proposed = next(
        event.tool_call
        for event in events
        if event.kind is AgentEventKind.TOOL_CALL
    )
    completed = next(
        event.tool_result
        for event in events
        if event.kind is AgentEventKind.TOOL_RESULT
    )

    assert proposed is not None
    assert completed is not None
    assert observed_call_ids == [proposed.call_id]
    assert completed.call_id == proposed.call_id


def test_native_tool_exception_logs_and_audits_omit_exception_text(
    isolated_runtime: Path,
) -> None:
    from src.agent.tool_policy import ToolAccessPolicy
    from src.agent.tools import SafetyLevel, ToolDef, ToolRegistry
    from src.core.agent_runtime import ToolResult

    secret = "SYNTHETIC_NATIVE_EXCEPTION_SECRET"
    output: list[str] = []

    def fail_sync(params: dict[str, object]) -> ToolResult:
        del params
        raise RuntimeError(secret)

    async def fail_async(params: dict[str, object]) -> ToolResult:
        del params
        raise RuntimeError(secret)

    registry = ToolRegistry(
        dedup_window=0,
        policy=ToolAccessPolicy(allowlist=("fail_sync", "fail_async")),
    )
    registry.register(
        ToolDef(
            "fail_sync",
            "fail synchronously",
            [],
            SafetyLevel.WHITELIST,
            fail_sync,
            max_retries=0,
        )
    )
    registry.register(
        ToolDef(
            "fail_async",
            "fail asynchronously",
            [],
            SafetyLevel.WHITELIST,
            fail_sync,
            execute_async_fn=fail_async,
            max_retries=0,
        )
    )

    sink = logger.add(lambda message: output.append(str(message)))
    try:
        sync_result = registry.execute("fail_sync", {}, "sync-session")
        async_result = asyncio.run(
            registry.execute_async("fail_async", {}, "async-session")
        )
    finally:
        logger.remove(sink)

    assert sync_result.error_code == "tool_execution_failed"
    assert async_result.error_code == "tool_execution_failed"
    logs = "".join(output)
    audit = (isolated_runtime / "logs" / "audit.jsonl").read_text(
        encoding="utf-8"
    )
    assert secret not in logs
    assert secret not in audit
    assert "exception_type=RuntimeError" in logs
    assert "error_code=tool_execution_failed" in logs


def test_builtin_external_tools_do_not_return_exception_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import duckduckgo_search

    from src.agent.tools import create_default_registry

    secret = "SYNTHETIC_EXTERNAL_EXCEPTION_SECRET"

    def fail(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError(secret)

    class FailingSearch:
        def text(self, *_args: object, **_kwargs: object) -> None:
            fail()

    monkeypatch.setattr(urllib.request, "urlopen", fail)
    monkeypatch.setattr(duckduckgo_search, "DDGS", FailingSearch)
    registry = create_default_registry()
    weather = registry.get_tool("get_weather")
    search = registry.get_tool("search_web")
    assert weather is not None
    assert search is not None

    weather_result = weather.execute_fn({"city": "Beijing"})
    search_result = search.execute_fn({"query": "safe query"})

    assert weather_result.error_code == "weather_lookup_failed"
    assert search_result.error_code == "web_search_failed"
    assert secret not in repr(weather_result)
    assert secret not in repr(search_result)


def test_graylist_sync_failure_is_not_retried() -> None:
    from src.agent.tools import SafetyLevel, ToolDef, ToolRegistry
    from src.core.agent_runtime import ToolResult

    effects: list[str] = []

    def execute(params: dict[str, object]) -> ToolResult:
        del params
        effects.append("sent")
        raise RuntimeError("acknowledgement lost")

    registry = ToolRegistry(dedup_window=0)
    registry.register(
        ToolDef(
            "send_once",
            "send once",
            [],
            SafetyLevel.GRAYLIST,
            execute,
            max_retries=3,
        )
    )

    result = registry.execute("send_once", {}, "sync-graylist")

    assert result.error_code == "tool_execution_failed"
    assert effects == ["sent"]


def test_approved_graylist_async_failure_is_not_retried() -> None:
    from src.agent.tool_approval import ToolApprovalManager
    from src.agent.tools import SafetyLevel, ToolDef, ToolRegistry
    from src.core.agent_runtime import ToolResult

    effects: list[str] = []

    async def execute(params: dict[str, object]) -> ToolResult:
        del params
        effects.append("sent")
        raise RuntimeError("acknowledgement lost")

    async def exercise() -> ToolResult:
        approvals = ToolApprovalManager(timeout_seconds=1)
        registry = ToolRegistry(
            dedup_window=0,
            approval_manager=approvals,
        )
        registry.register(
            ToolDef(
                "send_once",
                "send once",
                [],
                SafetyLevel.GRAYLIST,
                lambda params: ToolResult(success=True),
                execute_async_fn=execute,
                max_retries=3,
            )
        )
        call = asyncio.create_task(
            registry.execute_async("send_once", {}, "async-graylist")
        )
        for _ in range(100):
            pending = await approvals.pending_for_session("async-graylist")
            if pending:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("Approval did not become pending")
        await approvals.respond(
            "async-graylist",
            pending[0]["id"],
            action="approve",
        )
        result = await call
        await approvals.close()
        return result

    result = asyncio.run(exercise())

    assert result.error_code == "tool_execution_failed"
    assert effects == ["sent"]


@pytest.mark.parametrize(
    "params",
    [
        {"\ud800": "value"},
        {"query": "\ud800"},
    ],
)
def test_approval_rejects_surrogate_snapshot_without_pending_or_side_effect(
    params: dict[str, str],
) -> None:
    from src.agent.tool_approval import ToolApprovalManager

    async def exercise() -> tuple[str, tuple[dict[str, object], ...]]:
        approvals = ToolApprovalManager(timeout_seconds=1)
        decision = await approvals.authorize(
            "surrogate-session",
            tool_name="surrogate-tool",
            source="native",
            provider=None,
            category="test",
            params=params,
        )
        pending = await approvals.pending_for_session("surrogate-session")
        await approvals.close()
        return decision.outcome, pending

    outcome, pending = asyncio.run(exercise())

    assert outcome == "unavailable"
    assert pending == ()


def test_cancelled_registry_call_is_audited_and_still_propagates(
    isolated_runtime: Path,
) -> None:
    from src.agent.tools import SafetyLevel, ToolDef, ToolParam, ToolRegistry
    from src.core.agent_runtime import ToolResult

    secret = "cancelled-parameter-secret"

    async def exercise() -> None:
        started = asyncio.Event()

        async def wait(params: dict[str, object]) -> ToolResult:
            del params
            started.set()
            await asyncio.Event().wait()
            return ToolResult(success=True)

        registry = ToolRegistry(dedup_window=0)
        registry.register(
            ToolDef(
                "wait",
                "wait",
                [ToolParam("value", "str", "value")],
                SafetyLevel.WHITELIST,
                lambda params: ToolResult(success=True),
                execute_async_fn=wait,
                max_retries=0,
            )
        )
        call = asyncio.create_task(
            registry.execute_async(
                "wait",
                {"value": secret},
                "cancel-session",
                call_id="agent-call-123",
            )
        )
        await started.wait()
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call

    asyncio.run(exercise())

    audit_text = (isolated_runtime / "logs" / "audit.jsonl").read_text(
        encoding="utf-8"
    )
    record = json.loads(audit_text)
    assert record["call_id"] == "agent-call-123"
    assert record["tool_name"] == "wait"
    assert record["parameter_names"] == ["value"]
    assert record["success"] is False
    assert record["error_code"] == "tool_execution_cancelled"
    assert secret not in audit_text


def test_cancelled_approval_is_audited_without_executing(
    isolated_runtime: Path,
) -> None:
    from src.agent.tool_approval import ToolApprovalManager
    from src.agent.tools import SafetyLevel, ToolDef, ToolRegistry
    from src.core.agent_runtime import ToolResult

    effects: list[str] = []

    async def exercise() -> None:
        approvals = ToolApprovalManager(timeout_seconds=1)
        registry = ToolRegistry(
            dedup_window=0,
            approval_manager=approvals,
        )

        def execute(params: dict[str, object]) -> ToolResult:
            del params
            effects.append("executed")
            return ToolResult(success=True)

        registry.register(
            ToolDef(
                "approval_wait",
                "approval wait",
                [],
                SafetyLevel.GRAYLIST,
                execute,
                max_retries=3,
            )
        )
        call = asyncio.create_task(
            registry.execute_async(
                "approval_wait",
                {},
                "approval-cancel-session",
                call_id="approval-call-123",
            )
        )
        for _ in range(100):
            pending = await approvals.pending_for_session(
                "approval-cancel-session"
            )
            if pending:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("Approval did not become pending")
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        assert await approvals.pending_for_session(
            "approval-cancel-session"
        ) == ()
        await approvals.close()

    asyncio.run(exercise())

    audit_text = (isolated_runtime / "logs" / "audit.jsonl").read_text(
        encoding="utf-8"
    )
    record = json.loads(audit_text)
    assert record["call_id"] == "approval-call-123"
    assert record["error_code"] == "tool_execution_cancelled"
    assert effects == []
