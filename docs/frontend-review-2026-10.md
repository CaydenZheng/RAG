# 前端排查记录：附件 / 知识库 / 工具 / 偏好（2026-10）

本轮上游合并了 `feat/workspace-attachments-knowledge` 与 `feat/workspace-tools-convenience`。
以下是我对前端部分的复查结果与处理。

## 一、上游带来的变化（前端）

| 区域 | 变化 |
|---|---|
| 路由 | 新增 `/knowledge`、`/tools`、`/settings`；Pages 对应 `?view=knowledge|tools|settings` |
| 附件 | 新增 `AttachmentPanel`，按会话上传／解析／下载／删除，发送时带 `attachment_ids`（最多 5 个） |
| 知识库 | 新增 `KnowledgePage`，只读目录 + 搜索 + 摘录 |
| 工具 | 新增 `ToolsPage`，只读工具与 MCP Provider 目录，可把示例填进 Agent |
| 偏好 | 新增 `/workspace/preferences`：账号默认模型 + 主题（`useTheme` 回来了，只有 system/light/dark） |
| 技能 | 「从文档生成」以 `SkillCreatorDialog`（原生 `<dialog>`）回到 Skills 页；`/` 选择逻辑保留，并补了「技能列表加载中／加载失败可重试」 |
| 会话 | 目录支持标题搜索（`POST /conversations/search`，提交式，不逐键请求）；新增导出 Markdown；Pages 用 `DemoHistory` 顶替「即将支持」 |
| 服务状态 | `/ready` 的 `components` 参与文案，给出更具体的降级原因 |
| 快捷键 | `Ctrl/⌘ + Alt + I/H/N`：聚焦输入、聚焦历史搜索、新建对话 |

Pages 侧新增两处 alias（`toolClient`→`demo/tools.ts`、`resourceClient`→`demo/resources.ts`），
`workspaceClient` 的 demo 也补齐了 `readPreferences`/`savePreferences`/`response`，CSP 仍禁联外网。

## 二、已修（本轮唯一修复）

**服务状态在降级／启动中／不可用时被挤成竖排单字。**

`.service-status` 是单行 flex，新增的 `.service-detail` 与「重新检查服务」按钮把容器撑满后，
标签与按钮被压到最小宽度，实测标签 `28×110px`（中文逐字竖排）。

修复（`frontend/src/styles/index.css`、`frontend/src/styles/resources.css`）：

- `.service-status { flex-wrap: wrap }`
- `.service-status-label { white-space: nowrap }`
- `.service-status button { white-space: nowrap }`
- `.service-detail { flex: 1 1 100%; order: 3; margin: 0 0 6px }`

修复后实测：标签 `82×18`、按钮 `85×24`、说明独占一行 `196×36`，侧栏无溢出。

## 二之二、界面一致性返工（同一轮）

用户反馈四点，已全部处理：

1. **空状态不居中**：`min-height: 100%` 在带 padding 的滚动容器里会被内容高度带偏。
   改为 `.conversation-scroll:has(.empty-conversation) { position: relative }` +
   `.empty-conversation { position: absolute; inset: 0; display: grid; place-items: center }`，
   提示条再叠 `z-index: 1`。实测文案中心与对话区中心一致（315 vs 315）。
2. **会话记录看不到 `⋯`**：原来 `opacity: 0` 只在 hover 时出现，发现率太低。基础态改为 `0.45`，hover/展开时 `1`。
   Pages 演示的 `DemoHistory` 也补上同款菜单（重命名/清空/删除，内存生效），不再只是静态列表。
3. **小字注释位置混乱**：定了一套页面排版——
   `.skills-sub`（标题下一句说明）、`.page-note`（页面级补充）、`.field-hint`（控件下即时提示）、
   `.skills-status`（状态行）、`.page-section`（页内分节标题）；字号统一到 11–11.5px、颜色 `--faint`、行高 1.5–1.55，
   并把 Tools / Knowledge / Settings / Skills / 附件面板里没有 class 的自由段落全部归位。
4. **其他对齐细节**：附件面板限制到与输入框同宽（`--w`）并对齐居中；导出按钮右对齐；
   列表项统一 13px 名称 / 11.5px 描述 / 11px 元信息（去掉了浏览器默认的段落外边距）；
   搜索输入框与按钮同高（28px）；「刷新／重新读取」类次级动作改用 `.skills-action.plain`，避免整页都是主色胶囊。

## 二之三、列对齐、侧栏顺序、空状态与 11 条细节

**列对齐（根因）**：`.skills-page > *` 用 `margin-inline: auto` 居中 760px 列，但内容类把 `margin` 写成简写
（`margin: 10px 0 0`）把 auto 清零，于是标题/列表在 328，说明/搜索/字段/小节标题在 264，差 64px。
改为「内边距定列宽，子元素一律左对齐」：

```css
.skills-page { padding-inline: max(28px, calc((100% - var(--w)) / 2)); }
.skills-page > * { width: 100%; max-width: var(--w); margin-left: 0; margin-right: 0; }
```

实测四个页面所有直接子元素左边缘只剩 `[328]`；同时去掉 `.page-note/.field-hint` 的 `68ch`（原本只有 422px 宽），
页面上内边距统一到对话区的 40px。

**侧栏顺序**：`工具与 MCP` 移入主导航紧跟 `Skills 技能`，底部只留 工作台设置 / 知识库浏览 / 文档 / 账号 / 服务状态。

**空状态**：`clamp(26px, 2.6vw, 32px)` / 600 / 主文字色 / `-0.02em` / `text-wrap: balance` / 22ch；实测 30.7px、居中偏移 0。

**双 h1**：功能页存在时不再输出 App 的 `sr-only` h1，由各页可见标题承担；未登录的设置页补了正式 h1。

**11 条细节**：

| 项 | 处理 |
|---|---|
| 导出按钮占位 | 无已完成回合时不渲染；有内容时移到输入框上方右侧 |
| 附件行对齐 | 文件计数右对齐，与字符计数同一列 |
| 次级按钮可点性 | `.skills-action.plain` 加 1px 内描边，hover 才上底色 |
| `⋯` 对比度 | 0.45 → 0.6，hover/展开到 1 |
| 知识库搜索行 | 去掉上方 label，改 placeholder + 同行输入/按钮，高度统一 28px |
| 历史双入口 | 「搜索」与「刷新」合并成一行（输入框 + 清除 + 搜索 + 刷新图标） |
| 设置状态跳行 | 状态并入按钮行右侧，不再改变页面高度 |
| h2 兜底规则 | 删除 `.skills-page h2:not(.page-section)`，强制使用 `.page-section` |
| DemoHistory 清空 | 现在真的清空该示例（标记 + 点击提示），刷新恢复 |
| 窄屏侧栏高度 | 侧栏允许纵向滚动，历史列表保底 84px |
| macOS 快捷键 | 改为平台感知：macOS `⌥⇧`，其它 `Ctrl/⌘+Alt`；设置页文案同步 |

**GitHub Pages 注释精简**：只保留顶部一条横幅（两行），删掉 Skills / Tools / Knowledge / Settings / 附件面板 /
技能生成弹窗里重复的"模拟…不连接…刷新会重置"，Composer 底部改为一句"预设模拟内容。"，示例目录不再标注"非个人历史"。

## 三、未处理，需要产品／后续决定

1. **匿名本地模式下没有主题入口**：侧栏的「工作台设置」只在登录或 Pages 演示时出现，
   但 `main.tsx` 仍会读取并应用 `localStorage.ragflow_theme`。曾选过深色的用户会停在深色且无处可改。
   建议匿名模式也提供主题，或忽略该偏好。
2. **从工具／设置页点「新建对话」会多压一条历史记录**：`newConversation` 先 `navigate(mode)`（push `/`），
   `reset()` 再 push `/?conversation=<new>`。返回键会先落到 `/?`，触发 `forget()` 清空，
   上一次会话要按三次返回才回得去。建议工具页场景改用 `replaceState`。
3. **降级原因未覆盖 bm25**：`/ready` 的 `components` 含 `embedding/index/bm25/reranker`，
   前端只对前三个给专属文案，bm25 异常会落到「部分服务尚未就绪」。

## 四、验证

```
npx tsc -b                     ok
npm run test                   168 passed
npm run build / build:pages    ok
npm run test:e2e               15 passed
npm run test:e2e:pages         10 passed
uv run pytest tests/offline/test_workspace_attachments.py \
              tests/offline/test_workspace_convenience.py \
              tests/offline/test_web_pages.py     54 passed
```

`e2e/production.spec.ts` 已覆盖 6 条路由的严格 CSP、技能停用后的 422、真实附件解析、
偏好/目录/标题搜索，需要在有后端的环境执行。
