"""
Ragas 评估节点 — 计算 RAG 核心指标。

metrics: Context Precision, Context Recall, Faithfulness,
         Answer Relevance, Answer Correctness
"""

from typing import List, Dict
from pocketflow import Node
from loguru import logger


# ================================================================
# LangChain-compatible local embeddings wrapper (for RAGAS)
# ================================================================

class LocalRagasEmbeddings:
    """本地 bge embedding 的 LangChain duck-type 包装，供 RAGAS evaluate() 使用。"""

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


class RagasEvaluatorNode(Node):
    """对一批 QA 结果做 Ragas 评估"""

    def prep(self, shared: dict) -> dict:
        """获取评估所需数据"""
        return {
            "qa_results": shared.get("eval_results", []),
            "run_name": shared.get("eval_run_name", "default"),
        }

    def exec(self, inputs: dict) -> dict:
        qa_results = inputs["qa_results"]

        if not qa_results:
            return {"error": "No evaluation results to assess"}

        try:
            from ragas import evaluate
            from ragas.metrics import (
                Faithfulness,
                AnswerRelevancy,
                ContextPrecision,
                ContextRecall,
            )
            from datasets import Dataset
            from config.settings import settings

            # 构建 Dataset
            dataset = Dataset.from_dict({
                "question": [r["question"] for r in qa_results],
                "answer": [r["answer"] for r in qa_results],
                "contexts": [r["contexts"] for r in qa_results],
                "ground_truth": [r["ground_truth"] for r in qa_results],
            })

            logger.info("Running Ragas evaluation on {} samples...", len(qa_results))

            # 本地 embeddings（避免 RAGAS 自动创建 OpenAIEmbeddings 报错）
            local_embeddings = LocalRagasEmbeddings(settings.local_embedding_model)

            result = evaluate(
                dataset,
                metrics=[
                    ContextPrecision(),
                    ContextRecall(),
                    Faithfulness(),
                    AnswerRelevancy(),
                ],
                llm=None,  # 使用 RAGAS 默认 LLM judge
                embeddings=local_embeddings,
            )

            # RAGAS 0.1.x: result 是 dict; 0.2.x+: result 有 _repr_dict
            if hasattr(result, '_repr_dict'):
                scores = result._repr_dict
            elif isinstance(result, dict):
                scores = result
            else:
                scores = {}

            metrics = {
                "context_precision": round(float(scores.get("context_precision", 0)), 4),
                "context_recall": round(float(scores.get("context_recall", 0)), 4),
                "faithfulness": round(float(scores.get("faithfulness", 0)), 4),
                "answer_relevancy": round(float(scores.get("answer_relevancy", 0)), 4),
            }

            logger.info("Ragas metrics: {}", metrics)
            return metrics

        except Exception as e:
            logger.error("Ragas evaluation failed: {}", e)
            return {"error": str(e)}

    def post(self, shared: dict, prep_res, exec_res: dict) -> str:
        shared["eval_metrics"] = exec_res
        return "default"
