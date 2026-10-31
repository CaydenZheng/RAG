"""Owned exports and public tool projections use the real backend seams."""

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from tests.offline.test_account_ownership import accounts as accounts
from tests.offline.test_account_ownership import authorize
from tests.offline.test_conversation_directory import append, create


def test_export_owned_complete_history_with_literal_markdown(accounts: Any) -> None:
    from src.infra.exchange_context import ExchangeContext, exchange_context
    from src.infra.session_store import SessionTurn

    item = create(accounts, "agent")
    with authorize(accounts.service, accounts.logins["alice"]):
        storage = accounts.store.owned_session(accounts.logins["alice"].user.id, item["id"], "agent", create=False)
        token = exchange_context.set(ExchangeContext(str(uuid.uuid4()), "<script>q</script> ```", used_model="synthetic-pro",
            sources=({"ref":1,"label":"https://private.example/auth?token=HIDDEN_TOKEN","params":{"secret":"HIDDEN_PARAMS"}},)))
        try:
            assert accounts.store.append_turns_if_epoch(storage, item["epoch"], [SessionTurn("user","<script>q</script> ```"),SessionTurn("assistant","answer ``` <img src=x> [link](javascript:alert(1))")])
        finally:
            exchange_context.reset(token)
    url = f"/conversations/{item['id']}/export"
    assert accounts.browsers["bob"].get(url).status_code == 404
    result = accounts.browsers["alice"].get(url)
    assert result.status_code == 200
    assert result.headers["cache-control"] == "no-store"
    assert result.headers["content-disposition"] == 'attachment; filename="conversation.md"'
    assert "````text\n<script>" in result.text
    assert "synthetic-pro" in result.text and "引用 [1] 知识库来源" in result.text
    assert all(secret not in result.text for secret in ("HIDDEN_TOKEN","HIDDEN_PARAMS","private.example"))


def test_export_includes_history_beyond_first_page(accounts: Any) -> None:
    item = create(accounts, "rag")
    for index in range(52):
        append(accounts,item,question=f"question-{index}")
    result = accounts.browsers["alice"].get(f"/conversations/{item['id']}/export")
    assert result.status_code == 200
    assert "question-0" in result.text and "question-51" in result.text
    assert result.text.count("### 回答") == 52


def test_export_limit_rejects_before_sending_any_partial_file(accounts: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.infra import session_store

    item = create(accounts,"rag")
    append(accounts,item,question="SYNTHETIC_PRIVATE_QUESTION")
    monkeypatch.setattr(session_store,"MAX_EXPORT_BYTES",80)
    result = accounts.browsers["alice"].get(f"/conversations/{item['id']}/export")
    assert result.status_code == 413 and result.json()["detail"]["code"] == "export_too_large"
    assert "SYNTHETIC_PRIVATE" not in result.text


def test_export_after_clear_and_delete_obeys_lifecycle(accounts: Any) -> None:
    item = create(accounts,"rag")
    append(accounts,item,question="BEFORE_CLEAR")
    browser = accounts.browsers["alice"]
    path = f"/conversations/{item['id']}"
    current = browser.get(path).json()
    cleared = browser.post(path+"/clear",json={"expected_revision":current["revision"]}).json()
    # Clear retains the title, but exports no removed question/answer turns.
    cleared_export = browser.get(path+"/export")
    assert cleared_export.status_code == 200 and "### 回答" not in cleared_export.text
    assert browser.delete(path,headers={"If-Match":str(cleared["revision"])}).status_code == 204
    assert browser.get(path+"/export").status_code == 404


def test_tool_catalog_does_not_expose_mcp_description_or_connection_details() -> None:
    from src.agent.tools import SafetyLevel, ToolDef, ToolRegistry
    from src.core.agent_runtime import ToolResult

    registry = ToolRegistry()
    registry.register(ToolDef("external", "https://provider.example/auth?token=HIDDEN", [], SafetyLevel.WHITELIST,
                              lambda _: ToolResult(True), source="mcp", provider="synthetic",available=False))
    item = registry.status_snapshot()[0]
    assert item["available"] is False and item["source"] == "mcp"
    assert "HIDDEN" not in item["description"] and "https:" not in item["description"]


def test_successful_calculator_projects_only_numeric_summary() -> None:
    from src.agent.tools import _create_calculator_tool
    from src.core.agent_runtime import AgentEvent, AgentEventKind

    result = _create_calculator_tool().execute_fn({"expression":"(12 + 21) * 12"})
    event = AgentEvent(AgentEventKind.TOOL_RESULT,tool_result=result).to_dict()
    assert event["summary"] == "计算结果：396"
    assert "expression" not in event and "data" not in event


@pytest.mark.parametrize("success", [False, True])
def test_unprojected_mcp_result_never_becomes_public_summary(success: bool) -> None:
    from src.core.agent_runtime import AgentEvent, AgentEventKind, ToolResult

    event = AgentEvent(AgentEventKind.TOOL_RESULT,tool_result=ToolResult(success,data={"token":"HIDDEN"})).to_dict()
    assert event["summary"] is None and "HIDDEN" not in str(event)


@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_opt_in_mcp_examples_use_negotiated_elicitation_protocol(mode: str) -> None:
    from mcp import Client
    from mcp.client.session import ClientRequestContext
    from mcp.types import ElicitRequestParams, ElicitResult

    from scripts.mcp_workspace_demo import server

    modes: list[str] = []

    async def respond(context: ClientRequestContext, params: ElicitRequestParams) -> ElicitResult:
        del context
        modes.append(params.mode)
        if params.mode == "form":
            return ElicitResult(action="accept", content={"topic": "synthetic"})
        return ElicitResult(action="decline")

    async def exercise() -> None:
        async with asyncio.timeout(5):
            async with Client(server, mode=mode, elicitation_callback=respond) as client:
                listing = await client.list_tools()
                assert {tool.name for tool in listing.tools} == {
                    "confirm_preview", "request_form", "request_url",
                }
                for name, status in (
                    ("confirm_preview", "preview_confirmed"),
                    ("request_form", "accept"),
                    ("request_url", "decline"),
                ):
                    result = await client.call_tool(name, {})
                    assert not result.is_error
                    assert result.structured_content == {"status": status}
        assert modes == ["form", "url"]

    asyncio.run(exercise())


@pytest.mark.parametrize(("name", "blocked"), [
    ("form", False), ("confirm", False),
    ("mcp__workspace_demo__request_form", False),
    ("rm", True), ("mcp__remote__rm", True), ("shell-rm", True),
    ("execute_code", True), ("delete_file", True),
])
def test_default_hook_rm_boundary_preserves_actual_command_blocks(name: str, blocked: bool) -> None:
    from src.agent.hooks import HookContext, HookEvent, create_default_pipeline

    result = create_default_pipeline().fire(HookContext(
        HookEvent.PRE_TOOL_USE, "synthetic-hook-session", data={"tool_name": name},
    ))
    assert result.blocked is blocked


def test_form_example_reaches_registry_approval_and_elicitation_through_default_hooks(
    monkeypatch: pytest.MonkeyPatch, isolated_runtime: Path, fake_llm: Any,
) -> None:
    from mcp import Client

    import src.agent.harness as harness_module
    from config.settings import MCPStdioServerConfig
    from scripts.mcp_workspace_demo import server
    from src.agent.harness import AgentConfig, AgentHarness
    from src.agent.mcp_client import MCPClientManager
    from src.agent.memory import MemoryConfig, MemoryManager
    from src.agent.tool_approval import ToolApprovalManager
    from src.agent.tool_policy import ToolAccessPolicy
    from src.agent.tools import ToolRegistry
    from src.infra.session_store import SessionStore

    name = "mcp__workspace_demo__request_form"
    approvals = ToolApprovalManager(timeout_seconds=5)
    registry = ToolRegistry(approval_manager=approvals, policy=ToolAccessPolicy(allowlist=(name,)))
    config = MCPStdioServerConfig(id="workspace_demo", transport="stdio", command="unused-in-process")
    manager: MCPClientManager

    def factory(config: MCPStdioServerConfig) -> Client:
        return Client(server, elicitation_callback=manager.elicitation_callback(config.id))

    manager = MCPClientManager([config], client_factory=factory)
    harness = AgentHarness(
        config=AgentConfig(verbose=False), tools=registry,
        memory=MemoryManager(str(isolated_runtime / "memory"),
            config=MemoryConfig(compress_trigger_turns=100),
            store=SessionStore(str(isolated_runtime / "sessions.db"))),
    )
    plans = iter([harness_module._ToolCallPlan(name=name, params={}), harness_module._FinalAnswerPlan()])

    async def plan(_messages: object, max_tokens: int | None = None) -> object:
        del max_tokens
        return next(plans)

    monkeypatch.setattr(harness, "_plan_async", plan)
    fake_llm.responses.append("synthetic completed")

    async def exercise() -> None:
        await approvals.open()
        await manager.start(registry)
        task = asyncio.create_task(harness.execute("form-hook-session", "synthetic form"))
        try:
            async with asyncio.timeout(10):
                for _ in range(100):
                    pending = await approvals.pending_for_session("form-hook-session")
                    if pending or task.done():
                        break
                    await asyncio.sleep(.005)
                assert pending, "Default hooks must let the form tool reach Registry approval"
                assert pending[0]["tool_name"] == name
                assert not await manager.pending_elicitations("form-hook-session")
                await approvals.respond("form-hook-session", pending[0]["id"], action="approve")
                for _ in range(100):
                    forms = await manager.pending_elicitations("form-hook-session")
                    if forms or task.done():
                        break
                    await asyncio.sleep(.005)
                assert forms and forms[0]["mode"] == "form"
                await manager.respond_to_elicitation("form-hook-session", forms[0]["id"],
                    action="accept", content={"topic": "synthetic"})
                response = await task
                assert not response.error_code
                assert response.answer == "synthetic completed"
                assert len(response.tool_calls) == 1 and response.tool_calls[0].success
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await manager.close()
            await approvals.close()

    asyncio.run(exercise())
    audits = [json.loads(line) for line in (isolated_runtime / "logs/audit.jsonl").read_text(encoding="utf8").splitlines()]
    assert any(row["tool_name"] == name and row["success"] for row in audits)
