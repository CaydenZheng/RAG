"""Remove host application configuration before importing project settings."""

import os

import pytest

# Keep in step with Settings; the regression checks every declared field.
APPLICATION_ENV_NAMES: frozenset[str] = frozenset({
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "LLM_MODEL", "LOCAL_EMBEDDING_MODEL",
    "HF_ENDPOINT", "RERANK_MODEL", "OLLAMA_BASE_URL", "CHROMA_PERSIST_DIR",
    "CACHE_DB_PATH", "RRF_K", "VECTOR_TOP_K", "BM25_TOP_K", "RERANK_TOP_K",
    "MAX_CONTEXT_TOKENS", "SYSTEM_RESERVE_RATIO", "CONTEXT_BUFFER_RATIO",
    "PROMPT_VERSION", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST",
    "AGENT_MAX_ITERATIONS", "AGENT_PLANNER_TEMPERATURE", "AGENT_MAX_TOOL_RESULT_LENGTH",
    "AGENT_VERBOSE", "LOG_LEVEL", "MAX_UPLOAD_BYTES",
})


def clear_host_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Preserve system variables while clearing app settings and provider overrides."""
    for name in list(os.environ):
        normalized: str = name.upper()
        if (
            normalized in APPLICATION_ENV_NAMES
            or "PROXY" in normalized
            or normalized.startswith(("OPENAI_", "LLM_", "LANGFUSE_", "OLLAMA_", "HF_"))
        ):
            monkeypatch.delenv(name)
