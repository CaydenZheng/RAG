# MCP 集成

本项目在现有 `ToolRegistry` 与 Agent 安全链路上接入官方 MCP Python SDK 2.2.0。MCP 是可选工具来源；`MCP_SERVERS=[]` 时不会创建连接，也不会改变原生工具、RAG 或应用启动行为。

## 当前支持

| 能力 | 当前行为 |
|---|---|
| stdio | 通过官方 SDK 启动本地子进程；内置时间 Server 可直接演示 |
| Streamable HTTP | 通过官方 SDK 连接远程 URL，复用与 stdio 相同的发现、Schema、结果和状态链路 |
| OAuth | 可选使用官方 SDK Authorization Code + PKCE、动态客户端注册/CIMD 与自动刷新；Host 提供回调和取消协调 |
| 工具注册 | 命名为 `mcp__{server_id}__{tool_name}`，来源和 Provider 可在 `/agent/tools` 查看 |
| Schema | 保存完整 `inputSchema`，使用 JSON Schema Draft 2020-12 在调用前校验；原生 Tool Calling 使用完整 Schema，JSON Planner 使用兼容投影 |
| 调用安全 | MCP 工具默认为 Graylist、`max_retries=0`；结果经过审计、不可信包装和上下文长度限制 |
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
