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

from openai import OpenAI, AsyncOpenAI, Timeout
from sentence_transformers import SentenceTransformer
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from loguru import logger
from typing import List, Optional, AsyncGenerator

from config.settings import settings


class LLMClient:
    """LLM 生成 + 本地 Embedding 的统一入口"""

    def __init__(self):
        # --- 生成 Client (DeepSeek) ---
        self._chat_client = OpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            timeout=60.0,
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
            max_retries=2,
        )
        self._chat_model = settings.llm_model

        # --- Embedding 模型 (本地，延迟加载) ---
        self._embedding_model: Optional[SentenceTransformer] = None

    # ================================================================
    # Chat (Sync) — 保留兼容旧代码
    # ================================================================

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type(Exception),
    )
    def chat(
        self,
        messages: List[dict],
        model: Optional[str] = None,
        temperature: float = 0.3,
        max_tokens: Optional[int] = None,
        skip_cache: bool = False,
    ) -> str:
        """同步调用 LLM 生成回复。"""
        model = model or self._chat_model

        # 检查缓存
        if not skip_cache:
            from src.llm.cache import llm_cache
            cached = llm_cache.get(model, messages, temperature)
            if cached is not None:
                return cached

        kwargs = dict(
            model=model,
            messages=messages,
            temperature=temperature,
        )
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens

        logger.debug("LLM chat → model={} messages={}", kwargs["model"], len(messages))
        resp = self._chat_client.chat.completions.create(**kwargs)
        content = resp.choices[0].message.content or ""
        logger.debug("LLM chat ← {} chars, {} tokens",
                     len(content), resp.usage.total_tokens if resp.usage else "?")

        # 写入缓存
        if not skip_cache:
            from src.llm.cache import llm_cache
            llm_cache.set(model, messages, temperature, content)

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
    ) -> str:
        """异步调用 LLM 生成回复（非流式）。"""
        model = model or self._chat_model

        # 检查缓存（同步读 SQLite，毫秒级）
        if not skip_cache:
            from src.llm.cache import llm_cache
            cached = llm_cache.get(model, messages, temperature)
            if cached is not None:
                return cached

        kwargs = dict(
            model=model,
            messages=messages,
            temperature=temperature,
        )
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens

        logger.debug("LLM chat_async → model={} messages={}", kwargs["model"], len(messages))
        resp = await self._async_chat_client.chat.completions.create(**kwargs)
        content = resp.choices[0].message.content or ""
        logger.debug("LLM chat_async ← {} chars, {} tokens",
                     len(content), resp.usage.total_tokens if resp.usage else "?")

        # 写入缓存
        if not skip_cache:
            from src.llm.cache import llm_cache
            llm_cache.set(model, messages, temperature, content)

        return content

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

        model = model or self._chat_model

        kwargs = dict(
            model=model,
            messages=messages,
            temperature=temperature,
            stream=True,
            # 告知 API 最大生成 token 数，防止无限生成
            max_tokens=max_tokens,
        )

        logger.debug("LLM chat_stream_async → model={} messages={} max_tokens={}",
                     model, len(messages), max_tokens)
        stream = await self._async_chat_client.chat.completions.create(**kwargs)
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta is None or not delta.content:
                continue
            # 拆成逐词/逐空白：保留所有空白字符，让前端逐词渲染
            words = re.split(r'(\s+)', delta.content)
            for w in words:
                if w:
                    yield w

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
