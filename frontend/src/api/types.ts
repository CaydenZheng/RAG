export type WorkspaceMode = "rag" | "agent";

export type ConversationStatus =
  | "idle"
  | "connecting"
  | "streaming"
  | "completed"
  | "failed"
  | "cancelled";

export interface RagSource {
  ref: string;
  source: string;
  score: number | null;
  text: string;
}

export interface RagCompletion {
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
}

export interface AgentCompletion {
  answer: string;
  iterations: number | null;
  latencyMs: number | null;
}

export type StreamUpdate =
  | { type: "chunk"; chunk: string }
  | { type: "tool_step"; step: AgentToolStep };
