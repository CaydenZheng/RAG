"""
PocketFlow 顶层编排：离线索引 Flow + 在线检索 Flow（异步）。

离线: DocLoader → DocDeduplicator → Chunker → Embedder → IndexBuilder
在线: KnowledgeSystem → ContextBuilder → Generator
流式: KnowledgeSystem → ContextBuilder，SSE 生成由 app.py 处理
"""

from pocketflow import AsyncFlow, AsyncNode, Flow

from src.core.generation import ContextBuilderNode, GeneratorNode
from src.core.indexing import EmbedderNode, IndexBuilderNode
from src.core.ingestion import ChunkerNode, DocDeduplicatorNode, DocLoaderNode
from src.core.knowledge import (
    DEFAULT_RETRIEVAL_MODE,
    DEFAULT_RETRIEVAL_TOP_K,
    KnowledgeSystem,
    RetrievalResult,
    knowledge_system,
)

# ================================================================
# 离线索引 Flow
# ================================================================

def create_offline_flow() -> Flow:
    """文档摄入 → 分块 → Embedding → 双路索引"""
    loader = DocLoaderNode()
    dedup = DocDeduplicatorNode()
    chunker = ChunkerNode()
    embedder = EmbedderNode()
    indexer = IndexBuilderNode()

    loader >> dedup >> chunker >> embedder >> indexer
    return Flow(start=loader)


# ================================================================
# KnowledgeSystem PocketFlow adapter
# ================================================================

class KnowledgeRetrievalNode(AsyncNode):
    """Adapt the framework-free KnowledgeSystem result to the shared store."""

    def __init__(self, system: KnowledgeSystem | None = None) -> None:
        super().__init__()
        self._system = system or knowledge_system

    async def prep_async(self, shared: dict) -> tuple:
        return (
            shared.get("query", ""),
            shared.get("top_k", DEFAULT_RETRIEVAL_TOP_K),
            shared.get("filter"),
            shared.get("retrieval_mode", DEFAULT_RETRIEVAL_MODE),
        )

    async def exec_async(self, inputs: tuple) -> RetrievalResult:
        query, top_k, metadata_filter, mode = inputs
        return await self._system.retrieve(
            query,
            top_k=top_k,
            metadata_filter=metadata_filter,
            mode=mode,
        )

    async def post_async(
        self, shared: dict, prep_res: tuple, exec_res: RetrievalResult
    ) -> str:
        shared["queries"] = exec_res.query_variants
        shared["candidates"] = exec_res.candidates
        shared["retrieved_chunks"] = exec_res.chunks
        shared["warnings"] = list(exec_res.warnings)
        shared["index_version"] = exec_res.index_version
        return "default"


# ================================================================
# 在线检索 Flow
# ================================================================

def create_online_flow() -> AsyncFlow:
    """查询改写 → 混合检索 → Rerank → 上下文构建 → 答案生成（异步）"""
    retrieval = KnowledgeRetrievalNode()
    builder = ContextBuilderNode()
    generator = GeneratorNode()

    retrieval >> builder >> generator
    return AsyncFlow(start=retrieval)


def create_retrieval_flow() -> AsyncFlow:
    """
    仅检索管线（不含生成），供流式端点使用。
    KnowledgeSystem → ContextBuilder
    """
    retrieval = KnowledgeRetrievalNode()
    builder = ContextBuilderNode()

    retrieval >> builder
    return AsyncFlow(start=retrieval)


# ================================================================
# 全局实例（延迟加载）
# ================================================================

_offline_flow = None
_online_flow = None
_retrieval_flow = None


def get_offline_flow() -> Flow:
    global _offline_flow
    if _offline_flow is None:
        _offline_flow = create_offline_flow()
    return _offline_flow


def get_online_flow() -> AsyncFlow:
    global _online_flow
    if _online_flow is None:
        _online_flow = create_online_flow()
    return _online_flow


def get_retrieval_flow() -> AsyncFlow:
    """获取仅检索的 Flow（用于流式端点）"""
    global _retrieval_flow
    if _retrieval_flow is None:
        _retrieval_flow = create_retrieval_flow()
    return _retrieval_flow
