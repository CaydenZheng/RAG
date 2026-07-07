"""
工具安全与运行治理。

安全等级：
  WHITELIST → 自动执行（纯读取、纯计算，无副作用）
  GRAYLIST  → 审计记录后执行（可能读敏感信息，需留痕）
  BLACKLIST → 直接阻断 + 写审计日志（危险操作）

内置工具：
  search_knowledge_base — 复用现有 RAG 管线，检索知识库
  calculator            — 安全数学表达式求值（受限 eval + 白名单）
"""

import hashlib
import json
import math
import time
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from loguru import logger


# ================================================================
# 数据模型
# ================================================================

class SafetyLevel(str, Enum):
    """工具安全等级"""
    WHITELIST = "whitelist"   # 自动执行
    GRAYLIST = "graylist"     # 审计后执行
    BLACKLIST = "blacklist"   # 阻断


@dataclass
class ToolParam:
    """工具参数定义"""
    name: str
    type: str           # "str" | "int" | "float" | "bool" | "dict"
    description: str
    required: bool = True
    default: Any = None


@dataclass
class ToolResult:
    """工具执行结果"""
    success: bool
    data: Any = None
    error: str = ""
    tool_name: str = ""
    latency_ms: float = 0.0


@dataclass
class ToolDef:
    """工具定义"""
    name: str                     # 唯一标识
    description: str              # LLM 理解工具用途的描述
    params: List[ToolParam]       # 参数列表
    safety_level: SafetyLevel     # 安全等级
    execute_fn: Callable          # 执行函数 (params: dict) -> ToolResult
    category: str = "general"     # 分类
    max_retries: int = 1          # 失败重试次数


# ================================================================
# 工具注册中心
# ================================================================

class ToolRegistry:
    """
    工具注册 & 安全执行。

    安全流程：
      1. 参数校验（JSON Schema 风格，类型 + 必填检查）
      2. 安全等级判断 → 白名单放行 / 灰名单审计 / 黑名单阻断
      3. 去重检查（同一 session 内相同调用 30s 内不重复）
      4. 执行 + 审计
    """

    def __init__(self, dedup_window: float = 30.0):
        self._tools: Dict[str, ToolDef] = {}
        self._dedup_cache: Dict[str, Dict[str, float]] = {}  # {session_id: {hash: timestamp}}
        self._dedup_window = dedup_window

    # ----------------------------------------------------------------
    # 注册
    # ----------------------------------------------------------------

    def register(self, tool: ToolDef):
        """注册工具"""
        self._tools[tool.name] = tool
        logger.info("Tool registered: {} (safety={}, params={})",
                     tool.name, tool.safety_level.value, len(tool.params))

    def unregister(self, name: str):
        """移除工具"""
        self._tools.pop(name, None)

    @property
    def tools(self) -> Dict[str, ToolDef]:
        return self._tools

    def get_tool(self, name: str) -> Optional[ToolDef]:
        return self._tools.get(name)

    # ----------------------------------------------------------------
    # LLM 可见的工具描述（用于 Agent Planner prompt）
    # ----------------------------------------------------------------

    def get_tool_descriptions(self) -> str:
        """生成供 LLM 理解的工具列表（JSON 格式，注入 planner prompt）"""
        tools_list = []
        for name, tool in self._tools.items():
            params_desc = {
                p.name: {
                    "type": p.type,
                    "description": p.description,
                    "required": p.required,
                }
                for p in tool.params
            }
            tools_list.append({
                "name": name,
                "description": tool.description,
                "params": params_desc,
            })
        return json.dumps(tools_list, ensure_ascii=False, indent=2)

    # ----------------------------------------------------------------
    # 安全执行
    # ----------------------------------------------------------------

    def execute(
        self,
        tool_name: str,
        params: dict,
        session_id: str = "default",
    ) -> ToolResult:
        """安全执行工具调用"""
        start = time.time()

        # 1. 工具存在检查
        tool = self._tools.get(tool_name)
        if tool is None:
            return ToolResult(
                success=False,
                error=f"Unknown tool: {tool_name}. Available: {list(self._tools.keys())}",
                tool_name=tool_name,
            )

        # 2. 参数校验
        param_error = self._validate_params(tool, params)
        if param_error:
            return ToolResult(success=False, error=param_error, tool_name=tool_name)

        # 3. 安全等级判断
        if tool.safety_level == SafetyLevel.BLACKLIST:
            logger.warning("BLACKLIST tool blocked: {}", tool_name)
            return ToolResult(
                success=False,
                error=f"Tool '{tool_name}' is blocked by safety policy (blacklist).",
                tool_name=tool_name,
            )

        # 4. 去重检查
        call_hash = self._hash_call(tool_name, params)
        if self._is_duplicate(session_id, call_hash):
            return ToolResult(
                success=False,
                error=f"Duplicate call detected for '{tool_name}' within {self._dedup_window}s window.",
                tool_name=tool_name,
            )

        # 5. 执行（含重试）
        last_error = ""
        for attempt in range(tool.max_retries + 1):
            try:
                result = tool.execute_fn(params)
                result.tool_name = tool_name
                result.latency_ms = (time.time() - start) * 1000

                # 记录去重缓存
                self._record_call(session_id, call_hash)

                # 灰名单：写审计日志
                if tool.safety_level == SafetyLevel.GRAYLIST:
                    self._audit(tool_name, params, result, session_id)

                return result

            except Exception as e:
                last_error = str(e)
                logger.warning("Tool {} attempt {} failed: {}", tool_name, attempt + 1, e)

        return ToolResult(
            success=False,
            error=f"Tool '{tool_name}' failed after {tool.max_retries + 1} attempts: {last_error}",
            tool_name=tool_name,
            latency_ms=(time.time() - start) * 1000,
        )

    # ----------------------------------------------------------------
    # 校验 & 去重 & 审计
    # ----------------------------------------------------------------

    def _validate_params(self, tool: ToolDef, params: dict) -> Optional[str]:
        """参数类型和必填校验"""
        for param_def in tool.params:
            value = params.get(param_def.name)

            # 必填检查
            if param_def.required and value is None:
                return f"Missing required param '{param_def.name}' for tool '{tool.name}'"

            # 类型检查
            if value is not None:
                type_map = {
                    "str": str,
                    "int": int,
                    "float": (int, float),
                    "bool": bool,
                    "dict": dict,
                    "list": list,
                }
                expected = type_map.get(param_def.type)
                if expected and not isinstance(value, expected):
                    return (
                        f"Param '{param_def.name}' expected type '{param_def.type}', "
                        f"got '{type(value).__name__}'"
                    )
        return None

    def _hash_call(self, tool_name: str, params: dict) -> str:
        """计算调用指纹（去重用）"""
        raw = f"{tool_name}:{json.dumps(params, sort_keys=True)}"
        return hashlib.md5(raw.encode()).hexdigest()

    def _is_duplicate(self, session_id: str, call_hash: str) -> bool:
        """检查是否在去重窗口内重复调用"""
        if session_id not in self._dedup_cache:
            return False
        last_time = self._dedup_cache[session_id].get(call_hash)
        if last_time is None:
            return False
        return (time.time() - last_time) < self._dedup_window

    def _record_call(self, session_id: str, call_hash: str):
        """记录调用时间戳"""
        if session_id not in self._dedup_cache:
            self._dedup_cache[session_id] = {}
        self._dedup_cache[session_id][call_hash] = time.time()

    def _audit(self, tool_name: str, params: dict, result: ToolResult, session_id: str):
        """灰名单审计日志"""
        from pathlib import Path
        audit_path = Path("logs") / "audit.jsonl"
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "session_id": session_id,
            "tool_name": tool_name,
            "params": {k: str(v)[:100] for k, v in params.items()},
            "success": result.success,
            "latency_ms": round(result.latency_ms, 1),
        }
        with open(audit_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ================================================================
# 内置工具定义
# ================================================================

def _create_search_kb_tool() -> ToolDef:
    """
    知识库检索工具 — 复用现有 RAG 管线。

    这是 Agent 最核心的工具：Agent Plan 决定搜索什么 → 调用此工具
    → 获取检索结果 → 基于结果规划下一步或直接回答。
    """

    def execute(params: dict) -> ToolResult:
        query = params["query"]
        top_k = params.get("top_k", 5)

        try:
            # 复用现有 Online Flow
            from flow import get_online_flow
            flow = get_online_flow()
            shared = {"query": query}

            import time as _time
            t0 = _time.time()
            flow.run(shared)
            latency = (_time.time() - t0) * 1000

            answer = shared.get("answer", "")
            sources = shared.get("sources", [])

            return ToolResult(
                success=True,
                data={
                    "answer": answer,
                    "sources": sources[:top_k],
                    "context": shared.get("context", "")[:1000],
                },
                latency_ms=latency,
            )
        except Exception as e:
            return ToolResult(success=False, error=str(e))

    return ToolDef(
        name="search_knowledge_base",
        description="检索知识库获取信息。适用于需要查找文档、概念解释、技术细节。",
        params=[
            ToolParam("query", "str", "检索查询语句"),
            ToolParam("top_k", "int", "返回结果数量", required=False, default=5),
        ],
        safety_level=SafetyLevel.WHITELIST,  # 纯读取，无副作用
        execute_fn=execute,
        category="retrieval",
    )


def _create_calculator_tool() -> ToolDef:
    """
    安全计算器 — 仅支持基本数学运算。

    使用受限的 eval 环境：只允许数字、运算符、math 函数。
    """

    # 安全表达式求值白名单
    _SAFE_LOCALS = {
        "abs": abs, "round": round, "min": min, "max": max,
        "sum": sum, "pow": pow, "sqrt": math.sqrt,
        "sin": math.sin, "cos": math.cos, "tan": math.tan,
        "log": math.log, "log10": math.log10, "log2": math.log2,
        "pi": math.pi, "e": math.e,
        "ceil": math.ceil, "floor": math.floor,
        "int": int, "float": float, "str": str,
    }

    def execute(params: dict) -> ToolResult:
        expression = params["expression"]

        try:
            # 编译表达式，只允许 eval 白名单
            code = compile(expression, "<calculator>", "eval")

            # 检查是否只使用了安全名称
            for name in code.co_names:
                if name not in _SAFE_LOCALS and name not in __builtins__:
                    return ToolResult(
                        success=False,
                        error=f"Unsafe name in expression: '{name}'. Allowed: {list(_SAFE_LOCALS.keys())}",
                    )

            result = eval(code, {"__builtins__": {}}, _SAFE_LOCALS)
            return ToolResult(success=True, data={"result": result, "expression": expression})

        except SyntaxError as e:
            return ToolResult(success=False, error=f"Syntax error: {e}")
        except Exception as e:
            return ToolResult(success=False, error=str(e))

    return ToolDef(
        name="calculator",
        description="安全数学计算器。支持基本算术、三角函数、对数等。示例: 'sqrt(16) + 2*pi'",
        params=[
            ToolParam("expression", "str", "数学表达式，例如 '2 + 3 * 4'"),
        ],
        safety_level=SafetyLevel.WHITELIST,  # 受限 eval，无副作用
        execute_fn=execute,
        category="utility",
    )


# ================================================================
# 默认注册中心
# ================================================================

def create_default_registry() -> ToolRegistry:
    """创建带默认工具的注册中心"""
    registry = ToolRegistry(dedup_window=30.0)

    # 注册内置工具
    registry.register(_create_search_kb_tool())
    registry.register(_create_calculator_tool())

    return registry


# 全局单例
tool_registry = create_default_registry()
