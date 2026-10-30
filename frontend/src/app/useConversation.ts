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
import { createSessionId, loadOrCreateSessionId, resetAndRotateSession } from "../api/session";
import { createConversation, readConversation, readHistory, updateConversation, switchConversationModel, type ConversationSummary } from "../api/conversationClient";
import type { ModelChoice } from "../api/workspaceClient";
import { isConversationActive, type AgentInteraction, type InteractionDecision, type InteractionStatus, type StreamUpdate, type WorkspaceMode } from "../api/types";
import {
  conversationReducer,
  initialConversationState,
  type ConversationState,
} from "./conversationState";
import { IS_PAGES_DEMO, modeHref } from "./runtime";

const RESET_CONFIRMATION: Readonly<Record<WorkspaceMode, string>> = Object.freeze({
  rag: "新建对话会清除当前检索历史，是否继续？",
  agent: "新建对话会清除当前 Agent 历史，是否继续？",
});

const INTERACTION_POLL_MS = 350;

interface ConversationController {
  state: ConversationState;
  send: (rawMessage: string, skillIds?: readonly string[]) => Promise<void>;
  stop: (markCancelled?: boolean) => void;
  reset: () => Promise<void>;
  respondToInteraction: (decision: InteractionDecision) => Promise<void>;
  resetting: boolean;
  notice: string;
  draft: string;
  setDraft: Dispatch<SetStateAction<string>>;
  sessionId: string;
  summary: ConversationSummary | null;
  directoryRevision: number;
  nextBefore: string | null;
  open: (id: string) => Promise<void>;
  loadOlder: () => Promise<void>;
  mutate: (action: "rename" | "clear" | "delete", title?: string) => Promise<boolean>;
  mutateConversation: (
    item: ConversationSummary,
    action: "rename" | "clear" | "delete",
    title?: string,
  ) => Promise<boolean>;
  reconcile: () => Promise<void>;
  unconfirmed: boolean;
  forget: () => void;
  model: ModelChoice;
  chooseModel: (model: ModelChoice) => Promise<void>;
  modelUnconfirmed: boolean;
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

export function useConversation(mode: WorkspaceMode, account = false, defaultModel: ModelChoice = "flash"): ConversationController {
  const [state, dispatch] = useReducer(conversationReducer, initialConversationState);
  const [sessionId, setSessionId] = useState(() => account ? createSessionId() : loadOrCreateSessionId(mode));
  const [summary, setSummary] = useState<ConversationSummary | null>(null);
  const [directoryRevision, setDirectoryRevision] = useState(0);
  const [nextBefore, setNextBefore] = useState<string | null>(null);
  const [uncertainTurn, setUncertainTurn] = useState<string | null>(null);
  const uncertainTurnRef = useRef<string | null>(null);
  const [resetting, setResetting] = useState(false);
  const [notice, setNotice] = useState("");
  const [draft, setDraft] = useState("");
  const activeController = useRef<AbortController | null>(null);
  const resetController = useRef<AbortController | null>(null);
  const resettingRef = useRef(false);
  const runSequence = useRef(0);
  const [model, setModel] = useState<ModelChoice>(defaultModel);
  const modelRef = useRef<ModelChoice>(defaultModel);
  const manualModel = useRef(false);
  const modelUnconfirmedRef = useRef(false);
  const [modelUnconfirmed, setModelUnconfirmed] = useState(false);
  const applyModel = useCallback((choice: ModelChoice): void => { modelRef.current = choice; setModel(choice); }, []);
  useEffect(() => {
    if (!summary && !manualModel.current) applyModel(defaultModel);
  }, [applyModel,defaultModel,summary]);

  const stop = useCallback((markCancelled = true) => {
    runSequence.current += 1;
    resetController.current?.abort();
    resetController.current = null;
    resettingRef.current = false;
    setResetting(false);
    const controller = activeController.current;
    controller?.abort();
    activeController.current = null;
    if (controller && markCancelled) dispatch({ type: "cancel" });
  }, []);

  const forget = useCallback((): void => {
    stop(true);
    setSessionId(createSessionId()); setSummary(null); setNextBefore(null);
    uncertainTurnRef.current = null; setUncertainTurn(null);
    modelUnconfirmedRef.current = false; setModelUnconfirmed(false);
    manualModel.current = false; applyModel(defaultModel);
    dispatch({type:"clear"}); setDraft(""); setNotice("");
  }, [applyModel,defaultModel,stop]);

  const send = useCallback(
    async (rawMessage: string, skillIds?: readonly string[]): Promise<void> => {
      const message = rawMessage.trim();
      if (resettingRef.current || uncertainTurnRef.current || modelUnconfirmedRef.current || !message || message.length > 2000) return;

      stop(true);
      setNotice("");
      const controller = new AbortController();
      activeController.current = controller;
      const runId = ++runSequence.current;
      const clientTurnId = account ? createSessionId() : undefined;
      const requestedModel = modelRef.current;
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
        if (account && !summary) {
          const created = await createConversation(sessionId,mode,controller.signal,requestedModel);
          if (controller.signal.aborted || runSequence.current !== runId) return;
          setSummary(created);
          window.history.replaceState({}, "", `${modeHref(mode)}?conversation=${encodeURIComponent(sessionId)}`);
        }
        if (mode === "rag") {
          const completion = await streamRagAnswer(
            message,
            sessionId,
            controller.signal,
            onUpdate,
            clientTurnId,
            account || IS_PAGES_DEMO ? requestedModel : undefined,
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
            clientTurnId,
            account || IS_PAGES_DEMO ? requestedModel : undefined,
            skillIds,
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
          if (account && clientTurnId && (!(error instanceof PublicClientError) || ["disconnected", "http", "protocol"].includes(error.code))) {
            uncertainTurnRef.current = clientTurnId;
            setUncertainTurn(clientTurnId);
            setNotice("本轮保存状态未确认，不会自动重发。请核对历史；查询无结果不代表工具未执行。");
          }
        }
      } finally {
        if (runSequence.current === runId) {
          controller.abort();
          activeController.current = null;
          if (account) setDirectoryRevision((value) => value + 1);
        }
      }
    },
    [account, mode, sessionId, stop, summary],
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
    if (!account && !window.confirm(RESET_CONFIRMATION[mode])) return;

    stop(true);
    resettingRef.current = true;
    setNotice("");
    setResetting(true);
    const controller = new AbortController();
    resetController.current = controller;
    try {
      const nextSessionId = account ? createSessionId() : await resetAndRotateSession(mode,sessionId,controller.signal);
      const created = account ? await createConversation(nextSessionId,mode,controller.signal,modelRef.current) : null;
      if (controller.signal.aborted) return;
      stop(false);
      setSessionId(nextSessionId);
      setSummary(created);
      setNextBefore(null);
      uncertainTurnRef.current = null; setUncertainTurn(null);
      modelUnconfirmedRef.current = false; setModelUnconfirmed(false);
      if (account) {
        setDirectoryRevision((value) => value + 1);
        window.history.pushState({}, "", `${modeHref(mode)}?conversation=${encodeURIComponent(nextSessionId)}`);
      }
      dispatch({ type: "clear" });
    } catch (error) {
      if (!isAbortFailure(error, controller.signal)) {
        setNotice(toPublicMessage(error));
      }
    } finally {
      if (resetController.current === controller) {
        resetController.current = null;
        resettingRef.current = false;
        setResetting(false);
      }
    }
  }, [account, mode, sessionId, stop]);

  const open = useCallback(async (id: string): Promise<void> => {
    if (!account) return;
    resetController.current?.abort();
    stop(true);
    resettingRef.current = true; setResetting(true); setNotice("");
    const controller = new AbortController(); resetController.current = controller;
    const runId = runSequence.current;
    try {
      const [item, page] = await Promise.all([readConversation(id,controller.signal),readHistory(id,controller.signal)]);
      if (controller.signal.aborted || runSequence.current !== runId) return;
      if (item.mode !== mode) throw new PublicClientError("history", "该会话属于另一种模式，请从相应目录打开。");
      setSessionId(id); setSummary(item); setNextBefore(page.nextBefore);
      applyModel(item.model ?? defaultModel);
      modelUnconfirmedRef.current = false; setModelUnconfirmed(false);
      uncertainTurnRef.current = null; setUncertainTurn(null); setDraft("");
      dispatch({ type:"history", turns:page.items });
    } catch (error) {
      if (!controller.signal.aborted) setNotice(toPublicMessage(error));
    } finally {
      if (resetController.current === controller) { resettingRef.current = false; setResetting(false); resetController.current = null; }
      controller.abort();
    }
  }, [account,applyModel,defaultModel,mode,stop]);

  const chooseModel = useCallback(async (choice: ModelChoice): Promise<void> => {
    if (resettingRef.current || activeController.current || uncertainTurnRef.current || modelUnconfirmedRef.current) return;
    manualModel.current = true;
    if (!account || !summary) { applyModel(choice); return; }
    stop(false);
    resettingRef.current = true; setResetting(true); setNotice("");
    const controller = new AbortController(); resetController.current = controller;
    const runId = runSequence.current;
    try {
      const updated = await switchConversationModel(summary,choice,controller.signal);
      if (controller.signal.aborted || runSequence.current !== runId) return;
      setSummary(updated); applyModel(updated.model ?? choice);
      setDirectoryRevision(value => value + 1);
    } catch (error) {
      if (controller.signal.aborted || runSequence.current !== runId) return;
      try {
        const current = await readConversation(summary.id,controller.signal);
        if (controller.signal.aborted || runSequence.current !== runId) return;
        setSummary(current);
        if (current.model === choice && current.revision > summary.revision) {
          applyModel(choice); return;
        }
      } catch { /* A missing confirmation is kept locked, never replayed. */ }
      if (!controller.signal.aborted && runSequence.current === runId) {
        modelUnconfirmedRef.current = true; setModelUnconfirmed(true);
        setNotice("模型选择保存状态未确认，请从目录重新打开会话核对；不会自动重发。");
      }
    } finally {
      if (resetController.current === controller) { resetController.current = null; resettingRef.current = false; setResetting(false); }
      controller.abort();
    }
  }, [account,applyModel,summary,stop]);

  const loadOlder = useCallback(async (): Promise<void> => {
    if (!account || !nextBefore || resettingRef.current || isConversationActive(state.status)) return;
    resettingRef.current = true; setResetting(true);
    const controller = new AbortController(); resetController.current = controller;
    const runId = runSequence.current;
    try {
      const page = await readHistory(sessionId,controller.signal,{ before:nextBefore });
      if (controller.signal.aborted || runSequence.current !== runId) return;
      dispatch({ type:"history",turns:page.items,prepend:true }); setNextBefore(page.nextBefore);
    } catch (error) { if (!controller.signal.aborted) setNotice(toPublicMessage(error)); }
    finally { if (resetController.current === controller) { resettingRef.current = false; setResetting(false); resetController.current = null; } }
  }, [account,nextBefore,sessionId,state.status]);

  const mutateConversation = useCallback(async (
    item: ConversationSummary,
    action: "rename" | "clear" | "delete",
    title?: string,
  ): Promise<boolean> => {
    if (!account || resettingRef.current) return false;
    const isActive = summary?.id === item.id;
    if (action === "rename" && isActive && isConversationActive(state.status)) return false;
    if (action !== "rename" && !window.confirm(action === "clear" ? "清空该会话的所有问答？目录和标题将保留。" : "删除整个会话及历史？此操作不能撤销。")) return false;
    if (isActive) stop(true);
    resettingRef.current = true; setResetting(true); setNotice("");
    const controller = new AbortController(); resetController.current = controller;
    try {
      const updated = await updateConversation(item,controller.signal,action,title);
      if (controller.signal.aborted) return false;
      setDirectoryRevision((value) => value + 1);
      if (isActive) {
        setSummary(updated);
        if (action !== "rename") {
          stop(false); dispatch({type:"clear"}); setNextBefore(null);
          uncertainTurnRef.current = null; setUncertainTurn(null);
          if (action === "delete") { setSessionId(createSessionId()); window.history.replaceState({},"",modeHref(mode)); }
        }
      }
      return true;
    } catch (error) { if (!controller.signal.aborted) setNotice(toPublicMessage(error)); return false; }
    finally { if (resetController.current === controller) { resettingRef.current = false; setResetting(false); resetController.current = null; } }
  }, [account,summary,stop,mode,state.status]);

  const mutate = useCallback(async (action: "rename" | "clear" | "delete", title?: string): Promise<boolean> => {
    if (!summary) return false;
    return mutateConversation(summary, action, title);
  }, [mutateConversation,summary]);

  const reconcile = useCallback(async (): Promise<void> => {
    const turnId = uncertainTurnRef.current;
    if (!account || !turnId || resettingRef.current) return;
    const controller = new AbortController(); resetController.current = controller;
    const runId = runSequence.current;
    resettingRef.current = true; setResetting(true);
    try {
      const page = await readHistory(sessionId,controller.signal,{clientTurnId:turnId});
      if (controller.signal.aborted || runSequence.current !== runId || uncertainTurnRef.current !== turnId) return;
      if (page.items.length !== 1) { setNotice("尚无该回合的完成记录，结果仍未确认；不会自动重发。"); return; }
      const [latest, item] = await Promise.all([readHistory(sessionId,controller.signal),readConversation(sessionId,controller.signal)]);
      if (controller.signal.aborted || runSequence.current !== runId) return;
      dispatch({type:"history",turns:latest.items}); setNextBefore(latest.nextBefore); setSummary(item);
      uncertainTurnRef.current = null; setUncertainTurn(null);
      setNotice("");
    } catch (error) { if (!controller.signal.aborted) setNotice(toPublicMessage(error)); }
    finally { if (resetController.current === controller) { resettingRef.current = false; setResetting(false); resetController.current = null; } }
  }, [account,sessionId]);

  useEffect(() => {
    if (!account || state.status !== "completed") return undefined;
    const controller = new AbortController();
    resetController.current?.abort(); resetController.current = controller;
    const runId = runSequence.current;
    void readConversation(sessionId,controller.signal).then((item) => {
      if (!controller.signal.aborted && runSequence.current === runId) setSummary(item);
    }).catch(() => {
      if (!controller.signal.aborted) setNotice("回答已完成，但会话信息未刷新；修改前请从目录重新打开核对。");
    }).finally(() => { if (resetController.current === controller) resetController.current = null; });
    return () => controller.abort();
  }, [account,sessionId,state.status]);

  useEffect(
    () => () => {
      runSequence.current += 1;
      resettingRef.current = false;
      activeController.current?.abort();
      resetController.current?.abort();
    },
    [],
  );

  return { state, send, stop, reset, respondToInteraction, resetting, notice, draft, setDraft,
    sessionId, summary, directoryRevision, nextBefore, open, loadOlder, mutate, mutateConversation, reconcile, forget, unconfirmed:uncertainTurn !== null,
    model, chooseModel, modelUnconfirmed };
}
