"""ASGI request tracing for the RAG and Agent execution paths."""

import asyncio
import json
import secrets
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

from src.api.public_errors import public_error
from src.infra.tracer import tracer


class RequestTracingMiddleware:
    """Bind one trace to a complete HTTP response, including SSE streaming."""

    _PATHS = frozenset(
        {"/query", "/query/stream", "/agent/chat", "/agent/chat/stream"}
    )

    def __init__(self, app: Callable[..., Awaitable[None]]) -> None:
        self.app = app

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[..., Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if scope["type"] != "http" or scope.get("path") not in self._PATHS:
            await self.app(scope, receive, send)
            return

        state = scope.setdefault("state", {})
        request_id = state.get("request_id") or secrets.token_hex(6)
        state["request_id"] = request_id
        trace = tracer.start_trace(request_id, scope["path"])
        state["trace_id"] = trace["trace_id"]
        token = tracer.bind(trace)
        response_status: int | None = None

        async def send_with_trace(message: dict[str, Any]) -> None:
            nonlocal response_status
            if message["type"] == "http.response.start":
                response_status = message["status"]
                headers = list(message.get("headers", []))
                headers.extend(
                    [
                        (b"x-request-id", request_id.encode("ascii")),
                        (b"x-trace-id", trace["trace_id"].encode("ascii")),
                    ]
                )
                message = {**message, "headers": headers}
            await send(message)

        try:
            with logger.contextualize(
                request_id=request_id, trace_id=trace["trace_id"]
            ):
                await self.app(scope, receive, send_with_trace)
        except asyncio.CancelledError:
            if trace["status"] == "ok":
                tracer.record_outcome("cancelled", "request_cancelled", trace)
            raise
        except Exception as exc:
            tracer.record_error("unhandled_request_error", trace)
            logger.error("Unhandled request error: {}", type(exc).__name__)
            if response_status is not None:
                raise
            body = json.dumps(
                {
                    "detail": public_error("internal_server_error"),
                    "request_id": request_id,
                },
                ensure_ascii=False,
            ).encode("utf-8")
            await send_with_trace(
                {
                    "type": "http.response.start",
                    "status": 500,
                    "headers": [
                        (b"content-type", b"application/json; charset=utf-8"),
                        (b"cache-control", b"no-store"),
                        (b"content-length", str(len(body)).encode("ascii")),
                    ],
                }
            )
            await send_with_trace(
                {
                    "type": "http.response.body",
                    "body": body,
                    "more_body": False,
                }
            )
        finally:
            if response_status is not None and trace["status"] == "ok":
                if response_status >= 500:
                    tracer.record_error("request_failed", trace)
                elif response_status == 422:
                    tracer.record_error("request_validation_failed", trace)
                elif response_status >= 400:
                    tracer.record_error("request_rejected", trace)
            try:
                await asyncio.to_thread(tracer.finish_trace, trace)
            except OSError as exc:
                logger.error("Trace write failed: {}", type(exc).__name__)
            finally:
                tracer.reset(token)
