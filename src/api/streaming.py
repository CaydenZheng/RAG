"""SSE transport for RAG answers."""

import asyncio
import json
import time
from collections.abc import AsyncIterator

from loguru import logger
from starlette.requests import Request

from src.core.generation import AnswerInput, AnswerService

_PUBLIC_GENERATION_ERROR = {
    "code": "answer_generation_failed",
    "message": "回答生成失败，请稍后重试",
}


def encode_sse_event(event: dict) -> str:
    """Serialize one JSON payload as an SSE data event."""
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def answer_event(
    event_type: str,
    query_id: str,
    *,
    done: bool,
    **payload,
) -> dict:
    """Build the common envelope used by every answer data event."""
    return {
        "event": event_type,
        "query_id": query_id,
        "done": done,
        **payload,
    }


async def iter_answer_sse(
    *,
    request: Request,
    service: AnswerService,
    answer_input: AnswerInput,
    sources: list[dict],
    public_session_id: str,
    query_id: str,
    started_at: float,
) -> AsyncIterator[str]:
    """
    Stream one answer and emit a single terminal event.

    A disconnected consumer cancels the upstream stream and never persists a
    partial answer. Completed answers are persisted before the done event.
    """
    yield "retry: 3000\n\n"
    answer_chunks: list[str] = []
    answer_stream = service.stream(answer_input)

    try:
        try:
            async for chunk in answer_stream:
                if await request.is_disconnected():
                    logger.info(
                        "RAG stream {} disconnected; cancelling generation",
                        query_id,
                    )
                    return
                answer_chunks.append(chunk)
                yield encode_sse_event(
                    answer_event(
                        "chunk",
                        query_id,
                        done=False,
                        chunk=chunk,
                    )
                )
                await asyncio.sleep(0)
        finally:
            await answer_stream.aclose()

        if await request.is_disconnected():
            logger.info(
                "RAG stream {} disconnected before completion",
                query_id,
            )
            return

        answer = "".join(answer_chunks)
        service.persist(answer_input, answer)
    except asyncio.CancelledError:
        logger.info("RAG stream {} cancelled by server", query_id)
        raise
    except Exception:
        logger.exception("RAG stream {} failed", query_id)
        yield encode_sse_event(
            answer_event(
                "error",
                query_id,
                done=True,
                error=_PUBLIC_GENERATION_ERROR,
            )
        )
        return

    latency_ms = round((time.perf_counter() - started_at) * 1000, 1)
    yield encode_sse_event(
        answer_event(
            "done",
            query_id,
            done=True,
            answer=answer,
            sources=sources,
            session_id=public_session_id,
            latency_ms=latency_ms,
        )
    )
