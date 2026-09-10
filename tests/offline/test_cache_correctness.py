"""Regression tests for exact-cache semantic partitioning."""

import asyncio


def test_cache_key_covers_identity_prompt_generation_and_index(
    temporary_cache,
) -> None:
    messages = [{"role": "user", "content": "same content"}]
    base = {
        "max_tokens": 128,
        "identity_scope": "client-a",
        "index_version": "1" * 24,
        "prompt_version": "v1",
        "generation_parameters": {"top_p": 0.9},
    }
    temporary_cache.set(
        "model-a", messages, 0.2, "answer-a", **base
    )

    assert temporary_cache.get("model-a", messages, 0.2, **base) == "answer-a"
    variants = [
        {**base, "identity_scope": "client-b"},
        {**base, "index_version": "2" * 24},
        {**base, "prompt_version": "v2"},
        {**base, "max_tokens": 256},
        {**base, "generation_parameters": {"top_p": 0.8}},
    ]
    for dimensions in variants:
        assert temporary_cache.get(
            "model-a", messages, 0.2, **dimensions
        ) is None
    assert temporary_cache.get("model-b", messages, 0.2, **base) is None
    assert temporary_cache.get("model-a", messages, 0.3, **base) is None
    assert temporary_cache.get(
        "model-a",
        [{"role": "user", "content": "changed"}],
        0.2,
        **base,
    ) is None


def test_cache_identity_scope_is_hashed_and_propagates_to_threads(
    isolated_runtime,
) -> None:
    from src.llm.cache_context import (
        current_cache_identity,
        scoped_cache_identity,
    )

    async def read_from_thread() -> str:
        return await asyncio.to_thread(current_cache_identity)

    assert current_cache_identity() == "shared"
    with scoped_cache_identity("client-secret-a"):
        scoped = current_cache_identity()
        assert scoped != "client-secret-a"
        assert len(scoped) == 32
        assert asyncio.run(read_from_thread()) == scoped
    assert current_cache_identity() == "shared"

def test_llm_client_reuses_one_complete_cache_context(
    isolated_runtime,
    monkeypatch,
) -> None:
    from types import SimpleNamespace

    from src.llm import cache, llm_client
    from src.llm.cache_context import scoped_cache_identity

    calls: list[tuple[str, dict]] = []

    class CacheSpy:
        def get(self, *args, **kwargs):
            calls.append(("get", kwargs))
            return None

        def set(self, *args, **kwargs):
            calls.append(("set", kwargs))

    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="provider answer")
            )
        ],
        usage=None,
    )
    monkeypatch.setattr(cache, "llm_cache", CacheSpy())
    monkeypatch.setattr(
        llm_client._chat_client.chat.completions,
        "create",
        lambda **kwargs: response,
    )

    with scoped_cache_identity("client-a"):
        answer = llm_client.chat(
            [{"role": "user", "content": "question"}],
            model="model-a",
            temperature=0.4,
            max_tokens=77,
            index_version="a" * 24,
        )

    assert answer == "provider answer"
    assert [kind for kind, _ in calls] == ["get", "set"]
    assert calls[0][1] == calls[1][1]
    assert calls[0][1]["max_tokens"] == 77
    assert calls[0][1]["index_version"] == "a" * 24
    assert calls[0][1]["prompt_version"] == "v1"
    assert calls[0][1]["identity_scope"] != "client-a"

