"""Verify the external seams and temporary stores used by future regressions."""

import asyncio
import os
import socket
from pathlib import Path
from typing import Any

import pytest


def test_runtime_uses_temporary_storage(isolated_runtime: Path) -> None:
    from config.settings import settings

    assert settings.openai_api_key == "offline-test-key"
    assert Path.cwd() == isolated_runtime
    assert settings.chroma_path.is_relative_to(isolated_runtime)
    assert settings.cache_db_path_resolved.is_relative_to(isolated_runtime)
    assert not (isolated_runtime / ".env").exists()


@pytest.mark.parametrize("address", [("127.0.0.1", 9), ("192.0.2.1", 443)])
def test_network_is_denied(address: tuple[str, int]) -> None:
    with (
        socket.socket() as connection,
        pytest.raises(pytest.fail.Exception, match="socket connection"),
    ):
        connection.connect(address)


def test_dns_is_denied() -> None:
    with pytest.raises(pytest.fail.Exception, match="network access"):
        socket.getaddrinfo("example.invalid", 443)


def test_weights_are_denied() -> None:
    from sentence_transformers import CrossEncoder, SentenceTransformer

    for constructor in (CrossEncoder, SentenceTransformer):
        with pytest.raises(pytest.fail.Exception, match="model weights"):
            constructor("not-a-real-model")


def test_async_model_substitute_preserves_messages(fake_llm: Any) -> None:
    from src.llm import llm_client

    fake_llm.responses.extend(["first", "second", "third"])
    messages = [{"role": "user", "content": "fixture question"}]
    assert llm_client.chat(messages, max_tokens=8) == "first"

    async def run() -> tuple[str, str]:
        answer = await llm_client.chat_async(messages, max_tokens=16)
        chunks = [
            chunk async for chunk in llm_client.chat_stream_async(messages, max_tokens=32)
        ]
        return answer, "".join(chunks)

    assert asyncio.run(run()) == ("second", "third")
    assert [call["max_tokens"] for call in fake_llm.calls] == [8, 16, 32]
    assert all(call["messages"] == messages for call in fake_llm.calls)
    assert not fake_llm.responses


def test_embedding_adapter_uses_fixed_vectors(fixed_embedder: Any) -> None:
    from src.llm import llm_client

    fixed_embedder.vectors.update({"apple": [1.0, 0.0], "pear": [0.0, 1.0]})
    assert llm_client.embed(["apple", "pear"]) == [[1.0, 0.0], [0.0, 1.0]]
    assert llm_client.embed_single("pear") == [0.0, 1.0]
    assert llm_client.embedding_dim == 2


def test_real_cache_round_trip(temporary_cache: Any) -> None:
    messages = [{"role": "user", "content": "fixture question"}]
    assert temporary_cache.get("fixture-model", messages, 0.0) is None
    temporary_cache.set("fixture-model", messages, 0.0, "fixture answer")
    assert temporary_cache.get("fixture-model", messages, 0.0) == "fixture answer"
    temporary_cache.update_fingerprint("next-fixture-index")
    assert temporary_cache.get("fixture-model", messages, 0.0) is None


def test_real_sessions_are_isolated(temporary_sessions: Any) -> None:
    assert temporary_sessions.list_sessions() == []
    temporary_sessions.add_turn("a", "user", "question")
    temporary_sessions.add_turn("a", "assistant", "answer")
    temporary_sessions.add_turn("b", "user", "another question")
    assert temporary_sessions.get_history("a") == [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "answer"},
    ]
    temporary_sessions.clear("a")
    assert temporary_sessions.history_count("a") == 0
    assert temporary_sessions.history_count("b") == 1


def test_query_rewriter_uses_scripted_llm(fake_llm: Any) -> None:
    from src.core.retrieval import QueryRewriterNode

    fake_llm.responses.append("```yaml\nqueries:\n  - trial duration\n```")
    shared = {"query": "How long is the trial?"}
    asyncio.run(QueryRewriterNode().run_async(shared))
    assert shared["queries"] == ["How long is the trial?", "trial duration"]
    assert len(fake_llm.calls) == 1
    assert "How long is the trial?" in fake_llm.calls[0]["messages"][-1]["content"]


@pytest.mark.parametrize("lowercase", [False, True])
def test_host_configuration_cannot_override_defaults(
    monkeypatch: pytest.MonkeyPatch, lowercase: bool
) -> None:
    from offline_environment import clear_host_environment

    from config.settings import Settings

    # Exercise the real settings schema so new fields cannot silently escape isolation.
    for name, field in Settings.model_fields.items():
        for env_name in {name, field.alias or name}:
            monkeypatch.setenv(
                env_name.lower() if lowercase else env_name.upper(), "invalid-host-value"
            )
    monkeypatch.setenv("OFFLINE_TEST_SYSTEM_SENTINEL", "preserved")
    clear_host_environment(monkeypatch)

    defaults = Settings(_env_file=None, OPENAI_API_KEY="offline-test-key")
    for name, field in Settings.model_fields.items():
        if not field.is_required():
            assert getattr(defaults, name) == field.default, name
    assert os.environ["OFFLINE_TEST_SYSTEM_SENTINEL"] == "preserved"
