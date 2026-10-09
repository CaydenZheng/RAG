# 贡献指南

感谢你考虑改进 RAGFlow。这个仓库面向本地开发和小规模服务部署，请保持修改聚焦、可复现，并避免引入与项目定位无关的大型基础设施。

## 开始之前

- Bug 和功能建议请先搜索已有 Issue，避免重复讨论。
- 安全漏洞不要提交公开 Issue，请按照[安全策略](SECURITY.md)私密报告。
- 参与项目即表示你同意遵守[行为准则](CODE_OF_CONDUCT.md)。

## 开发环境

项目要求 Python 3.13 和 uv 0.12.11 或更高版本：

```powershell
uv sync --locked --no-default-groups --group dev
Copy-Item .env.example .env
```

按需编辑 `.env`。不要提交 API Key、Token、个人数据、本地数据库、模型缓存或日志。

## 提交修改

1. 从最新的 `master` 创建主题分支。
2. 保持一次修改只解决一个清晰问题；行为变化应补充永久回归测试和必要文档。
3. 优先复用现有接口和安全链路，不要让新的 Agent、MCP 或 HTTP 路径绕过校验、策略、审批和审计。
4. 提交信息说明实际行为变化，不使用只在个人工作区中有意义的任务编号。

提交 Pull Request 前运行默认离线检查：

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = "1"
uv lock --check --offline
uv run --no-sync --offline --no-env-file ruff check .
uv run --no-sync --offline --no-env-file python -B -m pytest -q
```

如果修改涉及依赖、文件路径、启动流程、并发或 Windows 行为，请在 Pull Request 中说明，并请求运行 Windows CI。真实 Provider 或模型检查必须单独标注，不能用它替代默认离线测试。

## Pull Request 内容

请简要说明：

- 解决的问题和采用的方案；
- 用户可见行为或兼容性变化；
- 实际运行的测试及结果；
- 仍然存在的限制或风险。

维护者可能要求缩小范围、补充回归测试或拆分无关修改。
