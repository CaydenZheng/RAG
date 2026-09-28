"""Bounded MCP tracing and protocol-event summaries."""

from __future__ import annotations

import hashlib
import math
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger
from mcp.client.session import LoggingFnT
from mcp.types import LoggingMessageNotificationParams

from config.settings import settings
from src.infra.jsonl import append_jsonl
from src.infra.tracer import TraceLogger, tracer

MAX_MCP_PROGRESS_EVENTS_PER_CALL = 32
MAX_MCP_SERVER_EVENTS_PER_WINDOW = 20
MCP_SERVER_EVENT_WINDOW_SECONDS = 60.0
_MAX_DROPPED_EVENT_COUNT = 1_000_000
_MAX_IDENTIFIER_CHARS = 160
_IDENTIFIER_DIGEST_CHARS = 24


@dataclass
class _RateWindow:
    started_at: float
    accepted: int = 0
    notice_emitted: bool = False


class MCPToolCallObserver:
    """Summarize one tool call without retaining peer-controlled messages."""

    def __init__(
        self,
        trace_logger: TraceLogger,
        *,
        server_id: str,
        transport: str,
        tool_name: str,
        progress_limit: int,
    ) -> None:
        self._trace_logger = trace_logger
        self._server_id = _bounded_identifier(server_id)
        self._transport = _bounded_identifier(transport)
        self._tool_name = _bounded_identifier(tool_name)
        self._progress_limit = progress_limit
        self._started_at = time.monotonic()
        self._progress_events = 0
        self._progress_events_dropped = 0
        self._invalid_progress_events = 0
        self._progress_messages_omitted = 0
        self._last_progress: float | None = None
        self._last_total: float | None = None
        self._finished = False

    async def on_progress(
        self,
        progress: float,
        total: float | None,
        message: str | None,
    ) -> None:
        """Accept a bounded numeric summary and discard the untrusted message."""
        if self._progress_events >= self._progress_limit:
            self._progress_events_dropped = min(
                self._progress_events_dropped + 1,
                _MAX_DROPPED_EVENT_COUNT,
            )
            return

        self._progress_events += 1
        if message is not None:
            self._progress_messages_omitted += 1
        if not _is_finite_number(progress) or (
            total is not None and not _is_finite_number(total)
        ):
            self._invalid_progress_events += 1
            return
        self._last_progress = float(progress)
        self._last_total = float(total) if total is not None else None

    def finish(self, *, status: str, error_code: str = "") -> None:
        """Append one request-scoped span at most once."""
        if self._finished:
            return
        self._finished = True
        attributes: dict[str, Any] = {
            "mcp_server_id": self._server_id,
            "mcp_transport": self._transport,
            "mcp_tool_name": self._tool_name,
            "progress_events": self._progress_events,
            "progress_events_dropped": self._progress_events_dropped,
            "invalid_progress_events": self._invalid_progress_events,
            "progress_messages_omitted": self._progress_messages_omitted,
        }
        if self._last_progress is not None:
            attributes["last_progress"] = self._last_progress
        if self._last_total is not None:
            attributes["last_total"] = self._last_total
        self._trace_logger.add_span(
            None,
            "mcp_tool_call",
            (time.monotonic() - self._started_at) * 1000,
            status=status,
            error_code=error_code,
            **attributes,
        )


class MCPServerLifecycleObserver:
    """Collect one bounded connection/discovery trace without binding context."""

    def __init__(
        self,
        trace_logger: TraceLogger,
        *,
        server_id: str,
        transport: str,
    ) -> None:
        self._trace_logger = trace_logger
        self._server_id = _bounded_identifier(server_id)
        self._transport = _bounded_identifier(transport)
        self._trace = trace_logger.start_trace(
            secrets.token_hex(6),
            "mcp_server_start",
        )
        self._finished = False

    def add_phase(
        self,
        name: str,
        started_at: float,
        *,
        status: str,
        error_code: str = "",
        **attributes: Any,
    ) -> None:
        self._trace_logger.add_span(
            self._trace,
            name,
            (time.monotonic() - started_at) * 1000,
            status=status,
            error_code=error_code,
            mcp_server_id=self._server_id,
            mcp_transport=self._transport,
            **attributes,
        )

    def finish(
        self,
        *,
        status: str,
        error_code: str = "",
        tool_count: int = 0,
        rejected_tool_count: int = 0,
    ) -> None:
        if self._finished:
            return
        self._finished = True
        try:
            self._trace_logger.finish_trace(
                self._trace,
                status=status,
                error_code=error_code,
                mcp_server_id=self._server_id,
                mcp_transport=self._transport,
                tool_count=max(0, tool_count),
                rejected_tool_count=max(0, rejected_tool_count),
            )
        except Exception as error:  # pragma: no cover - defensive I/O boundary
            logger.warning(
                "MCP lifecycle trace write failed: type={}",
                type(error).__name__,
            )


class MCPObservability:
    """Own bounded MCP event rate limits and trace adapters."""

    def __init__(
        self,
        *,
        trace_logger: TraceLogger | None = None,
        event_path: Path | None = None,
        max_events_per_window: int = MAX_MCP_SERVER_EVENTS_PER_WINDOW,
        window_seconds: float = MCP_SERVER_EVENT_WINDOW_SECONDS,
        progress_limit: int = MAX_MCP_PROGRESS_EVENTS_PER_CALL,
    ) -> None:
        if max_events_per_window < 1:
            raise ValueError("MCP event window limit must be positive")
        if window_seconds <= 0:
            raise ValueError("MCP event window duration must be positive")
        if progress_limit < 1:
            raise ValueError("MCP progress event limit must be positive")
        self._trace_logger = trace_logger or tracer
        self._event_path = event_path
        self._max_events_per_window = max_events_per_window
        self._window_seconds = window_seconds
        self._progress_limit = progress_limit
        self._rate_lock = threading.Lock()
        self._windows: dict[tuple[str, str], _RateWindow] = {}

    @property
    def event_path(self) -> Path:
        return self._event_path or settings.log_dir / "mcp_events.jsonl"

    def server_lifecycle(
        self,
        server_id: str,
        transport: str,
    ) -> MCPServerLifecycleObserver:
        return MCPServerLifecycleObserver(
            self._trace_logger,
            server_id=server_id,
            transport=transport,
        )

    def tool_call(
        self,
        server_id: str,
        transport: str,
        tool_name: str,
    ) -> MCPToolCallObserver:
        return MCPToolCallObserver(
            self._trace_logger,
            server_id=server_id,
            transport=transport,
            tool_name=tool_name,
            progress_limit=self._progress_limit,
        )

    def logging_callback(self, server_id: str) -> LoggingFnT:
        """Return an SDK callback that never retains Server log payloads."""

        async def record(params: LoggingMessageNotificationParams) -> None:
            self._record_limited_event(
                server_id,
                "server_log",
                level=_bounded_identifier(str(params.level)),
                payload_omitted=True,
            )

        return record

    def record_cancellation(self, server_id: str, tool_name: str) -> None:
        """Record the local cancellation that the SDK propagates to the Server."""
        self._record_limited_event(
            server_id,
            "tool_cancelled",
            tool_name=_bounded_identifier(tool_name),
        )

    def _record_limited_event(
        self,
        server_id: str,
        event_type: str,
        **fields: Any,
    ) -> None:
        safe_server_id = _bounded_identifier(server_id)
        safe_event_type = _bounded_identifier(event_type)
        key = (safe_server_id, safe_event_type)
        now = time.monotonic()
        with self._rate_lock:
            window = self._windows.get(key)
            if (
                window is None
                or now - window.started_at >= self._window_seconds
            ):
                window = _RateWindow(started_at=now)
                self._windows[key] = window
            if window.accepted < self._max_events_per_window:
                window.accepted += 1
                record = {
                    "event": safe_event_type,
                    "server_id": safe_server_id,
                    "recorded_at": time.time(),
                    **fields,
                }
            elif not window.notice_emitted:
                window.notice_emitted = True
                record = {
                    "event": "mcp_event_rate_limited",
                    "server_id": safe_server_id,
                    "source_event": safe_event_type,
                    "recorded_at": time.time(),
                }
            else:
                return

        try:
            written = append_jsonl(
                self.event_path,
                record,
                max_bytes=settings.agent_log_max_bytes,
                backup_count=settings.agent_log_backup_count,
                retention_seconds=settings.agent_log_retention_seconds,
            )
            if not written:
                logger.warning("MCP event record exceeded the file limit")
        except (OSError, TypeError, ValueError) as error:
            logger.warning(
                "MCP event log write failed: type={}",
                type(error).__name__,
            )


def _bounded_identifier(value: str) -> str:
    if len(value) <= _MAX_IDENTIFIER_CHARS:
        return value
    digest = hashlib.sha256(
        value.encode("utf-8", errors="surrogatepass")
    ).hexdigest()[:_IDENTIFIER_DIGEST_CHARS]
    suffix = f"#{digest}"
    prefix_length = _MAX_IDENTIFIER_CHARS - len(suffix)
    return f"{value[:prefix_length]}{suffix}"


def _is_finite_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


mcp_observability = MCPObservability()
