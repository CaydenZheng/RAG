import type {
  AgentInteraction,
  AgentCompletion,
  AgentToolStep,
  ConversationStatus,
  CompletionMetadata,
  InteractionStatus,
  RagCompletion,
  RagSource,
} from "../api/types";
import { agentInteractionKey, isConversationActive } from "../api/types";

export interface ConversationTurn extends CompletionMetadata {
  id: number;
  clientTurnId?: string;
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
  interactions: AgentInteraction[];
}

export interface ConversationState {
  status: ConversationStatus;
  turns: ConversationTurn[];
  nextTurnId: number;
}

export const initialConversationState: ConversationState = {
  status: "idle",
  turns: [],
  nextTurnId: 1,
};

export type ConversationAction =
  | { type: "history"; turns: ConversationTurn[]; prepend?: boolean }
  | { type: "start"; question: string }
  | { type: "chunk"; chunk: string }
  | { type: "tool_step"; step: AgentToolStep }
  | { type: "sync_interactions"; interactions: AgentInteraction[] }
  | { type: "interaction_status"; kind: AgentInteraction["kind"]; id: string; status: InteractionStatus; error?: string }
  | { type: "complete_rag"; completion: RagCompletion }
  | { type: "complete_agent"; completion: AgentCompletion }
  | { type: "fail"; message: string }
  | { type: "cancel" }
  | { type: "clear" };

function interactionIsTerminal(status: InteractionStatus): boolean {
  return status !== "pending" && status !== "submitting";
}

function closePendingInteractions(
  turn: ConversationTurn,
  status: "cancelled" | "closed",
): AgentInteraction[] {
  return turn.interactions.map((interaction) => interactionIsTerminal(interaction.status)
    ? interaction
    : { ...interaction, status: interaction.status === "submitting" ? "outcome_unknown" : status });
}

function updateActiveTurn(
  state: ConversationState,
  update: (turn: ConversationTurn) => ConversationTurn,
): ConversationState {
  if (!isConversationActive(state.status) || state.turns.length === 0) return state;
  const turnIndex = state.turns.length - 1;
  const nextTurn = update(state.turns[turnIndex]);
  return {
    ...state,
    status: nextTurn.status,
    turns: state.turns.map((turn, index) => index === turnIndex ? nextTurn : turn),
  };
}

export function conversationReducer(
  state: ConversationState,
  action: ConversationAction,
): ConversationState {
  switch (action.type) {
    case "history": {
      const turns = action.prepend ? [...action.turns, ...state.turns] : action.turns;
      return { status: "idle", turns, nextTurnId: Math.max(0, ...turns.map((turn) => turn.id)) + 1 };
    }
    case "start":
      return {
        status: "connecting",
        nextTurnId: state.nextTurnId + 1,
        turns: [
          ...state.turns,
          {
            id: state.nextTurnId,
            status: "connecting",
            question: action.question,
            answer: "",
            error: "",
            sources: [],
            warnings: [],
            steps: [],
            latencyMs: null,
            indexVersion: "—",
            iterations: null,
            interactions: [],
          },
        ],
      };
    case "chunk":
      return updateActiveTurn(state, (turn) => ({
        ...turn,
        status: "streaming",
        answer: turn.answer + action.chunk,
      }));
    case "tool_step":
      return updateActiveTurn(state, (turn) => ({
        ...turn,
        status: "streaming",
        steps: [...turn.steps, action.step],
      }));
    case "sync_interactions":
      return updateActiveTurn(state, (turn) => {
        const existing = new Map(turn.interactions.map((interaction) => [agentInteractionKey(interaction), interaction]));
        const pendingIds = new Set(action.interactions.map(agentInteractionKey));
        const merged = [...turn.interactions];
        for (const interaction of action.interactions) {
          const interactionKey = agentInteractionKey(interaction);
          const previous = existing.get(interactionKey);
          if (!previous) {
            merged.push(interaction);
          } else if (!interactionIsTerminal(previous.status)) {
            const index = merged.findIndex((item) => agentInteractionKey(item) === interactionKey);
            merged[index] = { ...interaction, status: previous.status, error: previous.error };
          }
        }
        for (let index = 0; index < merged.length; index += 1) {
          const interaction = merged[index];
          if (interaction.status === "pending" && !pendingIds.has(agentInteractionKey(interaction))) {
            merged[index] = { ...interaction, status: "closed" };
          }
        }
        return {
          ...turn,
          status: merged.some((interaction) => !interactionIsTerminal(interaction.status))
            ? "waiting_user"
            : turn.status === "waiting_user" ? "streaming" : turn.status,
          interactions: merged,
        };
      });
    case "interaction_status":
      return updateActiveTurn(state, (turn) => {
        const interactions = turn.interactions.map((interaction) => {
          if (interaction.kind !== action.kind || interaction.id !== action.id || interactionIsTerminal(interaction.status)) return interaction;
          return { ...interaction, status: action.status, error: action.error ?? "" };
        });
        return {
          ...turn,
          status: interactions.some((interaction) => !interactionIsTerminal(interaction.status))
            ? "waiting_user"
            : turn.status === "waiting_user" ? "streaming" : turn.status,
          interactions,
        };
      });
    case "complete_rag":
      return updateActiveTurn(state, (turn) => ({
        ...turn,
        requestedModel: action.completion.requestedModel,
        usedModel: action.completion.usedModel,
        skills: action.completion.skills,
        status: "completed",
        answer: action.completion.answer,
        sources: action.completion.sources,
        warnings: action.completion.warnings,
        latencyMs: action.completion.latencyMs,
        indexVersion: action.completion.indexVersion,
      }));
    case "complete_agent":
      return updateActiveTurn(state, (turn) => ({
        ...turn,
        requestedModel: action.completion.requestedModel,
        usedModel: action.completion.usedModel,
        skills: action.completion.skills,
        status: "completed",
        answer: action.completion.answer,
        iterations: action.completion.iterations,
        latencyMs: action.completion.latencyMs,
        interactions: closePendingInteractions(turn, "closed"),
      }));
    case "fail":
      return updateActiveTurn(state, (turn) => ({
        ...turn,
        status: "failed",
        error: action.message,
        interactions: closePendingInteractions(turn, "closed"),
      }));
    case "cancel":
      return updateActiveTurn(state, (turn) => ({
        ...turn,
        status: "cancelled",
        interactions: closePendingInteractions(turn, "cancelled"),
      }));
    case "clear":
      return initialConversationState;
  }
}
