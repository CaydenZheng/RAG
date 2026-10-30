# GitHub Pages 静态模拟演示

访问入口：<https://caydenzheng.github.io/RAG/>。Agent 入口使用
<https://caydenzheng.github.io/RAG/?mode=agent>，可直接刷新；不使用需要服务器重写的 `/agent` 路径。

## 能力边界

公开网页完全在浏览器中运行：回答、引用、流式追加、工具步骤、Approval、form/URL
Elicitation 都是预设模拟，不是实际模型、检索、审批服务或 MCP 执行。Agent 中输入
“审批”“表单”“URL”“错误”“超时”体验相应场景；超时场景等待 25 秒。URL 场景仅在点击
“打开安全页面”后打开本项目 GitHub 页面，不进行 OAuth 授权。任意问题都只返回预设内容。

输入与表单内容不发送到服务器，不写入 URL、日志或持久存储；对话刷新即清空。
页面不写入浏览器存储，外观跟随系统主题。请勿输入敏感信息。GitHub Pages 平台本身可能记录网页访问。
模型（Flash / Pro）与 Skills 技能为内存模拟，刷新即重置；最近对话、附件和知识库管理仍禁用并标注“即将支持”。

演示专用客户端在 `frontend/src/demo/`；独立 Vite 配置只在 Pages 构建时替换真实客户端。
不使用 Service Worker、API 代理或公开后端。构建不加载 `.env`，不复制 public 目录，
不生成 source map。CSP 使用 HTML meta：禁止连接后端、内联脚本、eval、原始 HTML
Markdown 与表单提交；Pages 无法配置自定义 HTTP 安全响应头，因此不能提供 FastAPI
入口的 `frame-ancestors` 等响应头级防护。

浏览器 session ID 不是用户身份。真实后端仍只适用于本地单用户；完成登录与数据隔离前，
不要作为多用户公网服务开放。公开模拟演示不代表真实 RAG 质量或外部 MCP 验收通过。

## 本地构建与验证

需要 Node 22.12–24 和 npm，不需要 Python、模型凭据或索引：

```powershell
Set-Location frontend
npm.cmd ci
npm.cmd run typecheck
npm.cmd run test
npm.cmd run build:pages
npx.cmd playwright install chromium
npm.cmd run test:e2e:pages
npm.cmd run preview:pages
```

预览：`http://127.0.0.1:4175/RAG/`。构建目录 `pages-dist/` 被 Git 忽略。
本地 FastAPI 的 `npm.cmd run build` 仍固定使用 `/static/app/`，输出 `src/web/dist`，
两种构建互不覆盖。重命名仓库时同步修改 Pages base、测试与本文链接。

## 部署与回滚

仓库 Settings → Pages → Source 选择 **GitHub Actions**（也可由有权限的 REST API 配置）。
首次创建 `github-pages` environment 后，保持仅默认分支允许部署；PR 只构建与测试，
不发布、不使用 secrets。`.github/workflows/deploy-pages.yml` 在推送 master 时验证并部署
当前提交的独立静态产物；后续推送自动更新，也可手动运行。deploy job 的权限仅为
Pages 写入和 OIDC，构建 job 只有内容读取权限。

失败时查看 Actions，不跳过验证发布。需要回滚时，在 PR 中 revert 对应改动并通过 CI
后合并，工作流会重新构建部署；也可经授权临时取消发布。不要上传整个仓库、`.env`、
索引、数据库或本地辅助目录。
