"""
P2: 检索层

QueryRewriterNode   → LLM 改写查询（保留原 query 兜底）
HybridRetrieverNode → 向量 + BM25 → RRF 融合 → Top-20 候选
RerankerNode        → bge-reranker 精排 → Top-5
"""

import math
import yaml
from typing import List, Dict, Tuple

from pocketflow import Node
from loguru import logger
import chromadb

from config.settings import settings
from src.llm import llm_client
from src.infra.prompt_manager import prompt_manager
from src.utils.bm25_store import bm25_store


# ================================================================
# P2-4: QueryRewriterNode
# ================================================================

class QueryRewriterNode(Node):
    """
    用 LLM 改写查询为多个检索友好变体。
    始终保留原始 query，避免改写负向效果。
    """

    def prep(self, shared: dict) -> str:
        return shared.get("query", "")

    def exec(self, query: str) -> List[str]:
        if not query.strip():
            return [query]

        logger.info("🔄 Rewriting query: {}", query[:80])

        try:
            messages = prompt_manager.render_chat_messages(
                "query_rewrite", query=query
            )

            resp = llm_client.chat(messages, temperature=0.2)

            # 解析 YAML — 兼容两种格式
            if "```yaml" in resp:
                yaml_str = resp.split("```yaml")[1].split("```")[0].strip()
                parsed = yaml.safe_load(yaml_str)
                # 格式1: {queries: [...]}
                if isinstance(parsed, dict):
                    rewritten = parsed.get("queries", [query])
                # 格式2: [...] (直接是列表)
                elif isinstance(parsed, list):
                    rewritten = parsed
                else:
                    rewritten = [query]
            else:
                rewritten = [query]

        except Exception as e:
            logger.warning("Query rewrite failed: {}, using original", e)
            rewritten = [query]

        # 始终保留原 query 作为兜底
        if query not in rewritten:
            rewritten.insert(0, query)

        logger.info("  → {} query variants", len(rewritten))
        return rewritten

    def post(self, shared: dict, prep_res, exec_res: List[str]) -> str:
        shared["queries"] = exec_res
        return "default"


# ================================================================
# P2-5: HybridRetrieverNode
# ================================================================

class HybridRetrieverNode(Node):
    """
    混合检索：向量 + BM25 → RRF 融合 → 去重 → Top-K 候选。

    支持元数据过滤（通过 shared["filter"] 传入 where 条件）。
    BM25 未就绪时自动降级为纯向量检索。
    """

    def prep(self, shared: dict):
        queries = shared.get("queries", [shared.get("query", "")])
        metadata_filter = shared.get("filter")
        mode = shared.get("retrieval_mode", "hybrid")
        return queries, metadata_filter, mode

    def exec(self, inputs: tuple) -> List[dict]:
        queries, metadata_filter, mode = inputs

        use_vector = mode in ("vector_only", "hybrid", "hybrid+rerank")
        use_bm25 = mode in ("bm25_only", "hybrid", "hybrid+rerank")

        all_vector_hits: Dict[str, Tuple[int, float]] = {}
        all_bm25_hits: Dict[str, Tuple[int, float]] = {}
        chunk_map: Dict[str, dict] = {}

        for qi, query in enumerate(queries):
            # --- 向量检索 ---
            if use_vector:
                vec_results = self._vector_search(query, metadata_filter)
                for rank, item in enumerate(vec_results):
                    cid = item["chunk_id"]
                    if cid not in all_vector_hits or rank < all_vector_hits[cid][0]:
                        all_vector_hits[cid] = (rank, item["similarity"])
                    if cid not in chunk_map:
                        chunk_map[cid] = {
                            "chunk_id": cid,
                            "text": item["text"],
                            "metadata": item["metadata"],
                        }

            # --- BM25 检索 ---
            if use_bm25:
                bm25_results = bm25_store.search(query, top_k=settings.bm25_top_k)
                for rank, (cid, score) in enumerate(bm25_results):
                    if cid not in all_bm25_hits or rank < all_bm25_hits[cid][0]:
                        all_bm25_hits[cid] = (rank, score)
                    # BM25 不返回原文，从未知 chunk 需从 ChromaDB 补拉
                    if cid not in chunk_map:
                        chunk_map[cid] = self._fetch_chunk_text(cid)

        logger.info("Vector hits: {} unique, BM25 hits: {} unique",
                     len(all_vector_hits), len(all_bm25_hits))

        # --- RRF 融合 ---
        from src.utils.rrf import compute_rrf

        vector_ranks = {cid: rank for cid, (rank, _) in all_vector_hits.items()}
        bm25_ranks = {cid: rank for cid, (rank, _) in all_bm25_hits.items()}

        sorted_chunks = compute_rrf(vector_ranks, bm25_ranks, k=settings.rrf_k)
        top_n = sorted_chunks[:settings.vector_top_k]

        results = []
        for cid, rrf_score in top_n:
            if cid in chunk_map:
                results.append({
                    **chunk_map[cid],
                    "rrf_score": round(rrf_score, 4),
                })

        logger.info("RRF merged: {} → {} candidates (mode={})",
                     len(sorted_chunks), len(results), mode)
        return results

    def _fetch_chunk_text(self, chunk_id: str) -> dict:
        """从 ChromaDB 按 chunk_id 拉取原文（BM25 结果补全用）"""
        try:
            import chromadb
            client = chromadb.PersistentClient(
                path=str(settings.chroma_path.resolve()),
                settings=chromadb.config.Settings(anonymized_telemetry=False),
            )
            collection = client.get_collection("rag_collection")
            result = collection.get(ids=[chunk_id])
            if result["documents"]:
                return {
                    "chunk_id": chunk_id,
                    "text": result["documents"][0],
                    "metadata": result["metadatas"][0] if result["metadatas"] else {},
                }
        except Exception:
            pass
        return {"chunk_id": chunk_id, "text": "", "metadata": {}}

    def _vector_search(self, query: str, metadata_filter: dict = None) -> List[dict]:
        """单次向量检索"""
        query_vec = llm_client.embed_single(query)

        persist_dir = str(settings.chroma_path.resolve())
        client = chromadb.PersistentClient(
            path=persist_dir,
            settings=chromadb.config.Settings(anonymized_telemetry=False),
        )

        try:
            collection = client.get_collection("rag_collection")
        except Exception:
            logger.warning("ChromaDB collection not found, run build_index.py first")
            return []

        kwargs = dict(
            query_embeddings=[query_vec],
            n_results=settings.vector_top_k,
        )
        if metadata_filter:
            kwargs["where"] = metadata_filter

        results = collection.query(**kwargs)

        # 转为列表
        items = []
        if results["ids"] and results["ids"][0]:
            for i, cid in enumerate(results["ids"][0]):
                items.append({
                    "chunk_id": cid,
                    "text": results["documents"][0][i] if results["documents"] else "",
                    "similarity": 1.0 - results["distances"][0][i] if results["distances"] else 0.0,
                    "metadata": results["metadatas"][0][i] if results["metadatas"] else {},
                })
        return items

    def post(self, shared: dict, prep_res, exec_res: List[dict]) -> str:
        shared["candidates"] = exec_res
        return "default"


# ================================================================
# P2-6: RerankerNode
# ================================================================

# ---- 模块级单例：避免 Agent 多轮调用时重复加载 CrossEncoder ----
_reranker_model = None


def _get_reranker():
    """延迟加载 bge-reranker，进程内全局唯一"""
    global _reranker_model
    if _reranker_model is None:
        from sentence_transformers import CrossEncoder
        logger.info("Loading reranker model: {}", settings.rerank_model)
        _reranker_model = CrossEncoder(
            settings.rerank_model,
            max_length=512,
            device="cpu",
        )
    return _reranker_model


class RerankerNode(Node):
    """
    bge-reranker 精排，从 ~20 候选到 ~5 最终。
    长 chunk 采用首尾截断策略（前 256 + 后 256 token 近似），
    保证关键信息不因硬截断丢失。
    """

    MAX_LENGTH = 512

    def prep(self, shared: dict) -> tuple:
        query = shared.get("query", "")
        candidates = shared.get("candidates", [])
        return query, candidates

    def exec(self, inputs: tuple) -> List[dict]:
        query, candidates = inputs

        if not candidates:
            return []

        reranker = _get_reranker()

        # 构建 (query, doc) 对
        pairs = []
        for c in candidates:
            text = c["text"]
            # 首尾截断：bge-reranker 内置 max_length=512 会硬截，我们提前做智能截断
            if len(text) > self.MAX_LENGTH * 4:
                half = self.MAX_LENGTH * 2
                text = text[:half] + text[-half:]
            pairs.append([query, text])

        # 计算分数
        scores = reranker.predict(pairs)

        if isinstance(scores, float):
            scores = [scores]

        for i, c in enumerate(candidates):
            c["rerank_score"] = round(float(scores[i]), 4)

        ranked = sorted(candidates, key=lambda x: x.get("rerank_score", 0), reverse=True)
        top = ranked[:settings.rerank_top_k]

        logger.info("Reranked: {} → {} chunks, top score: {:.4f}",
                     len(candidates), len(top),
                     top[0]["rerank_score"] if top else 0)
        return top

    def post(self, shared: dict, prep_res, exec_res: List[dict]) -> str:
        shared["retrieved_chunks"] = exec_res
        return "default"
