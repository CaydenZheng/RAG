#!/usr/bin/env python
"""
RAG 消融实验 — 4 组对照 + RAGAS 标准指标评估。

A: 纯向量检索
B: 纯 BM25 关键词检索
C: 混合检索 (Vector + BM25 + RRF)
D: 混合检索 + Rerank（最终方案）

用法:
    python scripts/run_eval.py
    python scripts/run_eval.py --testset data/testset/nq_test.json
"""

import sys
import json
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from typing import List
from loguru import logger
from pocketflow import Flow, Node

from config.settings import settings
from src.core.retrieval import QueryRewriterNode, HybridRetrieverNode, RerankerNode
from src.core.generation import ContextBuilderNode, GeneratorNode
from src.utils.token_counter import count_tokens


# ================================================================
# LangChain-compatible local embeddings wrapper (for RAGAS)
# ================================================================

class LocalRagasEmbeddings:
    """
    RAGAS evaluate() 需要 embeddings 参数来计算 AnswerRelevancy 等指标。
    项目使用本地 bge embedding，不走 OpenAI API，所以包装一个
    LangChain duck-type compatible 的 embeddings 类。

    只需实现 embed_documents 和 embed_query 两个方法即可。
    """

    def __init__(self, model_name: str = "BAAI/bge-base-en-v1.5"):
        from sentence_transformers import SentenceTransformer
        self._model = SentenceTransformer(model_name, device="cpu")

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        embeddings = self._model.encode(
            texts,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return embeddings.tolist()

    def embed_query(self, text: str) -> List[float]:
        return self.embed_documents([text])[0]


# ================================================================
# 轻量直通节点（跳过某些步骤）
# ================================================================

class PassThroughReranker(Node):
    """跳过 Rerank，直接透传 candidates → retrieved_chunks"""
    def prep(self, shared):
        return shared.get("candidates", [])
    def exec(self, items):
        for i, c in enumerate(items):
            c["rerank_score"] = c.get("rrf_score", 0)
        return sorted(items, key=lambda x: x.get("rerank_score", 0), reverse=True)[:settings.rerank_top_k]
    def post(self, shared, prep_res, exec_res):
        shared["retrieved_chunks"] = exec_res
        return "default"


class SkipRewriteNode(Node):
    """跳过查询改写，直接用原 query"""
    def prep(self, shared):
        return shared.get("query", "")
    def exec(self, q):
        return [q]
    def post(self, shared, prep_res, exec_res):
        shared["queries"] = exec_res
        return "default"


# ================================================================
# 构建各组 Flow
# ================================================================

def build_ablation_flows():
    """返回 4 组 Flow 及其名称"""
    flows = {}

    # --- Group A: Vector only ---
    rewriter = SkipRewriteNode()
    retriever = HybridRetrieverNode()
    reranker = PassThroughReranker()
    builder = ContextBuilderNode()
    generator = GeneratorNode()
    rewriter >> retriever >> reranker >> builder >> generator
    flows["A. Vector Only"] = (Flow(start=rewriter), "vector_only")

    # --- Group B: BM25 only ---
    rewriter_b = SkipRewriteNode()
    retriever_b = HybridRetrieverNode()
    reranker_b = PassThroughReranker()
    builder_b = ContextBuilderNode()
    generator_b = GeneratorNode()
    rewriter_b >> retriever_b >> reranker_b >> builder_b >> generator_b
    flows["B. BM25 Only"] = (Flow(start=rewriter_b), "bm25_only")

    # --- Group C: Hybrid (no Rerank) ---
    rewriter_c = QueryRewriterNode()
    retriever_c = HybridRetrieverNode()
    reranker_c = PassThroughReranker()
    builder_c = ContextBuilderNode()
    generator_c = GeneratorNode()
    rewriter_c >> retriever_c >> reranker_c >> builder_c >> generator_c
    flows["C. Hybrid (RRF)"] = (Flow(start=rewriter_c), "hybrid")

    # --- Group D: Full Pipeline ---
    rewriter_d = QueryRewriterNode()
    retriever_d = HybridRetrieverNode()
    reranker_d = RerankerNode()
    builder_d = ContextBuilderNode()
    generator_d = GeneratorNode()
    rewriter_d >> retriever_d >> reranker_d >> builder_d >> generator_d
    flows["D. Hybrid + Rerank"] = (Flow(start=rewriter_d), "hybrid+rerank")

    return flows


# ================================================================
# 冷启动预热
# ================================================================

def _warmup_bm25():
    """从 ChromaDB 重建 BM25，保证混合检索可区分向量和关键词贡献"""
    try:
        import chromadb
        from src.utils.bm25_store import bm25_store
        from config.settings import settings

        client = chromadb.PersistentClient(
            path=str(settings.chroma_path.resolve()),
            settings=chromadb.config.Settings(anonymized_telemetry=False),
        )
        collection = client.get_collection("rag_collection")
        if collection.count() == 0:
            logger.warning("ChromaDB empty, BM25 won't be available")
            return

        all_data = collection.get()
        texts = all_data["documents"] or []
        chunk_ids = all_data["ids"] or []
        bm25_store.build(texts, chunk_ids)
        logger.info("✅ BM25 warmed up: {} docs", len(texts))
    except Exception as e:
        logger.warning("BM25 warmup failed: {}", e)


# ================================================================
# 主流程
# ================================================================

def run_ablation(testset_path: str):
    """运行消融实验"""
    with open(testset_path, "r", encoding="utf-8") as f:
        testset = json.load(f)

    logger.info("=" * 60)
    logger.info("Running ablation study: {} questions x 4 groups", len(testset))
    logger.info("=" * 60)

    # 清空缓存，避免跨 session 污染实验结果
    from src.llm.cache import llm_cache
    llm_cache.clear()

    # 冷启动：从 ChromaDB 重建 BM25（eval 脚本是新进程，BM25 需要重建）
    _warmup_bm25()

    flows = build_ablation_flows()
    all_results = {}  # group_name → list of QA results

    for group_name, (flow, mode) in flows.items():
        logger.info("\n--- {} ---", group_name)
        qa_results = []

        for i, item in enumerate(testset):
            question = item["question"]
            logger.info("  [{}/{}] {}", i + 1, len(testset), question[:60])

            shared = {
                "query": question,
                "retrieval_mode": mode,
            }

            try:
                flow.run(shared)
            except Exception as e:
                logger.warning("  ⚠️ Failed: {}", e)
                shared["answer"] = ""
                shared["retrieved_chunks"] = []

            qa_results.append({
                "question": question,
                "answer": shared.get("answer", ""),
                "contexts": [c["text"] for c in shared.get("retrieved_chunks", [])],
                "ground_truth": item["ground_truth"],
            })

            time.sleep(0.3)  # 避免 API 限流

        all_results[group_name] = qa_results

    # --- 输出对比 ---
    print("\n" + "=" * 60)
    print("ABLATION RESULTS")
    print("=" * 60)

    for group_name, qa_results in all_results.items():
        # 简单统计
        avg_answer_len = sum(len(r["answer"]) for r in qa_results) / max(len(qa_results), 1)
        avg_contexts = sum(len(r["contexts"]) for r in qa_results) / max(len(qa_results), 1)

        print(f"\n{group_name}:")
        print(f"  Avg answer length: {avg_answer_len:.0f} chars")
        print(f"  Avg contexts:      {avg_contexts:.1f} chunks")

    # --- 检索层指标（纯规则，不依赖 LLM） ---
    print("\n" + "-" * 60)
    print("RETRIEVAL METRICS (rule-based)")
    print("-" * 60)
    retrieval_metrics = compute_retrieval_metrics(all_results)
    for group_name, metrics in retrieval_metrics.items():
        print(f"\n{group_name}:")
        for k, v in metrics.items():
            print(f"  {k}: {v:.4f}")

    # --- RAGAS 评估（4 组一起跑） ---
    print("\n" + "-" * 60)
    print("RAGAS METRICS")
    print("-" * 60)
    ragas_metrics = run_ragas_eval(all_results)
    if ragas_metrics:
        for group_name, metrics in ragas_metrics.items():
            print(f"\n{group_name}:")
            for k, v in metrics.items():
                if v is not None:
                    print(f"  {k}: {v:.4f}")

    # --- 写入文件 ---
    _write_results(all_results, testset, ragas_metrics, retrieval_metrics)


def _write_results(all_results: dict, testset: list, ragas_metrics: dict = None, retrieval_metrics: dict = None):
    """将消融结果写入 ablation_results.txt"""
    lines = []
    lines.append("=" * 60)
    lines.append("RAG ABLATION STUDY RESULTS")
    lines.append("=" * 60)
    lines.append(f"Questions: {len(testset)}")
    lines.append("")

    for group_name, qa_results in all_results.items():
        avg_answer_len = sum(len(r["answer"]) for r in qa_results) / max(len(qa_results), 1)
        avg_contexts = sum(len(r["contexts"]) for r in qa_results) / max(len(qa_results), 1)

        lines.append(f"--- {group_name} ---")
        lines.append(f"Avg answer length: {avg_answer_len:.0f} chars")
        lines.append(f"Avg contexts:      {avg_contexts:.1f} chunks")

        if retrieval_metrics and group_name in retrieval_metrics:
            lines.append("Retrieval (rule-based):")
            for k, v in retrieval_metrics[group_name].items():
                lines.append(f"  {k}: {v:.4f}")

        if ragas_metrics and group_name in ragas_metrics:
            lines.append("RAGAS (LLM judge):")
            for k, v in ragas_metrics[group_name].items():
                if v is not None:
                    lines.append(f"  {k}: {v:.4f}")

        lines.append("")

    # 逐题详情
    lines.append("-" * 60)
    lines.append("PER-QUESTION DETAILS")
    lines.append("-" * 60)

    for i, item in enumerate(testset):
        lines.append(f"\nQ{i+1}: {item['question']}")
        lines.append(f"Ground Truth: {item['ground_truth'][:200]}...")
        for group_name, qa_results in all_results.items():
            answer = qa_results[i]["answer"] if i < len(qa_results) else ""
            lines.append(f"  [{group_name}] {answer[:300]}{'...' if len(answer)>300 else ''}")

    with open("ablation_results.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    logger.info("📄 Results written to ablation_results.txt")


def _has_significant_overlap(chunk: str, sentence: str, threshold: float = 0.3) -> bool:
    """判断 chunk 与 ground truth 句子是否有显著词级重叠。"""
    chunk_tokens = set(chunk.lower().split())
    sent_tokens = set(sentence.lower().split())
    if not sent_tokens:
        return False
    overlap = len(chunk_tokens & sent_tokens) / len(sent_tokens)
    return overlap >= threshold


def compute_retrieval_metrics(all_results: dict) -> dict:
    """
    纯规则计算检索层指标（不依赖 LLM）。

    - Hit Rate@K: top-K chunk 中是否有任意一条与 ground truth 显著重叠
    - MRR: 第一个相关 chunk 排名的倒数均值

    与 RAGAS Context Precision/Recall 互补——这里是用规则算的"硬"检索指标，
    RAGAS 是用 LLM judge 算的"软"语义指标。
    """
    metrics = {}
    for group_name, qa_results in all_results.items():
        hit_5, hit_10 = 0, 0
        mrr_scores = []

        for r in qa_results:
            gt = r["ground_truth"]
            contexts = r["contexts"]

            # 拆 ground truth 为句子（忽略太短的片段）
            gt_sentences = [s.strip() for s in gt.replace('\n', '. ').split('.') if len(s.strip()) > 15]
            if not gt_sentences:
                continue

            # Hit Rate@5 和 @10
            for k in (5, 10):
                for chunk in contexts[:k]:
                    if any(_has_significant_overlap(chunk, s) for s in gt_sentences):
                        if k == 5:
                            hit_5 += 1
                        if k == 10:
                            hit_10 += 1
                        break

            # MRR: 第一个相关 chunk 排名的倒数
            for rank, chunk in enumerate(contexts, 1):
                if any(_has_significant_overlap(chunk, s) for s in gt_sentences):
                    mrr_scores.append(1.0 / rank)
                    break
            else:
                mrr_scores.append(0.0)

        n = len(qa_results)
        metrics[group_name] = {
            "hit_rate@5": round(hit_5 / n, 4) if n else 0,
            "hit_rate@10": round(hit_10 / n, 4) if n else 0,
            "mrr": round(sum(mrr_scores) / n, 4) if n else 0,
        }

    return metrics


def _evaluate_group(name: str, qa_results: list, evaluator_llm, local_embeddings) -> dict:
    """对单个消融组做 RAGAS 评估（供 ThreadPoolExecutor 并行调用）。"""
    from ragas import evaluate
    from ragas.metrics import (
        Faithfulness,
        AnswerRelevancy,
        ContextPrecision,
        ContextRecall,
    )
    from datasets import Dataset

    # 过滤掉空答案的样本（RAGAS 要求非空 answer）
    valid = [r for r in qa_results if r["answer"].strip()]
    if len(valid) < 2:
        logger.warning("{}: not enough valid answers ({})", name, len(valid))
        return {}

    logger.info("Running RAGAS on {} ({} samples)...", name, len(valid))

    dataset = Dataset.from_dict({
        "question": [r["question"] for r in valid],
        "answer": [r["answer"] for r in valid],
        "contexts": [r["contexts"] for r in valid],
        "ground_truth": [r["ground_truth"] for r in valid],
    })

    result = evaluate(
        dataset,
        metrics=[
            ContextPrecision(),
            ContextRecall(),
            Faithfulness(),
            AnswerRelevancy(),
        ],
        llm=evaluator_llm,
        embeddings=local_embeddings,
    )
    # RAGAS 0.4.3: _repr_dict 包含各指标均值
    scores = result._repr_dict
    return {
        "context_precision": round(float(scores.get("context_precision", 0)), 4),
        "context_recall": round(float(scores.get("context_recall", 0)), 4),
        "faithfulness": round(float(scores.get("faithfulness", 0)), 4),
        "answer_relevancy": round(float(scores.get("answer_relevancy", 0)), 4),
    }


def run_ragas_eval(all_results: dict) -> dict:
    """
    RAGAS 标准指标评估，使用 DeepSeek 作为 evaluator LLM。

    4 个消融组（A/B/C/D）并行评估。各组之间零共享状态，指标结果
    与串行完全一致。RAGAS 内部也会并行处理各 sample。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from ragas.llms import llm_factory
    from openai import OpenAI

    evaluator_llm = llm_factory(
        settings.llm_model,
        client=OpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
        ),
    )

    # 显式传入本地 embeddings，避免 RAGAS 内部自动创建 OpenAIEmbeddings 报错
    local_embeddings = LocalRagasEmbeddings(settings.local_embedding_model)

    metrics = {}
    n_groups = len(all_results)

    # 4 组并行评估 — I/O 密集型，线程池天然适配
    with ThreadPoolExecutor(max_workers=min(4, n_groups)) as pool:
        futures = {
            pool.submit(_evaluate_group, name, results, evaluator_llm, local_embeddings): name
            for name, results in all_results.items()
        }
        for f in as_completed(futures):
            name = futures[f]
            try:
                metrics[name] = f.result()
                logger.info("✅ RAGAS done for {}", name)
            except Exception as e:
                logger.warning("RAGAS failed for {}: {}", name, e)
                metrics[name] = {}

    return metrics


def print_comparison_table(all_results: dict):
    """打印对比表格"""
    metrics_names = ["context_precision", "context_recall", "faithfulness", "answer_relevancy"]

    # Header
    header = f"{'Metric':<25}"
    for name in all_results:
        header += f" {name:<20}"
    print("\n" + header)
    print("-" * len(header))

    # Rows
    for metric in metrics_names:
        row = f"{metric:<25}"
        for group_results in all_results.values():
            # Compute metric for this group
            try:
                metrics = run_ragas_eval(group_results, "")
                val = metrics.get(metric, "N/A")
                row += f" {str(val):<20}"
            except Exception:
                row += f" {'N/A':<20}"
        print(row)


# ================================================================
# Entry
# ================================================================

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--testset", default="data/testset/ground_truth.json")
    args = parser.parse_args()

    run_ablation(args.testset)
