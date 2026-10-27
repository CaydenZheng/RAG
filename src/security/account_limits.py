"""Single-process request limits; durable conservative model reservations."""

import hashlib
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from config.settings import settings
from src.security.request_authorization import current_authorization

MAX_LIMIT_SUBJECTS: int = 8192


def source_scope(source: str) -> str:
    return hashlib.sha256(source.encode()).hexdigest()


@dataclass
class _Window:
    started: float
    requests: int = 0
    active: int = 0


@dataclass
class AccountRequestLimits:
    _subjects: dict[str, _Window] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def acquire(self, user: str, source: str) -> bool:
        now: float = time.monotonic()
        with self._lock:
            for key in list(self._subjects):
                if (
                    self._subjects[key].active == 0
                    and now - self._subjects[key].started >= 60
                ):
                    del self._subjects[key]
            keys: tuple[str, str] = ("user:" + user, "source:" + source)
            if (
                len(self._subjects) + sum(key not in self._subjects for key in keys)
                > MAX_LIMIT_SUBJECTS
            ):
                return False
            windows: list[_Window] = [
                self._subjects.setdefault(key, _Window(now)) for key in keys
            ]
            for window, rate, concurrency in zip(
                windows,
                (
                    settings.account_requests_per_minute,
                    settings.source_requests_per_minute,
                ),
                (settings.account_max_concurrent, settings.source_max_concurrent),
                strict=True,
            ):
                if now - window.started >= 60:
                    window.started, window.requests = now, 0
                if window.requests >= rate or window.active >= concurrency:
                    return False
            for window in windows:
                window.requests += 1
                window.active += 1
            return True

    def release(self, user: str, source: str) -> None:
        with self._lock:
            for key in ("user:" + user, "source:" + source):
                self._subjects[key].active -= 1


def reserve_model_call(
    messages: list[dict[str, Any]],
    max_tokens: int | None,
    tools: list[dict[str, Any]] | None = None,
) -> int | None:
    authorization = current_authorization()
    if authorization is None:
        return max_tokens
    import json

    from src.infra.session_store import session_store

    output: int = max_tokens if max_tokens is not None else 1024
    # UTF-8 bytes are a conservative text token reservation, not billing data.
    units: int = (
        len(json.dumps([messages, tools or []], ensure_ascii=False).encode()) + output
    ) * (settings.llm_max_retries + 1)
    session_store.reserve_model_units(units)
    return output
