"""Session-scoped, in-process approval coordination for tool execution."""

from __future__ import annotations

import asyncio
import json
import math
import uuid
from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass
from typing import Literal, TypedDict

from config.settings import settings

ToolApprovalAction = Literal["approve", "reject", "cancel"]
ToolApprovalOutcome = Literal[
    "approved",
    "rejected",
    "cancelled",
    "timed_out",
    "unavailable",
]
_PendingState = Literal[
    "waiting",
    "approved",
    "rejected",
    "cancelled",
    "timed_out",
    "closed",
]
MAX_PENDING_TOOL_APPROVALS = 128
MAX_PENDING_TOOL_APPROVALS_PER_SESSION = 8
MAX_TOOL_APPROVAL_PARAMS_BYTES = 64 * 1024
MAX_TOOL_APPROVAL_RESPONSE_BODY_BYTES = 4 * 1024
MAX_TOOL_APPROVAL_PARAM_FIELDS = 64
MAX_TOOL_APPROVAL_PARAM_LIST_ITEMS = 64
MAX_TOOL_APPROVAL_PARAM_NODES = 512
MAX_TOOL_APPROVAL_PARAM_DEPTH = 8
MAX_TOOL_APPROVAL_PARAM_KEY_BYTES = 256
MAX_TOOL_APPROVAL_PARAM_STRING_BYTES = 8 * 1024
MAX_TOOL_APPROVAL_INTEGER_ABS = 2**63 - 1
MAX_TOOL_APPROVAL_METADATA_LENGTH = 512


class ToolApprovalSnapshot(TypedDict):
    """Caller-visible projection of one pending tool approval."""

    id: str
    tool_name: str
    source: Literal["native", "mcp"]
    provider: str | None
    category: str
    params: dict[str, object]


class ToolApprovalNotPending(LookupError):
    """Raised when a caller cannot access one pending approval."""


class _ToolApprovalSnapshotInvalid(ValueError):
    """Raised when untrusted approval display data exceeds Host bounds."""


@dataclass(frozen=True)
class ToolApprovalDecision:
    """Terminal approval outcome plus the exact approved argument snapshot."""

    outcome: ToolApprovalOutcome
    params: dict[str, object] | None = None


def _bounded_params_snapshot(params: dict[str, object]) -> dict[str, object]:
    """Copy JSON-like arguments while enforcing Host-owned display limits."""
    nodes = 0

    def copy_value(value: object, depth: int) -> object:
        nonlocal nodes
        nodes += 1
        if nodes > MAX_TOOL_APPROVAL_PARAM_NODES:
            raise _ToolApprovalSnapshotInvalid("too many parameter values")
        if depth > MAX_TOOL_APPROVAL_PARAM_DEPTH:
            raise _ToolApprovalSnapshotInvalid("parameters are too deeply nested")
        if isinstance(value, dict):
            if len(value) > MAX_TOOL_APPROVAL_PARAM_FIELDS:
                raise _ToolApprovalSnapshotInvalid("too many parameter fields")
            copied: dict[str, object] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise _ToolApprovalSnapshotInvalid("parameter key is not text")
                if len(key) > MAX_TOOL_APPROVAL_PARAM_KEY_BYTES or len(
                    key.encode("utf-8")
                ) > MAX_TOOL_APPROVAL_PARAM_KEY_BYTES:
                    raise _ToolApprovalSnapshotInvalid("parameter key is too large")
                copied[key] = copy_value(item, depth + 1)
            return copied
        if isinstance(value, list):
            if len(value) > MAX_TOOL_APPROVAL_PARAM_LIST_ITEMS:
                raise _ToolApprovalSnapshotInvalid("parameter list is too large")
            return [copy_value(item, depth + 1) for item in value]
        if isinstance(value, str):
            if len(value) > MAX_TOOL_APPROVAL_PARAM_STRING_BYTES or len(
                value.encode("utf-8")
            ) > MAX_TOOL_APPROVAL_PARAM_STRING_BYTES:
                raise _ToolApprovalSnapshotInvalid("parameter text is too large")
            return value
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, int):
            if abs(value) > MAX_TOOL_APPROVAL_INTEGER_ABS:
                raise _ToolApprovalSnapshotInvalid("parameter integer is too large")
            return value
        if isinstance(value, float) and math.isfinite(value):
            return value
        raise _ToolApprovalSnapshotInvalid("parameter value is not safe JSON")

    copied = copy_value(params, 0)
    if not isinstance(copied, dict):
        raise _ToolApprovalSnapshotInvalid("parameters are not an object")
    try:
        encoded = json.dumps(
            copied,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (RecursionError, TypeError, ValueError) as exc:
        raise _ToolApprovalSnapshotInvalid(
            "parameters are not safe JSON"
        ) from exc
    if len(encoded) > MAX_TOOL_APPROVAL_PARAMS_BYTES:
        raise _ToolApprovalSnapshotInvalid("parameters exceed the size limit")
    return copied


@dataclass
class _PendingApproval:
    session_id: str
    snapshot: ToolApprovalSnapshot
    result: asyncio.Future[ToolApprovalDecision]
    state: _PendingState = "waiting"


class ToolApprovalManager:
    """Hide pending ownership, timeout, and terminal-state competition."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 25.0,
        max_pending: int = MAX_PENDING_TOOL_APPROVALS,
        max_pending_per_session: int = MAX_PENDING_TOOL_APPROVALS_PER_SESSION,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("Tool approval timeout must be positive")
        if max_pending < 1 or max_pending_per_session < 1:
            raise ValueError("Tool approval capacity must be positive")
        self._timeout_seconds = timeout_seconds
        self._max_pending = max_pending
        self._max_pending_per_session = max_pending_per_session
        self._lock = asyncio.Lock()
        self._pending: dict[str, _PendingApproval] = {}
        self._pending_by_session: dict[str, set[str]] = defaultdict(set)
        self._closed = False

    async def authorize(
        self,
        session_id: str,
        *,
        tool_name: str,
        source: Literal["native", "mcp"],
        provider: str | None,
        category: str,
        params: dict[str, object],
    ) -> ToolApprovalDecision:
        """Wait for one caller decision before the tool may execute."""
        try:
            if any(
                len(value) > MAX_TOOL_APPROVAL_METADATA_LENGTH
                for value in (session_id, tool_name, category, provider or "")
            ):
                raise _ToolApprovalSnapshotInvalid(
                    "approval metadata is too large"
                )
            safe_params = _bounded_params_snapshot(params)
        except _ToolApprovalSnapshotInvalid:
            return ToolApprovalDecision("unavailable")
        async with self._lock:
            if self._closed:
                return ToolApprovalDecision("cancelled")
            if (
                len(self._pending) >= self._max_pending
                or len(self._pending_by_session.get(session_id, ()))
                >= self._max_pending_per_session
            ):
                return ToolApprovalDecision("unavailable")
            approval_id = uuid.uuid4().hex
            pending = _PendingApproval(
                session_id=session_id,
                snapshot={
                    "id": approval_id,
                    "tool_name": tool_name,
                    "source": source,
                    "provider": provider,
                    "category": category,
                    "params": safe_params,
                },
                result=asyncio.get_running_loop().create_future(),
            )
            self._pending[approval_id] = pending
            self._pending_by_session[session_id].add(approval_id)

        timeout_task = asyncio.create_task(
            self._timeout_pending(approval_id, pending)
        )
        try:
            return await asyncio.shield(pending.result)
        except asyncio.CancelledError:
            async with self._lock:
                self._finish_pending_locked(
                    approval_id,
                    pending,
                    state="cancelled",
                    outcome="cancelled",
                )
            raise
        finally:
            timeout_task.cancel()
            try:
                await timeout_task
            except asyncio.CancelledError:
                pass

    async def pending_for_session(
        self,
        session_id: str,
    ) -> tuple[ToolApprovalSnapshot, ...]:
        """Return detached pending approvals visible to one Agent session."""
        async with self._lock:
            approval_ids = sorted(
                self._pending_by_session.get(session_id, ())
            )
            return tuple(
                deepcopy(self._pending[approval_id].snapshot)
                for approval_id in approval_ids
                if approval_id in self._pending
            )

    async def respond(
        self,
        session_id: str,
        approval_id: str,
        *,
        action: ToolApprovalAction,
    ) -> None:
        """Commit one caller decision without exposing another session."""
        outcome_by_action: dict[ToolApprovalAction, ToolApprovalOutcome] = {
            "approve": "approved",
            "reject": "rejected",
            "cancel": "cancelled",
        }
        async with self._lock:
            pending = self._pending.get(approval_id)
            if pending is None or pending.session_id != session_id:
                raise ToolApprovalNotPending()
            outcome = outcome_by_action[action]
            if not self._finish_pending_locked(
                approval_id,
                pending,
                state=outcome,
                outcome=outcome,
            ):
                raise ToolApprovalNotPending()

    async def close(self) -> None:
        """Cancel pending approvals and reject future requests."""
        async with self._lock:
            self._closed = True
            for approval_id, pending in tuple(self._pending.items()):
                self._finish_pending_locked(
                    approval_id,
                    pending,
                    state="closed",
                    outcome="cancelled",
                )

    async def open(self) -> None:
        """Allow approvals for a newly started application lifecycle."""
        async with self._lock:
            self._closed = False

    async def _timeout_pending(
        self,
        approval_id: str,
        pending: _PendingApproval,
    ) -> None:
        await asyncio.sleep(self._timeout_seconds)
        async with self._lock:
            self._finish_pending_locked(
                approval_id,
                pending,
                state="timed_out",
                outcome="timed_out",
            )

    def _finish_pending_locked(
        self,
        approval_id: str,
        pending: _PendingApproval,
        *,
        state: _PendingState,
        outcome: ToolApprovalOutcome,
    ) -> bool:
        if (
            self._pending.get(approval_id) is not pending
            or pending.state != "waiting"
        ):
            return False
        pending.state = state
        self._pending.pop(approval_id)
        session_pending = self._pending_by_session[pending.session_id]
        session_pending.discard(approval_id)
        if not session_pending:
            self._pending_by_session.pop(pending.session_id, None)
        approved_params = (
            pending.snapshot["params"] if outcome == "approved" else None
        )
        pending.result.set_result(
            ToolApprovalDecision(outcome, approved_params)
        )
        return True


tool_approval_manager = ToolApprovalManager(
    timeout_seconds=settings.agent_tool_approval_timeout_seconds
)


async def open_tool_approval_manager() -> None:
    """Open the shared manager for one application lifecycle."""
    await tool_approval_manager.open()


async def close_tool_approval_manager() -> None:
    """Close the shared manager and cancel all pending approvals."""
    await tool_approval_manager.close()
