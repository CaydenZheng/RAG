"""Planner history is data; bounded tools retain a final-answer token allowance."""
import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from tests.offline.test_agent_runtime import _runtime
from tests.offline.test_agent_runtime import agent_token_counter as agent_token_counter


@pytest.mark.parametrize("mode", ["json", "native"])
@pytest.mark.parametrize("history", [False, True])
@pytest.mark.parametrize("skills", [False, True])
def test_old_answers_are_context_for_planning_and_dialogue_for_final_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, history: bool, skills: bool,
) -> None:
    from src.agent.tools import create_default_registry
    from src.core.run_options import RunOptions, SkillSnapshot, scoped_run_options
    from src.llm import llm_client
    from src.llm.client import NativeChatResponse, NativeToolCall

    harness, memory, _ = _runtime(tmp_path, planner_mode=mode)
    harness.tools = create_default_registry()
    old_question, old_answer = "不用工具，用一句话说明 2 加 2 等于几。", "2 加 2 等于 4。"
    if history:
        memory.add_turn("original", "user", old_question)
        memory.add_turn("original", "assistant", old_answer)
    question = "请使用计算器计算 (12 + 21) * 12，给出结果。"
    planning_calls = 0
    final_limits: list[int] = []

    async def plan(messages: list[dict], *args: Any, **kwargs: Any) -> Any:
        nonlocal planning_calls
        planning_calls += 1
        if history:
            assert not any(message["role"] == "assistant" and message.get("content") == old_answer for message in messages)
            context = next(message for message in messages if old_answer in (message.get("content") or ""))
            assert context["role"] == "user"
            assert old_question in context["content"]
            assert '"role": "assistant"' in context["content"]
        assert any(message["role"] == "user" and message.get("content") == question for message in messages)
        if mode == "json":
            return json.dumps({"action": "tool_call", "tool_name": "calculator", "tool_params": {"expression": "(12 + 21) * 12"}} if planning_calls == 1 else {"action": "final_answer"})
        if planning_calls == 2:
            assert messages[-1]["role"] == "tool"
            assert messages[-1]["tool_call_id"] == "current-call"
            return NativeChatResponse(content="READY_TO_ANSWER")
        alias = next(item["function"]["name"] for item in args[0] if item["function"]["name"] == "calculator")
        return NativeChatResponse(content="", tool_calls=(NativeToolCall("current-call", alias, '{"expression":"(12 + 21) * 12"}'),))

    async def stream(messages: list[dict], **kwargs: Any):
        final_limits.append(kwargs["max_tokens"])
        if history:
            assert any(message["role"] == "assistant" and message.get("content") == old_answer for message in messages)
        assert any("396" in (message.get("content") or "") for message in messages)
        yield "U2-VERIFIED\n396" if skills else "396"

    monkeypatch.setattr(llm_client, "chat_async", plan)
    monkeypatch.setattr(llm_client, "chat_with_tools_async", plan)
    monkeypatch.setattr(llm_client, "chat_stream_async", stream)
    options = RunOptions("pro", "fixture-pro", (SkillSnapshot("test", "acceptance-marker", "v1", "Begin the final answer with U2-VERIFIED."),) if skills else ())

    async def run() -> Any:
        with scoped_run_options(options):
            return await harness.execute("original", question)

    response = asyncio.run(run())
    assert not response.error_code
    assert response.answer.endswith("396")
    assert len(response.tool_calls) == 1 and response.tool_calls[0].success
    assert final_limits == [harness.config.final_max_tokens]
    assert planning_calls == 2
    assert memory.load_history("original")[-1].content == response.answer


@pytest.mark.parametrize("mode", ["json", "native"])
def test_tool_is_not_executed_when_final_answer_allowance_cannot_be_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    from src.agent.harness import _NativeToolContext, _ToolCallPlan
    from src.llm import llm_client

    harness, memory, tools = _runtime(tmp_path, planner_mode=mode, max_token_budget=6500)
    if mode == "native":
        tools.get_model_tools = lambda: []
    planned: list[bool] = []
    streamed: list[bool] = []

    async def plan(*args: Any, **kwargs: Any) -> Any:
        planned.append(True)
        return _ToolCallPlan("lookup", {"key": "value"}, _NativeToolContext("call", {"role": "assistant", "content": None, "tool_calls": []}) if mode == "native" else None)

    async def stream(*args: Any, **kwargs: Any):
        streamed.append(True)
        yield "must not run"

    monkeypatch.setattr(harness, "_plan_async", plan)
    monkeypatch.setattr(llm_client, "chat_stream_async", stream)
    response = asyncio.run(harness.execute("insufficient-final", "question"))
    assert planned == [True]
    assert response.error_code == "agent_token_budget_exceeded"
    assert tools.calls == [] and streamed == []
    assert memory.load_history("insufficient-final") == []


def test_input_budget_uses_token_estimate_and_margin_instead_of_characters(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.agent.harness import _RunBudget
    from src.utils import token_counter

    monkeypatch.setattr(token_counter, "count_tokens", lambda text: 50)
    budget = _RunBudget(100, 1, 1000)
    budget.consume_text("English text and 中文。" * 50)
    assert 50 <= budget.tokens_used < 100
    assert budget.reserve_output(10) == 10
    assert 60 <= budget.tokens_used < 100


@pytest.mark.parametrize("mode", ["json", "native"])
def test_past_answer_is_not_accepted_as_a_new_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    from src.agent.tools import ToolRegistry
    from src.llm import llm_client
    from src.llm.client import NativeChatResponse

    harness, memory, tools = _runtime(tmp_path, planner_mode=mode)
    if mode == "native":
        harness.tools = ToolRegistry()
    memory.add_turn("strict-plan", "user", "old question")
    memory.add_turn("strict-plan", "assistant", "old natural answer")
    streamed: list[bool] = []

    async def plan(*args: Any, **kwargs: Any) -> Any:
        return "3 加 3 等于 6。" if mode == "json" else NativeChatResponse(content="3 加 3 等于 6。")

    async def stream(*args: Any, **kwargs: Any):
        streamed.append(True)
        yield "must not run"

    monkeypatch.setattr(llm_client, "chat_async", plan)
    monkeypatch.setattr(llm_client, "chat_with_tools_async", plan)
    monkeypatch.setattr(llm_client, "chat_stream_async", stream)
    response = asyncio.run(harness.execute("strict-plan", "new question"))
    assert response.error_code == "invalid_agent_plan"
    assert not streamed and not tools.calls
    assert len(memory.load_history("strict-plan")) == 2


def test_literal_tokenizer_control_spelling_is_counted_without_disclosing_text(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.agent.harness import _RunBudget
    from src.utils import token_counter

    def reject_special(text: str) -> int:
        raise ValueError("sensitive text must not escape into errors")

    monkeypatch.setattr(token_counter, "count_tokens", reject_special)
    budget = _RunBudget(100, 1, 1000)
    text = "<|endoftext|>中文"
    budget.consume_text(text)
    assert len(text.encode("utf-8")) <= budget.tokens_used < 100


@pytest.mark.parametrize("mode", ["json", "native"])
@pytest.mark.parametrize("outcome", ["success", "error", "cancel"])
def test_bounded_dense_tool_result_keeps_final_allowance_before_next_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, outcome: str,
) -> None:
    from src.agent.harness import _RunBudget
    from src.agent.tools import (
        SafetyLevel,
        ToolDef,
        ToolResult,
        create_default_registry,
    )
    from src.core.agent_runtime import AgentEventKind
    from src.llm import llm_client
    from src.llm.client import NativeChatResponse, NativeToolCall
    from src.utils import token_counter

    # Model the real cl100k density of this fixture (3 tokens per character),
    # without requiring a personal tokenizer cache or network in offline tests.
    original_count = token_counter.count_tokens
    monkeypatch.setattr(token_counter, "count_tokens", lambda text: original_count(text) + 2 * text.count("🧪"))
    harness, memory, _ = _runtime(tmp_path, planner_mode=mode)
    tools = create_default_registry()
    tool_calls: list[bool] = []

    def execute(params: dict) -> ToolResult:
        tool_calls.append(True)
        return ToolResult(success=True, data={"text": "🧪" * 1000})

    tools.register(ToolDef(name="high_token_fixture", description="Synthetic bounded fixture.",
                           params=[], safety_level=SafetyLevel.WHITELIST, execute_fn=execute))
    harness.tools = tools
    planning_calls: list[bool] = []
    final_limits: list[int] = []
    charged_inputs: list[str] = []
    original_consume = _RunBudget.consume_text

    def consume(budget: _RunBudget, text: str) -> None:
        charged_inputs.append(text)
        original_consume(budget, text)

    async def plan(*args: Any, **kwargs: Any) -> Any:
        planning_calls.append(True)
        assert len(planning_calls) == 1, "Cannot spend the reserved final allowance on another planner call"
        if mode == "native":
            return NativeChatResponse(content="", tool_calls=(NativeToolCall("dense-call", "high_token_fixture", "{}"),))
        return json.dumps({"action": "tool_call", "tool_name": "high_token_fixture", "tool_params": {}})

    async def stream(messages: list[dict], **kwargs: Any) -> AsyncIterator[str]:
        final_limits.append(kwargs["max_tokens"])
        prompt = messages[0]["content"]
        assert "budget" in prompt and "limitations" in prompt
        assert any("🧪" in (message.get("content") or "") for message in messages)
        if mode == "native":
            assert messages[-1]["role"] == "tool" and messages[-1]["tool_call_id"] == "dense-call"
            assert messages[-2]["tool_calls"][0]["id"] == "dense-call"
        yield "Only the gathered result is available."
        if outcome == "error":
            raise RuntimeError("synthetic private provider detail")
        if outcome == "cancel":
            raise asyncio.CancelledError

    monkeypatch.setattr(_RunBudget, "consume_text", consume)
    monkeypatch.setattr(llm_client, "chat_async", plan)
    monkeypatch.setattr(llm_client, "chat_with_tools_async", plan)
    monkeypatch.setattr(llm_client, "chat_stream_async", stream)
    events: list[Any] = []

    async def run() -> None:
        async for event in harness.events("bounded-dense", "Run high_token_fixture once, then explain its result briefly."):
            events.append(event)

    if outcome == "cancel":
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(run())
    else:
        asyncio.run(run())
    assert tool_calls == [True] and planning_calls == [True]
    assert final_limits == [4096]
    # Only the final input may charge the result, never an unissued next plan.
    assert sum("🧪" in text for text in charged_inputs) == 1
    assert sum(event.kind is AgentEventKind.PLANNING for event in events) == 1
    if outcome == "success":
        assert events[-1].kind is AgentEventKind.DONE
        assert memory.load_history("bounded-dense")[-1].content == "Only the gathered result is available."
    else:
        assert not any(event.kind is AgentEventKind.DONE for event in events)
        assert memory.load_history("bounded-dense") == []
        if outcome == "error":
            assert events[-1].kind is AgentEventKind.ERROR
            assert events[-1].error_code == "agent_final_generation_failed"


@pytest.mark.parametrize("mode", ["json", "native"])
def test_unaffordable_next_tool_uses_existing_observation_without_protocol_or_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    from src.agent.harness import _NativeToolContext, _ToolCallPlan
    from src.llm import llm_client

    harness, memory, tools = _runtime(tmp_path, planner_mode=mode, max_token_budget=10_000)
    if mode == "native":
        tools.get_model_tools = lambda: []
    planned: list[str] = []
    streamed: list[bool] = []

    async def plan(*args: Any, **kwargs: Any) -> Any:
        call_id = "first-call" if not planned else "unperformed-call"
        planned.append(call_id)
        params: dict[str, str] = {"key": call_id}
        context = _NativeToolContext(call_id, {"role": "assistant", "content": None, "tool_calls": [{
            "id": call_id, "type": "function", "function": {"name": "lookup", "arguments": json.dumps(params)},
        }]}) if mode == "native" else None
        return _ToolCallPlan("lookup", params, context)

    async def stream(messages: list[dict], **kwargs: Any) -> AsyncIterator[str]:
        streamed.append(True)
        assert kwargs["max_tokens"] == 4096
        assert "limitations" in messages[0]["content"]
        assert "trusted fact" in json.dumps(messages)
        assert "unperformed-call" not in json.dumps(messages)
        if mode == "native":
            assert messages[-1]["tool_call_id"] == "first-call"
        yield "Existing observation only; further work was limited."

    monkeypatch.setattr(harness, "_plan_async", plan)
    monkeypatch.setattr(llm_client, "chat_stream_async", stream)
    response = asyncio.run(harness.execute("next-tool", "question"))
    assert not response.error_code
    assert planned == ["first-call", "unperformed-call"]
    assert tools.calls == [("lookup", {"key": "first-call"}, "next-tool")]
    assert streamed == [True]
    assert len(response.tool_calls) == 1
    assert memory.load_history("next-tool")[-1].content == response.answer


@pytest.mark.parametrize("mode", ["json", "native"])
def test_current_skill_format_reminder_follows_old_dialogue_only_in_final_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    from src.agent.harness import _FinalAnswerPlan
    from src.core.run_options import RunOptions, SkillSnapshot, scoped_run_options
    from src.llm import llm_client

    harness, memory, tools = _runtime(tmp_path, planner_mode=mode)
    if mode == "native":
        tools.get_model_tools = lambda: []
    memory.add_turn("format-history", "user", "What is 2 plus 2?")
    memory.add_turn("format-history", "assistant", "4.")
    skill_body: str = "Begin with the configured heading, then answer normally."
    options = RunOptions("pro", "fixture-pro", (SkillSnapshot("format", "answer-format", "v1", skill_body),))

    async def plan(messages: list[dict], **kwargs: Any) -> Any:
        assert "exact JSON decision" in messages[0]["content"]
        assert messages[-1]["role"] == "user"
        return _FinalAnswerPlan()

    async def stream(messages: list[dict], **kwargs: Any) -> AsyncIterator[str]:
        assert messages[-1]["role"] == "system"
        assert "enabled skill instructions and their output format" in messages[-1]["content"]
        assert "very short response" in messages[-1]["content"]
        assert "System safety rules remain in force" in messages[-1]["content"]
        assert "configured heading" not in messages[-1]["content"]
        assert sum(skill_body in (message.get("content") or "") for message in messages) == 1
        assert messages[-2] == {"role": "user", "content": "In one sentence, what is 3 plus 3?"}
        assert any(message.get("role") == "assistant" and message.get("content") == "4." for message in messages)
        yield "Configured heading\n6."

    monkeypatch.setattr(harness, "_plan_async", plan)
    monkeypatch.setattr(llm_client, "chat_stream_async", stream)

    async def run() -> Any:
        with scoped_run_options(options):
            return await harness.execute("format-history", "In one sentence, what is 3 plus 3?")

    response = asyncio.run(run())
    assert not response.error_code
    assert response.answer == "Configured heading\n6."
    assert tools.calls == []
    assert memory.load_history("format-history")[-1].content == response.answer
