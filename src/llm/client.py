"""
统一的 LLM / Embedding 调用接口。

- chat():    走 DeepSeek API（OpenAI 兼容），内置 timeout + 重试
- embed():   走本地 sentence-transformers 模型

用法:
    from src.llm import llm_client
    answer = llm_client.chat([{"role": "user", "content": "hello"}])
    vec = llm_client.embed("some text")
"""

from openai import OpenAI
from sentence_transformers import SentenceTransformer
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from loguru import logger
from typing import List, Optional

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
        self._chat_model = settings.llm_model

        # --- Embedding 模型 (本地，延迟加载) ---
        self._embedding_model: Optional[SentenceTransformer] = None

    # ================================================================
    # Chat
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
        """调用 LLM 生成回复。"""
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
