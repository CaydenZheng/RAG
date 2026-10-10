import type {
  AgentCompletion,
  AgentToolStep,
  ConversationStatus,
  RagCompletion,
  RagSource,
} from "../api/types";

export interface ConversationState {
  status: ConversationStatus;
  question: string;
  answer: string;
  error: string;
  sources: RagSource[];
  warnings: string[];
  steps: AgentToolStep[];
  latencyMs: number | null;
  indexVersion: string;
  iterations: number | null;
}

export const initialConversationState: ConversationState = {
  status: "idle",
  question: "",
  answer: "",
  error: "",
  sources: [],
  warnings: [],
  steps: [],
  latencyMs: null,
  indexVersion: "—",
  iterations: null,
};

export type ConversationAction =
  | { type: "start"; question: string }
  | { type: "chunk"; chunk: string }
  | { type: "tool_step"; step: AgentToolStep }
  | { type: "complete_rag"; completion: RagCompletion }
  | { type: "complete_agent"; completion: AgentCompletion }
  | { type: "fail"; message: string }
  | { type: "cancel" }
  | { type: "clear" };

function isActive(status: ConversationStatus): boolean {
  return status === "connecting" || status === "streaming";
}

export function conversationReducer(
  state: ConversationState,
  action: ConversationAction,
): ConversationState {
  switch (action.type) {
    case "start":
      return {
        ...initialConversationState,
        status: "connecting",
        question: action.question,
      };
    case "chunk":
      if (!isActive(state.status)) return state;
      return {
        ...state,
        status: "streaming",
        answer: state.answer + action.chunk,
      };
    case "tool_step":
      if (!isActive(state.status)) return state;
      return {
        ...state,
        status: "streaming",
        steps: [...state.steps, action.step],
      };
    case "complete_rag":
      if (!isActive(state.status)) return state;
      return {
        ...state,
        status: "completed",
        answer: action.completion.answer,
        sources: action.completion.sources,
        warnings: action.completion.warnings,
        latencyMs: action.completion.latencyMs,
        indexVersion: action.completion.indexVersion,
      };
    case "complete_agent":
      if (!isActive(state.status)) return state;
      return {
        ...state,
        status: "completed",
        answer: action.completion.answer,
        iterations: action.completion.iterations,
        latencyMs: action.completion.latencyMs,
      };
    case "fail":
      if (!isActive(state.status)) return state;
      return { ...state, status: "failed", error: action.message };
    case "cancel":
      if (!isActive(state.status)) return state;
      return { ...state, status: "cancelled" };
    case "clear":
      return initialConversationState;
  }
}
