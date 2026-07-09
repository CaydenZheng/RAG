"""
降级链 — LLM 调用异常时的逐级回退。

链路: 原 Provider → Ollama 本地 → 原文兜底
检索降级: 混合检索 → 纯向量检索

用法:
    from src.infra.fallback import chat_with_fallback, chat_with_fallback_async
    answer = chat_with_fallback(messages)
    answer = await chat_with_fallback_async(messages)
"""

from typing import List, Optional
from loguru import logger

from config.settings import settings
from src.llm import llm_client


def chat_with_fallback(
    messages: List[dict],
    model: Optional[str] = None,
    temperature: float = 0.3,
    max_tokens: Optional[int] = None,
    skip_cache: bool = False,
) -> str:
    """
    带降级链的 LLM 调用（同步）。

    链路: DeepSeek → Ollama → 原文兜底
    """
    model = model or settings.llm_model

    try:
        return llm_client.chat(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            skip_cache=skip_cache,
        )
    except Exception as e:
        logger.warning("Primary LLM failed: {}. Trying fallback...", e)

    if settings.ollama_base_url:
        try:
            from openai import OpenAI
            ollama = OpenAI(base_url=settings.ollama_base_url, api_key="ollama", timeout=30)
            resp = ollama.chat.completions.create(
                model="llama3.2", messages=messages, temperature=temperature)
            content = resp.choices[0].message.content or ""
            logger.info("Ollama fallback succeeded")
            return content
        except Exception as e:
            logger.warning("Ollama fallback failed: {}", e)

    logger.warning("All LLM providers failed. Returning fallback message.")
    return "当前生成服务暂时不可用，请稍后重试。"


async def chat_with_fallback_async(
    messages: List[dict],
    model: Optional[str] = None,
    temperature: float = 0.3,
    max_tokens: Optional[int] = None,
    skip_cache: bool = False,
) -> str:
    """
    带降级链的 LLM 调用（异步）。

    链路: DeepSeek (async) → Ollama (sync fallback) → 原文兜底
    """
    model = model or settings.llm_model

    try:
        return await llm_client.chat_async(
            messages=messages, model=model, temperature=temperature,
            max_tokens=max_tokens, skip_cache=skip_cache)
    except Exception as e:
        logger.warning("Primary async LLM failed: {}. Falling back...", e)

    # Layer 2: Ollama（用同步 client，Ollama 通常本地低延迟）
    if settings.ollama_base_url:
        try:
            from openai import OpenAI
            ollama = OpenAI(base_url=settings.ollama_base_url, api_key="ollama", timeout=30)
            resp = ollama.chat.completions.create(
                model="llama3.2", messages=messages, temperature=temperature)
            content = resp.choices[0].message.content or ""
            logger.info("Ollama fallback succeeded")
            return content
        except Exception as e:
            logger.warning("Ollama fallback failed: {}", e)

    logger.warning("All LLM providers failed. Returning fallback message.")
    return "当前生成服务暂时不可用，请稍后重试。"


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
