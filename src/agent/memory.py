"""
结构化记忆管理。

MEMORY.md     — 长期记忆（用户偏好、重要事实），全量注入 system prompt
sessions.db   — RAG／Agent 共用的有序短期历史，达到阈值时 LLM 自动压缩归档

压缩策略（三级）：
  1. 工具调用结果截断：超过 500 token 只保留前 200 + 后 200
  2. 历史消息硬截断：保留最近 N 条完整消息
  3. 记忆摘要固化：LLM 对旧对话做摘要，替换原始消息
"""

import json
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

from src.infra.session_store import SessionStore, SessionTurn, session_store
from src.security.session_ids import (
    client_scope_from_scoped_session,
    validate_storage_session_id,
)

# ================================================================
# 数据模型
# ================================================================

@dataclass
class MemoryTurn:
    """一次对话轮次"""
    role: str         # "user" | "assistant" | "summary"
    content: str
    timestamp: float = field(default_factory=time.time)
    token_count: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class MemoryConfig:
    """记忆配置"""
    max_history_turns: int = 15          # 最多保留的对话轮次
    compress_trigger_turns: int = 10     # 触发压缩的轮次阈值
    compress_keep_recent: int = 5        # 压缩时保留最近 N 轮不压缩
    compress_target_turns: int = 3       # 压缩后目标保留的摘要轮次
    tool_result_max_tokens: int = 500    # 工具结果截断阈值
    tool_result_head_tokens: int = 200   # 保留头部
    tool_result_tail_tokens: int = 200   # 保留尾部


# ================================================================
# MemoryManager
# ================================================================

class MemoryManager:
    """
    两层记忆管理器。

    目录结构：
      memory/
      ├── long_term.md                 # 旧版单用户长期记忆
      ├── clients/{client_scope}/
      │   └── long_term.md             # HTTP 客户端长期记忆

    短期历史保存在共享 SQLite sessions 表中；旧 history.json 仅用于首次迁移。
    """

    def __init__(
        self,
        memory_dir: str = "memory",
        config: MemoryConfig = None,
        store: SessionStore | None = None,
    ):
        self.memory_dir = Path(memory_dir)
        self.config = config or MemoryConfig()
        self._store = store or SessionStore(str(self.memory_dir / "sessions.db"))

        # 确保目录存在
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        (self.memory_dir / "sessions").mkdir(parents=True, exist_ok=True)

        # 长期记忆文件路径
        self._long_term_path = self.memory_dir / "long_term.md"

        self._initialize_long_term_file(self._long_term_path)

    # ================================================================
    # 长期记忆
    # ================================================================

    @staticmethod
    def _initialize_long_term_file(path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(
                "# Long-term Memory\n\n"
                "<!-- This file stores persistent user preferences and important facts -->\n"
                "<!-- It is injected into the system prompt of every conversation -->\n\n",
                encoding="utf-8",
            )

    @staticmethod
    def _read_long_term(path: Path) -> str:
        content = path.read_text(encoding="utf-8")
        lines = [line for line in content.split("\n") if not line.strip().startswith("<!--")]
        return "\n".join(lines).strip()

    @property
    def long_term_memory(self) -> str:
        """读取旧版单用户长期记忆。"""
        return self._read_long_term(self._long_term_path)

    def long_term_memory_for_session(self, session_id: str) -> str:
        """读取与客户端身份绑定的长期记忆。"""
        session_id = validate_storage_session_id(session_id)
        client_scope = client_scope_from_scoped_session(session_id)
        if client_scope is None:
            return self.long_term_memory
        path = self.memory_dir / "clients" / client_scope / "long_term.md"
        self._initialize_long_term_file(path)
        return self._read_long_term(path)

    def update_long_term(self, content: str):
        """覆写长期记忆"""
        self._long_term_path.write_text(content, encoding="utf-8")

    def append_long_term(self, fact: str):
        """追加一条长期记忆（LLM 调用后自动记录）"""
        current = self._long_term_path.read_text(encoding="utf-8")
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        new_entry = f"\n- [{timestamp}] {fact}"
        self._long_term_path.write_text(current + new_entry, encoding="utf-8")

    # ================================================================
    # 会话历史
    # ================================================================

    def _session_dir(self, session_id: str, *, create: bool = False) -> Path:
        session_id = validate_storage_session_id(session_id)
        directory = self.memory_dir / "sessions" / session_id
        if create:
            directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _history_path(self, session_id: str, *, create_parent: bool = False) -> Path:
        return self._session_dir(session_id, create=create_parent) / "history.json"

    @staticmethod
    def _as_memory_turn(turn: SessionTurn) -> MemoryTurn:
        return MemoryTurn(
            role=turn.role,
            content=turn.content,
            timestamp=turn.timestamp,
            token_count=turn.token_count,
            metadata=turn.metadata,
        )

    @staticmethod
    def _as_session_turn(turn: MemoryTurn) -> SessionTurn:
        return SessionTurn(
            role=turn.role,
            content=turn.content,
            timestamp=turn.timestamp,
            token_count=turn.token_count,
            metadata=turn.metadata,
        )

    def _migrate_legacy_history(self, session_id: str) -> List[MemoryTurn]:
        path = self._history_path(session_id)
        if not path.exists():
            return []
        try:
            legacy_turns = [
                MemoryTurn(**item)
                for item in json.loads(path.read_text(encoding="utf-8"))
            ]
        except Exception as exc:
            logger.warning("Failed to load history: {}", type(exc).__name__)
            return []

        def import_if_empty(current: list[SessionTurn]) -> list[SessionTurn]:
            if current:
                return current
            return [self._as_session_turn(turn) for turn in legacy_turns]

        stored = self._store.update_history(session_id, import_if_empty)
        migrated_path = path.with_suffix(".json.migrated")
        if migrated_path.exists():
            path.unlink()
        else:
            path.replace(migrated_path)
        return [self._as_memory_turn(turn) for turn in stored]

    def load_history(self, session_id: str) -> List[MemoryTurn]:
        """Load ordered Agent history from the shared conversation store."""
        session_id = validate_storage_session_id(session_id)
        turns = self._store.get_turns(session_id)
        if turns:
            return [self._as_memory_turn(turn) for turn in turns]
        return self._migrate_legacy_history(session_id)

    def save_history(self, session_id: str, turns: List[MemoryTurn]) -> None:
        """Atomically replace Agent history after compression or migration."""
        session_id = validate_storage_session_id(session_id)
        self._store.replace_history(
            session_id, [self._as_session_turn(turn) for turn in turns]
        )

    def add_turn(
        self,
        session_id: str,
        role: str,
        content: str,
        token_count: int = 0,
        metadata: dict | None = None,
    ) -> None:
        """Append one message while preserving its original role."""
        self.add_turns(
            session_id,
            [
                MemoryTurn(
                    role=role,
                    content=content,
                    token_count=token_count,
                    metadata=metadata or {},
                )
            ],
        )

    def add_turns(
        self, session_id: str, new_turns: Iterable[MemoryTurn]
    ) -> None:
        """Append one complete Agent interaction as an atomic history update."""
        session_id = validate_storage_session_id(session_id)
        pending = list(new_turns)
        if not pending:
            return

        def append_and_compress(current: list[SessionTurn]) -> list[SessionTurn]:
            turns = [self._as_memory_turn(turn) for turn in current]
            turns.extend(pending)
            if len(turns) >= self.config.compress_trigger_turns:
                turns = self._compress_history(turns)
            return [self._as_session_turn(turn) for turn in turns]

        self._store.update_history(session_id, append_and_compress)

    def clear_session(self, session_id: str) -> bool:
        """Clear shared history atomically and report whether it existed."""
        session_id = validate_storage_session_id(session_id)
        existed = self._store.clear(session_id)
        path = self._history_path(session_id)
        if path.exists():
            path.unlink()
            existed = True
        logger.info("Agent session cleared")
        return existed

    # ================================================================
    # 构建 Agent 消息列表
    # ================================================================

    def build_messages(
        self,
        session_id: str,
        system_prompt: str,
        user_message: str,
        max_turns: int = None,
    ) -> List[Dict[str, str]]:
        """
        构建完整的 Agent 消息列表。

        格式：system prompt（含长期记忆）+ 历史对话 + 当前用户消息
        """
        max_turns = max_turns or self.config.max_history_turns
        turns = self.load_history(session_id)

        # System prompt：基础 prompt + 长期记忆
        full_system = system_prompt
        long_mem = self.long_term_memory_for_session(session_id)
        if long_mem and "<!--" not in long_mem[:50]:  # 有实际内容
            full_system += f"\n\n## User Profile (Long-term Memory)\n{long_mem}"

        messages = [{"role": "system", "content": full_system}]

        # 历史对话（最近 N 轮），非标准 role 映射为 user
        recent_turns = turns[-max_turns:] if len(turns) > max_turns else turns
        for turn in recent_turns:
            role = turn.role if turn.role in ("user", "assistant", "system") else "user"
            messages.append({"role": role, "content": turn.content})

        # 当前用户消息
        messages.append({"role": "user", "content": user_message})

        return messages

    # ================================================================
    # 压缩逻辑（三级）
    # ================================================================

    def truncate_tool_result(self, content: str) -> str:
        """
        第一级：截断工具调用结果。

        超过 tool_result_max_tokens 时，保留首尾关键信息。
        """
        max_t = self.config.tool_result_max_tokens
        head_t = self.config.tool_result_head_tokens
        tail_t = self.config.tool_result_tail_tokens

        # 用字符数近似 token 数（中文字符约 1.5 token，英文约 0.75）
        est_tokens = len(content) // 2
        if est_tokens <= max_t:
            return content

        head_chars = head_t * 2
        tail_chars = tail_t * 2
        truncated = (
            content[:head_chars]
            + f"\n\n... [中间 {est_tokens - head_t - tail_t} tokens 已截断] ...\n\n"
            + content[-tail_chars:]
        )
        return truncated

    def _compress_history(self, turns: List[MemoryTurn]) -> List[MemoryTurn]:
        """
        第二级 + 第三级：历史消息压缩。

        策略：
          1. 保留最近 N 轮（compress_keep_recent）完整保留
          2. 其他旧轮次 → 触发 LLM 摘要固化（第三级）
          3. 摘要结果作为 summary 角色插入

        如果 LLM 压缩不可用，退化为硬截断（第二级）。
        """
        keep = self.config.compress_keep_recent
        if len(turns) <= keep:
            return turns

        recent = turns[-keep:]          # 最近 N 轮完整保留
        old = turns[:-keep]             # 需要压缩的旧轮次

        logger.info("Memory compress: old_turns={} recent_turns={}",
                     len(old), len(recent))

        # 尝试 LLM 压缩
        summary = self._llm_compress(old)
        if summary:
            # 用一条 summary 消息替代所有旧轮次
            summary_turn = MemoryTurn(
                role="summary",
                content=f"[历史对话摘要，{len(old)} 轮已压缩]\n{summary}",
                token_count=len(summary) // 2,
                metadata={"compressed_turns": len(old), "method": "llm_summary"},
            )
            return [summary_turn] + recent

        # 退化：硬截断，只保留旧轮次中最近 2 条
        logger.warning("LLM compression failed, falling back to hard truncation")
        return old[-2:] + recent

    def _llm_compress(self, old_turns: List[MemoryTurn]) -> Optional[str]:
        """
        第三级：LLM 摘要固化。

        调用 LLM 将旧对话压缩为简短摘要（2-5 句话），
        保留关键决策、用户偏好、重要事实。
        """
        if not old_turns:
            return None

        try:
            # 构建压缩 prompt
            conversation = "\n".join(
                f"[{t.role}] {t.content[:300]}" for t in old_turns
            )

            messages = [
                {
                    "role": "system",
                    "content": (
                        "将以下对话历史压缩为 2-5 句简短摘要。"
                        "保留：1) 用户的重要偏好和需求 2) 关键决策和结论 3) 待办事项。"
                        "忽略闲聊和重复内容。直接输出摘要，不要加前缀。"
                    ),
                },
                {"role": "user", "content": f"对话历史（{len(old_turns)} 轮）：\n\n{conversation}"},
            ]

            from src.llm import llm_client
            summary = llm_client.chat(messages, temperature=0.1, max_tokens=300)
            logger.info("Memory compressed: {} turns → {} chars summary",
                         len(old_turns), len(summary))
            return summary

        except Exception as e:
            logger.error("LLM compression error: {}", type(e).__name__)
            return None


# ================================================================
# 全局单例
# ================================================================

memory_manager = MemoryManager(store=session_store)
