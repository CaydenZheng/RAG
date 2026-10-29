"""Offline pytest fixtures; real-model checks remain standalone scripts."""

import os
import shutil
import socket
import sqlite3
import tempfile
from collections.abc import Iterator
from contextvars import ContextVar
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import url2pathname

import pytest
from offline_environment import clear_host_environment

_IMPORT_ENVIRONMENT: pytest.StashKey[pytest.MonkeyPatch] = pytest.StashKey()


def pytest_sessionstart(session: pytest.Session) -> None:
    """Isolate import-time SQLite singletons before test collection can create them."""
    scratch: Path = Path(__file__).resolve().parents[1] / ".test-tmp"
    scratch.mkdir(exist_ok=True)
    import_runtime: Path = Path(tempfile.mkdtemp(prefix="pytest-import-", dir=scratch))
    patch: pytest.MonkeyPatch = pytest.MonkeyPatch()
    clear_host_environment(patch)
    for name, value in {
        "OPENAI_API_KEY": "offline-collection-key",
        "ADMIN_API_KEY": "offline-collection-admin",
        "OPENAI_BASE_URL": "http://127.0.0.1:9/v1",
        "AUTH_ENABLED": "false",
        "AUTH_ALLOWED_ORIGINS": "[]",
        "MCP_SERVERS": "[]",
        "CHROMA_PERSIST_DIR": str(import_runtime / "data/chroma"),
        "CACHE_DB_PATH": str(import_runtime / "data/cache.db"),
        "AUTH_DB_PATH": str(import_runtime / "data/auth.db"),
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
    }.items():
        patch.setenv(name, value)
    session.config.stash[_IMPORT_ENVIRONMENT] = patch


def pytest_sessionfinish(session: pytest.Session) -> None:
    patch: pytest.MonkeyPatch | None = session.config.stash.get(
        _IMPORT_ENVIRONMENT, None
    )
    if patch is not None:
        patch.undo()


@pytest.fixture(autouse=True)
def offline_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Block connections, including localhost; preserve stdlib socketpair only."""
    inside_socketpair: ContextVar[bool] = ContextVar("inside_socketpair", default=False)
    original_connect = socket.socket.connect

    def connect(sock: socket.socket, address: Any) -> None:
        if inside_socketpair.get():
            return original_connect(sock, address)
        pytest.fail("Offline test attempted a socket connection", pytrace=False)

    def deny(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("Offline test attempted network access", pytrace=False)

    def protect_socketpair(original: Any) -> Any:
        def socketpair(*args: Any, **kwargs: Any) -> Any:
            token = inside_socketpair.set(True)
            try:
                return original(*args, **kwargs)
            finally:
                inside_socketpair.reset(token)

        return socketpair

    monkeypatch.setattr(socket, "socketpair", protect_socketpair(socket.socketpair))
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(socket.socket, "sendto", deny)
    for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr"):
        monkeypatch.setattr(socket, name, deny)
    if hasattr(socket.socket, "sendmsg"):
        monkeypatch.setattr(socket.socket, "sendmsg", deny)


@pytest.fixture(autouse=True)
def isolated_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, offline_network: None
) -> Iterator[Path]:
    """Import settings only after moving away from the real .env and data."""
    project = Path(__file__).resolve().parents[1]
    monkeypatch.chdir(tmp_path)
    clear_host_environment(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test-key")
    monkeypatch.setenv("ADMIN_API_KEY", "offline-admin-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("HF_HOME", str(tmp_path / "huggingface"))
    monkeypatch.setenv("SENTENCE_TRANSFORMERS_HOME", str(tmp_path / "models"))
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(tmp_path / "tiktoken"))
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    monkeypatch.setenv("HF_HUB_DISABLE_TELEMETRY", "1")
    monkeypatch.setenv("CHROMA_PERSIST_DIR", str(tmp_path / "data/chroma"))
    monkeypatch.setenv("CACHE_DB_PATH", str(tmp_path / "data/cache.db"))
    shutil.copytree(project / "prompts", tmp_path / "prompts")

    import config.settings as settings_module
    from config.settings import Settings, settings

    monkeypatch.setattr(settings_module, "PROJECT_ROOT", tmp_path)

    # Reset the existing object: modules can retain references to the singleton.
    defaults = Settings(_env_file=None)
    for name in type(defaults).model_fields:
        monkeypatch.setattr(settings, name, getattr(defaults, name))

    original_sqlite_connect = sqlite3.connect

    def isolated_sqlite_connect(
        database: Any, *args: Any, **kwargs: Any
    ) -> sqlite3.Connection:
        database_name: str = os.fsdecode(database)
        if database_name.lower().startswith("file:"):
            uri = urlsplit(database_name)
            target: Path = Path(url2pathname(uri.path))
            # Assessment readers need local read-only URIs, not arbitrary VFS options.
            if (
                not kwargs.get("uri", args[6] if len(args) > 6 else False)
                or uri.netloc not in ("", "localhost")
                or uri.query != "mode=ro"
                or uri.fragment
                or not target.is_absolute()
            ):
                pytest.fail("Offline SQLite attempted access outside isolated runtime")
            database_name = str(target.resolve())
            database = target.resolve().as_uri() + "?mode=ro"
        if database_name != ":memory:" and not Path(database_name).resolve().is_relative_to(
            tmp_path.resolve()
        ):
            pytest.fail(
                "Offline SQLite attempted access outside isolated runtime", pytrace=True
            )
        return original_sqlite_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", isolated_sqlite_connect)

    import sentence_transformers

    def no_weights(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Offline test attempted to load model weights", pytrace=False)

    monkeypatch.setattr(
        sentence_transformers.SentenceTransformer, "__init__", no_weights
    )
    monkeypatch.setattr(sentence_transformers.CrossEncoder, "__init__", no_weights)
    try:
        yield tmp_path
    finally:
        from src.infra.chroma_clients import close_chroma_clients

        close_chroma_clients()


@pytest.fixture
def fake_llm(monkeypatch: pytest.MonkeyPatch, isolated_runtime: Path) -> Any:
    from offline_fakes import ScriptedLLM

    from src.llm import llm_client

    fake = ScriptedLLM()
    monkeypatch.setattr(llm_client, "chat", fake.chat)
    monkeypatch.setattr(llm_client, "chat_async", fake.chat_async)
    monkeypatch.setattr(llm_client, "chat_stream_async", fake.chat_stream_async)
    return fake


@pytest.fixture
def fixed_embedder(monkeypatch: pytest.MonkeyPatch, isolated_runtime: Path) -> Any:
    from offline_fakes import FixedEmbedder

    from src.llm import llm_client

    fake = FixedEmbedder()
    monkeypatch.setattr(llm_client, "_embedding_model", fake)
    return fake


@pytest.fixture
def temporary_cache(isolated_runtime: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    from src.llm import cache

    store = cache.LLMCache()
    monkeypatch.setattr(cache, "llm_cache", store)
    return store


@pytest.fixture
def temporary_sessions(isolated_runtime: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    from src.infra import session_store

    store = session_store.SessionStore(str(isolated_runtime / "data/sessions.db"))
    monkeypatch.setattr(session_store, "session_store", store)
    return store
