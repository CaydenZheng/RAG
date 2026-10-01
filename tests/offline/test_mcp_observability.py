"""MCP traces and SDK events remain correlated, bounded, and secret-free."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from mcp import Client
from mcp.server.mcpserver import Context, MCPServer


def _jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_default_transports_enable_bounded_server_logging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from config.settings import (
        MCPStdioServerConfig,
        MCPStreamableHTTPServerConfig,
    )
    from src.agent import mcp_client

    client_options: list[dict[str, object]] = []

    class FakeClient:
        def __init__(self, transport: object, **options: object) -> None:
            del transport
            client_options.append(options)

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(
            self,
            exc_type: object,
            exc_value: object,
            traceback: object,
        ) -> None:
            return None

    class FakeHTTPClient:
        async def __aenter__(self) -> FakeHTTPClient:
            return self

        async def __aexit__(
            self,
            exc_type: object,
            exc_value: object,
            traceback: object,
        ) -> None:
            return None

    async def logging_callback(params: object) -> None:
        del params

    monkeypatch.setattr(mcp_client, "Client", FakeClient)
    monkeypatch.setattr(
        mcp_client,
        "stdio_client",
        lambda parameters, *, errlog: object(),
    )
    monkeypatch.setattr(
        mcp_client.httpx2,
        "AsyncClient",
        lambda **options: FakeHTTPClient(),
    )
    monkeypatch.setattr(
        mcp_client,
        "streamable_http_client",
        lambda url, *, http_client: object(),
    )
    stdio = MCPStdioServerConfig(
        id="stdio-observed",
        transport="stdio",
        command="unused-in-unit-tests",
    )
    http = MCPStreamableHTTPServerConfig(
        id="http-observed",
        transport="streamable_http",
        url="https://mcp.example.test/endpoint",
    )

    async def exercise() -> None:
        async with mcp_client._default_client_factory(
            stdio,
            logging_callback=logging_callback,
        ):
            pass
        async with mcp_client._default_client_factory(
            http,
            logging_callback=logging_callback,
        ):
            pass

    asyncio.run(exercise())

    assert client_options == [
        {
            "logging_callback": logging_callback,
            "log_level": "warning",
        },
        {
            "logging_callback": logging_callback,
            "log_level": "warning",
        },
    ]


def test_real_sdk_progress_and_logs_are_bounded_and_secret_free(
    monkeypatch: pytest.MonkeyPatch,
    isolated_runtime: Path,
) -> None:
    from config.settings import MCPStdioServerConfig
    from src.agent import mcp_client
    from src.agent.mcp_client import MCPClientManager
    from src.agent.mcp_observability import MCPObservability
    from src.agent.tools import ToolRegistry
    from src.infra.tracer import TraceLogger

    secret = "server-payload-secret-must-not-be-recorded"
    server = MCPServer("observability-test")

    @server.tool(name="observe")
    async def observe(ctx: Context) -> dict[str, bool]:
        for index in range(40):
            await ctx.report_progress(index, 40, f"{secret}-{index}")
        for _ in range(25):
            await ctx.log(
                "warning",
                {"secret": secret},
                logger_name=secret,
            )
        return {"observed": True}

    trace_path = isolated_runtime / "traces.jsonl"
    event_path = isolated_runtime / "mcp_events.jsonl"
    local_trace = TraceLogger(trace_path)
    local_observability = MCPObservability(
        trace_logger=local_trace,
        event_path=event_path,
    )
    monkeypatch.setattr(
        mcp_client,
        "mcp_observability",
        local_observability,
    )
    configured = MCPStdioServerConfig(
        id="observed",
        transport="stdio",
        command="unused-in-unit-tests",
    )

    def create_client(config: MCPStdioServerConfig) -> Client:
        return Client(
            server,
            logging_callback=local_observability.logging_callback(config.id),
            log_level="warning",
        )

    registry = ToolRegistry(dedup_window=0)
    manager = MCPClientManager([configured], client_factory=create_client)

    async def exercise() -> object:
        await manager.start(registry)
        trace = local_trace.start_trace(
            "agent-request",
            "/agent/chat",
            trace_id="agent-trace",
        )
        token = local_trace.bind(trace)
        try:
            result = await registry.execute_async(
                "mcp__observed__observe",
                {},
                session_id="observability-session",
            )
            local_trace.finish_trace(trace)
            return result
        finally:
            local_trace.reset(token)
            await manager.close()

    result = asyncio.run(exercise())

    assert result.success is True
    traces = _jsonl(trace_path)
    lifecycle = next(
        record for record in traces if record["operation"] == "mcp_server_start"
    )
    assert [span["name"] for span in lifecycle["spans"]] == [
        "mcp_connect",
        "mcp_discover",
    ]
    request = next(record for record in traces if record["trace_id"] == "agent-trace")
    call = next(span for span in request["spans"] if span["name"] == "mcp_tool_call")
    assert call["status"] == "ok"
    assert call["attributes"] == {
        "invalid_progress_events": 0,
        "last_progress": 31.0,
        "last_total": 40.0,
        "mcp_server_id": "observed",
        "mcp_tool_name": "mcp__observed__observe",
        "mcp_transport": "stdio",
        "progress_events": 32,
        "progress_events_dropped": 8,
        "progress_messages_omitted": 32,
    }

    events = _jsonl(event_path)
    server_logs = [event for event in events if event["event"] == "server_log"]
    notices = [
        event for event in events if event["event"] == "mcp_event_rate_limited"
    ]
    assert len(server_logs) == 20
    assert len(notices) == 1
    assert notices[0]["source_event"] == "server_log"
    assert all(event["payload_omitted"] is True for event in server_logs)
    assert all(event["level"] == "warning" for event in server_logs)
    assert secret not in trace_path.read_text(encoding="utf-8")
    assert secret not in event_path.read_text(encoding="utf-8")


def test_real_sdk_cancellation_interrupts_server_and_records_no_arguments(
    monkeypatch: pytest.MonkeyPatch,
    isolated_runtime: Path,
) -> None:
    from config.settings import MCPStdioServerConfig
    from src.agent import mcp_client
    from src.agent.mcp_client import MCPClientManager
    from src.agent.mcp_observability import MCPObservability
    from src.agent.tools import ToolRegistry
    from src.infra.tracer import TraceLogger

    secret = "cancelled-argument-secret"
    started = asyncio.Event()
    server_stopped = asyncio.Event()
    server = MCPServer("cancellation-test")

    @server.tool(name="wait")
    async def wait_for_cancel(ctx: Context, value: str) -> dict[str, str]:
        del ctx, value
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            server_stopped.set()

    trace_path = isolated_runtime / "cancel-traces.jsonl"
    event_path = isolated_runtime / "cancel-events.jsonl"
    local_trace = TraceLogger(trace_path)
    local_observability = MCPObservability(
        trace_logger=local_trace,
        event_path=event_path,
    )
    monkeypatch.setattr(
        mcp_client,
        "mcp_observability",
        local_observability,
    )
    configured = MCPStdioServerConfig(
        id="cancelled",
        transport="stdio",
        command="unused-in-unit-tests",
    )
    manager = MCPClientManager(
        [configured],
        client_factory=lambda config: Client(server, mode="legacy"),
    )
    registry = ToolRegistry(dedup_window=0)

    async def exercise() -> None:
        await manager.start(registry)
        trace = local_trace.start_trace(
            "cancel-request",
            "/agent/chat",
            trace_id="cancel-trace",
        )
        token = local_trace.bind(trace)
        try:
            call = asyncio.create_task(
                registry.execute_async(
                    "mcp__cancelled__wait",
                    {"value": secret},
                    session_id="cancel-session",
                    call_id="agent-call-cancelled",
                )
            )
            await asyncio.wait_for(started.wait(), timeout=1)
            call.cancel()
            with pytest.raises(asyncio.CancelledError):
                await call
            await asyncio.wait_for(server_stopped.wait(), timeout=1)
            local_trace.finish_trace(trace)
        finally:
            local_trace.reset(token)
            await manager.close()

    asyncio.run(exercise())

    request = next(
        record for record in _jsonl(trace_path) if record["trace_id"] == "cancel-trace"
    )
    call_span = next(
        span for span in request["spans"] if span["name"] == "mcp_tool_call"
    )
    assert call_span["status"] == "cancelled"
    assert call_span["error_code"] == "mcp_call_cancelled"
    assert call_span["attributes"]["call_id"] == "agent-call-cancelled"
    events = _jsonl(event_path)
    assert [event["event"] for event in events] == ["tool_cancelled"]
    assert events[0]["tool_name"] == "mcp__cancelled__wait"
    assert events[0]["call_id"] == "agent-call-cancelled"
    assert secret not in trace_path.read_text(encoding="utf-8")
    assert secret not in event_path.read_text(encoding="utf-8")


def test_long_tool_names_remain_distinct_in_traces_and_cancellation_events(
    isolated_runtime: Path,
) -> None:
    from src.agent.mcp_observability import MCPObservability
    from src.infra.tracer import TraceLogger

    common_prefix = "mcp__remote__" + ("x" * 200)
    tool_names = (f"{common_prefix}alpha", f"{common_prefix}beta")
    assert tool_names[0][:160] == tool_names[1][:160]

    trace_path = isolated_runtime / "long-name-traces.jsonl"
    event_path = isolated_runtime / "long-name-events.jsonl"
    local_trace = TraceLogger(trace_path)
    local_observability = MCPObservability(
        trace_logger=local_trace,
        event_path=event_path,
    )
    trace = local_trace.start_trace(
        "long-name-request",
        "/agent/chat",
        trace_id="long-name-trace",
    )
    token = local_trace.bind(trace)
    try:
        for tool_name in tool_names:
            local_observability.tool_call(
                "remote",
                "stdio",
                tool_name,
            ).finish(status="ok")
        local_trace.finish_trace(trace)
    finally:
        local_trace.reset(token)

    for tool_name in tool_names:
        local_observability.record_cancellation("remote", tool_name)

    trace_tool_names = [
        span["attributes"]["mcp_tool_name"]
        for span in _jsonl(trace_path)[0]["spans"]
    ]
    event_tool_names = [
        event["tool_name"] for event in _jsonl(event_path)
    ]
    assert trace_tool_names == event_tool_names
    assert len(set(trace_tool_names)) == 2
    assert all(len(name) <= 160 for name in trace_tool_names)


def test_surrogate_in_long_tool_name_does_not_block_sdk_call(
    monkeypatch: pytest.MonkeyPatch,
    isolated_runtime: Path,
) -> None:
    from collections.abc import AsyncIterator
    from contextlib import asynccontextmanager
    from typing import cast
    from unittest.mock import ANY, AsyncMock

    from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool

    from config.settings import MCPStdioServerConfig
    from src.agent import mcp_client
    from src.agent.mcp_client import MCPClientManager
    from src.agent.mcp_observability import MCPObservability
    from src.agent.tools import ToolRegistry
    from src.infra.tracer import TraceLogger

    remote_tool_name = ("x" * 200) + "\ud800"
    registered_name = f"mcp__remote__{remote_tool_name}"
    client = AsyncMock(spec=Client)
    client.list_tools.return_value = ListToolsResult(
        tools=[
            Tool(
                name=remote_tool_name,
                inputSchema={"type": "object", "properties": {}},
            )
        ]
    )
    client.call_tool.return_value = CallToolResult(
        content=[TextContent(type="text", text="called")]
    )

    @asynccontextmanager
    async def client_context(
        config: MCPStdioServerConfig,
    ) -> AsyncIterator[Client]:
        del config
        yield cast(Client, client)

    trace_path = isolated_runtime / "surrogate-traces.jsonl"
    local_trace = TraceLogger(trace_path)
    local_observability = MCPObservability(
        trace_logger=local_trace,
        event_path=isolated_runtime / "surrogate-events.jsonl",
    )
    monkeypatch.setattr(mcp_client, "mcp_observability", local_observability)
    manager = MCPClientManager(
        [
            MCPStdioServerConfig(
                id="remote",
                transport="stdio",
                command="unused-in-unit-tests",
            )
        ],
        client_factory=client_context,
    )
    registry = ToolRegistry(dedup_window=0)

    async def exercise() -> object:
        await manager.start(registry)
        trace = local_trace.start_trace(
            "surrogate-request",
            "/agent/chat",
            trace_id="surrogate-trace",
        )
        token = local_trace.bind(trace)
        try:
            result = await registry.execute_async(
                registered_name,
                {},
                session_id="surrogate-session",
            )
            local_trace.finish_trace(trace)
            return result
        finally:
            local_trace.reset(token)
            await manager.close()

    result = asyncio.run(exercise())

    assert result.success is True
    client.call_tool.assert_awaited_once_with(
        remote_tool_name,
        {},
        read_timeout_seconds=30.0,
        progress_callback=ANY,
    )
    request_trace = next(
        record
        for record in _jsonl(trace_path)
        if record["trace_id"] == "surrogate-trace"
    )
    call_span = next(
        span
        for span in request_trace["spans"]
        if span["name"] == "mcp_tool_call"
    )
    assert len(call_span["attributes"]["mcp_tool_name"]) <= 160


def test_trace_span_count_and_rotated_files_are_bounded(
    monkeypatch: pytest.MonkeyPatch,
    isolated_runtime: Path,
) -> None:
    from config.settings import settings
    from src.infra import tracer as tracer_module
    from src.infra.tracer import TraceLogger

    monkeypatch.setattr(tracer_module, "MAX_TRACE_SPANS", 2)
    monkeypatch.setattr(settings, "agent_log_max_bytes", 700)
    monkeypatch.setattr(settings, "agent_log_backup_count", 1)
    path = isolated_runtime / "bounded-traces.jsonl"
    local = TraceLogger(path)

    first = local.start_trace("request-1", "/agent/chat", trace_id="trace-1")
    for index in range(5):
        local.add_span(first, f"span-{index}", index)
    first_record = local.finish_trace(first)
    assert len(first_record["spans"]) == 2
    assert first_record["dropped_spans"] == 3

    for index in range(2, 6):
        trace = local.start_trace(
            f"request-{index}",
            "/agent/chat",
            trace_id=f"trace-{index}",
        )
        local.finish_trace(trace)

    assert path.stat().st_size <= settings.agent_log_max_bytes
    backup = path.with_name("bounded-traces.jsonl.1")
    assert backup.is_file()
    assert backup.stat().st_size <= settings.agent_log_max_bytes
