"""Owned, bounded conversation files; opaque paths and transactional lifecycle."""

import hashlib
import json
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from src.core.run_options import AttachmentSnapshot, WorkspaceError
from src.infra.exchange_context import canonical_uuid
from src.infra.sqlite_transactions import WritePolicy, storage_connection
from src.security.request_authorization import PrivateSessionError

if TYPE_CHECKING:
    from src.infra.session_store import CommitPermit, SessionStore

MAX_ATTACHMENTS: int = 5
OWNER_BYTES: int = 100 * 1024 * 1024
TEXT_RESERVE: int = 2 * 1024 * 1024
ATTACHMENT_WRITE_POLICY = WritePolicy(1.0, 1.0, "attachment_storage_busy", "attachment_unavailable")


def initialize_attachment_schema(connection: sqlite3.Connection) -> None:
    version = connection.execute("SELECT version FROM schema_migrations WHERE name='workspace_attachments'").fetchone()
    if version is not None and version[0] != 1:
        raise RuntimeError("Unsupported attachment schema")
    connection.execute("""CREATE TABLE IF NOT EXISTS conversation_attachments (
        id TEXT PRIMARY KEY, storage_id TEXT NOT NULL, owner_user_id TEXT NOT NULL,
        client_upload_id TEXT NOT NULL, name TEXT NOT NULL, suffix TEXT NOT NULL,
        bytes INTEGER NOT NULL, checksum TEXT NOT NULL, state TEXT NOT NULL,
        generation TEXT NOT NULL, text_bytes INTEGER NOT NULL DEFAULT 2097152,
        pages TEXT NOT NULL DEFAULT '[]', error_code TEXT, cleanup INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL, UNIQUE(storage_id,client_upload_id)
    )""")
    columns: set[str] = {row[1] for row in connection.execute("PRAGMA table_info(transcript_exchanges)")}
    if "attachments" not in columns:
        connection.execute("ALTER TABLE transcript_exchanges ADD COLUMN attachments TEXT NOT NULL DEFAULT '[]'")
    connection.execute("INSERT OR IGNORE INTO schema_migrations VALUES('workspace_attachments',1,?)", (time.time(),))


class AttachmentStore:
    def __init__(self, store: "SessionStore") -> None:
        self.store: SessionStore = store
        self.root: Path = Path(store._db_path).resolve().with_suffix(".attachments")

    def _path(self, attachment_id: str, suffix: str) -> Path:
        if canonical_uuid(attachment_id) != attachment_id or suffix not in {"raw", "text"}:
            raise WorkspaceError("document_invalid")
        if self.root.is_symlink():
            raise WorkspaceError("attachment_unavailable", 503)
        path: Path = self.root / f"{attachment_id}.{suffix}"
        if path.is_symlink():
            raise WorkspaceError("attachment_unavailable", 503)
        return path

    def _conversation(self, connection: sqlite3.Connection, public_id: str) -> str:
        owner: str = self.store._owner_id()
        row = connection.execute("SELECT storage_id FROM conversations WHERE owner_user_id=? AND public_id=? AND deleted=0", (owner, public_id)).fetchone()
        if row is None:
            raise PrivateSessionError("private_session_not_found")
        return row[0]

    @staticmethod
    def _public(row: sqlite3.Row) -> dict[str, Any]:
        pages: list[dict[str, int]] = json.loads(row["pages"])
        return {key: row[key] for key in ("id", "client_upload_id", "name", "suffix", "bytes", "checksum", "state", "error_code")} | {
            "page_count": len(pages), "parser_version": "local-text-v1", "cleanup_pending": bool(row["cleanup"]),
        }

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        with storage_connection(self.store._connect, ATTACHMENT_WRITE_POLICY) as connection:
            connection.execute("PRAGMA busy_timeout=0")
            yield connection

    def _remove_files(self, attachment_id: str, suffixes: tuple[str, ...] = ("raw", "text")) -> bool:
        try:
            for suffix in suffixes:
                self._path(attachment_id, suffix).unlink(missing_ok=True)
        except (OSError, WorkspaceError):
            logger.warning("Attachment file cleanup pending; recovery must retry")
            return False
        return True

    def accept(self, public_id: str, upload_id: str, name: str, suffix: str, data: bytes,
               *, can_commit: "CommitPermit | None" = None, admit: Callable[[], None] | None = None) -> tuple[dict[str, Any], bool]:
        from src.infra.document_parser import MAX_DOCUMENT_BYTES

        upload_id = canonical_uuid(upload_id)
        if suffix not in {"txt", "md", "pdf", "docx"}:
            raise WorkspaceError("document_unsupported", 415)
        if not data or len(data) > MAX_DOCUMENT_BYTES:
            raise WorkspaceError("document_too_large", 413)
        if not name or len(name) > 255 or any(ord(char) < 32 or char in '/\\:' for char in name):
            raise WorkspaceError("document_invalid")
        checksum = hashlib.sha256(data).hexdigest()

        def existing(connection: sqlite3.Connection, storage_id: str) -> dict[str, Any] | None:
            row = connection.execute("SELECT * FROM conversation_attachments WHERE storage_id=? AND client_upload_id=?", (storage_id, upload_id)).fetchone()
            if row is None:
                return None
            if row["state"] == "deleted":
                raise WorkspaceError("upload_deleted", 409)
            if (row["checksum"], row["bytes"], row["suffix"]) != (checksum, len(data), suffix):
                raise WorkspaceError("upload_id_conflict", 409)
            return self._public(row)

        # Reconciliation is read-only, including while another reader is active.
        with self._read() as connection:
            connection.row_factory = sqlite3.Row
            found = existing(connection, self._conversation(connection, public_id))
            if found is not None:
                return found, False
        created: str | None = None
        wrote_raw = False
        committed = False
        try:
            with self.store.write_transaction(ATTACHMENT_WRITE_POLICY, can_commit) as transaction:
                connection = transaction.connection
                connection.row_factory = sqlite3.Row
                storage_id = self._conversation(connection, public_id)
                found = existing(connection, storage_id)
                if found is not None:
                    return found, False
                if connection.execute("SELECT COUNT(*) FROM conversation_attachments WHERE storage_id=? AND state!='deleted'", (storage_id,)).fetchone()[0] >= MAX_ATTACHMENTS:
                    raise WorkspaceError("attachment_limit", 413)
                owner = self.store._owner_id()
                used = connection.execute("SELECT COALESCE(SUM(bytes+text_bytes),0) FROM conversation_attachments WHERE owner_user_id=? AND (state!='deleted' OR cleanup=1)", (owner,)).fetchone()[0]
                if used + len(data) + TEXT_RESERVE > OWNER_BYTES:
                    raise WorkspaceError("attachment_quota", 413)
                if admit is not None:
                    admit()
                created = str(uuid.uuid4())
                self.root.mkdir(parents=True, exist_ok=True)
                with self._path(created, "raw").open("xb") as handle:
                    wrote_raw = True
                    handle.write(data)
                connection.execute("INSERT INTO conversation_attachments(id,storage_id,owner_user_id,client_upload_id,name,suffix,bytes,checksum,state,generation,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                                   (created, storage_id, owner, upload_id, name, suffix, len(data), checksum, "parsing", created, time.time()))
                item = self._public(connection.execute("SELECT * FROM conversation_attachments WHERE id=?", (created,)).fetchone())
                transaction.commit()
                committed = True
                return item, True
        except OSError:
            raise WorkspaceError("attachment_unavailable", 503) from None
        finally:
            if created and wrote_raw and not committed:
                self._remove_files(created, ("raw",))

    def listing(self, public_id: str) -> list[dict[str, Any]]:
        self.cleanup()
        with self._read() as connection:
            connection.row_factory = sqlite3.Row
            storage_id: str = self._conversation(connection, public_id)
            return [self._public(row) for row in connection.execute("SELECT * FROM conversation_attachments WHERE storage_id=? AND state!='deleted' ORDER BY created_at,id", (storage_id,))]

    def read(self, public_id: str, attachment_id: str, *, content: bool = False) -> Any:
        with self._read() as connection:
            connection.row_factory = sqlite3.Row
            storage_id: str = self._conversation(connection, public_id)
            row = connection.execute("SELECT * FROM conversation_attachments WHERE storage_id=? AND id=? AND state!='deleted'", (storage_id, attachment_id)).fetchone()
            if row is None:
                raise PrivateSessionError("private_session_not_found")
            try:
                return (self._public(row), self._path(attachment_id, "raw").read_bytes()) if content else self._public(row)
            except OSError:
                raise WorkspaceError("attachment_unavailable", 503) from None

    def parse_input(self, public_id: str, attachment_id: str) -> tuple[str, bytes, str]:
        with self._read() as connection:
            connection.row_factory = sqlite3.Row
            storage_id: str = self._conversation(connection, public_id)
            row = connection.execute("SELECT * FROM conversation_attachments WHERE storage_id=? AND id=? AND state='parsing'", (storage_id, attachment_id)).fetchone()
            if row is None:
                raise PrivateSessionError("private_session_not_found")
            return row["generation"], self._path(attachment_id, "raw").read_bytes(), row["suffix"]

    def finish(self, public_id: str, attachment_id: str, generation: str, document: dict[str, Any] | None, code: str | None = None,
               *, can_commit: "CommitPermit | None" = None) -> None:
        wrote_text = False
        committed = False
        try:
            with self.store.write_transaction(ATTACHMENT_WRITE_POLICY, can_commit) as transaction:
                connection = transaction.connection
                storage_id = self._conversation(connection, public_id)
                row = connection.execute("SELECT 1 FROM conversation_attachments WHERE storage_id=? AND id=? AND generation=? AND state='parsing'", (storage_id, attachment_id, generation)).fetchone()
                if row is None:
                    return
                text = document["text"] if document else ""
                if document:
                    with self._path(attachment_id, "text").open("x", encoding="utf8") as handle:
                        wrote_text = True
                        handle.write(text)
                connection.execute("UPDATE conversation_attachments SET state=?,text_bytes=CASE WHEN ? THEN ? ELSE text_bytes END,pages=?,error_code=?,cleanup=? WHERE id=?",
                                   ("ready" if document else "failed", bool(document), len(text.encode("utf8")), json.dumps(document["pages"] if document else []), code, int(document is None), attachment_id))
                transaction.commit()
                committed = True
        except OSError:
            raise WorkspaceError("attachment_unavailable", 503) from None
        finally:
            if wrote_text and not committed:
                self._remove_files(attachment_id, ("text",))
        if document is None:
            self.cleanup()

    def delete(self, public_id: str, attachment_id: str, *, can_commit: "CommitPermit | None" = None) -> None:
        from src.security.request_authorization import cancel_session_generation

        with self.store.write_transaction(ATTACHMENT_WRITE_POLICY, can_commit) as transaction:
            connection = transaction.connection
            storage_id = self._conversation(connection, public_id)
            if not connection.execute("SELECT 1 FROM conversation_attachments WHERE storage_id=? AND id=? AND state!='deleted'", (storage_id, attachment_id)).fetchone():
                raise PrivateSessionError("private_session_not_found")
            cancel_session_generation(storage_id)
            connection.execute("UPDATE conversation_attachments SET state='deleted',cleanup=1 WHERE id=?", (attachment_id,))
            transaction.commit()
        self.cleanup()

    def snapshot(self, public_id: str, ids: list[str]) -> tuple[AttachmentSnapshot, ...]:
        if len(ids) > MAX_ATTACHMENTS or len(set(ids)) != len(ids):
            raise WorkspaceError("attachment_limit")
        result: list[AttachmentSnapshot] = []
        with storage_connection(self.store._connect, ATTACHMENT_WRITE_POLICY) as connection, connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN")
            storage_id: str = self._conversation(connection, public_id)
            for attachment_id in ids:
                row = connection.execute("SELECT * FROM conversation_attachments WHERE storage_id=? AND id=? AND state='ready'", (storage_id, attachment_id)).fetchone()
                if row is None:
                    raise WorkspaceError("attachment_not_ready", 409)
                result.append(AttachmentSnapshot(attachment_id, row["name"], row["checksum"], self._path(attachment_id, "text").read_text(encoding="utf8"), tuple(json.loads(row["pages"]))))
        return tuple(result)

    def cleanup(self) -> bool:
        """Idempotent maintenance. Pending flags/quota survive any failed cleanup."""
        try:
            with self._read() as connection:
                if not connection.execute("SELECT 1 FROM conversation_attachments WHERE cleanup=1 LIMIT 1").fetchone():
                    return True
            complete = True
            with self.store.write_transaction(ATTACHMENT_WRITE_POLICY, authorize=False) as transaction:
                connection = transaction.connection
                rows = connection.execute("SELECT id FROM conversation_attachments WHERE cleanup=1").fetchall()
                for (attachment_id,) in rows:
                    if not self._remove_files(attachment_id):
                        complete = False
                        continue
                    connection.execute("UPDATE conversation_attachments SET cleanup=0,text_bytes=0 WHERE id=? AND cleanup=1", (attachment_id,))
                transaction.commit()
            return complete
        except WorkspaceError as error:
            logger.warning("Attachment cleanup deferred: code={}", error.code)
            return False

    def interrupt(self, attachment_id: str, generation: str, code: str = "parse_interrupted") -> None:
        """Runtime-only tombstoning, valid after revocation; cannot publish ready."""
        with self._read() as connection:
            pending = connection.execute("SELECT 1 FROM conversation_attachments WHERE id=? AND generation=? AND state='parsing'", (attachment_id, generation)).fetchone()
        if pending is None:
            self.cleanup()
            return
        with self.store.write_transaction(ATTACHMENT_WRITE_POLICY, authorize=False) as transaction:
            transaction.connection.execute("UPDATE conversation_attachments SET state='failed',error_code=?,cleanup=1 WHERE id=? AND generation=? AND state='parsing'", (code, attachment_id, generation))
            transaction.commit()
        self.cleanup()

    def recover(self) -> None:
        """Startup only, before accepting uploads or launching parser tasks."""
        with self.store.write_transaction(ATTACHMENT_WRITE_POLICY, authorize=False) as transaction:
            transaction.connection.execute("UPDATE conversation_attachments SET state='failed',error_code='parse_interrupted',cleanup=1 WHERE state='parsing'")
            transaction.commit()
        self.cleanup()
        if self.root.exists() and not self.root.is_symlink():
            with self._read() as connection:
                known = {row[0] for row in connection.execute("SELECT id FROM conversation_attachments")}
            for path in self.root.iterdir():
                if path.suffix not in {".raw", ".text"} or path.is_symlink() or not path.is_file():
                    continue
                try:
                    attachment_id = canonical_uuid(path.stem)
                except ValueError:
                    continue
                if attachment_id not in known:
                    self._remove_files(attachment_id, (path.suffix[1:],))
