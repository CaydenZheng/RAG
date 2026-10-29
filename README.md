# RAGFlow

基于 FastAPI、PocketFlow 和本地检索模型构建的 RAG 与 Agent 应用。项目提供版本化文档索引、混合检索、带引用回答、会话隔离、Agent 工具调用、SSE 流式输出、可观测性和可复现评测。

当前代码面向本地开发和小规模服务部署。默认评测数据仍是未经人工核验的 AI 生成候选，不能把开发集结果当作正式质量结论。

导航：[架构](#架构) · [配置](#配置) · [MCP](docs/mcp.md) · [索引与回滚](#索引生命周期) · [测试与 CI](#测试与-ci) · [评测](#评测) · [排障](#排障) · [已知限制](#已知限制) · [ADR 0001](docs/adr/0001-core-seams.md)

## 核心能力

| 能力 | 当前行为 |
|---|---|
| RAG 查询 | 查询改写、Dense／BM25、RRF、可选 Rerank、上下文预算和引用约束 |
| Agent | JSON／原生 Tool Calling 可配置、有界迭代、SQLite 短期历史、同步结果与 SSE 共用事件模型 |
| MCP | 官方 SDK 的 stdio 与 Streamable HTTP、OAuth、Elicitation、动态工具发现及统一安全链路 |
| 索引 | 上传、删除、全量重建、版本化 collection、原子发布和回滚 |
| 可靠性 | 并发上限、总时限、有限重试、稳定错误码和显式降级警告 |
| 安全 | 客户端会话隔离、metadata filter 校验、上传校验、工具参数和输出约束 |
| 可观测性 | request/trace ID、阶段 Span、延迟、模型用量、缓存命中和索引版本 |
| 评测 | 版本化数据、确定性检索／引用指标、可选 Ragas、逐样本可复现报告 |

## 架构

```mermaid
flowchart LR
    Client[Web / HTTP Client] --> API[FastAPI entry]
    API --> RAGFlow[PocketFlow adapters]
    RAGFlow --> Knowledge[KnowledgeSystem.retrieve]
    Knowledge --> Retrieval[Rewrite / Dense / BM25 / RRF / Rerank]
    RAGFlow --> Answer[AnswerService]
    API --> Runtime[AgentRuntime]
    Runtime --> Tools[ToolRegistry]
    MCPServers[MCP Servers] --> MCP[MCP Client Manager]
    MCP --> Tools
    Tools --> Knowledge
    API --> Jobs[IndexJobs]
    Jobs --> Indexing[Ingestion and versioned indexing]
    Eval[EvaluationRunner] --> Knowledge
    Eval --> Answer
```

核心逻辑集中在少量稳定接口：

- `KnowledgeSystem.retrieve` 隐藏查询改写、候选召回、融合、精排和降级。
- `AnswerService` 统一普通回答与流式回答的上下文、引用和会话落盘语义。
- `AgentRuntime` 统一普通 Agent 响应、事件流和会话重置。
- `MCPRuntime` 管理可选 MCP Server 的后台启动、关闭、工具注册和脱敏状态。
- `IndexJobs` 统一索引任务提交、幂等、状态查询和后台执行。
- `EvaluationRunner` 直接复用检索与生成接口，不另建一套评测专用 RAG。

PocketFlow 只保留离线索引和 RAG 顶层编排。架构决策见 [ADR 0001](docs/adr/0001-core-seams.md)。

## 目录结构

```text
ragrag/
├── app.py                    # FastAPI 入口与 HTTP 路由
├── frontend/                 # React + TypeScript + Vite 前端源码与测试
├── config/settings.py        # 环境配置及类型校验
├── prompts/                  # 版本化 Prompt
├── scripts/                  # 索引、数据和评测命令
├── src/
│   ├── agent/                # Agent 循环、工具、Hook 和记忆
│   ├── mcp_servers/          # 官方 SDK 实现的本地 MCP Server
│   ├── api/                  # HTTP schema、中间件、SSE 与启动逻辑
│   ├── core/                 # 检索、生成、索引和 Agent 核心接口
│   ├── evaluation/           # 数据规则、指标、Runner 和可选 Judge
│   ├── infra/                # SQLite、Chroma、任务、Trace 等 Adapter
│   ├── orchestration/        # PocketFlow 顶层编排
│   ├── security/             # 会话标识与安全校验
│   ├── utils/                # RRF、BM25 和 Token 工具
│   └── web/                  # FastAPI Web 入口与本地构建产物目录
├── data/testset/             # 版本化评测数据和审核说明
└── tests/                    # 默认离线测试与显式真实模型检查
```

运行数据库、索引、日志、模型缓存、评测输出和个人辅助资料不进入 Git。

## 快速开始

### 1. 安装

要求 Python 3.13、uv 0.12.11 或更高版本，以及 Node.js 22.12～24。

```powershell
uv sync --locked --no-default-groups --group dev
Copy-Item .env.example .env
Set-Location frontend
npm ci
Set-Location ..
```

如果多个项目共用虚拟环境，可以显式指定环境，不必在仓库内创建 `.venv`：

```powershell
$env:UV_PROJECT_ENVIRONMENT = "C:\path\to\shared\.venv"
uv sync --locked --no-default-groups --group dev --inexact
```

编辑 `.env`，至少设置模型 Provider：

```dotenv
OPENAI_API_KEY=your-key
OPENAI_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-chat
LOCAL_EMBEDDING_MODEL=BAAI/bge-base-en-v1.5
RERANK_MODEL=BAAI/bge-reranker-base
ADMIN_API_KEY=replace-with-a-long-random-secret
CHROMA_PERSIST_DIR=C:\ragflow-data\chroma
```

> Windows 上，Chroma 1.5.9 可能无法重新加载位于中文路径中的 HNSW 文件。建议让 `CHROMA_PERSIST_DIR` 指向纯英文绝对路径。

### 2. 构建索引

将 UTF-8 编码的 `.txt` 或 `.md` 文档放入 `data/raw/`，然后执行：

```powershell
uv run --no-sync python scripts/build_index.py
```

首次运行会加载本地 Embedding 模型。构建完成后会输出文档数、分块数、版本、校验和及 collection 名称。

### 3. 启动

生产式本地启动先构建前端，再启动 FastAPI：

```powershell
Set-Location frontend
npm run build
Set-Location ..
uv run --no-sync uvicorn app:app --host 127.0.0.1 --port 8000
```

Vite 构建输出到被 Git 忽略的 `src/web/dist`。缺少构建产物时，业务
接口仍可启动，`/` 和 `/agent` 会返回包含上述构建命令的 503。前端开发
时可在 `frontend` 中运行 `npm run dev`，Vite 会把同源 API 路径代理到
`127.0.0.1:8000`。

前端开发使用两个终端。先在仓库根目录启动后端：

```powershell
uv run --no-sync uvicorn app:app --host 127.0.0.1 --port 8000
```

再启动 Vite：

```powershell
Set-Location frontend
npm run dev -- --host 127.0.0.1
```

开发入口为 <http://127.0.0.1:5173/> 和
<http://127.0.0.1:5173/agent>；Vite 只把真实 API 路径代理到 FastAPI，
`/agent` 页面本身仍由 Vite 提供热更新。页面支持跟随系统、浅色和深色主题，
主题偏好只保存在本地浏览器中。

启动后访问：

- RAG 页面：<http://127.0.0.1:8000/>
- Agent 页面：<http://127.0.0.1:8000/agent>
- OpenAPI 文档：<http://127.0.0.1:8000/docs>

默认 Web 工作台仍是本地匿名单用户模式。`AUTH_ENABLED=true` 的邀请制账号候选
提供登录、初始改密、独立历史和新建／清空／删除，需统一 Review 后受控启用。
浏览器公开 session ID 只定位对话，不是身份；Cookie 凭据只由服务端验证。
本轮不授权迁移真实数据或将站点作为多用户公网服务开放，详见 [账号与历史](docs/authentication.md)。

账号工作台新增 Flash／Pro 会话模型选择与 Skills 管理；配置、文件格式和转换边界见 [双模型与 Skills](docs/workspace-models-skills.md)。Pages 对应交互仍为明确标识的模拟。

运行状态接口（供脚本或部署检查）：

- 存活检查：`GET /health`
- 就绪检查：`GET /ready`

启动后由一个后台线程按 Embedding → 活跃向量索引／BM25 → 可选 Reranker 的顺序预热，避免多个模型并发加载造成瞬时内存峰值。`/health` 只表示 HTTP 进程存活；`/ready` 在 Embedding 或索引尚未就绪／加载失败时返回 503，必需组件就绪但 BM25 或 Reranker 降级时返回 200 和 `degraded`。低内存机器可设置 `STARTUP_PRELOAD_RERANKER=false`，让精排模型在首次使用 `hybrid+rerank` 时按需加载。

## 配置

环境变量模板见 [`.env.example`](.env.example)。常用配置如下：

| 配置 | 用途 |
|---|---|
| `OPENAI_API_KEY`、`OPENAI_BASE_URL`、`LLM_MODEL` | 兼容 OpenAI 协议的生成模型 |
| `LOCAL_EMBEDDING_MODEL` | 本地向量模型，必须与既有索引维度和语义一致 |
| `RERANK_MODEL`、`RERANK_TIMEOUT_SECONDS`、`STARTUP_PRELOAD_RERANKER` | 本地精排模型、单次时限及是否启动预热 |
| `CHROMA_PERSIST_DIR` | Chroma 持久化目录 |
| `CACHE_DB_PATH`、`CACHE_MAX_ENTRIES` | 精确 LLM 缓存 SQLite 文件及最大记录数 |
| `VECTOR_TOP_K`、`BM25_TOP_K`、`RRF_K`、`RERANK_TOP_K` | 候选召回、融合和精排预算 |
| `ABSTENTION_THRESHOLDS`、`ABSTENTION_CALIBRATION_ID`、`ABSTENTION_CALIBRATION_MODELS` | 独立人工核验 calibration 报告校准的分模式阈值、报告哈希及模型绑定 |
| `MAX_CONTEXT_TOKENS`、`SYSTEM_RESERVE_RATIO`、`CONTEXT_BUFFER_RATIO` | 上下文 Token 预算 |
| `MAX_CONCURRENT_QUERIES`、`REQUEST_TIMEOUT_SECONDS`、`LLM_MAX_RETRIES` | 请求容量、总时限和 Provider 重试 |
| `ADMIN_API_KEY`、`ALLOW_UNAUTHENTICATED_ADMIN` | 索引管理密钥及仅限本地开发的显式免认证开关 |
| `AGENT_MAX_ITERATIONS`、`AGENT_MAX_TOOL_CALLS`、`AGENT_MAX_TOKEN_BUDGET` | Agent 运行预算；规划/最终回答的完整输入按 token 近似计数加 10% 余量，与预留最大输出累计，不作为 Provider 计费数据 |
| `AGENT_PLANNER_MODE` | `json` 为兼容默认；`native` 使用 OpenAI-compatible 原生 Tool Calling |
| `AGENT_TIMEOUT_SECONDS`、`AGENT_PLANNER_MAX_TOKENS`、`AGENT_FINAL_MAX_TOKENS` | Agent 时限和生成预算；规划默认 512，最终回答默认 4096 token |
| `AGENT_TOOL_APPROVAL_TIMEOUT_SECONDS` | Graylist 工具等待调用方批准的时限，默认 25 秒 |
| `AGENT_TOOL_ALLOWLIST`、`AGENT_TOOL_DENYLIST` | 工具精确注册名 JSON 数组；空 allowlist 默认允许，denylist 优先 |
| `TOOL_DEDUP_MAX_SESSIONS` | 工具调用去重状态的最大 session 数 |
| `MCP_SERVERS` | 可选 MCP Server JSON 列表；支持 stdio、Streamable HTTP 及可选标准 OAuth |
| `AGENT_LOG_MAX_BYTES`、`AGENT_LOG_BACKUP_COUNT`、`AGENT_LOG_RETENTION_SECONDS` | Agent 事件、工具审计、Trace 与 MCP 事件 JSONL 的轮转和保留边界 |
| `PROMPT_VERSION` | 选择 `prompts/<version>/` |
| `LANGFUSE_*` | 预留的远端观测字段；当前运行时未接入 |
| `MAX_UPLOAD_BYTES` | 单文件上传上限 |

配置在进程启动时由 Pydantic 校验。默认必须设置 `ADMIN_API_KEY`；只有服务绑定 `127.0.0.1` 且仅供本机开发时，才可显式设置 `ALLOW_UNAUTHENTICATED_ADMIN=true`。不要在对外监听时启用免认证，也不要提交 `.env`、密钥或本地数据库。

使用官方 `api.deepseek.com` 端点时，生成客户端显式发送
`thinking.type=disabled`。DeepSeek V4 默认开启的思考会占用同一个
`max_tokens` 上限，小预算可能在产生正文前耗尽。该参数只发送给官方
DeepSeek 端点，其他兼容端点保持原行为。精确缓存按此生成合同隔离。
Trace 仅记录 Provider 提供的思考 token 数，不记录思考正文；未报告时为空。

Agent 规划默认 512 token，最终回答默认 4096 token；它们仍受总预算和
总时限限制。Token 不是字数：旧 1024 上限不足以完成约 1500 词的教程。
已有 `.env` 中显式设置的 `AGENT_FINAL_MAX_TOKENS=1024` 会覆盖新默认值，
需要长回答时改为 `4096` 并重启后端。达到上限的未完成回答仍按失败处理，
不缓存、不保存为成功历史。更长正文或多轮工具任务仍需按实际用量配置预算。

## RAG 接口

### 普通查询

```powershell
curl.exe -X POST http://127.0.0.1:8000/query `
  -H "Content-Type: application/json" `
  -d "{\"query\":\"What is reciprocal rank fusion?\",\"top_k\":5,\"retrieval_mode\":\"hybrid+rerank\"}"
```

请求字段：

- `query`：必填问题，长度为 1～2000 个字符。
- `session_id`：可选；为空时不保存 RAG 历史。
- `top_k`：1～20，控制最终证据数量。
- `retrieval_mode`：`vector_only`、`bm25_only`、`hybrid` 或 `hybrid+rerank`。
- `filter`：受限的 Chroma metadata 条件。

响应包含 `query_id`、`answer`、`sources`、`warnings`、`index_version` 和 `latency_ms`。调用方必须展示 `warnings`。当前稳定 warning code 为：

- `bm25_unavailable`：请求了 BM25，但索引尚未就绪。
- `bm25_version_mismatch`：BM25 索引与本次请求固定的 Dense 索引版本不一致。
- `rerank_timeout`：Reranker 超时，结果退化为融合排序。
- `rerank_unavailable`：Reranker 调用失败，结果退化为融合排序。

BM25 正常参与但没有命中时不返回 warning；`vector_only` 模式不会返回 BM25 warning。

### 流式查询

```text
GET /query/stream?query=...&session_id=...&top_k=5&retrieval_mode=hybrid%2Brerank
```

SSE 使用 `chunk`、`done` 和 `error` 终止语义。只有收到 `done` 才表示回答已完整生成并保存。

### 会话

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/session/{session_id}` | 查看当前客户端可访问的 RAG 历史 |
| `POST` | `/session/reset?session_id=...` | 清除当前客户端的 RAG 会话 |

公开 session ID 会绑定浏览器客户端身份；另一个客户端即使知道 ID，也不能读取或清除该会话。

## Agent

Agent 通过同一个 `AgentRuntime` 生成普通响应和 SSE 事件。当前默认工具包括：

- `search_knowledge_base`：调用 `KnowledgeSystem.retrieve`，只返回证据。
- `calculator`：执行受限算术表达式。
- `get_weather`：获取天气信息。
- `search_web`：受控外部搜索，属于灰名单工具。

工具调用会校验名称、Host allowlist/denylist、参数、调用次数和总预算；不可信工具输出带明确边界并截断后再交给模型。`AGENT_PLANNER_MODE=json` 保持兼容的 JSON Planner；设为 `native` 时通过 OpenAI-compatible SDK 发送标准工具 Schema，并用 `assistant.tool_calls` 与 `tool_call_id` 续接同一轮调用。两种模式共用 ToolRegistry、预算、Hook、审计、参数校验和结果安全链路。普通与流式 Agent 的 `message` 长度均为 1～2000 个字符。

过去已完成的问答仅作为规划阶段的上下文数据，不作为规划器自己的 assistant 输出范例；最终回答阶段保留自然语言对话角色，持久历史不改写为规划 JSON。扣除下一次规划输入（含 native Schema）前，先检查该次规划与完整最终回答的合计费用；工具检查还保留有界结果的上下文空间。已有工具观察且只够收尾时，跳过后续规划或新工具，用已有结果生成明确说明限制的最终回答；不扣未发出的规划费用，也不追加未执行的 native 工具调用。没有观察或连收尾额度也不足时，返回预算错误。不调高默认上限、不自动重放工具；该运行预算仍是带余量的估算，独立于账号每日硬额度。

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/agent/chat` | 普通 Agent 对话 |
| `POST` | `/agent/chat/stream` | `planning → tool_call → tool_done → chunk → done` 事件流；Planner 确认无需继续调用工具后，答案按 Provider 增量实时发送 |
| `GET` | `/agent/elicitation/{session_id}` | 查询当前客户端 Agent session 的待处理 MCP Elicitation |
| `POST` | `/agent/elicitation/{session_id}/{elicitation_id}` | 提交 `accept`、`decline` 或 `cancel`；成功响应不回显表单内容 |
| `POST` | `/agent/reset?session_id=...` | 清除当前客户端的 Agent 会话 |
| `GET` | `/agent/memory/{session_id}` | 查看受当前客户端约束的 SQLite 短期历史 |

配置 MCP 后，发现的工具会以 `mcp__{server_id}__{tool_name}` 注册到同一个 ToolRegistry；未配置时保持上述原生工具行为。MCP 工具来源与 Server 状态可通过 `GET /agent/tools` 查看。

`AGENT_TOOL_ALLOWLIST` 与 `AGENT_TOOL_DENYLIST` 使用工具在 Registry 中的精确名称，例如 `["calculator","mcp__clock__get_current_time"]`。allowlist 为空时保持默认兼容；非空时只有列出的工具可见和可执行，denylist 在重叠时优先。策略同时作用于原生和 MCP 工具、JSON 与 native Planner 目录以及同步/异步 Registry 调用。启用任一列表后，允许、拒绝及后续 Hook 阻断结果都会进入有界 `logs/audit.jsonl`，只记录参数名和稳定策略原因，不记录参数值。参数名最多记录 32 个、单名 128 UTF-8 bytes、合计 256 bytes；任何编码后无法容纳在单文件上限内的 JSONL 记录都会在写入和轮转前安全丢弃并告警，不会突破配置的文件大小。它是单进程 Host 基础策略，不是角色、租户或资源级授权系统。

若强制最终回答生成失败，Agent 返回 `agent_final_generation_failed`，记录失败 Trace，且不写入本轮历史。

RAG 与 Agent 在请求开始使用历史前捕获 session epoch；reset 后旧请求失去写入资格，不能重建清空的历史。Agent 的规划使用 `AGENT_PLANNER_MAX_TOKENS`，最终回答始终使用 `AGENT_FINAL_MAX_TOKENS`，并共同受整体预算限制。总时限包含记忆保存与压缩；同步摘要不能被强制终止，但取消或过期后的事务不得提交迟到历史。带资格守卫的 SQLite 提交不会在 SQLite 内部等待读锁：忙锁时短周期重试，每次检查取消/deadline，失效回滚；原连接的等待上限和 busy_timeout 配置仍保留。Ollama 降级也必须确认完整完成，不能将截断文本当成成功回答。

Registry 在审批/执行前原子声明同会话、同工具、同参数指纹的执行中占位；重复调用返回 `duplicate_tool_call`，不发起第二次审批或执行。实际退出后，无论成功、失败、拒绝、取消或超时，均沿用配置的去重冷却窗口（默认 30 秒），窗口后可重试；`dedup_window=0` 只关闭完成后的冷却，不关闭执行中保护。取消同步工具的等待不会终止线程，占位保留到线程真正退出，再开始冷却。`max_sessions` 同时限制完成缓存的会话数和执行中声明总数，后者满载返回 `tool_capacity_exceeded`，不驱逐仍运行的声明。这是单 Registry 实例内同步/异步调用共享的进程内去重，不保证多实例、进程重启或远端副作用的严格幂等；未知执行结果的重试仍需要工具侧幂等键或人工确认。

示例：

```powershell
curl.exe -X POST http://127.0.0.1:8000/agent/chat `
  -H "Content-Type: application/json" `
  -d "{\"message\":\"Search the knowledge base for Ada Lovelace and summarize the evidence.\"}"
```

## MCP

MCP 默认关闭。设置 `MCP_SERVERS` 后，应用会在后台连接启用的 Server；连接失败或握手等待不会阻塞核心 HTTP 服务。stdio 和 Streamable HTTP 共用工具注册、完整 JSON Schema 校验、结果安全、审计和状态模型，工具调用默认不自动重试。

原生与 MCP 的 Graylist 工具在参数校验后、任何副作用前进入同一条轻量审批链路。调用方应显式提供稳定的 Agent `session_id`，并在另一个并发请求中用 `GET /agent/approvals/{session_id}` 查询 pending，再向 `POST /agent/approvals/{session_id}/{approval_id}` 提交 `approve`、`reject` 或 `cancel`。批准后只会重新校验并执行调用方实际看到的有界参数快照，等待期间对原始参数对象的修改不会改变执行内容；Registry 还会确认工具定义未被替换、仍可用且仍为 Graylist，MCP 重新发现或连接故障导致的注销、替换和禁用会稳定 fail closed。一次批准最多发起一次执行；即使副作用完成后确认响应丢失，也不会按 `max_retries` 自动重放。拒绝、审批取消、审批超时，以及审批完成前发生的调用取消或应用关闭，都不会启动工具。执行已经开始后，取消仍会传播并写入终态审计，但不能强制终止已在线程中运行的同步工具；需要可撤销副作用时，工具自身必须支持取消，或由业务提供幂等与补偿。Whitelist 仍自动执行，Blacklist 仍直接阻断。

内置时间 Server 可通过 stdio 开箱演示；远程 URL 使用 Streamable HTTP，并可选用官方 SDK 的 OAuth Authorization Code + PKCE。MCP Server 发起 Elicitation 时，调用方可通过 Agent session 查询并提交有效响应；当前使用独立 HTTP 请求协调，不把单向 SSE 描述成双向通道。Host 独立限制响应请求体和 form 内容大小，超时、关闭与响应竞争只会接受一个终态。配置示例、授权与 Elicitation 流程、具体资源上限、官方 Inspector 命令、真实传输测试和明确的能力边界见 [MCP 集成](docs/mcp.md)。

可观测入口：

- `GET /agent/tools`：原生与 MCP 工具来源、Provider、分类、安全等级、可用性和脱敏 Server 状态。
- `GET /ready`：MCP 作为可选组件汇总；MCP 不可用时核心服务仍按自身状态报告 readiness。
- `GET /agent/oauth/{server_id}`：管理员读取待处理授权 URL；回调端点公开接收 Provider 重定向，state 仍由 Host 与 SDK 双重校验。
- `GET /agent/elicitation/{session_id}` 与对应 `POST`：仅当前客户端可见的进程内 MCP Elicitation 查询与响应。
- `GET /agent/approvals/{session_id}` 与对应 `POST`：仅当前客户端可见的进程内 Graylist 工具审批查询与决策。

MCP 连接与发现写入独立的 `mcp_server_start` Trace，工具调用在当前 Agent 请求 Trace 中写入 `mcp_tool_call` span，并关联脱敏的 Server ID、Transport、Registry 工具名和内部 `ToolCall.call_id`。进度最多保留 32 条数值摘要，Server 日志只接收 `warning` 及以上级别并丢弃正文；取消沿官方 SDK 生命周期传播，同时写入值脱敏的 Registry 终态审计和可关联的 MCP 取消事件。具体事件边界见 [MCP 集成](docs/mcp.md#观测与取消)。

## 索引生命周期

全量构建根据语料内容生成不可变版本和 `rag_v_<version>` collection；构建成功后再原子切换 active 指针。旧的 `rag_collection` 仍可作为无 manifest 时的兼容索引。

在线索引操作返回 `202 Accepted` 和 `job_id`。除任务状态查询外，以下接口必须携带 `X-Admin-Key`，其值与 `ADMIN_API_KEY` 一致：

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/upload?replace=false` | 新增或显式替换文档 |
| `DELETE` | `/documents/{filename}` | 删除文档并重建版本 |
| `POST` | `/index/rebuild` | 重建当前语料 |
| `POST` | `/index/rollback?version_id=...` | 激活指定版本；省略版本时回到前一版本 |
| `GET` | `/index/jobs/{job_id}` | 查询任务状态 |

修改请求可携带 `Idempotency-Key`，重复提交同一操作会复用已有任务。上传、删除和发布使用同一后台任务通道，避免并发覆盖。

```powershell
$adminKey = Read-Host "ADMIN_API_KEY"
curl.exe -X POST http://127.0.0.1:8000/index/rebuild `
  -H "X-Admin-Key: $adminKey" `
  -H "Idempotency-Key: rebuild-v1"
```

## 可靠性与安全

- HTTP 请求先执行客户端身份、Trace 和容量／时限中间件。
- 查询改写失败时保留原问题；BM25、向量召回或 Rerank 不可用时返回稳定警告并按可用链路降级。
- RAG 会话历史和 Agent SQLite 短期历史按客户端身份派生内部存储 ID，接口不暴露实际路径。
- metadata filter 只接受有限操作符、深度、分支数和标量类型。
- 上传校验扩展名、文件名、大小和目标路径，索引发布采用原子替换。
- 页面使用文本节点和安全 Markdown 渲染，响应设置 CSP 等安全头。
- Agent 拒绝未注册工具、越界参数和超过迭代、工具、Token 或总时间预算的执行。

## 可观测性

普通响应和 SSE 都返回 `X-Request-ID` 与 `X-Trace-ID`。本地 Trace 默认追加到 `logs/traces.jsonl`，覆盖查询改写、候选检索、Rerank、上下文构建、模型生成、Agent 和工具调用；每条 Trace 最多保留 128 个 span，超出部分只累计丢弃数。
Trace、Agent 事件、工具审计和 MCP 事件 JSONL 都按配置的单文件大小轮转，并同时受备份数量和保留时长约束。

Trace 记录稳定错误码、耗时、模型名、Token、缓存命中和索引版本；不记录完整查询、回答、会话身份、凭据或原始工具参数。`LANGFUSE_*` 目前只是预留配置，运行时不会向 Langfuse 发送 Trace。

当前部署仍是单实例、本地 SQLite／Chroma／JSONL 与进程内 OAuth Token／审批状态；浏览器客户端 cookie 不是用户或租户身份，单个管理员密钥也不是企业授权系统。多租户隔离、持久审批、高可用、网关、企业 IdP／Secret Store 及集中监控需要真实组织和部署条件，不通过空壳配置声称完成。各能力的成熟方案、现有接入点、暂不实现原因和落地前置条件见 [ENT-1 企业化能力路线](docs/mcp.md#企业化能力路线ent-1)。

当前 Agent 运行时没有接入 LangChain 或 LangGraph；运行时使用 `langchain-text-splitters`、评测依赖包含 LangChain 包，都不等于已有 LangChain Agent。若未来需求转向通用 Agent middleware，可在独立实现中评估由 LangChain `create_agent` 替换 `AgentHarness`；若出现长运行、分支和跨进程恢复需求，可评估由 LangGraph 状态图替换编排层。两条路线都必须重新验证现有 ToolRegistry、MCP、安全、审批、审计和结果边界，不能与当前循环叠层后宣称迁移完成。选择条件、连接点和官方来源见 [ALT-1 Agent 框架替代路线](docs/mcp.md#agent-框架替代路线alt-1)。

## 评测

评测数据规则见 [`data/testset/README.md`](data/testset/README.md)。当前 56 条 development 样本由 AI 生成且未经人工核验；`final_v1.json` 只有人工确认后才能加入样本。单文档组合事实使用 `multi_fact`，`multi_hop` 仅表示至少需要两个不同来源的跨文档问题。

### 数据校验

```powershell
uv run --no-sync --offline --no-env-file python scripts/validate_eval_dataset.py
```

### 5 条真实 RAG smoke

```powershell
uv run --no-sync --offline --no-env-file python scripts/run_eval.py --split development --limit 5
```

默认只运行 `hybrid+rerank`。报告写入被忽略的 `data/eval-runs/`，保存逐样本答案、证据、引用、错误、Trace、Token 和复现信息。正式报告可作为 CI artifact、Release 附件或受控附件交付，不要求提交原始报告；final 评测只允许在干净提交上运行，报告会绑定代码 SHA、数据与索引版本、模型、Prompt、完整配置和 seed。

> 独立评测进程不会执行 FastAPI startup。运行前应确认 Chroma 可加载、Reranker 权重可用，并检查日志中 BM25 是否实际参与；出现降级警告时不能把结果描述为完整 hybrid+rerank。

### 完整评测

```powershell
# 先用独立人工核验 calibration 集校准，不使用 final 调参
uv run --no-sync --offline --no-env-file python scripts/run_eval.py --split calibration --ablation
uv run --no-sync --offline --no-env-file python scripts/calibrate_abstention.py data/eval-runs/<report>.json

# 固定阈值与配置后，用独立人工核验 final 做最终验收
uv run --no-sync --offline --no-env-file python scripts/run_eval.py --split final --ablation

# Ragas Faithfulness / Relevancy；先安装 eval 依赖，再执行模型评判
uv sync --locked --no-default-groups --group eval
uv run --no-sync --offline --no-env-file --group eval python scripts/run_eval.py --split final --with-ragas
```

Recall@K、MRR、NDCG 和引用指标是确定性指标；Faithfulness 与 Relevancy 只有显式启用 Ragas 才计算。报告同时给出正确拒答率、错误拒答率和应拒答样本的硬答率。development 指标只能用于调试，不能作为发布门槛；对外引用指标时必须同时提供可访问的报告 artifact 或附件及其 run_id，不引用缺少复现信息的历史数值。阈值校准器仅接受独立人工核验 calibration 报告，拒绝 development/final 报告；输出的 thresholds、calibration_id 与 models 必须一起配置，且模型必须与运行时一致。当前尚无 calibration 样本，final 为 0，质量验收仍受阻；标注、隔离与冻结流程见 [人工核验指南](data/testset/annotation-guide.md)。未配置阈值时，系统只对空检索结果确定性拒答，不根据任意固定分数拒答。

## 公开网页演示

[打开 GitHub Pages 演示](https://caydenzheng.github.io/RAG/)：无需登录或安装，
可体验对话工作台、主题、模拟流式回答、引用、工具过程与审批交互。
**这是明确标注的静态模拟，不调用真实模型、索引或 MCP，不执行工具。**
真实后端仍只面向本地单用户，登录与数据隔离完成前不可作为多用户公网服务开放。
构建、自动部署与能力边界见 [GitHub Pages 说明](docs/github-pages.md)。

## 测试与 CI

默认测试全部离线，不调用真实模型、浏览器或 Ragas。MCP 测试仅使用真实本地子进程和数值型 loopback Server，不访问外部 MCP 服务：

```powershell
Set-Location frontend
npm ci
npm run typecheck
npm run test
npm run build
npx playwright install chromium
npm run test:e2e
npm run test:e2e:production
Set-Location ..
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = "1"
uv lock --check --offline
uv run --no-sync --offline --no-env-file python -B -m pytest -q
```

真实模型检查需要单独运行：

```powershell
uv run --no-sync --offline --no-env-file python tests/test_smoke.py
uv run --no-sync --offline --no-env-file python tests/test_agent.py
```

Playwright 首次运行需要安装 Chromium。默认浏览器套件会自行启动仅监听回环地址的
Vite 和本地假后端，覆盖分段 SSE、停止、错误、引用、工具过程、Approval、
form/URL Elicitation、主题、键盘与响应式布局；不读取用户文件、不连接真实 Provider
或外部服务。`test:e2e:production` 复用已构建资源，以禁用 lifespan 的本地 FastAPI
进程验证 `/`、`/agent`、生产资源和严格 CSP，不发送真实问答请求。若本机设置了代理，
测试配置会让 `127.0.0.1` 和 `localhost` 保持直连。

Ragas 依赖位于 `eval` 组，不进入默认 CI。Windows 与 Linux 的离线检查以及独立的
Chromium E2E job 定义在 [`.github/workflows/offline-checks.yml`](.github/workflows/offline-checks.yml)。

## 排障

### 找不到 `rag_collection`

当前 Chroma 目录没有 legacy collection 或 active manifest。确认 `CHROMA_PERSIST_DIR` 后运行 `scripts/build_index.py`。

### `Error loading hnsw index`

先执行 SQLite 完整性检查并确认 HNSW 分片齐全。Windows 下若仓库路径含中文，将索引复制到纯英文目录并修改 `CHROMA_PERSIST_DIR`；不要只复制 `chroma.sqlite3`。

### `rerank_unavailable`

Reranker 权重未缓存、模型路径错误、资源不足或超时。准备完整模型后重启；服务会在启动日志中报告预热结果。降级期间使用 RRF 顺序。

### BM25 命中为 0

服务启动时会在 Embedding 之后从当前 Chroma collection 重建 BM25。查看 `/ready`：索引不可读时返回 503；仅 BM25 重建失败时返回 200 和 `degraded`，查询降级为纯向量。

若 `/health` 为 200、`/ready` 的 index 为 `failed/index_unavailable`，说明 HTTP 服务已启动，但活动索引尚不可读。先备份索引和原始语料，检查存储可访问性；确认备份有效或原始语料完整后，再按“索引生命周期”使用管理接口恢复版本或重建。不要删除 Chroma 目录、自动创建空 collection 或将 503 当成模型拒答；应用不会自动修复或覆盖已有索引。完成恢复后重启后端，再点击“重新检查服务”确认 `/ready` 状态。

非流式模型响应也必须有明确完成原因：文本要求 `stop` 且内容非空，native tool call 要求 `tool_calls`。截断、过滤、缺失完成原因或空输出会进入失败路径，不缓存、不执行工具，也不保存为成功回答；此前未验证的缓存通过新命名空间绕过，原缓存数据保留。

### `final` 没有样本

这是数据质量保护。按 `data/testset/README.md` 完成人工审核、来源校验和版本更新后再运行正式评测。

### 共享虚拟环境被 uv 重建

先激活目标环境并设置 `UV_PROJECT_ENVIRONMENT`，随后使用 `uv run --no-sync`。只有依赖确实变化时才执行 `uv sync`。

## 已知限制

### 账号认证与会话所有权（AUTH-USER-1/2）

已提供默认关闭的账号与可撤销登录会话 API，以及本地管理员创建、停用、启用和重置密码命令。
参见[认证合同、权限矩阵与运维说明](docs/authentication.md)。初始密码必须修改，密码使用 Argon2id；
登录凭证仅存于 HttpOnly Cookie，服务器只保存摘要。无公开注册、邮件找回或登录页面。

账号模式已为旧 RAG/Agent/Memory/reset/pending 路径接入所有权及撤销防护，不把匿名 Cookie 升级为账号。
**共享资源细化权限、登录 UI 和完整跨设备历史仍未完成。** 普通账号暂不能调用共享 MCP 工具，原生工具仍经后端安全链。
在后续身份隔离和权限验收完成前，不得将后端作为多人或公网服务开放；现有 GitHub Pages 仍仅为静态模拟。

- 开发评测数据未经人工核验，不能用于发布门槛。
- 本地 Embedding 与 Reranker 首次加载需要模型文件、内存和启动时间。
- Windows 下 Chroma HNSW 对 Unicode 持久化路径存在兼容问题。
- 完整 Agent 评测和新旧实现对照仍待补充。
- 原生 Tool Calling 当前每个模型响应只接受一个工具调用；多个调用会稳定拒绝。模型若不支持完整工具 Schema，应显式切回 `AGENT_PLANNER_MODE=json`；系统不会静默改写 MCP 原始 Schema。
- MCP OAuth Token 与 Elicitation pending 状态默认只保存在当前进程内存；尚未接入企业 Secret Store、企业 SSO、远端 Token Revocation、多租户、高可用或持久化 HITL。

## 参与项目

提交修改前请阅读[贡献指南](CONTRIBUTING.md)和[行为准则](CODE_OF_CONDUCT.md)。安全漏洞请按照[安全策略](SECURITY.md)私密报告，不要创建公开 Issue。

## License

[MIT](LICENSE)
