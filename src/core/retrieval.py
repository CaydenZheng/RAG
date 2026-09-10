"""
P2: 检索层

QueryRewriterNode   → LLM 改写查询（保留原 query 兜底）
HybridRetrieverNode → 向量 + BM25 → RRF 融合 → Top-20 候选
RerankerNode        → bge-reranker 精排 → Top-5
"""

from typing import Dict, List, Tuple

import chromadb
import yaml
from loguru import logger
from pocketflow import AsyncNode, Node

from config.settings import settings
from src.core.index_versions import CandidateBatch
from src.infra.index_catalog import index_catalog
from src.infra.prompt_manager import prompt_manager
from src.infra.tracer import tracer
from src.llm import llm_client
from src.utils.bm25_store import bm25_store

# ================================================================
# P2-4: QueryRewriterNode
# ================================================================

class QueryRewriterNode(AsyncNode):
    """
    用 LLM 改写查询为多个检索友好变体。
    始终保留原始 query，避免改写负向效果。
    """

    async def prep_async(self, shared: dict) -> str:
        return shared.get("query", "")

    async def exec_async(self, query: str) -> List[str]:
        return await self.rewrite(query)

    async def rewrite(self, query: str) -> List[str]:
        """Return retrieval variants while always retaining the original query."""
        if not query.strip():
            return [query]

        logger.info("Rewriting query: chars={}", len(query))

        try:
            messages = prompt_manager.render_chat_messages(
                "query_rewrite", query=query
            )

            with tracer.stage("query_rewrite_llm"):
                resp = await llm_client.chat_async(messages, temperature=0.2)

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

        except Exception as exc:
            logger.warning(
                "Query rewrite failed: {}; using original", type(exc).__name__
            )
            rewritten = [query]

        # 始终保留原 query 作为兜底
        if query not in rewritten:
            rewritten.insert(0, query)

        logger.info("  → {} query variants", len(rewritten))
        return rewritten

    async def post_async(self, shared: dict, prep_res, exec_res: List[str]) -> str:
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

    def exec(self, inputs: tuple) -> CandidateBatch:
        return self.search(*inputs)

    def search(
        self,
        queries: List[str],
        metadata_filter: dict | None,
        mode: str,
        top_k: int | None = None,
    ) -> CandidateBatch:
        """Run Dense and/or BM25 retrieval against one captured version."""
        result_limit = top_k or settings.rerank_top_k
        vector_limit = max(settings.vector_top_k, result_limit)
        bm25_limit = max(settings.bm25_top_k, result_limit)
        use_vector = mode in ("vector_only", "hybrid", "hybrid+rerank")
        use_bm25 = mode in ("bm25_only", "hybrid", "hybrid+rerank")
        active_index = index_catalog.capture()
        collection = self._get_collection(active_index.collection_name)
        allowed_chunk_ids = self._resolve_allowed_chunk_ids(
            metadata_filter,
            collection,
        )

        all_vector_hits: Dict[str, Tuple[int, float]] = {}
        all_bm25_hits: Dict[str, Tuple[int, float]] = {}
        chunk_map: Dict[str, dict] = {}

        for qi, query in enumerate(queries):
            # --- 向量检索 ---
            if use_vector:
                vec_results = self._vector_search(
                    query,
                    metadata_filter,
                    collection,
                    top_k=vector_limit,
                )
                for rank, item in enumerate(vec_results):
                    cid = item["chunk_id"]
                    if (
                        allowed_chunk_ids is not None
                        and cid not in allowed_chunk_ids
                    ):
                        continue
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
                bm25_results = bm25_store.search(
                    query,
                    top_k=bm25_limit,
                    allowed_chunk_ids=allowed_chunk_ids,
                    version_id=active_index.version_id,
                )
                for rank, (cid, score) in enumerate(bm25_results):
                    if (
                        allowed_chunk_ids is not None
                        and cid not in allowed_chunk_ids
                    ):
                        continue
                    if cid not in all_bm25_hits or rank < all_bm25_hits[cid][0]:
                        all_bm25_hits[cid] = (rank, score)
                    # BM25 不返回原文，从 ChromaDB 按相同 scope 补拉。
                    if cid not in chunk_map:
                        chunk = self._fetch_chunk_text(
                            cid,
                            metadata_filter,
                            collection,
                        )
                        if chunk is not None:
                            chunk_map[cid] = chunk

        logger.info("Vector hits: {} unique, BM25 hits: {} unique",
                     len(all_vector_hits), len(all_bm25_hits))

        # --- RRF 融合 ---
        from src.utils.rrf import compute_rrf

        vector_ranks = {cid: rank for cid, (rank, _) in all_vector_hits.items()}
        bm25_ranks = {cid: rank for cid, (rank, _) in all_bm25_hits.items()}

        sorted_chunks = compute_rrf(vector_ranks, bm25_ranks, k=settings.rrf_k)
        top_n = sorted_chunks[:vector_limit]

        results = []
        for cid, rrf_score in top_n:
            if cid in chunk_map:
                results.append({
                    **chunk_map[cid],
                    "rrf_score": round(rrf_score, 4),
                })

        logger.info("RRF merged: {} → {} candidates (mode={})",
                     len(sorted_chunks), len(results), mode)
        return CandidateBatch(
            index_version=active_index.version_id,
            candidates=tuple(results),
        )

    def _resolve_allowed_chunk_ids(
        self,
        metadata_filter: dict | None,
        collection,
    ) -> set[str] | None:
        """Resolve the Chroma scope once for BM25 and fusion."""
        if not metadata_filter:
            return None
        if collection is None:
            return set()

        result = collection.get(where=metadata_filter, include=[])
        return set(result.get("ids", []))

    def _fetch_chunk_text(
        self,
        chunk_id: str,
        metadata_filter: dict | None,
        collection,
    ) -> dict | None:
        """从本次请求固定的 Chroma collection 补拉 BM25 原文。"""
        try:
            if collection is None:
                return None
            kwargs = {"ids": [chunk_id]}
            if metadata_filter:
                kwargs["where"] = metadata_filter
            result = collection.get(**kwargs)
            if result["documents"]:
                return {
                    "chunk_id": chunk_id,
                    "text": result["documents"][0],
                    "metadata": result["metadatas"][0] if result["metadatas"] else {},
                }
        except Exception as exc:
            logger.warning(
                "Failed to fetch scoped chunk: {}", type(exc).__name__
            )
        return None

    def _get_collection(self, collection_name: str):
        client = chromadb.PersistentClient(
            path=str(settings.chroma_path.resolve()),
            settings=chromadb.config.Settings(anonymized_telemetry=False),
        )
        try:
            return client.get_collection(collection_name)
        except Exception:
            logger.warning(
                "ChromaDB collection {} not found, run build_index.py first",
                collection_name,
            )
            return None

    def _vector_search(
        self,
        query: str,
        metadata_filter: dict | None,
        collection,
        top_k: int | None = None,
    ) -> List[dict]:
        """Search the vector collection captured for this request."""
        query_vec = llm_client.embed_single(query)
        if collection is None:
            return []

        kwargs = dict(
            query_embeddings=[query_vec],
            n_results=top_k or settings.vector_top_k,
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

    def post(self, shared: dict, prep_res, exec_res: CandidateBatch) -> str:
        shared["candidates"] = list(exec_res)
        shared["index_version"] = exec_res.index_version
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
        return self.rerank(*inputs)

    def rerank(
        self,
        query: str,
        candidates: List[dict],
        top_k: int | None = None,
    ) -> List[dict]:
        """Score and return the requested number of ranked candidates."""
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
        top = ranked[: top_k or settings.rerank_top_k]

        logger.info("Reranked: {} → {} chunks, top score: {:.4f}",
                     len(candidates), len(top),
                     top[0]["rerank_score"] if top else 0)
        return top

    def post(self, shared: dict, prep_res, exec_res: List[dict]) -> str:
        shared["retrieved_chunks"] = exec_res
        return "default"
