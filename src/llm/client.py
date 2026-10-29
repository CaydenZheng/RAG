"""
统一的 LLM / Embedding 调用接口。

- chat():            同步调用（兼容旧代码）
- chat_async():      异步调用
- chat_stream_async(): 异步流式生成（yield 文本增量）
- embed():           本地 sentence-transformers 模型

用法:
    from src.llm import llm_client
    answer = llm_client.chat([{"role": "user", "content": "hello"}])
    answer = await llm_client.chat_async([{"role": "user", "content": "hello"}])
    async for chunk in llm_client.chat_stream_async([...]):
        print(chunk, end="")
"""

import asyncio
import time
from dataclasses import dataclass
from typing import Any, AsyncGenerator, List, Optional
from urllib.parse import urlsplit

from loguru import logger
from openai import APIError, AsyncOpenAI, OpenAI, Timeout
from openai.types.chat import ChatCompletion, ChatCompletionMessage
from sentence_transformers import SentenceTransformer

from config.settings import settings
from src.infra.tracer import tracer
from src.security.request_authorization import require_active_authorization


@dataclass(frozen=True)
class NativeToolCall:
    """Provider-neutral model request to invoke one named tool."""

    call_id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class NativeChatResponse:
    """Provider-neutral native chat response consumed by AgentHarness."""

    content: str
    tool_calls: tuple[NativeToolCall, ...] = ()


class LLMCompletionError(RuntimeError):
    """An unconfirmed response; no provider text is carried across layers."""

    def __init__(self, failure_kind: str) -> None:
        super().__init__("Unexpected tool call delta in text stream" if failure_kind == "unexpected_tool_delta"
                         else "LLM completion ended without a complete response")
        self.failure_kind = failure_kind


class LLMClient:
    """LLM 生成 + 本地 Embedding 的统一入口"""

    def __init__(self):
        # These bounded interactive calls expect visible output. DeepSeek V4
        # otherwise defaults to high-effort thinking inside the same token cap.
        self._deepseek_non_thinking = (
            urlsplit(settings.openai_base_url).hostname == "api.deepseek.com"
        )
        # --- 生成 Client (DeepSeek) ---
        self._chat_client = OpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            timeout=60.0,
            max_retries=settings.llm_max_retries,
        )
        self._async_chat_client = AsyncOpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            timeout=Timeout(
                connect=15.0,   # 建立连接 15s
                read=180.0,     # 流式读取总时长 3 分钟
                write=60.0,     # 发送请求 60s
                pool=10.0,      # 连接池等待 10s
            ),
            max_retries=settings.llm_max_retries,
        )
        self._chat_model = settings.llm_model

        # --- Embedding 模型 (本地，延迟加载) ---
        self._embedding_model: Optional[SentenceTransformer] = None

    def _completion_options(self) -> dict[str, Any]:
        if self._deepseek_non_thinking:
            return {"extra_body": {"thinking": {"type": "disabled"}}}
        return {}

    def _cache_dimensions(
        self,
        max_tokens: int | None,
        index_version: str | None,
    ) -> dict:
        from src.infra.index_catalog import index_catalog
        from src.llm.cache_context import current_cache_identity

        if index_version is None:
            try:
                index_version = index_catalog.capture().version_id
            except Exception:
                index_version = "unavailable"
        return {
            "max_tokens": max_tokens,
            "identity_scope": current_cache_identity(),
            "index_version": index_version,
            "prompt_version": settings.prompt_version,
            "generation_parameters": {
                "completion_contract": "validated-v2",
                "thinking": "disabled" if self._deepseek_non_thinking else "provider_default",
            },
        }

    @staticmethod
    def _completed_message(
        response: ChatCompletion, *, allow_tools: bool = False,
    ) -> ChatCompletionMessage:
        """Reject truncated or unconfirmed outputs before caching or tool planning."""
        if response.choices:
            choice = response.choices[0]
            message = choice.message
            tool_calls = message.tool_calls
            if (
                choice.finish_reason == "stop"
                and not tool_calls
                and message.content
                and message.content.strip()
            ) or (
                allow_tools and choice.finish_reason == "tool_calls" and tool_calls
            ):
                return message
        reason = response.choices[0].finish_reason if response.choices else None
        failure_kind = (
            "incomplete_output" if reason == "length"
            else "filtered_output" if reason == "content_filter"
            else "empty_output" if reason == "stop" and not (message.content or "").strip()
            else "unconfirmed_output"
        )
        raise LLMCompletionError(failure_kind)

    @staticmethod
    def _record_chat_span(
        started_at: float,
        model: str,
        *,
        cache_hit: bool,
        response_chars: int = 0,
        usage=None,
        status: str = "ok",
        error_code: str = "",
        response: ChatCompletion | None = None,
        output_limit: int | None = None,
        error: Exception | None = None,
        finish_reason: str | None = None,
    ) -> None:
        if status == "ok":
            from src.infra.exchange_context import completed_model

            completed_model.set(model)
        if response is not None:
            usage = response.usage
        reason = response.choices[0].finish_reason if response is not None and response.choices else finish_reason
        finish_reason = reason if reason in {None, "stop", "length", "tool_calls", "content_filter", "function_call"} else "other"
        status_code = getattr(error, "status_code", None)
        prompt_tokens = getattr(usage, "prompt_tokens", 0) if usage else 0
        completion_tokens = (
            getattr(usage, "completion_tokens", 0) if usage else 0
        )
        total_tokens = getattr(usage, "total_tokens", 0) if usage else 0
        details = getattr(usage, "completion_tokens_details", None)
        reasoning_tokens = getattr(details, "reasoning_tokens", None)
        if type(reasoning_tokens) is not int or reasoning_tokens < 0:
            reasoning_tokens = None
        tracer.add_span(
            None,
            tracer.current_stage,
            (time.perf_counter() - started_at) * 1000,
            status=status,
            error_code=error_code,
            model=model,
            cache_hit=cache_hit,
            response_chars=response_chars,
            prompt_tokens=prompt_tokens or 0,
            completion_tokens=completion_tokens or 0,
            reasoning_tokens=reasoning_tokens,
            total_tokens=total_tokens or 0,
            usage_reported=usage is not None,
            output_limit=output_limit,
            finish_reason=finish_reason,
            failure_kind=(error.failure_kind if isinstance(error, LLMCompletionError)
                          else "upstream_error" if isinstance(error, APIError)
                          else "client_error" if error is not None else ""),
            exception_type=type(error).__name__ if error is not None else "",
            http_status=status_code if isinstance(status_code, int) and 100 <= status_code <= 599 else None,
        )

    # ================================================================
    # Chat (Sync) — 保留兼容旧代码
    # ================================================================

    def chat(
        self,
        messages: List[dict],
        model: Optional[str] = None,
        temperature: float = 0.3,
        max_tokens: Optional[int] = None,
        skip_cache: bool = False,
        index_version: str | None = None,
    ) -> str:
        """同步调用 LLM 生成回复。"""
        require_active_authorization()
        from src.security.account_limits import reserve_model_call

        max_tokens = reserve_model_call(messages,max_tokens)
        model = model or self._chat_model
        started_at = time.perf_counter()
        cache_dimensions = (
            self._cache_dimensions(max_tokens, index_version)
            if not skip_cache
            else {}
        )

        # 检查缓存
        if not skip_cache:
            from src.llm.cache import llm_cache
            cached = llm_cache.get(
                model, messages, temperature, **cache_dimensions
            )
            if cached is not None:
                self._record_chat_span(
                    started_at, model, cache_hit=True, response_chars=len(cached), output_limit=max_tokens,
                )
                return cached

        kwargs = dict(
            model=model,
            messages=messages,
            temperature=temperature,
            **self._completion_options(),
        )
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens

        logger.debug("LLM chat → model={} messages={}", kwargs["model"], len(messages))
        resp: ChatCompletion | None = None
        try:
            resp = self._chat_client.chat.completions.create(**kwargs)
            content: str = self._completed_message(resp).content or ""
        except Exception as error:
            self._record_chat_span(
                started_at, model, cache_hit=False, status="error",
                error_code="llm_call_failed",
                response=resp, output_limit=max_tokens, error=error,
            )
            raise
        self._record_chat_span(
            started_at, model, cache_hit=False, response_chars=len(content),
            usage=resp.usage,
            response=resp, output_limit=max_tokens,
        )
        logger.debug("LLM chat ← {} chars, {} tokens",
                     len(content), resp.usage.total_tokens if resp.usage else "?")

        # 写入缓存
        if not skip_cache:
            from src.llm.cache import llm_cache
            llm_cache.set(
                model,
                messages,
                temperature,
                content,
                **cache_dimensions,
            )

        return content

    # ================================================================
    # Chat (Async)
    # ================================================================

    async def chat_async(
        self,
        messages: List[dict],
        model: Optional[str] = None,
        temperature: float = 0.3,
        max_tokens: Optional[int] = None,
        skip_cache: bool = False,
        index_version: str | None = None,
    ) -> str:
        """异步调用 LLM 生成回复（非流式）。"""
        require_active_authorization()
        from src.security.account_limits import reserve_model_call

        max_tokens = reserve_model_call(messages,max_tokens)
        model = model or self._chat_model
        started_at = time.perf_counter()
        cache_dimensions = (
            self._cache_dimensions(max_tokens, index_version)
            if not skip_cache
            else {}
        )

        # 检查缓存（同步读 SQLite，毫秒级）
        if not skip_cache:
            from src.llm.cache import llm_cache
            cached = llm_cache.get(
                model, messages, temperature, **cache_dimensions
            )
            if cached is not None:
                self._record_chat_span(
                    started_at, model, cache_hit=True, response_chars=len(cached), output_limit=max_tokens,
                )
                return cached

        kwargs = dict(
            model=model,
            messages=messages,
            temperature=temperature,
            **self._completion_options(),
        )
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens

        logger.debug("LLM chat_async → model={} messages={}", kwargs["model"], len(messages))
        resp: ChatCompletion | None = None
        try:
            resp = await self._async_chat_client.chat.completions.create(**kwargs)
            content: str = self._completed_message(resp).content or ""
        except Exception as error:
            self._record_chat_span(
                started_at, model, cache_hit=False, status="error",
                error_code="llm_call_failed",
                response=resp, output_limit=max_tokens, error=error,
            )
            raise
        self._record_chat_span(
            started_at, model, cache_hit=False, response_chars=len(content),
            usage=resp.usage,
            response=resp, output_limit=max_tokens,
        )
        logger.debug("LLM chat_async ← {} chars, {} tokens",
                     len(content), resp.usage.total_tokens if resp.usage else "?")

        # 写入缓存
        if not skip_cache:
            from src.llm.cache import llm_cache
            llm_cache.set(
                model,
                messages,
                temperature,
                content,
                **cache_dimensions,
            )

        return content

    async def chat_with_tools_async(
        self,
        messages: List[dict],
        tools: list[dict[str, Any]],
        model: Optional[str] = None,
        temperature: float = 0.3,
        max_tokens: Optional[int] = None,
    ) -> NativeChatResponse:
        """Call an OpenAI-compatible model and preserve native tool requests."""
        require_active_authorization()
        from src.security.account_limits import reserve_model_call

        max_tokens = reserve_model_call(messages,max_tokens,tools)
        model = model or self._chat_model
        started_at = time.perf_counter()
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            **self._completion_options(),
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        logger.debug(
            "LLM native chat → model={} messages={} tools={}",
            model,
            len(messages),
            len(tools),
        )
        response: ChatCompletion | None = None
        try:
            response = await self._async_chat_client.chat.completions.create(
                **kwargs
            )
            message: ChatCompletionMessage = self._completed_message(
                response, allow_tools=bool(tools),
            )
        except Exception as error:
            self._record_chat_span(
                started_at,
                model,
                cache_hit=False,
                status="error",
                error_code="llm_call_failed",
                response=response, output_limit=max_tokens, error=error,
            )
            raise

        content = message.content or ""
        tool_calls = tuple(
            NativeToolCall(
                call_id=call.id,
                name=call.function.name,
                arguments=call.function.arguments,
            )
            for call in (message.tool_calls or ())
        )
        self._record_chat_span(
            started_at,
            model,
            cache_hit=False,
            response_chars=len(content),
            usage=response.usage,
            response=response, output_limit=max_tokens,
        )
        logger.debug(
            "LLM native chat ← {} chars, {} tool calls",
            len(content),
            len(tool_calls),
        )
        return NativeChatResponse(content=content, tool_calls=tool_calls)

    # ================================================================
    # Chat Stream (Async Generator)
    # ================================================================

    async def chat_stream_async(
        self,
        messages: List[dict],
        model: Optional[str] = None,
        temperature: float = 0.3,
        max_tokens: Optional[int] = 1024,
    ) -> AsyncGenerator[str, None]:
        """
        异步流式调用 LLM，逐词 yield 文本增量。

        DeepSeek 等 API 的 stream=True 返回大块文本（几十 token），
        这里拆成逐词输出，确保前端能感知到打字效果。

        注意：默认 max_tokens=1024 防止生成失控（与 answer_generation.yaml 对齐）。
        """
        import re

        require_active_authorization()
        from src.security.account_limits import reserve_model_call

        max_tokens = reserve_model_call(messages,max_tokens)
        model = model or self._chat_model
        started_at = time.perf_counter()

        kwargs = dict(
            model=model,
            messages=messages,
            temperature=temperature,
            stream=True,
            stream_options={"include_usage": True},
            # 告知 API 最大生成 token 数，防止无限生成
            max_tokens=max_tokens,
            **self._completion_options(),
        )

        logger.debug("LLM chat_stream_async → model={} messages={} max_tokens={}",
                     model, len(messages), max_tokens)
        response_chars = 0
        usage = None
        stream = None
        completed = False
        reported_finish_reason: str | None = None
        try:
            stream = await self._async_chat_client.chat.completions.create(**kwargs)
            async for chunk in stream:
                require_active_authorization()
                if getattr(chunk, "usage", None) is not None:
                    usage = chunk.usage
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                finish_reason = getattr(choice, "finish_reason", None)
                if finish_reason is not None:
                    reported_finish_reason = finish_reason
                if finish_reason not in (None, "stop"):
                    raise LLMCompletionError("incomplete_output" if finish_reason == "length" else "filtered_output" if finish_reason == "content_filter" else "unconfirmed_output")
                delta = choice.delta
                if delta is None:
                    completed = finish_reason == "stop" or completed
                    continue
                if getattr(delta, "tool_calls", None):
                    raise LLMCompletionError("unexpected_tool_delta")
                if not delta.content:
                    completed = finish_reason == "stop" or completed
                    continue
                response_chars += len(delta.content)
                # 拆成逐词/逐空白：保留所有空白字符，让前端逐词渲染
                words = re.split(r'(\s+)', delta.content)
                for word in words:
                    if word:
                        yield word
                completed = finish_reason == "stop" or completed
            if not completed:
                raise LLMCompletionError("unconfirmed_output")
        except (asyncio.CancelledError, GeneratorExit):
            self._record_chat_span(
                started_at, model, cache_hit=False,
                response_chars=response_chars, usage=usage, status="cancelled",
                error_code="llm_stream_cancelled",
                output_limit=max_tokens, finish_reason=reported_finish_reason,
            )
            raise
        except Exception as error:
            self._record_chat_span(
                started_at, model, cache_hit=False,
                response_chars=response_chars, usage=usage, status="error",
                error_code="llm_stream_failed",
                output_limit=max_tokens, finish_reason=reported_finish_reason, error=error,
            )
            raise
        else:
            self._record_chat_span(
                started_at, model, cache_hit=False,
                response_chars=response_chars, usage=usage,
                output_limit=max_tokens, finish_reason=reported_finish_reason,
            )
        finally:
            if stream is not None:
                try:
                    await stream.close()
                except Exception as exc:
                    logger.warning(
                        "LLM stream close failed: {}", type(exc).__name__
                    )

    # ================================================================
    # Embedding
    # ================================================================

    @property
    def _embed_model(self) -> SentenceTransformer:
        """延迟加载 embedding 模型（首次调用时自动下载）"""
        if self._embedding_model is None:
            logger.info("Loading local embedding model: {}", settings.local_embedding_model)
            self._embedding_model = SentenceTransformer(
                settings.local_embedding_model,
                device="cpu",  # CPU inference
            )
            try:
                # 新版 API 优先
                if hasattr(self._embedding_model, 'get_embedding_dimension'):
                    dim = self._embedding_model.get_embedding_dimension()
                else:
                    dim = self._embedding_model.get_sentence_embedding_dimension()
            except Exception:
                dim = "?"
            logger.info("Embedding model loaded, dim={}", dim)
        return self._embedding_model

    def embed(self, texts: List[str]) -> List[List[float]]:
        """对文本列表做 embedding。

        Args:
            texts: 文本列表

        Returns:
            embedding 向量列表，每个为 float 列表
        """
        if isinstance(texts, str):
            texts = [texts]
        embeddings = self._embed_model.encode(
            texts,
            normalize_embeddings=True,   # 归一化，配合内积=余弦相似度
            show_progress_bar=False,
        )
        return embeddings.tolist()

    def embed_single(self, text: str) -> List[float]:
        """对单条文本做 embedding。"""
        return self.embed([text])[0]

    @property
    def embedding_dim(self) -> int:
        """返回 embedding 维度。"""
        if hasattr(self._embed_model, 'get_embedding_dimension'):
            return self._embed_model.get_embedding_dimension()
        return self._embed_model.get_sentence_embedding_dimension()


# 全局单例
llm_client = LLMClient()
