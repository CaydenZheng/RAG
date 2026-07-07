"""
RRF 融合公式单元测试 — 覆盖 5 个场景。

用法:
    python tests/test_rrf.py
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.utils.rrf import compute_rrf


def test_single_source_vector_only():
    """单一来源（只有向量）：按 rank 排序"""
    result = compute_rrf(
        vector_ranks={"A": 0, "B": 1, "C": 2},
        bm25_ranks={},
        k=60,
    )
    assert len(result) == 3
    assert result[0][0] == "A"      # rank 0 → 最高分
    assert result[2][0] == "C"      # rank 2 → 最低分
    assert result[0][1] > result[1][1] > result[2][1]


def test_single_source_bm25_only():
    """单一来源（只有 BM25）：按 rank 排序"""
    result = compute_rrf(
        vector_ranks={},
        bm25_ranks={"X": 0, "Y": 1},
        k=60,
    )
    assert len(result) == 2
    assert result[0][0] == "X"


def test_dual_source_same_doc():
    """同一文档在两边都有排名 → 分数累加，应高于单源同 rank"""
    result = compute_rrf(
        vector_ranks={"A": 0, "B": 1},
        bm25_ranks={"A": 2},          # A 在 BM25 排第 3
        k=60,
    )
    # A 得分 = 1/61 + 1/63 > B 得分 = 1/62
    score_a = result[0][1]  # A should be first
    score_b = result[1][1]
    assert result[0][0] == "A"
    assert score_a > score_b


def test_k_value_sensitivity():
    """k 越小，高排名优势越大"""
    ranks_v = {"A": 0, "B": 5}
    ranks_b = {}

    result_k1 = compute_rrf(ranks_v, ranks_b, k=1)
    result_k60 = compute_rrf(ranks_v, ranks_b, k=60)

    # k=1 时 A 的分数远超 B；k=60 时差距更平滑
    gap_k1 = result_k1[0][1] / result_k1[1][1]
    gap_k60 = result_k60[0][1] / result_k60[1][1]
    assert gap_k1 > gap_k60, f"k=1 gap ({gap_k1:.2f}) should be > k=60 gap ({gap_k60:.2f})"


def test_exact_values():
    """手动计算验证精确值"""
    # A: vector rank 0, bm25 rank 1
    # B: vector rank 1, bm25 没有
    # C: bm25 rank 0, vector 没有
    result = compute_rrf(
        vector_ranks={"A": 0, "B": 1},
        bm25_ranks={"A": 1, "C": 0},
        k=60,
    )

    # A: 1/(60+0+1) + 1/(60+1+1) = 1/61 + 1/62 = 0.0163934 + 0.0161290 = 0.0325225
    # C: 1/(60+0+1) = 1/61 = 0.0163934
    # B: 1/(60+1+1) = 1/62 = 0.0161290
    expected_a = 1/61 + 1/62
    expected_c = 1/61
    expected_b = 1/62

    scores = {cid: s for cid, s in result}
    assert abs(scores["A"] - expected_a) < 0.0001
    assert abs(scores["B"] - expected_b) < 0.0001
    assert abs(scores["C"] - expected_c) < 0.0001

    # 排序验证
    assert result[0][0] == "A"
    assert result[1][0] == "C"
    assert result[2][0] == "B"


def test_empty_input():
    """两边都空 → 返回空列表"""
    result = compute_rrf({}, {}, k=60)
    assert result == []


def test_partial_overlap():
    """部分重叠 + 独有文档"""
    result = compute_rrf(
        vector_ranks={"shared": 0, "vec_only": 1},
        bm25_ranks={"shared": 0, "bm25_only": 1},
        k=60,
    )
    # shared 在两路都排第一 → 总分最高
    assert result[0][0] == "shared"
    assert len(result) == 3


# ================================================================
# Runner
# ================================================================

if __name__ == "__main__":
    tests = [
        ("single source (vector only)", test_single_source_vector_only),
        ("single source (BM25 only)", test_single_source_bm25_only),
        ("dual source same doc", test_dual_source_same_doc),
        ("k value sensitivity", test_k_value_sensitivity),
        ("exact values", test_exact_values),
        ("empty input", test_empty_input),
        ("partial overlap", test_partial_overlap),
    ]

    passed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ✅ {name}")
            passed += 1
        except AssertionError as e:
            print(f"  ❌ {name}: {e}")
        except Exception as e:
            print(f"  💥 {name}: {e}")

    print(f"\n{passed}/{len(tests)} tests passed")
    if passed == len(tests):
        print("🎉 All RRF tests passed!")
    else:
        sys.exit(1)
