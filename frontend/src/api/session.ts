import { PUBLIC_MESSAGES, PublicClientError } from "./errors";
import type { WorkspaceMode } from "./types";

const SESSION_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;

export const SESSION_KEYS: Readonly<Record<WorkspaceMode, string>> = Object.freeze({
  rag: "ragflow_session_id",
  agent: "ragflow_agent_session",
});

export function createSessionId(): string {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
}

function readStoredSessionId(key: string): string | null {
  try {
    const value = localStorage.getItem(key);
    return value && SESSION_PATTERN.test(value) ? value : null;
  } catch {
    return null;
  }
}

function storeSessionId(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    // Storage is optional; the in-memory session remains usable.
  }
}

export function loadOrCreateSessionId(mode: WorkspaceMode): string {
  const key = SESSION_KEYS[mode];
  const existing = readStoredSessionId(key);
  if (existing) return existing;
  const created = createSessionId();
  storeSessionId(key, created);
  return created;
}

export async function resetAndRotateSession(
  mode: WorkspaceMode,
  oldSessionId: string,
  signal?: AbortSignal,
): Promise<string> {
  const endpoint = mode === "rag" ? "/session/reset" : "/agent/reset";
  const search = new URLSearchParams({ session_id: oldSessionId });
  let response: Response;
  try {
    response = await fetch(`${endpoint}?${search.toString()}`, {
      method: "POST",
      signal,
    });
  } catch {
    throw new PublicClientError("reset", PUBLIC_MESSAGES.resetFailed);
  }
  if (!response.ok && response.status !== 404) {
    throw new PublicClientError("reset", PUBLIC_MESSAGES.resetFailed);
  }

  const nextSessionId = createSessionId();
  storeSessionId(SESSION_KEYS[mode], nextSessionId);
  return nextSessionId;
}
