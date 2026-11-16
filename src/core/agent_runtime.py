"""Typed interface and events for one bounded Agent execution."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, Protocol


class AgentEventKind(StrEnum):
    """Observable stages emitted by an Agent runtime."""

    PLANNING = "planning"
    DECISION = "decision"
    MODEL_CONTEXT = "model_context"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    CHUNK = "chunk"
    DONE = "done"
    ERROR = "error"


@dataclass
class ToolResult:
    """Result returned by a validated tool invocation."""

    success: bool
    call_id: str = ""
    data: Any = None
    error: str = ""
    error_code: str = ""
    tool_name: str = ""
    latency_ms: float = 0.0
    public_summary: str | None = None
    execution_status: str = "unknown"
    attempts: int = 0
    observation: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolCall:
    """Stable record of one proposed or completed tool invocation."""

    call_id: str
    name: str
    params: dict[str, Any]
    success: bool | None = None
    blocked: bool = False
    reason: str = ""
    error_code: str = ""
    latency_ms: float = 0.0
    status: str = "proposed"
    attempts: int = 0

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "call_id": self.call_id,
            "tool": self.name,
            "params": dict(self.params),
        }
        if self.success is not None:
            payload["success"] = self.success
        if self.blocked:
            payload["blocked"] = True
        if self.reason:
            payload["reason"] = self.reason
        if self.error_code:
            payload["error_code"] = self.error_code
        if self.latency_ms:
            payload["latency_ms"] = round(self.latency_ms, 1)
        payload["status"] = self.status
        payload["attempts"] = self.attempts
        return payload


@dataclass(frozen=True)
class AgentResponse:
    """Final result shared by ordinary and streaming Agent callers."""

    session_id: str
    answer: str
    tool_calls: tuple[ToolCall, ...] = ()
    iterations: int = 0
    total_latency_ms: float = 0.0
    error_code: str = ""
    error: str = ""
    trace_id: str = ""
    execution_id: str = ""
    termination_reason: str = ""
    first_answer_token_ms: float | None = None
    usage: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "answer": self.answer,
            "tool_calls": [call.to_dict() for call in self.tool_calls],
            "iterations": self.iterations,
            "latency_ms": round(self.total_latency_ms, 1),
            "error_code": self.error_code,
            "error": self.error,
            "trace_id": self.trace_id,
            "execution_id": self.execution_id,
            "termination_reason": self.termination_reason,
            "first_answer_token_ms": self.first_answer_token_ms,
            "usage": self.usage,
        }


@dataclass(frozen=True)
class AgentEvent:
    """One typed event from the shared Agent execution loop."""

    kind: AgentEventKind
    iteration: int = 0
    tool_call: ToolCall | None = None
    tool_result: ToolResult | None = None
    chunk: str = ""
    response: AgentResponse | None = None
    error_code: str = ""
    message: str = ""
    observation: dict[str, Any] = field(default_factory=dict)
    telemetry: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {**self._public_payload(), **self.telemetry}

    def to_record(self) -> dict[str, Any]:
        """Detailed local evaluation record; public SSE uses to_dict instead."""
        record = self.to_dict()
        if self.observation:
            record['observation'] = self.observation
        if self.tool_result:
            record['tool_result'] = asdict(self.tool_result)
        if self.response and self.kind is AgentEventKind.ERROR:
            record['response'] = self.response.to_dict()
        return record

    def _public_payload(self) -> dict[str, Any]:
        if self.kind in {AgentEventKind.DECISION, AgentEventKind.MODEL_CONTEXT}:
            return {"step": self.kind.value, "iteration": self.iteration}
        if self.kind is AgentEventKind.PLANNING:
            return {"step": "planning", "iteration": self.iteration}
        if self.kind is AgentEventKind.TOOL_CALL and self.tool_call:
            return {
                "step": "tool_call",
                "iteration": self.iteration,
                "tool": self.tool_call.name,
                "params": dict(self.tool_call.params),
                "call_id": self.tool_call.call_id,
            }
        if self.kind is AgentEventKind.TOOL_RESULT and self.tool_result:
            return {
                "step": "tool_done",
                "iteration": self.iteration,
                "tool": self.tool_result.tool_name,
                "call_id": self.tool_result.call_id,
                "success": self.tool_result.success,
                "error_code": self.tool_result.error_code,
                "execution_status": self.tool_result.execution_status,
                "attempts": self.tool_result.attempts,
                "summary": self.tool_result.public_summary[:500] if self.tool_result.success and self.tool_result.public_summary else None,
            }
        if self.kind is AgentEventKind.CHUNK:
            return {"chunk": self.chunk}
        if self.kind is AgentEventKind.DONE and self.response:
            return {"done": True, **self.response.to_dict()}
        payload = {
            "error": self.message,
            "code": self.error_code or "agent_failed",
        }
        if self.response:
            payload.update({key: value for key, value in self.response.to_dict().items()
                            if key not in {'session_id', 'answer', 'error'}})
        return payload


class AgentRuntime(Protocol):
    """Small seam for bounded Agent execution and event streaming."""

    async def execute(
        self, session_id: str, user_message: str
    ) -> AgentResponse: ...

    def events(
        self, session_id: str, user_message: str
    ) -> AsyncIterator[AgentEvent]: ...

    def reset_session(self, session_id: str) -> bool:
        """Delete one Agent session and report whether it existed."""
        ...

