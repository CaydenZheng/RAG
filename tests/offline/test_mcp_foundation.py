"""MCP configuration and ToolRegistry foundations stay bounded and compatible."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from pydantic import ValidationError

if TYPE_CHECKING:
    from config.settings import Settings


def _settings(**overrides: Any) -> "Settings":
    from config.settings import Settings

    return Settings(
        _env_file=None,
        openai_api_key="test-key",
        admin_api_key="admin-key",
        **overrides,
    )


def test_mcp_configuration_is_disabled_by_default() -> None:
    configured = _settings()

    assert configured.mcp_servers == ()


def test_mcp_configuration_parses_both_transports() -> None:
    configured = _settings(
        mcp_servers=[
            {
                "id": "clock",
                "transport": "stdio",
                "command": "python",
                "args": ["-m", "src.mcp.clock_server"],
            },
            {
                "id": "remote",
                "transport": "streamable_http",
                "url": "https://example.com/mcp",
                "timeout_seconds": 5,
            },
        ]
    )

    assert configured.mcp_servers[0].transport == "stdio"
    assert configured.mcp_servers[1].transport == "streamable_http"


def test_mcp_configuration_parses_json_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from config.settings import Settings

    monkeypatch.setenv(
        "MCP_SERVERS",
        '[{"id":"clock","transport":"stdio","command":"python"}]',
    )
    configured = Settings(
        _env_file=None,
        openai_api_key="test-key",
        admin_api_key="admin-key",
    )

    assert configured.mcp_servers[0].id == "clock"


def test_stdio_environment_secrets_are_redacted_when_serialized() -> None:
    configured = _settings(
        mcp_servers=[
            {
                "id": "secret-server",
                "transport": "stdio",
                "command": "python",
                "env": {"API_TOKEN": "do-not-expose"},
            }
        ]
    )

    server = configured.mcp_servers[0]
    assert server.transport == "stdio"
    assert server.env is not None
    assert server.env["API_TOKEN"].get_secret_value() == "do-not-expose"
    serialized = server.model_dump_json()
    assert "do-not-expose" not in serialized
    assert "**********" in serialized


def test_mcp_configuration_errors_do_not_echo_stdio_secrets() -> None:
    with pytest.raises(ValidationError) as captured:
        _settings(
            mcp_servers=[
                {
                    "id": "secret-server",
                    "transport": "unsupported",
                    "command": "python",
                    "env": {"API_TOKEN": "do-not-expose"},
                }
            ]
        )

    assert "do-not-expose" not in str(captured.value)


@pytest.mark.parametrize(
    "servers",
    [
        [
            {"id": "same", "transport": "stdio", "command": "python"},
            {
                "id": "same",
                "transport": "streamable_http",
                "url": "https://example.com/mcp",
            },
        ],
        [{"id": "clock", "transport": "stdio", "command": "   "}],
        [{"id": "bad", "transport": "websocket", "url": "https://example.com"}],
        [
            {
                "id": "remote",
                "transport": "streamable_http",
                "url": "https://user:secret@example.com/mcp",
            }
        ],
    ],
)
def test_mcp_configuration_rejects_unsafe_or_ambiguous_values(
    servers: list[dict[str, Any]],
) -> None:
    with pytest.raises(ValidationError):
        _settings(mcp_servers=servers)


def test_native_tool_defaults_remain_compatible() -> None:
    from src.agent.tools import SafetyLevel, ToolDef
    from src.core.agent_runtime import ToolResult

    tool = ToolDef(
        name="native",
        description="native test tool",
        params=[],
        safety_level=SafetyLevel.WHITELIST,
        execute_fn=lambda params: ToolResult(success=True),
    )

    assert tool.source == "native"
    assert tool.provider is None
    assert tool.input_schema is None
    assert tool.available is True


def test_json_schema_validation_runs_before_tool_side_effects() -> None:
    from src.agent.tools import SafetyLevel, ToolDef, ToolRegistry
    from src.core.agent_runtime import ToolResult

    calls: list[dict[str, Any]] = []
    schema = {
        "type": "object",
        "properties": {
            "profile": {
                "type": "object",
                "properties": {
                    "tags": {"type": "array", "items": {"type": "string"}}
                },
                "required": ["tags"],
                "additionalProperties": False,
            }
        },
        "required": ["profile"],
        "additionalProperties": False,
    }
    registry = ToolRegistry(dedup_window=0)

    def execute(params: dict[str, Any]) -> ToolResult:
        calls.append(params)
        return ToolResult(success=True)

    tool = ToolDef(
        name="mcp__server__complex",
        description="complex schema",
        params=[],
        safety_level=SafetyLevel.GRAYLIST,
        execute_fn=execute,
        source="mcp",
        provider="server",
        input_schema=schema,
        max_retries=0,
    )
    registry.register(tool)
    invalid = registry.execute(
        tool.name, {"profile": {"tags": [1]}}, session_id="schema"
    )

    assert tool.input_schema == schema
    assert invalid.error_code == "invalid_tool_parameters"
    assert "rule: type" in invalid.error
    assert calls == []


def test_complex_schema_keeps_full_definition_and_builds_planner_view() -> None:
    from src.agent.tools import tool_params_from_schema

    schema = {
        "type": "object",
        "properties": {
            "timezone": {
                "type": "string",
                "description": "IANA timezone",
                "default": "UTC",
            },
            "filters": {
                "oneOf": [
                    {"type": "object"},
                    {"type": "array", "items": {"type": "string"}},
                ]
            },
        },
        "required": ["timezone"],
    }

    params, warnings = tool_params_from_schema(schema)

    assert [(param.name, param.type, param.required) for param in params] == [
        ("timezone", "str", True),
        ("filters", "dict", False),
    ]
    assert any("filters" in warning for warning in warnings)
    assert schema["properties"]["filters"]["oneOf"][1]["type"] == "array"


def test_registry_rejects_duplicate_names_and_oversized_schemas() -> None:
    from src.agent.tool_schema import MAX_TOOL_SCHEMA_BYTES, InvalidToolSchema
    from src.agent.tools import SafetyLevel, ToolDef, ToolRegistry
    from src.core.agent_runtime import ToolResult

    registry = ToolRegistry()

    def make_tool(schema: dict[str, Any] | None = None) -> ToolDef:
        return ToolDef(
            name="duplicate",
            description="test",
            params=[],
            safety_level=SafetyLevel.WHITELIST,
            execute_fn=lambda params: ToolResult(success=True),
            input_schema=schema,
        )

    registry.register(make_tool())
    with pytest.raises(ValueError, match="already registered"):
        registry.register(make_tool())

    oversized = {
        "type": "object",
        "description": "x" * MAX_TOOL_SCHEMA_BYTES,
    }
    with pytest.raises(InvalidToolSchema, match="size limit"):
        ToolRegistry().register(make_tool(oversized))


def test_registry_rejects_schemas_beyond_the_depth_limit() -> None:
    from src.agent.tool_schema import MAX_TOOL_SCHEMA_DEPTH, InvalidToolSchema
    from src.agent.tools import SafetyLevel, ToolDef, ToolRegistry
    from src.core.agent_runtime import ToolResult

    nested: dict[str, Any] = {"type": "string"}
    for _ in range(MAX_TOOL_SCHEMA_DEPTH):
        nested = {
            "type": "object",
            "properties": {"child": nested},
        }
    tool = ToolDef(
        name="deep",
        description="deep schema",
        params=[],
        safety_level=SafetyLevel.WHITELIST,
        execute_fn=lambda params: ToolResult(success=True),
        input_schema=nested,
    )

    with pytest.raises(InvalidToolSchema, match="nesting limit"):
        ToolRegistry().register(tool)


@pytest.mark.parametrize(
    "instance_keyword, instance_value",
    [
        ("default", {"$ref": "literal-user-data"}),
        ("const", {"$ref": "literal-user-data"}),
        ("examples", [{"$ref": "literal-user-data"}]),
        ("enum", [{"$ref": "literal-user-data"}, None]),
    ],
)
def test_literal_ref_in_instance_data_is_not_treated_as_a_schema_reference(
    instance_keyword: str,
    instance_value: object,
) -> None:
    from src.agent.tool_schema import ensure_safe_input_schema

    ensure_safe_input_schema(
        {
            "type": "object",
            instance_keyword: instance_value,
        }
    )


def test_business_property_may_be_named_ref() -> None:
    from src.agent.tool_schema import ensure_safe_input_schema

    ensure_safe_input_schema(
        {
            "type": "object",
            "properties": {
                "$ref": {
                    "type": "string",
                }
            },
            "additionalProperties": False,
        }
    )


def test_valid_local_schema_reference_is_resolved() -> None:
    from src.agent.tools import SafetyLevel, ToolDef, ToolRegistry
    from src.core.agent_runtime import ToolResult

    calls: list[dict[str, Any]] = []
    schema = {
        "type": "object",
        "$defs": {
            "label": {
                "type": "string",
                "minLength": 1,
            }
        },
        "properties": {"label": {"$ref": "#/$defs/label"}},
        "required": ["label"],
        "additionalProperties": False,
    }

    def execute(params: dict[str, Any]) -> ToolResult:
        calls.append(params)
        return ToolResult(success=True)

    registry = ToolRegistry(dedup_window=0)
    registry.register(
        ToolDef(
            name="local-ref",
            description="local reference",
            params=[],
            safety_level=SafetyLevel.WHITELIST,
            execute_fn=execute,
            input_schema=schema,
        )
    )

    valid = registry.execute("local-ref", {"label": "ok"}, "valid-ref")
    invalid = registry.execute("local-ref", {"label": ""}, "invalid-ref")

    assert valid.success is True
    assert invalid.error_code == "invalid_tool_parameters"
    assert calls == [{"label": "ok"}]


def test_unresolved_local_schema_reference_is_rejected_at_registration() -> None:
    from src.agent.tool_schema import InvalidToolSchema
    from src.agent.tools import SafetyLevel, ToolDef, ToolRegistry
    from src.core.agent_runtime import ToolResult

    tool = ToolDef(
        name="missing-ref",
        description="missing reference",
        params=[],
        safety_level=SafetyLevel.WHITELIST,
        execute_fn=lambda params: ToolResult(success=True),
        input_schema={
            "type": "object",
            "properties": {"value": {"$ref": "#/$defs/missing"}},
        },
    )

    with pytest.raises(InvalidToolSchema, match="could not be resolved"):
        ToolRegistry().register(tool)


def test_circular_local_schema_reference_is_rejected_at_registration() -> None:
    from src.agent.tool_schema import InvalidToolSchema
    from src.agent.tools import SafetyLevel, ToolDef, ToolRegistry
    from src.core.agent_runtime import ToolResult

    tool = ToolDef(
        name="circular-ref",
        description="circular reference",
        params=[],
        safety_level=SafetyLevel.WHITELIST,
        execute_fn=lambda params: ToolResult(success=True),
        input_schema={"$ref": "#"},
    )

    with pytest.raises(InvalidToolSchema, match="circular"):
        ToolRegistry().register(tool)


def test_schema_validation_converts_reference_failures_to_stable_errors() -> None:
    from src.agent.tool_schema import validate_tool_arguments

    error = validate_tool_arguments({"$ref": "#"}, {"secret": "do-not-expose"})

    assert error == "Tool parameters could not be validated safely"
    assert "do-not-expose" not in error


def test_unavailable_tool_is_hidden_and_cannot_execute() -> None:
    import json

    from src.agent.tools import SafetyLevel, ToolDef, ToolRegistry
    from src.core.agent_runtime import ToolResult

    calls: list[dict[str, Any]] = []

    def execute(params: dict[str, Any]) -> ToolResult:
        calls.append(params)
        return ToolResult(success=True)

    registry = ToolRegistry()
    registry.register(
        ToolDef(
            name="offline",
            description="offline tool",
            params=[],
            safety_level=SafetyLevel.WHITELIST,
            execute_fn=execute,
            available=False,
        )
    )

    descriptions = json.loads(registry.get_tool_descriptions())
    result = registry.execute("offline", {}, "unavailable")

    assert descriptions == []
    assert result.success is False
    assert result.error_code == "tool_unavailable"
    assert result.error == "Tool is unavailable"
    assert calls == []
