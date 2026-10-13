import {
  useCallback,
  useEffect,
  useReducer,
  useRef,
  useState,
  type Dispatch,
  type SetStateAction,
} from "react";

import { streamAgentAnswer } from "../api/agentClient";
import { isAbortFailure, PUBLIC_MESSAGES, PublicClientError, toPublicMessage } from "../api/errors";
import { fetchPendingInteractions, postInteractionDecision } from "../api/interactionClient";
import { streamRagAnswer } from "../api/ragClient";
import { loadOrCreateSessionId, resetAndRotateSession } from "../api/session";
import { type AgentInteraction, type InteractionDecision, type InteractionStatus, type StreamUpdate, type WorkspaceMode } from "../api/types";
import {
  conversationReducer,
  initialConversationState,
  type ConversationState,
} from "./conversationState";

const RESET_CONFIRMATION: Readonly<Record<WorkspaceMode, string>> = Object.freeze({
  rag: "新建对话会清除当前检索历史，是否继续？",
  agent: "新建对话会清除当前 Agent 历史，是否继续？",
});

const INTERACTION_POLL_MS = 350;

interface ConversationController {
  state: ConversationState;
  send: (rawMessage: string) => Promise<void>;
  stop: (markCancelled?: boolean) => void;
  reset: () => Promise<void>;
  respondToInteraction: (decision: InteractionDecision) => Promise<void>;
  resetting: boolean;
  notice: string;
  draft: string;
  setDraft: Dispatch<SetStateAction<string>>;
}

function abortableDelay(milliseconds: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal.aborted) {
      resolve();
      return;
    }
    const timer = window.setTimeout(done, milliseconds);
    function done(): void {
      window.clearTimeout(timer);
      signal.removeEventListener("abort", done);
      resolve();
    }
    signal.addEventListener("abort", done, { once: true });
  });
}

function decisionStatus(decision: InteractionDecision): InteractionStatus {
  if (decision.action === "cancel") return "cancelled";
  if (decision.action === "approve") return "approved";
  if (decision.action === "reject") return "rejected";
  if (decision.action === "accept") return "accepted";
  return "declined";
}

function interactionError(error: unknown): string {
  return error instanceof PublicClientError
    ? error.message
    : PUBLIC_MESSAGES.interactionUnavailable;
}

export function useConversation(mode: WorkspaceMode): ConversationController {
  const [state, dispatch] = useReducer(conversationReducer, initialConversationState);
  const [sessionId, setSessionId] = useState(() => loadOrCreateSessionId(mode));
  const [resetting, setResetting] = useState(false);
  const [notice, setNotice] = useState("");
  const [draft, setDraft] = useState("");
  const activeController = useRef<AbortController | null>(null);
  const resetController = useRef<AbortController | null>(null);
  const resettingRef = useRef(false);
  const runSequence = useRef(0);

  const stop = useCallback((markCancelled = true) => {
    runSequence.current += 1;
    const controller = activeController.current;
    controller?.abort();
    activeController.current = null;
    if (controller && markCancelled) dispatch({ type: "cancel" });
  }, []);

  const send = useCallback(
    async (rawMessage: string): Promise<void> => {
      const message = rawMessage.trim();
      if (resettingRef.current || !message || message.length > 2000) return;

      stop(true);
      setNotice("");
      const controller = new AbortController();
      activeController.current = controller;
      const runId = ++runSequence.current;
      dispatch({ type: "start", question: message });
      setDraft("");

      const onUpdate = (update: StreamUpdate): void => {
        if (runSequence.current !== runId || controller.signal.aborted) return;
        dispatch(update);
      };

      const syncInteractions = (interactions: AgentInteraction[]): void => {
        if (runSequence.current !== runId || controller.signal.aborted) return;
        dispatch({ type: "sync_interactions", interactions });
      };

      const pollInteractions = async (): Promise<void> => {
        while (!controller.signal.aborted && runSequence.current === runId) {
          try {
            syncInteractions(await fetchPendingInteractions(sessionId, controller.signal));
          } catch (error) {
            if (isAbortFailure(error, controller.signal)) return;
          }
          await abortableDelay(INTERACTION_POLL_MS, controller.signal);
        }
      };

      try {
        if (mode === "rag") {
          const completion = await streamRagAnswer(
            message,
            sessionId,
            controller.signal,
            onUpdate,
          );
          if (runSequence.current === runId && !controller.signal.aborted) {
            dispatch({ type: "complete_rag", completion });
          }
        } else {
          void pollInteractions();
          const completion = await streamAgentAnswer(
            message,
            sessionId,
            controller.signal,
            onUpdate,
          );
          if (runSequence.current === runId && !controller.signal.aborted) {
            dispatch({ type: "complete_agent", completion });
          }
        }
      } catch (error) {
        if (
          runSequence.current === runId &&
          !isAbortFailure(error, controller.signal)
        ) {
          dispatch({ type: "fail", message: toPublicMessage(error) });
        }
      } finally {
        if (runSequence.current === runId) {
          controller.abort();
          activeController.current = null;
        }
      }
    },
    [mode, sessionId, stop],
  );

  const respondToInteraction = useCallback(async (decision: InteractionDecision): Promise<void> => {
    const controller = activeController.current;
    if (!controller || controller.signal.aborted) return;
    const runId = runSequence.current;
    dispatch({ type: "interaction_status", kind: decision.kind, id: decision.id, status: "submitting" });
    try {
      await postInteractionDecision(sessionId, decision, controller.signal);
      if (runSequence.current !== runId || controller.signal.aborted) return;
      dispatch({ type: "interaction_status", kind: decision.kind, id: decision.id, status: decisionStatus(decision) });
      if (decision.action === "cancel") stop(true);
    } catch (error) {
      if (runSequence.current !== runId) return;
      if (error instanceof PublicClientError && error.code === "interaction_outcome_unknown") {
        dispatch({ type: "interaction_status", kind: decision.kind, id: decision.id, status: "outcome_unknown", error: interactionError(error) });
      } else if (isAbortFailure(error, controller.signal)) {
        return;
      } else if (error instanceof PublicClientError && error.code === "interaction_closed") {
        dispatch({ type: "interaction_status", kind: decision.kind, id: decision.id, status: "closed", error: interactionError(error) });
      } else {
        dispatch({ type: "interaction_status", kind: decision.kind, id: decision.id, status: "pending", error: interactionError(error) });
      }
    }
  }, [sessionId, stop]);

  const reset = useCallback(async (): Promise<void> => {
    if (resettingRef.current) return;
    if (!window.confirm(RESET_CONFIRMATION[mode])) return;

    resettingRef.current = true;
    stop(true);
    setNotice("");
    setResetting(true);
    const controller = new AbortController();
    resetController.current = controller;
    try {
      const nextSessionId = await resetAndRotateSession(
        mode,
        sessionId,
        controller.signal,
      );
      stop(false);
      setSessionId(nextSessionId);
      dispatch({ type: "clear" });
    } catch (error) {
      if (!isAbortFailure(error, controller.signal)) {
        setNotice(toPublicMessage(error));
      }
    } finally {
      if (resetController.current === controller) resetController.current = null;
      resettingRef.current = false;
      setResetting(false);
    }
  }, [mode, sessionId, stop]);

  useEffect(
    () => () => {
      runSequence.current += 1;
      resettingRef.current = false;
      activeController.current?.abort();
      resetController.current?.abort();
    },
    [],
  );

  return { state, send, stop, reset, respondToInteraction, resetting, notice, draft, setDraft };
}
