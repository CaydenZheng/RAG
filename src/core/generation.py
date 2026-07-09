"""
P2: 生成层

ContextBuilderNode  → 按分数降序排列 chunk，逐 chunk 累加 token，触达预算停止
GeneratorNode       → 调用 LLM 生成带引用的答案
"""

from typing import List

from pocketflow import Node, AsyncNode
from loguru import logger

from config.settings import settings
from src.llm import llm_client
from src.infra.prompt_manager import prompt_manager
from src.infra.session_store import session_store
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

class GeneratorNode(AsyncNode):
    """
    调用 LLM 生成最终答案，带引用标注 + 多轮对话支持。

    当 shared 中有 session_id 时：
      - 自动加载最近 N 轮对话历史
      - 将历史注入 LLM 消息列表（保持对话连贯性）
      - 生成后保存本轮问答到会话存储
    """

    HISTORY_TURNS = 6  # 最多注入 6 条历史消息（3 轮对话）

    async def prep_async(self, shared: dict):
        query = shared.get("query", "")
        context = shared.get("context", "")
        session_id = shared.get("session_id", "")

        # 加载会话历史
        history = []
        if session_id:
            history = session_store.get_recent_history(session_id, limit=self.HISTORY_TURNS)
            if history:
                logger.info("📜 Loaded {} history turns for session {}", len(history), session_id)

        return query, context, history, session_id

    async def exec_async(self, inputs: tuple) -> str:
        query, context, history, session_id = inputs

        logger.info("✍️ Generating answer for: {}", query[:80])

        # 构建基础 messages（system + user 含 context + query）
        messages = prompt_manager.render_chat_messages(
            "answer_generation",
            query=query,
            context=context,
        )

        # 将会话历史插入到 system 和当前 user 之间（带 token 预算保护）
        if history:
            from src.utils.token_counter import count_tokens
            max_history_tokens = int(settings.max_context_tokens * 0.40)
            history_msgs = []
            token_sum = 0
            for turn in reversed(history):
                role = turn["role"] if turn["role"] in ("user", "assistant") else "user"
                t = count_tokens(turn["content"])
                if token_sum + t > max_history_tokens:
                    break
                history_msgs.insert(0, {"role": role, "content": turn["content"]})
                token_sum += t
            messages[1:1] = history_msgs

        from src.infra.fallback import chat_with_fallback_async

        answer = await chat_with_fallback_async(
            messages,
            temperature=0.3,
        )
        return answer, session_id

    async def post_async(self, shared: dict, prep_res, exec_res) -> str:
        answer, session_id = exec_res
        shared["answer"] = answer

        # 保存本轮对话
        if session_id:
            query = shared.get("query", "")
            session_store.add_turn(session_id, "user", query)
            session_store.add_turn(session_id, "assistant", answer)
            logger.info("💾 Session {} saved: {} turns total", session_id,
                         session_store.history_count(session_id))

        logger.info("✅ Answer generated: {} chars", len(answer))
        return "default"
