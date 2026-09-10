"""Filesystem adapter for immutable index manifests and the active pointer."""

import json
import os
import threading
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from config.settings import settings
from src.core.index_versions import (
    LEGACY_COLLECTION_NAME,
    LEGACY_INDEX_VERSION,
    ActiveIndex,
    IndexVersion,
    validate_index_version_id,
)


class IndexCatalog:
    """Persist version manifests and atomically switch the active pointer."""

    def __init__(self, root: Path | None = None) -> None:
        self._configured_root = root
        self._lock = threading.RLock()
        self._cached_root: Path | None = None
        self._cached_active = ActiveIndex(
            LEGACY_INDEX_VERSION,
            LEGACY_COLLECTION_NAME,
        )

    @property
    def root(self) -> Path:
        if self._configured_root is not None:
            return self._configured_root
        return settings.chroma_path.parent / "index-manifests"

    def capture(self) -> ActiveIndex:
        """Capture one version for a complete retrieval request."""
        with self._lock:
            root = self.root.resolve()
            pointer = root / "active.json"
            try:
                pointer_data = json.loads(pointer.read_text(encoding="utf-8"))
            except FileNotFoundError:
                self._reset_cache(root)
                return self._cached_active

            version_id = validate_index_version_id(
                str(pointer_data["version_id"])
            )
            if (
                root == self._cached_root
                and version_id == self._cached_active.version_id
            ):
                return self._cached_active
            manifest_path = root / "versions" / f"{version_id}.json"
            manifest = IndexVersion.from_dict(
                json.loads(manifest_path.read_text(encoding="utf-8"))
            )
            self._cached_root = root
            self._cached_active = ActiveIndex(
                manifest.version_id,
                manifest.collection_name,
                manifest,
            )
            return self._cached_active

    def publish(self, manifest: IndexVersion) -> ActiveIndex:
        """Persist a complete candidate, then atomically switch the pointer."""
        with self._lock:
            root = self.root.resolve()
            versions = root / "versions"
            versions.mkdir(parents=True, exist_ok=True)
            manifest_path = versions / f"{manifest.version_id}.json"
            if manifest_path.exists():
                persisted = IndexVersion.from_dict(
                    json.loads(manifest_path.read_text(encoding="utf-8"))
                )
                if (
                    persisted.content_checksum != manifest.content_checksum
                    or persisted.chunk_count != manifest.chunk_count
                    or persisted.sources != manifest.sources
                    or persisted.build != manifest.build
                ):
                    raise ValueError("index version manifest is immutable")
                manifest = persisted
            else:
                self._write_json_atomic(manifest_path, manifest.to_dict())
            pointer = root / "active.json"
            self._write_json_atomic(
                pointer,
                {"version_id": manifest.version_id},
            )
            active = ActiveIndex(
                manifest.version_id,
                manifest.collection_name,
                manifest,
            )
            self._cached_root = root
            self._cached_active = active
            return active

    def _reset_cache(self, root: Path) -> None:
        self._cached_root = root
        self._cached_active = ActiveIndex(
            LEGACY_INDEX_VERSION,
            LEGACY_COLLECTION_NAME,
        )

    @staticmethod
    def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(
                    payload,
                    handle,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


index_catalog = IndexCatalog()
