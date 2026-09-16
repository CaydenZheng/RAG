"""
Generation provider fallback with explicit terminal failure.

Primary provider calls use the OpenAI client's bounded transient retry policy.
The optional Ollama provider is attempted once. If both fail, callers receive a
GenerationUnavailableError rather than a normal-looking answer.
"""

import asyncio
from typing import List, Optional

from loguru import logger
from openai import OpenAI

from config.settings import settings
from src.core.errors import GenerationUnavailableError
from src.llm import llm_client


def _chat_with_ollama(
    messages: List[dict],
    temperature: float,
) -> str:
    client = OpenAI(
        base_url=settings.ollama_base_url,
        api_key="ollama",
        timeout=30,
        max_retries=0,
    )
    response = client.chat.completions.create(
        model="llama3.2",
        messages=messages,
        temperature=temperature,
    )
    return response.choices[0].message.content or ""


async def chat_with_fallback_async(
    messages: List[dict],
    model: Optional[str] = None,
    temperature: float = 0.3,
    max_tokens: Optional[int] = None,
    skip_cache: bool = False,
    index_version: str | None = None,
) -> str:
    """Async primary call with a non-blocking optional Ollama fallback."""
    model = model or settings.llm_model
    last_error: Exception | None = None

    try:
        return await llm_client.chat_async(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            skip_cache=skip_cache,
            index_version=index_version,
        )
    except Exception as exc:
        last_error = exc
        logger.warning("Primary async LLM failed; trying configured fallback")

    if settings.ollama_base_url:
        try:
            answer = await asyncio.to_thread(
                _chat_with_ollama,
                messages,
                temperature,
            )
            logger.info("Ollama fallback succeeded")
            return answer
        except Exception as exc:
            last_error = exc
            logger.warning("Ollama fallback failed")

    raise GenerationUnavailableError(
        "all answer-generation providers failed"
    ) from last_error
