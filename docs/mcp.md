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
- 高可用、服务发现、网关、集中策略、生产告警或自动扩缩容。

这些能力需要真实 IdP、Secret Store、租户模型、部署平台或业务审批决策，将按独立任务评估，不在基础 MCP 接入中提供空壳。
