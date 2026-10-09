"""Native model tool-calling compatibility and safety contracts."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError


def test_planner_mode_defaults_to_json_and_accepts_native() -> None:
    from config.settings import Settings

    common = {
        "_env_file": None,
        "openai_api_key": "test-key",
        "admin_api_key": "admin-key",
    }

    assert Settings(**common).agent_planner_mode == "json"
    assert (
        Settings(**common, agent_planner_mode="native").agent_planner_mode == "native"
    )
    with pytest.raises(ValidationError):
        Settings(**common, agent_planner_mode="unsupported")


def test_registry_builds_available_openai_tools_without_mutating_schemas() -> None:
    from src.agent.tools import SafetyLevel, ToolDef, ToolParam, ToolRegistry
    from src.core.agent_runtime import ToolResult

    registry = ToolRegistry()
    complete_schema = {
        "type": "object",
        "$defs": {"filter": {"type": "array", "items": {"type": "string"}}},
        "properties": {"filters": {"$ref": "#/$defs/filter"}},
        "required": ["filters"],
        "additionalProperties": False,
    }
    registry.register(
        ToolDef(
            name="mcp__remote__lookup",
            description="Remote lookup",
            params=[],
            safety_level=SafetyLevel.GRAYLIST,
            execute_fn=lambda params: ToolResult(success=True),
            input_schema=complete_schema,
            source="mcp",
            provider="remote",
        )
    )
    registry.register(
        ToolDef(
            name="native_lookup",
            description="Native lookup",
            params=[
                ToolParam(
                    "query",
                    "str",
                    "Search query",
                    min_length=1,
                    max_length=50,
                ),
                ToolParam(
                    "limit",
                    "int",
                    "Maximum results",
                    required=False,
                    default=5,
                    minimum=1,
                    maximum=10,
                ),
                ToolParam(
                    "options",
                    "dict",
                    "Optional lookup settings",
                    required=False,
                    default={"tags": ["stable"]},
                ),
            ],
            safety_level=SafetyLevel.WHITELIST,
            execute_fn=lambda params: ToolResult(success=True),
        )
    )
    registry.register(
        ToolDef(
            name="offline",
            description="Unavailable tool",
            params=[],
            safety_level=SafetyLevel.WHITELIST,
            execute_fn=lambda params: ToolResult(success=True),
            available=False,
        )
    )

    model_tools = registry.get_model_tools()

    assert model_tools == [
        {
            "type": "function",
            "function": {
                "name": "mcp__remote__lookup",
                "description": "Remote lookup",
                "parameters": complete_schema,
            },
        },
        {
            "type": "function",
            "function": {
                "name": "native_lookup",
                "description": "Native lookup",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search query",
                            "minLength": 1,
                            "maxLength": 50,
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum results",
                            "default": 5,
                            "minimum": 1,
                            "maximum": 10,
                        },
                        "options": {
                            "type": "object",
                            "description": "Optional lookup settings",
                            "default": {"tags": ["stable"]},
                        },
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
        },
    ]
    model_tools[0]["function"]["parameters"]["properties"]["filters"] = {
        "type": "string"
    }
    assert complete_schema["properties"]["filters"] == {"$ref": "#/$defs/filter"}
    model_tools[1]["function"]["parameters"]["properties"]["options"]["default"][
        "tags"
    ].append("mutated")
    assert registry.get_tool("native_lookup").params[2].default == {"tags": ["stable"]}


def test_model_tool_catalog_uses_legal_unique_reversible_names() -> None:
    from src.agent.tools import SafetyLevel, ToolDef, ToolRegistry
    from src.core.agent_runtime import ToolResult

    registry = ToolRegistry()
    registry_names = (
        "native_lookup",
        "mcp__clock__weather.current",
        "mcp__clock__weather_current",
        "mcp__clock__" + "forecast" * 12,
    )
    for name in registry_names:
        registry.register(
            ToolDef(
                name=name,
                description="Lookup",
                params=[],
                safety_level=SafetyLevel.WHITELIST,
                execute_fn=lambda params: ToolResult(success=True),
            )
        )

    catalog = registry.get_model_tool_catalog()
    provider_names = tuple(
        definition["function"]["name"] for definition in catalog.tools
    )

    assert len(set(provider_names)) == len(registry_names)
    assert all(
        len(name) <= 64 and re.fullmatch(r"[A-Za-z0-9_-]+", name)
        for name in provider_names
    )
    assert {
        catalog.registry_name(provider_name) for provider_name in provider_names
    } == set(registry_names)
    assert catalog.registry_name("not-offered") is None
    assert provider_names == tuple(
        definition["function"]["name"]
        for definition in registry.get_model_tool_catalog().tools
    )


def test_model_tool_catalog_retries_an_alias_reserved_by_a_legal_tool() -> None:
    from src.agent.tools import SafetyLevel, ToolDef, ToolRegistry
    from src.core.agent_runtime import ToolResult

    incompatible_name = "mcp__clock__weather.current"
    initial_registry = ToolRegistry()
    initial_registry.register(
        ToolDef(
            name=incompatible_name,
            description="Weather",
            params=[],
            safety_level=SafetyLevel.WHITELIST,
            execute_fn=lambda params: ToolResult(success=True),
        )
    )
    initial_catalog = initial_registry.get_model_tool_catalog()
    occupied_alias = initial_catalog.tools[0]["function"]["name"]

    registry = ToolRegistry()
    for name in (incompatible_name, occupied_alias):
        registry.register(
            ToolDef(
                name=name,
                description="Weather",
                params=[],
                safety_level=SafetyLevel.WHITELIST,
                execute_fn=lambda params: ToolResult(success=True),
            )
        )

    catalog = registry.get_model_tool_catalog()
    provider_name_by_registry_name = {
        catalog.registry_name(definition["function"]["name"]): definition["function"][
            "name"
        ]
        for definition in catalog.tools
    }

    assert provider_name_by_registry_name[occupied_alias] == occupied_alias
    assert provider_name_by_registry_name[incompatible_name] != occupied_alias
    assert len(set(provider_name_by_registry_name.values())) == 2
    assert all(
        re.fullmatch(r"[A-Za-z0-9_-]{1,64}", provider_name)
        for provider_name in provider_name_by_registry_name.values()
    )


def test_llm_client_returns_native_tool_calls_from_openai_compatible_sdk() -> None:
    from src.llm.client import LLMClient, NativeToolCall

    sdk = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=None))
    )
    sdk.chat.completions.create = AsyncMock(
        return_value=SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=None,
                        tool_calls=[
                            SimpleNamespace(
                                id="provider-call-1",
                                type="function",
                                function=SimpleNamespace(
                                    name="lookup",
                                    arguments='{"query":"value"}',
                                ),
                            )
                        ],
                    )
                )
            ],
            usage=None,
        )
    )
    client = object.__new__(LLMClient)
    client._async_chat_client = sdk
    client._chat_model = "test-model"
    tools = [
        {
            "type": "function",
            "function": {
                "name": "lookup",
                "description": "Lookup",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]

    result = asyncio.run(
        client.chat_with_tools_async(
            [{"role": "user", "content": "question"}],
            tools,
            temperature=0.1,
            max_tokens=128,
        )
    )

    assert result.content == ""
    assert result.tool_calls == (
        NativeToolCall(
            call_id="provider-call-1",
            name="lookup",
            arguments='{"query":"value"}',
        ),
    )
    sdk.chat.completions.create.assert_awaited_once_with(
        model="test-model",
        messages=[{"role": "user", "content": "question"}],
        temperature=0.1,
        max_tokens=128,
        tools=tools,
        tool_choice="auto",
    )

    sdk.chat.completions.create.reset_mock()
    sdk.chat.completions.create.return_value = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="plain answer", tool_calls=None)
            )
        ],
        usage=None,
    )
    no_tool_result = asyncio.run(
        client.chat_with_tools_async(
            [{"role": "user", "content": "plain question"}],
            [],
        )
    )

    assert no_tool_result.content == "plain answer"
    assert no_tool_result.tool_calls == ()
    sdk.chat.completions.create.assert_awaited_once_with(
        model="test-model",
        messages=[{"role": "user", "content": "plain question"}],
        temperature=0.3,
    )


def test_llm_text_stream_closes_provider_when_consumer_stops() -> None:
    from src.llm.client import LLMClient

    class ProviderStream:
        def __init__(self) -> None:
            self.closed = False
            self.sent = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.sent:
                await asyncio.Event().wait()
            self.sent = True
            return SimpleNamespace(
                usage=None,
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content="first")
                    )
                ],
            )

        async def close(self) -> None:
            self.closed = True

    provider_stream = ProviderStream()
    sdk = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=AsyncMock(return_value=provider_stream)
            )
        )
    )
    client = object.__new__(LLMClient)
    client._async_chat_client = sdk
    client._chat_model = "test-model"

    async def exercise() -> None:
        output = client.chat_stream_async(
            [{"role": "user", "content": "question"}]
        )
        assert await anext(output) == "first"
        await output.aclose()

    asyncio.run(exercise())

    assert provider_stream.closed is True


def test_llm_text_stream_rejects_mixed_tool_delta_before_content() -> None:
    from src.llm.client import LLMClient

    class ProviderStream:
        def __init__(self) -> None:
            self.closed = False
            self.sent = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.sent:
                raise StopAsyncIteration
            self.sent = True
            return SimpleNamespace(
                usage=None,
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            content="must-not-be-emitted",
                            tool_calls=[SimpleNamespace(id="unexpected")],
                        )
                    )
                ],
            )

        async def close(self) -> None:
            self.closed = True

    provider_stream = ProviderStream()
    sdk = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=AsyncMock(return_value=provider_stream)
            )
        )
    )
    client = object.__new__(LLMClient)
    client._async_chat_client = sdk
    client._chat_model = "test-model"

    async def consume() -> list[str]:
        return [
            chunk
            async for chunk in client.chat_stream_async(
                [{"role": "user", "content": "question"}]
            )
        ]

    with pytest.raises(RuntimeError, match="Unexpected tool call delta"):
        asyncio.run(consume())

    assert provider_stream.closed is True


def _native_harness(
    tmp_path: Path,
    *,
    tool_name: str = "lookup",
) -> tuple[Any, Any, list[dict[str, Any]]]:
    from src.agent.harness import AgentConfig, AgentHarness
    from src.agent.hooks import HookPipeline
    from src.agent.memory import MemoryConfig, MemoryManager
    from src.agent.tools import SafetyLevel, ToolDef, ToolParam, ToolRegistry
    from src.core.agent_runtime import ToolResult
    from src.infra.session_store import SessionStore

    calls: list[dict[str, Any]] = []
    registry = ToolRegistry(dedup_window=0)

    def execute(params: dict[str, Any]) -> ToolResult:
        calls.append(dict(params))
        return ToolResult(success=True, data={"answer": params["query"]})

    registry.register(
        ToolDef(
            name=tool_name,
            description="Lookup a value",
            params=[ToolParam("query", "str", "Value to look up")],
            safety_level=SafetyLevel.WHITELIST,
            execute_fn=execute,
        )
    )
    memory = MemoryManager(
        str(tmp_path / "memory"),
        config=MemoryConfig(compress_trigger_turns=100),
        store=SessionStore(str(tmp_path / "sessions.db")),
    )
    harness = AgentHarness(
        config=AgentConfig(planner_mode="native", verbose=False),
        memory=memory,
        tools=registry,
        hooks=HookPipeline(),
    )
    return harness, memory, calls


def test_native_planner_uses_provider_protocol_and_shared_execution_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.core.agent_runtime import AgentEventKind
    from src.llm import llm_client
    from src.llm.client import NativeChatResponse, NativeToolCall

    harness, memory, calls = _native_harness(tmp_path)
    responses = iter(
        [
            NativeChatResponse(
                content="",
                tool_calls=(
                    NativeToolCall(
                        call_id="provider-call-1",
                        name="lookup",
                        arguments='{"query":"needle"}',
                    ),
                ),
            ),
            NativeChatResponse(content="READY_TO_ANSWER"),
        ]
    )
    requests: list[tuple[list[dict[str, Any]], list[dict[str, Any]]]] = []

    async def chat_with_tools_async(
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        **_kwargs: object,
    ) -> NativeChatResponse:
        requests.append(
            ([dict(message) for message in messages], json.loads(json.dumps(tools)))
        )
        return next(responses)

    monkeypatch.setattr(
        llm_client,
        "chat_with_tools_async",
        chat_with_tools_async,
        raising=False,
    )

    async def chat_stream_async(*_args: object, **_kwargs: object):
        yield "final answer"

    monkeypatch.setattr(llm_client, "chat_stream_async", chat_stream_async)

    async def consume() -> list[Any]:
        return [event async for event in harness.events("native", "question")]

    events = asyncio.run(consume())

    assert events[-1].kind is AgentEventKind.DONE
    assert events[-1].response is not None
    assert events[-1].response.answer == "final answer"
    assert calls == [{"query": "needle"}]
    assert requests[0][1][0]["function"]["name"] == "lookup"
    assert requests[1][0][-2] == {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "provider-call-1",
                "type": "function",
                "function": {
                    "name": "lookup",
                    "arguments": '{"query":"needle"}',
                },
            }
        ],
    }
    assert requests[1][0][-1]["role"] == "tool"
    assert requests[1][0][-1]["tool_call_id"] == "provider-call-1"
    assert "UNTRUSTED_TOOL_RESULT" in requests[1][0][-1]["content"]
    assert [turn.role for turn in memory.load_history("native")] == [
        "user",
        "tool",
        "assistant",
    ]


def test_native_planner_requires_answer_ready_marker_before_streaming(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.llm import llm_client
    from src.llm.client import NativeChatResponse

    harness, memory, calls = _native_harness(tmp_path)
    stream_called = False

    async def chat_with_tools_async(
        *_args: object,
        **_kwargs: object,
    ) -> NativeChatResponse:
        return NativeChatResponse(content="already generated answer")

    async def chat_stream_async(*_args: object, **_kwargs: object):
        nonlocal stream_called
        stream_called = True
        yield "unexpected"

    monkeypatch.setattr(
        llm_client,
        "chat_with_tools_async",
        chat_with_tools_async,
        raising=False,
    )
    monkeypatch.setattr(llm_client, "chat_stream_async", chat_stream_async)

    response = asyncio.run(harness.execute("native-marker", "question"))

    assert response.error_code == "invalid_agent_plan"
    assert response.error == "Agent planner returned an invalid answer decision."
    assert stream_called is False
    assert calls == []
    assert memory.load_history("native-marker") == []


def test_native_answer_chunk_arrives_before_provider_completes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.core.agent_runtime import AgentEventKind
    from src.llm import llm_client
    from src.llm.client import NativeChatResponse

    harness, memory, calls = _native_harness(tmp_path)
    release_provider = asyncio.Event()
    provider_finished = False

    async def chat_with_tools_async(
        *_args: object,
        **_kwargs: object,
    ) -> NativeChatResponse:
        return NativeChatResponse(content="READY_TO_ANSWER")

    async def chat_stream_async(*_args: object, **_kwargs: object):
        nonlocal provider_finished
        yield "native"
        await release_provider.wait()
        yield " answer"
        provider_finished = True

    monkeypatch.setattr(
        llm_client,
        "chat_with_tools_async",
        chat_with_tools_async,
        raising=False,
    )
    monkeypatch.setattr(llm_client, "chat_stream_async", chat_stream_async)

    async def exercise() -> list[Any]:
        events = harness.events("native-stream", "question")
        planning = await anext(events)
        first_chunk = await anext(events)
        assert planning.kind is AgentEventKind.PLANNING
        assert first_chunk.kind is AgentEventKind.CHUNK
        assert first_chunk.chunk == "native"
        assert provider_finished is False
        assert memory.load_history("native-stream") == []
        release_provider.set()
        return [first_chunk, *[event async for event in events]]

    events = asyncio.run(exercise())

    assert provider_finished is True
    assert calls == []
    assert events[-1].kind is AgentEventKind.DONE
    assert events[-1].response is not None
    assert events[-1].response.answer == "native answer"


@pytest.mark.parametrize("planner_mode", ["json", "native"])
def test_agent_modes_fail_closed_on_invalid_final_stream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    planner_mode: str,
) -> None:
    from src.agent.harness import AgentConfig
    from src.llm import llm_client
    from src.llm.client import NativeChatResponse

    harness, memory, _ = _native_harness(tmp_path)
    harness.config = AgentConfig(planner_mode=planner_mode, verbose=False)

    async def chat_async(*_args: object, **_kwargs: object) -> str:
        return json.dumps({"action": "final_answer"})

    async def chat_with_tools_async(
        *_args: object, **_kwargs: object
    ) -> NativeChatResponse:
        return NativeChatResponse(content="READY_TO_ANSWER")

    async def chat_stream_async(*_args: object, **_kwargs: object):
        if False:
            yield ""
        raise RuntimeError("invalid mixed final delta")

    monkeypatch.setattr(llm_client, "chat_async", chat_async)
    monkeypatch.setattr(
        llm_client,
        "chat_with_tools_async",
        chat_with_tools_async,
        raising=False,
    )
    monkeypatch.setattr(llm_client, "chat_stream_async", chat_stream_async)

    response = asyncio.run(
        harness.execute(f"{planner_mode}-invalid-stream", "question")
    )

    assert response.error_code == "agent_final_generation_failed"
    assert response.answer == ""
    assert memory.load_history(f"{planner_mode}-invalid-stream") == []


@pytest.mark.parametrize(
    ("failure_mode", "expected_error"),
    [("empty", "agent_final_generation_failed"), ("timeout", "agent_timeout")],
)
def test_native_final_stream_edges_do_not_persist_partial_answer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
    expected_error: str,
) -> None:
    from src.agent.harness import AgentConfig
    from src.llm import llm_client
    from src.llm.client import NativeChatResponse

    harness, memory, _ = _native_harness(tmp_path)
    harness.config = AgentConfig(
        planner_mode="native",
        verbose=False,
        timeout_seconds=0.05,
    )
    provider_closed = False

    async def chat_with_tools_async(
        *_args: object, **_kwargs: object
    ) -> NativeChatResponse:
        return NativeChatResponse(content="READY_TO_ANSWER")

    async def chat_stream_async(*_args: object, **_kwargs: object):
        nonlocal provider_closed
        try:
            if failure_mode == "empty":
                yield ""
                return
            yield "partial"
            await asyncio.Event().wait()
        finally:
            provider_closed = True

    monkeypatch.setattr(
        llm_client,
        "chat_with_tools_async",
        chat_with_tools_async,
        raising=False,
    )
    monkeypatch.setattr(llm_client, "chat_stream_async", chat_stream_async)

    response = asyncio.run(
        harness.execute(f"native-{failure_mode}", "question")
    )

    assert response.error_code == expected_error
    assert response.answer == ""
    assert provider_closed is True
    assert memory.load_history(f"native-{failure_mode}") == []


def test_native_token_budget_stops_before_planner_or_final_stream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.agent.harness import AgentConfig
    from src.llm import llm_client

    harness, memory, calls = _native_harness(tmp_path)
    harness.config = AgentConfig(
        planner_mode="native",
        verbose=False,
        max_token_budget=20,
    )
    planner_called = False
    stream_called = False

    async def chat_with_tools_async(*_args: object, **_kwargs: object):
        nonlocal planner_called
        planner_called = True
        raise AssertionError("planner must not run after budget exhaustion")

    async def chat_stream_async(*_args: object, **_kwargs: object):
        nonlocal stream_called
        stream_called = True
        yield "unexpected"

    monkeypatch.setattr(
        llm_client,
        "chat_with_tools_async",
        chat_with_tools_async,
        raising=False,
    )
    monkeypatch.setattr(llm_client, "chat_stream_async", chat_stream_async)

    response = asyncio.run(harness.execute("native-budget", "question"))

    assert response.error_code == "agent_token_budget_exceeded"
    assert planner_called is False
    assert stream_called is False
    assert calls == []
    assert memory.load_history("native-budget") == []


def test_final_request_input_is_charged_to_native_run_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.agent.harness import AgentConfig
    from src.llm import llm_client
    from src.llm.client import NativeChatResponse

    session_id = "native-final-input-budget"
    user_message = "question"
    harness, memory, calls = _native_harness(tmp_path)
    initial_messages = memory.build_messages(
        session_id,
        harness._build_planner_prompt(),
        user_message,
    )
    initial_input_cost = len(
        json.dumps(initial_messages, ensure_ascii=False, default=str)
    )
    tool_input_cost = len(
        json.dumps(
            harness.tools.get_model_tools(),
            ensure_ascii=False,
            default=str,
        )
    )
    final_messages = harness._build_final_answer_messages(
        initial_messages,
        step_limit_reached=False,
    )
    final_input_cost = len(
        json.dumps(final_messages, ensure_ascii=False, default=str)
    )
    harness.config = AgentConfig(
        planner_mode="native",
        verbose=False,
        max_token_budget=(
            initial_input_cost
            + tool_input_cost
            + harness.config.planner_max_tokens
            + final_input_cost
            - 1
        ),
    )
    planner_called = False
    stream_called = False

    async def chat_with_tools_async(
        *_args: object, **_kwargs: object
    ) -> NativeChatResponse:
        nonlocal planner_called
        planner_called = True
        return NativeChatResponse(content="READY_TO_ANSWER")

    async def chat_stream_async(*_args: object, **_kwargs: object):
        nonlocal stream_called
        stream_called = True
        yield "must not run"

    monkeypatch.setattr(
        llm_client,
        "chat_with_tools_async",
        chat_with_tools_async,
        raising=False,
    )
    monkeypatch.setattr(llm_client, "chat_stream_async", chat_stream_async)

    response = asyncio.run(harness.execute(session_id, user_message))

    assert planner_called is True
    assert stream_called is False
    assert response.error_code == "agent_token_budget_exceeded"
    assert calls == []
    assert memory.load_history(session_id) == []


def test_native_planner_resolves_provider_name_to_registry_tool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.llm import llm_client
    from src.llm.client import NativeChatResponse, NativeToolCall

    registry_name = "mcp__clock__weather.current"
    harness, _, calls = _native_harness(tmp_path, tool_name=registry_name)
    provider_names: list[str] = []
    requests: list[list[dict[str, Any]]] = []

    async def chat_with_tools_async(
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        **_kwargs: object,
    ) -> NativeChatResponse:
        requests.append([dict(message) for message in messages])
        if not provider_names:
            provider_name = tools[0]["function"]["name"]
            provider_names.append(provider_name)
            return NativeChatResponse(
                content="",
                tool_calls=(
                    NativeToolCall(
                        call_id="provider-call-1",
                        name=provider_name,
                        arguments='{"query":"Shanghai"}',
                    ),
                ),
            )
        return NativeChatResponse(content="READY_TO_ANSWER")

    monkeypatch.setattr(
        llm_client,
        "chat_with_tools_async",
        chat_with_tools_async,
        raising=False,
    )

    async def chat_stream_async(*_args: object, **_kwargs: object):
        yield "round-trip complete"

    monkeypatch.setattr(llm_client, "chat_stream_async", chat_stream_async)

    response = asyncio.run(harness.execute("provider-name", "question"))

    assert response.answer == "round-trip complete"
    assert response.error_code == ""
    assert calls == [{"query": "Shanghai"}]
    assert provider_names[0] != registry_name
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,64}", provider_names[0])
    assert requests[1][-2]["tool_calls"][0]["function"]["name"] == provider_names[0]


def test_json_planner_mode_keeps_the_existing_text_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.agent.harness import AgentConfig
    from src.llm import llm_client

    harness, _, calls = _native_harness(tmp_path)
    harness.config = AgentConfig(planner_mode="json", verbose=False)

    async def chat_async(
        *_args: object,
        **_kwargs: object,
    ) -> str:
        return json.dumps({"action": "final_answer"})

    async def chat_stream_async(*_args: object, **_kwargs: object):
        yield "json answer"

    async def reject_native(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("native planner must not run in json mode")

    monkeypatch.setattr(llm_client, "chat_async", chat_async)
    monkeypatch.setattr(llm_client, "chat_stream_async", chat_stream_async)
    monkeypatch.setattr(
        llm_client,
        "chat_with_tools_async",
        reject_native,
        raising=False,
    )

    response = asyncio.run(harness.execute("json", "question"))

    assert response.answer == "json answer"
    assert response.error_code == ""
    assert calls == []


def test_native_planner_rejects_invalid_argument_json_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.llm import llm_client
    from src.llm.client import NativeChatResponse, NativeToolCall

    harness, memory, calls = _native_harness(tmp_path)

    async def chat_with_tools_async(
        *_args: object,
        **_kwargs: object,
    ) -> NativeChatResponse:
        return NativeChatResponse(
            content="",
            tool_calls=(NativeToolCall("one", "lookup", "not-json"),),
        )

    monkeypatch.setattr(
        llm_client,
        "chat_with_tools_async",
        chat_with_tools_async,
        raising=False,
    )

    response = asyncio.run(harness.execute("invalid-native", "question"))

    assert response.error_code == "invalid_agent_plan"
    assert response.error == "Agent planner returned invalid tool arguments."
    assert calls == []
    assert memory.load_history("invalid-native") == []


def test_native_planner_rejects_unknown_provider_name_without_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.llm import llm_client
    from src.llm.client import NativeChatResponse, NativeToolCall

    harness, memory, calls = _native_harness(tmp_path)

    async def chat_with_tools_async(
        *_args: object,
        **_kwargs: object,
    ) -> NativeChatResponse:
        return NativeChatResponse(
            content="",
            tool_calls=(
                NativeToolCall(
                    call_id="provider-call-1",
                    name="not-offered",
                    arguments='{"query":"value"}',
                ),
            ),
        )

    monkeypatch.setattr(
        llm_client,
        "chat_with_tools_async",
        chat_with_tools_async,
        raising=False,
    )

    response = asyncio.run(harness.execute("unknown-provider-name", "question"))

    assert response.error_code == "invalid_agent_plan"
    assert response.error == "Agent planner returned an invalid tool call."
    assert calls == []
    assert memory.load_history("unknown-provider-name") == []


def test_native_planner_rejects_multiple_calls_without_executing_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.llm import llm_client
    from src.llm.client import NativeChatResponse, NativeToolCall

    harness, memory, calls = _native_harness(tmp_path)

    async def chat_with_tools_async(
        *_args: object,
        **_kwargs: object,
    ) -> NativeChatResponse:
        return NativeChatResponse(
            content="",
            tool_calls=(
                NativeToolCall("one", "lookup", '{"query":"one"}'),
                NativeToolCall("two", "lookup", '{"query":"two"}'),
            ),
        )

    monkeypatch.setattr(
        llm_client,
        "chat_with_tools_async",
        chat_with_tools_async,
        raising=False,
    )

    response = asyncio.run(harness.execute("multiple", "question"))

    assert response.error_code == "invalid_agent_plan"
    assert response.error == "Agent planner returned multiple tool calls."
    assert calls == []
    assert memory.load_history("multiple") == []
