"""Small output budgets must produce visible text, not default hidden thinking."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


@pytest.mark.parametrize("method", ["chat", "chat_async", "native", "stream"])
@pytest.mark.parametrize("base_url", ["https://api.deepseek.com", "https://api.deepseek.com/v1"])
def test_deepseek_small_budget_explicitly_disables_default_thinking(monkeypatch, method, base_url):
    from config.settings import settings
    from src.llm.client import LLMClient

    monkeypatch.setattr(settings, "openai_base_url", base_url)
    client = LLMClient()
    response = SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="visible", tool_calls=None))], usage=None)
    create = Mock(return_value=response)
    async_create = AsyncMock(return_value=response)
    monkeypatch.setattr(client._chat_client.chat.completions, "create", create)
    monkeypatch.setattr(client._async_chat_client.chat.completions, "create", async_create)

    class Stream:
        def __aiter__(self):
            async def chunks():
                yield SimpleNamespace(usage=None, choices=[SimpleNamespace(finish_reason="stop", delta=SimpleNamespace(content="visible", tool_calls=None))])
            return chunks()

        async def close(self):
            pass

    async def call():
        messages = [{"role": "user", "content": "public contract fixture"}]
        if method == "chat_async":
            return await client.chat_async(messages, max_tokens=512, skip_cache=True)
        if method == "native":
            return (await client.chat_with_tools_async(messages, [], max_tokens=512)).content
        async_create.return_value = Stream()
        return "".join([part async for part in client.chat_stream_async(messages, max_tokens=512)])

    if method == "chat":
        assert client.chat([{"role": "user", "content": "public contract fixture"}], max_tokens=512, skip_cache=True) == "visible"
        kwargs = create.call_args.kwargs
    else:
        assert asyncio.run(call()) == "visible"
        kwargs = async_create.call_args.kwargs
    assert kwargs["max_tokens"] == 512
    assert kwargs.get("extra_body") == {"thinking": {"type": "disabled"}}


def test_other_compatible_endpoints_do_not_receive_deepseek_only_options(monkeypatch):
    from config.settings import settings
    from src.llm.client import LLMClient

    monkeypatch.setattr(settings, "openai_base_url", "https://compatible.invalid/v1")
    client = LLMClient()
    create = Mock(return_value=SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="visible", tool_calls=None))], usage=None))
    monkeypatch.setattr(client._chat_client.chat.completions, "create", create)
    assert client.chat([{"role": "user", "content": "fixture"}], max_tokens=512, skip_cache=True) == "visible"
    assert "extra_body" not in create.call_args.kwargs


def test_thinking_policy_partitions_exact_cache_without_deleting_old_results(monkeypatch, temporary_cache):
    from config.settings import settings
    from src.llm.client import LLMClient

    messages = [{"role": "user", "content": "same public question"}]
    monkeypatch.setattr(settings, "openai_base_url", "https://api.deepseek.com")
    deepseek = LLMClient()
    monkeypatch.setattr(settings, "openai_base_url", "https://compatible.invalid/v1")
    other = LLMClient()
    deepseek_dimensions = deepseek._cache_dimensions(512, "fixture-index")
    default_dimensions = other._cache_dimensions(512, "fixture-index")
    temporary_cache.set("same-model", messages, 0.3, "other contract", **default_dimensions)
    assert temporary_cache.get("same-model", messages, 0.3, **deepseek_dimensions) is None
    assert temporary_cache.get("same-model", messages, 0.3, **default_dimensions) == "other contract"


def test_safe_diagnostics_count_reasoning_tokens_without_recording_reasoning(monkeypatch, isolated_runtime):
    from src.infra.tracer import TraceLogger
    from src.llm import client as module

    tracer = TraceLogger(isolated_runtime / "reasoning-counts.jsonl")
    monkeypatch.setattr(module, "tracer", tracer)
    trace = tracer.start_trace("fixture", "agent_planning")
    token = tracer.bind(trace)
    try:
        response = SimpleNamespace(choices=[SimpleNamespace(finish_reason="length", message=SimpleNamespace(content="", reasoning_content="PRIVATE_REASONING"))],
                                   usage=SimpleNamespace(prompt_tokens=72, completion_tokens=512, total_tokens=584, completion_tokens_details=SimpleNamespace(reasoning_tokens=512)))
        module.LLMClient._record_chat_span(time.perf_counter(), "deepseek-v4-flash", cache_hit=False,
                                          response=response, status="error", output_limit=512)
    finally:
        tracer.reset(token)
    assert trace["spans"][-1]["attributes"]["reasoning_tokens"] == 512
    tracer.finish_trace(trace)
    assert "PRIVATE_REASONING" not in tracer.trace_file.read_text(encoding="utf-8")
