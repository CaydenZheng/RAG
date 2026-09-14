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
) -> None:
    """Append one JSON record and enforce size and retention limits."""
    if max_bytes < 1:
        raise ValueError("max_bytes must be at least 1")
    if backup_count < 0:
        raise ValueError("backup_count must not be negative")
    if retention_seconds < 1:
        raise ValueError("retention_seconds must be at least 1")

    encoded = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    with _WRITE_LOCK:
        _prune_backups(path, backup_count, retention_seconds)
        current_size = path.stat().st_size if path.exists() else 0
        if current_size and current_size + len(encoded) > max_bytes:
            _rotate(path, backup_count)
        with path.open("ab") as stream:
            stream.write(encoded)


def _prune_backups(
    path: Path, backup_count: int, retention_seconds: int
) -> None:
    cutoff = time.time() - retention_seconds
    prefix = f"{path.name}."
    for candidate in path.parent.glob(f"{path.name}.*"):
        suffix = candidate.name.removeprefix(prefix)
        if not suffix.isdigit():
            continue
        if int(suffix) > backup_count or candidate.stat().st_mtime < cutoff:
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
