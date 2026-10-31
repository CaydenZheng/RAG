import { PublicClientError } from "../api/errors";
import type {
  AgentCompletion, AgentInteraction, InteractionDecision, RagCompletion, StreamUpdate, WorkspaceMode,
} from "../api/types";

const CHUNK_DELAY_MS: number = 60;
const INTERACTION_TIMEOUT_MS: number = 25_000;

interface PendingDemo {
  interaction: AgentInteraction;
  decision: InteractionDecision | null;
}

const pending: Map<string, PendingDemo> = new Map();

function delay(milliseconds: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    signal.throwIfAborted();
    const timer: number = window.setTimeout(() => {
      signal.removeEventListener("abort", abort);
      resolve();
    }, milliseconds);
    function abort(): void {
      window.clearTimeout(timer);
      reject(new DOMException("Demo cancelled", "AbortError"));
    }
    signal.addEventListener("abort", abort, { once: true });
  });
}

async function streamText(answer: string, signal: AbortSignal, onUpdate: (update: StreamUpdate) => void): Promise<void> {
  for (let offset: number = 0; offset < answer.length; offset += 6) {
    await delay(CHUNK_DELAY_MS, signal);
    onUpdate({ type: "chunk", chunk: answer.slice(offset, offset + 6) });
  }
}

export function createSessionId(): string {
  return crypto.randomUUID();
}

export function loadOrCreateSessionId(_mode: WorkspaceMode): string {
  return createSessionId();
}

export async function resetAndRotateSession(
  _mode: WorkspaceMode, oldSessionId: string, signal?: AbortSignal,
): Promise<string> {
  signal?.throwIfAborted();
  pending.delete(oldSessionId);
  return createSessionId();
}

export async function streamRagAnswer(
  query: string, _sessionId: string, signal: AbortSignal, onUpdate: (update: StreamUpdate) => void,
): Promise<RagCompletion> {
  await delay(200, signal);
  if (query.includes("错误")) throw new PublicClientError("terminal_error", "模拟错误终态：本轮未保存为成功回答。");
  const answer: string = "这是**预设模拟回答**，不是模型推理结果。\n\n本项目将知识问答与 Agent 放在统一工作台中：\n\n1. 知识问答展示流式文本与引用来源。[1]\n2. Agent 展示模拟工具过程、审批与补充输入。[2]\n3. 可切换主题、停止输出或新建会话。\n\n真实本地部署需要 FastAPI、模型凭据与可用索引；此网页不包含这些服务。";
  await streamText(answer, signal, onUpdate);
  return {
    answer,
    sources: [
      { ref: "1", source: "模拟资料 · 工作台介绍", score: null, text: "预设示例：知识问答通过检索与生成提供引用；此演示未运行检索。" },
      { ref: "2", source: "模拟资料 · Agent 交互", score: null, text: "预设示例：真实工具调用必须经过后端 Registry、安全策略、Schema、审批与审计。" },
    ],
    warnings: [], latencyMs: null, indexVersion: "demo · 非真实索引",
  };
}

function demoInteraction(message: string): AgentInteraction | null {
  const base: { id: string; status: "pending"; error: string } = { id: crypto.randomUUID(), status: "pending", error: "" };
  if (message.includes("审批")) return {
    ...base, kind: "approval", toolName: "demo.preview", source: "模拟工具（不执行）",
    provider: "Demo（无真实 Provider）", category: "演示", params: { example: "只展示预设结果" },
  };
  if (message.includes("表单")) return {
    ...base, kind: "form", serverId: "demo · 非真实 MCP", message: "模拟表单：请使用示例文字，不要填写个人信息。内容只用于当前页面交互，不发送或保存。",
    requestedSchema: { type: "object", properties: { topic: { type: "string", title: "示例主题" } }, required: ["topic"] },
  };
  if (/url/i.test(message)) return {
    ...base, kind: "url", serverId: "demo · 非真实 MCP", message: "模拟 URL 交互",
    url: "https://github.com/CaydenZheng/RAG", elicitationId: null,
  };
  return null;
}

export async function streamAgentAnswer(
  message: string, sessionId: string, signal: AbortSignal, onUpdate: (update: StreamUpdate) => void,
): Promise<AgentCompletion> {
  let entry: PendingDemo | null = null;
  try {
    await delay(200, signal);
    onUpdate({ type: "tool_step", step: { id: "demo-plan", kind: "planning", iteration: 1, tool: "模拟规划", params: null, success: null } });
    if (message.includes("超时")) {
      await delay(INTERACTION_TIMEOUT_MS, signal);
      throw new PublicClientError("terminal_error", "模拟 25 秒超时：未执行工具，本轮未保存为成功回答。");
    }
    if (message.includes("错误")) throw new PublicClientError("terminal_error", "模拟 Agent 错误：未执行工具，本轮未保存为成功回答。");
    const interaction: AgentInteraction | null = demoInteraction(message);
    let allowed: boolean = true;
    if (interaction) {
      entry = { interaction, decision: null };
      pending.set(sessionId, entry);
      const deadline: number = Date.now() + INTERACTION_TIMEOUT_MS;
      while (!entry.decision) {
        await delay(100, signal);
        if (Date.now() >= deadline && !entry.decision) {
          throw new PublicClientError("terminal_error", "模拟交互等待 25 秒超时：未执行工具。");
        }
      }
      allowed = entry.decision.action === "approve" || entry.decision.action === "accept";
    }
    if (allowed) {
      onUpdate({ type: "tool_step", step: { id: "demo-call", kind: "tool_call", iteration: 1, tool: "demo.preview（模拟）", params: { example: "预设示例，无实际执行" }, success: null } });
      await delay(600, signal);
      onUpdate({ type: "tool_step", step: { id: "demo-result", kind: "tool_done", iteration: 1, tool: "demo.preview（模拟）", params: null, success: true, summary:"预设模拟结果，未执行真实工具。" } });
    }
    const answer: string = allowed
      ? "**模拟任务完成**。上方展示的是预设工具过程，不是实际执行结果。\n\n此演示没有调用模型或 MCP，也没有提交表单数据或进行 OAuth 授权。真实工具仍须通过后端安全链。\n\n你可以继续体验“审批”“表单”“URL”“错误”或“超时”，也可以在输出期间点击停止。"
      : "**模拟交互已拒绝**。没有运行工具，也没有提交任何信息。";
    await streamText(answer, signal, onUpdate);
    return { answer, iterations: 1, latencyMs: null };
  } finally {
    if (entry && pending.get(sessionId) === entry) pending.delete(sessionId);
  }
}

export async function fetchPendingInteractions(sessionId: string, signal: AbortSignal): Promise<AgentInteraction[]> {
  signal.throwIfAborted();
  const entry: PendingDemo | undefined = pending.get(sessionId);
  return entry && !entry.decision ? [entry.interaction] : [];
}

export async function postInteractionDecision(
  sessionId: string, decision: InteractionDecision, signal: AbortSignal,
): Promise<void> {
  signal.throwIfAborted();
  const entry: PendingDemo | undefined = pending.get(sessionId);
  if (!entry || entry.decision || entry.interaction.id !== decision.id || entry.interaction.kind !== decision.kind) {
    throw new PublicClientError("interaction_closed", "模拟交互已结束。");
  }
  // Form contents are deliberately neither read nor retained by the demo.
  entry.decision = decision.kind === "form" && decision.action === "accept"
    ? { kind: "form", id: decision.id, action: "accept", content: {} }
    : decision;
}
