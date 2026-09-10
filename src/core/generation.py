"""
P2: 生成层

ContextBuilderNode  → 在同一预算内选择历史与本次证据
GeneratorNode       → 复用统一消息并约束答案引用
"""

from typing import List

from loguru import logger
from pocketflow import AsyncNode, Node

from config.settings import settings
from src.infra.prompt_manager import prompt_manager
from src.infra.session_store import session_store
from src.utils.token_counter import count_tokens

_POSITION_KEYS = (
    "chunk_index",
    "page",
    "section",
    "start_char",
    "end_char",
)


def build_answer_messages(
    query: str,
    context: str,
    history: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Build the same prompt messages for normal and streaming answers."""
    messages = prompt_manager.render_chat_messages(
        "answer_generation",
        query=query,
        context=context,
    )
    if history:
        messages[1:1] = history
    return messages


class ContextBuilderNode(Node):
    """
    Select conversation history and retrieved evidence under one input budget.

    The configured reserve covers the system prompt, question and output. History
    receives a capped share; evidence uses the remaining input budget. A chunk is
    either included completely or omitted.
    """

    HISTORY_TURNS = 6
    HISTORY_BUDGET_RATIO = 0.40

    def prep(self, shared: dict) -> tuple[list[dict], str]:
        return (
            shared.get("retrieved_chunks", []),
            shared.get("session_id", ""),
        )

    def exec(self, inputs: tuple[list[dict], str]) -> dict:
        chunks, session_id = inputs
        history = self._load_history(session_id)

        total_budget = settings.max_context_tokens
        reserve = int(total_budget * settings.system_reserve_ratio)
        buffer = int(total_budget * settings.context_buffer_ratio)
        input_budget = max(total_budget - reserve - buffer, 0)

        history_limit = min(
            int(total_budget * self.HISTORY_BUDGET_RATIO),
            input_budget,
        )
        selected_history, history_tokens = self._select_history(
            history,
            history_limit,
        )
        evidence_budget = max(input_budget - history_tokens, 0)
        context, sources, evidence_tokens = self._build_evidence(
            chunks,
            evidence_budget,
        )

        logger.info(
            "Answer input built: {} history + {} evidence tokens / {} available",
            history_tokens,
            evidence_tokens,
            input_budget,
        )
        return {
            "context": context,
            "sources": sources,
            "history": selected_history,
            "context_budget": {
                "total": total_budget,
                "available": input_budget,
                "history_tokens": history_tokens,
                "evidence_tokens": evidence_tokens,
            },
        }

    def _load_history(self, session_id: str) -> list[dict[str, str]]:
        if not session_id:
            return []
        history = session_store.get_recent_history(
            session_id,
            limit=self.HISTORY_TURNS,
        )
        if history:
            logger.info(
                "📜 Loaded {} history turns for session {}",
                len(history),
                session_id,
            )
        return history

    @staticmethod
    def _select_history(
        history: list[dict[str, str]],
        token_budget: int,
    ) -> tuple[list[dict[str, str]], int]:
        selected: list[dict[str, str]] = []
        token_count = 0
        for turn in reversed(history):
            content = turn.get("content", "")
            tokens = count_tokens(content)
            if token_count + tokens > token_budget:
                break
            role = turn.get("role", "user")
            if role not in ("user", "assistant"):
                role = "user"
            selected.insert(0, {"role": role, "content": content})
            token_count += tokens
        return selected, token_count

    @staticmethod
    def _source_from_chunk(chunk: dict, ref: int) -> dict:
        metadata = chunk.get("metadata") or {}
        position = {}
        for key in _POSITION_KEYS:
            value = metadata.get(key, chunk.get(key))
            if value is not None:
                position[key] = value
        chunk_index = metadata.get("chunk_index", chunk.get("chunk_index"))
        return {
            "ref": ref,
            "chunk_id": chunk["chunk_id"],
            "document_id": metadata.get("doc_id", chunk.get("doc_id", "")),
            "source": metadata.get("source", chunk.get("source", "")),
            "version": metadata.get(
                "version",
                metadata.get("source_version", chunk.get("version", "")),
            ),
            "chunk_index": chunk_index,
            "position": position,
            "text": chunk.get("text", "")[:200],
            "score": chunk.get(
                "rerank_score",
                chunk.get("rrf_score", 0),
            ),
        }

    @classmethod
    def _build_evidence(
        cls,
        chunks: List[dict],
        token_budget: int,
    ) -> tuple[str, list[dict], int]:
        sorted_chunks = sorted(
            chunks,
            key=lambda chunk: chunk.get(
                "rerank_score",
                chunk.get("rrf_score", 0),
            ),
            reverse=True,
        )
        snippets: list[str] = []
        sources: list[dict] = []
        token_count = 0

        for chunk in sorted_chunks:
            ref = len(sources) + 1
            source = cls._source_from_chunk(chunk, ref)
            location = ", ".join(
                f"{key}={value}"
                for key, value in source["position"].items()
            )
            header_parts = [
                f"[{ref}] Source: {source['source'] or 'unknown'}",
                f"Chunk: {source['chunk_id']}",
            ]
            if source["version"]:
                header_parts.append(f"Version: {source['version']}")
            if location:
                header_parts.append(f"Position: {location}")
            snippet = "; ".join(header_parts) + "\n" + chunk.get("text", "")
            snippet_tokens = count_tokens(snippet)
            if token_count + snippet_tokens > token_budget:
                break

            snippets.append(snippet)
            sources.append(source)
            token_count += snippet_tokens

        context = (
            "\n\n".join(snippets)
            if snippets
            else "No relevant documents found."
        )
        return context, sources, token_count

    def post(self, shared: dict, prep_res, exec_res: dict) -> str:
        shared.update(exec_res)
        return "default"


class GeneratorNode(AsyncNode):
    """Generate and persist an answer using the prepared answer inputs."""

    async def prep_async(self, shared: dict) -> tuple:
        return (
            shared.get("query", ""),
            shared.get("context", ""),
            shared.get("history", []),
            shared.get("session_id", ""),
        )

    async def exec_async(self, inputs: tuple) -> tuple[str, str]:
        query, context, history, session_id = inputs
        logger.info("✍️ Generating answer for: {}", query[:80])
        messages = build_answer_messages(query, context, history)

        from src.infra.fallback import chat_with_fallback_async

        answer = await chat_with_fallback_async(
            messages,
            temperature=0.3,
        )
        return answer, session_id

    async def post_async(self, shared: dict, prep_res, exec_res) -> str:
        answer, session_id = exec_res
        shared["answer"] = answer

        if session_id:
            query = shared.get("query", "")
            session_store.append_exchange(session_id, query, answer)
            logger.info(
                "💾 Session {} saved: {} turns total",
                session_id,
                session_store.history_count(session_id),
            )

        logger.info("✅ Answer generated: {} chars", len(answer))
        return "default"
