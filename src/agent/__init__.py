"""
Agent 模块 — 工具调用、记忆管理、Hook 拦截的智能体。

  hooks.py   → 事件拦截管线（日志、限流、审计、黑名单阻断）
  memory.py  → 两层记忆（长期偏好 + 短期历史，LLM 自动压缩归档）
  tools.py   → 工具注册 + 三级审批 + 参数校验 + 去重
  harness.py → Agent 核心循环（Plan → Tool Call → Observe → Answer）
"""

from src.agent.harness import AgentHarness, AgentResponse, AgentConfig
from src.agent.hooks import HookPipeline, HookEvent, HookContext
from src.agent.memory import MemoryManager, MemoryConfig, MemoryTurn
from src.agent.tools import ToolRegistry, ToolResult, SafetyLevel, ToolDef, ToolParam

__all__ = [
    "AgentHarness", "AgentResponse", "AgentConfig",
    "HookPipeline", "HookEvent", "HookContext",
    "MemoryManager", "MemoryConfig", "MemoryTurn",
    "ToolRegistry", "ToolResult", "SafetyLevel", "ToolDef", "ToolParam",
]
