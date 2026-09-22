"""Bounded, in-process coordination for MCP Elicitation callbacks."""

from __future__ import annotations

import asyncio
import json
import math
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from ipaddress import ip_address
from typing import Literal, TypedDict
from urllib.parse import urlsplit

from mcp.client.session import ClientRequestContext
from mcp.types import (
    INTERNAL_ERROR,
    ElicitRequestFormParams,
    ElicitRequestParams,
    ElicitRequestURLParams,
    ElicitResult,
    ErrorData,
)

from src.agent.tool_schema import (
    InvalidToolSchema,
    ensure_safe_input_schema,
    validate_tool_arguments,
)

MCPElicitationAction = Literal["accept", "decline", "cancel"]
_PendingState = Literal[
    "waiting",
    "responded",
    "timed_out",
    "closed",
    "call_cancelled",
]
MCPElicitationCallback = Callable[
    [ClientRequestContext, ElicitRequestParams],
    Awaitable[ElicitResult | ErrorData],
]
MCPElicitationResponseValidator = Callable[
    [dict[str, object], object],
    str | None,
]
MAX_ELICITATION_MESSAGE_LENGTH = 4_000
MAX_ELICITATION_URL_LENGTH = 4_096
MAX_ELICITATION_SERVER_ID_LENGTH = 512
MAX_PENDING_ELICITATIONS = 128
MAX_PENDING_ELICITATIONS_PER_SESSION = 8
MAX_ELICITATION_RESPONSE_CONTENT_BYTES = 64 * 1024
MAX_ELICITATION_RESPONSE_BODY_BYTES = 128 * 1024
MAX_ELICITATION_RESPONSE_FIELDS = 64
MAX_ELICITATION_RESPONSE_KEY_BYTES = 256
MAX_ELICITATION_RESPONSE_STRING_BYTES = 8 * 1024
MAX_ELICITATION_RESPONSE_LIST_ITEMS = 64


class MCPElicitationSnapshot(TypedDict, total=False):
    """Caller-visible projection of one pending SDK request."""

    id: str
    server_id: str
    mode: Literal["form", "url"]
    message: str
    requested_schema: dict[str, object]
    url: str
    elicitation_id: str | None


class MCPElicitationNotPending(LookupError):
    """Raised when a caller cannot access the requested interaction."""


class MCPElicitationInvalidResponse(ValueError):
    """Raised when a caller response violates the Server request."""


class MCPElicitationResponseTooLarge(MCPElicitationInvalidResponse):
    """Raised when response content exceeds a Host-owned resource limit."""


def _validate_content_bounds(content: dict[str, object]) -> None:
    """Reject content that exceeds Host limits independent of Server Schema."""
    if len(content) > MAX_ELICITATION_RESPONSE_FIELDS:
        raise MCPElicitationResponseTooLarge(
            "Elicitation response has too many fields"
        )
    for key, value in content.items():
        if (
            not isinstance(key, str)
            or len(key.encode("utf-8")) > MAX_ELICITATION_RESPONSE_KEY_BYTES
        ):
            raise MCPElicitationResponseTooLarge(
                "Elicitation response key exceeds the size limit"
            )
        values = value if isinstance(value, list) else (value,)
        if isinstance(value, list) and len(value) > MAX_ELICITATION_RESPONSE_LIST_ITEMS:
            raise MCPElicitationResponseTooLarge(
                "Elicitation response list exceeds the item limit"
            )
        for item in values:
            if isinstance(item, str):
                if (
                    len(item.encode("utf-8"))
                    > MAX_ELICITATION_RESPONSE_STRING_BYTES
                ):
                    raise MCPElicitationResponseTooLarge(
                        "Elicitation response string exceeds the size limit"
                    )
            elif item is None or isinstance(item, bool | int):
                continue
            elif isinstance(item, float):
                if not math.isfinite(item):
                    raise MCPElicitationInvalidResponse(
                        "Elicitation response number must be finite"
                    )
            else:
                raise MCPElicitationInvalidResponse(
                    "Elicitation response contains an unsupported value"
                )
    try:
        encoded = json.dumps(
            content,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (RecursionError, TypeError, ValueError) as exc:
        raise MCPElicitationInvalidResponse(
            "Elicitation response is not valid JSON content"
        ) from exc
    if len(encoded) > MAX_ELICITATION_RESPONSE_CONTENT_BYTES:
        raise MCPElicitationResponseTooLarge(
            "Elicitation response exceeds the total size limit"
        )


@dataclass(frozen=True)
class _ActiveCall:
    token: str
    server_id: str
    session_id: str


@dataclass
class _PendingElicitation:
    call_token: str
    session_id: str
    snapshot: MCPElicitationSnapshot
    requested_schema: dict[str, object] | None
    result: asyncio.Future[ElicitResult]
    state: _PendingState = "waiting"


_ACTIVE_CALL_TOKEN: ContextVar[str | None] = ContextVar(
    "mcp_elicitation_active_call_token",
    default=None,
)


class MCPElicitationManager:
    """Translate SDK callbacks into session-scoped, short-lived interactions."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 25.0,
        response_validator: MCPElicitationResponseValidator = (
            validate_tool_arguments
        ),
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("Elicitation timeout must be positive")
        self._timeout_seconds = timeout_seconds
        self._response_validator = response_validator
        self._lock = asyncio.Lock()
        self._active_calls: dict[str, _ActiveCall] = {}
        self._active_by_server: dict[str, set[str]] = defaultdict(set)
        self._pending: dict[str, _PendingElicitation] = {}
        self._pending_by_session: dict[str, set[str]] = defaultdict(set)
        self._closed = False

    def callback_for(self, server_id: str) -> MCPElicitationCallback:
        """Build the official SDK callback bound to one configured Server."""

        async def callback(
            context: ClientRequestContext,
            params: ElicitRequestParams,
        ) -> ElicitResult | ErrorData:
            del context
            return await self._handle_request(server_id, params)

        return callback

    @asynccontextmanager
    async def active_call(
        self,
        server_id: str,
        session_id: str,
    ) -> AsyncIterator[None]:
        """Associate callbacks with the Agent session executing one MCP tool."""
        call = _ActiveCall(
            token=uuid.uuid4().hex,
            server_id=server_id,
            session_id=session_id,
        )
        async with self._lock:
            self._active_calls[call.token] = call
            self._active_by_server[server_id].add(call.token)
        context_token = _ACTIVE_CALL_TOKEN.set(call.token)
        try:
            yield
        finally:
            _ACTIVE_CALL_TOKEN.reset(context_token)
            await self._remove_active_call(call)

    async def pending_for_session(
        self,
        session_id: str,
    ) -> tuple[MCPElicitationSnapshot, ...]:
        """Return detached snapshots visible only to the owning session."""
        async with self._lock:
            pending_ids = sorted(self._pending_by_session.get(session_id, ()))
            return tuple(
                deepcopy(self._pending[pending_id].snapshot)
                for pending_id in pending_ids
                if pending_id in self._pending
            )

    async def respond(
        self,
        session_id: str,
        elicitation_id: str,
        *,
        action: MCPElicitationAction,
        content: dict[str, object] | None,
    ) -> None:
        """Validate and deliver one caller response without echoing its content."""
        async with self._lock:
            pending = self._pending.get(elicitation_id)
            if pending is None or pending.session_id != session_id:
                raise MCPElicitationNotPending()
            if action == "accept" and pending.requested_schema is not None:
                if content is None:
                    raise MCPElicitationInvalidResponse(
                        "Accepted form Elicitation requires content"
                    )
                requested_schema = pending.requested_schema
            elif content is not None:
                raise MCPElicitationInvalidResponse(
                    "Elicitation response content is not allowed"
                )
            else:
                requested_schema = None
            if requested_schema is None:
                if not self._finish_pending_locked(
                    elicitation_id,
                    pending,
                    state="responded",
                    result=ElicitResult(action=action, content=content),
                ):
                    raise MCPElicitationNotPending()
                return

        assert content is not None
        await asyncio.to_thread(
            self._validate_accepted_content,
            requested_schema,
            content,
        )
        async with self._lock:
            if not self._finish_pending_locked(
                elicitation_id,
                pending,
                state="responded",
                result=ElicitResult(action=action, content=content),
            ):
                raise MCPElicitationNotPending()

    def _validate_accepted_content(
        self,
        requested_schema: dict[str, object],
        content: dict[str, object],
    ) -> None:
        _validate_content_bounds(content)
        error = self._response_validator(requested_schema, content)
        if error is not None:
            raise MCPElicitationInvalidResponse(error)

    async def close(self) -> None:
        """Cancel every in-memory interaction during manager shutdown."""
        async with self._lock:
            self._closed = True
            for pending_id, pending in tuple(self._pending.items()):
                self._finish_pending_locked(
                    pending_id,
                    pending,
                    state="closed",
                    result=ElicitResult(action="cancel"),
                )

    async def open(self) -> None:
        """Allow callbacks for a newly started MCP Client lifecycle."""
        async with self._lock:
            self._closed = False

    async def _handle_request(
        self,
        server_id: str,
        params: ElicitRequestParams,
    ) -> ElicitResult | ErrorData:
        async with self._lock:
            if self._closed:
                return ErrorData(
                    code=INTERNAL_ERROR,
                    message="Elicitation manager is closed",
                )
            call = self._resolve_active_call(server_id)
            if call is None:
                return ErrorData(
                    code=INTERNAL_ERROR,
                    message="Elicitation cannot be associated with an active tool call",
                )
            try:
                snapshot, requested_schema = self._snapshot(server_id, params)
            except (InvalidToolSchema, TypeError, ValueError):
                return ErrorData(
                    code=INTERNAL_ERROR,
                    message="Elicitation request was rejected",
                )
            if (
                len(self._pending) >= MAX_PENDING_ELICITATIONS
                or len(self._pending_by_session.get(call.session_id, ()))
                >= MAX_PENDING_ELICITATIONS_PER_SESSION
            ):
                return ErrorData(
                    code=INTERNAL_ERROR,
                    message="Elicitation request capacity was exceeded",
                )
            pending_id = snapshot["id"]
            pending = _PendingElicitation(
                call_token=call.token,
                session_id=call.session_id,
                snapshot=snapshot,
                requested_schema=requested_schema,
                result=asyncio.get_running_loop().create_future(),
            )
            self._pending[pending_id] = pending
            self._pending_by_session[call.session_id].add(pending_id)

        timeout_task = asyncio.create_task(
            self._timeout_pending(pending_id, pending)
        )
        try:
            return await asyncio.shield(pending.result)
        except asyncio.CancelledError:
            async with self._lock:
                self._finish_pending_locked(
                    pending_id,
                    pending,
                    state="call_cancelled",
                    result=ElicitResult(action="cancel"),
                )
            raise
        finally:
            timeout_task.cancel()
            try:
                await timeout_task
            except asyncio.CancelledError:
                pass

    def _resolve_active_call(self, server_id: str) -> _ActiveCall | None:
        token = _ACTIVE_CALL_TOKEN.get()
        if token is not None:
            call = self._active_calls.get(token)
            if call is not None and call.server_id == server_id:
                return call
        server_calls = self._active_by_server.get(server_id, set())
        if len(server_calls) != 1:
            return None
        return self._active_calls.get(next(iter(server_calls)))

    @staticmethod
    def _snapshot(
        server_id: str,
        params: ElicitRequestParams,
    ) -> tuple[MCPElicitationSnapshot, dict[str, object] | None]:
        if (
            not params.message
            or len(params.message) > MAX_ELICITATION_MESSAGE_LENGTH
        ):
            raise ValueError("Elicitation message is invalid")
        pending_id = uuid.uuid4().hex
        if isinstance(params, ElicitRequestFormParams):
            schema = deepcopy(params.requested_schema)
            ensure_safe_input_schema(schema)
            return (
                {
                    "id": pending_id,
                    "server_id": server_id,
                    "mode": "form",
                    "message": params.message,
                    "requested_schema": schema,
                },
                schema,
            )
        if isinstance(params, ElicitRequestURLParams):
            MCPElicitationManager._validate_url(params.url)
            if (
                params.elicitation_id is not None
                and len(params.elicitation_id)
                > MAX_ELICITATION_SERVER_ID_LENGTH
            ):
                raise ValueError("Elicitation Server ID is too long")
            return (
                {
                    "id": pending_id,
                    "server_id": server_id,
                    "mode": "url",
                    "message": params.message,
                    "url": params.url,
                    "elicitation_id": params.elicitation_id,
                },
                None,
            )
        raise TypeError("Unsupported Elicitation request")

    @staticmethod
    def _validate_url(value: str) -> None:
        if len(value) > MAX_ELICITATION_URL_LENGTH:
            raise ValueError("Elicitation URL is too long")
        parsed = urlsplit(value)
        if (
            not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise ValueError("Elicitation URL is invalid")
        if parsed.scheme == "https":
            return
        if parsed.scheme != "http":
            raise ValueError("Elicitation URL must use HTTPS")
        try:
            is_loopback = ip_address(parsed.hostname).is_loopback
        except ValueError:
            is_loopback = False
        if not is_loopback:
            raise ValueError(
                "HTTP Elicitation URL must use a numeric loopback host"
            )

    async def _timeout_pending(
        self,
        pending_id: str,
        pending: _PendingElicitation,
    ) -> None:
        await asyncio.sleep(self._timeout_seconds)
        async with self._lock:
            self._finish_pending_locked(
                pending_id,
                pending,
                state="timed_out",
                result=ElicitResult(action="cancel"),
            )

    def _finish_pending_locked(
        self,
        pending_id: str,
        pending: _PendingElicitation,
        *,
        state: _PendingState,
        result: ElicitResult,
    ) -> bool:
        """Commit exactly one terminal outcome while the manager lock is held."""
        if (
            self._pending.get(pending_id) is not pending
            or pending.state != "waiting"
        ):
            return False
        pending.state = state
        self._pending.pop(pending_id)
        session_pending = self._pending_by_session[pending.session_id]
        session_pending.discard(pending_id)
        if not session_pending:
            self._pending_by_session.pop(pending.session_id, None)
        pending.result.set_result(result)
        return True

    async def _remove_active_call(self, call: _ActiveCall) -> None:
        async with self._lock:
            self._active_calls.pop(call.token, None)
            server_calls = self._active_by_server[call.server_id]
            server_calls.discard(call.token)
            if not server_calls:
                self._active_by_server.pop(call.server_id, None)
            for pending_id, pending in tuple(self._pending.items()):
                if pending.call_token == call.token:
                    self._finish_pending_locked(
                        pending_id,
                        pending,
                        state="call_cancelled",
                        result=ElicitResult(action="cancel"),
                    )
