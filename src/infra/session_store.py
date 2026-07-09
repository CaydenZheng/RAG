"""
SQLite 会话存储 — 统一管理 RAG 和 Agent 的多轮对话历史。

设计要点:
  - 与 llm/cache.py 一样的 SQLite 模式，零额外依赖
  - 按 session_id 隔离，支持多用户并发
  - 自动限制历史长度，防止 token 膨胀

用法:
    from src.infra.session_store import session_store
    session_store.add_turn("session_123", "user", "What is RRF?")
    history = session_store.get_recent_history("session_123", limit=6)
"""

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import List, Dict, Optional
from loguru import logger

from config.settings import settings


class SessionStore:
    """SQLite 会话存储管理器"""

    def __init__(self, db_path: str = None):
        if db_path is None:
            db_path = str(Path(settings.chroma_persist_dir).parent / "sessions.db")
        self._db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    # ----------------------------------------------------------------
    # 数据库初始化
    # ----------------------------------------------------------------

    def _init_db(self):
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    metadata TEXT DEFAULT '{}'
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_session_id
                ON sessions(session_id)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_session_time
                ON sessions(session_id, timestamp)
            """)
            conn.commit()

    # ----------------------------------------------------------------
    # 读写操作
    # ----------------------------------------------------------------

    def add_turn(self, session_id: str, role: str, content: str,
                 metadata: dict = None):
        """追加一轮对话"""
        with self._lock:
            with sqlite3.connect(self._db_path) as conn:
                conn.execute(
                    "INSERT INTO sessions (session_id, role, content, timestamp, metadata) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (session_id, role, content, time.time(),
                     json.dumps(metadata or {}, ensure_ascii=False)),
                )
                conn.commit()

    def get_history(self, session_id: str, limit: int = 20) -> List[Dict[str, str]]:
        """
        获取会话历史（按时间升序），返回简单的 role/content 列表。

        Args:
            session_id: 会话 ID
            limit: 最多返回轮数

        Returns:
            [{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}, ...]
        """
        with self._lock:
            with sqlite3.connect(self._db_path) as conn:
                rows = conn.execute(
                    "SELECT role, content FROM sessions "
                    "WHERE session_id = ? ORDER BY timestamp ASC LIMIT ?",
                    (session_id, limit),
                ).fetchall()
        return [{"role": r, "content": c} for r, c in rows]

    def get_recent_history(self, session_id: str, limit: int = 6) -> List[Dict[str, str]]:
        """
        获取最近 N 轮对话（按时间降序取 N 条后翻转）。

        用于追加到 LLM 上下文，确保不超过 token 预算。
        """
        with self._lock:
            with sqlite3.connect(self._db_path) as conn:
                rows = conn.execute(
                    "SELECT role, content FROM sessions "
                    "WHERE session_id = ? ORDER BY timestamp DESC LIMIT ?",
                    (session_id, limit),
                ).fetchall()
        # 翻转回时间正序
        result = [{"role": r, "content": c} for r, c in reversed(rows)]
        return result

    def history_count(self, session_id: str) -> int:
        """返回会话轮次总数"""
        with self._lock:
            with sqlite3.connect(self._db_path) as conn:
                row = conn.execute(
                    "SELECT COUNT(*) FROM sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
        return row[0] if row else 0

    def clear(self, session_id: str):
        """清除指定会话的全部历史"""
        with self._lock:
            with sqlite3.connect(self._db_path) as conn:
                conn.execute(
                    "DELETE FROM sessions WHERE session_id = ?", (session_id,),
                )
                conn.commit()
        logger.info("Session cleared: {}", session_id)

    def list_sessions(self) -> List[str]:
        """列出所有活跃的 session_id"""
        with self._lock:
            with sqlite3.connect(self._db_path) as conn:
                rows = conn.execute(
                    "SELECT DISTINCT session_id FROM sessions ORDER BY session_id"
                ).fetchall()
        return [r[0] for r in rows]


# 全局单例
session_store = SessionStore()
