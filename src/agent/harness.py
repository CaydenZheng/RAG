"""
Agent 核心循环。

核心流程（Plan → Execute → Observe 循环）：
  1. 加载长期记忆 + 近期对话历史 → 构建 messages
  2. LLM 规划：输出 JSON {action: "tool_call"|"final_answer", ...}
  3. tool_call → Hook 管线 → 安全检查 → 执行工具 → 结果注入 → 回到步骤 2
  4. final_answer → 更新记忆 → 返回

特性：
  - 最大迭代 5 轮（防无限循环）
  - 集成 HookPipeline（日志、限流、审计、黑名单阻断）
  - 集成 MemoryManager（两层记忆 + 自动压缩）
  - 集成 ToolRegistry（三级审批 + 参数校验 + 去重）
"""

import json
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field
from loguru import logger

from src.agent.hooks import HookPipeline, HookContext, HookEvent, create_default_pipeline
from src.agent.memory import MemoryManager, MemoryConfig, memory_manager
from src.agent.tools import ToolRegistry, ToolResult, SafetyLevel, tool_registry


# ================================================================
# 数据模型
# ================================================================

@dataclass
class AgentConfig:
    """Agent 配置"""
    max_iterations: int = 5              # 最大规划-执行轮次
    planner_model: str = ""              # 空表示使用 settings.llm_model
    planner_temperature: float = 0.1     # 规划用低温，保证决策稳定
    max_tool_result_length: int = 1000   # 工具结果注入消息列表时的截断长度
    verbose: bool = True                 # 是否打印规划过程


@dataclass
class AgentResponse:
    """Agent 响应"""
    session_id: str
    answer: str
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    iterations: int = 0
    total_latency_ms: float = 0.0
    error: str = ""


# ================================================================
# Agent Planner Prompt（内联，避免复杂 prompt 文件）
# ================================================================

PLANNER_SYSTEM_PROMPT = """You are an AI Agent. Your ENTIRE response must be a single JSON object — no text before or after.

## Available Tools
{tool_descriptions}

## CRITICAL: Response Format
You MUST output ONLY a JSON object in one of these two formats, nothing else:

Tool call:
{{"action":"tool_call","tool_name":"<name>","tool_params":{{...}},"reasoning":"<why>"}}

Final answer:
{{"action":"final_answer","answer":"<your answer>","reasoning":"<summary>"}}

## CRITICAL Rules
1. For ANY factual/知识类 question (history, science, definitions, people, places, etc), you MUST call search_knowledge_base. NEVER answer from your own knowledge.
2. For math calculation questions, use calculator.
3. Only use final_answer if the question is chitchat ("你好") or you already have tool results.
4. NEVER fabricate information. If search returns nothing, say so in final_answer.
5. Cite sources from tool results in your final answer."""


# ================================================================
# Agent Harness
# ================================================================

class AgentHarness:
    """
    Agent 核心循环控制器。

    使用方式：
        harness = AgentHarness()
        resp = harness.run("session_123", "PocketFlow 是怎么实现 Node 生命周期的？")
        print(resp.answer)
    """

    def __init__(
        self,
        config: AgentConfig = None,
        memory: MemoryManager = None,
        tools: ToolRegistry = None,
        hooks: HookPipeline = None,
    ):
        self.config = config or AgentConfig()
        self.memory = memory or memory_manager
        self.tools = tools or tool_registry
        self.hooks = hooks or create_default_pipeline()

    # ================================================================
    # 主入口
    # ================================================================

    def run(self, session_id: str, user_message: str) -> AgentResponse:
        """
        执行一次 Agent 对话。

        Args:
            session_id: 会话标识（用于记忆持久化）
            user_message: 用户输入

        Returns:
            AgentResponse（答案、工具调用记录、耗时等）
        """
        start_time = time.time()
        tool_calls_log = []

        try:
            # 1. 会话开始 Hook
            self._fire_hook(HookEvent.SESSION_START, session_id, {
                "user_message": user_message[:200],
            })

            # 2. 构建初始 messages
            messages = self.memory.build_messages(
                session_id=session_id,
                system_prompt=self._build_planner_prompt(),
                user_message=user_message,
            )

            # 3. Plan → Execute → Observe 循环
            final_answer = ""
            iterations = 0

            for iteration in range(1, self.config.max_iterations + 1):
                iterations = iteration

                # 3a. LLM 规划
                plan = self._plan(messages)
                if self.config.verbose:
                    logger.info("[Agent iter={}] plan: {}", iteration,
                                json.dumps(plan, ensure_ascii=False)[:200])

                # 3b. 执行
                if plan.get("action") == "tool_call":
                    tool_name = plan.get("tool_name", "")
                    tool_params = plan.get("tool_params", {})

                    # 安全检查（Hook 管线）
                    hook_ctx = self._fire_hook(HookEvent.PRE_TOOL_USE, session_id, {
                        "tool_name": tool_name,
                        "tool_params": tool_params,
                    })
                    if hook_ctx.blocked:
                        # 工具被 Hook 阻断
                        blocked_msg = f"[Blocked] {hook_ctx.block_reason}"
                        messages.append({"role": "user", "content": blocked_msg})
                        tool_calls_log.append({
                            "tool": tool_name, "params": tool_params,
                            "blocked": True, "reason": hook_ctx.block_reason,
                        })
                        self.memory.add_turn(session_id, "user", blocked_msg,
                                                 metadata={"blocked": True})
                        continue

                    # 执行工具
                    result = self.tools.execute(tool_name, tool_params, session_id)

                    # 截断工具结果，控制上下文
                    result_content = str(result.data) if result.success else f"Error: {result.error}"
                    result_content = self.memory.truncate_tool_result(
                        result_content[:self.config.max_tool_result_length]
                    )

                    # 结果注入消息列表（用 user 角色，DeepSeek 不支持 role=tool）
                    tool_msg = f"[Tool Result: {tool_name}]\n{result_content}"
                    messages.append({"role": "user", "content": tool_msg})

                    # 记录操作
                    tool_calls_log.append({
                        "tool": tool_name,
                        "params": tool_params,
                        "success": result.success,
                        "latency_ms": round(result.latency_ms, 1),
                    })
                    self.memory.add_turn(session_id, "user", tool_msg,
                                         metadata={"tool_name": tool_name,
                                                   "success": result.success})

                    # Post-tool Hook
                    self._fire_hook(HookEvent.POST_TOOL_USE, session_id, {
                        "tool_name": tool_name,
                        "result_success": result.success,
                        "result_length": len(result_content),
                    })

                elif plan.get("action") == "final_answer":
                    final_answer = plan.get("answer", "")

                    # Pre-generation Hook
                    self._fire_hook(HookEvent.PRE_GENERATION, session_id, {
                        "answer_length": len(final_answer),
                    })

                    # 保存助手回复到记忆
                    self.memory.add_turn(session_id, "assistant", final_answer)

                    # Post-generation Hook
                    self._fire_hook(HookEvent.POST_GENERATION, session_id, {
                        "answer": final_answer[:200],
                        "iterations": iteration,
                    })
                    break

                else:
                    # 格式异常
                    logger.warning("Unknown plan action: {}", plan.get("action"))
                    final_answer = "I encountered an internal error. Please try again."
                    break

            # 4. 达最大迭代次数仍未产出答案
            if not final_answer:
                final_answer = self._force_final_answer(messages)
                self.memory.add_turn(session_id, "assistant", final_answer)

            # 5. 会话结束 Hook
            total_latency = (time.time() - start_time) * 1000
            self._fire_hook(HookEvent.SESSION_END, session_id, {
                "iterations": iterations,
                "tool_calls": len(tool_calls_log),
                "total_latency_ms": round(total_latency, 1),
            })

            return AgentResponse(
                session_id=session_id,
                answer=final_answer,
                tool_calls=tool_calls_log,
                iterations=iterations,
                total_latency_ms=round(total_latency, 1),
            )

        except Exception as e:
            logger.error("Agent run error: {}", e)
            self._fire_hook(HookEvent.ON_ERROR, session_id, {"error": str(e)})
            return AgentResponse(
                session_id=session_id,
                answer="I encountered an error. Please try again.",
                error=str(e),
                total_latency_ms=(time.time() - start_time) * 1000,
            )

    def reset_session(self, session_id: str):
        """重置会话（/new 命令）"""
        self.memory.clear_session(session_id)
        self._fire_hook(HookEvent.SESSION_END, session_id,
                        {"reason": "manual_reset"})

    # ================================================================
    # 内部方法
    # ================================================================

    def _build_planner_prompt(self) -> str:
        """构建 Planner 的 system prompt（含工具描述）"""
        return PLANNER_SYSTEM_PROMPT.format(
            tool_descriptions=self.tools.get_tool_descriptions()
        )

    def _plan(self, messages: List[dict]) -> dict:
        """
        调用 LLM 做规划决策。

        Returns:
            {"action": "tool_call", "tool_name": "...", "tool_params": {...}, "reasoning": "..."}
            或 {"action": "final_answer", "answer": "...", "reasoning": "..."}
        """
        from src.llm import llm_client
        from config.settings import settings as _s

        model = self.config.planner_model or _s.llm_model
        raw = llm_client.chat(
            messages,
            model=model,
            temperature=self.config.planner_temperature,
            max_tokens=1024,
        )

        return self._parse_plan_json(raw)

    def _parse_plan_json(self, raw: str) -> dict:
        """解析 LLM 输出的 JSON。三级兜底：代码块提取 → 正则匹配首个 JSON 对象 → 全文降级"""
        # 1. 尝试提取 Markdown 代码块
        if "```json" in raw:
            raw = raw.split("```json")[1].split("```")[0].strip()
        elif "```" in raw:
            raw = raw.split("```")[1].split("```")[0].strip()

        # 2. 尝试直接解析
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass

        # 3. 正则匹配第一个完整的 JSON 对象
        import re
        m = re.search(r'\{[^{}]*"action"\s*:\s*"(?:tool_call|final_answer)"[^{}]*\}', raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass

        # 4. 兜底：全文当最终答案
        logger.warning("Failed to parse planner JSON: {}", raw[:200])
        return {"action": "final_answer", "answer": raw.strip(),
                "reasoning": "JSON parse failed, returning raw output"}

    def _force_final_answer(self, messages: List[dict]) -> str:
        """
        达到最大迭代次数后，强制 LLM 基于当前信息给出最终答案。
        """
        force_prompt = (
            "\n\n[SYSTEM] You have reached the maximum number of tool calls. "
            "Please provide your best answer NOW based on all the information gathered so far. "
            "Do NOT request any more tools. Output only the final answer, no JSON wrapper."
        )
        messages.append({"role": "user", "content": force_prompt})

        from src.llm import llm_client
        try:
            return llm_client.chat(messages, temperature=0.3, max_tokens=1024)
        except Exception:
            return "I was unable to complete the task within the allowed steps. Please try a more specific question."

    def _fire_hook(self, event: HookEvent, session_id: str,
                   data: dict = None) -> HookContext:
        """触发 Hook 管线"""
        ctx = HookContext(
            event=event,
            session_id=session_id,
            data=data or {},
        )
        return self.hooks.fire(ctx)


# ================================================================
# 全局单例
# ================================================================

agent_harness = AgentHarness()
