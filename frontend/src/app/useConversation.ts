import { useCallback, useEffect, useReducer, useRef, useState } from "react";

import { streamAgentAnswer } from "../api/agentClient";
import { isAbortFailure, toPublicMessage } from "../api/errors";
import { streamRagAnswer } from "../api/ragClient";
import { loadOrCreateSessionId, resetAndRotateSession } from "../api/session";
import type { StreamUpdate, WorkspaceMode } from "../api/types";
import { conversationReducer, initialConversationState } from "./conversationState";

const RESET_CONFIRMATION: Readonly<Record<WorkspaceMode, string>> = Object.freeze({
  rag: "新建对话会清除当前检索历史，是否继续？",
  agent: "新建对话会清除当前 Agent 历史，是否继续？",
});

export function useConversation(mode: WorkspaceMode) {
  const [state, dispatch] = useReducer(conversationReducer, initialConversationState);
  const [sessionId, setSessionId] = useState(() => loadOrCreateSessionId(mode));
  const [resetting, setResetting] = useState(false);
  const [notice, setNotice] = useState("");
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

      stop(false);
      setNotice("");
      const controller = new AbortController();
      activeController.current = controller;
      const runId = ++runSequence.current;
      dispatch({ type: "start", question: message });

      const onUpdate = (update: StreamUpdate): void => {
        if (runSequence.current !== runId || controller.signal.aborted) return;
        dispatch(update);
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
        if (runSequence.current === runId) activeController.current = null;
      }
    },
    [mode, sessionId, stop],
  );

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

  return { state, send, stop, reset, resetting, notice };
}
