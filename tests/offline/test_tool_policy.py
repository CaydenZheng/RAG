"""Tool allow/deny policy is shared by native and MCP execution paths."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Literal

import pytest
from pydantic import ValidationError


def _tool(
    name: str,
    calls: list[tuple[str, dict[str, Any]]],
    *,
    source: Literal["native", "mcp"] = "native",
) -> object:
    from src.agent.tools import SafetyLevel, ToolDef, ToolParam
    from src.core.agent_runtime import ToolResult

    def execute(params: dict[str, Any]) -> ToolResult:
        calls.append((name, params))
        return ToolResult(success=True, data={"name": name})

    return ToolDef(
        name=name,
        description=f"Test tool {name}",
        params=[ToolParam("value", "str", "Test value")],
        safety_level=(
            SafetyLevel.GRAYLIST
            if source == "mcp"
            else SafetyLevel.WHITELIST
        ),
        execute_fn=execute,
        source=source,
        provider="remote" if source == "mcp" else None,
        max_retries=0,
    )


def test_tool_policy_configuration_defaults_and_validation() -> None:
    from config.settings import Settings

    common = {
        "_env_file": None,
        "OPENAI_API_KEY": "offline-test-key",
        "ADMIN_API_KEY": "offline-admin-key",
    }
    defaults = Settings(**common)
    configured = Settings(
        **common,
        AGENT_TOOL_ALLOWLIST=["search_knowledge_base", "mcp__clock__time"],
        AGENT_TOOL_DENYLIST=["search_web"],
    )

    assert defaults.agent_tool_allowlist == ()
    assert defaults.agent_tool_denylist == ()
    assert configured.agent_tool_allowlist == (
        "search_knowledge_base",
        "mcp__clock__time",
    )
    assert configured.agent_tool_denylist == ("search_web",)

    for field_name in ("AGENT_TOOL_ALLOWLIST", "AGENT_TOOL_DENYLIST"):
        with pytest.raises(ValidationError):
            Settings(**common, **{field_name: ["valid", "  "]})


def test_tool_policy_configuration_parses_json_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from config.settings import Settings

    monkeypatch.setenv(
        "AGENT_TOOL_ALLOWLIST",
        '["calculator","mcp__clock__time"]',
    )
    monkeypatch.setenv("AGENT_TOOL_DENYLIST", '["search_web"]')

    configured = Settings(
        _env_file=None,
        OPENAI_API_KEY="offline-test-key",
        ADMIN_API_KEY="offline-admin-key",
    )

    assert configured.agent_tool_allowlist == (
        "calculator",
        "mcp__clock__time",
    )
    assert configured.agent_tool_denylist == ("search_web",)


def test_denylist_wins_when_a_tool_is_also_allowlisted() -> None:
    from src.agent.tool_policy import ToolAccessPolicy

    policy = ToolAccessPolicy(
        allowlist=("overlap",),
        denylist=("overlap",),
    )

    decision = policy.evaluate("overlap")

    assert decision.allowed is False
    assert decision.reason == "denylist"


def test_native_and_mcp_tools_share_visibility_and_execution_policy(
    isolated_runtime: Path,
) -> None:
    from src.agent.tool_policy import ToolAccessPolicy
    from src.agent.tools import ToolRegistry

    calls: list[tuple[str, dict[str, Any]]] = []
    policy = ToolAccessPolicy(
        allowlist=("native_allowed", "mcp__remote__allowed"),
        denylist=("mcp__remote__denied",),
    )
    registry = ToolRegistry(dedup_window=0, policy=policy)
    for name, source in (
        ("native_allowed", "native"),
        ("native_unlisted", "native"),
        ("mcp__remote__allowed", "mcp"),
        ("mcp__remote__denied", "mcp"),
    ):
        registry.register(_tool(name, calls, source=source))

    descriptions = json.loads(registry.get_tool_descriptions())
    catalog = registry.get_model_tool_catalog()
    native_denied = registry.execute(
        "native_unlisted",
        {"unexpected": "native-secret"},
        "native-session",
    )
    mcp_denied = asyncio.run(
        registry.execute_async(
            "mcp__remote__denied",
            {"value": "mcp-secret"},
            "mcp-session",
        )
    )

    assert [item["name"] for item in descriptions] == [
        "native_allowed",
        "mcp__remote__allowed",
    ]
    assert {
        item["function"]["name"] for item in catalog.tools
    } == {"native_allowed", "mcp__remote__allowed"}
    assert native_denied.error_code == "tool_policy_denied"
    assert mcp_denied.error_code == "tool_policy_denied"
    assert calls == []

    audit_text = (
        isolated_runtime / "logs" / "audit.jsonl"
    ).read_text(encoding="utf-8")
    records = [json.loads(line) for line in audit_text.splitlines()]
    assert [record["tool_name"] for record in records] == [
        "native_unlisted",
        "mcp__remote__denied",
    ]
    assert [record["policy_decision"] for record in records] == [
        "denied",
        "denied",
    ]
    assert [record["policy_reason"] for record in records] == [
        "not_allowlisted",
        "denylist",
    ]
    assert "native-secret" not in audit_text
    assert "mcp-secret" not in audit_text


def test_policy_rejection_bounds_untrusted_parameter_names(
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from config.settings import settings
    from src.agent.tool_policy import ToolAccessPolicy
    from src.agent.tools import (
        MAX_AUDIT_PARAMETER_NAME_BYTES,
        MAX_AUDIT_PARAMETER_NAMES,
        MAX_AUDIT_PARAMETER_NAMES_BYTES,
        ToolRegistry,
    )

    monkeypatch.setattr(settings, "agent_log_max_bytes", 1_024)
    monkeypatch.setattr(settings, "agent_log_backup_count", 0)
    calls: list[tuple[str, dict[str, Any]]] = []
    registry = ToolRegistry(
        dedup_window=0,
        policy=ToolAccessPolicy(denylist=("native_denied",)),
    )
    registry.register(_tool("native_denied", calls))
    params = {
        "超" * 100_000: "secret-value",
        **{f"field-{index:03d}": "not-in-audit" for index in range(80)},
    }

    result = registry.execute("native_denied", params, "bounded-audit")

    assert result.error_code == "tool_policy_denied"
    assert calls == []
    audit_path = isolated_runtime / "logs" / "audit.jsonl"
    assert audit_path.stat().st_size <= settings.agent_log_max_bytes
    audit_text = audit_path.read_text(encoding="utf-8")
    record = json.loads(audit_text)
    names = record["parameter_names"]
    assert len(names) <= MAX_AUDIT_PARAMETER_NAMES
    assert all(
        len(name.encode("utf-8")) <= MAX_AUDIT_PARAMETER_NAME_BYTES
        for name in names
    )
    assert sum(len(name.encode("utf-8")) for name in names) <= (
        MAX_AUDIT_PARAMETER_NAMES_BYTES
    )
    assert record["omitted_parameter_name_count"] > 0
    assert record["truncated_parameter_name_count"] > 0
    assert "not-in-audit" not in audit_text
    assert "secret-value" not in audit_text


def test_configured_policy_audits_allowed_native_and_mcp_results(
    isolated_runtime: Path,
) -> None:
    from src.agent.tool_policy import ToolAccessPolicy
    from src.agent.tools import ToolRegistry

    calls: list[tuple[str, dict[str, Any]]] = []
    registry = ToolRegistry(
        dedup_window=0,
        policy=ToolAccessPolicy(
            allowlist=("native_allowed", "mcp__remote__allowed"),
        ),
    )
    registry.register(_tool("native_allowed", calls))
    registry.register(_tool("mcp__remote__allowed", calls, source="mcp"))

    native_result = registry.execute(
        "native_allowed", {"value": "native-value"}, "native"
    )
    mcp_result = asyncio.run(
        registry.execute_async(
            "mcp__remote__allowed",
            {"value": "mcp-value"},
            "mcp",
        )
    )

    assert native_result.success is True
    assert mcp_result.success is True
    assert [name for name, _ in calls] == [
        "native_allowed",
        "mcp__remote__allowed",
    ]
    audit_text = (
        isolated_runtime / "logs" / "audit.jsonl"
    ).read_text(encoding="utf-8")
    records = [json.loads(line) for line in audit_text.splitlines()]
    assert [record["policy_decision"] for record in records] == [
        "allowed",
        "allowed",
    ]
    assert [record["policy_reason"] for record in records] == [
        "allowlist",
        "allowlist",
    ]
    assert "native-value" not in audit_text
    assert "mcp-value" not in audit_text


def test_configured_policy_audits_allowed_execution_failure(
    isolated_runtime: Path,
) -> None:
    from src.agent.tool_policy import ToolAccessPolicy
    from src.agent.tools import SafetyLevel, ToolDef, ToolRegistry

    def fail(_params: dict[str, Any]) -> object:
        raise RuntimeError("expected failure")

    registry = ToolRegistry(
        dedup_window=0,
        policy=ToolAccessPolicy(allowlist=("failing_native",)),
    )
    registry.register(
        ToolDef(
            name="failing_native",
            description="Always fails",
            params=[],
            safety_level=SafetyLevel.WHITELIST,
            execute_fn=fail,
            max_retries=0,
        )
    )

    result = registry.execute("failing_native", {}, "failure")

    assert result.error_code == "tool_execution_failed"
    record = json.loads(
        (isolated_runtime / "logs" / "audit.jsonl").read_text(
            encoding="utf-8"
        )
    )
    assert record["policy_decision"] == "allowed"
    assert record["policy_reason"] == "allowlist"
    assert record["error_code"] == "tool_execution_failed"


@pytest.mark.parametrize(
    (
        "policy_mode",
        "expected_error_code",
        "expected_decision",
        "expected_reason",
    ),
    [
        ("deny", "tool_policy_denied", "denied", "denylist"),
        ("allow", "tool_blocked", "allowed", "allowlist"),
    ],
)
def test_agent_policy_and_overlapping_hook_outcomes_are_audited(
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
    policy_mode: str,
    expected_error_code: str,
    expected_decision: str,
    expected_reason: str,
) -> None:
    from src.agent.harness import AgentConfig, AgentHarness
    from src.agent.hooks import create_default_pipeline
    from src.agent.memory import MemoryConfig, MemoryManager
    from src.agent.tool_policy import ToolAccessPolicy
    from src.agent.tools import SafetyLevel, ToolDef, ToolRegistry
    from src.core.agent_runtime import AgentEventKind, ToolResult
    from src.infra.session_store import SessionStore

    calls: list[dict[str, Any]] = []
    policy = (
        ToolAccessPolicy(denylist=("execute_code",))
        if policy_mode == "deny"
        else ToolAccessPolicy(allowlist=("execute_code",))
    )
    registry = ToolRegistry(dedup_window=0, policy=policy)
    registry.register(
        ToolDef(
            name="execute_code",
            description="Static-hook overlap probe",
            params=[],
            safety_level=SafetyLevel.WHITELIST,
            execute_fn=lambda params: (
                calls.append(params)
                or ToolResult(success=True)
            ),
        )
    )
    memory = MemoryManager(
        str(isolated_runtime / "memory"),
        config=MemoryConfig(compress_trigger_turns=100),
        store=SessionStore(str(isolated_runtime / "sessions.db")),
    )
    harness = AgentHarness(
        config=AgentConfig(verbose=False),
        memory=memory,
        tools=registry,
        hooks=create_default_pipeline(),
    )
    plans = iter(
        [
            {
                "action": "tool_call",
                "tool_name": "execute_code",
                "tool_params": {},
            },
            {"action": "final_answer", "answer": "blocked safely"},
        ]
    )

    async def plan(
        _messages: object, max_tokens: int | None = None
    ) -> dict[str, object]:
        del max_tokens
        return next(plans)

    monkeypatch.setattr(harness, "_plan_async", plan)

    async def consume() -> list[object]:
        return [
            event
            async for event in harness.events("policy-agent", "run tool")
        ]

    events = asyncio.run(consume())
    response = events[-1].response

    assert events[-1].kind is AgentEventKind.DONE
    assert response is not None
    assert response.tool_calls[0].blocked is True
    assert response.tool_calls[0].error_code == expected_error_code
    assert calls == []
    record = json.loads(
        (isolated_runtime / "logs" / "audit.jsonl").read_text(
            encoding="utf-8"
        )
    )
    assert record["policy_decision"] == expected_decision
    assert record["policy_reason"] == expected_reason
    assert record["error_code"] == expected_error_code


def test_default_registry_uses_settings_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from config.settings import settings
    from src.agent.tools import create_default_registry

    monkeypatch.setattr(
        settings,
        "agent_tool_allowlist",
        ("calculator",),
        raising=False,
    )
    monkeypatch.setattr(
        settings,
        "agent_tool_denylist",
        (),
        raising=False,
    )

    registry = create_default_registry()
    visible = json.loads(registry.get_tool_descriptions())

    assert [item["name"] for item in visible] == ["calculator"]
    denied = registry.execute(
        "search_knowledge_base",
        {"query": "must-not-run"},
        "policy-wiring",
    )
    assert denied.error_code == "tool_policy_denied"
