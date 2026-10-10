import { PUBLIC_MESSAGES, PublicClientError } from "./errors";

function findEventBoundary(buffer: string): { index: number; length: number } | null {
  const match = /\r\n\r\n|\r\n\n|\n\r\n|\n\n|\r\r/.exec(buffer);
  return match ? { index: match.index, length: match[0].length } : null;
}

function parseDataBlock(block: string): unknown | null {
  const data = block
    .split(/\r\n|\r|\n/)
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(5).replace(/^ /, ""))
    .join("\n");

  if (!data) return null;
  try {
    return JSON.parse(data) as unknown;
  } catch {
    throw new PublicClientError("protocol", PUBLIC_MESSAGES.protocol);
  }
}

export async function* readSseEvents(
  stream: ReadableStream<Uint8Array>,
  signal: AbortSignal,
): AsyncGenerator<unknown> {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let streamEnded = false;
  const cancelReader = (): void => {
    void reader.cancel();
  };

  signal.addEventListener("abort", cancelReader, { once: true });
  try {
    while (!streamEnded) {
      if (signal.aborted) {
        throw new DOMException("Aborted", "AbortError");
      }

      const result = await reader.read();
      streamEnded = result.done;
      buffer += decoder.decode(result.value, { stream: !streamEnded });

      let boundary = findEventBoundary(buffer);
      while (boundary) {
        const block = buffer.slice(0, boundary.index);
        buffer = buffer.slice(boundary.index + boundary.length);
        const event = parseDataBlock(block);
        if (event !== null) yield event;
        boundary = findEventBoundary(buffer);
      }
    }
  } finally {
    signal.removeEventListener("abort", cancelReader);
    if (!streamEnded) {
      await reader.cancel().catch(() => undefined);
    }
    reader.releaseLock();
  }
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function finiteNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}
