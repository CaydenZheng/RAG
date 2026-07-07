"""
Hook 事件拦截管线。

在 Agent 生命周期关键节点（PreToolUse、PostToolUse、SessionStart 等）构建
事件拦截管线，支持精确/正则模式匹配，与核心循环解耦，实现非侵入式扩展。

内置 Hook：
  LoggingHook   — 记录所有事件到 agent_events.jsonl
  RateLimitHook — 限制每分钟工具调用次数
  AuditHook     — 审计灰名单/黑名单工具调用
  BlacklistBlockHook — 匹配危险模式直接阻断
"""

import json
import re
import time
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from dataclasses import dataclass, field
from loguru import logger


# ================================================================
# 事件定义
# ================================================================

class HookEvent(str, Enum):
    """Agent 生命周期事件"""
    SESSION_START = "session_start"
    SESSION_END = "session_end"
    PRE_PLANNING = "pre_planning"           # LLM 规划前
    POST_PLANNING = "post_planning"         # LLM 规划后
    PRE_TOOL_USE = "pre_tool_use"           # 工具执行前
    POST_TOOL_USE = "post_tool_use"         # 工具执行后
    PRE_RETRIEVAL = "pre_retrieval"         # RAG 检索前（search_knowledge_base 内部）
    POST_RETRIEVAL = "post_retrieval"       # RAG 检索后
    PRE_GENERATION = "pre_generation"       # 最终答案生成前
    POST_GENERATION = "post_generation"     # 最终答案生成后
    ON_ERROR = "on_error"                   # 异常
    MEMORY_COMPRESS = "memory_compress"     # 记忆压缩触发


@dataclass
class HookContext:
    """Hook 事件携带的上下文数据"""
    event: HookEvent
    session_id: str
    timestamp: float = field(default_factory=time.time)
    data: Dict[str, Any] = field(default_factory=dict)   # 事件相关数据
    blocked: bool = False                                  # 是否被 Hook 阻断
    block_reason: str = ""                                 # 阻断原因


# Handler 签名：接收 HookContext，返回 HookContext（可修改或设置 blocked=True）
HookHandler = Callable[[HookContext], HookContext]


# ================================================================
# Hook 管线
# ================================================================

class HookPipeline:
    """
    有序 Hook 管线。

    使用方式：
        pipeline = HookPipeline()
        pipeline.register(HookEvent.PRE_TOOL_USE, logging_hook, priority=10)
        pipeline.register(HookEvent.PRE_TOOL_USE, audit_hook, priority=20,
                          pattern=r"delete_.*|execute_.*")

        ctx = HookContext(event=HookEvent.PRE_TOOL_USE, ...)
        ctx = pipeline.fire(ctx)
        if ctx.blocked:
            return f"Blocked: {ctx.block_reason}"
    """

    def __init__(self):
        # { event: [(priority, pattern, handler), ...] }
        self._handlers: Dict[HookEvent, List[tuple]] = {}

    def register(
        self,
        event: HookEvent,
        handler: HookHandler,
        priority: int = 100,
        pattern: Optional[str] = None,
    ):
        """
        注册一个 Hook handler。

        Args:
            event: 监听的事件类型
            handler: 处理函数
            priority: 优先级（越小越先执行）
            pattern: 正则匹配模式。非空时，仅当 data["tool_name"] 匹配时才触发。
                     用于区分不同工具的 Hook（如黑名单工具才走审计 Hook）。
        """
        if event not in self._handlers:
            self._handlers[event] = []
        self._handlers[event].append((priority, pattern, handler))
        self._handlers[event].sort(key=lambda x: x[0])  # 按优先级排序

    def fire(self, ctx: HookContext) -> HookContext:
        """触发事件，按序执行所有匹配的 handler"""
        handlers = self._handlers.get(ctx.event, [])
        if not handlers:
            return ctx

        for priority, pattern, handler in handlers:
            # 模式匹配：如果配置了 pattern，检查 tool_name 是否匹配
            if pattern:
                tool_name = ctx.data.get("tool_name", "")
                if not re.search(pattern, tool_name):
                    continue

            try:
                ctx = handler(ctx)
                if ctx.blocked:
                    logger.info("Hook blocked: event={} reason={} handler_priority={}",
                                ctx.event.value, ctx.block_reason, priority)
                    break  # 一旦被阻断，不再执行后续 handler
            except Exception as e:
                logger.error("Hook handler error: event={} priority={} error={}",
                             ctx.event.value, priority, e)
        return ctx


# ================================================================
# 内置 Handler
# ================================================================

def create_logging_hook(log_dir: str = "logs") -> HookHandler:
    """创建日志 Hook — 记录所有事件到 JSON Lines 文件"""
    log_path = Path(log_dir) / "agent_events.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def logging_hook(ctx: HookContext) -> HookContext:
        record = {
            "event": ctx.event.value,
            "session_id": ctx.session_id,
            "timestamp": ctx.timestamp,
            "data": {k: str(v)[:200] for k, v in ctx.data.items()},  # 截断长数据
        }
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return ctx

    return logging_hook


def create_rate_limit_hook(max_per_minute: int = 30) -> HookHandler:
    """
    创建限流 Hook — 限制每分钟工具调用次数。

    使用滑动窗口记录最近 60 秒内的调用时间戳。
    """
    call_times: List[float] = []

    def rate_limit_hook(ctx: HookContext) -> HookContext:
        nonlocal call_times
        now = time.time()
        # 清理 60 秒外的记录
        call_times = [t for t in call_times if now - t < 60]
        if len(call_times) >= max_per_minute:
            ctx.blocked = True
            ctx.block_reason = f"Rate limit exceeded: {max_per_minute}/min"
            return ctx
        call_times.append(now)
        return ctx

    return rate_limit_hook


def create_audit_hook(audit_dir: str = "logs") -> HookHandler:
    """
    创建审计 Hook — 记录灰名单/黑名单工具调用。

    仅对 PRE_TOOL_USE 事件生效，记录工具名、参数、时间戳。
    """
    audit_path = Path(audit_dir) / "audit.jsonl"
    audit_path.parent.mkdir(parents=True, exist_ok=True)

    def audit_hook(ctx: HookContext) -> HookContext:
        record = {
            "event": ctx.event.value,
            "session_id": ctx.session_id,
            "timestamp": ctx.timestamp,
            "tool_name": ctx.data.get("tool_name", "unknown"),
            "tool_params": str(ctx.data.get("tool_params", {}))[:500],
            "safety_level": ctx.data.get("safety_level", "unknown"),
        }
        with open(audit_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return ctx

    return audit_hook


def create_blacklist_block_hook(blacklist_patterns: List[str]) -> HookHandler:
    """
    创建黑名单阻断 Hook — 匹配的工具调用直接拦截。

    Args:
        blacklist_patterns: 正则模式列表，如 ["delete_.*", "execute_code"]

    Hook 的 blocked=True 会阻止核心循环执行该工具。
    """

    def blacklist_hook(ctx: HookContext) -> HookContext:
        tool_name = ctx.data.get("tool_name", "")
        for pattern in blacklist_patterns:
            if re.search(pattern, tool_name):
                ctx.blocked = True
                ctx.block_reason = f"Tool '{tool_name}' blocked by blacklist policy (pattern: {pattern})"
                return ctx
        return ctx

    return blacklist_hook


# ================================================================
# 默认管线工厂
# ================================================================

def create_default_pipeline() -> HookPipeline:
    """创建带默认 handler 的管线"""
    pipeline = HookPipeline()

    # 日志：所有事件
    log_hook = create_logging_hook()
    for event in HookEvent:
        pipeline.register(event, log_hook, priority=1000)

    # 限流：仅工具调用
    rate_hook = create_rate_limit_hook(max_per_minute=30)
    pipeline.register(HookEvent.PRE_TOOL_USE, rate_hook, priority=10)

    # 审计：灰名单/黑名单工具（匹配危险操作）
    audit_hook = create_audit_hook()
    pipeline.register(HookEvent.PRE_TOOL_USE, audit_hook, priority=20,
                      pattern=r"read_file|web_search|execute_")

    # 黑名单阻断
    blacklist = create_blacklist_block_hook([
        r"delete_.*",
        r"execute_code",
        r"exec\b",
        r"rm\b",
        r"sudo\b",
    ])
    pipeline.register(HookEvent.PRE_TOOL_USE, blacklist, priority=5)

    return pipeline
