"""
LLM 精确缓存 — SQLite 实现。

Key = hash(model + messages_json + temperature + knowledge_fingerprint)
文档变更 → fingerprint 变化 → 旧缓存自动失效。

用法:
    from src.llm.cache import llm_cache
    cached = llm_cache.get("deepseek-chat", messages, 0.3)
    llm_cache.set("deepseek-chat", messages, 0.3, response_text)
"""

import json
import hashlib
import sqlite3
import threading
from typing import Optional, List
from loguru import logger

from config.settings import settings


class LLMCache:
    """SQLite 精确缓存"""

    def __init__(self):
        self._lock = threading.Lock()
        self._fingerprint = self._load_fingerprint()
        self._init_db()

    # ----------------------------------------------------------------
    # 知识库指纹
    # ----------------------------------------------------------------

    @property
    def fingerprint_path(self):
        from pathlib import Path
        return Path(settings.chroma_persist_dir).parent / ".fingerprint"

    def _load_fingerprint(self) -> str:
        try:
            return self.fingerprint_path.read_text().strip()
        except Exception:
            return "no-index"

    def update_fingerprint(self, fingerprint: str):
        self._fingerprint = fingerprint
        self.fingerprint_path.parent.mkdir(parents=True, exist_ok=True)
        self.fingerprint_path.write_text(fingerprint)
        logger.info("Cache fingerprint updated: {}", fingerprint)

    # ----------------------------------------------------------------
    # SQLite
    # ----------------------------------------------------------------

    def _init_db(self):
        from pathlib import Path as P
        db_path = settings.cache_db_path_resolved
        P(db_path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cache (
                    key TEXT PRIMARY KEY,
                    response TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()

    def _make_key(self, model: str, messages: List[dict], temperature: float) -> str:
        raw = json.dumps({
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "fingerprint": self._fingerprint,
        }, sort_keys=True)
        return hashlib.md5(raw.encode()).hexdigest()

    # ----------------------------------------------------------------
    # 读写
    # ----------------------------------------------------------------

    def get(self, model: str, messages: List[dict], temperature: float) -> Optional[str]:
        key = self._make_key(model, messages, temperature)
        with self._lock:
            with sqlite3.connect(str(settings.cache_db_path_resolved)) as conn:
                row = conn.execute(
                    "SELECT response FROM cache WHERE key = ?", (key,)
                ).fetchone()
        if row:
            logger.debug("Cache HIT: key={}...", key[:8])
            return row[0]
        logger.debug("Cache MISS: key={}...", key[:8])
        return None

    def set(self, model: str, messages: List[dict], temperature: float, response: str):
        key = self._make_key(model, messages, temperature)
        with self._lock:
            with sqlite3.connect(str(settings.cache_db_path_resolved)) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO cache (key, response) VALUES (?, ?)",
                    (key, response),
                )
                conn.commit()
        logger.debug("Cache SET: key={}...", key[:8])

    def clear(self):
        """清空所有缓存"""
        with self._lock:
            with sqlite3.connect(str(settings.cache_db_path_resolved)) as conn:
                conn.execute("DELETE FROM cache")
                conn.commit()
        logger.info("Cache cleared")


# 全局单例
llm_cache = LLMCache()
