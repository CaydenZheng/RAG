"""Incomplete provider responses must not reach planners or the exact cache."""

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest


@pytest.mark.parametrize("method", ["chat", "chat_async", "chat_with_tools_async"])
@pytest.mark.parametrize(
    ("finish_reason", "content"),
    [
        ("length", '{"action":"final_answer"}'),
        ("content_filter", "partial answer"),
        (None, "partial answer"),
        ("stop", ""),
        ("stop", "  \n"),
    ],
)
def test_incomplete_completion_is_rejected_without_cache_write(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    finish_reason: str | None,
    content: str,
) -> None:
    from src.llm import cache, llm_client

    response = SimpleNamespace(
        choices=[SimpleNamespace(
            finish_reason=finish_reason,
            message=SimpleNamespace(content=content, tool_calls=None),
        )],
        usage=None,
    )
    cache_write = Mock()
    monkeypatch.setattr(cache.llm_cache, "get", Mock(return_value=None))
    monkeypatch.setattr(cache.llm_cache, "set", cache_write)
    monkeypatch.setattr(
        llm_client._chat_client.chat.completions,
        "create", Mock(return_value=response),
    )
    monkeypatch.setattr(
        llm_client._async_chat_client.chat.completions,
        "create", AsyncMock(return_value=response),
    )
    messages = [{"role": "user", "content": "public test question"}]

    with pytest.raises(RuntimeError, match="complete response"):
        if method == "chat":
            llm_client.chat(messages)
        elif method == "chat_async":
            asyncio.run(llm_client.chat_async(messages))
        else:
            asyncio.run(llm_client.chat_with_tools_async(messages, []))

    cache_write.assert_not_called()


@pytest.mark.parametrize("finish_reason", ["length", "stop", None])
def test_unconfirmed_native_tool_call_never_reaches_agent(
    monkeypatch: pytest.MonkeyPatch, finish_reason: str | None,
) -> None:
    from src.llm import llm_client

    response = SimpleNamespace(
        choices=[SimpleNamespace(
            finish_reason=finish_reason,
            message=SimpleNamespace(content=None, tool_calls=[SimpleNamespace(
                id="fixture-call",
                function=SimpleNamespace(name="calculator", arguments='{"expression":"1+1"}'),
            )]),
        )],
        usage=None,
    )
    monkeypatch.setattr(
        llm_client._async_chat_client.chat.completions,
        "create", AsyncMock(return_value=response),
    )

    with pytest.raises(RuntimeError, match="complete response"):
        asyncio.run(llm_client.chat_with_tools_async(
            [{"role": "user", "content": "public test question"}],
            [{"type": "function", "function": {
                "name": "calculator", "parameters": {"type": "object"},
            }}],
        ))


def test_old_unvalidated_cache_is_bypassed_without_deleting_it(
    monkeypatch: pytest.MonkeyPatch, temporary_cache: Any,
) -> None:
    from src.llm import llm_client

    messages = [{"role": "user", "content": "public test question"}]
    dimensions = llm_client._cache_dimensions(512, "fixture-index")
    legacy_dimensions = {
        key: value for key, value in dimensions.items()
        if key != "generation_parameters"
    }
    temporary_cache.set(
        "fixture-model", messages, 0.3, "unvalidated partial answer",
        **legacy_dimensions,
    )
    create = Mock(return_value=SimpleNamespace(
        choices=[SimpleNamespace(
            finish_reason="stop",
            message=SimpleNamespace(content="complete answer", tool_calls=None),
        )],
        usage=None,
    ))
    monkeypatch.setattr(llm_client._chat_client.chat.completions, "create", create)

    assert llm_client.chat(
        messages, model="fixture-model", max_tokens=512, index_version="fixture-index",
    ) == "complete answer"
    create.assert_called_once()
    assert temporary_cache.get(
        "fixture-model", messages, 0.3, **legacy_dimensions,
    ) == "unvalidated partial answer"
    assert temporary_cache.get(
        "fixture-model", messages, 0.3, **dimensions,
    ) == "complete answer"
