# 前端界面改版记录（2026-02）

本轮只改前端界面与交互，没有改动任何接口、SSE 协议与鉴权逻辑。后端测试全部通过。

## 一、改了什么

| 区域 | 变化 |
|---|---|
| 页头 | 删除整块（不再显示"知识问答 / 知识问答工作台"）；仅保留一个 `sr-only` 的 `h1` 供读屏与路由识别 |
| 侧栏 | 承载账号 + 退出、服务状态（固定在最底部）；导航与列表去边框，仅 hover 变色 |
| 空状态 | 只在中间显示一句"有什么我能帮你的吗？"，去掉图标、标题与建议问题 |
| 消息 | 去掉头像与昵称；用户消息右对齐浅底气泡，助手回答为纯文本 + 元信息标签 |
| 输入框 | 更扁更小；内部按钮透明背景 + 主色图标；模型选择器只对读屏可见；阻断提示改为单行 notice |
| 历史会话 | **每一条**右侧都有 `⋯` 菜单（重命名 / 清空内容 / 删除会话），重命名为就地输入；菜单 portal 到 `body`，避免被侧栏滚动容器裁切。操作非当前会话时只刷新目录，不动正在看的会话 |
| Skills | 一行一个技能：名称 + 浅色小字说明 + 开关 + `⋯`；`⋯` 内为详情 / 删除；详情为弹层；上传入口在页头 |
| Agent 输入 | 输入 `/` 唤起技能选择（过滤、↑↓、Enter 插入、Esc 关闭） |
| 主题 | 删除主题选择控件，跟随系统；`index.html` 去掉写死的 `color-scheme: light` |
| 滚动 | 整页锁高（`100dvh`），侧栏固定，只有对话区 / Skills 区内部滚动；滚动条透明轨道、拇指 9–12% 不透明度 |

涉及文件：`frontend/src/styles/index.css`、`frontend/src/styles/skills.css`、`frontend/src/app/App.tsx`、
`frontend/src/app/AccountBoundary.tsx`、`frontend/src/components/{Sidebar,HistoryDirectory,ConversationView,Composer,ModelSelect,SkillsPage}.tsx`、
新增 `frontend/src/components/RowMenu.tsx`、删除 `ThemeControl.tsx` 与 `useTheme.ts`、`frontend/index.html`。
测试：`src/test/*` 与 `e2e/*` 同步更新。

## 二、有意移除，需确认

1. **外观主题选择付之阙如**：现在只跟随系统（`light-dark()` + `color-scheme`）。旧的 `localStorage["ragflow_theme"]` 不再读取，曾手工选过深色的用户会回到系统值。
2. **Skills「从文档生成」区块整体移除**：`createSkill` 与 `POST /skills/creator/stream` 仍在，但界面上没有入口。
3. 空状态建议问题、消息头像与昵称不再提供。

## 三、排查到但受"不改接口"限制未处理

1. ~~`/` 技能选择只停留在 UI 层~~ **已支持（后端已提供 `skill_ids`）**：
   `Composer` 解析消息开头的 `/技能名`，抽出 id 并通过 `send(message, skillIds)` → `streamAgentAnswer` 发送
   `skill_ids`；消息里只发剥离命令后的正文。列表过滤内置 `skill-creator`，未知/停用技能、只有命令没有正文时
   在前端直接拦截。后端 422 的 `skill_not_selected` / `skill_selection_limit` 已映射为可读文案。
2. ~~重命名 / 清空 / 删除只对当前会话开放~~ **已支持**：`updateConversation(item, …)` 走 `/conversations/{id}`，
   侧栏每行都挂了 `⋯`；`useConversation` 新增 `mutateConversation(item, action, title)`，
   非当前会话操作只刷新目录，当前会话仍走原来的清空/换 session 流程。
   生成进行中（`resetting` 或本轮仍在流式）时菜单项禁用，避免与流式写入竞争。
3. **技能目录读取失败后**，`/` 只提示"技能列表暂不可用"，没有重试入口；Skills 页有"刷新目录"可重试。
4. **检索在流式开始前失败仍回 503**，前端统一显示"服务尚未就绪"，丢失"检索服务暂时不可用"（此前记录于 `docs/frontend-issues.md`）。
5. **`safeExternalUrl` 仍拒绝 `http://localhost`**，URL 交互里本地地址打不开（可能是故意的收紧策略）。
6. **历史分页按钮在侧栏底部**，加载多页后会挤占总高度；后续可改为滚动到底自动加载。

## 四、验证结果

```
npx tsc -b                     ok
npm run test                   132 passed
npm run build / build:pages    ok
npm run test:e2e:pages         7 passed
npm run test:e2e               15 passed
uv run pytest tests/offline    894 passed
```

`e2e/production.spec.ts` 未在本地运行（需要真实后端与凭据）；本轮只把其中的标题断言改为 `toBeAttached()`
（页头标题移入 `sr-only` 后仍然存在，但视觉上不可见）。

首次跑浏览器测试需要 `npx playwright install chromium`。
