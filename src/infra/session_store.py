"""SQLite-backed conversation history shared by RAG and Agent flows."""

import json
import sqlite3
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from config.settings import settings
from src.security.session_ids import validate_storage_session_id


@dataclass(frozen=True)
class SessionTurn:
    """One stored conversation message with its original role and metadata."""

    role: str
    content: str
    timestamp: float = field(default_factory=time.time)
    token_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HistorySnapshot:
    """Ordered session turns paired with their optimistic revision."""

    turns: tuple[SessionTurn, ...]
    revision: tuple[int, int, int]


class SessionStore:
    """Persist ordered session history with atomic multi-message writes."""

    def __init__(self, db_path: str | None = None) -> None:
        if db_path is None:
            db_path = str(Path(settings.chroma_persist_dir).parent / "sessions.db")
        self._db_path = db_path
        self._session_locks = tuple(threading.RLock() for _ in range(64))
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path, timeout=30)
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _init_db(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    token_count INTEGER NOT NULL DEFAULT 0
                )
            """)
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(sessions)")
            }
            if "token_count" not in columns:
                connection.execute(
                    "ALTER TABLE sessions "
                    "ADD COLUMN token_count INTEGER NOT NULL DEFAULT 0"
                )
            connection.execute("""
                CREATE INDEX IF NOT EXISTS idx_session_id
                ON sessions(session_id)
            """)
            connection.execute("""
                CREATE INDEX IF NOT EXISTS idx_session_order
                ON sessions(session_id, id)
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS session_state (
                    session_id TEXT PRIMARY KEY,
                    epoch INTEGER NOT NULL DEFAULT 0
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS memory_compression_claims (
                    session_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    claimed_at REAL NOT NULL
                )
            """)

    def _lock_for(self, session_id: str) -> threading.RLock:
        return self._session_locks[hash(session_id) % len(self._session_locks)]

    @staticmethod
    def _validate_turn(turn: SessionTurn) -> None:
        if not isinstance(turn.role, str) or not turn.role:
            raise ValueError("session role must be a non-empty string")
        if not isinstance(turn.content, str):
            raise ValueError("session content must be a string")

    @staticmethod
    def _encoded_turn(turn: SessionTurn) -> tuple[Any, ...]:
        return (
            turn.role,
            turn.content,
            turn.timestamp,
            json.dumps(turn.metadata, ensure_ascii=False),
            turn.token_count,
        )

    @staticmethod
    def _decoded_turn(row: tuple[Any, ...]) -> SessionTurn:
        role, content, timestamp, metadata, token_count = row
        try:
            decoded_metadata = json.loads(metadata)
        except (TypeError, json.JSONDecodeError):
            decoded_metadata = {}
        return SessionTurn(
            role=role,
            content=content,
            timestamp=timestamp,
            token_count=token_count,
            metadata=decoded_metadata,
        )

    def _insert_turns(
        self,
        connection: sqlite3.Connection,
        session_id: str,
        turns: list[SessionTurn],
    ) -> None:
        connection.executemany(
            "INSERT INTO sessions "
            "(session_id, role, content, timestamp, metadata, token_count) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(session_id, *self._encoded_turn(turn)) for turn in turns],
        )

    def add_turn(
        self,
        session_id: str,
        role: str,
        content: str,
        metadata: dict[str, Any] | None = None,
        *,
        token_count: int = 0,
    ) -> None:
        """Append one message. Prefer append_exchange for a completed RAG turn."""
        self.append_turns(
            session_id,
            [
                SessionTurn(
                    role=role,
                    content=content,
                    token_count=token_count,
                    metadata=metadata or {},
                )
            ],
        )

    def append_exchange(
        self,
        session_id: str,
        user_content: str,
        assistant_content: str,
    ) -> None:
        """Append one user/assistant exchange in a single transaction."""
        self.append_turns(
            session_id,
            [
                SessionTurn(role="user", content=user_content),
                SessionTurn(role="assistant", content=assistant_content),
            ],
        )

    def append_turns(
        self, session_id: str, turns: Iterable[SessionTurn]
    ) -> None:
        """Append a contiguous message group atomically in insertion order."""
        session_id = validate_storage_session_id(session_id)
        pending = list(turns)
        for turn in pending:
            self._validate_turn(turn)
        if not pending:
            return

        with self._lock_for(session_id), self._connect() as connection:
            self._insert_turns(connection, session_id, pending)

    @staticmethod
    def _invalidate_older_operations(
        connection: sqlite3.Connection, session_id: str
    ) -> None:
        connection.execute(
            "DELETE FROM memory_compression_claims WHERE session_id = ?",
            (session_id,),
        )
        connection.execute(
            "INSERT INTO session_state (session_id, epoch) VALUES (?, 1) "
            "ON CONFLICT(session_id) DO UPDATE SET epoch = epoch + 1",
            (session_id,),
        )

    def replace_history(
        self, session_id: str, turns: Iterable[SessionTurn]
    ) -> None:
        """Replace a session's history and invalidate older operations."""
        session_id = validate_storage_session_id(session_id)
        replacement: list[SessionTurn] = list(turns)
        for turn in replacement:
            self._validate_turn(turn)

        with self._lock_for(session_id), self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM sessions WHERE session_id = ?", (session_id,)
            )
            self._insert_turns(connection, session_id, replacement)
            self._invalidate_older_operations(connection, session_id)
            connection.commit()

    def read_snapshot(self, session_id: str) -> HistorySnapshot:
        """Read one history and revision while holding only a short lock."""
        session_id = validate_storage_session_id(session_id)
        with self._lock_for(session_id), self._connect() as connection:
            connection.execute("BEGIN")
            current_rows: list[tuple[Any, ...]] = connection.execute(
                "SELECT id, role, content, timestamp, metadata, token_count "
                "FROM sessions WHERE session_id = ? ORDER BY id ASC",
                (session_id,),
            ).fetchall()
            revision: tuple[int, int, int] = self._revision(
                connection, session_id
            )
            connection.commit()
        turns: tuple[SessionTurn, ...] = tuple(
            self._decoded_turn(row[1:]) for row in current_rows
        )
        return HistorySnapshot(turns=turns, revision=revision)

    def replace_history_if_revision(
        self,
        session_id: str,
        expected_revision: tuple[int, int, int],
        turns: Iterable[SessionTurn],
        *,
        compression_owner: str | None = None,
    ) -> bool:
        """Replace history only when its revision and claim are unchanged."""
        session_id = validate_storage_session_id(session_id)
        replacement: list[SessionTurn] = list(turns)
        for turn in replacement:
            self._validate_turn(turn)

        with self._lock_for(session_id), self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if self._revision(connection, session_id) != expected_revision:
                connection.rollback()
                return False
            if compression_owner is not None:
                owner: tuple[Any, ...] | None = connection.execute(
                    "SELECT owner_id FROM memory_compression_claims "
                    "WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if owner is None or owner[0] != compression_owner:
                    connection.rollback()
                    return False
            connection.execute(
                "DELETE FROM sessions WHERE session_id = ?", (session_id,)
            )
            self._insert_turns(connection, session_id, replacement)
            connection.commit()
        return True

    def append_turns_if_epoch(
        self,
        session_id: str,
        expected_epoch: int,
        turns: Iterable[SessionTurn],
    ) -> bool:
        """Append only while the session has not been cleared or replaced."""
        session_id = validate_storage_session_id(session_id)
        pending: list[SessionTurn] = list(turns)
        for turn in pending:
            self._validate_turn(turn)
        if not pending:
            return True

        with self._lock_for(session_id), self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if self._revision(connection, session_id)[0] != expected_epoch:
                connection.rollback()
                return False
            self._insert_turns(connection, session_id, pending)
            connection.commit()
        return True

    def try_claim_compression(
        self,
        session_id: str,
        expected_epoch: int,
        owner_id: str,
        *,
        ttl_seconds: float,
    ) -> bool:
        """Claim one session's compression across processes."""
        session_id = validate_storage_session_id(session_id)
        now: float = time.time()
        with self._lock_for(session_id), self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if self._revision(connection, session_id)[0] != expected_epoch:
                connection.rollback()
                return False
            connection.execute(
                "DELETE FROM memory_compression_claims "
                "WHERE session_id = ? AND claimed_at <= ?",
                (session_id, now - ttl_seconds),
            )
            cursor: sqlite3.Cursor = connection.execute(
                "INSERT OR IGNORE INTO memory_compression_claims "
                "(session_id, owner_id, claimed_at) VALUES (?, ?, ?)",
                (session_id, owner_id, now),
            )
            connection.commit()
        return cursor.rowcount > 0

    def release_compression_claim(
        self, session_id: str, owner_id: str
    ) -> None:
        """Release only the compression claim owned by this operation."""
        session_id = validate_storage_session_id(session_id)
        with self._lock_for(session_id), self._connect() as connection:
            connection.execute(
                "DELETE FROM memory_compression_claims "
                "WHERE session_id = ? AND owner_id = ?",
                (session_id, owner_id),
            )

    @staticmethod
    def _revision(
        connection: sqlite3.Connection, session_id: str
    ) -> tuple[int, int, int]:
        history: tuple[Any, ...] = connection.execute(
            "SELECT COUNT(*), COALESCE(MAX(id), 0) "
            "FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        state: tuple[Any, ...] | None = connection.execute(
            "SELECT epoch FROM session_state WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return (state[0] if state else 0, history[0], history[1])

    def get_turns(
        self, session_id: str, limit: int | None = None
    ) -> list[SessionTurn]:
        """Return messages in stable insertion order."""
        session_id = validate_storage_session_id(session_id)
        query = (
            "SELECT role, content, timestamp, metadata, token_count "
            "FROM sessions WHERE session_id = ? ORDER BY id ASC"
        )
        params: tuple[Any, ...] = (session_id,)
        if limit is not None:
            query += " LIMIT ?"
            params = (session_id, limit)
        with self._lock_for(session_id), self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._decoded_turn(row) for row in rows]

    def get_history(
        self, session_id: str, limit: int = 20
    ) -> list[dict[str, str]]:
        """Return role/content pairs in stable insertion order."""
        return [
            {"role": turn.role, "content": turn.content}
            for turn in self.get_turns(session_id, limit=limit)
        ]

    def get_recent_history(
        self, session_id: str, limit: int = 6
    ) -> list[dict[str, str]]:
        """Return the most recent messages in stable insertion order."""
        session_id = validate_storage_session_id(session_id)
        with self._lock_for(session_id), self._connect() as connection:
            rows = connection.execute(
                "SELECT role, content FROM sessions "
                "WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        return [
            {"role": role, "content": content}
            for role, content in reversed(rows)
        ]

    def history_count(self, session_id: str) -> int:
        """Return the number of stored messages for a session."""
        session_id = validate_storage_session_id(session_id)
        with self._lock_for(session_id), self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return row[0] if row else 0

    def clear(self, session_id: str) -> bool:
        """Delete a session and invalidate older operations atomically."""
        session_id = validate_storage_session_id(session_id)
        with self._lock_for(session_id), self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor: sqlite3.Cursor = connection.execute(
                "DELETE FROM sessions WHERE session_id = ?", (session_id,)
            )
            self._invalidate_older_operations(connection, session_id)
            connection.commit()
        existed = cursor.rowcount > 0
        logger.info("Session cleared")
        return existed

    def list_sessions(self) -> list[str]:
        """List all session IDs with stored history."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT session_id FROM sessions ORDER BY session_id"
            ).fetchall()
        return [row[0] for row in rows]


session_store = SessionStore()
