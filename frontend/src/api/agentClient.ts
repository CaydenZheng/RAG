import { PUBLIC_MESSAGES, PublicClientError } from "./errors";
import { workspaceFetch as fetch } from "./authClient";
import { AUTH_EXPIRED_EVENT } from "./authClient";
import { requireEventStream } from "./http";
import { finiteNumber, isRecord, readSseEvents } from "./sse";
import type { AgentCompletion, AgentToolStep, StreamUpdate } from "./types";
import type { ModelChoice } from "./workspaceClient";
import { completionMetadata } from "./completionMetadata";

const AGENT_FAILURE_MESSAGES: Readonly<Record<string, string>> = Object.freeze({
  account_quota_exceeded: "今日模型使用额度已达上限，请明日再试或联系管理员。",
  agent_timeout: "任务处理超时，请精简任务后重试。",
  agent_token_budget_exceeded: "本轮任务已达到处理预算，请拆分任务后重试。",
  agent_tool_budget_exceeded: "本轮工具调用已达上限，请拆分任务后重试。",
  agent_repeated_tool_call: "规划持续请求重复工具，已停止本轮任务；请核对已有工具结果后调整任务。",
  agent_planning_incomplete: "规划输出未完整结束，请精简任务后重试。",
  agent_planning_upstream_failed: "规划服务暂时不可用，请稍后重试。",
  agent_final_generation_failed: "回答生成未完成，本轮未保存为成功；请精简任务后重试。",
  invalid_agent_plan: "规划格式无效，本轮任务未完成，请调整任务后重试。",
});

function safeParams(value: unknown): Record<string, unknown> | null {
  return isRecord(value) ? value : null;
}

function stepFromEvent(
  event: Record<string, unknown>,
  sequence: number,
): AgentToolStep | null {
  const kind = event.step;
  if (kind !== "planning" && kind !== "tool_call" && kind !== "tool_done") {
    return null;
  }
  return {
    id:
      typeof event.call_id === "string" && event.call_id
        ? `${event.call_id}-${kind}-${sequence}`
        : `${kind}-${sequence}`,
    kind,
    iteration: finiteNumber(event.iteration),
    tool: typeof event.tool === "string" ? event.tool : "未知工具",
    params: kind === "tool_call" ? safeParams(event.params) : null,
    success: kind === "tool_done" && typeof event.success === "boolean" ? event.success : null,
  };
}

export async function streamAgentAnswer(
  message: string,
  sessionId: string,
  signal: AbortSignal,
  onUpdate: (update: StreamUpdate) => void,
  clientTurnId?: string,
  model?: ModelChoice,
  skillIds?: readonly string[],
): Promise<AgentCompletion> {
  const response = await fetch("/agent/chat/stream", {
    method: "POST",
    headers: {
      Accept: "text/event-stream",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      message,
      session_id: sessionId,
      ...(clientTurnId ? { conversation_id: sessionId, client_turn_id: clientTurnId } : {}),
      ...(model ? { model } : {}),
      ...(skillIds && skillIds.length ? { skill_ids: [...skillIds] } : {}),
    }),
    signal,
  });
  const stream = await requireEventStream(response);
  let streamedAnswer = "";
  let stepSequence = 0;

  for await (const rawEvent of readSseEvents(stream, signal)) {
    if (!isRecord(rawEvent)) continue;
    const errorCode = rawEvent.code || (rawEvent.done === true ? rawEvent.error_code : undefined);
    if ((rawEvent.error !== undefined && rawEvent.error !== "") || errorCode) {
      if (errorCode === "auth_required") window.dispatchEvent(new Event(AUTH_EXPIRED_EVENT));
      throw new PublicClientError("terminal_error", typeof errorCode === "string" &&
        Object.hasOwn(AGENT_FAILURE_MESSAGES, errorCode)
        ? AGENT_FAILURE_MESSAGES[errorCode] : PUBLIC_MESSAGES.agentFailed);
    }

    const step = stepFromEvent(rawEvent, ++stepSequence);
    if (step) {
      onUpdate({ type: "tool_step", step });
      continue;
    }
    if (typeof rawEvent.chunk === "string" && rawEvent.chunk) {
      streamedAnswer += rawEvent.chunk;
      onUpdate({ type: "chunk", chunk: rawEvent.chunk });
      continue;
    }
    if (rawEvent.done !== true) continue;
    if (typeof rawEvent.answer !== "string" || rawEvent.answer !== streamedAnswer) {
      throw new PublicClientError("protocol", PUBLIC_MESSAGES.protocol);
    }
    return {
      ...completionMetadata(rawEvent),
      answer: rawEvent.answer,
      iterations: finiteNumber(rawEvent.iterations),
      latencyMs: finiteNumber(rawEvent.latency_ms),
    };
  }

  throw new PublicClientError("disconnected", PUBLIC_MESSAGES.disconnected);
}
