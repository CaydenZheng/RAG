"""Optional model-based quality judges.

This module is imported only when a caller explicitly opts into slow evaluation.
"""

from __future__ import annotations

from typing import Any

from config.settings import settings


class LocalRagasEmbeddings:
    """LangChain-compatible wrapper around the configured local embeddings."""

    def __init__(self, model_name: str) -> None:
        from sentence_transformers import SentenceTransformer

        self._model = SentenceTransformer(model_name, device="cpu")

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        embeddings = self._model.encode(
            texts,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return embeddings.tolist()

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]


class RagasJudge:
    """Batch Faithfulness and Relevancy scoring through the configured provider."""

    name = "ragas"

    def score(
        self,
        results: list[dict[str, Any]],
    ) -> dict[str, dict[str, float]]:
        from datasets import Dataset
        from openai import OpenAI
        from ragas import evaluate
        from ragas.llms import llm_factory
        from ragas.metrics import AnswerRelevancy, Faithfulness

        eligible = [
            result
            for result in results
            if result["expected_behavior"] == "answer"
            and not result["error_code"]
            and result["answer"].strip()
            and result["retrieved_chunks"]
        ]
        if not eligible:
            return {}

        dataset = Dataset.from_dict(
            {
                "question": [result["question"] for result in eligible],
                "answer": [result["answer"] for result in eligible],
                "contexts": [
                    [chunk.get("text", "") for chunk in result["retrieved_chunks"]]
                    for result in eligible
                ],
                "ground_truth": [result["ground_truth"] for result in eligible],
            }
        )
        evaluator = llm_factory(
            settings.llm_model,
            client=OpenAI(
                api_key=settings.openai_api_key,
                base_url=settings.openai_base_url,
            ),
        )
        evaluated = evaluate(
            dataset,
            metrics=[Faithfulness(), AnswerRelevancy()],
            llm=evaluator,
            embeddings=LocalRagasEmbeddings(settings.local_embedding_model),
        )
        frame = evaluated.to_pandas()
        scores = {}
        for result, (_, row) in zip(eligible, frame.iterrows(), strict=True):
            scores[result["sample_id"]] = {
                "faithfulness": float(row["faithfulness"]),
                "relevancy": float(row["answer_relevancy"]),
            }
        return scores
