"""Bounded append-only JSONL files for local operational logs."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

_WRITE_LOCK = threading.Lock()


def append_jsonl(
    path: Path,
    record: dict[str, Any],
    *,
    max_bytes: int,
    backup_count: int,
    retention_seconds: int,
) -> bool:
    """Append one bounded JSON record; return false when it cannot fit."""
    if max_bytes < 1:
        raise ValueError("max_bytes must be at least 1")
    if backup_count < 0:
        raise ValueError("backup_count must not be negative")
    if retention_seconds < 1:
        raise ValueError("retention_seconds must be at least 1")

    encoded = _encode_record(record, max_bytes)
    if encoded is None:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with _WRITE_LOCK:
        _prune_backups(path, backup_count, retention_seconds)
        current_size = path.stat().st_size if path.exists() else 0
        if current_size and current_size + len(encoded) > max_bytes:
            _rotate(path, backup_count)
        with path.open("ab") as stream:
            stream.write(encoded)
    return True


def _encode_record(
    record: dict[str, Any],
    max_bytes: int,
) -> bytes | None:
    """Encode incrementally without allocating an oversized UTF-8 payload."""

    remaining = max_bytes - 1
    if remaining < 0:
        return None
    encoded = bytearray()
    encoder = json.JSONEncoder(ensure_ascii=False)
    for chunk in encoder.iterencode(record):
        if len(chunk) > remaining:
            return None
        chunk_bytes = chunk.encode("utf-8", errors="replace")
        if len(chunk_bytes) > remaining:
            return None
        encoded.extend(chunk_bytes)
        remaining -= len(chunk_bytes)
    encoded.append(0x0A)
    return bytes(encoded)


def _prune_backups(
    path: Path, backup_count: int, retention_seconds: int
) -> None:
    cutoff = time.time() - retention_seconds
    prefix = f"{path.name}."
    for candidate in path.parent.glob(f"{path.name}.*"):
        suffix = candidate.name.removeprefix(prefix)
        if not suffix.isdigit():
            continue
        if int(suffix) > backup_count:
            candidate.unlink(missing_ok=True)
            continue
        try:
            expired = candidate.stat().st_mtime < cutoff
        except FileNotFoundError:
            continue
        if expired:
            candidate.unlink(missing_ok=True)


def _rotate(path: Path, backup_count: int) -> None:
    if backup_count == 0:
        path.unlink(missing_ok=True)
        return

    for index in range(backup_count, 1, -1):
        source = path.with_name(f"{path.name}.{index - 1}")
        target = path.with_name(f"{path.name}.{index}")
        target.unlink(missing_ok=True)
        if source.exists():
            source.replace(target)
    first_backup = path.with_name(f"{path.name}.1")
    first_backup.unlink(missing_ok=True)
    path.replace(first_backup)
