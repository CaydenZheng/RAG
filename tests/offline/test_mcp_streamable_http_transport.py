"""The default MCP factory uses the official Streamable HTTP transport."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from src.agent.tools import ToolRegistry
    from src.core.agent_runtime import ToolResult

_NATIVE_CONNECT = socket.socket.connect
_NATIVE_CONNECT_EX = socket.socket.connect_ex
_NATIVE_GETADDRINFO = socket.getaddrinfo


def _import_app(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    import tiktoken

    monkeypatch.setattr(
        tiktoken,
        "get_encoding",
        lambda name: SimpleNamespace(encode=lambda text: list(text)),
    )
    import app

    return app


@contextmanager
def _without_model_warm_up(api: ModuleType) -> Iterator[None]:
    """Run the ASGI lifespan without loading unrelated local models."""

    def skip_model_warm_up() -> None:
        return None

    startup_handlers = api.app.router.on_startup
    warm_up_index = startup_handlers.index(api.warm_up_runtime)
    original_warm_up = startup_handlers[warm_up_index]
    startup_handlers[warm_up_index] = skip_model_warm_up
    try:
        yield
    finally:
        startup_handlers[warm_up_index] = original_warm_up


def _registry() -> ToolRegistry:
    from src.agent.tools import ToolRegistry

    return ToolRegistry(dedup_window=0)


def _allow_loopback_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Restore socket calls only for numeric loopback test addresses."""

    def connect(sock: socket.socket, address: Any) -> None:
        if not isinstance(address, tuple) or address[0] not in {"127.0.0.1", "::1"}:
            pytest.fail("HTTP transport test attempted non-loopback access")
        _NATIVE_CONNECT(sock, address)

    def connect_ex(sock: socket.socket, address: Any) -> int:
        if not isinstance(address, tuple) or address[0] not in {"127.0.0.1", "::1"}:
            pytest.fail("HTTP transport test attempted non-loopback access")
        return _NATIVE_CONNECT_EX(sock, address)

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if host not in {"127.0.0.1", "::1"}:
            pytest.fail("HTTP transport test attempted external DNS resolution")
        return _NATIVE_GETADDRINFO(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,::1")


def _reserve_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@pytest.fixture
def streamable_http_server(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Run a real official SDK server while preserving offline test isolation."""
    _allow_loopback_network(monkeypatch)
    port = _reserve_port()
    project_root = Path(__file__).resolve().parents[2]
    server_script = project_root / "tests" / "fixtures" / "mcp_streamable_http_server.py"
    environment = os.environ.copy()
    environment["MCP_TEST_PORT"] = str(port)
    process = subprocess.Popen(
        [sys.executable, str(server_script)],
        cwd=project_root,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail("Streamable HTTP test server exited during startup")
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                if probe.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.02)
        else:
            pytest.fail("Streamable HTTP test server did not become ready")
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def test_streamable_http_discovers_calls_and_closes_real_server(
    streamable_http_server: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from config.settings import MCPStreamableHTTPServerConfig
    from src.agent.mcp_client import MCPClientManager

    query_secret = "http-query-do-not-log"
    argument_secret = "http-argument-do-not-log"
    caplog.set_level(logging.DEBUG)
    server = MCPStreamableHTTPServerConfig(
        id="http",
        transport="streamable_http",
        url=f"{streamable_http_server}?token={query_secret}",
        timeout_seconds=2,
        read_timeout_seconds=5,
    )
    registry = _registry()
    manager = MCPClientManager([server])

    async def exercise() -> tuple[dict[str, object], ToolResult, dict[str, object]]:
        await manager.start(registry)
        started = dict(manager.snapshot()[0])
        result = await registry.execute_async(
            "mcp__http__echo",
            {"value": argument_secret},
            session_id="http-integration",
        )
        await manager.close()
        return started, result, dict(manager.snapshot()[0])

    started, result, closed = asyncio.run(exercise())

    assert started["status"] == "available"
    assert result.success is True
    assert result.data == {"result": argument_secret}
    assert closed["status"] == "unavailable"
    assert closed["error_code"] == "mcp_client_closed"
    assert query_secret not in caplog.text
    assert argument_secret not in caplog.text
    assert streamable_http_server not in caplog.text
    assert all(
        record.getMessage() == "HTTP transport event"
        for record in caplog.records
        if record.name.startswith("httpcore2.")
    )
    tool = registry.get_tool("mcp__http__echo")
    assert tool is not None
    assert tool.available is False


def test_application_configuration_calls_real_streamable_http_server(
    streamable_http_server: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from config.settings import MCPStreamableHTTPServerConfig
    from src.agent import mcp_runtime as mcp_runtime_module
    from src.agent.mcp_runtime import MCPRuntime

    api = _import_app(monkeypatch)
    registry = _registry()
    runtime = MCPRuntime(registry)
    configured = MCPStreamableHTTPServerConfig(
        id="http-app",
        transport="streamable_http",
        url=streamable_http_server,
        timeout_seconds=2,
        read_timeout_seconds=5,
        call_timeout_seconds=2,
    )
    monkeypatch.setattr(mcp_runtime_module, "mcp_runtime", runtime)
    monkeypatch.setattr(
        mcp_runtime_module.settings,
        "mcp_servers",
        (configured,),
        raising=False,
    )

    async def exercise() -> tuple[ToolResult, dict[str, Any], bool]:
        async with asyncio.timeout(10):
            async with api.app.router.lifespan_context(api.app):
                while True:
                    tool = registry.get_tool("mcp__http-app__echo")
                    if tool is not None and tool.available:
                        break
                    await asyncio.sleep(0.01)
                result = await registry.execute_async(
                    "mcp__http-app__echo",
                    {"value": "application-round-trip"},
                    session_id="http-application-integration",
                )
                payload = dict(runtime.status_payload())
        closed_tool = registry.get_tool("mcp__http-app__echo")
        assert closed_tool is not None
        return result, payload, closed_tool.available

    with _without_model_warm_up(api):
        result, payload, available_after_close = asyncio.run(exercise())

    assert result.success is True
    assert result.data == {"result": "application-round-trip"}
    assert payload["mcp_servers"] == [
        {
            "id": "http-app",
            "transport": "streamable_http",
            "status": "available",
            "error_code": None,
            "message": "server connection is available",
            "tool_count": 2,
        }
    ]
    assert available_after_close is False


def test_streamable_http_initialization_failure_is_isolated(
    streamable_http_server: str,
) -> None:
    from config.settings import MCPStreamableHTTPServerConfig
    from src.agent.mcp_client import MCPClientManager

    invalid_endpoint = streamable_http_server.removesuffix("/mcp") + "/missing"
    manager = MCPClientManager(
        [
            MCPStreamableHTTPServerConfig(
                id="invalid-http",
                transport="streamable_http",
                url=invalid_endpoint,
                timeout_seconds=1,
                read_timeout_seconds=1,
            )
        ]
    )

    async def exercise() -> dict[str, object]:
        await manager.start(_registry())
        snapshot = dict(manager.snapshot()[0])
        await manager.close()
        return snapshot

    snapshot = asyncio.run(exercise())

    assert snapshot["status"] == "unavailable"
    assert snapshot["error_code"] == "mcp_connect_failed"


def test_streamable_http_real_call_timeout_recovers_on_next_response(
    streamable_http_server: str,
) -> None:
    from config.settings import MCPStreamableHTTPServerConfig
    from src.agent.mcp_client import MCPClientManager

    registry = _registry()
    manager = MCPClientManager(
        [
            MCPStreamableHTTPServerConfig(
                id="http-timeout",
                transport="streamable_http",
                url=streamable_http_server,
                timeout_seconds=2,
                read_timeout_seconds=5,
                call_timeout_seconds=0.2,
            )
        ]
    )

    async def exercise() -> tuple[
        ToolResult,
        dict[str, object],
        ToolResult,
        dict[str, object],
    ]:
        await manager.start(registry)
        timeout_result = await registry.execute_async(
            "mcp__http-timeout__wait_for",
            {"delay_seconds": 2},
            session_id="http-real-timeout",
        )
        degraded = dict(manager.snapshot()[0])
        recovered_result = await registry.execute_async(
            "mcp__http-timeout__echo",
            {"value": "recovered"},
            session_id="http-timeout-recovery",
        )
        recovered = dict(manager.snapshot()[0])
        await manager.close()
        return timeout_result, degraded, recovered_result, recovered

    timeout_result, degraded, recovered_result, recovered = asyncio.run(
        exercise()
    )

    assert timeout_result.success is False
    assert timeout_result.error_code == "mcp_call_timeout"
    assert degraded["status"] == "degraded"
    assert degraded["error_code"] == "mcp_call_timeout"
    assert recovered_result.success is True
    assert recovered_result.data == {"result": "recovered"}
    assert recovered["status"] == "available"
    assert recovered["error_code"] is None

def test_streamable_http_unreachable_url_isolated_and_secret_free(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from config.settings import MCPStreamableHTTPServerConfig
    from src.agent.mcp_client import MCPClientManager

    _allow_loopback_network(monkeypatch)
    query_secret = "unreachable-query-do-not-log"
    port = _reserve_port()
    caplog.set_level(logging.DEBUG)
    server = MCPStreamableHTTPServerConfig(
        id="missing-http",
        transport="streamable_http",
        url=f"http://127.0.0.1:{port}/mcp?token={query_secret}",
        timeout_seconds=0.1,
        read_timeout_seconds=0.1,
    )
    manager = MCPClientManager([server])

    async def exercise() -> dict[str, object]:
        await manager.start(_registry())
        snapshot = dict(manager.snapshot()[0])
        await manager.close()
        return snapshot

    snapshot = asyncio.run(exercise())

    assert snapshot["status"] == "unavailable"
    assert snapshot["error_code"] == "mcp_connect_failed"
    assert query_secret not in caplog.text
    assert query_secret not in repr(snapshot)


def test_streamable_http_configures_official_client_timeouts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from config.settings import MCPStreamableHTTPServerConfig
    from src.agent import mcp_client

    captured: dict[str, object] = {}

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

    class FakeMCPClient:
        def __init__(self, transport: object) -> None:
            captured["transport"] = transport

        async def __aenter__(self) -> FakeMCPClient:
            return self

        async def __aexit__(
            self,
            exc_type: object,
            exc_value: object,
            traceback: object,
        ) -> None:
            return None

    def create_transport(
        url: str,
        *,
        http_client: object,
    ) -> object:
        captured["url"] = url
        captured["http_client"] = http_client
        return object()

    def create_http_client(*, timeout: object) -> FakeHTTPClient:
        captured["timeout"] = timeout
        return FakeHTTPClient()

    monkeypatch.setattr(mcp_client.httpx2, "AsyncClient", create_http_client)
    monkeypatch.setattr(mcp_client, "streamable_http_client", create_transport)
    monkeypatch.setattr(mcp_client, "Client", FakeMCPClient)
    server = MCPStreamableHTTPServerConfig(
        id="timeouts",
        transport="streamable_http",
        url="https://mcp.example.test/endpoint",
        timeout_seconds=1.25,
        read_timeout_seconds=8.5,
    )

    async def exercise() -> None:
        async with mcp_client._default_client_factory(server):
            pass

    asyncio.run(exercise())

    timeout = captured["timeout"]
    assert getattr(timeout, "connect") == 1.25
    assert getattr(timeout, "write") == 1.25
    assert getattr(timeout, "pool") == 1.25
    assert getattr(timeout, "read") == 8.5
    assert captured["url"] == "https://mcp.example.test/endpoint"

def test_streamable_http_sdk_logs_redact_untrusted_details(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from src.agent import mcp_client as registered_filters

    session_secret = "http-session-id-do-not-log"
    response_secret = "http-response-body-do-not-log"
    header_secret = "http-response-header-do-not-log"
    assert registered_filters is not None
    caplog.set_level(logging.DEBUG)

    sdk_logger = logging.getLogger("mcp.client.streamable_http")
    sdk_logger.info(f"Received session ID: {session_secret}")
    try:
        raise ValueError(response_secret)
    except ValueError:
        sdk_logger.exception("Error parsing JSON response")
    logging.getLogger("httpcore2.http11").debug(
        f"receive_response_headers.complete x-secret={header_secret}"
    )

    assert session_secret not in caplog.text
    assert response_secret not in caplog.text
    assert header_secret not in caplog.text
    assert "Received StreamableHTTP session ID" in caplog.text
    parse_records = [
        record
        for record in caplog.records
        if record.name == "mcp.client.streamable_http"
        and record.getMessage() == "MCP Streamable HTTP transport event"
    ]
    assert parse_records
    assert all(record.exc_info is None for record in parse_records)
