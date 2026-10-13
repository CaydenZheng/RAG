import { isRecord } from "./sse";
import { PUBLIC_MESSAGES, PublicClientError } from "./errors";
import type { AgentInteraction, InteractionDecision } from "./types";

const INTERACTION_HEADERS = Object.freeze({
  Accept: "application/json",
  "Cache-Control": "no-store",
});
const INTERACTION_REQUEST_TIMEOUT_MS = 25_000;

interface RequestScope {
  controller: AbortController;
  didTimeOut: () => boolean;
  close: () => void;
}

function createRequestScope(parentSignal: AbortSignal): RequestScope {
  const controller = new AbortController();
  let timedOut = false;
  const abortFromParent = (): void => controller.abort(parentSignal.reason);
  if (parentSignal.aborted) abortFromParent();
  else parentSignal.addEventListener("abort", abortFromParent, { once: true });
  const timeout = window.setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, INTERACTION_REQUEST_TIMEOUT_MS);
  return {
    controller,
    didTimeOut: () => timedOut,
    close: () => {
      window.clearTimeout(timeout);
      parentSignal.removeEventListener("abort", abortFromParent);
      controller.abort();
    },
  };
}

function stringField(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

function parseApproval(value: unknown): AgentInteraction | null {
  if (!isRecord(value)) return null;
  const id = stringField(value.id);
  const toolName = stringField(value.tool_name);
  const source = stringField(value.source);
  const category = stringField(value.category);
  if (!id || !toolName || !source || !category || !isRecord(value.params)) return null;
  if (value.provider !== null && typeof value.provider !== "string") return null;
  return {
    kind: "approval",
    id,
    status: "pending",
    error: "",
    toolName,
    source,
    provider: value.provider,
    category,
    params: value.params,
  };
}

function parseElicitation(value: unknown): AgentInteraction | null {
  if (!isRecord(value)) return null;
  const id = stringField(value.id);
  const serverId = stringField(value.server_id);
  const message = stringField(value.message);
  if (!id || !serverId || !message) return null;
  if (value.mode === "form" && isRecord(value.requested_schema)) {
    return {
      kind: "form",
      id,
      serverId,
      status: "pending",
      error: "",
      message,
      requestedSchema: value.requested_schema,
    };
  }
  if (value.mode === "url") {
    const url = stringField(value.url);
    if (!url) return null;
    if (value.elicitation_id !== null && value.elicitation_id !== undefined && typeof value.elicitation_id !== "string") {
      return null;
    }
    return {
      kind: "url",
      id,
      serverId,
      status: "pending",
      error: "",
      message,
      url,
      elicitationId: typeof value.elicitation_id === "string" ? value.elicitation_id : null,
    };
  }
  return null;
}

async function readPending(
  path: string,
  signal: AbortSignal,
  parse: (value: unknown) => AgentInteraction | null,
): Promise<AgentInteraction[]> {
  const response = await fetch(path, { headers: INTERACTION_HEADERS, signal });
  if (!response.ok) {
    throw new PublicClientError("interaction_unavailable", PUBLIC_MESSAGES.interactionUnavailable);
  }
  const payload: unknown = await response.json().catch(() => null);
  if (!isRecord(payload) || !Array.isArray(payload.pending)) {
    throw new PublicClientError("interaction_unavailable", PUBLIC_MESSAGES.interactionUnavailable);
  }
  return payload.pending.map(parse).filter((item): item is AgentInteraction => item !== null);
}

export async function fetchPendingInteractions(
  sessionId: string,
  signal: AbortSignal,
): Promise<AgentInteraction[]> {
  const encodedSession = encodeURIComponent(sessionId);
  const requestScope = createRequestScope(signal);
  try {
    const [approvals, elicitations] = await Promise.all([
      readPending(`/agent/approvals/${encodedSession}`, requestScope.controller.signal, parseApproval),
      readPending(`/agent/elicitation/${encodedSession}`, requestScope.controller.signal, parseElicitation),
    ]);
    return [...approvals, ...elicitations];
  } catch (error) {
    if (requestScope.didTimeOut()) {
      throw new PublicClientError("interaction_unavailable", PUBLIC_MESSAGES.interactionUnavailable);
    }
    throw error;
  } finally {
    requestScope.close();
  }
}

export async function postInteractionDecision(
  sessionId: string,
  decision: InteractionDecision,
  signal: AbortSignal,
): Promise<void> {
  const family = decision.kind === "approval" ? "approvals" : "elicitation";
  const body = "content" in decision
    ? { action: decision.action, content: decision.content }
    : { action: decision.action };
  const requestScope = createRequestScope(signal);
  let response: Response;
  try {
    response = await fetch(
      `/agent/${family}/${encodeURIComponent(sessionId)}/${encodeURIComponent(decision.id)}`,
      {
        method: "POST",
        headers: { ...INTERACTION_HEADERS, "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal: requestScope.controller.signal,
      },
    );
  } catch (error) {
    if (signal.aborted) throw error;
    throw new PublicClientError(
      "interaction_outcome_unknown",
      PUBLIC_MESSAGES.interactionOutcomeUnknown,
    );
  } finally {
    requestScope.close();
  }
  if (response.status === 400) {
    throw new PublicClientError("interaction_invalid", PUBLIC_MESSAGES.interactionInvalid);
  }
  if (response.status === 404) {
    throw new PublicClientError("interaction_closed", PUBLIC_MESSAGES.interactionClosed);
  }
  if (response.status === 413) {
    throw new PublicClientError("interaction_too_large", PUBLIC_MESSAGES.interactionTooLarge);
  }
  if (!response.ok) {
    throw new PublicClientError(
      "interaction_outcome_unknown",
      PUBLIC_MESSAGES.interactionOutcomeUnknown,
    );
  }
}
