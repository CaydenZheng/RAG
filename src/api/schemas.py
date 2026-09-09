"""Request and response models for the HTTP interface."""

from pydantic import BaseModel, Field


class QueryRequest(BaseModel):
    query: str = Field(..., description="用户查询")
    session_id: str = Field(default="", description="会话 ID，空则不保存历史（一问一答）")
    top_k: int = Field(default=5, description="最终返回的文档数量")
    filter: dict | None = Field(default=None, description="元数据过滤条件，如 {'category':'design_pattern'}")


class QueryResponse(BaseModel):
    query_id: str
    answer: str
    sources: list[dict]
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
