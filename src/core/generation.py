"""
P2: 生成层

ContextBuilderNode  → 按分数降序排列 chunk，逐 chunk 累加 token，触达预算停止
GeneratorNode       → 调用 LLM 生成带引用的答案
"""

from typing import List

from pocketflow import Node
from loguru import logger

from config.settings import settings
from src.llm import llm_client
from src.infra.prompt_manager import prompt_manager
from src.utils.token_counter import count_tokens


# ================================================================
# P2-7: ContextBuilderNode
# ================================================================

class ContextBuilderNode(Node):
    """
    构建 LLM 上下文：
    1. chunk 按 rerank_score (或 rrf_score) 降序排列
    2. 预留 system prompt + user query 的 token 配额
    3. 逐个累加完整 chunk token，触及预算停止
    4. 绝不拆分单个 chunk（保证引用标记完整性）
    """

    def prep(self, shared: dict):
        chunks = shared.get("retrieved_chunks", [])
        return chunks

    def exec(self, chunks: List[dict]) -> dict:
        if not chunks:
            return {"context": "No relevant documents found.", "sources": []}

        # 按 rerank_score 降序（若无则用 rrf_score）
        sorted_chunks = sorted(
            chunks,
            key=lambda c: c.get("rerank_score", c.get("rrf_score", 0)),
            reverse=True,
        )

        # Token 预算
        total_budget = settings.max_context_tokens
        reserve = int(total_budget * settings.system_reserve_ratio)
        buffer = int(total_budget * settings.context_buffer_ratio)
        available = total_budget - reserve - buffer

        # 累加
        context_snippets = []
        sources = []
        token_count = 0

        for i, chunk in enumerate(sorted_chunks):
            # 构造带引用标记的片段
            snippet = f"[{i + 1}] Source: {chunk['metadata'].get('source', 'unknown')}\n{chunk['text']}"
            snippet_tokens = count_tokens(snippet)

            if token_count + snippet_tokens > available:
                logger.debug("Context budget reached: {} tokens, skipped {} chunks",
                             token_count, len(sorted_chunks) - i)
                break

            context_snippets.append(snippet)
            sources.append({
                "chunk_id": chunk["chunk_id"],
                "text": chunk["text"][:200],
                "source": chunk["metadata"].get("source", ""),
                "score": chunk.get("rerank_score", chunk.get("rrf_score", 0)),
                "ref": i + 1,
            })
            token_count += snippet_tokens

        context = "\n\n".join(context_snippets)
        logger.info("Context built: {} chunks, ~{} tokens / {} available",
                     len(sources), token_count, available)

        return {"context": context, "sources": sources, "token_count": token_count}

    def post(self, shared: dict, prep_res, exec_res: dict) -> str:
        shared["context"] = exec_res["context"]
        shared["sources"] = exec_res["sources"]
        return "default"


# ================================================================
# P2-8: GeneratorNode
# ================================================================

class GeneratorNode(Node):
    """
    调用 LLM 生成最终答案，带引用标注。
    """

    def prep(self, shared: dict):
        query = shared.get("query", "")
        context = shared.get("context", "")
        return query, context

    def exec(self, inputs: tuple) -> str:
        query, context = inputs

        logger.info("✍️ Generating answer for: {}", query[:80])

        messages = prompt_manager.render_chat_messages(
            "answer_generation",
            query=query,
            context=context,
        )

        from src.infra.fallback import chat_with_fallback

        answer = chat_with_fallback(
            messages,
            temperature=0.3,
        )
        return answer

    def post(self, shared: dict, prep_res, exec_res: str) -> str:
        shared["answer"] = exec_res
        logger.info("✅ Answer generated: {} chars", len(exec_res))
        return "default"
