"""Request-scoped structured tracing with conservative data redaction."""

from __future__ import annotations

import json
import re
import secrets
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Any, Iterator

from loguru import logger

TRACE_FILE = Path("logs") / "traces.jsonl"
_MAX_TEXT_LENGTH = 160
_SECRET_VALUE = re.compile(
    r"(?i)(?:bearer\s+\S+|sk-[a-z0-9_-]{8,}|api[_-]?key\s*[:=]\s*\S+)"
)
_SENSITIVE_KEYS = frozenset(
    {
        "answer",
        "authorization",
        "content",
        "cookie",
        "identity",
        "identity_scope",
        "message",
        "output",
        "params",
        "password",
        "prompt",
        "query",
        "result",
        "secret",
        "session",
        "session_id",
        "api_key",
        "access_token",
        "auth_token",
        "refresh_token",
    }
)
_SENSITIVE_SUFFIXES = tuple(f"_{key}" for key in _SENSITIVE_KEYS)


def _is_sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
    return normalized in _SENSITIVE_KEYS or normalized.endswith(
        _SENSITIVE_SUFFIXES
    )


def _safe_value(key: str, value: Any) -> Any:
    """Return bounded diagnostic data without request content or credentials."""
    if _is_sensitive_key(key):
        return "[REDACTED]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if _SECRET_VALUE.search(value):
            return "[REDACTED]"
        return value[:_MAX_TEXT_LENGTH]
    if isinstance(value, (list, tuple, set)):
        return [_safe_value(key, item) for item in list(value)[:20]]
    if isinstance(value, dict):
        return {
            str(item_key)[:64]: _safe_value(str(item_key), item_value)
            for item_key, item_value in list(value.items())[:30]
        }
    return type(value).__name__


def safe_attributes(values: dict[str, Any]) -> dict[str, Any]:
    """Sanitize a diagnostic mapping through the shared redaction policy."""
    return {key: _safe_value(key, value) for key, value in values.items()}


class TraceLogger:
    """Collect one bounded JSONL record behind a request-scoped interface."""

    def __init__(self, trace_file: Path | None = None) -> None:
        self._trace_file = trace_file
        self._current: ContextVar[dict[str, Any] | None] = ContextVar(
            f"request_trace_{id(self)}", default=None
        )
        self._stage: ContextVar[str] = ContextVar(
            f"request_trace_stage_{id(self)}", default="llm"
        )
        self._write_lock = threading.Lock()

    @property
    def trace_file(self) -> Path:
        return self._trace_file or TRACE_FILE

    def start_trace(
        self,
        request_id: str,
        operation: str,
        *,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a trace without retaining user input or identity data."""
        return {
            "request_id": request_id,
            "trace_id": trace_id or secrets.token_hex(16),
            "operation": operation,
            "spans": [],
            "started_at": time.perf_counter(),
            "status": "ok",
            "error_code": "",
            "index_version": "",
        }

    def bind(self, trace: dict[str, Any]) -> Token:
        return self._current.set(trace)

    def reset(self, token: Token) -> None:
        self._current.reset(token)

    @property
    def current(self) -> dict[str, Any] | None:
        return self._current.get()

    @property
    def current_stage(self) -> str:
        return self._stage.get()

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        token = self._stage.set(name)
        try:
            yield
        finally:
            self._stage.reset(token)

    def add_span(
        self,
        trace: dict[str, Any] | None,
        name: str,
        latency_ms: float = 0,
        *,
        status: str = "ok",
        error_code: str = "",
        **attributes: Any,
    ) -> None:
        target = trace or self.current
        if target is None:
            return
        span = {
            "name": name,
            "latency_ms": round(max(0.0, latency_ms), 2),
            "status": status,
            "attributes": safe_attributes(attributes),
        }
        if error_code:
            span["error_code"] = error_code
        target["spans"].append(span)

    def record_outcome(
        self,
        status: str,
        error_code: str = "",
        trace: dict[str, Any] | None = None,
    ) -> None:
        target = trace or self.current
        if target is not None:
            target["status"] = status
            target["error_code"] = _safe_value("error_code", error_code)

    def record_error(
        self, error_code: str, trace: dict[str, Any] | None = None
    ) -> None:
        self.record_outcome("error", error_code, trace)

    def set_index_version(
        self, version: str, trace: dict[str, Any] | None = None
    ) -> None:
        target = trace or self.current
        if target is not None:
            target["index_version"] = _safe_value("index_version", version)

    def finish_trace(
        self,
        trace: dict[str, Any],
        *,
        status: str | None = None,
        error_code: str = "",
        **metrics: Any,
    ) -> dict[str, Any]:
        if error_code:
            self.record_error(error_code, trace)
        if status is not None:
            trace["status"] = status
        record = {
            "request_id": trace["request_id"],
            "trace_id": trace["trace_id"],
            "operation": trace["operation"],
            "status": trace["status"],
            "error_code": trace["error_code"],
            "total_ms": round(
                max(0.0, (time.perf_counter() - trace["started_at"]) * 1000),
                2,
            ),
            "index_version": trace["index_version"] or "unavailable",
            "spans": trace["spans"],
            "metrics": safe_attributes(metrics),
        }
        path = self.trace_file
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(record, ensure_ascii=False, sort_keys=True)
        with self._write_lock, path.open("a", encoding="utf-8") as stream:
            stream.write(encoded + "\n")
        logger.info(
            "Trace written: request_id={}, trace_id={}, status={}, total_ms={:.1f}",
            record["request_id"],
            record["trace_id"],
            record["status"],
            record["total_ms"],
        )
        return record


tracer = TraceLogger()
