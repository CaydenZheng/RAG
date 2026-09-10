"""Application startup warm-up tasks."""

import threading

import chromadb
from loguru import logger

from config.settings import settings
from src.infra.index_catalog import index_catalog


def warm_up_runtime() -> None:
    """服务启动时预热模型 + 异步重建 BM25"""

    # 1. 预加载 Reranker（调用模块级单例，确保查询时命中缓存）
    def _preload_reranker():
        try:
            logger.info("⏳ Preloading reranker model: {}", settings.rerank_model)
            from src.core.retrieval import _get_reranker
            _get_reranker()
            logger.info("✅ Reranker model ready")
        except Exception as e:
            logger.warning("Reranker preload failed: {}", e)

    threading.Thread(target=_preload_reranker, daemon=True).start()

    # 2. 预加载 Embedding 模型（触发 llm_client 的延迟加载缓存）
    def _preload_embedding():
        try:
            logger.info("⏳ Preloading embedding model: {}", settings.local_embedding_model)
            from src.llm import llm_client
            _ = llm_client.embedding_dim
            logger.info("✅ Embedding model ready")
        except Exception as e:
            logger.warning("Embedding preload failed: {}", e)

    threading.Thread(target=_preload_embedding, daemon=True).start()

    # 3. 从 ChromaDB 异步重建 BM25（索引未就绪时自动降级为纯向量）
    def _rebuild_bm25():
        try:
            from src.utils.bm25_store import bm25_store
            persist_dir = str(settings.chroma_path.resolve())
            client = chromadb.PersistentClient(
                path=persist_dir,
                settings=chromadb.config.Settings(anonymized_telemetry=False),
            )
            active_index = index_catalog.capture()
            collection = client.get_collection(active_index.collection_name)
            if collection.count() == 0:
                logger.info("ChromaDB is empty, skipping BM25 rebuild")
                return

            # 拉取全部文档
            all_data = collection.get()
            texts = all_data["documents"] or []
            chunk_ids = all_data["ids"] or []

            logger.info("⏳ Rebuilding BM25 from {} ChromaDB chunks...", len(texts))
            bm25_store.build(
                texts,
                chunk_ids,
                version_id=active_index.version_id,
            )
            logger.info(
                "✅ BM25 ready: {} docs, version={}",
                len(texts),
                active_index.version_id,
            )
        except Exception as e:
            logger.warning("BM25 rebuild failed (will use vector-only): {}", e)

    threading.Thread(target=_rebuild_bm25, daemon=True).start()
