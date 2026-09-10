"""SQLite-backed single-worker adapter for the IndexJobs interface."""

from __future__ import annotations

import hashlib
import sqlite3
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config.settings import settings
from src.core.index_jobs import (
    IndexCommand,
    IndexJob,
    IndexJobConflictError,
    IndexJobState,
    IndexOperation,
    validate_idempotency_key,
    validate_job_id,
)
from src.infra.uploads import (
    ValidatedUpload,
    delete_document,
    write_document,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class LocalIndexJobs:
    """Persist status and execute document mutations in submission order."""

    def __init__(
        self,
        *,
        db_path: Path | None = None,
        raw_dir: Path | None = None,
        build: Callable[[], dict[str, Any]] | None = None,
        rollback: Callable[[str], dict[str, Any]] | None = None,
    ) -> None:
        self.db_path = (db_path or (settings.data_dir / "index-jobs.db")).resolve()
        self.raw_dir = (raw_dir or settings.raw_dir).resolve()
        self._build = build or self._run_build
        self._rollback = rollback or self._run_rollback
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="index-jobs"
        )
        self._closed = False
        self._init_db()

    def submit(
        self,
        command: IndexCommand,
        *,
        idempotency_key: str | None = None,
    ) -> IndexJob:
        """Persist one job and schedule it once for each idempotency key."""
        request_key = (
            validate_idempotency_key(idempotency_key)
            if idempotency_key is not None
            else f"auto:{uuid.uuid4().hex}"
        )
        digest = command.digest()
        inserted = False
        with self._lock:
            if self._closed:
                raise RuntimeError("index job runner is closed")
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM index_jobs WHERE request_key = ?",
                    (request_key,),
                ).fetchone()
                if row is not None:
                    if row["command_digest"] != digest:
                        raise IndexJobConflictError(
                            "idempotency key already identifies another command"
                        )
                    return self._from_row(row)

                job_id = uuid.uuid4().hex
                submitted_at = _now()
                conn.execute(
                    """
                    INSERT INTO index_jobs (
                        job_id, request_key, command_digest, operation, state,
                        submitted_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        request_key,
                        digest,
                        command.operation.value,
                        IndexJobState.QUEUED.value,
                        submitted_at,
                    ),
                )
                conn.commit()
                inserted = True
                job = self._get(conn, job_id)

        if inserted:
            try:
                self._executor.submit(self._execute, job.job_id, command)
            except Exception:
                self._finish(
                    job.job_id,
                    IndexJobState.FAILED,
                    error_code="index_job_unavailable",
                )
        return self.status(job.job_id) or job

    def status(self, job_id: str) -> IndexJob | None:
        validate_job_id(job_id)
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM index_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            return self._from_row(row) if row is not None else None

    def shutdown(self, *, wait: bool = True) -> None:
        with self._lock:
            self._closed = True
        self._executor.shutdown(wait=wait, cancel_futures=False)

    def _execute(self, job_id: str, command: IndexCommand) -> None:
        self._mark_running(job_id)
        try:
            if command.operation is IndexOperation.UPLOAD:
                upload = ValidatedUpload(
                    command.filename,
                    command.content,
                    hashlib.sha256(command.content).hexdigest(),
                )
                write_document(
                    upload,
                    self.raw_dir,
                    replace=command.replace,
                    idempotent=True,
                )
                info = self._build()
            elif command.operation is IndexOperation.DELETE:
                delete_document(command.filename, self.raw_dir)
                info = self._build()
            elif command.operation is IndexOperation.REBUILD:
                info = self._build()
            else:
                info = self._rollback(command.version_id)
            version_id = str(info.get("version_id") or "") or None
            self._finish(
                job_id,
                IndexJobState.SUCCEEDED,
                index_version=version_id,
            )
        except FileExistsError:
            self._finish(
                job_id,
                IndexJobState.FAILED,
                error_code="document_conflict",
            )
        except ValueError:
            self._finish(
                job_id,
                IndexJobState.FAILED,
                error_code="invalid_index_operation",
            )
        except Exception:
            self._finish(
                job_id,
                IndexJobState.FAILED,
                error_code="index_build_failed",
            )

    def _mark_running(self, job_id: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE index_jobs SET state = ?, started_at = ? WHERE job_id = ?",
                (IndexJobState.RUNNING.value, _now(), job_id),
            )
            conn.commit()

    def _finish(
        self,
        job_id: str,
        state: IndexJobState,
        *,
        index_version: str | None = None,
        error_code: str | None = None,
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE index_jobs
                SET state = ?, finished_at = ?, index_version = ?, error_code = ?
                WHERE job_id = ?
                """,
                (state.value, _now(), index_version, error_code, job_id),
            )
            conn.commit()

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS index_jobs (
                    job_id TEXT PRIMARY KEY,
                    request_key TEXT NOT NULL UNIQUE,
                    command_digest TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    state TEXT NOT NULL,
                    submitted_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    index_version TEXT,
                    error_code TEXT
                )
                """
            )
            conn.execute(
                """
                UPDATE index_jobs
                SET state = ?, finished_at = ?, error_code = ?
                WHERE state IN (?, ?)
                """,
                (
                    IndexJobState.FAILED.value,
                    _now(),
                    "index_job_interrupted",
                    IndexJobState.QUEUED.value,
                    IndexJobState.RUNNING.value,
                ),
            )
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _from_row(row: sqlite3.Row) -> IndexJob:
        return IndexJob(
            job_id=row["job_id"],
            operation=IndexOperation(row["operation"]),
            state=IndexJobState(row["state"]),
            submitted_at=row["submitted_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            index_version=row["index_version"],
            error_code=row["error_code"],
        )

    def _get(self, conn: sqlite3.Connection, job_id: str) -> IndexJob:
        row = conn.execute(
            "SELECT * FROM index_jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("persisted index job disappeared")
        return self._from_row(row)

    @staticmethod
    def _run_build() -> dict[str, Any]:
        from src.orchestration.rag import get_offline_flow

        shared: dict[str, Any] = {}
        get_offline_flow().run(shared)
        return dict(shared.get("index_info") or {})

    @staticmethod
    def _run_rollback(version_id: str) -> dict[str, Any]:
        from src.core.indexing import IndexBuilderNode

        return IndexBuilderNode().rollback(version_id or None)


_index_jobs: LocalIndexJobs | None = None
_index_jobs_path: Path | None = None
_index_jobs_lock = threading.Lock()


def get_index_jobs() -> LocalIndexJobs:
    """Return a runtime adapter scoped to the currently configured data path."""
    global _index_jobs, _index_jobs_path
    configured = (settings.data_dir / "index-jobs.db").resolve()
    with _index_jobs_lock:
        if _index_jobs is None or _index_jobs_path != configured:
            if _index_jobs is not None:
                _index_jobs.shutdown(wait=True)
            _index_jobs = LocalIndexJobs(db_path=configured)
            _index_jobs_path = configured
        return _index_jobs


