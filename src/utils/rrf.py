"""
RRF (Reciprocal Rank Fusion) 纯函数 — 从检索模块提取，方便单元测试。

公式: RRF_score(d) = Σ_i 1 / (k + rank_i(d) + 1)

其中 rank_i 是 chunk d 在第 i 路检索结果中的 0-indexed 排名。
k 控制高排名 vs 低排名的权重平衡（k 越小越强调高排名结果）。
"""

from typing import Dict, List, Tuple


def compute_rrf(
    vector_ranks: Dict[str, int],
    bm25_ranks: Dict[str, int],
    k: int = 60,
) -> List[Tuple[str, float]]:
    """
    对向量和 BM25 两路检索的排名做 RRF 融合，返回按分数降序的 (chunk_id, score) 列表。

    Args:
        vector_ranks: chunk_id → 0-indexed rank（越小越靠前）
        bm25_ranks:   chunk_id → 0-indexed rank
        k:            RRF 平滑参数（默认 60）

    Returns:
        [(chunk_id, rrf_score), ...]，按分数降序

    Example:
        >>> compute_rrf({"A": 0, "B": 1}, {"A": 2, "C": 0}, k=60)
        [('A', 0.0322...), ('C', 0.0163...), ('B', 0.0161...)]
    """
    scores: Dict[str, float] = {}

    for cid, rank in vector_ranks.items():
        scores[cid] = scores.get(cid, 0) + 1.0 / (k + rank + 1)

    for cid, rank in bm25_ranks.items():
        scores[cid] = scores.get(cid, 0) + 1.0 / (k + rank + 1)

    return sorted(scores.items(), key=lambda x: x[1], reverse=True)
