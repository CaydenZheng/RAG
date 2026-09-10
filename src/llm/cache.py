"""SQLite exact cache partitioned by request and generation semantics."""

import hashlib
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional

from loguru import logger

from config.settings import settings


class LLMCache:
    """Store exact provider responses under a complete semantic key."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._fingerprint = self._load_fingerprint()
        self._init_db()

    @property
    def fingerprint_path(self) -> Path:
        return Path(settings.chroma_persist_dir).parent / ".fingerprint"

    def _load_fingerprint(self) -> str:
        try:
            return self.fingerprint_path.read_text(encoding="utf-8").strip()
        except Exception:
            return "no-index"

    def update_fingerprint(self, fingerprint: str) -> None:
        with self._lock:
            self._fingerprint = fingerprint
            self.fingerprint_path.parent.mkdir(parents=True, exist_ok=True)
            self.fingerprint_path.write_text(fingerprint, encoding="utf-8")
        logger.info("Cache fingerprint updated: {}", fingerprint)

    def _init_db(self) -> None:
        db_path = settings.cache_db_path_resolved
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS cache (
                    key TEXT PRIMARY KEY,
                    response TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.commit()

    def _make_key(
        self,
        model: str,
        messages: list[dict],
        temperature: float,
        *,
        max_tokens: int | None = None,
        identity_scope: str = "shared",
        index_version: str | None = None,
        prompt_version: str | None = None,
        generation_parameters: dict[str, Any] | None = None,
    ) -> str:
        parameters = {
            "temperature": temperature,
            "max_tokens": max_tokens,
            **dict(generation_parameters or {}),
        }
        raw = json.dumps(
            {
                "model": model,
                "messages": messages,
                "parameters": parameters,
                "identity_scope": identity_scope,
                "index_version": index_version or self._fingerprint,
                "prompt_version": prompt_version or settings.prompt_version,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def get(
        self,
        model: str,
        messages: list[dict],
        temperature: float,
        **dimensions: Any,
    ) -> Optional[str]:
        key = self._make_key(model, messages, temperature, **dimensions)
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

    def set(
        self,
        model: str,
        messages: list[dict],
        temperature: float,
        response: str,
        **dimensions: Any,
    ) -> None:
        key = self._make_key(model, messages, temperature, **dimensions)
        with self._lock:
            with sqlite3.connect(str(settings.cache_db_path_resolved)) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO cache (key, response) VALUES (?, ?)",
                    (key, response),
                )
                conn.commit()
        logger.debug("Cache SET: key={}...", key[:8])

    def clear(self) -> None:
        with self._lock:
            with sqlite3.connect(str(settings.cache_db_path_resolved)) as conn:
                conn.execute("DELETE FROM cache")
                conn.commit()
        logger.info("Cache cleared")


llm_cache = LLMCache()
