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
from src.core.run_options import current_run_options
from src.llm import llm_client
from src.security.request_authorization import (
    AccountLimitExceeded,
    AuthorizationRevoked,
    require_active_authorization,
)


def _chat_with_ollama(
    messages: List[dict],
    temperature: float,
    max_tokens: int | None = None,
) -> str:
    require_active_authorization()
    from src.security.account_limits import reserve_model_call

    max_tokens = reserve_model_call(messages,max_tokens)
    with OpenAI(
        base_url=settings.ollama_base_url,
        api_key="ollama",
        timeout=30,
        max_retries=0,
    ) as client:
        response = client.chat.completions.create(
            model="llama3.2",
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        require_active_authorization()
        return llm_client._completed_message(response).content or ""


async def chat_with_fallback_async(
    messages: List[dict],
    model: Optional[str] = None,
    temperature: float = 0.3,
    max_tokens: Optional[int] = None,
    skip_cache: bool = False,
    index_version: str | None = None,
) -> str:
    """Async primary call with a non-blocking optional Ollama fallback."""
    require_active_authorization()
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
    except (AuthorizationRevoked, AccountLimitExceeded):
        raise
    except Exception as exc:
        last_error = exc
        if current_run_options.get() is not None:
            raise GenerationUnavailableError("Selected model failed; fallback disabled") from exc
        logger.warning("Primary async LLM failed; trying configured fallback")

    if settings.ollama_base_url:
        require_active_authorization()
        try:
            answer = await asyncio.to_thread(
                _chat_with_ollama,
                messages,
                temperature,
                max_tokens,
            )
            logger.info("Ollama fallback succeeded")
            from src.infra.exchange_context import completed_model

            completed_model.set("llama3.2")
            return answer
        except (AuthorizationRevoked, AccountLimitExceeded):
            raise
        except Exception as exc:
            last_error = exc
            logger.warning("Ollama fallback failed")

    raise GenerationUnavailableError(
        "all answer-generation providers failed"
    ) from last_error
