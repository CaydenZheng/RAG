"""Process-level Chroma client lifecycle regression tests."""

import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock
from typing import Any


class FakeClient:
    """Minimal close-aware Chroma client double."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class RecordingFactory:
    """Thread-safe client factory used to count real constructions."""

    def __init__(self, *, delay: float = 0.0) -> None:
        self._delay = delay
        self._lock = Lock()
        self.clients: list[FakeClient] = []

    def __call__(self, *, path: str, settings: Any) -> FakeClient:
        time.sleep(self._delay)
        with self._lock:
            client = FakeClient(path)
            self.clients.append(client)
            return client


def test_same_normalized_path_reuses_one_client(tmp_path: Path) -> None:
    from src.infra.chroma_clients import ChromaClientManager

    factory = RecordingFactory()
    manager = ChromaClientManager(client_factory=factory)

    first = manager.get(tmp_path / "nested" / ".." / "index")
    second = manager.get(tmp_path / "index")

    assert first is second
    assert len(factory.clients) == 1
    assert first.path == os.path.normcase(str((tmp_path / "index").resolve()))


def test_concurrent_first_access_constructs_one_client(tmp_path: Path) -> None:
    from src.infra.chroma_clients import ChromaClientManager

    factory = RecordingFactory(delay=0.01)
    manager = ChromaClientManager(client_factory=factory)

    with ThreadPoolExecutor(max_workers=16) as executor:
        clients = list(executor.map(lambda _: manager.get(tmp_path), range(100)))

    assert len({id(client) for client in clients}) == 1
    assert len(factory.clients) == 1


def test_close_all_is_idempotent_and_allows_reopening(tmp_path: Path) -> None:
    from src.infra.chroma_clients import ChromaClientManager

    factory = RecordingFactory()
    manager = ChromaClientManager(client_factory=factory)
    first = manager.get(tmp_path)

    manager.close_all()
    manager.close_all()

    assert first.close_calls == 1

    second = manager.get(tmp_path)

    assert second is not first
    assert len(factory.clients) == 2

    manager.close_all()
    assert second.close_calls == 1
