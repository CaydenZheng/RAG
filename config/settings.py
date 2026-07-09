"""
Pydantic Settings — 读取 .env 的所有配置项，提供类型校验与默认值。

用法:
    from config.settings import settings
    print(settings.llm_model)  # deepseek-chat
"""

from pydantic_settings import BaseSettings
from pydantic import Field
from pathlib import Path
from typing import Optional


class Settings(BaseSettings):
    # ================================================================
    # LLM 生成 (DeepSeek)
    # ================================================================
    openai_api_key: str = Field(alias="OPENAI_API_KEY")
    openai_base_url: str = Field(
        default="https://api.deepseek.com", alias="OPENAI_BASE_URL"
    )
    llm_model: str = Field(default="deepseek-chat", alias="LLM_MODEL")

    # ================================================================
    # Embedding (本地模型)
    # ================================================================
    local_embedding_model: str = Field(
        default="BAAI/bge-base-en-v1.5", alias="LOCAL_EMBEDDING_MODEL"
    )

    # ================================================================
    # HuggingFace 镜像 (国内加速)
    # ================================================================
    hf_endpoint: Optional[str] = Field(default=None, alias="HF_ENDPOINT")

    # ================================================================
    # Rerank (本地模型)
    # ================================================================
    rerank_model: str = Field(
        default="BAAI/bge-reranker-base", alias="RERANK_MODEL"
    )

    # ================================================================
    # 多 Provider 降级
    # ================================================================
    ollama_base_url: Optional[str] = Field(
        default=None, alias="OLLAMA_BASE_URL"
    )

    # ================================================================
    # 向量存储
    # ================================================================
    chroma_persist_dir: str = Field(
        default="./data/chroma", alias="CHROMA_PERSIST_DIR"
    )

    # ================================================================
    # 精确缓存
    # ================================================================
    cache_db_path: str = Field(
        default="./data/cache.db", alias="CACHE_DB_PATH"
    )

    # ================================================================
    # 检索参数
    # ================================================================
    rrf_k: int = Field(default=60, alias="RRF_K")
    vector_top_k: int = Field(default=20, alias="VECTOR_TOP_K")
    bm25_top_k: int = Field(default=20, alias="BM25_TOP_K")
    rerank_top_k: int = Field(default=10, alias="RERANK_TOP_K")

    # ================================================================
    # Token 预算
    # ================================================================
    max_context_tokens: int = Field(default=4096, alias="MAX_CONTEXT_TOKENS")
    system_reserve_ratio: float = Field(
        default=0.30, alias="SYSTEM_RESERVE_RATIO"
    )
    context_buffer_ratio: float = Field(
        default=0.05, alias="CONTEXT_BUFFER_RATIO"
    )

    # ================================================================
    # Prompt 版本管理
    # ================================================================
    prompt_version: str = Field(default="v1", alias="PROMPT_VERSION")

    # ================================================================
    # 追踪监控 (Langfuse, 可选)
    # ================================================================
    langfuse_public_key: Optional[str] = Field(
        default=None, alias="LANGFUSE_PUBLIC_KEY"
    )
    langfuse_secret_key: Optional[str] = Field(
        default=None, alias="LANGFUSE_SECRET_KEY"
    )
    langfuse_host: Optional[str] = Field(
        default="https://cloud.langfuse.com", alias="LANGFUSE_HOST"
    )

    # ================================================================
    # Agent 配置
    # ================================================================
    agent_max_iterations: int = Field(
        default=5, alias="AGENT_MAX_ITERATIONS"
    )
    agent_planner_temperature: float = Field(
        default=0.1, alias="AGENT_PLANNER_TEMPERATURE"
    )
    agent_max_tool_result_length: int = Field(
        default=1000, alias="AGENT_MAX_TOOL_RESULT_LENGTH"
    )
    agent_verbose: bool = Field(
        default=True, alias="AGENT_VERBOSE"
    )

    # ================================================================
    # 日志
    # ================================================================
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # ================================================================
    # 派生属性
    # ================================================================
    @property
    def langfuse_enabled(self) -> bool:
        """Langfuse 是否可用（公钥+私钥均配置时启用）"""
        return bool(self.langfuse_public_key and self.langfuse_secret_key)

    @property
    def prompt_dir(self) -> Path:
        """Prompt 模板目录"""
        return Path("prompts") / self.prompt_version

    @property
    def data_dir(self) -> Path:
        """数据根目录"""
        return Path("data")

    @property
    def raw_dir(self) -> Path:
        """原始文档目录"""
        return self.data_dir / "raw"

    @property
    def chroma_path(self) -> Path:
        """ChromaDB 持久化路径（绝对路径）"""
        return Path(self.chroma_persist_dir).resolve()

    @property
    def cache_db_path_resolved(self) -> Path:
        """缓存数据库绝对路径"""
        return Path(self.cache_db_path).resolve()

    # ================================================================
    # pydantic-settings 配置
    # ================================================================
    model_config = dict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",       # 忽略 .env 中未定义的变量
        populate_by_name=True, # 允许用字段名或 alias 访问
    )


# 全局单例
settings = Settings()
