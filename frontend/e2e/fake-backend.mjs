import { createServer } from "node:http";

const HOST = "127.0.0.1";
const PORT = 8000;
const sessions = new Map();
const activeResponses = new Set();
const hangingResponses = new Set();
const interactionActions = [];
let readyState = "ready";

function json(response, status, body) {
  response.writeHead(status, {
    "Cache-Control": "no-store",
    "Content-Type": "application/json; charset=utf-8",
  });
  response.end(JSON.stringify(body));
}

function readJson(request) {
  return new Promise((resolve) => {
    let body = "";
    request.setEncoding("utf8");
    request.on("data", (chunk) => { body += chunk; });
    request.on("end", () => {
      try {
        resolve(JSON.parse(body));
      } catch {
        resolve({});
      }
    });
  });
}

function openEventStream(request, response) {
  response.writeHead(200, {
    "Cache-Control": "no-cache, no-store",
    Connection: "keep-alive",
    "Content-Type": "text/event-stream; charset=utf-8",
  });
  activeResponses.add(response);
  const timers = new Set();
  let closed = false;
  const close = () => {
    if (closed) return;
    closed = true;
    for (const timer of timers) clearTimeout(timer);
    timers.clear();
    activeResponses.delete(response);
  };
  request.on("close", close);
  response.on("close", close);
  return {
    send(payload) {
      if (!closed && !response.writableEnded) response.write(`data: ${JSON.stringify(payload)}\n\n`);
    },
    later(delay, action) {
      const timer = setTimeout(() => {
        timers.delete(timer);
        if (!closed) action();
      }, delay);
      timers.add(timer);
    },
    end() {
      if (!closed && !response.writableEnded) response.end();
      close();
    },
  };
}

function finishAgent(session, answer) {
  if (!session || session.streamFinished) return;
  session.streamFinished = true;
  session.pending = false;
  session.stream.send({
    step: "tool_done",
    iteration: 1,
    tool: session.toolName,
    call_id: "fixture-call",
    success: true,
  });
  session.stream.send({ chunk: answer });
  session.stream.send({ done: true, answer, iterations: 1, latency_ms: 12 });
  session.stream.end();
}

function approvalPayload(session) {
  if (!session?.pending || (session.kind !== "approval" && session.kind !== "slow-approval")) return [];
  return [{
    id: session.id,
    tool_name: session.toolName,
    source: "native",
    provider: "local-fake",
    category: "external",
    params: { destination: "fixture" },
  }];
}

function elicitationPayload(session) {
  if (!session?.pending) return [];
  if (session.kind === "form") {
    return [{
      id: session.id,
      server_id: "local-form-provider",
      mode: "form",
      message: "请补充本地测试信息。",
      requested_schema: {
        type: "object",
        properties: {
          summary: { title: "摘要", type: "string" },
          count: { title: "数量", type: "integer" },
        },
        required: ["summary", "count"],
      },
    }];
  }
  if (session.kind === "url") {
    return [{
      id: session.id,
      server_id: "local-url-provider",
      mode: "url",
      message: "请在外部页面完成操作。",
      url: "https://login.example.test/authorize?token=fixture-private-query",
      elicitation_id: "fixture-elicitation",
    }];
  }
  return [];
}

function startRag(request, response, url) {
  const query = url.searchParams.get("query") ?? "";
  if (query === "HTTP 422") {
    json(response, 422, { detail: "fixture" });
    return;
  }
  if (query === "HTTP 503") {
    json(response, 503, { detail: "fixture" });
    return;
  }

  const stream = openEventStream(request, response);
  if (query === "触发错误") {
    stream.send({ event: "chunk", done: false, chunk: "半截内容" });
    stream.later(80, () => {
      stream.send({
        event: "error",
        done: true,
        error: { code: "answer_generation_failed", message: "fixture-only" },
      });
      stream.end();
    });
    return;
  }
  if (query === "等待停止") {
    stream.send({ event: "chunk", done: false, chunk: "持续生成" });
    const append = () => {
      stream.send({ event: "chunk", done: false, chunk: "。" });
      stream.later(80, append);
    };
    stream.later(80, append);
    return;
  }

  stream.send({ event: "chunk", done: false, chunk: "第一段" });
  stream.later(160, () => stream.send({ event: "chunk", done: false, chunk: "第二段" }));
  stream.later(360, () => {
    stream.send({
      event: "done",
      done: true,
      answer: "第一段第二段",
      sources: [{ ref: "1", source: "https://docs.example.test/fixture", score: 0.92, text: "本地合成引用。" }],
      warnings: ["rerank_timeout"],
      index_version: "fixture-v1",
      latency_ms: 360,
    });
    stream.end();
  });
}

async function startAgent(request, response) {
  const body = await readJson(request);
  const sessionId = typeof body.session_id === "string" ? body.session_id : "fixture-session";
  const message = typeof body.message === "string" ? body.message : "";
  const stream = openEventStream(request, response);
  let kind = "approval";
  if (message === "表单交互") kind = "form";
  if (message === "URL 交互") kind = "url";
  if (message === "审批超时") kind = "slow-approval";
  if (message === "路由取消") kind = "route-cancel";
  const session = {
    id: `${kind}-fixture`,
    kind,
    pending: kind !== "route-cancel",
    stream,
    streamFinished: false,
    toolName: kind === "approval" || kind === "slow-approval" ? "send_message" : "mcp__fixture__prompt",
  };
  sessions.set(sessionId, session);
  stream.send({ step: "planning", iteration: 1 });
  stream.send({
    step: "tool_call",
    iteration: 1,
    tool: session.toolName,
    call_id: "fixture-call",
    params: { fixture: true },
  });
}

async function respondToInteraction(request, response, sessionId) {
  const session = sessions.get(sessionId);
  const body = await readJson(request);
  const action = typeof body.action === "string" ? body.action : "unknown";
  interactionActions.push(action);
  if (!session?.pending) {
    json(response, 404, { detail: "closed" });
    return;
  }
  if (session.kind === "slow-approval") {
    hangingResponses.add(response);
    response.on("close", () => hangingResponses.delete(response));
    return;
  }
  session.pending = false;
  response.writeHead(202, { "Cache-Control": "no-store" });
  response.end();
  if (action === "cancel") {
    session.stream.end();
    return;
  }
  finishAgent(session, session.kind === "approval" ? "工具已完成" : "交互已完成");
}

const server = createServer(async (request, response) => {
  const url = new URL(request.url ?? "/", `http://${HOST}:${PORT}`);
  if (url.pathname === "/health") {
    json(response, 200, { status: "ok" });
    return;
  }
  if (url.pathname === "/ready") {
    json(response, readyState === "unavailable" ? 503 : 200, { status: readyState });
    return;
  }
  if (url.pathname.startsWith("/__test__/ready/")) {
    readyState = decodeURIComponent(url.pathname.slice("/__test__/ready/".length));
    json(response, 200, { status: readyState });
    return;
  }
  if (url.pathname === "/__test__/state") {
    json(response, 200, {
      active_streams: activeResponses.size,
      actions: interactionActions,
    });
    return;
  }
  if (url.pathname === "/query/stream" && request.method === "GET") {
    startRag(request, response, url);
    return;
  }
  if (url.pathname === "/agent/chat/stream" && request.method === "POST") {
    await startAgent(request, response);
    return;
  }
  const approvalMatch = url.pathname.match(/^\/agent\/approvals\/([^/]+)(?:\/[^/]+)?$/);
  if (approvalMatch) {
    const sessionId = decodeURIComponent(approvalMatch[1]);
    if (request.method === "POST") await respondToInteraction(request, response, sessionId);
    else json(response, 200, { pending: approvalPayload(sessions.get(sessionId)) });
    return;
  }
  const elicitationMatch = url.pathname.match(/^\/agent\/elicitation\/([^/]+)(?:\/[^/]+)?$/);
  if (elicitationMatch) {
    const sessionId = decodeURIComponent(elicitationMatch[1]);
    if (request.method === "POST") await respondToInteraction(request, response, sessionId);
    else json(response, 200, { pending: elicitationPayload(sessions.get(sessionId)) });
    return;
  }
  if ((url.pathname === "/session/reset" || url.pathname === "/agent/reset") && request.method === "POST") {
    json(response, 200, { status: "reset" });
    return;
  }
  json(response, 404, { detail: "fixture route not found" });
});

server.listen(PORT, HOST);

function shutdown() {
  for (const response of activeResponses) response.destroy();
  for (const response of hangingResponses) response.destroy();
  server.close(() => process.exit(0));
}

process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);
