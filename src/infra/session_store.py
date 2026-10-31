"""SQLite-backed conversation history shared by RAG and Agent flows."""

import base64
import hashlib
import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from contextlib import closing, contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from loguru import logger

from config.settings import settings
from src.infra.exchange_context import exchange_context
from src.security.request_authorization import (
    AccountLimitExceeded,
    AuthorizationRevoked,
    ConversationConflict,
    PrivateSessionError,
    authorization_commit_guard,
    current_authorization,
    require_active_authorization,
    require_session_owner,
)
from src.security.session_ids import (
    SessionNamespace,
    account_scoped_session_id,
    validate_storage_session_id,
)

_COMMIT_RETRY_INTERVAL_SECONDS: float = 0.005
MAX_ACCOUNT_CONVERSATIONS: int = 200
MAX_CONVERSATION_EXCHANGES: int = 2000
MAX_SQLITE_SEQUENCE: int = (1 << 63) - 1
MAX_EXPORT_BYTES: int = 2 * 1024 * 1024


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


class CommitPermit:
    """Order cancellation acknowledgement with one non-blocking commit attempt."""

    def __init__(self, deadline: float | None = None) -> None:
        self._deadline: float | None = deadline
        self._revoked: threading.Event = threading.Event()
        self._lock: threading.Lock = threading.Lock()

    def __call__(self) -> bool:
        """Preflight snapshot only; the store must hold guard through COMMIT."""
        return not self._revoked.is_set() and (
            self._deadline is None or time.monotonic() < self._deadline
        )

    def revoke(self) -> None:
        with self._lock:
            self._revoked.set()

    @contextmanager
    def guard(self) -> Iterator[bool]:
        with self._lock:
            yield self()


class _ModelChangePermit(CommitPermit):
    """Order model metadata commits against generation registration."""

    def __init__(self, session_id: str) -> None:
        super().__init__()
        self.session_id: str = session_id

    @contextmanager
    def guard(self) -> Iterator[bool]:
        from src.security.request_authorization import (
            _RUNS,
            _RUNS_LOCK,
            ConversationBusy,
        )

        # Held only for one non-blocking COMMIT; released before BUSY retries.
        with _RUNS_LOCK:
            running = _RUNS.get(self.session_id)
            if running is not None and not running.revoked.is_set():
                raise ConversationBusy("conversation_busy")
            yield True


class SessionStore:
    """Persist ordered session history with atomic multi-message writes."""

    def __init__(self, db_path: str | None = None) -> None:
        if db_path is None:
            db_path = str(settings.chroma_path.parent / "sessions.db")
        self._db_path = db_path
        self._session_locks = tuple(threading.RLock() for _ in range(64))
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path, timeout=30)
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
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

    def initialize_ownership(self) -> None:
        """Explicit account-mode migration only; never claim old anonymous rows."""
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    name TEXT PRIMARY KEY, version INTEGER NOT NULL, applied_at REAL NOT NULL
                )
            """)
            version = connection.execute(
                "SELECT version FROM schema_migrations WHERE name='conversation_ownership'"
            ).fetchone()
            if version is not None and version[0] != 1:
                raise RuntimeError("Unsupported ownership schema")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS conversations (
                    owner_user_id TEXT NOT NULL, public_id TEXT NOT NULL,
                    mode TEXT NOT NULL CHECK(mode IN ('rag','agent')),
                    storage_id TEXT NOT NULL UNIQUE, title TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 0, last_model TEXT,
                    deleted INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(owner_user_id, public_id)
                )
            """)
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations VALUES('conversation_ownership',1,?)",
                (time.time(),),
            )
            connection.execute("""
                CREATE TABLE IF NOT EXISTS transcript_exchanges (
                    id TEXT NOT NULL UNIQUE, storage_id TEXT NOT NULL,
                    seq INTEGER NOT NULL, client_turn_id TEXT NOT NULL,
                    question TEXT NOT NULL, answer TEXT NOT NULL,
                    completed_at REAL NOT NULL, used_model TEXT, index_version TEXT,
                    sources TEXT NOT NULL DEFAULT '[]',
                    warnings TEXT NOT NULL DEFAULT '[]',
                    PRIMARY KEY(storage_id,seq), UNIQUE(storage_id,client_turn_id),
                    FOREIGN KEY(storage_id) REFERENCES conversations(storage_id)
                )
            """)
            transcript_version = connection.execute("SELECT version FROM schema_migrations WHERE name='conversation_transcript'").fetchone()
            if transcript_version is not None and transcript_version[0] != 1:
                raise RuntimeError("Unsupported transcript schema")
            transcript_columns = {row[1] for row in connection.execute("PRAGMA table_info(transcript_exchanges)")}
            if "warnings" not in transcript_columns:
                connection.execute("ALTER TABLE transcript_exchanges ADD COLUMN warnings TEXT NOT NULL DEFAULT '[]'")
            conversation_columns = {row[1] for row in connection.execute("PRAGMA table_info(conversations)")}
            if "last_seq" not in conversation_columns:
                connection.execute("ALTER TABLE conversations ADD COLUMN last_seq INTEGER NOT NULL DEFAULT 0")
                connection.execute("UPDATE conversations SET last_seq=COALESCE((SELECT MAX(seq) FROM transcript_exchanges WHERE transcript_exchanges.storage_id=conversations.storage_id),0)")
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations VALUES('conversation_transcript',1,?)",
                (time.time(),),
            )
            connection.execute("CREATE TABLE IF NOT EXISTS model_reservations (subject TEXT NOT NULL, day INTEGER NOT NULL, units INTEGER NOT NULL, PRIMARY KEY(subject,day))")
            if "selected_model" not in conversation_columns:
                connection.execute("ALTER TABLE conversations ADD COLUMN selected_model TEXT")
            if "requested_model" not in transcript_columns:
                connection.execute("ALTER TABLE transcript_exchanges ADD COLUMN requested_model TEXT")
            if "skills" not in transcript_columns:
                connection.execute("ALTER TABLE transcript_exchanges ADD COLUMN skills TEXT NOT NULL DEFAULT '[]'")
            from src.infra.skill_store import initialize_workspace_schema

            initialize_workspace_schema(connection)
            from src.infra.attachment_store import initialize_attachment_schema

            initialize_attachment_schema(connection)

    def reserve_model_units(self, units: int) -> None:
        authorization = current_authorization()
        if authorization is None:
            return
        require_active_authorization()
        day: int = int(time.time() // 86400)
        from src.security.session_ids import account_scope

        subjects: tuple[tuple[str,int], ...] = (
            ("user:"+account_scope(authorization.user_id),settings.account_daily_model_units),
            ("source:"+authorization.source_scope,settings.source_daily_model_units),
        )
        with closing(self._connect()) as connection, connection:
            # A model preflight must never block the event loop behind a writer.
            # Busy reservation fails closed before spending external API budget.
            connection.execute("PRAGMA busy_timeout=0")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM model_reservations WHERE day<?", (day,))
            for subject, limit in subjects:
                row = connection.execute("SELECT units FROM model_reservations WHERE subject=? AND day=?", (subject,day)).fetchone()
                if (row[0] if row else 0) + units > limit:
                    raise AccountLimitExceeded("account_quota_exceeded")
                connection.execute("INSERT INTO model_reservations VALUES(?,?,?) ON CONFLICT(subject,day) DO UPDATE SET units=units+excluded.units",(subject,day,units))
            if not self._commit_if_permitted(connection,None):
                raise AuthorizationRevoked("auth_required")

    @staticmethod
    def _owner_id() -> str:
        require_active_authorization()
        authorization = current_authorization()
        if authorization is None:
            raise AuthorizationRevoked("auth_required")
        return authorization.user_id

    @staticmethod
    def _iso(timestamp: float) -> str:
        return datetime.fromtimestamp(timestamp, UTC).isoformat()

    def _summary(self, connection: sqlite3.Connection, public_id: str, *, deleted: bool = False) -> dict[str, Any]:
        row = connection.execute(
            "SELECT public_id,mode,title,created_at,updated_at,revision,last_model,storage_id,deleted,selected_model "
            "FROM conversations WHERE owner_user_id=? AND public_id=?",
            (self._owner_id(), public_id),
        ).fetchone()
        if row is None or (row[8] and not deleted):
            raise PrivateSessionError("private_session_not_found")
        epoch: int = self._revision(connection, row[7])[0]
        from src.core.run_options import conversation_model

        preference = connection.execute("SELECT default_model FROM workspace_preferences WHERE owner_user_id=?", (self._owner_id(),)).fetchone()
        choice = conversation_model(row[9], row[6], preference[0] if preference else "flash")
        return dict(id=row[0], mode=row[1], title=row[2], created_at=self._iso(row[3]),
                    updated_at=self._iso(row[4]), revision=row[5], epoch=epoch, last_model=row[6], model=choice)

    def conversation(self, public_id: str) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            return self._summary(connection, public_id)

    @staticmethod
    def _cursor(value: list[Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode()

    @staticmethod
    def _decode_cursor(cursor: str, resource: str) -> list[Any]:
        try:
            if len(cursor) > 512:
                raise ValueError
            value = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
            if not isinstance(value, list) or len(value) != 3 or value[0] != resource:
                raise ValueError
            return value
        except (ValueError, UnicodeError, TypeError):
            raise ValueError("Invalid cursor") from None

    def conversations(self, *, mode: str | None = None, limit: int = 20,
                      cursor: str | None = None, title_query: str | None = None) -> dict[str, Any]:
        owner: str = self._owner_id()
        if mode not in {None, "rag", "agent"} or not 1 <= limit <= 50:
            raise ValueError("Invalid page")
        clauses: list[str] = ["owner_user_id=?", "deleted=0"]
        parameters: list[Any] = [owner]
        if mode:
            clauses.append("mode=?")
            parameters.append(mode)
        if title_query is not None:
            clauses.append("instr(lower(title),lower(?))>0")
            parameters.append(title_query)
        resource: str = "conversations:" + (mode or "all") + ":" + hashlib.sha256((title_query or "").encode()).hexdigest()[:16]
        if cursor:
            _, timestamp, public_id = self._decode_cursor(cursor, resource)
            if type(timestamp) not in {float, int} or not 0 <= timestamp <= 253402300799 or not isinstance(public_id, str):
                raise ValueError("Invalid cursor")
            clauses.append("(updated_at,public_id)<(?,?)")
            parameters.extend([timestamp, public_id])
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT public_id,updated_at FROM conversations WHERE " + " AND ".join(clauses)
                + " ORDER BY updated_at DESC,public_id DESC LIMIT ?", (*parameters, limit + 1),
            ).fetchall()
            items: list[dict[str, Any]] = [self._summary(connection, row[0]) for row in rows[:limit]]
        next_cursor: str | None = self._cursor([resource, rows[limit-1][1], rows[limit-1][0]]) if len(rows) > limit else None
        return {"items": items, "next_cursor": next_cursor}

    def transcript(self, public_id: str, *, limit: int = 20, before: str | None = None,
                   client_turn_id: str | None = None) -> dict[str, Any]:
        summary: dict[str, Any] = self.conversation(public_id)
        storage_id: str = self.owned_session(self._owner_id(), public_id, summary["mode"], create=False)
        if not 1 <= limit <= 50:
            raise ValueError("Invalid page")
        clauses: list[str] = ["storage_id=?"]
        parameters: list[Any] = [storage_id]
        if client_turn_id:
            clauses.append("client_turn_id=?")
            parameters.append(client_turn_id)
        if before:
            _, resource, sequence = self._decode_cursor(before, "turns")
            if resource != public_id or type(sequence) is not int or not 1 <= sequence <= MAX_SQLITE_SEQUENCE:
                raise ValueError("Invalid cursor")
            clauses.append("seq<?")
            parameters.append(sequence)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT id,client_turn_id,seq,question,answer,completed_at,used_model,index_version,sources,warnings,requested_model,skills,attachments "
                "FROM transcript_exchanges WHERE " + " AND ".join(clauses) + " ORDER BY seq DESC LIMIT ?",
                (*parameters, limit + 1),
            ).fetchall()
        page = rows[:limit]
        items: list[dict[str, Any]] = [
            dict(id=row[0],client_turn_id=row[1],seq=row[2],question=row[3],answer=row[4],
                 completed_at=self._iso(row[5]),requested_model=row[10],used_model=row[6],
                 index_version=row[7],sources=json.loads(row[8]),attachments=json.loads(row[12]),skills=json.loads(row[11]),warnings=json.loads(row[9]))
            for row in reversed(page)
        ]
        with closing(self._connect()) as connection:
            for item in items:
                for attachment in item["attachments"]:
                    attachment["deleted"] = not connection.execute("SELECT 1 FROM conversation_attachments WHERE storage_id=? AND id=? AND state='ready'", (storage_id, attachment["id"])).fetchone()
        return {"items": items, "next_before": self._cursor(["turns", public_id, page[-1][2]]) if len(rows)>limit else None}

    def export_conversation(self, public_id: str) -> bytes:
        """One owner-scoped read snapshot, bounded before sending any bytes."""
        import re

        from src.core.run_options import WorkspaceError

        output: bytearray = bytearray()
        def append(text: str) -> None:
            encoded: bytes = text.encode("utf8")
            if len(output) + len(encoded) > MAX_EXPORT_BYTES:
                raise WorkspaceError("export_too_large", 413)
            output.extend(encoded)
        def literal(text: str) -> str:
            # Literal blocks keep external Markdown viewers from interpreting
            # untrusted HTML or links as active content.
            fence: str = "`" * max(3, max((len(item) + 1 for item in re.findall(r"`+", text)), default=3))
            return f"{fence}text\n{text}\n{fence}\n\n"
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN")
            summary: dict[str, Any] = self._summary(connection, public_id)
            row = connection.execute("SELECT storage_id FROM conversations WHERE owner_user_id=? AND public_id=? AND deleted=0",
                                     (self._owner_id(), public_id)).fetchone()
            append("# 会话导出\n\n" + literal(summary["title"]) + "仅包含已完成问答；正文以原样文本保存，不重放工具或隐藏过程。\n\n")
            for turn in connection.execute("SELECT seq,question,answer,used_model,sources,attachments FROM transcript_exchanges WHERE storage_id=? ORDER BY seq", (row[0],)):
                require_active_authorization()
                append(f"## 回合 {turn[0]}\n\n### 问题\n\n" + literal(turn[1]) + "### 回答\n\n" + literal(turn[2]))
                if turn[3]:
                    append("实际模型\n\n" + literal(turn[3]))
                for source in json.loads(turn[4]):
                    ref: Any = source.get("ref")
                    if isinstance(ref, int) and not isinstance(ref, bool) and ref > 0:
                        label: str = "附件来源" if source.get("source_kind") == "attachment" else "知识库来源"
                        append(literal(f"引用 [{ref}] {label}"))
                for attachment in json.loads(turn[5]):
                    exists: bool = connection.execute("SELECT 1 FROM conversation_attachments WHERE storage_id=? AND id=? AND state='ready'", (row[0], attachment["id"])).fetchone() is not None
                    append(literal("附件 " + attachment["name"] + ("" if exists else "（已删除）")))
            require_active_authorization()
        return bytes(output)

    def ensure_new_turn(self, storage_id: str, client_turn_id: str) -> None:
        storage_id = self._authorized_session_id(storage_id)
        with closing(self._connect()) as connection:
            exists = connection.execute(
                "SELECT 1 FROM transcript_exchanges WHERE storage_id=? AND client_turn_id=?",
                (storage_id, client_turn_id),
            ).fetchone()
            count: int = connection.execute("SELECT COUNT(*) FROM transcript_exchanges WHERE storage_id=?", (storage_id,)).fetchone()[0]
        if exists or count >= MAX_CONVERSATION_EXCHANGES:
            raise ConversationConflict("conversation_conflict")

    def mutate_conversation(self, public_id: str, expected_revision: int, *,
                            title: str | None = None, clear: bool = False,
                            delete: bool = False, model: str | None = None) -> dict[str, Any]:
        owner: str = self._owner_id()
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            summary = self._summary(connection, public_id, deleted=delete)
            row = connection.execute("SELECT storage_id,deleted FROM conversations WHERE owner_user_id=? AND public_id=?", (owner, public_id)).fetchone()
            if row[1] and delete:
                return summary
            if summary["revision"] != expected_revision:
                raise ConversationConflict("conversation_conflict")
            if clear or delete:
                from src.security.request_authorization import cancel_session_generation

                cancel_session_generation(row[0])
                connection.execute("DELETE FROM sessions WHERE session_id=?", (row[0],))
                connection.execute("DELETE FROM transcript_exchanges WHERE storage_id=?", (row[0],))
                connection.execute("UPDATE conversation_attachments SET state='deleted',cleanup=1 WHERE storage_id=?", (row[0],))
                self._invalidate_older_operations(connection, row[0])
            if model is not None:
                from src.core.run_options import resolve_model

                resolve_model(model)
                connection.execute("UPDATE conversations SET selected_model=? WHERE owner_user_id=? AND public_id=?", (model, owner, public_id))
            connection.execute(
                "UPDATE conversations SET title=?,updated_at=?,revision=revision+1,deleted=? WHERE owner_user_id=? AND public_id=?",
                (title if title is not None else summary["title"], time.time(), int(delete), owner, public_id),
            )
            if not self._commit_if_permitted(connection, _ModelChangePermit(row[0]) if model is not None else None):
                raise AuthorizationRevoked("auth_required")
            if clear or delete:
                from src.infra.attachment_store import AttachmentStore

                AttachmentStore(self).cleanup()
            return self._summary(connection, public_id, deleted=delete)

    def _commit_completed_exchange(self, connection: sqlite3.Connection, session_id: str,
                                   turns: list[SessionTurn]) -> None:
        """Same transaction as Memory; compression leaves the complete tail intact."""
        context = exchange_context.get()
        if context is None or current_authorization() is None:
            return
        owner: str = self._owner_id()
        row = connection.execute("SELECT title,deleted,last_seq FROM conversations WHERE owner_user_id=? AND storage_id=?", (owner, session_id)).fetchone()
        if row is None or row[1]:
            raise PrivateSessionError("private_session_not_found")
        count: int = connection.execute("SELECT COUNT(*) FROM transcript_exchanges WHERE storage_id=?", (session_id,)).fetchone()[0]
        if count >= MAX_CONVERSATION_EXCHANGES:
            raise ConversationConflict("conversation_conflict")
        if not turns or turns[-1].role != "assistant":
            raise ValueError("Incomplete exchange")
        for attachment in context.attachments:
            if not connection.execute("SELECT 1 FROM conversation_attachments WHERE storage_id=? AND id=? AND checksum=? AND state='ready'", (session_id, attachment["id"], attachment["checksum"])).fetchone():
                raise ConversationConflict("conversation_conflict")
        sequence: int = row[2] + 1
        now: float = time.time()
        connection.execute(
            "INSERT INTO transcript_exchanges(id,storage_id,seq,client_turn_id,question,answer,completed_at,used_model,index_version,sources,warnings,requested_model,skills,attachments) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex,session_id,sequence,context.client_turn_id,
             context.question,turns[-1].content,now,context.used_model,context.index_version,
             json.dumps(context.sources,ensure_ascii=False),json.dumps(context.warnings),
             context.requested_model,json.dumps(context.skills),json.dumps(context.attachments)),
        )
        title: str = row[0] or " ".join(context.question.split())[:80]
        connection.execute(
            "UPDATE conversations SET title=?,updated_at=?,revision=revision+1,"
            "last_seq=?,last_model=COALESCE(?,last_model) "
            "WHERE owner_user_id=? AND storage_id=? AND deleted=0",
            (title, now, sequence, context.used_model, owner, session_id),
        )

    def owned_session(self, user_id: str, public_id: str, mode: SessionNamespace, *, create: bool, model: str | None = None) -> str:
        storage_id: str = account_scoped_session_id(user_id, public_id, mode)
        require_active_authorization()
        require_session_owner(storage_id)
        with closing(self._connect()) as connection, connection:
            if create:
                connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT mode,storage_id,deleted FROM conversations WHERE owner_user_id=? AND public_id=?",
                (user_id, public_id),
            ).fetchone()
            if row is not None:
                if row[2]:
                    raise PrivateSessionError("private_session_not_found")
                if row[0] != mode:
                    if create:
                        raise ConversationConflict("conversation_conflict")
                    raise PrivateSessionError("private_session_not_found")
                return row[1]
            if not create:
                raise PrivateSessionError("private_session_not_found")
            count: int = connection.execute(
                "SELECT COUNT(*) FROM conversations WHERE owner_user_id=? AND deleted=0", (user_id,)
            ).fetchone()[0]
            if count >= MAX_ACCOUNT_CONVERSATIONS:
                raise ConversationConflict("conversation_conflict")
            if model is not None:
                from src.core.run_options import resolve_model

                resolve_model(model)
            now: float = time.time()
            connection.execute("""
                INSERT INTO conversations(owner_user_id,public_id,mode,storage_id,created_at,updated_at,selected_model)
                VALUES(?,?,?,?,?,?,?)
            """, (user_id, public_id, mode, storage_id, now, now, model))
            if not self._commit_if_permitted(connection, None):
                raise AuthorizationRevoked("auth_required")
            if current_authorization() is not None:
                from src.infra.attachment_store import AttachmentStore

                AttachmentStore(self).cleanup()
        return storage_id

    def _authorized_session_id(self, session_id: str) -> str:
        session_id = validate_storage_session_id(session_id)
        require_active_authorization()
        require_session_owner(session_id)
        authorization = current_authorization()
        if authorization is not None:
            with closing(self._connect()) as connection, connection:
                row = connection.execute(
                    "SELECT 1 FROM conversations WHERE owner_user_id=? AND storage_id=? AND deleted=0",
                    (authorization.user_id, session_id),
                ).fetchone()
            if row is None:
                raise PrivateSessionError("private_session_not_found")
        return session_id

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
        session_id = self._authorized_session_id(session_id)
        pending = list(turns)
        for turn in pending:
            self._validate_turn(turn)
        if not pending:
            return

        with self._lock_for(session_id), self._connect() as connection:
            self._insert_turns(connection, session_id, pending)
            if current_authorization() is not None and not self._commit_if_permitted(connection, None):
                raise AuthorizationRevoked("auth_required")

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
        session_id = self._authorized_session_id(session_id)
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
            if not self._commit_if_permitted(connection, None):
                raise AuthorizationRevoked("auth_required")

    def read_snapshot(self, session_id: str) -> HistorySnapshot:
        """Read one history and revision while holding only a short lock."""
        session_id = self._authorized_session_id(session_id)
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

    @staticmethod
    def _commit_if_permitted(
        connection: sqlite3.Connection,
        can_commit: Callable[[], bool] | None,
    ) -> bool:
        """Use CommitPermit for ordered revocation; plain predicates only preflight."""
        if can_commit is None and current_authorization() is None:
            connection.commit()
            return True

        busy_timeout_ms: int = connection.execute("PRAGMA busy_timeout").fetchone()[0]
        retry_deadline: float = time.monotonic() + busy_timeout_ms / 1000
        # A reader may hold COMMIT's lock upgrade past the request deadline.
        # Make each attempt non-blocking, then recheck the guard (which owns
        # cancellation/deadline) before every retry. Keep SQLite's wait cap.
        connection.execute("PRAGMA busy_timeout = 0")
        try:
            while True:
                try:
                    with authorization_commit_guard() as authorized:
                        if not authorized:
                            connection.rollback()
                            return False
                        # The permit and its revoker share this lock. Do not
                        # hold it during auth I/O, compression or busy retry sleep.
                        with (
                            can_commit.guard() if isinstance(can_commit, CommitPermit)
                            else nullcontext(can_commit is None or can_commit())
                        ) as permitted:
                            if not permitted:
                                connection.rollback()
                                return False
                            connection.commit()
                            return True
                except sqlite3.OperationalError as exc:
                    error_code: int = getattr(exc, "sqlite_errorcode", 0)
                    remaining: float = retry_deadline - time.monotonic()
                    if error_code & 0xFF != sqlite3.SQLITE_BUSY or remaining <= 0:
                        raise
                    time.sleep(min(_COMMIT_RETRY_INTERVAL_SECONDS, remaining))
        finally:
            connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")

    def replace_history_if_revision(
        self,
        session_id: str,
        expected_revision: tuple[int, int, int],
        turns: Iterable[SessionTurn],
        *,
        compression_owner: str | None = None,
        can_commit: Callable[[], bool] | None = None,
    ) -> bool:
        """Replace history only when its revision and claim are unchanged."""
        session_id = self._authorized_session_id(session_id)
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
            self._commit_completed_exchange(connection, session_id, replacement)
            return self._commit_if_permitted(connection, can_commit)

    def append_turns_if_epoch(
        self,
        session_id: str,
        expected_epoch: int,
        turns: Iterable[SessionTurn],
        *,
        can_commit: Callable[[], bool] | None = None,
    ) -> bool:
        """Append only while the session has not been cleared or replaced."""
        session_id = self._authorized_session_id(session_id)
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
            self._commit_completed_exchange(connection, session_id, pending)
            return self._commit_if_permitted(connection, can_commit)

    def try_claim_compression(
        self,
        session_id: str,
        expected_epoch: int,
        owner_id: str,
        *,
        ttl_seconds: float,
    ) -> bool:
        """Claim one session's compression across processes."""
        session_id = self._authorized_session_id(session_id)
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
        require_session_owner(session_id)
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
        session_id = self._authorized_session_id(session_id)
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
        session_id = self._authorized_session_id(session_id)
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
        session_id = self._authorized_session_id(session_id)
        with self._lock_for(session_id), self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return row[0] if row else 0

    def clear(self, session_id: str) -> bool:
        """Delete a session and invalidate older operations atomically."""
        session_id = self._authorized_session_id(session_id)
        with self._lock_for(session_id), self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor: sqlite3.Cursor = connection.execute(
                "DELETE FROM sessions WHERE session_id = ?", (session_id,)
            )
            self._invalidate_older_operations(connection, session_id)
            if current_authorization() is not None:
                connection.execute("DELETE FROM transcript_exchanges WHERE storage_id=?", (session_id,))
                connection.execute("UPDATE conversation_attachments SET state='deleted',cleanup=1 WHERE storage_id=?", (session_id,))
                connection.execute("UPDATE conversations SET updated_at=?,revision=revision+1 WHERE storage_id=?", (time.time(),session_id))
            if not self._commit_if_permitted(connection, None):
                raise AuthorizationRevoked("auth_required")
        if current_authorization() is not None:
            from src.infra.attachment_store import AttachmentStore

            AttachmentStore(self).cleanup()
        existed = cursor.rowcount > 0
        logger.info("Session cleared")
        return existed

    def list_sessions(self) -> list[str]:
        """List all session IDs with stored history."""
        if current_authorization() is not None:
            raise PrivateSessionError("private_session_not_found")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT session_id FROM sessions ORDER BY session_id"
            ).fetchall()
        return [row[0] for row in rows]


session_store = SessionStore()
