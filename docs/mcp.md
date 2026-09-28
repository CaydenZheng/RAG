# MCP 集成

本项目在现有 `ToolRegistry` 与 Agent 安全链路上接入官方 MCP Python SDK 2.2.0。MCP 是可选工具来源；`MCP_SERVERS=[]` 时不会创建连接，也不会改变原生工具、RAG 或应用启动行为。

## 当前支持

| 能力 | 当前行为 |
|---|---|
| stdio | 通过官方 SDK 启动本地子进程；内置时间 Server 可直接演示 |
| Streamable HTTP | 通过官方 SDK 连接远程 URL，复用与 stdio 相同的发现、Schema、结果和状态链路 |
| OAuth | 可选使用官方 SDK Authorization Code + PKCE、动态客户端注册/CIMD 与自动刷新；Host 提供回调和取消协调 |
| Elicitation | 复用官方 SDK form／URL callback 与现代 InputRequired 驱动；Host 通过 Agent session 传递请求和响应 |
| 工具注册 | 命名为 `mcp__{server_id}__{tool_name}`，来源和 Provider 可在 `/agent/tools` 查看 |
| Schema | 保存完整 `inputSchema`，使用 JSON Schema Draft 2020-12 在调用前校验；原生 Tool Calling 使用完整 Schema，JSON Planner 使用兼容投影 |
| 调用安全 | MCP 工具默认为 Graylist、`max_retries=0`；执行前等待 Host 审批，结果经过审计、不可信包装和上下文长度限制 |
| 可用性 | MCP 在后台并发初始化；单个 Server 等待授权或失败不阻塞核心 HTTP 服务及其他 Provider |

## 配置

`MCP_SERVERS` 是 JSON 数组。Server ID 只能使用字母、数字、下划线和连字符，并且必须唯一。

本地时间 Server：

```dotenv
MCP_SERVERS=[{"id":"clock","transport":"stdio","command":"python","args":["-m","src.mcp_servers.time_server"],"call_timeout_seconds":30}]
```

Streamable HTTP：

```dotenv
MCP_SERVERS=[{"id":"remote","transport":"streamable_http","url":"https://mcp.example.com/mcp","timeout_seconds":10,"read_timeout_seconds":300,"call_timeout_seconds":30}]
```

带标准 OAuth 的 Streamable HTTP：

```dotenv
MCP_SERVERS=[{"id":"remote","transport":"streamable_http","url":"https://mcp.example.com/mcp","oauth":{"redirect_uri":"http://127.0.0.1:8000/agent/oauth/remote/callback","client_name":"ragrag local client","scope":"mcp:tools offline_access"}}]
```

- `timeout_seconds` 控制 HTTP 建连、写入和连接池等待。
- `read_timeout_seconds` 控制 HTTP 长连接读取。
- `call_timeout_seconds` 控制单次工具调用，取值必须大于 0 且不超过 300 秒。
- stdio 的 `env` 值按秘密处理，只在建立官方 SDK 传输时解封；公共状态和错误不会返回命令、URL、Token 或远端正文。
- 工具调用不会因连接错误自动重放。对可能产生副作用的调用，由上层调用方决定是否重新执行。
- OAuth `redirect_uri` 对 `native` 客户端允许 HTTPS 或 loopback IP（`127.0.0.0/8`、`::1`）上的 HTTP；`web` 客户端及非 loopback 地址必须使用 HTTPS。

OAuth Server 返回 `401` 后，SDK 完成元数据发现并生成授权 URL。使用管理员凭据读取 `GET /agent/oauth/{server_id}`，在浏览器打开其中的 `authorization_url`；Provider 随后重定向到配置的 `/agent/oauth/{server_id}/callback`。回调端点不要求管理员 Header，但必须携带匹配的 `state`，并由 Host 与 SDK 再次校验；Uvicorn 访问日志会在记录前移除该回调的完整查询串。`POST /agent/oauth/{server_id}/cancel` 可由管理员取消当前待处理授权。OAuth 控制响应均设置 `Cache-Control: no-store`。

多个 MCP Server 会独立并发初始化；一个 Provider 等待人工 OAuth 回调时，其他健康 Provider 仍可完成工具发现并变为可用。调用期间发生刷新、重新授权或取消失败时，Server 分别收敛为 `mcp_oauth_failed` 或 `mcp_oauth_cancelled`，并禁用该 Provider 的工具，避免继续调度不可用凭据。

首版 `TokenStorage` 是可替换接口，默认实现只保存在当前进程内存，可在同一进程内供 SDK 自动刷新使用；应用重启后需要重新授权。生产 Secret Store 需要按部署环境另行实现。取消待处理授权不等于调用远端 Token Revocation Endpoint，本项目当前不声明远端撤销能力。

应用启动后可查看：

- `GET /agent/tools`：工具来源、Provider、分类、安全等级、可用状态及脱敏后的 Server 状态。
- `GET /ready`：核心组件 readiness 与可选 MCP 汇总。MCP 降级不会把健康的核心服务误报为宕机。

## Elicitation

MCP Client 在 stdio 和 Streamable HTTP 上都注册官方 `elicitation_callback`，因此会按 SDK 协议声明 form 和 URL Elicitation 能力。legacy server→client callback 与 2026 `InputRequiredResult` 自动多轮驱动共用同一条 Host 协调链路。

当前 Agent HTTP 与 SSE 都是单向请求，不能在同一个请求体中途接收用户响应。调用方应在发起 `/agent/chat` 或 `/agent/chat/stream` 时显式提供稳定的 `session_id`，并用另一个并发请求完成交互：

1. `GET /agent/elicitation/{session_id}` 查询该 Agent session 的 `pending` 列表。
2. form 项包含 `message` 与 `requested_schema`；URL 项包含 `message`、受限 URL 和 Server 的不透明 `elicitation_id`。
3. `POST /agent/elicitation/{session_id}/{pending_id}` 提交 `{"action":"accept","content":{...}}`，或提交不带 `content` 的 `decline`／`cancel`。
4. Host 校验响应后只返回 `202 {"status":"received"}`，不回显表单内容；SDK callback 将标准 `ElicitResult` 返回 Server，原工具调用随后继续。

公开 `session_id` 仍绑定客户端身份 cookie。另一个客户端即使知道相同 ID，也看不到或无法响应 pending；form `accept` 内容按 Server 的 JSON Schema 校验，非法内容不会解除等待。URL 只允许 HTTPS，或数值型 loopback IP 上的 HTTP，并且 URL 模式不接收 form 内容。Host 在 Server Schema 之外独立限制响应：HTTP 请求体最多 128 KiB，JSON 编码后的 `content` 最多 64 KiB、64 个字段，单个 key 最多 256 UTF-8 bytes，单个字符串最多 8 KiB，单个列表最多 64 项；非有限数字、嵌套对象和其他协议外值会被拒绝。消息、URL、Schema 和响应内容不写入日志或普通 MCP 状态接口。

pending 仅保存在当前进程内存，默认最多等待 25 秒，并仍受更短的 MCP `call_timeout_seconds` 和 Agent 总时限约束。响应、超时、调用取消、连接关闭和 Manager 关闭通过同一个锁内终态提交点竞争；只有一个结果能成功，胜者立即清理 pending，较晚的响应返回 not-pending，不会出现 Host 确认成功但 Server 已收到 `cancel`。legacy callback 在同一 Server 存在多个并发工具调用且无法确定归属时会安全拒绝，不会猜测 session。调用方若省略 Agent `session_id`，在阻塞请求完成前拿不到自动生成的 ID，因此不能可靠完成 Elicitation。

Elicitation 是 Server 主动请求输入的 MCP 协议能力，不是 Host 的工具执行审批。两者使用独立协调器和端点，不会把工具审批伪装成 MCP 协议消息。

## 工具执行审批

所有 Graylist 工具，包括原生外部工具和 MCP 工具，都在 `ToolRegistry.execute_async` 完成参数校验与安全策略判断后、执行副作用和进入工具重试循环前等待 Host 审批。Whitelist 自动执行，Blacklist 直接阻断；配置审批协调器时，同步 `ToolRegistry.execute` 会安全返回 `tool_approval_required`，不能绕过异步审批。

Agent HTTP 与 SSE 都是单向请求。调用方应显式提供稳定的 `session_id`，并用另一并发请求完成审批：

1. `GET /agent/approvals/{session_id}` 查询当前客户端、当前 Agent session 的 `pending` 列表；每项包含工具名、`native`／`mcp` 来源、Provider、分类和有界参数快照。
2. `POST /agent/approvals/{session_id}/{approval_id}` 提交 `{"action":"approve"}`、`{"action":"reject"}` 或 `{"action":"cancel"}`。
3. Host 只返回 `202 {"status":"received"}`，不在响应中回显参数。批准后 Registry 会重新校验并只执行 pending 中展示的同一份参数快照，不再读取调用方原始可变对象；同时复核 Registry 中仍是同一工具定义、工具仍可用且仍为 Graylist。审批期间发生 MCP 注销、禁用、同名替换或安全等级变化时返回稳定 `tool_unavailable`，原定义和替换定义都不会执行。工具只执行一次；拒绝、取消或超时分别以稳定工具错误返回 Agent 循环，并且不执行、不进入自动重试。

公开 `session_id` 仍绑定客户端身份 cookie；其他客户端即使知道相同 ID，也看不到或无法处理 pending。审批响应体在 DTO 解析前流式限制为 4 KiB。参数快照最多 64 KiB，并限制字段数、列表项、嵌套深度、节点数、key 和字符串长度；全局最多 128 个 pending、单 session 最多 8 个。超出容量或无法安全展示参数时，工具以 `tool_approval_unavailable` 失败关闭。审计只记录参数名、结果和稳定错误码，不记录参数值。

pending 默认等待 `AGENT_TOOL_APPROVAL_TIMEOUT_SECONDS=25` 秒，并仍受 Agent 总时限约束。响应、超时、调用取消和应用关闭通过同一个锁内终态提交点竞争；只有一个结果成功，应用重启时协调器可重新打开。pending 只存在于当前进程内存，因此不支持跨进程持久恢复、审批委托、复杂工作流或高可用审批；这些能力需要真实业务和部署基础设施后另行设计。

## 工具访问策略

`AGENT_TOOL_ALLOWLIST` 和 `AGENT_TOOL_DENYLIST` 接受 JSON 字符串数组，并按 ToolRegistry 中的完整精确名称匹配。原生工具直接使用注册名；MCP 工具使用 `mcp__{server_id}__{tool_name}`。空 allowlist 表示默认允许；非空 allowlist 只允许列出的工具；denylist 始终优先，重叠项仍拒绝。配置项会拒绝空白、前后空格、重复、过长或过多的名称，不使用通配符或正则，因此不会因模式扩张意外授权新工具。

策略位于共享 `ToolRegistry` 边界：被拒绝的工具不会出现在 JSON Planner 描述或 native Tool Calling Schema 中；即使模型臆造名称或代码直接调用同步/异步 Registry，也会在参数校验、审批、副作用和重试之前稳定返回 `tool_policy_denied`。原生与 MCP 工具使用同一判断。Agent 的既有危险名称正则 Hook 仍作为额外防线，但与配置拒绝重叠时由 Registry 策略提供权威错误码和审计。

启用任一列表后，每次已注册工具调用的允许、拒绝或后续 Hook 阻断结果都会写入现有有界 `logs/audit.jsonl`，包含 `policy_decision` 和稳定的 `policy_reason`（`allowlist`、`default_allow`、`denylist` 或 `not_allowlisted`）。审计只记录参数名，不记录值；参数名最多记录 32 个、单名最多 128 UTF-8 bytes、合计最多 256 bytes，超出部分以计数元数据表示。通用 JSONL 写入会在创建目录或轮转前拒绝任何编码后超过 `AGENT_LOG_MAX_BYTES` 的单条记录并记录告警，现有日志不会被超限记录替换，活动文件也不会突破上限。该能力是当前单进程 Host 的基础工具策略，不实现用户角色、租户、资源关系或集中策略服务；只有出现真实授权模型后才评估 Casbin、OPA 或 OpenFGA。

`GET /agent/tools` 中的 `available` 继续表示工具及其 Provider 的运行可用性，不混入授权含义；策略生效结果以 Planner 有效目录、调用错误码和上述审计记录为准。

## 观测与取消

MCP 观测复用现有 `TraceLogger`，不建立第二套追踪模型。每个启用的 Server 在启动时产生一个独立 `mcp_server_start` Trace，其中 `mcp_connect` 和 `mcp_discover` span 记录脱敏 Server ID、Transport、耗时、状态、稳定错误码和工具数量。每次工具调用在当前 Agent 请求 Trace 中追加 `mcp_tool_call` span，关联同一 Server、Transport 和具体 Registry 工具名，并记录耗时、状态及稳定错误码；没有当前请求 Trace 时不会伪造跨请求关联。每条 Trace 最多保留 128 个 span，超出部分只累计有界 `dropped_spans`。

工具调用通过官方 SDK 的 `progress_callback` 接收进度。单次调用最多保留 32 条事件的有限数值摘要及最后一次 `progress`／`total`，非有限数值单独计数；超出数量只累计有界丢弃数。Server 提供的 progress message 无论内容如何都不进入 Trace 或事件文件，只记录省略数量。

stdio 和 Streamable HTTP 的默认 Client 都注册官方 `logging_callback`，并把 SDK 日志级别设为 `warning`。Host 只记录级别，不保存 Server 的日志正文或 logger 名；每个 Server、每种事件在 60 秒窗口内最多写入 20 条，首次超限只追加一条 `mcp_event_rate_limited` 通知，其余丢弃。记录写入 `logs/mcp_events.jsonl`，与 `logs/traces.jsonl` 一样复用 `AGENT_LOG_MAX_BYTES`、`AGENT_LOG_BACKUP_COUNT` 和 `AGENT_LOG_RETENTION_SECONDS` 的单条大小、轮转、备份与保留边界。锁定的 MCP SDK 2.2.0 仍支持 logging callback，但 MCP logging capability 已于 2026-07-28 标记 deprecated；这里是对当前锁定版本的有界兼容接入，不承诺未来协议版本继续提供该能力。

调用方取消工具任务时，Host 保留 `CancelledError` 语义并让官方 SDK 负责协议级取消或传输中止，不自行构造 `notifications/cancelled`。本地只写一条不含参数值的 `tool_cancelled` 事件和状态为 cancelled 的调用 span。观测写入失败不会替换工具结果或取消结果。

这些文件仍是单实例本地 JSONL，不是 OpenTelemetry、Langfuse、Prometheus、集中日志、告警或 SLO 后端。生产集中观测的外部条件和接入位置见[企业化能力路线](#集中观测告警与-slo)。

## 本地演示

内置 `get_current_time(timezone="UTC")` 使用标准库 `zoneinfo`。支持 `UTC` 和系统可用的 IANA 时区；非法时区返回稳定的 MCP 工具错误。

直接启动 Server：

```powershell
uv run --no-sync --offline --no-env-file python -m src.mcp_servers.time_server
```

也可以用官方 MCP Inspector 做人工补充验证。先激活项目约定的虚拟环境并设置 `UV_PROJECT_ENVIRONMENT`，再运行：

```powershell
npx @modelcontextprotocol/inspector@2.7.0 --cli `
  uv run python .\src\mcp_servers\time_server.py `
  -e "UV_PROJECT_ENVIRONMENT=$env:VIRTUAL_ENV" `
  -e "UV_NO_SYNC=1" `
  -e "UV_OFFLINE=1" `
  -e "UV_NO_ENV_FILE=1" `
  --method tools/list
```

上述 CLI 应列出 `get_current_time`；去掉 `--cli` 和 `--method tools/list` 可启动浏览器界面，再分别调用 `UTC`、`Asia/Shanghai` 和非法时区。`-e` 参数确保 Inspector 启动的 stdio 子进程仍使用已激活的共享 uv 环境，且不会同步依赖或读取环境文件。Inspector 是官方开发工具，不作为项目运行时依赖，也不在仓库中复制实现。

## 测试

默认离线测试使用官方 SDK Server 验证两种真实传输：

- stdio：子进程握手、工具发现、结构化调用和可靠关闭。
- Streamable HTTP：仅放行测试进程的数值型 loopback 地址，覆盖应用配置接线、初始化失败、工具发现、调用超时、恢复和关闭。
- Elicitation：真实 in-process MCPServer 覆盖 legacy callback 与现代 InputRequired，并通过真实 stdio 子进程验证默认 Client factory 的 callback 接线；另覆盖 form／URL、Schema 与 Host 资源边界、非阻塞校验、超时／关闭终态竞争、并发归属和客户端 session 隔离。
- 工具审批：覆盖真实 Agent 循环、原生与 MCP Graylist、批准／拒绝／取消／超时、调用取消、关闭重开、终态竞争、客户端隔离、容量与请求体边界，以及审计参数值脱敏。
- 观测：真实 SDK Server 覆盖 progress、Server logging 和取消传播；另覆盖默认双传输 callback 接线、事件限频、秘密丢弃、Trace span 数量及 JSONL 轮转边界。
- 单元测试通过官方 SDK `Client` 边界进行 Mock，不实现 Fake MCP 协议或传输。

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = "1"
uv run --no-sync --offline --no-env-file pytest tests/offline -q -k mcp
```

## 能力边界

`AGENT_PLANNER_MODE` 默认为 `json`，可设为 `native` 使用 OpenAI-compatible 原生 Tool Calling。原生模式直接投影可用工具并传递完整 MCP `inputSchema`，不修改 Registry 中的原始 Schema；不符合 Provider function-name 约束的 MCP 名称只在模型边界映射为稳定别名，调用返回后会反解为 Registry 原名。两种模式复用同一个 `ToolRegistry`、MCP Adapter、预算、审计、参数校验和结果安全链路。供应商若不支持某项 Schema 特性，应显式切回 JSON 模式，当前不会静默裁剪 Schema 或自动重放请求。原生模式当前每个模型响应只接受一个工具调用。

当前标准 OAuth Client 不代表已经具备以下能力：

- 企业 SSO、生产 IdP 集成、跨进程 Token 持久化或远端 Token Revocation；
- 多租户凭证、数据或索引隔离；
- 持久化 Human-in-the-loop、跨进程恢复或工作流编排；
- 高可用、服务发现、网关、集中策略服务、生产告警或自动扩缩容。

这些能力需要真实 IdP、Secret Store、租户模型、部署平台或业务审批决策，将按独立任务评估，不在基础 MCP 接入中提供空壳。

## 企业化能力路线（ENT-1）

本节是架构边界和落地清单，不代表仓库已经提供对应生产能力。方案选择必须由真实组织的身份体系、数据分类、合规要求、流量规模和部署平台驱动；在这些输入缺失时，仓库不增加无法验证的适配器、配置项或部署清单。

### 多租户身份、数据与审计隔离

- **解决的问题**：阻止不同组织访问彼此的会话、索引、MCP 凭据、审批和审计数据，并支持租户级保留、导出、删除和用量归属。
- **当前状态**：`ragflow_client` 只是浏览器客户端的随机 HttpOnly cookie，派生的 storage session 提供当前应用内的客户端作用域隔离；它不是已认证用户或租户主体。管理员认证是单个 `ADMIN_API_KEY`。会话 SQLite、Chroma、缓存、文件和 JSONL 日志均为本机共享资源，没有 tenant 列、数据库行级安全或租户密钥。
- **成熟方案**：由企业 OIDC IdP 签发用户和服务身份，在可信入口校验 issuer、audience 与签名；将稳定 `tenant_id` 作为授权上下文传递。数据层可按风险选用独立数据库／索引，或 PostgreSQL tenant 列配合 Row-Level Security；对象存储、向量库、审计流和加密密钥采用同样的租户分区策略。资源级授权模型明确后，再评估 OPA、OpenFGA 或 Casbin。
- **现有连接点**：身份上下文应在 `ClientIdentityMiddleware` 之前或替换该边界建立；`scope_request_session`、`SessionStore`、Chroma collection／索引版本、`ToolRegistry` 策略、OAuth `TokenStorage`、审批／Elicitation pending 和审计写入都必须显式接收租户上下文，不能从可伪造 Header 或公开 session ID 推断。
- **当前不实现原因**：仓库没有租户生命周期、成员与角色、资源归属、计费、数据地域或删除 SLA；仅添加 `tenant_id` 字段无法证明端到端隔离，反而容易产生遗漏路径。
- **落地前置条件**：确定身份 claim 到租户的映射、跨租户管理员规则、数据分类与隔离等级、存储拓扑、迁移方案、密钥轮换、备份恢复、审计保留和租户删除流程，并用越权与并发测试覆盖每个存储和工具调用边界。

### 持久化审批与可靠工作流

- **解决的问题**：让长时间审批跨进程重启、实例切换和人工离线时间继续存在，并保证批准、拒绝、超时、取消和工具副作用只有一个可恢复终态。
- **当前状态**：Elicitation 与工具审批 pending 均保存在单进程内存，受短超时和应用关闭控制；应用重启或请求被调度到另一实例后不能恢复。MCP Elicitation callback、active call 和等待结果的 Future 还绑定原 Client session 与在途协议请求，连接断开后即使另存 pending 也无法向该请求继续回包。SQLite 索引任务是单 worker 本地任务，不是通用可靠工作流引擎。
- **成熟方案**：工具业务审批等 Host 长事务可使用 Temporal、Camunda 等持久工作流，或使用具备事务 outbox、租约、幂等键和可靠队列的数据库状态机；人工任务需要独立的身份授权、通知、升级、委托和不可抵赖审计。MCP Elicitation 必须按单次 Client session 请求处理：断连时该次请求失败或取消，不能通过恢复本地 Future 冒充协议续接。若业务流程需要跨进程继续，应由 Server 在新 session 上重发请求，或由 Host 以幂等方式重新发起工具调用。
- **现有连接点**：工具审批可从 `ToolApprovalManager` 的 pending／终态边界替换存储和调度实现，`ToolRegistry` 仍保留“审批后、执行副作用前”的权威复核；工具定义版本、批准参数快照和幂等键必须随工作流状态持久化。Elicitation 协调器只能在原连接生命周期内提交协议结果；断连时应收敛为失败或取消。新的 callback 必须由 Server 重发或新的工具调用产生，再通过独立于 connection／Future 的稳定业务关联键与持久工作流关联。
- **当前不实现原因**：目前没有需要跨天等待的审批 SLA、审批角色、通知渠道、委托规则或可安全重放的业务工具语义；模拟一张 pending 表不能解决副作用幂等和恢复一致性。
- **落地前置条件**：定义工作流状态机、审批权限、超时与升级策略、工具幂等契约、补偿动作、数据保留、灾难恢复目标及 worker 部署方式，并验证崩溃发生在“批准后／执行前”和“执行后／确认前”时的行为。对跨进程 Elicitation 还必须与 Server 约定稳定业务关联键和重发／重新调用协议，定义幂等处理、重复与过期响应协调，以及断连后由哪一方负责超时和取消；没有这些约定时只能终止原请求，不能声称可恢复。

### 高可用、服务发现与弹性伸缩

- **解决的问题**：在实例、节点或可用区故障时维持服务，并按 HTTP、Agent 与 MCP 负载独立扩缩容。
- **当前状态**：应用按单实例运行；本地 SQLite、Chroma、JSONL、进程内 Token／pending 和 stdio 子进程都具有实例亲和性。`/ready` 只能报告本实例状态，MCP Manager 也只管理本进程的连接。
- **成熟方案**：在已有平台上使用 Kubernetes Deployment／StatefulSet、Service、PodDisruptionBudget、Horizontal Pod Autoscaler 和跨区策略；依赖使用具备备份与故障转移的托管数据库、对象存储、向量服务和消息系统。远程 MCP Server 通过平台服务发现或受控服务目录提供稳定地址。
- **现有连接点**：ASGI startup／shutdown、`/ready`、MCP Runtime 与外部存储 adapter 是迁移边界；只有将 session、Token、审批、任务和索引元数据移出本地进程后，HTTP 实例才可能无状态扩容。stdio Server 若保留，必须明确其 pod 生命周期和容量模型。
- **当前不实现原因**：仓库没有集群、共享数据服务、镜像发布流程、容量基线或恢复目标；提交 Kubernetes YAML 不能证明实际调度、存储和故障切换可用。
- **落地前置条件**：确定部署平台、网络拓扑、共享存储、镜像与供应链策略、健康探针语义、容量与压测数据、RPO／RTO、备份恢复演练和 MCP 连接所有权，再编写与目标环境匹配的部署清单。

### API Gateway、集中认证与网络策略

- **解决的问题**：统一 TLS、用户／服务认证、路由、限流、配额、WAF、请求大小、访问日志和南北向／东西向网络控制。
- **当前状态**：Uvicorn 直接暴露 FastAPI；普通请求依赖随机客户端 cookie，管理接口使用单个静态 API key。应用内存在局部请求和资源上限，但没有集中网关策略、服务身份、mTLS 或 MCP egress allowlist。
- **成熟方案**：根据现有平台选择 Envoy、Kong、APISIX 或云 API Gateway，结合企业 OIDC、workload identity、mTLS 和 Kubernetes NetworkPolicy／服务网格；网关负责粗粒度入口控制，应用仍负责 session、工具和资源级授权，不能把两者混为一层。
- **现有连接点**：HTTP 中间件可接收网关验证后、经过防伪保护的身份上下文；`require_admin` 应迁移为角色／scope 校验；Streamable HTTP MCP 配置与 transport factory 是 egress、代理、私有 CA 和 mTLS 的接入点。
- **当前不实现原因**：没有选定网关、IdP、证书颁发体系、域名、可信代理链或网络分区。通用的“X-User” Header 方案会扩大身份伪造风险。
- **落地前置条件**：确定信任边界、Token audience／scope、Header 清洗、TLS 与证书轮换、入口和 egress 路由、限流维度、真实客户端 IP 规则、失败模式及网关与应用的责任矩阵。

### 企业 IdP 与 Secret Store

- **解决的问题**：集中管理用户和 workload 身份，并安全保存 MCP OAuth Token、客户端凭据、模型密钥和管理员秘密，支持最小权限、轮换、吊销和访问审计。
- **当前状态**：MCP 使用官方 OAuth Client，但默认 `InMemoryOAuthTokenStorage` 只服务当前进程；重启后需重新授权。`.env`／进程环境承载其他秘密，仓库没有企业 SSO、workload identity、持久 Token 加密、远端 Token Revocation 或密钥轮换控制面。
- **成熟方案**：对接组织已有的 Entra ID、Okta、Keycloak 或其他 OIDC IdP；秘密使用 HashiCorp Vault、AWS Secrets Manager、Azure Key Vault 或 GCP Secret Manager，并优先采用 workload identity 获取短期访问权。持久 Token 应使用 KMS 包封加密、版本化和租户隔离。
- **现有连接点**：`OAuthCredentialStorage`／storage factory 是 MCP Token 持久化 seam；`SecretStr` 配置、LLM Client、管理员认证和 stdio 环境构造需要改为启动时或按需解析 secret reference，公共状态仍只输出脱敏字段。
- **当前不实现原因**：没有真实 Provider metadata、客户端注册政策、云账号、KMS key、secret path 命名、轮换 SLA 或事故响应流程；伪造 Vault adapter 无法验证权限和吊销行为。
- **落地前置条件**：选择 IdP 与 Secret Store，定义用户和服务主体、OAuth client 类型、redirect URI、scope、Token 归属、加密与轮换、缓存时限、故障降级、审计访问和远端吊销策略，并在真实测试租户中完成集成验证。

### 集中观测、告警与 SLO

- **解决的问题**：跨实例关联一次 Agent 请求、MCP Provider 和工具调用，发现延迟、错误、容量与安全异常，并用可量化目标驱动告警和容量决策。
- **当前状态**：请求 Trace、Agent 事件和审计写入本地 JSONL；Trace 字段会脱敏和截断，Agent 事件与审计文件另有轮转和保留边界，但这些都不是集中遥测。`/ready` 只提供本实例快照，`LANGFUSE_*` 是预留配置且运行时不会上报。当前没有 OpenTelemetry Collector、集中日志、Prometheus 指标、告警路由、值班或 SLO。
- **成熟方案**：使用 OpenTelemetry SDK／Collector 输出 Trace 与指标，Prometheus 和 Grafana 展示服务指标，Alertmanager 或组织告警平台负责路由；日志可进入 Loki、OpenSearch／Elastic 或云日志服务。供应商和存储应由现有平台标准决定。
- **现有连接点**：复用 request／trace ID、`TraceLogger` 的 span 语义、MCP Manager 状态、工具稳定错误码和 JSONL 脱敏规则；OBS-1 先补足单请求到 MCP Server／工具的关联及有界 SDK 事件，再考虑 exporter，避免建立第二套语义。
- **当前不实现原因**：没有遥测后端、采样与成本预算、数据保留、敏感字段政策、服务等级目标或值班流程；只新增 exporter 不能构成生产监控。
- **落地前置条件**：定义可用性和延迟 SLI／SLO、错误预算、指标基数、采样、日志与 Trace 保留、敏感数据过滤、租户隔离、dashboard owner、告警阈值与 runbook，并通过故障演练验证告警可行动。

以上各项必须分别进行威胁建模、迁移和故障演练。它们不能仅凭依赖已安装、接口已预留或示例部署文件存在就标记为完成。

## Agent 框架替代路线（ALT-1）

本节只说明未来可能的独立替代实现，不代表仓库已经接入 LangChain、LangGraph、LangSmith 或 LangChain MCPAdapter。当前运行依赖中的 `langchain-text-splitters` 只用于文本切分，`eval` 依赖组中的 LangChain 包只服务 Ragas 兼容评测；生产 Agent 仍由 `AgentHarness`、`ToolRegistry` 和官方 MCP Python SDK 薄适配组成。替代路线必须从 `AgentRuntime` 边界整体切换，不能在现有模型—工具循环外再套一层同职责框架。

| 选择 | 适用条件 | 替换的主要责任 | 不能默认获得 |
|---|---|---|---|
| 保持当前实现 | 单次请求内 Agent 与工具调用为主，现有安全和 MCP 能力已满足需求 | 不替换 | 跨进程工作流、图式编排、集中观测平台 |
| LangChain | 需要通用 Agent harness、多模型／工具生态和 middleware，并愿意让框架接管模型—工具循环 | `AgentHarness`，并适配或保留 `ToolRegistry` | 本仓库策略、审批快照、稳定错误码、秘密保护和 MCP 行为等价 |
| LangGraph | 已有明确分支、循环、并行节点、长时间暂停或恢复需求 | Agent 编排及状态生命周期；可独立使用 | Exactly-once、副作用幂等、生产数据库、身份隔离或 LangSmith 部署 |

### LangChain 高层 Agent 替代

- **解决的问题**：用统一的 model、tools、prompt 和 middleware 组成通用 Agent harness，复用供应商集成、标准工具循环以及工具错误处理、调用限制和 HITL 等 middleware。官方说明 LangChain Agent 构建在 LangGraph 上；需要更低层的确定性流程与 Agent 步骤混合时应直接评估 LangGraph。[LangChain overview](https://docs.langchain.com/oss/python/langchain/overview) · [Agents](https://docs.langchain.com/oss/python/langchain/agents) · [Middleware](https://docs.langchain.com/oss/python/langchain/middleware/built-in)
- **当前状态**：项目没有 LangChain Agent 运行时或平行版本。现有 JSON／native Planner、工具预算、Hook、策略、审批、审计、MCP OAuth／Elicitation 和结果安全都由自有小接口实现；依赖中出现 LangChain 相关包不改变这一事实。
- **成熟方案**：让 `create_agent` 成为唯一模型—工具循环，并把需要的业务控制实现为明确 middleware 或唯一执行网关。若采用 MCP，可评估官方 `langchain.mcp.MCPAdapter`；截至本说明日期该接口标为 beta、连接能力基于 FastMCP，工具结果、错误和生命周期边界与本项目当前官方 `mcp` SDK Adapter 不同，不能视为无损替换。[MCPAdapter](https://docs.langchain.com/oss/python/langchain/mcp) · [MCP tools](https://docs.langchain.com/oss/python/langchain/mcp/tools) · [MCP authentication](https://docs.langchain.com/oss/python/langchain/mcp/auth)
- **现有连接点**：在 `AgentRuntime.execute/events` 后替换 `AgentHarness`；`ToolRegistry` 要么继续作为所有工具的唯一安全执行网关，要么转换为 LangChain `BaseTool` 后逐项重建完整 Schema、策略、审批、生命周期复核、审计和结果包装。若以 MCPAdapter 替换 `MCPClientManager`，还必须重新验证 stdio／Streamable HTTP、OAuth、Elicitation、Provider 隔离、状态接口、取消、错误映射及秘密保护。
- **当前不实现原因**：当前循环已经满足验收，引入另一套 Tool、Agent、state 和 middleware 抽象不能删除足够多的现有代码，反而容易产生两条安全链路。MCPAdapter 仍为 beta，仓库也没有需要通过 LangChain 生态立即解决的确定业务缺口，因此不增加依赖或维护平行运行时。
- **落地前置条件**：先确定要改善的指标，例如供应商接入时间、middleware 复用率或复杂任务成功率；在独立分支对同一模型、Prompt 和工具集做基准。候选实现必须通过当前参数校验、零隐式副作用重试、allow／deny、审批快照、取消、审计、输出大小、秘密保护和 Provider 故障隔离测试，再决定一次性切换，不能长期双写或双执行。

### LangGraph 状态图与持久工作流替代

- **解决的问题**：把确定性业务步骤与 LLM 决策表示为可检查的状态图，支持分支、循环、流式、暂停和长运行流程。LangGraph 是低层编排运行时，可以独立于 LangChain 使用。[LangGraph overview](https://docs.langchain.com/oss/python/langgraph/overview) · [官方仓库](https://github.com/langchain-ai/langgraph/blob/main/README.md)
- **当前状态**：项目没有 LangGraph graph、checkpointer 或 store。SQLite 会话历史不是 graph checkpoint；进程内工具审批和 Elicitation pending 也不能跨重启恢复。现有 `AgentRuntime` 只抽象有界执行和事件流，没有声明持久图语义。
- **成熟方案**：以显式 state schema、nodes 和 edges 重建编排；用 checkpointer 保存单个 `thread_id` 的图状态，用 store 保存跨 thread 数据，并通过 interrupt／resume 实现需要人工输入的业务节点。生产环境使用目标数据库 saver，而不是 `InMemorySaver`。[Persistence](https://docs.langchain.com/oss/python/langgraph/persistence) · [Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)
- **现有连接点**：`AgentRuntime` 是整体替换 seam；检索、生成和 `ToolRegistry` 可作为 graph node 继续复用，Agent 事件需从 graph stream 映射回现有 `AgentEvent`。`SessionStore`／`MemoryManager` 与 checkpointer／store 的归属、清除和迁移必须明确；工具业务审批可以映射到 interrupt，但 MCP Elicitation 仍受原 Client session 和协议请求生命周期约束，不能仅靠恢复 graph checkpoint 向已断开的请求回包。
- **当前不实现原因**：尚无经过确认的长时间工作流、状态图、恢复 SLA 或跨进程审批业务。官方说明 interrupt 恢复会从节点开头重新执行，副作用仍需幂等或任务封装；仅接入内存 checkpointer 只能演示 API，不能证明可靠恢复。当前也没有生产 saver、稳定 thread／user 身份或状态保留策略。
- **落地前置条件**：定义真实 graph、状态所有权、稳定 `thread_id`／用户／租户映射、数据库 saver 与 store、加密和保留、并发恢复规则、幂等键、重复／过期 resume、补偿和灾难恢复；验证崩溃位于“副作用前后”和“checkpoint 前后”的行为。只有候选实现达到现有 API、工具安全、MCP 和可观测性语义等价，并在故障演练中满足恢复目标，才可替换当前运行时。

### 观测与验证边界

LangSmith 是可观测、评测和监控平台，不是启用 LangChain／LangGraph 后自动存在的本地后端；使用它需要单独的账户／部署、凭据、采样、脱敏、保留、租户隔离和成本决策。[LangSmith Observability](https://docs.langchain.com/langsmith/observability) 当前 `TraceLogger` 可作为未来 instrumentation 语义来源，但不能直接称为 LangSmith 集成。

如果未来确有迁移需求，应只建立短期独立原型：选择一个无副作用原生工具和一个 MCP 工具，对比 Schema、调用、错误、取消、审批和 Trace；持久化原型必须使用目标生产 saver 并执行重启／并发／重放测试。原型达不到可量化收益或无法保持安全不变量时即删除，不在主运行时长期维护双实现。MCP 的 Host／Client／Server 协议责任仍以 [MCP 架构](https://modelcontextprotocol.io/docs/learn/architecture) 和 [官方 Python SDK](https://github.com/modelcontextprotocol/python-sdk/blob/main/README.md) 为准，Agent 框架不能替代 Host 的业务授权与安全责任。
