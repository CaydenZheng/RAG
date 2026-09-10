"""Cross-cutting capacity, deadline, and public error handling for RAG requests."""

import asyncio
import json
import secrets
import threading
from collections.abc import Awaitable, Callable
from typing import Any

from config.settings import settings
from src.api.public_errors import public_error
from src.api.streaming import answer_event, encode_sse_event


class RequestGate:
    """Cross-event-loop, fail-fast concurrency gate."""

    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        self._capacity = capacity
        self._active = 0
        self._lock = threading.Lock()

    def try_acquire(self) -> bool:
        with self._lock:
            if self._active >= self._capacity:
                return False
            self._active += 1
            return True

    def release(self) -> None:
        with self._lock:
            if self._active < 1:
                raise RuntimeError("request gate released without acquisition")
            self._active -= 1


class RAGRequestReliabilityMiddleware:
    """Apply one deadline and capacity limit to complete RAG HTTP responses."""

    _PATHS = frozenset({"/query", "/query/stream"})

    def __init__(
        self,
        app: Callable[..., Awaitable[None]],
        *,
        max_concurrent_queries: int | None = None,
        request_timeout_seconds: float | None = None,
    ) -> None:
        self.app = app
        self._gate = RequestGate(
            max_concurrent_queries or settings.max_concurrent_queries
        )
        self._timeout = (
            request_timeout_seconds or settings.request_timeout_seconds
        )

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[..., Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if scope["type"] != "http" or scope.get("path") not in self._PATHS:
            await self.app(scope, receive, send)
            return

        query_id = secrets.token_hex(6)
        scope.setdefault("state", {})["request_id"] = query_id
        if not self._gate.try_acquire():
            await self._send_json_error(
                send,
                status=503,
                query_id=query_id,
                code="query_capacity_exceeded",
            )
            return

        response_started = False

        async def track_response(message: dict[str, Any]) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            try:
                async with asyncio.timeout(self._timeout):
                    await self.app(scope, receive, track_response)
            except TimeoutError:
                if response_started and scope.get("path") == "/query/stream":
                    await send(
                        {
                            "type": "http.response.body",
                            "body": encode_sse_event(
                                answer_event(
                                    "error",
                                    query_id,
                                    done=True,
                                    error=public_error("request_timeout"),
                                )
                            ).encode("utf-8"),
                            "more_body": False,
                        }
                    )
                elif not response_started:
                    await self._send_json_error(
                        send,
                        status=504,
                        query_id=query_id,
                        code="request_timeout",
                    )
        finally:
            self._gate.release()

    @staticmethod
    async def _send_json_error(
        send: Callable[[dict[str, Any]], Awaitable[None]],
        *,
        status: int,
        query_id: str,
        code: str,
    ) -> None:
        body = json.dumps(
            {
                "detail": public_error(code),
                "query_id": query_id,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"cache-control", b"no-store"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": body,
                "more_body": False,
            }
        )
