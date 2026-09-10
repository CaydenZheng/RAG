"""
工具安全与运行治理。

安全等级：
  WHITELIST → 自动执行（纯读取、纯计算，无副作用）
  GRAYLIST  → 审计记录后执行（可能读敏感信息，需留痕）
  BLACKLIST → 直接阻断 + 写审计日志（危险操作）

内置工具：
  search_knowledge_base — 直连统一 KnowledgeSystem，检索知识库
  calculator            — 安全数学表达式求值（受限 eval + 白名单）
"""

import asyncio
import hashlib
import json
import math
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from loguru import logger

from src.core.agent_runtime import ToolResult
from src.infra.tracer import tracer

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
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[Any, ...] | None = None
    min_length: int | None = None
    max_length: int | None = None


@dataclass
class ToolDef:
    """工具定义"""
    name: str                     # 唯一标识
    description: str              # LLM 理解工具用途的描述
    params: List[ToolParam]       # 参数列表
    safety_level: SafetyLevel     # 安全等级
    execute_fn: Callable          # 执行函数 (params: dict) -> ToolResult
    execute_async_fn: Callable | None = None
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
                    "default": p.default,
                    "minimum": p.minimum,
                    "maximum": p.maximum,
                    "choices": p.choices,
                    "min_length": p.min_length,
                    "max_length": p.max_length,
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
        """Validate and execute a tool synchronously."""
        start = time.time()
        prepared = self._prepare(tool_name, params, session_id)
        if isinstance(prepared, ToolResult):
            return prepared
        tool, call_hash = prepared
        for attempt in range(tool.max_retries + 1):
            try:
                result = tool.execute_fn(params)
                return self._complete(
                    tool, params, result, session_id, call_hash, start
                )
            except Exception as exc:
                logger.warning(
                    "Tool {} attempt {} failed: {}",
                    tool_name,
                    attempt + 1,
                    exc,
                )
        return ToolResult(
            success=False,
            error="Tool execution failed",
            error_code="tool_execution_failed",
            tool_name=tool_name,
            latency_ms=(time.time() - start) * 1000,
        )

    async def execute_async(
        self,
        tool_name: str,
        params: dict,
        session_id: str = "default",
    ) -> ToolResult:
        """Validate once, then run async tools directly and sync tools in a thread."""
        start = time.time()
        prepared = self._prepare(tool_name, params, session_id)
        if isinstance(prepared, ToolResult):
            return prepared
        tool, call_hash = prepared
        for attempt in range(tool.max_retries + 1):
            try:
                if tool.execute_async_fn is not None:
                    result = await tool.execute_async_fn(params)
                else:
                    result = await asyncio.to_thread(tool.execute_fn, params)
                return self._complete(
                    tool, params, result, session_id, call_hash, start
                )
            except Exception as exc:
                logger.warning(
                    "Tool {} async attempt {} failed: {}",
                    tool_name,
                    attempt + 1,
                    exc,
                )
        return ToolResult(
            success=False,
            error="Tool execution failed",
            error_code="tool_execution_failed",
            tool_name=tool_name,
            latency_ms=(time.time() - start) * 1000,
        )

    def _prepare(
        self, tool_name: str, params: dict, session_id: str
    ) -> tuple[ToolDef, str] | ToolResult:
        tool = self._tools.get(tool_name)
        if tool is None:
            return ToolResult(
                success=False,
                error="Unknown tool",
                error_code="unknown_tool",
                tool_name=tool_name,
            )
        param_error = self._validate_params(tool, params)
        if param_error:
            return ToolResult(
                success=False,
                error=param_error,
                error_code="invalid_tool_parameters",
                tool_name=tool_name,
            )
        if tool.safety_level == SafetyLevel.BLACKLIST:
            logger.warning("BLACKLIST tool blocked: {}", tool_name)
            return ToolResult(
                success=False,
                error="Tool blocked by safety policy",
                error_code="tool_blocked",
                tool_name=tool_name,
            )
        call_hash = self._hash_call(tool_name, params)
        if self._is_duplicate(session_id, call_hash):
            return ToolResult(
                success=False,
                error="Duplicate tool call",
                error_code="duplicate_tool_call",
                tool_name=tool_name,
            )
        return tool, call_hash

    def _complete(
        self,
        tool: ToolDef,
        params: dict,
        result: ToolResult,
        session_id: str,
        call_hash: str,
        start: float,
    ) -> ToolResult:
        result.tool_name = tool.name
        result.latency_ms = (time.time() - start) * 1000
        if not result.success and not result.error_code:
            result.error_code = "tool_failed"
        self._record_call(session_id, call_hash)
        if tool.safety_level == SafetyLevel.GRAYLIST:
            self._audit(tool.name, params, result, session_id)
        return result

    # ----------------------------------------------------------------
    # 校验 & 去重 & 审计
    # ----------------------------------------------------------------

    def _validate_params(self, tool: ToolDef, params: dict) -> Optional[str]:
        """Reject malformed and undeclared parameters before side effects."""
        if not isinstance(params, dict):
            return f"Params for tool '{tool.name}' must be an object"
        allowed = {param.name for param in tool.params}
        unexpected = sorted(
            str(name) for name in params if name not in allowed
        )
        if unexpected:
            return (
                f"Unexpected params for tool '{tool.name}': "
                f"{', '.join(unexpected)}"
            )
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
                wrong_type = expected and not isinstance(value, expected)
                if param_def.type == "int" and isinstance(value, bool):
                    wrong_type = True
                if wrong_type:
                    return (
                        f"Param '{param_def.name}' expected type '{param_def.type}', "
                        f"got '{type(value).__name__}'"
                    )
                if (
                    param_def.minimum is not None
                    and value < param_def.minimum
                ):
                    return (
                        f"Param '{param_def.name}' must be at least "
                        f"{param_def.minimum:g}"
                    )
                if (
                    param_def.maximum is not None
                    and value > param_def.maximum
                ):
                    return (
                        f"Param '{param_def.name}' must be at most "
                        f"{param_def.maximum:g}"
                    )
                if (
                    param_def.choices is not None
                    and value not in param_def.choices
                ):
                    return (
                        f"Param '{param_def.name}' must be one of "
                        f"{list(param_def.choices)}"
                    )
                if (
                    isinstance(value, str)
                    and param_def.min_length is not None
                    and len(value) < param_def.min_length
                ):
                    return (
                        f"Param '{param_def.name}' is shorter than allowed"
                    )
                if (
                    isinstance(value, str)
                    and param_def.max_length is not None
                    and len(value) > param_def.max_length
                ):
                    return (
                        f"Param '{param_def.name}' is longer than allowed"
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
        trace = tracer.current or {}
        record = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "request_id": trace.get("request_id", "unavailable"),
            "trace_id": trace.get("trace_id", "unavailable"),
            "tool_name": tool_name,
            "parameter_names": sorted(params),
            "success": result.success,
            "error_code": result.error_code,
            "latency_ms": round(result.latency_ms, 1),
        }
        with open(audit_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ================================================================
# 内置工具定义
# ================================================================

async def _execute_search_kb(params: dict) -> ToolResult:
    """Call KnowledgeSystem directly; Agent owns final answer generation."""
    from src.core.knowledge import (
        DEFAULT_RETRIEVAL_MODE,
        DEFAULT_RETRIEVAL_TOP_K,
        knowledge_system,
        validate_metadata_filter,
        validate_retrieval_mode,
        validate_retrieval_top_k,
    )

    query = params["query"]
    top_k = validate_retrieval_top_k(
        params.get("top_k", DEFAULT_RETRIEVAL_TOP_K)
    )
    metadata_filter = validate_metadata_filter(params.get("filter"))
    retrieval_mode = validate_retrieval_mode(
        params.get("retrieval_mode", DEFAULT_RETRIEVAL_MODE)
    )
    result = await knowledge_system.retrieve(
        query,
        top_k=top_k,
        metadata_filter=metadata_filter,
        mode=retrieval_mode,
    )
    sources: list[dict] = []
    snippets: list[str] = []
    for ref, chunk in enumerate(result.chunks[:top_k], 1):
        metadata = dict(chunk.get("metadata") or {})
        source = {
            "ref": ref,
            "chunk_id": chunk.get("chunk_id", ""),
            "source": metadata.get("source", chunk.get("source", "")),
            "version": metadata.get(
                "source_version", chunk.get("version", "")
            ),
            "score": chunk.get(
                "rerank_score", chunk.get("rrf_score", 0)
            ),
        }
        sources.append(source)
        snippets.append(
            f"[{ref}] {source['source'] or 'unknown'}\n"
            f"{chunk.get('text', '')}"
        )
    return ToolResult(
        success=True,
        data={
            "query": result.query,
            "context": "\n\n".join(snippets),
            "sources": sources,
            "warnings": list(result.warnings),
            "index_version": result.index_version,
        },
    )


def _create_search_kb_tool() -> ToolDef:
    """Create the read-only KnowledgeSystem retrieval adapter."""

    def execute(params: dict) -> ToolResult:
        return asyncio.run(_execute_search_kb(params))

    return ToolDef(
        name="search_knowledge_base",
        description="检索知识库获取信息。适用于需要查找文档、概念解释、技术细节。",
        params=[
            ToolParam(
                "query",
                "str",
                "检索查询语句",
                min_length=1,
                max_length=2000,
            ),
            ToolParam(
                "top_k",
                "int",
                "返回结果数量（1-20）",
                required=False,
                default=5,
                minimum=1,
                maximum=20,
            ),
            ToolParam(
                "filter",
                "dict",
                "Chroma metadata where 条件",
                required=False,
            ),
            ToolParam(
                "retrieval_mode",
                "str",
                "检索模式",
                required=False,
                default="hybrid+rerank",
                choices=(
                    "vector_only",
                    "bm25_only",
                    "hybrid",
                    "hybrid+rerank",
                ),
            ),
        ],
        safety_level=SafetyLevel.WHITELIST,
        execute_fn=execute,
        execute_async_fn=_execute_search_kb,
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
            ToolParam(
                "expression",
                "str",
                "数学表达式，例如 '2 + 3 * 4'",
                min_length=1,
                max_length=512,
            ),
        ],
        safety_level=SafetyLevel.WHITELIST,  # 受限 eval，无副作用
        execute_fn=execute,
        category="utility",
    )


def _create_weather_tool() -> ToolDef:
    """
    天气查询工具 — 调 wttr.in 免费 API，无需注册。

    安全等级 WHITELIST：纯外部 HTTP GET，无副作用。
    """

    def execute(params: dict) -> ToolResult:
        try:
            import urllib.error
            import urllib.request

            city = params["city"]
            url = f"https://wttr.in/{urllib.parse.quote(city)}?format=j1"

            req = urllib.request.Request(url, headers={"User-Agent": "RAGFlow-Agent/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                import json as _json
                data = _json.loads(resp.read().decode())

            current = data.get("current_condition", [{}])[0]
            result = {
                "city": city,
                "temperature_c": current.get("temp_C"),
                "humidity": current.get("humidity"),
                "weather_desc": current.get("weatherDesc", [{}])[0].get("value"),
                "wind_speed_kmh": current.get("windspeedKmph"),
                "feels_like_c": current.get("FeelsLikeC"),
            }
            return ToolResult(success=True, data=result)

        except urllib.error.HTTPError as e:
            return ToolResult(success=False, error=f"Weather API HTTP {e.code}")
        except Exception as e:
            return ToolResult(success=False, error=str(e))

    return ToolDef(
        name="get_weather",
        description="查询指定城市的实时天气（温度、湿度、风速、天气描述）。参数 city 为英文城市名，如 'Beijing'、'Tokyo'、'London'。",
        params=[
            ToolParam(
                "city",
                "str",
                "城市名（英文），例如 Beijing, Tokyo, London",
                min_length=1,
                max_length=100,
            ),
        ],
        safety_level=SafetyLevel.WHITELIST,
        execute_fn=execute,
        category="external",
    )


def _create_web_search_tool() -> ToolDef:
    """
    网页搜索工具 — 优先用 duckduckgo_search 包，fallback 用 HTML 抓取。

    安全等级 GRAYLIST：外部搜索需审计留痕。
    """

    def execute(params: dict) -> ToolResult:
        query = params["query"]
        max_results = params.get("max_results", 3)

        # 尝试 duckduckgo_search（v3.x，5 秒超时防卡死）
        try:
            import concurrent.futures

            from duckduckgo_search import DDGS

            def _ddg_search():
                results = []
                ddgs = DDGS()
                for r in ddgs.text(query):
                    results.append({
                        "title": r.get("title", ""),
                        "snippet": r.get("body", "")[:300],
                        "url": r.get("href", ""),
                    })
                    if len(results) >= max_results:
                        break
                return results

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(_ddg_search)
                results = future.result(timeout=5)

            if results:
                return ToolResult(success=True, data={"query": query, "results": results})
        except ImportError:
            logger.debug("duckduckgo_search not installed, using fallback")
        except Exception as e:
            logger.debug(
                "duckduckgo_search failed: {}; trying fallback", type(e).__name__
            )

        # Fallback: DDG Lite HTML
        try:
            import re
            import urllib.parse
            import urllib.request

            url = f"https://lite.duckduckgo.com/lite/?q={urllib.parse.quote(query)}"
            req = urllib.request.Request(url, headers={"User-Agent": "RAGFlow-Agent/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                html = resp.read().decode()

            results = []
            # 匹配每个结果块：一个带 nofollow 链接的 <tr> + 紧随的 <tr>
            block_pattern = (
                r'<tr[^>]*>\s*<td[^>]*>.*?'
                r'<a[^>]*rel="nofollow"[^>]*href="([^"]*)"[^>]*>([^<]+)</a>'
                r'.*?</td>\s*</tr>\s*'
                r'<tr[^>]*>\s*<td[^>]*>(.*?)</td>\s*</tr>'
            )
            for m in re.finditer(block_pattern, html, re.DOTALL | re.IGNORECASE):
                if len(results) >= max_results:
                    break
                raw_href, title, raw_snippet = m.group(1), m.group(2), m.group(3)
                # 提取真实 URL
                real_url = raw_href
                uddg = re.search(r'uddg=([^&]+)', raw_href)
                if uddg:
                    real_url = urllib.parse.unquote(uddg.group(1))
                # 清理 snippet
                snippet = re.sub(r'<[^>]+>', '', raw_snippet)
                snippet = re.sub(r'&nbsp;', ' ', snippet)
                snippet = re.sub(r'&amp;', '&', snippet)
                snippet = re.sub(r'&lt;', '<', snippet)
                snippet = re.sub(r'&gt;', '>', snippet)
                snippet = re.sub(r'&quot;', '"', snippet)
                snippet = re.sub(r'\s+', ' ', snippet).strip()
                snippet = re.sub(r'^Zero-click info:\s*', '', snippet)
                results.append({
                    "title": title.strip(),
                    "snippet": snippet[:300],
                    "url": real_url,
                })

            if results:
                return ToolResult(success=True, data={"query": query, "results": results})
            return ToolResult(success=False, error="No results found")

        except Exception as e:
            return ToolResult(success=False, error=f"Web search failed: {e}")

    return ToolDef(
        name="search_web",
        description="搜索互联网获取最新信息。当知识库中没有相关信息时使用。返回标题、摘要和链接。",
        params=[
            ToolParam(
                "query",
                "str",
                "搜索关键词",
                min_length=1,
                max_length=500,
            ),
            ToolParam("max_results", "int", "最多返回结果数", required=False, default=3),
        ],
        safety_level=SafetyLevel.GRAYLIST,
        execute_fn=execute,
        category="external",
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
    registry.register(_create_weather_tool())
    registry.register(_create_web_search_tool())

    return registry


# 全局单例
tool_registry = create_default_registry()
