"""Process-level ownership for persistent Chroma clients."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import chromadb
from loguru import logger

from config.settings import settings

ClientFactory = Callable[..., Any]


class ChromaClientManager:
    """Reuse one client wrapper per normalized persistence path."""

    def __init__(self, *, client_factory: ClientFactory | None = None) -> None:
        self._client_factory = client_factory or chromadb.PersistentClient
        self._clients: dict[str, Any] = {}
        self._lock = threading.RLock()

    def get(self, path: str | Path) -> Any:
        """Return the process-owned client for one persistence path."""
        normalized_path = self._normalize(path)
        with self._lock:
            client = self._clients.get(normalized_path)
            if client is None:
                client = self._client_factory(
                    path=normalized_path,
                    settings=chromadb.config.Settings(anonymized_telemetry=False),
                )
                self._clients[normalized_path] = client
            return client

    def close_all(self) -> None:
        """Close every owned wrapper once and permit later reinitialization."""
        with self._lock:
            clients = tuple(self._clients.values())
            self._clients.clear()
            for client in clients:
                try:
                    client.close()
                except Exception as error:
                    logger.warning(
                        "Failed to close Chroma client: {}", type(error).__name__
                    )

    @staticmethod
    def _normalize(path: str | Path) -> str:
        resolved = Path(path).expanduser().resolve()
        return os.path.normcase(str(resolved))


chroma_clients = ChromaClientManager()


def get_chroma_client(path: str | Path | None = None) -> Any:
    """Return the shared client for the configured persistence path."""
    return chroma_clients.get(settings.chroma_path if path is None else path)


def close_chroma_clients() -> None:
    """Release all process-owned Chroma wrappers."""
    chroma_clients.close_all()
