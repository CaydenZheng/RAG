import { workspaceFetch } from "./authClient";
import { PublicClientError } from "./errors";
import { isRecord } from "./sse";
import type { WorkspaceMode } from "./types";
import type { ConversationTurn } from "../app/conversationState";
import type { ModelChoice } from "./workspaceClient";
import { completionMetadata } from "./completionMetadata";

export interface ConversationSummary {
  id: string;
  mode: WorkspaceMode;
  title: string;
  revision: number;
  epoch: number;
  updated_at: string;
  model?: ModelChoice;
  lastModel?: string;
}

export interface HistoryPage { items: ConversationTurn[]; nextBefore: string | null }
export interface DirectoryPage { items: ConversationSummary[]; nextCursor: string | null }

async function request(path: string, signal: AbortSignal, init: RequestInit = {}): Promise<unknown> {
  const response = await workspaceFetch(path, { ...init, signal,
    headers: { "Content-Type": "application/json", Accept: "application/json", ...init.headers } });
  if (!response.ok) throw new PublicClientError("history", response.status === 409
    ? "会话已变化、正在生成或达到容量上限，请刷新后重试。" : response.status === 404
      ? "会话不存在或不可访问，请刷新目录。" : "历史操作未确认，请检查连接并刷新核对；不要自动重发问题。");
  return response.status === 204 ? null : response.json();
}

function summary(value: unknown): ConversationSummary {
  if (!isRecord(value) || typeof value.id !== "string" || !["rag", "agent"].includes(String(value.mode)) ||
    typeof value.title !== "string" || typeof value.revision !== "number" || typeof value.epoch !== "number" || typeof value.updated_at !== "string") {
    throw new PublicClientError("protocol", "会话信息无效，请刷新核对。");
  }
  return { id: value.id, mode: value.mode === "agent" ? "agent" : "rag", title: value.title,
    revision: value.revision, epoch: value.epoch, updated_at: value.updated_at,
    model: value.model === "flash" || value.model === "pro" ? value.model : undefined,
    lastModel: typeof value.last_model === "string" ? value.last_model : undefined };
}

export async function createConversation(id: string, mode: WorkspaceMode, signal: AbortSignal, model?: ModelChoice): Promise<ConversationSummary> {
  return summary(await request("/conversations", signal, { method: "POST", body: JSON.stringify({ id, mode, ...(model ? { model } : {}) }) }));
}

export async function switchConversationModel(item: ConversationSummary, model: ModelChoice, signal: AbortSignal): Promise<ConversationSummary> {
  return summary(await request(`/conversations/${encodeURIComponent(item.id)}`, signal, {
    method: "PATCH", body: JSON.stringify({ model, expected_revision: item.revision }),
  }));
}

export async function readConversation(id: string, signal: AbortSignal): Promise<ConversationSummary> {
  return summary(await request(`/conversations/${encodeURIComponent(id)}`, signal));
}

export async function listConversations(mode: WorkspaceMode, signal: AbortSignal, cursor?: string): Promise<DirectoryPage> {
  const search = new URLSearchParams({ mode });
  if (cursor) search.set("cursor", cursor);
  const payload = await request(`/conversations?${search}`, signal);
  if (!isRecord(payload) || !Array.isArray(payload.items)) throw new PublicClientError("protocol", "目录信息无效。");
  return { items: payload.items.map(summary), nextCursor: typeof payload.next_cursor === "string" ? payload.next_cursor : null };
}

export async function readHistory(id: string, signal: AbortSignal, options: { before?: string; clientTurnId?: string } = {}): Promise<HistoryPage> {
  const search = new URLSearchParams();
  if (options.before) search.set("before", options.before);
  if (options.clientTurnId) search.set("client_turn_id", options.clientTurnId);
  const payload = await request(`/conversations/${encodeURIComponent(id)}/turns${search.size ? `?${search}` : ""}`, signal);
  if (!isRecord(payload) || !Array.isArray(payload.items)) throw new PublicClientError("protocol", "历史信息无效。");
  const items: ConversationTurn[] = payload.items.map((item: unknown) => {
    if (!isRecord(item) || typeof item.seq !== "number" || typeof item.question !== "string" || typeof item.answer !== "string" ||
      typeof item.client_turn_id !== "string" || (options.clientTurnId && item.client_turn_id !== options.clientTurnId)) {
      throw new PublicClientError("protocol", "历史回合信息无效。");
    }
    return { ...completionMetadata(item), id: item.seq, clientTurnId: item.client_turn_id, status: "completed", question: item.question,
      answer: item.answer, error: "", sources: Array.isArray(item.sources) ? item.sources.filter(isRecord).map((source, index) => ({
        ref: String(source.ref ?? index + 1), source: typeof source.label === "string" ? source.label : "回答时的来源",
        text: typeof source.excerpt === "string" ? source.excerpt : "", score: null,
      })) : [], warnings: [...(Array.isArray(item.warnings) ? item.warnings.filter((warning): warning is string => typeof warning === "string") : []), "历史仅回看已完成问答和回答时的来源，不重放工具过程。"],
      steps: [], latencyMs: null, indexVersion: typeof item.index_version === "string" ? item.index_version : "—", iterations: null, interactions: [] };
  });
  return { items, nextBefore: typeof payload.next_before === "string" ? payload.next_before : null };
}

export async function updateConversation(item: ConversationSummary, signal: AbortSignal, action: "rename" | "clear" | "delete", title?: string): Promise<ConversationSummary | null> {
  const path = `/conversations/${encodeURIComponent(item.id)}`;
  if (action === "delete") { await request(path, signal, { method: "DELETE", headers: { "If-Match": String(item.revision) } }); return null; }
  return summary(await request(action === "clear" ? `${path}/clear` : path, signal,
    { method: action === "clear" ? "POST" : "PATCH", body: JSON.stringify({ expected_revision: item.revision, ...(title !== undefined ? { title } : {}) }) }));
}
