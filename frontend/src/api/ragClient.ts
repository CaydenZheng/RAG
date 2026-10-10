import { PUBLIC_MESSAGES, PublicClientError } from "./errors";
import { requireEventStream } from "./http";
import { finiteNumber, isRecord, readSseEvents } from "./sse";
import type { RagCompletion, RagSource, StreamUpdate } from "./types";

const RAG_ERROR_MESSAGES: Readonly<Record<string, string>> = Object.freeze({
  answer_generation_failed: PUBLIC_MESSAGES.ragFailed,
  query_capacity_exceeded: "服务繁忙，请稍后重试。",
  request_timeout: "请求处理超时，请稍后重试。",
  retrieval_failed: "检索服务暂时不可用，请稍后重试。",
});

function normalizeSource(value: unknown, index: number): RagSource | null {
  if (!isRecord(value)) return null;
  return {
    ref:
      typeof value.ref === "string" || typeof value.ref === "number"
        ? String(value.ref)
        : String(index + 1),
    source: typeof value.source === "string" ? value.source : "未知来源",
    score: finiteNumber(value.score),
    text: typeof value.text === "string" ? value.text : "",
  };
}

function terminalError(payload: Record<string, unknown>): PublicClientError {
  const error = isRecord(payload.error) ? payload.error : null;
  const code = error && typeof error.code === "string" ? error.code : "";
  return new PublicClientError(
    "terminal_error",
    RAG_ERROR_MESSAGES[code] ?? PUBLIC_MESSAGES.ragFailed,
  );
}

export async function streamRagAnswer(
  query: string,
  sessionId: string,
  signal: AbortSignal,
  onUpdate: (update: StreamUpdate) => void,
): Promise<RagCompletion> {
  const search = new URLSearchParams({ query, session_id: sessionId });
  const response = await fetch(`/query/stream?${search.toString()}`, {
    method: "GET",
    headers: { Accept: "text/event-stream" },
    cache: "no-store",
    signal,
  });
  const stream = requireEventStream(response);
  let streamedAnswer = "";

  for await (const rawEvent of readSseEvents(stream, signal)) {
    if (!isRecord(rawEvent)) continue;
    if (rawEvent.event === "error" || rawEvent.error !== undefined) {
      throw terminalError(rawEvent);
    }
    if (
      rawEvent.event === "chunk" &&
      rawEvent.done === false &&
      typeof rawEvent.chunk === "string"
    ) {
      streamedAnswer += rawEvent.chunk;
      onUpdate({ type: "chunk", chunk: rawEvent.chunk });
      continue;
    }
    if (rawEvent.event !== "done" || rawEvent.done !== true) continue;
    if (typeof rawEvent.answer !== "string" || rawEvent.answer !== streamedAnswer) {
      throw new PublicClientError("protocol", PUBLIC_MESSAGES.protocol);
    }

    const sources = Array.isArray(rawEvent.sources)
      ? rawEvent.sources
          .map(normalizeSource)
          .filter((source): source is RagSource => source !== null)
      : [];
    const warnings = Array.isArray(rawEvent.warnings)
      ? rawEvent.warnings.filter((warning): warning is string => typeof warning === "string")
      : [];
    return {
      answer: rawEvent.answer,
      sources,
      warnings,
      latencyMs: finiteNumber(rawEvent.latency_ms),
      indexVersion:
        typeof rawEvent.index_version === "string" ? rawEvent.index_version : "—",
    };
  }

  throw new PublicClientError("disconnected", PUBLIC_MESSAGES.disconnected);
}
