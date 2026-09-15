"""
Pydantic Settings — 读取 .env 的所有配置项，提供类型校验与默认值。

用法:
    from config.settings import settings
    print(settings.llm_model)  # deepseek-chat
"""

import math
from pathlib import Path
from typing import Optional, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def project_path(path: str | Path) -> Path:
    """Resolve repository-owned relative paths without rewriting absolutes."""
    candidate = Path(path).expanduser()
    if candidate.is_absolute():
        return candidate
    return (PROJECT_ROOT / candidate).resolve()


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
    rerank_model: str = Field(default="BAAI/bge-reranker-base", alias="RERANK_MODEL")
    rerank_timeout_seconds: float = Field(
        default=5.0, gt=0, alias="RERANK_TIMEOUT_SECONDS"
    )
    startup_preload_reranker: bool = Field(
        default=True, alias="STARTUP_PRELOAD_RERANKER"
    )

    # ================================================================
    # 请求可靠性
    # ================================================================
    max_concurrent_queries: int = Field(default=8, ge=1, alias="MAX_CONCURRENT_QUERIES")
    request_timeout_seconds: float = Field(
        default=90.0, gt=0, alias="REQUEST_TIMEOUT_SECONDS"
    )
    llm_max_retries: int = Field(default=1, ge=0, le=3, alias="LLM_MAX_RETRIES")

    # ================================================================
    # 管理接口认证
    # ================================================================
    admin_api_key: SecretStr | None = Field(default=None, alias="ADMIN_API_KEY")
    allow_unauthenticated_admin: bool = Field(
        default=False, alias="ALLOW_UNAUTHENTICATED_ADMIN"
    )

    # ================================================================
    # 多 Provider 降级
    # ================================================================
    ollama_base_url: Optional[str] = Field(default=None, alias="OLLAMA_BASE_URL")

    # ================================================================
    # 向量存储
    # ================================================================
    chroma_persist_dir: str = Field(default="./data/chroma", alias="CHROMA_PERSIST_DIR")

    # ================================================================
    # 精确缓存
    # ================================================================
    cache_db_path: str = Field(default="./data/cache.db", alias="CACHE_DB_PATH")
    cache_max_entries: int = Field(
        default=10000, ge=1, alias="CACHE_MAX_ENTRIES"
    )

    # ================================================================
    # 检索参数
    # ================================================================
    rrf_k: int = Field(default=60, ge=1, alias="RRF_K")
    vector_top_k: int = Field(default=20, ge=1, alias="VECTOR_TOP_K")
    bm25_top_k: int = Field(default=20, ge=1, alias="BM25_TOP_K")
    rerank_top_k: int = Field(default=10, ge=1, alias="RERANK_TOP_K")
    abstention_thresholds: dict[str, float] = Field(
        default_factory=dict, alias="ABSTENTION_THRESHOLDS"
    )
    abstention_calibration_id: str = Field(
        default="", alias="ABSTENTION_CALIBRATION_ID"
    )
    abstention_calibration_models: dict[str, str] = Field(
        default_factory=dict, alias="ABSTENTION_CALIBRATION_MODELS"
    )

    # ================================================================
    # Token 预算
    # ================================================================
    max_context_tokens: int = Field(default=4096, ge=256, alias="MAX_CONTEXT_TOKENS")
    system_reserve_ratio: float = Field(
        default=0.30, ge=0, lt=1, alias="SYSTEM_RESERVE_RATIO"
    )
    context_buffer_ratio: float = Field(
        default=0.05, ge=0, lt=1, alias="CONTEXT_BUFFER_RATIO"
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
        default=5, ge=1, le=20, alias="AGENT_MAX_ITERATIONS"
    )
    agent_max_tool_calls: int = Field(
        default=5, ge=0, le=20, alias="AGENT_MAX_TOOL_CALLS"
    )
    agent_max_token_budget: int = Field(
        default=12000, ge=256, le=131072, alias="AGENT_MAX_TOKEN_BUDGET"
    )
    agent_timeout_seconds: float = Field(
        default=60.0, gt=0, le=300, alias="AGENT_TIMEOUT_SECONDS"
    )
    agent_planner_temperature: float = Field(
        default=0.1, ge=0, le=2, alias="AGENT_PLANNER_TEMPERATURE"
    )
    agent_planner_max_tokens: int = Field(
        default=512, ge=64, le=4096, alias="AGENT_PLANNER_MAX_TOKENS"
    )
    agent_final_max_tokens: int = Field(
        default=1024, ge=64, le=8192, alias="AGENT_FINAL_MAX_TOKENS"
    )
    agent_max_tool_result_length: int = Field(
        default=1000, ge=128, le=20000, alias="AGENT_MAX_TOOL_RESULT_LENGTH"
    )
    agent_verbose: bool = Field(default=True, alias="AGENT_VERBOSE")
    tool_dedup_max_sessions: int = Field(
        default=10000, ge=1, alias="TOOL_DEDUP_MAX_SESSIONS"
    )

    # ================================================================
    # 日志
    # ================================================================
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    agent_log_max_bytes: int = Field(
        default=10 * 1024 * 1024, ge=1, alias="AGENT_LOG_MAX_BYTES"
    )
    agent_log_backup_count: int = Field(
        default=5, ge=0, alias="AGENT_LOG_BACKUP_COUNT"
    )
    agent_log_retention_seconds: int = Field(
        default=7 * 24 * 60 * 60, ge=1, alias="AGENT_LOG_RETENTION_SECONDS"
    )

    # ================================================================
    # 上传限制
    # ================================================================
    max_upload_bytes: int = Field(
        default=20 * 1024 * 1024, gt=0, alias="MAX_UPLOAD_BYTES"
    )

    @field_validator(
        "openai_api_key",
        "openai_base_url",
        "llm_model",
        "local_embedding_model",
        "rerank_model",
        "chroma_persist_dir",
        "cache_db_path",
        "prompt_version",
    )
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        """Reject values that pass type validation but contain no content."""
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("log_level")
    @classmethod
    def normalize_log_level(cls, value: str) -> str:
        """Normalize and validate Loguru's supported severity names."""
        normalized: str = value.strip().upper()
        allowed: frozenset[str] = frozenset(
            {"TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"}
        )
        if normalized not in allowed:
            raise ValueError(f"LOG_LEVEL must be one of {sorted(allowed)}")
        return normalized

    @model_validator(mode="after")
    def validate_admin_auth(self) -> Self:
        """Require an admin secret unless local unauthenticated mode is explicit."""
        if self.allow_unauthenticated_admin:
            return self
        if (
            self.admin_api_key is None
            or not self.admin_api_key.get_secret_value().strip()
        ):
            raise ValueError(
                "ADMIN_API_KEY must be set unless ALLOW_UNAUTHENTICATED_ADMIN=true"
            )
        return self

    @model_validator(mode="after")
    def validate_abstention_calibration(self) -> Self:
        """Only accept finite thresholds tied to a reproducible calibration."""
        supported_modes = {
            "vector_only",
            "bm25_only",
            "hybrid",
            "hybrid+rerank",
        }
        unknown_modes = set(self.abstention_thresholds) - supported_modes
        if unknown_modes:
            raise ValueError(
                f"ABSTENTION_THRESHOLDS contains unknown modes: {sorted(unknown_modes)}"
            )
        if any(
            not math.isfinite(threshold)
            for threshold in self.abstention_thresholds.values()
        ):
            raise ValueError("ABSTENTION_THRESHOLDS must contain finite numbers")
        if self.abstention_thresholds and not self.abstention_calibration_id.strip():
            raise ValueError(
                "ABSTENTION_CALIBRATION_ID is required when thresholds are configured"
            )
        if self.abstention_calibration_id and not self.abstention_thresholds:
            raise ValueError(
                "ABSTENTION_THRESHOLDS are required when a calibration ID is configured"
            )
        if self.abstention_calibration_models and not self.abstention_thresholds:
            raise ValueError(
                "ABSTENTION_THRESHOLDS are required when calibration models are configured"
            )
        if self.abstention_thresholds:
            expected_models = {"embedding": self.local_embedding_model}
            if "hybrid+rerank" in self.abstention_thresholds:
                expected_models["reranker"] = self.rerank_model
            mismatches = {
                name: {
                    "expected": expected,
                    "configured": self.abstention_calibration_models.get(name),
                }
                for name, expected in expected_models.items()
                if self.abstention_calibration_models.get(name) != expected
            }
            if mismatches:
                raise ValueError(
                    "ABSTENTION_CALIBRATION_MODELS do not match runtime models: "
                    f"{mismatches}"
                )
        return self

    @model_validator(mode="after")
    def validate_context_reserve(self) -> Self:
        """Keep a positive share of the context window for retrieved evidence."""
        reserve_ratio: float = self.system_reserve_ratio + self.context_buffer_ratio
        if reserve_ratio >= 1:
            raise ValueError("context reserve ratios must sum to less than 1")
        return self

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
        return project_path("prompts") / self.prompt_version

    @property
    def data_dir(self) -> Path:
        """数据根目录"""
        return project_path("data")

    @property
    def log_dir(self) -> Path:
        """运行日志目录"""
        return project_path("logs")

    @property
    def memory_dir(self) -> Path:
        """Agent legacy 记忆目录"""
        return project_path("memory")

    @property
    def raw_dir(self) -> Path:
        """原始文档目录"""
        return self.data_dir / "raw"

    @property
    def chroma_path(self) -> Path:
        """ChromaDB 持久化路径（绝对路径）"""
        return project_path(self.chroma_persist_dir)

    @property
    def cache_db_path_resolved(self) -> Path:
        """缓存数据库绝对路径"""
        return project_path(self.cache_db_path)

    # ================================================================
    # pydantic-settings 配置
    # ================================================================
    model_config = dict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # 忽略 .env 中未定义的变量
        populate_by_name=True,  # 允许用字段名或 alias 访问
    )


# 全局单例
settings = Settings()
