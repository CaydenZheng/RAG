# RAG Agent 工程化改进报告

> 从"一问一答的 RAG 检索器"升级为"能规划、能调用工具、有记忆的 Agent"

---

## 1. Situation（背景与问题）

### 1.1 项目起点

`ragrag` 是一个基于 PocketFlow 的高级 RAG 系统，具备以下能力：

- 离线索引管线：文档摄入 → 去重 → 语义分块 → Embedding → ChromaDB + BM25 双路索引
- 在线检索管线：Query 改写 → 混合检索（向量 + BM25 → RRF 融合）→ bge-reranker 精排 → 答案生成
- 消融实验：4 组对照实验验证每一步优化价值
- 300 篇多领域 Wikipedia 作为知识库，FastAPI 服务化

### 1.2 核心问题

项目有一个根本缺陷：**它是"检索器"而非"智能体"**。

- 每次请求是独立的单轮问答，没有对话记忆
- 没有工具调用能力（LLM 只能回答，不能执行操作）
- 没有安全机制（无法区分安全/危险操作）
- 架构是线性的 Node 管道，缺乏横切关注点的解耦机制
- 缺乏系统化的评测基准（只有 4 组消融实验）

---

## 2. Task（目标定义）

### 2.1 核心目标

将 `ragrag` 从 RAG 检索器升级为具备以下能力的 Agent：

1. **自主规划**：LLM 判断当前需要检索知识库、做计算、还是直接回答
2. **工具调用**：不止检索，还能执行安全计算等操作
3. **对话记忆**：跨轮次记忆，上下文窗口自动管理
4. **安全防护**：三级审批机制，黑名单阻断危险操作
5. **系统评测**：≥8 条 benchmark 覆盖所有维度的定量验证

### 2.2 验收标准

| 指标 | 目标 |
|---|---|
| 单元测试通过率 | 6/6 |
| Agent Benchmark 通过率 | ≥7/8 |
| 多步推理正确性 | 先检索后计算，工具调用链路正确 |
| Agent 是否自动调用 RAG | 事实类问题必须调用 search_knowledge_base |
| 服务不崩溃 | 连续多轮多步推理不报错 |

---

## 3. Action（实施过程）

### 3.1 新建 4 个核心模块（`src/agent/`）

#### 3.1.1 Hook 事件拦截管线（`hooks.py`，~250 行）

**设计思路**：参考 Web 框架中间件模式，将横切关注点（日志、限流、审计、黑名单阻断）从核心 Agent 循环中解耦。

**核心实现**：

- 定义 9 个生命周期事件：`SESSION_START`、`SESSION_END`、`PRE_PLANNING`、`POST_PLANNING`、`PRE_TOOL_USE`、`POST_TOOL_USE`、`PRE_RETRIEVAL`、`POST_RETRIEVAL`、`PRE_GENERATION`、`POST_GENERATION`、`ON_ERROR`
- `HookPipeline` 类：有序注册 handler，按优先级 + 正则模式匹配触发
- `HookContext` 数据类：携带事件数据 + `blocked` 阻断标志，handler 可设 `blocked=True` 阻止后续执行
- 内置 4 个 handler：
  - `LoggingHook`：所有事件写 `logs/agent_events.jsonl`
  - `RateLimitHook`：滑动窗口限制每分钟 30 次工具调用
  - `AuditHook`：灰名单/黑名单工具调用写 `logs/audit.jsonl`
  - `BlacklistBlockHook`：匹配 `delete_.*`、`execute_code` 等危险模式直接阻断

**关键设计决策**：

- 模式匹配用正则，因为同类型工具（如所有 `delete_*`）有相同的安全级别
- 阻断不是抛异常，而是设 `ctx.blocked=True`，让调用方优雅处理
- 默认管线在 `create_default_pipeline()` 中组装，一行代码启用全部防护

#### 3.1.2 结构化记忆管理（`memory.py`，~310 行）

**设计思路**：采用两层记忆架构 + 分级压缩方案，在保持记忆连贯性的同时控制上下文窗口。

**两层记忆**：

| 层级 | 存储 | 注入方式 | 生命周期 |
|---|---|---|---|
| 长期记忆 | `memory/long_term.md` | 全量注入 system prompt | 跨 session 持久化 |
| 短期记忆 | `memory/sessions/{id}/history.json` | 最近 N 轮追加到 messages | 单 session，/new 清除 |

**三级压缩策略**：

1. **第一级**：工具调用结果截断。超过 500 token 的结果只保留前 200 + 后 200 token，中间标注省略量。
2. **第二级**：历史消息硬截断。超过 `max_history_turns`（默认 15 轮）时，最旧的直接丢弃。
3. **第三级**：LLM 摘要固化。达到 `compress_trigger_turns`（默认 10 轮）时，触发 LLM 将旧对话压缩为 2-5 句摘要，替换原始消息。

**验证数据**：单元测试中 12 轮对话压缩到 4 轮（LLM 摘要仅 26-54 chars，压缩比 ~30:1）。

**关键设计决策**：

- 压缩保留最近 5 轮不压缩（`compress_keep_recent=5`），保证最近上下文完整
- LLM 压缩失败时退化为硬截断（`old[-2:] + recent`），保证系统不崩溃
- `build_messages()` 自动将非标准 role 映射为 `user`，防止 API 拒绝

#### 3.1.3 工具安全与运行治理（`tools.py`，~370 行）

**设计思路**：采用三级审批 + RBAC 权限模型 + JSON Schema 风格参数校验。

**三级安全模型**：

| 级别 | 策略 | 代表工具 | 触发条件 |
|---|---|---|---|
| WHITELIST | 自动执行 | `search_knowledge_base`、`calculator` | 纯读取/纯计算，无副作用 |
| GRAYLIST | 审计后执行 | `read_file`、`web_search` | 可能有敏感操作，写审计日志 |
| BLACKLIST | 直接阻断 | `execute_code`、`delete_*` | 危险操作，阻断 + 写审计 |

**四重防御**：

1. **参数校验**：每个 `ToolDef` 定义 `params: List[ToolParam]`（名称、类型、必填/可选），执行前强制校验
2. **安全等级判断**：BLACKLIST 工具在 `ToolRegistry.execute()` 内直接拒绝
3. **去重机制**：同一 session 内相同工具+相同参数在 30s 窗口内不重复执行（MD5 指纹）
4. **审计日志**：所有 GRAYLIST/BLACKLIST 触发写 `logs/audit.jsonl`，独立于业务日志

**内置工具**：

- `search_knowledge_base`：复用现有 Online Flow（QueryRewriter → HybridRetriever → Reranker → ContextBuilder → Generator），Agent 调用一次即走完整 RAG 管线
- `calculator`：受限 `eval()` 白名单（仅 `math.*` + 基本运算符），防止代码注入

**关键设计决策**：

- 去重用 MD5 hash 而非内容对比，避免大参数比较的性能开销
- `calculator` 的 `compile()` + `co_names` 检查确保白名单之外的名字（如 `__import__`）无法被调用

#### 3.1.4 Agent 核心循环（`harness.py`，~360 行）

**设计思路**：实现 Plan → Execute → Observe 循环，将 RAG 管线作为 Agent 的一个工具而非唯一能力。

**核心循环流程**：

```
用户输入
  │
  ├─ 1. SESSION_START Hook
  ├─ 2. build_messages（system prompt + 长期记忆 + 历史 + 用户消息）
  │
  └─ 3. [循环，max 5 轮]
       │
       ├─ 3a. PRE_PLANNING Hook → LLM 规划
       │     输出 JSON: {"action":"tool_call",...} 或 {"action":"final_answer",...}
       │
       ├─ 3b. 如果是 tool_call:
       │     ├─ PRE_TOOL_USE Hook → 安全检查 → 执行 → 结果截断
       │     ├─ 结果注入 messages + 保存到记忆
       │     └─ POST_TOOL_USE Hook
       │
       └─ 3c. 如果是 final_answer:
             ├─ PRE_GENERATION Hook
             ├─ 答案保存到记忆
             └─ POST_GENERATION Hook → 跳出循环
  │
  ├─ 4. 达到 max 迭代 → _force_final_answer() 强制产出答案
  └─ 5. SESSION_END Hook → 返回 AgentResponse
```

**Planner Prompt 设计（经过 3 轮迭代优化）**：

- v1（失败）：礼貌性提示 → LLM 不遵守 JSON 格式，直接返回纯文本
- v2（失败）：加 `You MUST respond with a valid JSON object` → 仍有时忽略
- v3（最终）：`Your ENTIRE response must be a single JSON object — no text before or after` + 单行紧凑格式 + 明确指令 `For ANY factual question, you MUST call search_knowledge_base. NEVER answer from your own knowledge`

**JSON 解析兜底（三级）**：

1. 尝试提取 Markdown 代码块中的 JSON
2. 直接 `json.loads()` 解析
3. 正则 `re.search(r'\{[^{}]*"action"\s*:\s*"(?:tool_call|final_answer)"[^{}]*\}', raw)` 匹配 JSON 对象片段
4. 全部失败则全文作为 `final_answer`，降级不崩溃

### 3.2 新增 API 端点（`app.py`）

| 端点 | 方法 | 功能 |
|---|---|---|
| `/agent/chat` | POST | Agent Plan-Execute-Observe 对话 |
| `/agent/reset` | POST | 重置会话记忆（`/new` 命令） |
| `/agent/memory/{id}` | GET | 查看会话记忆（调试用） |

返回格式：

```json
{
  "session_id": "abc123",
  "answer": "Actinium-227 的半衰期是 21.772 年...",
  "tool_calls": [
    {"tool": "search_knowledge_base", "success": true, "latency_ms": 2169.8},
    {"tool": "calculator", "params": {"expression": "21.772 * 365.25"}, "success": true}
  ],
  "iterations": 3,
  "latency_ms": 6018.6
}
```

### 3.3 评测体系（`tests/test_agent.py`，~500 行）

**8 条 Benchmark 覆盖 4 个维度**：

| 维度 | 用例 | 验证点 |
|---|---|---|
| 规划正确性 | `planning_01` 事实检索 | Agent 必须调用 search_knowledge_base |
| 规划正确性 | `planning_02` 闲聊 | Agent 应直接回答，不调用工具 |
| 规划正确性 | `planning_03` 数学计算 | Agent 必须调用 calculator |
| 工具安全 | `safety_01` delete_files | 被拒绝（LLM 层 + Hook 层双重防护） |
| 工具安全 | `safety_02` execute_code | 被拒绝 |
| 记忆 | `memory_01` 记忆召回 | Agent 能处理跨轮次引用 |
| 端到端 | `e2e_01` 多步推理 | 先 search 知识库 → 得半衰期 21.772 年 → calculator 算天数 |
| 端到端 | `e2e_02` Fresnel 原理 | search 知识库 → 答案带引用标注 [2] |

**6 项指标**：

| 指标 | 值 |
|---|---|
| pass_rate | 100%（8/8） |
| avg_tool_rounds | 1.8 |
| avg_latency | 8694ms（含首次冷启动加载模型） |
| failure_category | 0 类失败 |
| tool_trace | 多步推理链路正确：search→calculator→answer |
| verifier_reason | 每条用例均附 LLM 输出供人工核查 |

**6 项单元测试**：

| 测试 | 验证内容 |
|---|---|
| test_hook_pipeline | 黑名单 `delete_files` 阻断，白名单 `search_knowledge_base` 放行 |
| test_memory_compress_trigger | 12 轮对话自动压缩到 4 轮 |
| test_tool_validation | 缺必填参数报错、类型错误报错 |
| test_tool_blacklist | BLACKLIST 工具直接被拒 |
| test_tool_dedup | 同一 session 相同调用 30s 内去重 |
| test_agent_response_structure | 数据结构完整性 |

### 3.4 Bug 修复（3 轮迭代）

#### 第 1 轮：Agent 不调用工具

**现象**：事实类问题 `"What is the half-life of actinium?"` 返回 `tool_calls: []`，LLM 直接用自身知识回答，未走 RAG 管线。

**根因**：LLM 未遵守 JSON 输出格式，直接返回纯文本答案。`_parse_plan_json()` 解析失败后降级为 `final_answer`。

**修复**：
1. Planner prompt 从温和的 "Always try to answer with knowledge_base" 改为强制的 "For ANY factual question, you MUST call search_knowledge_base. NEVER answer from your own knowledge"
2. 新增正则兜底解析：`re.search(r'\{[^{}]*"action"\s*:\s*"(?:tool_call|final_answer)"[^{}]*\}', raw)`

#### 第 2 轮：BadRequestError 崩溃

**现象**：Agent 第一次工具调用成功后，第二次 planning 时服务崩溃，curl 返回 `Connection was reset`。

**根因**：工具结果以 `role: "tool"` 注入消息列表。DeepSeek API 只接受 `system/user/assistant` 三种角色。

**修复**：
1. 工具结果和阻断消息的 role 从 `"tool"` 改为 `"user"`
2. `memory.add_turn()` 的 role 同步改为 `"user"`
3. `memory.build_messages()` 增加 role 映射：非 `user/assistant/system` 的 role 一律映射为 `"user"`

#### 第 3 轮：Reranker 重复加载

**现象**：Agent 多轮调用中 Reranker（CrossEncoder）被多次加载。

**根因**：`RerankerNode.__init__()` 每次创建实例时设 `self._reranker = None`，PocketFlow 在某些场景下重建节点。

**修复**：CrossEncoder 从实例变量改为模块级 `_get_reranker()` 单例函数，进程内全局唯一。

---

## 4. Result（成果与量化）

### 4.1 功能成果

| 能力 | 改进前 | 改进后 |
|---|---|---|
| 对话模式 | 单轮一问一答 | 多轮对话 + 记忆持久化 |
| 工具调用 | 无 | 检索知识库 + 安全计算器，可扩展 |
| 安全防护 | 无 | 三级审批 + 黑名单阻断 + 审计日志 |
| 横切关注点 | 与业务逻辑耦合 | Hook 管线完全解耦 |
| 上下文管理 | 无 | 三级压缩 + LLM 摘要归档 |
| 评测体系 | 4 组消融实验 | 8 条 benchmark + 6 单元测试 |

### 4.2 量化指标

```
==================== FINAL SUMMARY ====================
Unit tests:      6/6  passed
Benchmark:       8/8  passed (100%)
Avg tool rounds: 1.8
Avg latency:     8694ms
Report:          data/agent_benchmark_report.json
============================================================
```

### 4.3 端到端验证

**事实检索**：
```json
"tool_calls": [{"tool": "search_knowledge_base", "success": true}],
"iterations": 2,
"answer": "Actinium has several isotopes with different half-lives: ²²⁷Ac: 21.772 years..."
```

**多步推理**：
```json
"tool_calls": [
  {"tool": "search_knowledge_base", "success": true},
  {"tool": "calculator", "params": {"expression": "21.772 * 365.25"}, "success": true}
],
"iterations": 3,
"answer": "21.772 年 × 365.25 天/年 ≈ 7952.22 天"
```

Agent 自主完成：检索 Wikipedia → 提取半衰期 21.772 → 调用计算器 × 365.25 → 输出最终答案。

### 4.4 新增/修改文件清单

| 操作 | 文件 | 行数 | 说明 |
|---|---|---|---|
| **新建** | `src/agent/__init__.py` | 18 | 模块入口 |
| **新建** | `src/agent/hooks.py` | 250 | Hook 事件拦截管线 |
| **新建** | `src/agent/memory.py` | 310 | 两层记忆 + 三级压缩 |
| **新建** | `src/agent/tools.py` | 370 | 工具注册 + 三级审批 + 去重 |
| **新建** | `src/agent/harness.py` | 360 | Agent 核心循环 |
| **新建** | `flow_agent.py` | 110 | Agent 的 PocketFlow 编排 |
| **新建** | `tests/test_agent.py` | 500 | Benchmark + 单元测试 |
| **修改** | `config/settings.py` | +5 字段 | Agent 配置项 |
| **修改** | `app.py` | +60 行 | 新增 3 个 Agent API 端点 |
| **修改** | `src/core/retrieval.py` | Reranker 单例化 | 避免重复加载 |

总计：新增 ~1900 行代码，修改 ~70 行。
