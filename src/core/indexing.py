"""
P1-4~P1-5: 索引构建

EmbedderNode     → 对每个 chunk 调用本地 embedding 模型
IndexBuilderNode → 写入 ChromaDB + 构建 BM25 索引
"""

import hashlib
from typing import List

from pocketflow import Node, BatchNode
from loguru import logger

from config.settings import settings
from src.llm import llm_client
from src.utils.bm25_store import bm25_store


# ================================================================
# P1-4: EmbedderNode
# ================================================================

class EmbedderNode(BatchNode):
    """批量 embedding，输出向量列表"""

    def prep(self, shared: dict) -> List[dict]:
        return shared.get("chunks", [])

    def exec(self, chunk: dict) -> dict:
        """对单个 chunk 做 embedding"""
        vector = llm_client.embed_single(chunk["text"])
        chunk["embedding"] = vector
        return chunk

    def post(self, shared: dict, prep_res, exec_res_list: List[dict]) -> str:
        """存储带 embedding 的 chunk"""
        shared["chunks_with_embedding"] = exec_res_list
        logger.info("✅ Embedded {} chunks, dim={}",
                     len(exec_res_list), llm_client.embedding_dim)
        return "default"


# ================================================================
# P1-5: IndexBuilderNode
# ================================================================

class IndexBuilderNode(Node):
    """
    双路索引构建：
    1. ChromaDB：写入向量 + 元数据 + 原文（持久化）
    2. BM25：构建关键词索引（内存，支持运行时同步）
    """

    def prep(self, shared: dict) -> List[dict]:
        return shared.get("chunks_with_embedding", [])

    def exec(self, chunks: List[dict]) -> dict:
        """写入 ChromaDB 并构建 BM25"""
        import chromadb
        from chromadb.config import Settings as ChromaSettings

        persist_dir_abs = str(settings.chroma_path.resolve())
        client = chromadb.PersistentClient(
            path=persist_dir_abs,
            settings=ChromaSettings(anonymized_telemetry=False),
        )

        # 每次全量重建：先删后建
        try:
            client.delete_collection("rag_collection")
        except Exception:
            pass

        collection = client.create_collection(
            name="rag_collection",
            metadata={"hnsw:space": "cosine"},
        )

        # 批量写入 ChromaDB
        ids = [c["chunk_id"] for c in chunks]
        embeddings = [c["embedding"] for c in chunks]
        documents = [c["text"] for c in chunks]
        metadatas = [c["metadata"] for c in chunks]

        # ChromaDB 分批写入（避免单次过大）
        batch_size = 100
        for i in range(0, len(ids), batch_size):
            collection.add(
                ids=ids[i:i + batch_size],
                embeddings=embeddings[i:i + batch_size],
                documents=documents[i:i + batch_size],
                metadatas=metadatas[i:i + batch_size],
            )

        logger.info("ChromaDB: {} vectors persisted to {}", collection.count(), persist_dir_abs)

        # --- BM25 ---
        texts = [c["text"] for c in chunks]
        chunk_ids = [c["chunk_id"] for c in chunks]

        bm25_store.build(texts, chunk_ids)
        logger.info("BM25: indexed {} documents", len(texts))

        # --- 知识库指纹（用于缓存失效） ---
        fingerprint = self._compute_fingerprint(chunks)
        logger.info("Knowledge base fingerprint: {}", fingerprint)

        # 持久化指纹到文件
        from src.llm.cache import llm_cache
        llm_cache.update_fingerprint(fingerprint)

        return {
            "chunks_count": len(chunks),
            "fingerprint": fingerprint,
        }

    def _compute_fingerprint(self, chunks: List[dict]) -> str:
        """计算知识库指纹 = hash(所有 doc_id 排序拼接)"""
        doc_ids = sorted(set(c["doc_id"] for c in chunks))
        return hashlib.md5("|".join(doc_ids).encode()).hexdigest()[:16]

    def post(self, shared: dict, prep_res, exec_res: dict) -> str:
        shared["index_info"] = exec_res
        shared["knowledge_fingerprint"] = exec_res["fingerprint"]
        logger.info("✅ Index built: {} chunks, fingerprint={}",
                     exec_res["chunks_count"], exec_res["fingerprint"])
        return "default"
