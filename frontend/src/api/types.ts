export type WorkspaceMode = "rag" | "agent";

export type ConversationStatus =
  | "idle"
  | "connecting"
  | "streaming"
  | "waiting_user"
  | "completed"
  | "failed"
  | "cancelled";

export function isConversationActive(status: ConversationStatus): boolean {
  return status === "connecting" || status === "streaming" || status === "waiting_user";
}

export interface RagSource {
  ref: string;
  source: string;
  score: number | null;
  text: string;
}

export interface CompletionMetadata {
  requestedModel?: "flash" | "pro";
  usedModel?: string;
  skills?: { id: string; name: string; version: string }[];
  attachments?: { id: string; name: string; checksum: string; deleted?: boolean }[];
}

export interface RagCompletion extends CompletionMetadata {
  answer: string;
  sources: RagSource[];
  warnings: string[];
  latencyMs: number | null;
  indexVersion: string;
}

export interface AgentToolStep {
  id: string;
  kind: "planning" | "tool_call" | "tool_done";
  iteration: number | null;
  tool: string;
  params: Record<string, unknown> | null;
  success: boolean | null;
  summary?: string;
}

export interface AgentCompletion extends CompletionMetadata {
  answer: string;
  iterations: number | null;
  latencyMs: number | null;
}

export type StreamUpdate =
  | { type: "chunk"; chunk: string }
  | { type: "tool_step"; step: AgentToolStep };

export type InteractionStatus =
  | "pending"
  | "submitting"
  | "approved"
  | "rejected"
  | "accepted"
  | "declined"
  | "cancelled"
  | "closed"
  | "outcome_unknown";

interface InteractionBase {
  id: string;
  status: InteractionStatus;
  error: string;
}

interface ElicitationInteractionBase extends InteractionBase {
  serverId: string;
  message: string;
}

export interface ApprovalInteraction extends InteractionBase {
  kind: "approval";
  toolName: string;
  source: string;
  provider: string | null;
  category: string;
  params: Record<string, unknown>;
}

export interface FormElicitationInteraction extends ElicitationInteractionBase {
  kind: "form";
  requestedSchema: Record<string, unknown>;
}

export interface UrlElicitationInteraction extends ElicitationInteractionBase {
  kind: "url";
  url: string;
  elicitationId: string | null;
}

export type AgentInteraction =
  | ApprovalInteraction
  | FormElicitationInteraction
  | UrlElicitationInteraction;

export type InteractionDecision =
  | { kind: "approval"; id: string; action: "approve" | "reject" | "cancel" }
  | {
      kind: "form";
      id: string;
      action: "accept";
      content: Record<string, string | number | boolean | string[] | null>;
    }
  | { kind: "form" | "url"; id: string; action: "decline" | "cancel" }
  | { kind: "url"; id: string; action: "accept" };

export function agentInteractionKey(value: Pick<AgentInteraction, "kind" | "id">): string {
  return `${value.kind}:${value.id}`;
}
