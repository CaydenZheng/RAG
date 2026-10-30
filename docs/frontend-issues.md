# 前端问题记录（功能/正确性，不含界面细节）

## 严重

### 1. Agent 流式完成事件被误判为失败

- 位置：`frontend/src/api/agentClient.ts` 的 `streamAgentAnswer`
- 后端 Agent 的 DONE 事件由 `AgentResponse.to_dict()` 组成（`src/core/agent_runtime.py`），**恒带** `"error": ""` 和 `"error_code": ""` 两个空字段。
- 前端判断：

```ts
if (rawEvent.error !== undefined) {
  throw new PublicClientError("terminal_error", PUBLIC_MESSAGES.agentFailed);
}
```

空串 `"" !== undefined` 为 `true`，于是每次成功回答走到 DONE 事件都被当成错误丢弃，UI 显示"Agent 处理失败，请稍后重试"。

- 测试未覆盖：`frontend/src/test/streaming.test.ts` 里 mock 的 DONE 事件写成了 `{"done":true,"answer":"2",...}`，漏掉 `error`/`error_code`，与真实后端格式不一致，所以测试全绿。
- 修法方向：改成只认"非空字符串 error"或"带 code 的错误事件"，不要用 `error !== undefined`。

## 次要

### 2. 检索失败的具体文案被吞

- `frontend/src/api/ragClient.ts` 里 `retrieval_failed` → "检索服务暂时不可用" 只在 SSE error 事件生效。
- 检索在流式开始前失败会直接回 HTTP 503，走 `requireEventStream`，统一映射成"服务尚未就绪"，丢了更准的文案。

### 3. `color-scheme` 与主题冲突

- `frontend/index.html` 写死 `<meta name="color-scheme" content="light">`，深色主题下原生控件/滚动条仍按浅色渲染。

### 4. `safeExternalUrl` 拒绝 `http://localhost`

- `frontend/src/api/safeUrls.ts` 只放行 `https://` 或 `127.*` 的 `http://`，`http://localhost` 会被判不安全，URL 交互/OAuth 本地回调打不开（可能是故意收紧，需确认）。

### 5. 本地前端未构建时 `/` 无界面

- `src/web/dist` 被 gitignore 且当前未构建，跑 `npm run build` 之前访问 `/` 只回 "Web application is not built" 提示。
