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


def chat_with_fallback(
    messages: List[dict],
    model: Optional[str] = None,
    temperature: float = 0.3,
    max_tokens: Optional[int] = None,
    skip_cache: bool = False,
) -> str:
    """Call the primary provider, then optional Ollama, or raise."""
    model = model or settings.llm_model
    last_error: Exception | None = None

    try:
        return llm_client.chat(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            skip_cache=skip_cache,
        )
    except Exception as exc:
        last_error = exc
        logger.warning("Primary LLM failed; trying configured fallback")

    if settings.ollama_base_url:
        try:
            answer = _chat_with_ollama(messages, temperature)
            logger.info("Ollama fallback succeeded")
            return answer
        except Exception as exc:
            last_error = exc
            logger.warning("Ollama fallback failed")

    raise GenerationUnavailableError(
        "all answer-generation providers failed"
    ) from last_error


async def chat_with_fallback_async(
    messages: List[dict],
    model: Optional[str] = None,
    temperature: float = 0.3,
    max_tokens: Optional[int] = None,
    skip_cache: bool = False,
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


def retrieval_fallback_message(query: str, sources: list) -> str:
    """
    当 LLM 全部不可用时，返回检索原文 + 提示语。
    这不是 mock 回答，而是给用户有用的信息。
    """
    if not sources:
        return "系统暂时不可用，请稍后重试。"

    parts = ["当前生成服务暂时不可用，以下是检索到的最相关内容供参考：\n"]
    for i, src in enumerate(sources[:5], 1):
        parts.append(f"[{i}] ({src.get('source', 'unknown')}) {src.get('text', '')[:300]}")
    return "\n\n".join(parts)
