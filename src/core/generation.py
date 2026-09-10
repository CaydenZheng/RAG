"""
P2: 生成层

ContextBuilderNode  → 在同一预算内选择历史与本次证据
GeneratorNode       → 复用统一消息并约束答案引用
"""

import asyncio
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import List

from loguru import logger
from pocketflow import AsyncNode, Node

from config.settings import settings
from src.core.index_versions import LEGACY_INDEX_VERSION
from src.infra.prompt_manager import prompt_manager
from src.infra.session_store import session_store
from src.infra.tracer import tracer
from src.llm import llm_client
from src.utils.token_counter import count_tokens

_CITATION_PATTERN = re.compile(r"\[((?:\d+\s*,\s*)*\d+)\]")
_PARTIAL_CITATION_PATTERN = re.compile(
    r"\[(?:\d+\s*(?:,\s*\d*\s*)*)?"
)
_POSITION_KEYS = (
    "chunk_index",
    "page",
    "section",
    "start_char",
    "end_char",
)


def sanitize_answer_citations(answer: str, valid_refs: set[int]) -> str:
    """Remove reference numbers that are absent from this request's evidence."""

    def replace(match: re.Match[str]) -> str:
        refs = [int(value.strip()) for value in match.group(1).split(",")]
        kept = list(dict.fromkeys(ref for ref in refs if ref in valid_refs))
        if not kept:
            return ""
        return "[" + ", ".join(str(ref) for ref in kept) + "]"

    return _CITATION_PATTERN.sub(replace, answer)


class CitationStreamGuard:
    """Validate numeric citations even when SSE chunks split a marker."""

    MAX_PENDING_CHARS = 64

    def __init__(self, valid_refs: set[int]) -> None:
        self._valid_refs = valid_refs
        self._pending = ""

    def feed(self, chunk: str) -> str:
        self._pending += chunk
        output: list[str] = []

        while self._pending:
            start = self._pending.find("[")
            if start < 0:
                output.append(self._pending)
                self._pending = ""
                break

            output.append(self._pending[:start])
            candidate = self._pending[start:]
            closing = candidate.find("]")
            if closing >= 0:
                marker = candidate[: closing + 1]
                if _CITATION_PATTERN.fullmatch(marker):
                    output.append(
                        sanitize_answer_citations(marker, self._valid_refs)
                    )
                    self._pending = candidate[closing + 1 :]
                    continue
                output.append("[")
                self._pending = candidate[1:]
                continue

            if (
                len(candidate) <= self.MAX_PENDING_CHARS
                and _PARTIAL_CITATION_PATTERN.fullmatch(candidate)
            ):
                self._pending = candidate
                break

            output.append("[")
            self._pending = candidate[1:]

        return "".join(output)

    def finish(self) -> str:
        pending = self._pending
        self._pending = ""
        return pending


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


@dataclass(frozen=True)
class AnswerInput:
    """Transport-independent inputs for one answer generation request."""

    query: str
    context: str
    history: list[dict[str, str]]
    session_id: str
    valid_citation_refs: frozenset[int]
    index_version: str = LEGACY_INDEX_VERSION

    @classmethod
    def from_shared(cls, shared: dict) -> "AnswerInput":
        return cls(
            query=shared.get("query", ""),
            context=shared.get("context", ""),
            history=list(shared.get("history", [])),
            session_id=shared.get("session_id", ""),
            valid_citation_refs=frozenset(
                shared.get("valid_citation_refs", set())
            ),
            index_version=shared.get("index_version", LEGACY_INDEX_VERSION),
        )


class AnswerService:
    """Run normal or streaming generation with one input and finalization contract."""

    @staticmethod
    def _messages(answer_input: AnswerInput) -> list[dict[str, str]]:
        return build_answer_messages(
            answer_input.query,
            answer_input.context,
            answer_input.history,
        )

    @staticmethod
    def _config() -> dict:
        return prompt_manager.get_prompt_config("answer_generation")

    async def generate(self, answer_input: AnswerInput) -> str:
        from src.infra.fallback import chat_with_fallback_async

        config = self._config()
        with tracer.stage("answer_generation"):
            answer = await chat_with_fallback_async(
                self._messages(answer_input),
                model=config["model"],
                temperature=config["temperature"],
                max_tokens=config["max_tokens"],
                index_version=answer_input.index_version,
            )
        return sanitize_answer_citations(
            answer,
            set(answer_input.valid_citation_refs),
        )

    async def stream(self, answer_input: AnswerInput) -> AsyncIterator[str]:
        config = self._config()
        citation_guard = CitationStreamGuard(
            set(answer_input.valid_citation_refs)
        )
        with tracer.stage("answer_generation"):
            provider_stream = llm_client.chat_stream_async(
                self._messages(answer_input),
                model=config["model"],
                temperature=config["temperature"],
                max_tokens=config["max_tokens"],
            )
        try:
            with tracer.stage("answer_generation"):
                async for chunk in provider_stream:
                    safe_chunk = citation_guard.feed(chunk)
                    if safe_chunk:
                        yield safe_chunk
        except asyncio.CancelledError:
            logger.info("Answer generation cancelled")
            raise
        finally:
            close = getattr(provider_stream, "aclose", None)
            if close is not None:
                await close()

        tail = citation_guard.finish()
        if tail:
            yield tail

    @staticmethod
    def persist(answer_input: AnswerInput, answer: str) -> None:
        if not answer_input.session_id:
            return
        session_store.append_exchange(
            answer_input.session_id,
            answer_input.query,
            answer,
        )
        logger.info(
            "Session exchange saved: {} turns total",
            session_store.history_count(answer_input.session_id),
        )


answer_service = AnswerService()


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
        started_at = time.perf_counter()
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

        tracer.add_span(
            None,
            "context_build",
            (time.perf_counter() - started_at) * 1000,
            history_tokens=history_tokens,
            evidence_tokens=evidence_tokens,
            sources=len(sources),
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
            "valid_citation_refs": {source["ref"] for source in sources},
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
                "Loaded {} history turns",
                len(history),
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
    """Generate and persist an answer through the shared answer service."""

    async def prep_async(self, shared: dict) -> AnswerInput:
        return AnswerInput.from_shared(shared)

    async def exec_async(self, answer_input: AnswerInput) -> str:
        logger.info("Generating answer: query_chars={}", len(answer_input.query))
        return await answer_service.generate(answer_input)

    async def post_async(
        self,
        shared: dict,
        prep_res: AnswerInput,
        exec_res: str,
    ) -> str:
        shared["answer"] = exec_res
        answer_service.persist(prep_res, exec_res)
        logger.info("✅ Answer generated: {} chars", len(exec_res))
        return "default"
