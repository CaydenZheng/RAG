"""Application lifecycle and public status coverage for MCP providers."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from fastapi import Response
from fastapi.testclient import TestClient
from pydantic import SecretStr

if TYPE_CHECKING:
    from src.agent.tools import ToolRegistry


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
    """Run an app lifespan without starting unrelated model-loading threads."""

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


async def _asgi_get_json(
    application: Any,
    path: str,
) -> tuple[int, dict[str, Any]]:
    """Issue one in-process HTTP GET without managing application lifespan."""
    request_sent = False
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await application(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": [],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
            "root_path": "",
        },
        receive,
        send,
    )
    status = next(
        message["status"]
        for message in messages
        if message["type"] == "http.response.start"
    )
    body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    return status, json.loads(body)


def _registry() -> ToolRegistry:
    from src.agent.tools import ToolRegistry

    return ToolRegistry(dedup_window=0)


def _register_native_probe(registry: ToolRegistry) -> None:
    from src.agent.tools import SafetyLevel, ToolDef
    from src.core.agent_runtime import ToolResult

    registry.register(
        ToolDef(
            name="native_probe",
            description="A native status probe.",
            params=[],
            safety_level=SafetyLevel.WHITELIST,
            execute_fn=lambda params: ToolResult(success=True),
            category="diagnostic",
        )
    )


def test_tool_status_snapshot_is_sorted_and_detached() -> None:
    from src.agent.tools import SafetyLevel, ToolDef
    from src.core.agent_runtime import ToolResult

    registry = _registry()
    registry.register(
        ToolDef(
            name="zeta",
            description="MCP probe",
            params=[],
            safety_level=SafetyLevel.GRAYLIST,
            execute_fn=lambda params: ToolResult(success=True),
            category="mcp",
            source="mcp",
            provider="remote",
            available=False,
        )
    )
    _register_native_probe(registry)

    snapshot = registry.status_snapshot()

    assert [item["name"] for item in snapshot] == ["native_probe", "zeta"]
    assert snapshot[0] == {
        "name": "native_probe",
        "source": "native",
        "provider": None,
        "category": "diagnostic",
        "safety_level": "whitelist",
        "available": True,
    }
    assert snapshot[1] == {
        "name": "zeta",
        "source": "mcp",
        "provider": "remote",
        "category": "mcp",
        "safety_level": "graylist",
        "available": False,
    }
    snapshot[1]["available"] = True
    assert registry.get_tool("zeta").available is False


def test_empty_mcp_configuration_preserves_native_tools() -> None:
    from src.agent.mcp_runtime import MCPRuntime

    registry = _registry()
    _register_native_probe(registry)
    runtime = MCPRuntime(registry)

    async def exercise() -> tuple[dict[str, Any], dict[str, Any]]:
        await runtime.start(())
        payload = dict(runtime.status_payload())
        summary = dict(runtime.readiness_summary())
        await runtime.close()
        return payload, summary

    payload, summary = asyncio.run(exercise())

    assert payload == {
        "tools": [
            {
                "name": "native_probe",
                "source": "native",
                "provider": None,
                "category": "diagnostic",
                "safety_level": "whitelist",
                "available": True,
            }
        ],
        "mcp_servers": [],
    }
    assert summary == {
        "status": "disabled",
        "configured_servers": 0,
        "enabled_servers": 0,
        "available_servers": 0,
        "degraded_servers": 0,
    }


def test_application_starts_reports_and_closes_real_mcp_server(
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from config.settings import MCPStdioServerConfig
    from src.agent import mcp_runtime as mcp_runtime_module
    from src.agent.mcp_runtime import MCPRuntime

    api = _import_app(monkeypatch)
    project_root = Path(__file__).resolve().parents[2]
    launcher = project_root / "tests" / "fixtures" / "mcp_time_server_launcher.py"
    exit_marker = isolated_runtime / "status-time-server-closed"
    secret = "mcp-status-do-not-expose"
    configured = MCPStdioServerConfig(
        id="clock",
        transport="stdio",
        command=sys.executable,
        args=(str(launcher),),
        env={
            "MCP_TEST_EXIT_MARKER": SecretStr(str(exit_marker)),
            "MCP_TEST_TOKEN": SecretStr(secret),
        },
        cwd=str(project_root),
    )
    registry = _registry()
    _register_native_probe(registry)
    runtime = MCPRuntime(registry)
    monkeypatch.setattr(mcp_runtime_module, "mcp_runtime", runtime)
    monkeypatch.setattr(
        mcp_runtime_module.settings,
        "mcp_servers",
        (configured,),
        raising=False,
    )

    with _without_model_warm_up(api), TestClient(api.app) as client:
        deadline = time.monotonic() + 10
        while True:
            response = client.get("/agent/tools")
            if any(
                tool["name"] == "mcp__clock__get_current_time"
                for tool in response.json()["tools"]
            ):
                break
            if time.monotonic() >= deadline:
                pytest.fail("time MCP tool discovery did not complete")
            time.sleep(0.01)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()

    assert payload["tools"] == [
        {
            "name": "mcp__clock__get_current_time",
            "source": "mcp",
            "provider": "clock",
            "category": "mcp",
            "safety_level": "graylist",
            "available": True,
        },
        {
            "name": "native_probe",
            "source": "native",
            "provider": None,
            "category": "diagnostic",
            "safety_level": "whitelist",
            "available": True,
        },
    ]
    assert payload["mcp_servers"] == [
        {
            "id": "clock",
            "transport": "stdio",
            "status": "available",
            "error_code": None,
            "message": "server connection is available",
            "tool_count": 1,
        }
    ]
    assert secret not in response.text
    assert str(launcher) not in response.text
    assert str(project_root) not in response.text

    assert exit_marker.read_text(encoding="utf-8") == "closed"
    tool = registry.get_tool("mcp__clock__get_current_time")
    assert tool is not None
    assert tool.available is False


def test_silent_mcp_handshake_does_not_block_or_outlive_application(
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from config.settings import MCPStdioServerConfig
    from src.agent import mcp_runtime as mcp_runtime_module
    from src.agent.mcp_runtime import MCPRuntime

    api = _import_app(monkeypatch)
    project_root = Path(__file__).resolve().parents[2]
    silent_server = (
        project_root / "tests" / "fixtures" / "mcp_silent_stdio_server.py"
    )
    started_marker = isolated_runtime / "silent-mcp-started"
    exit_marker = isolated_runtime / "silent-mcp-closed"
    configured = MCPStdioServerConfig(
        id="silent",
        transport="stdio",
        command=sys.executable,
        args=(str(silent_server),),
        env={
            "MCP_TEST_STARTED_MARKER": SecretStr(str(started_marker)),
            "MCP_TEST_EXIT_MARKER": SecretStr(str(exit_marker)),
        },
        cwd=str(project_root),
    )
    registry = _registry()
    _register_native_probe(registry)
    runtime = MCPRuntime(registry)
    monkeypatch.setattr(mcp_runtime_module, "mcp_runtime", runtime)
    monkeypatch.setattr(
        mcp_runtime_module.settings,
        "mcp_servers",
        (configured,),
        raising=False,
    )

    async def exercise() -> tuple[int, dict[str, Any], dict[str, Any]]:
        async with asyncio.timeout(10):
            async with api.app.router.lifespan_context(api.app):
                health_status, health_payload = await _asgi_get_json(
                    api.app,
                    "/health",
                )
                deadline = asyncio.get_running_loop().time() + 5
                while not started_marker.exists():
                    if asyncio.get_running_loop().time() >= deadline:
                        pytest.fail("silent MCP subprocess did not start")
                    await asyncio.sleep(0.01)
                tools_status, tools_payload = await _asgi_get_json(
                    api.app,
                    "/agent/tools",
                )
                assert tools_status == 200
                return health_status, health_payload, tools_payload

    with _without_model_warm_up(api):
        health_status, health_payload, tools_payload = asyncio.run(exercise())

    assert health_status == 200
    assert health_payload == {"status": "ok"}
    assert tools_payload["mcp_servers"] == [
        {
            "id": "silent",
            "transport": "stdio",
            "status": "connecting",
            "error_code": None,
            "message": "server connection is starting",
            "tool_count": 0,
        }
    ]
    assert exit_marker.read_text(encoding="utf-8") == "closed"


def test_failed_mcp_server_degrades_without_blocking_application(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from config.settings import MCPStdioServerConfig
    from src.agent import mcp_runtime as mcp_runtime_module
    from src.agent.mcp_runtime import MCPRuntime

    api = _import_app(monkeypatch)
    command_secret = "missing-mcp-command-do-not-expose"
    registry = _registry()
    _register_native_probe(registry)
    runtime = MCPRuntime(registry)
    configured = MCPStdioServerConfig(
        id="broken",
        transport="stdio",
        command=command_secret,
        env={"MCP_TOKEN": SecretStr("mcp-token-do-not-expose")},
    )
    monkeypatch.setattr(mcp_runtime_module, "mcp_runtime", runtime)
    monkeypatch.setattr(
        mcp_runtime_module.settings,
        "mcp_servers",
        (configured,),
        raising=False,
    )
    api.runtime_readiness.reset()
    for component in ("embedding", "index", "bm25", "reranker"):
        api.runtime_readiness.update(component, "ready", message="ready")

    with _without_model_warm_up(api), TestClient(api.app) as client:
        deadline = time.monotonic() + 10
        while True:
            tools_response = client.get("/agent/tools")
            server_status = tools_response.json()["mcp_servers"][0]["status"]
            if server_status == "unavailable":
                break
            if time.monotonic() >= deadline:
                pytest.fail("failed MCP connection did not settle")
            time.sleep(0.01)
        ready_response = client.get("/ready")

    assert tools_response.status_code == 200
    assert tools_response.json() == {
        "tools": [
            {
                "name": "native_probe",
                "source": "native",
                "provider": None,
                "category": "diagnostic",
                "safety_level": "whitelist",
                "available": True,
            }
        ],
        "mcp_servers": [
            {
                "id": "broken",
                "transport": "stdio",
                "status": "unavailable",
                "error_code": "mcp_connect_failed",
                "message": "server connection failed",
                "tool_count": 0,
            }
        ],
    }
    assert command_secret not in tools_response.text
    assert "mcp-token-do-not-expose" not in tools_response.text
    assert ready_response.status_code == 200
    assert ready_response.json()["status"] == "degraded"
    assert ready_response.json()["mcp"] == {
        "status": "degraded",
        "configured_servers": 1,
        "enabled_servers": 1,
        "available_servers": 0,
        "degraded_servers": 1,
    }


def test_readiness_reports_optional_mcp_degradation_without_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.agent import mcp_runtime as mcp_runtime_module

    api = _import_app(monkeypatch)

    class DegradedRuntime:
        def readiness_summary(self) -> dict[str, object]:
            return {
                "status": "degraded",
                "configured_servers": 1,
                "enabled_servers": 1,
                "available_servers": 0,
                "degraded_servers": 1,
            }

    monkeypatch.setattr(
        mcp_runtime_module,
        "mcp_runtime",
        DegradedRuntime(),
    )
    api.runtime_readiness.reset()
    for component in ("embedding", "index", "bm25", "reranker"):
        api.runtime_readiness.update(component, "ready", message="ready")

    response = Response()
    payload = api.readiness(response)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert payload["status"] == "degraded"
    assert payload["mcp"] == {
        "status": "degraded",
        "configured_servers": 1,
        "enabled_servers": 1,
        "available_servers": 0,
        "degraded_servers": 1,
    }
