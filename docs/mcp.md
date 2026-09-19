# MCP 集成

本项目在现有 `ToolRegistry` 与 Agent 安全链路上接入官方 MCP Python SDK 2.2.0。MCP 是可选工具来源；`MCP_SERVERS=[]` 时不会创建连接，也不会改变原生工具、RAG 或应用启动行为。

## 当前支持

| 能力 | 当前行为 |
|---|---|
| stdio | 通过官方 SDK 启动本地子进程；内置时间 Server 可直接演示 |
| Streamable HTTP | 通过官方 SDK 连接远程 URL，复用与 stdio 相同的发现、Schema、结果和状态链路 |
| 工具注册 | 命名为 `mcp__{server_id}__{tool_name}`，来源和 Provider 可在 `/agent/tools` 查看 |
| Schema | 保存完整 `inputSchema`，使用 JSON Schema Draft 2020-12 在调用前校验；JSON Planner 使用兼容投影 |
| 调用安全 | MCP 工具默认为 Graylist、`max_retries=0`；结果经过审计、不可信包装和上下文长度限制 |
| 可用性 | MCP 在后台初始化；单个 Server 失败只降低可选 MCP 状态，不阻塞核心 HTTP 服务 |

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

- `timeout_seconds` 控制 HTTP 建连、写入和连接池等待。
- `read_timeout_seconds` 控制 HTTP 长连接读取。
- `call_timeout_seconds` 控制单次工具调用，取值必须大于 0 且不超过 300 秒。
- stdio 的 `env` 值按秘密处理，只在建立官方 SDK 传输时解封；公共状态和错误不会返回命令、URL、Token 或远端正文。
- 工具调用不会因连接错误自动重放。对可能产生副作用的调用，由上层调用方决定是否重新执行。

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

当前仍使用 JSON Planner。完整 MCP Schema 已保留，后续原生模型 Tool Calling 会复用同一个 `ToolRegistry`、预算、审计和结果安全链路。

基础 Streamable HTTP 连接不代表已经具备以下能力：

- OAuth、企业 SSO 或 Token 持久化刷新；
- 多租户凭证、数据或索引隔离；
- 持久化 Human-in-the-loop、跨进程恢复或工作流编排；
- 高可用、服务发现、网关、集中策略、生产告警或自动扩缩容。

这些能力需要真实 IdP、Secret Store、租户模型、部署平台或业务审批决策，将按独立任务评估，不在基础 MCP 接入中提供空壳。
