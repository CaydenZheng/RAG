"""Graylist tools require one session-scoped Host approval before execution."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient


class _CopyGuardBodyChunk:
    """Fail if the application copies a chunk known to exceed its budget."""

    def __init__(self, size: int) -> None:
        self._size = size

    def __len__(self) -> int:
        return self._size

    def __bool__(self) -> bool:
        return True

    def __iter__(self) -> object:
        raise AssertionError("Oversized ASGI body chunk was copied")


async def _asgi_post_chunks(
    application: Any,
    path: str,
    chunks: tuple[bytes | _CopyGuardBodyChunk, ...],
) -> tuple[int, dict[str, object]]:
    """Issue one streaming POST without a Content-Length header."""
    next_chunk = 0
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        nonlocal next_chunk
        if next_chunk >= len(chunks):
            return {"type": "http.disconnect"}
        chunk = chunks[next_chunk]
        next_chunk += 1
        return {
            "type": "http.request",
            "body": chunk,
            "more_body": next_chunk < len(chunks),
        }

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await application(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
            "root_path": "",
        },
        receive,
        send,
    )
    status = next(
        message["status"]
        for message in messages
        if message["type"] == "http.response.start"
    )
    response_body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    return status, json.loads(response_body)


def test_graylist_tool_waits_for_approval_before_side_effect() -> None:
    from src.agent.tool_approval import ToolApprovalManager
    from src.agent.tools import (
        SafetyLevel,
        ToolDef,
        ToolParam,
        ToolRegistry,
    )
    from src.core.agent_runtime import ToolResult

    side_effects: list[str] = []

    def execute(params: dict) -> ToolResult:
        side_effects.append(params["message"])
        return ToolResult(success=True, data={"sent": True})

    approvals = ToolApprovalManager(timeout_seconds=1)
    registry = ToolRegistry(
        dedup_window=0,
        approval_manager=approvals,
    )
    registry.register(
        ToolDef(
            name="send_message",
            description="Send one message",
            params=[ToolParam("message", "str", "Message")],
            safety_level=SafetyLevel.GRAYLIST,
            execute_fn=execute,
            category="external",
        )
    )

    async def exercise() -> tuple[object, tuple[dict[str, object], ...]]:
        call = asyncio.create_task(
            registry.execute_async(
                "send_message",
                {"message": "hello"},
                "session-a",
            )
        )
        for _ in range(100):
            pending = await approvals.pending_for_session("session-a")
            if pending:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("Approval did not become pending")
        assert side_effects == []
        await approvals.respond(
            "session-a",
            pending[0]["id"],
            action="approve",
        )
        result = await call
        await approvals.close()
        return result, pending

    result, pending = asyncio.run(exercise())

    assert pending == (
        {
            "id": pending[0]["id"],
            "tool_name": "send_message",
            "source": "native",
            "provider": None,
            "category": "external",
            "params": {"message": "hello"},
        },
    )
    assert result.success is True
    assert result.data == {"sent": True}
    assert side_effects == ["hello"]


def test_tool_executes_exact_approved_snapshot_after_caller_mutation() -> None:
    from src.agent.tool_approval import ToolApprovalManager
    from src.agent.tools import SafetyLevel, ToolDef, ToolParam, ToolRegistry
    from src.core.agent_runtime import ToolResult

    approvals = ToolApprovalManager(timeout_seconds=1)
    registry = ToolRegistry(dedup_window=0, approval_manager=approvals)
    executed: list[dict[str, object]] = []

    def execute(params: dict[str, object]) -> ToolResult:
        executed.append(params)
        return ToolResult(success=True)

    registry.register(
        ToolDef(
            name="send_message",
            description="Send one message",
            params=[ToolParam("message", "str", "Message")],
            safety_level=SafetyLevel.GRAYLIST,
            execute_fn=execute,
        )
    )
    original: dict[str, object] = {"message": "approved-value"}

    async def exercise() -> tuple[dict[str, object], object]:
        call = asyncio.create_task(
            registry.execute_async("send_message", original, "session-a")
        )
        await asyncio.sleep(0)
        pending = await approvals.pending_for_session("session-a")
        approved = pending[0]["params"]
        original["message"] = 123
        await approvals.respond(
            "session-a", pending[0]["id"], action="approve"
        )
        return approved, await call

    approved, result = asyncio.run(exercise())

    assert approved == {"message": "approved-value"}
    assert result.success is True
    assert executed == [{"message": "approved-value"}]
    assert executed[0] is not original


def test_nested_approved_snapshot_isolated_from_list_and_dict_mutation() -> None:
    from src.agent.tool_approval import ToolApprovalManager
    from src.agent.tools import SafetyLevel, ToolDef, ToolParam, ToolRegistry
    from src.core.agent_runtime import ToolResult

    approvals = ToolApprovalManager(timeout_seconds=1)
    registry = ToolRegistry(dedup_window=0, approval_manager=approvals)
    executed: list[dict[str, object]] = []
    registry.register(
        ToolDef(
            name="send_payload",
            description="Send nested payload",
            params=[ToolParam("payload", "dict", "Payload")],
            safety_level=SafetyLevel.GRAYLIST,
            execute_fn=lambda params: (
                executed.append(params) or ToolResult(success=True)
            ),
        )
    )
    original: dict[str, object] = {
        "payload": {
            "items": ["approved-item"],
            "metadata": {"approved": True},
        }
    }

    async def exercise() -> tuple[dict[str, object], object]:
        call = asyncio.create_task(
            registry.execute_async("send_payload", original, "session-a")
        )
        await asyncio.sleep(0)
        pending = await approvals.pending_for_session("session-a")
        approved = pending[0]["params"]
        payload = original["payload"]
        assert isinstance(payload, dict)
        items = payload["items"]
        metadata = payload["metadata"]
        assert isinstance(items, list)
        assert isinstance(metadata, dict)
        items.append("unapproved-item")
        metadata["approved"] = False
        await approvals.respond(
            "session-a", pending[0]["id"], action="approve"
        )
        return approved, await call

    approved, result = asyncio.run(exercise())

    expected = {
        "payload": {
            "items": ["approved-item"],
            "metadata": {"approved": True},
        }
    }
    assert approved == expected
    assert result.success is True
    assert executed == [expected]


@pytest.mark.parametrize(
    "lifecycle_change",
    ["unregister", "disable", "replace", "blacklist"],
)
def test_approved_tool_fails_closed_after_registry_lifecycle_change(
    lifecycle_change: str,
) -> None:
    from src.agent.tool_approval import ToolApprovalManager
    from src.agent.tools import SafetyLevel, ToolDef, ToolRegistry
    from src.core.agent_runtime import ToolResult

    approvals = ToolApprovalManager(timeout_seconds=1)
    registry = ToolRegistry(dedup_window=0, approval_manager=approvals)
    executions: list[str] = []

    def make_tool(label: str) -> ToolDef:
        return ToolDef(
            name="provider_action",
            description="Provider action",
            params=[],
            safety_level=SafetyLevel.GRAYLIST,
            execute_fn=lambda params: (
                executions.append(label) or ToolResult(success=True)
            ),
            max_retries=3,
            source="mcp",
            provider="remote",
        )

    original = make_tool("original")
    registry.register(original)

    async def exercise() -> object:
        call = asyncio.create_task(
            registry.execute_async("provider_action", {}, "session-a")
        )
        await asyncio.sleep(0)
        pending = await approvals.pending_for_session("session-a")
        if lifecycle_change == "unregister":
            registry.unregister("provider_action")
        elif lifecycle_change == "disable":
            original.available = False
        elif lifecycle_change == "replace":
            registry.unregister("provider_action")
            registry.register(make_tool("replacement"))
        else:
            original.safety_level = SafetyLevel.BLACKLIST
        await approvals.respond(
            "session-a", pending[0]["id"], action="approve"
        )
        return await call

    result = asyncio.run(exercise())

    assert result.success is False
    assert result.error_code == "tool_unavailable"
    assert executions == []


def test_approved_snapshot_revalidation_failure_never_executes_or_retries() -> None:
    from src.agent.tool_approval import ToolApprovalManager
    from src.agent.tools import SafetyLevel, ToolDef, ToolParam, ToolRegistry
    from src.core.agent_runtime import ToolResult

    approvals = ToolApprovalManager(timeout_seconds=1)
    registry = ToolRegistry(dedup_window=0, approval_manager=approvals)
    executions = 0
    message_param = ToolParam("message", "str", "Message")

    def execute(params: dict[str, object]) -> ToolResult:
        nonlocal executions
        del params
        executions += 1
        return ToolResult(success=True)

    registry.register(
        ToolDef(
            name="send_message",
            description="Send one message",
            params=[message_param],
            safety_level=SafetyLevel.GRAYLIST,
            execute_fn=execute,
            max_retries=3,
        )
    )

    async def exercise() -> object:
        call = asyncio.create_task(
            registry.execute_async(
                "send_message",
                {"message": "approved-value"},
                "session-a",
            )
        )
        await asyncio.sleep(0)
        pending = await approvals.pending_for_session("session-a")
        message_param.type = "int"
        await approvals.respond(
            "session-a", pending[0]["id"], action="approve"
        )
        return await call

    result = asyncio.run(exercise())

    assert result.success is False
    assert result.error_code == "invalid_tool_parameters"
    assert executions == 0


def test_rejected_approval_never_executes_or_retries_tool() -> None:
    from src.agent.tool_approval import ToolApprovalManager
    from src.agent.tools import SafetyLevel, ToolDef, ToolRegistry
    from src.core.agent_runtime import ToolResult

    side_effects = 0

    def execute(params: dict) -> ToolResult:
        nonlocal side_effects
        del params
        side_effects += 1
        raise RuntimeError("must not execute")

    approvals = ToolApprovalManager(timeout_seconds=1)
    registry = ToolRegistry(dedup_window=0, approval_manager=approvals)
    registry.register(
        ToolDef(
            name="dangerous",
            description="Dangerous action",
            params=[],
            safety_level=SafetyLevel.GRAYLIST,
            execute_fn=execute,
            max_retries=3,
        )
    )

    async def exercise() -> tuple[object, tuple[dict[str, object], ...]]:
        call = asyncio.create_task(
            registry.execute_async("dangerous", {}, "session-a")
        )
        for _ in range(100):
            pending = await approvals.pending_for_session("session-a")
            if pending:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("Approval did not become pending")
        await approvals.respond(
            "session-a",
            pending[0]["id"],
            action="reject",
        )
        result = await call
        remaining = await approvals.pending_for_session("session-a")
        await approvals.close()
        return result, remaining

    result, remaining = asyncio.run(exercise())

    assert result.success is False
    assert result.error_code == "tool_approval_rejected"
    assert side_effects == 0
    assert remaining == ()


def test_approval_timeout_cleans_pending_without_executing_tool() -> None:
    from src.agent.tool_approval import ToolApprovalManager
    from src.agent.tools import SafetyLevel, ToolDef, ToolRegistry
    from src.core.agent_runtime import ToolResult

    side_effects = 0

    def execute(params: dict) -> ToolResult:
        nonlocal side_effects
        del params
        side_effects += 1
        return ToolResult(success=True)

    approvals = ToolApprovalManager(timeout_seconds=0.01)
    registry = ToolRegistry(dedup_window=0, approval_manager=approvals)
    registry.register(
        ToolDef(
            name="dangerous",
            description="Dangerous action",
            params=[],
            safety_level=SafetyLevel.GRAYLIST,
            execute_fn=execute,
            max_retries=3,
        )
    )

    async def exercise() -> tuple[object, tuple[dict[str, object], ...]]:
        result = await registry.execute_async("dangerous", {}, "session-a")
        remaining = await approvals.pending_for_session("session-a")
        await approvals.close()
        return result, remaining

    result, remaining = asyncio.run(exercise())

    assert result.success is False
    assert result.error_code == "tool_approval_timeout"
    assert side_effects == 0
    assert remaining == ()


def test_agent_session_can_query_and_respond_to_tool_approval(
    monkeypatch: pytest.MonkeyPatch,
    isolated_runtime: Path,
) -> None:
    del isolated_runtime
    import tiktoken

    monkeypatch.setattr(
        tiktoken,
        "get_encoding",
        lambda name: SimpleNamespace(encode=lambda text: list(text)),
    )
    import app as api

    calls: list[tuple[object, ...]] = []

    class FakeApprovalManager:
        async def pending_for_session(
            self,
            session_id: str,
        ) -> tuple[dict[str, object], ...]:
            calls.append(("pending", session_id))
            return (
                {
                    "id": "approval-1",
                    "tool_name": "send_message",
                    "source": "native",
                    "provider": None,
                    "category": "external",
                    "params": {"message": "secret"},
                },
            )

        async def respond(
            self,
            session_id: str,
            approval_id: str,
            *,
            action: str,
        ) -> None:
            calls.append(("respond", session_id, approval_id, action))

    monkeypatch.setattr(
        api.tool_approval_module,
        "tool_approval_manager",
        FakeApprovalManager(),
    )
    client = TestClient(api.app)
    try:
        pending = client.get("/agent/approvals/chat-session")
        response = client.post(
            "/agent/approvals/chat-session/approval-1",
            json={"action": "approve"},
        )
    finally:
        client.close()

    assert pending.status_code == 200
    assert pending.headers["cache-control"] == "no-store"
    assert pending.json() == {
        "session_id": "chat-session",
        "pending": [
            {
                "id": "approval-1",
                "tool_name": "send_message",
                "source": "native",
                "provider": None,
                "category": "external",
                "params": {"message": "secret"},
            }
        ],
    }
    assert response.status_code == 202
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"status": "received"}
    assert "secret" not in response.text
    assert calls[0][0] == "pending"
    assert calls[1][0] == "respond"
    assert calls[0][1] == calls[1][1]
    assert calls[0][1] != "chat-session"


def test_tool_approval_response_rejects_oversized_chunk_before_copy(
    monkeypatch: pytest.MonkeyPatch,
    isolated_runtime: Path,
) -> None:
    del isolated_runtime
    import tiktoken

    monkeypatch.setattr(
        tiktoken,
        "get_encoding",
        lambda name: SimpleNamespace(encode=lambda text: list(text)),
    )
    import app as api

    status, payload = asyncio.run(
        _asgi_post_chunks(
            api.app,
            "/agent/approvals/chat-session/approval-1",
            (
                _CopyGuardBodyChunk(
                    api.MAX_TOOL_APPROVAL_RESPONSE_BODY_BYTES + 1
                ),
            ),
        )
    )

    assert status == 413
    assert payload["detail"]["code"] == "tool_approval_response_too_large"

    cumulative_status, cumulative_payload = asyncio.run(
        _asgi_post_chunks(
            api.app,
            "/agent/approvals/chat-session/approval-1",
            (
                b"x" * api.MAX_TOOL_APPROVAL_RESPONSE_BODY_BYTES,
                b"x",
            ),
        )
    )

    assert cumulative_status == 413
    assert (
        cumulative_payload["detail"]["code"]
        == "tool_approval_response_too_large"
    )


def test_cancelled_approval_never_executes_or_retries_tool() -> None:
    from src.agent.tool_approval import ToolApprovalManager
    from src.agent.tools import SafetyLevel, ToolDef, ToolRegistry
    from src.core.agent_runtime import ToolResult

    side_effects = 0

    def execute(params: dict) -> ToolResult:
        nonlocal side_effects
        del params
        side_effects += 1
        raise RuntimeError("must not execute")

    approvals = ToolApprovalManager(timeout_seconds=1)
    registry = ToolRegistry(dedup_window=0, approval_manager=approvals)
    registry.register(
        ToolDef(
            name="dangerous",
            description="Dangerous action",
            params=[],
            safety_level=SafetyLevel.GRAYLIST,
            execute_fn=execute,
            max_retries=3,
        )
    )

    async def exercise() -> object:
        call = asyncio.create_task(
            registry.execute_async("dangerous", {}, "session-a")
        )
        for _ in range(100):
            pending = await approvals.pending_for_session("session-a")
            if pending:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("Approval did not become pending")
        await approvals.respond(
            "session-a",
            pending[0]["id"],
            action="cancel",
        )
        result = await call
        assert await approvals.pending_for_session("session-a") == ()
        return result

    result = asyncio.run(exercise())

    assert result.success is False
    assert result.error_code == "tool_approval_cancelled"
    assert side_effects == 0


def test_other_client_cannot_read_or_respond_to_tool_approval(
    monkeypatch: pytest.MonkeyPatch,
    isolated_runtime: Path,
) -> None:
    del isolated_runtime
    import tiktoken

    monkeypatch.setattr(
        tiktoken,
        "get_encoding",
        lambda name: SimpleNamespace(encode=lambda text: list(text)),
    )
    import app as api
    from src.agent.tool_approval import ToolApprovalNotPending

    owner: str | None = None

    class FakeApprovalManager:
        async def pending_for_session(
            self,
            session_id: str,
        ) -> tuple[dict[str, object], ...]:
            nonlocal owner
            if owner is None:
                owner = session_id
            if session_id != owner:
                return ()
            return ({"id": "approval-1", "tool_name": "send_message"},)

        async def respond(
            self,
            session_id: str,
            approval_id: str,
            *,
            action: str,
        ) -> None:
            del approval_id, action
            if session_id != owner:
                raise ToolApprovalNotPending()

    monkeypatch.setattr(
        api.tool_approval_module,
        "tool_approval_manager",
        FakeApprovalManager(),
    )
    client_a = TestClient(api.app)
    client_b = TestClient(api.app)
    try:
        own = client_a.get("/agent/approvals/chat-session")
        foreign = client_b.get("/agent/approvals/chat-session")
        rejected = client_b.post(
            "/agent/approvals/chat-session/approval-1",
            json={"action": "approve"},
        )
    finally:
        client_a.close()
        client_b.close()

    assert own.json()["pending"] == [
        {"id": "approval-1", "tool_name": "send_message"}
    ]
    assert foreign.json()["pending"] == []
    assert rejected.status_code == 404
    assert rejected.headers["cache-control"] == "no-store"
    assert rejected.json()["detail"]["code"] == "tool_approval_not_pending"


def test_manager_close_is_terminal_until_reopened() -> None:
    from src.agent.tool_approval import ToolApprovalManager

    approvals = ToolApprovalManager(timeout_seconds=1)

    async def exercise() -> tuple[str, str, str]:
        first = asyncio.create_task(
            approvals.authorize(
                "session-a",
                tool_name="send_message",
                source="native",
                provider=None,
                category="external",
                params={},
            )
        )
        await asyncio.sleep(0)
        await approvals.close()
        closed_result = await first
        rejected_after_close = await approvals.authorize(
            "session-a",
            tool_name="send_message",
            source="native",
            provider=None,
            category="external",
            params={},
        )
        assert await approvals.pending_for_session("session-a") == ()

        await approvals.open()
        reopened = asyncio.create_task(
            approvals.authorize(
                "session-a",
                tool_name="send_message",
                source="native",
                provider=None,
                category="external",
                params={},
            )
        )
        await asyncio.sleep(0)
        pending = await approvals.pending_for_session("session-a")
        await approvals.respond(
            "session-a", pending[0]["id"], action="approve"
        )
        return (
            closed_result.outcome,
            rejected_after_close.outcome,
            (await reopened).outcome,
        )

    assert asyncio.run(exercise()) == ("cancelled", "cancelled", "approved")


def test_pending_capacity_and_parameter_snapshot_are_bounded() -> None:
    from src.agent.tool_approval import (
        MAX_TOOL_APPROVAL_PARAMS_BYTES,
        ToolApprovalManager,
    )

    approvals = ToolApprovalManager(
        timeout_seconds=1,
        max_pending=1,
        max_pending_per_session=1,
    )

    async def exercise() -> tuple[str, str]:
        first = asyncio.create_task(
            approvals.authorize(
                "session-a",
                tool_name="send_message",
                source="native",
                provider=None,
                category="external",
                params={"message": "bounded"},
            )
        )
        await asyncio.sleep(0)
        capacity_result = await approvals.authorize(
            "session-b",
            tool_name="send_message",
            source="native",
            provider=None,
            category="external",
            params={},
        )
        oversized_result = await approvals.authorize(
            "session-c",
            tool_name="send_message",
            source="native",
            provider=None,
            category="external",
            params={"message": "x" * (MAX_TOOL_APPROVAL_PARAMS_BYTES + 1)},
        )
        pending = await approvals.pending_for_session("session-a")
        await approvals.respond(
            "session-a", pending[0]["id"], action="cancel"
        )
        await first
        return capacity_result.outcome, oversized_result.outcome

    assert asyncio.run(exercise()) == ("unavailable", "unavailable")


def test_per_session_capacity_and_total_snapshot_size_are_bounded() -> None:
    from src.agent.tool_approval import (
        MAX_TOOL_APPROVAL_PARAM_STRING_BYTES,
        ToolApprovalManager,
    )

    approvals = ToolApprovalManager(
        timeout_seconds=1,
        max_pending=2,
        max_pending_per_session=1,
    )

    async def authorize(
        session_id: str,
        params: dict[str, object],
    ) -> str:
        decision = await approvals.authorize(
            session_id,
            tool_name="send_message",
            source="native",
            provider=None,
            category="external",
            params=params,
        )
        return decision.outcome

    async def exercise() -> tuple[str, str, str]:
        first = asyncio.create_task(authorize("session-a", {}))
        await asyncio.sleep(0)
        same_session = await authorize("session-a", {})
        second = asyncio.create_task(authorize("session-b", {}))
        await asyncio.sleep(0)
        total_too_large = await authorize(
            "session-c",
            {
                f"field-{index}": "x" * MAX_TOOL_APPROVAL_PARAM_STRING_BYTES
                for index in range(9)
            },
        )
        huge_integer = await authorize("session-c", {"value": 10**10_000})
        for session_id in ("session-a", "session-b"):
            pending = await approvals.pending_for_session(session_id)
            await approvals.respond(
                session_id, pending[0]["id"], action="cancel"
            )
        await asyncio.gather(first, second)
        return same_session, total_too_large, huge_integer

    assert asyncio.run(exercise()) == (
        "unavailable",
        "unavailable",
        "unavailable",
    )


def test_sync_graylist_execution_cannot_bypass_configured_approval() -> None:
    from src.agent.tool_approval import ToolApprovalManager
    from src.agent.tools import SafetyLevel, ToolDef, ToolRegistry
    from src.core.agent_runtime import ToolResult

    side_effects = 0

    def execute(params: dict) -> ToolResult:
        nonlocal side_effects
        del params
        side_effects += 1
        return ToolResult(success=True)

    registry = ToolRegistry(
        dedup_window=0,
        approval_manager=ToolApprovalManager(timeout_seconds=1),
    )
    registry.register(
        ToolDef(
            name="send_message",
            description="Send one message",
            params=[],
            safety_level=SafetyLevel.GRAYLIST,
            execute_fn=execute,
        )
    )

    result = registry.execute("send_message", {}, "session-a")

    assert result.success is False
    assert result.error_code == "tool_approval_required"
    assert side_effects == 0


def test_default_registry_and_app_lifecycle_share_approval_manager(
    monkeypatch: pytest.MonkeyPatch,
    isolated_runtime: Path,
) -> None:
    del isolated_runtime
    import tiktoken

    monkeypatch.setattr(
        tiktoken,
        "get_encoding",
        lambda name: SimpleNamespace(encode=lambda text: list(text)),
    )
    import app as api
    from src.agent.tool_approval import tool_approval_manager
    from src.agent.tools import create_default_registry

    registry = create_default_registry()

    assert registry._approval_manager is tool_approval_manager
    assert api.tool_approval_module.open_tool_approval_manager in (
        api.app.router.on_startup
    )
    assert api.tool_approval_module.close_tool_approval_manager in (
        api.app.router.on_shutdown
    )


def test_agent_loop_waits_for_graylist_approval_before_side_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.agent.harness import AgentConfig, AgentHarness
    from src.agent.hooks import HookPipeline
    from src.agent.memory import MemoryConfig, MemoryManager
    from src.agent.tool_approval import ToolApprovalManager
    from src.agent.tools import SafetyLevel, ToolDef, ToolParam, ToolRegistry
    from src.core.agent_runtime import ToolResult
    from src.infra.session_store import SessionStore

    approvals = ToolApprovalManager(timeout_seconds=1)
    registry = ToolRegistry(dedup_window=0, approval_manager=approvals)
    side_effects: list[str] = []
    registry.register(
        ToolDef(
            name="send_message",
            description="Send one message",
            params=[ToolParam("message", "str", "Message")],
            safety_level=SafetyLevel.GRAYLIST,
            execute_fn=lambda params: (
                side_effects.append(params["message"])
                or ToolResult(success=True, data={"sent": True})
            ),
            category="external",
        )
    )
    memory = MemoryManager(
        str(tmp_path / "memory"),
        config=MemoryConfig(compress_trigger_turns=100),
        store=SessionStore(str(tmp_path / "sessions.db")),
    )
    harness = AgentHarness(
        config=AgentConfig(verbose=False),
        memory=memory,
        tools=registry,
        hooks=HookPipeline(),
    )
    plans = iter(
        [
            {
                "action": "tool_call",
                "tool_name": "send_message",
                "tool_params": {"message": "hello"},
            },
            {"action": "final_answer", "answer": "sent"},
        ]
    )

    async def plan(messages: object, max_tokens: int | None = None) -> object:
        del messages, max_tokens
        return next(plans)

    monkeypatch.setattr(harness, "_plan_async", plan)

    async def exercise() -> object:
        run = asyncio.create_task(harness.execute("agent-session", "send it"))
        for _ in range(100):
            pending = await approvals.pending_for_session("agent-session")
            if pending:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("Agent tool approval did not become pending")
        assert side_effects == []
        await approvals.respond(
            "agent-session", pending[0]["id"], action="approve"
        )
        return await run

    response = asyncio.run(exercise())

    assert response.answer == "sent"
    assert response.error_code == ""
    assert side_effects == ["hello"]


def test_cancelled_call_cleans_pending_approval() -> None:
    from src.agent.tool_approval import ToolApprovalManager, ToolApprovalNotPending

    approvals = ToolApprovalManager(timeout_seconds=1)

    async def exercise() -> tuple[dict[str, object], ...]:
        call = asyncio.create_task(
            approvals.authorize(
                "session-a",
                tool_name="send_message",
                source="native",
                provider=None,
                category="external",
                params={},
            )
        )
        await asyncio.sleep(0)
        pending = await approvals.pending_for_session("session-a")
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        assert await approvals.pending_for_session("session-a") == ()
        with pytest.raises(ToolApprovalNotPending):
            await approvals.respond(
                "session-a", pending[0]["id"], action="approve"
            )
        return pending

    assert asyncio.run(exercise())


def test_timeout_and_response_compete_for_one_terminal_outcome() -> None:
    from src.agent.tool_approval import ToolApprovalManager, ToolApprovalNotPending

    async def one_race() -> None:
        approvals = ToolApprovalManager(timeout_seconds=0.005)
        call = asyncio.create_task(
            approvals.authorize(
                "session-a",
                tool_name="send_message",
                source="native",
                provider=None,
                category="external",
                params={},
            )
        )
        await asyncio.sleep(0)
        pending = await approvals.pending_for_session("session-a")
        await asyncio.sleep(0.005)
        try:
            await approvals.respond(
                "session-a", pending[0]["id"], action="approve"
            )
            response_won = True
        except ToolApprovalNotPending:
            response_won = False
        outcome = (await call).outcome
        assert (response_won, outcome) in {
            (True, "approved"),
            (False, "timed_out"),
        }
        assert await approvals.pending_for_session("session-a") == ()

    async def exercise() -> None:
        for _ in range(10):
            await one_race()

    asyncio.run(exercise())


def test_native_policy_levels_and_mcp_tools_use_one_approval_boundary() -> None:
    from src.agent.tool_approval import ToolApprovalManager
    from src.agent.tools import SafetyLevel, ToolDef, ToolRegistry
    from src.core.agent_runtime import ToolResult

    approvals = ToolApprovalManager(timeout_seconds=1)
    registry = ToolRegistry(dedup_window=0, approval_manager=approvals)
    executed: list[str] = []

    def tool(name: str, safety: SafetyLevel, *, source: str = "native") -> ToolDef:
        return ToolDef(
            name=name,
            description=name,
            params=[],
            safety_level=safety,
            execute_fn=lambda params: (
                executed.append(name) or ToolResult(success=True)
            ),
            source=source,
            provider="remote" if source == "mcp" else None,
        )

    registry.register(tool("read", SafetyLevel.WHITELIST))
    registry.register(tool("blocked", SafetyLevel.BLACKLIST))
    registry.register(tool("mcp__remote__send", SafetyLevel.GRAYLIST, source="mcp"))

    async def exercise() -> tuple[object, object, object, dict[str, object]]:
        allowed = await registry.execute_async("read", {}, "session-a")
        blocked = await registry.execute_async("blocked", {}, "session-a")
        mcp_call = asyncio.create_task(
            registry.execute_async("mcp__remote__send", {}, "session-a")
        )
        await asyncio.sleep(0)
        pending = await approvals.pending_for_session("session-a")
        assert executed == ["read"]
        await approvals.respond(
            "session-a", pending[0]["id"], action="approve"
        )
        return allowed, blocked, await mcp_call, pending[0]

    allowed, blocked, mcp_result, pending = asyncio.run(exercise())

    assert allowed.success is True
    assert blocked.error_code == "tool_blocked"
    assert mcp_result.success is True
    assert pending["source"] == "mcp"
    assert pending["provider"] == "remote"
    assert executed == ["read", "mcp__remote__send"]


def test_rejected_approval_audit_does_not_record_parameter_values(
    isolated_runtime: Path,
) -> None:
    from src.agent.tool_approval import ToolApprovalManager
    from src.agent.tools import SafetyLevel, ToolDef, ToolParam, ToolRegistry
    from src.core.agent_runtime import ToolResult

    approvals = ToolApprovalManager(timeout_seconds=1)
    registry = ToolRegistry(dedup_window=0, approval_manager=approvals)
    registry.register(
        ToolDef(
            name="send_message",
            description="Send one message",
            params=[ToolParam("message", "str", "Message")],
            safety_level=SafetyLevel.GRAYLIST,
            execute_fn=lambda params: ToolResult(success=True),
        )
    )

    async def exercise() -> object:
        call = asyncio.create_task(
            registry.execute_async(
                "send_message",
                {"message": "approval-secret-value"},
                "session-a",
            )
        )
        await asyncio.sleep(0)
        pending = await approvals.pending_for_session("session-a")
        await approvals.respond(
            "session-a", pending[0]["id"], action="reject"
        )
        return await call

    result = asyncio.run(exercise())
    audit_text = (isolated_runtime / "logs" / "audit.jsonl").read_text(
        encoding="utf-8"
    )
    audit_record = json.loads(audit_text)

    assert result.error_code == "tool_approval_rejected"
    assert audit_record["parameter_names"] == ["message"]
    assert audit_record["error_code"] == "tool_approval_rejected"
    assert "approval-secret-value" not in audit_text
