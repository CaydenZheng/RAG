"""Concurrent Registry reservations do not bypass safety or outlive cleanup."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest


def test_approval_claim_survives_window_and_capacity_pressure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.agent import tools
    from src.agent.tool_approval import ToolApprovalDecision

    now = 100.0
    entered, release = asyncio.Event(), asyncio.Event()
    approvals: list[str] = []
    executed: list[dict] = []

    class Approver:
        async def authorize(self, session: str, **kwargs: Any) -> ToolApprovalDecision:
            approvals.append(session)
            entered.set()
            await release.wait()
            return ToolApprovalDecision("approved", kwargs["params"])

    async def execute(params: dict) -> tools.ToolResult:
        executed.append(params)
        return tools.ToolResult(success=True)

    monkeypatch.setattr(tools.time, "time", lambda: now)
    registry = tools.ToolRegistry(max_sessions=1, approval_manager=Approver())
    registry.register(tools.ToolDef(
        "effect", "test", [], tools.SafetyLevel.GRAYLIST,
        execute_fn=lambda _params: tools.ToolResult(success=True), execute_async_fn=execute,
    ))

    async def exercise() -> None:
        nonlocal now
        first = asyncio.create_task(registry.execute_async("effect", {}, "session"))
        try:
            await entered.wait()
            now += 100
            duplicate = await registry.execute_async("effect", {}, "session")
            assert duplicate.error_code == "duplicate_tool_call"
            capacity = await registry.execute_async("effect", {}, "other")
            assert capacity.error_code == "tool_capacity_exceeded"
        finally:
            release.set()
            await first
        assert (await registry.execute_async("effect", {}, "session")).error_code == "duplicate_tool_call"
        now += 31
        assert (await registry.execute_async("effect", {}, "session")).success

    asyncio.run(exercise())
    assert approvals == ["session", "session"]
    assert len(executed) == 2


@pytest.mark.parametrize("outcome", ["success", "failure", "cancel", "timeout"])
def test_async_claim_is_released_after_each_terminal_outcome(outcome: str) -> None:
    from src.agent.tools import SafetyLevel, ToolDef, ToolRegistry, ToolResult

    entered = asyncio.Event()
    fail = True

    async def execute(_params: dict) -> ToolResult:
        entered.set()
        if fail and outcome == "failure":
            raise RuntimeError("synthetic-secret")
        if fail and outcome in {"cancel", "timeout"}:
            await asyncio.Event().wait()
        return ToolResult(success=True)

    registry = ToolRegistry(dedup_window=0, max_sessions=1)
    registry.register(ToolDef(
        "effect", "test", [], SafetyLevel.GRAYLIST,
        execute_fn=lambda _params: ToolResult(success=True), execute_async_fn=execute,
    ))

    async def exercise() -> None:
        nonlocal fail
        first = asyncio.create_task(registry.execute_async("effect", {}, "session"))
        await entered.wait()
        if outcome == "cancel":
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
        elif outcome == "timeout":
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(first, 0.01)
        else:
            assert (await first).success is (outcome == "success")
        fail = False
        assert (await registry.execute_async("effect", {}, "session")).success

    asyncio.run(exercise())


def test_sync_and_async_callers_share_atomic_claim() -> None:
    from src.agent.tools import (
        SafetyLevel,
        ToolDef,
        ToolParam,
        ToolRegistry,
        ToolResult,
    )

    entered, release = threading.Event(), threading.Event()
    count = 0

    def execute(_params: dict) -> ToolResult:
        nonlocal count
        count += 1
        entered.set()
        assert release.wait(2)
        return ToolResult(success=True)

    registry = ToolRegistry()
    registry.register(ToolDef(
        "effect", "test", [ToolParam("value", "int", "value")],
        SafetyLevel.GRAYLIST, execute_fn=execute,
    ))
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(registry.execute, "effect", {"value": 1}, "session")
        try:
            assert entered.wait(1)
            assert registry.execute("effect", {"value": 1}, "session").error_code == "duplicate_tool_call"
            assert asyncio.run(registry.execute_async("effect", {"value": 1}, "session")).error_code == "duplicate_tool_call"
        finally:
            release.set()
        assert first.result().success
    assert count == 1
    assert registry.execute("effect", {"value": 2}, "session").success
    assert registry.execute("effect", {"value": 1}, "other").success


@pytest.mark.parametrize("raises", [False, True])
def test_cancelled_sync_worker_keeps_reservation_until_actual_exit(raises: bool) -> None:
    from src.agent.tools import SafetyLevel, ToolDef, ToolRegistry, ToolResult

    entered, release = threading.Event(), threading.Event()
    count = 0

    def execute(_params: dict) -> ToolResult:
        nonlocal count
        count += 1
        entered.set()
        assert release.wait(2)
        if raises:
            raise RuntimeError("synthetic-secret")
        return ToolResult(success=True)

    registry = ToolRegistry(dedup_window=0)
    registry.register(ToolDef("effect", "test", [], SafetyLevel.GRAYLIST, execute_fn=execute))

    async def exercise() -> None:
        exceptions: list[dict] = []
        asyncio.get_running_loop().set_exception_handler(lambda _loop, context: exceptions.append(context))
        first = asyncio.create_task(registry.execute_async("effect", {}, "session"))
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            assert registry.execute("effect", {}, "session").error_code == "duplicate_tool_call"
            assert count == 1
        finally:
            release.set()
        # The worker's completion releases the reservation, not cancellation
        # of its waiter. Await the actual executor, including its callback.
        await asyncio.get_running_loop().shutdown_default_executor()
        await asyncio.sleep(0)
        registry.execute("effect", {}, "session")
        assert count == 2
        assert exceptions == []

    asyncio.run(exercise())
