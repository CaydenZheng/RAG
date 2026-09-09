"""Offline pytest fixtures; real-model checks remain standalone scripts."""

import os
import shutil
import socket
from collections.abc import Iterator
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import pytest


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
    for name in list(os.environ):
        if "PROXY" in name.upper() or name.startswith(
            ("OPENAI_", "LLM_", "LANGFUSE_", "OLLAMA_", "HF_")
        ):
            monkeypatch.delenv(name)
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test-key")
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

    from config.settings import Settings, settings

    # Reset the existing object: modules can retain references to the singleton.
    defaults = Settings(_env_file=None)
    for name in type(defaults).model_fields:
        monkeypatch.setattr(settings, name, getattr(defaults, name))

    import sentence_transformers

    def no_weights(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Offline test attempted to load model weights", pytrace=False)

    monkeypatch.setattr(sentence_transformers.SentenceTransformer, "__init__", no_weights)
    monkeypatch.setattr(sentence_transformers.CrossEncoder, "__init__", no_weights)
    yield tmp_path


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
