"""Domain interface for asynchronous index mutations and rollback."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Protocol

from src.core.index_versions import validate_index_version_id

_JOB_ID_PATTERN = re.compile(r"[0-9a-f]{32}")
_IDEMPOTENCY_KEY_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,128}")


class IndexOperation(StrEnum):
    """Supported mutations of the local knowledge index."""

    UPLOAD = "upload"
    DELETE = "delete"
    REBUILD = "rebuild"
    ROLLBACK = "rollback"


class IndexJobState(StrEnum):
    """Persistent lifecycle of one submitted job."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True)
class IndexCommand:
    """One validated intent; execution details stay behind IndexJobs."""

    operation: IndexOperation
    filename: str = ""
    content: bytes = b""
    replace: bool = False
    version_id: str = ""

    def __post_init__(self) -> None:
        if self.operation is IndexOperation.UPLOAD:
            if not self.filename or not self.content:
                raise ValueError("upload requires a filename and content")
        elif self.operation is IndexOperation.DELETE:
            if not self.filename:
                raise ValueError("delete requires a filename")
        elif self.operation is IndexOperation.ROLLBACK and self.version_id:
            validate_index_version_id(self.version_id)

    @classmethod
    def upload(
        cls, filename: str, content: bytes, *, replace: bool = False
    ) -> "IndexCommand":
        return cls(IndexOperation.UPLOAD, filename, content, replace)

    @classmethod
    def delete(cls, filename: str) -> "IndexCommand":
        return cls(IndexOperation.DELETE, filename)

    @classmethod
    def rebuild(cls) -> "IndexCommand":
        return cls(IndexOperation.REBUILD)

    @classmethod
    def rollback(cls, version_id: str = "") -> "IndexCommand":
        return cls(IndexOperation.ROLLBACK, version_id=version_id)

    def digest(self) -> str:
        payload = {
            "operation": self.operation,
            "filename": self.filename,
            "content_sha256": hashlib.sha256(self.content).hexdigest(),
            "replace": self.replace,
            "version_id": self.version_id,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class IndexJob:
    """Stable status returned by submit and status."""

    job_id: str
    operation: IndexOperation
    state: IndexJobState
    submitted_at: str
    started_at: str | None = None
    finished_at: str | None = None
    index_version: str | None = None
    error_code: str | None = None

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["operation"] = self.operation.value
        payload["state"] = self.state.value
        return payload


class IndexJobs(Protocol):
    """Deep seam for idempotent background index operations."""

    def submit(
        self,
        command: IndexCommand,
        *,
        idempotency_key: str | None = None,
    ) -> IndexJob: ...

    def status(self, job_id: str) -> IndexJob | None: ...


class IndexJobConflictError(ValueError):
    """An idempotency key was reused for a different command."""


def validate_job_id(value: str) -> str:
    if _JOB_ID_PATTERN.fullmatch(value) is None:
        raise ValueError("invalid index job ID")
    return value


def validate_idempotency_key(value: str) -> str:
    if _IDEMPOTENCY_KEY_PATTERN.fullmatch(value) is None:
        raise ValueError("invalid idempotency key")
    return value


