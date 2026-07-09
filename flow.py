"""
PocketFlow 顶层编排：离线索引 Flow + 在线检索 Flow（异步）。

离线: DocLoader → DocDeduplicator → Chunker → Embedder → IndexBuilder
在线: QueryRewriter → HybridRetriever → Reranker → ContextBuilder → Generator
流式: 同在线但 Generator 替换为流式 SSE 输出（在 app.py 中手动处理）
"""

from pocketflow import Flow, AsyncFlow

from src.core.ingestion import DocLoaderNode, DocDeduplicatorNode, ChunkerNode
from src.core.indexing import EmbedderNode, IndexBuilderNode
from src.core.retrieval import QueryRewriterNode, HybridRetrieverNode, RerankerNode
from src.core.generation import ContextBuilderNode, GeneratorNode


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
# 在线检索 Flow
# ================================================================

def create_online_flow() -> AsyncFlow:
    """查询改写 → 混合检索 → Rerank → 上下文构建 → 答案生成（异步）"""
    rewriter = QueryRewriterNode()
    retriever = HybridRetrieverNode()
    reranker = RerankerNode()
    builder = ContextBuilderNode()
    generator = GeneratorNode()

    rewriter >> retriever >> reranker >> builder >> generator
    return AsyncFlow(start=rewriter)


def create_retrieval_flow() -> AsyncFlow:
    """
    仅检索管线（不含生成），供流式端点使用。
    QueryRewriter → HybridRetriever → Reranker → ContextBuilder
    """
    rewriter = QueryRewriterNode()
    retriever = HybridRetrieverNode()
    reranker = RerankerNode()
    builder = ContextBuilderNode()

    rewriter >> retriever >> reranker >> builder
    return AsyncFlow(start=rewriter)


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
