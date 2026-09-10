"""Request and response models for the HTTP interface."""

import json

from pydantic import BaseModel, Field, field_validator

from src.core.index_versions import LEGACY_INDEX_VERSION
from src.core.knowledge import (
    DEFAULT_RETRIEVAL_MODE,
    DEFAULT_RETRIEVAL_TOP_K,
    MAX_RETRIEVAL_TOP_K,
    RetrievalMode,
    validate_metadata_filter,
)


class IndexJobResponse(BaseModel):
    job_id: str
    operation: str
    state: str
    submitted_at: str
    started_at: str | None = None
    finished_at: str | None = None
    index_version: str | None = None
    error_code: str | None = None


class QueryRequest(BaseModel):
    query: str = Field(..., description="用户查询")
    session_id: str = Field(
        default="",
        description="会话 ID，空则不保存历史（一问一答）",
    )
    top_k: int = Field(
        default=DEFAULT_RETRIEVAL_TOP_K,
        ge=1,
        le=MAX_RETRIEVAL_TOP_K,
        strict=True,
        description="最终返回的文档数量",
    )
    filter: dict | None = Field(
        default=None,
        description="Chroma metadata where 条件",
    )
    retrieval_mode: RetrievalMode = Field(
        default=DEFAULT_RETRIEVAL_MODE,
        description="vector_only、bm25_only、hybrid 或 hybrid+rerank",
    )

    @field_validator("filter")
    @classmethod
    def validate_filter(cls, value: dict | None) -> dict | None:
        return validate_metadata_filter(value)


def parse_metadata_filter_json(value: str | None) -> dict | None:
    """Parse the GET endpoint's JSON-encoded metadata filter."""
    if value is None:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("filter must be valid JSON") from exc
    return validate_metadata_filter(parsed)


class QueryResponse(BaseModel):
    query_id: str
    answer: str
    sources: list[dict]
    warnings: list[str] = Field(default_factory=list)
    index_version: str = LEGACY_INDEX_VERSION
    latency_ms: float


class AgentChatRequest(BaseModel):
    session_id: str = Field(default="", description="会话 ID，空则自动生成")
    message: str = Field(..., description="用户消息")


class AgentChatResponse(BaseModel):
    session_id: str
    answer: str
    tool_calls: list[dict] = []
    iterations: int = 0
    latency_ms: float = 0.0
    error: str = ""
