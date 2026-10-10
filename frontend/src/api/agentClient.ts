import { PUBLIC_MESSAGES, PublicClientError } from "./errors";
import { requireEventStream } from "./http";
import { finiteNumber, isRecord, readSseEvents } from "./sse";
import type { AgentCompletion, AgentToolStep, StreamUpdate } from "./types";

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
): Promise<AgentCompletion> {
  const response = await fetch("/agent/chat/stream", {
    method: "POST",
    headers: {
      Accept: "text/event-stream",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ message, session_id: sessionId }),
    signal,
  });
  const stream = requireEventStream(response);
  let streamedAnswer = "";
  let stepSequence = 0;

  for await (const rawEvent of readSseEvents(stream, signal)) {
    if (!isRecord(rawEvent)) continue;
    if (rawEvent.error !== undefined) {
      throw new PublicClientError("terminal_error", PUBLIC_MESSAGES.agentFailed);
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
      answer: rawEvent.answer,
      iterations: finiteNumber(rawEvent.iterations),
      latencyMs: finiteNumber(rawEvent.latency_ms),
    };
  }

  throw new PublicClientError("disconnected", PUBLIC_MESSAGES.disconnected);
}
