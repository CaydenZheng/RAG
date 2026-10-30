import { AUTH_EXPIRED_EVENT, workspaceFetch } from "./authClient";
import { PublicClientError } from "./errors";
import { readSseEvents, isRecord } from "./sse";
import { requireEventStream } from "./http";

export type ModelChoice = "flash" | "pro";
export interface ModelOption { id: ModelChoice; label: string; available: boolean }
export interface ModelCatalog { items: ModelOption[]; defaultModel: ModelChoice }
export interface SkillItem { id: string; name: string; description: string; builtin: boolean; enabled: boolean; revision: number; version: string; content?: string }
export interface CreatorResult { filename: string; content: string; checksum: string }

const MESSAGES: Readonly<Record<string, string>> = Object.freeze({
  model_not_configured: "所选模型尚未配置，不能自动切换其他模型。",
  skill_invalid: "技能格式无效，请检查规定的 Markdown 元数据和正文。",
  skill_too_large: "技能文件超过 64 KiB，请精简后重试。",
  skill_reserved: "skill-creator 为内置保留名称。",
  skill_exists: "已有同名技能，请核对或删除旧版本。",
  skill_limit: "自定义技能已达 32 个，请先删除不需要的技能。",
  skill_conflict: "内容已变化，请刷新核对后再操作。",
  skill_builtin: "内置技能不能删除。",
  skills_context_too_large: "启用技能超过上下文预算，请关闭其他技能。",
  creator_disabled: "请先启用 skill-creator。",
  creator_limit: "近期转换操作已达上限，请稍后重试。",
  creator_operation_unknown: "转换操作已接收，请核对结果，不自动重发。",
  creator_output_invalid: "生成内容未通过技能校验，请调整资料后重试。",
  creator_timeout: "技能转换超时，请精简资料后重试。",
  creator_failed: "技能转换未完成，没有保存或启用技能。",
  document_unsupported: "仅支持 TXT、Markdown、文字型 PDF 和 DOCX。",
  document_invalid: "文件无法完整解析或没有可提取文字。",
  document_timeout: "解析超时，请精简资料后重试。",
  document_too_large: "文件大小、页数或文本超过限制，请拆分资料。",
  document_busy: "已有文件正在解析，请稍后重试。",
  context_too_large: "资料无法完整放入上下文，请拆分文件。",
  account_quota_exceeded: "今日模型使用额度已达上限。",
});

function errorMessage(code: unknown): string {
  return typeof code === "string" && Object.hasOwn(MESSAGES, code) ? MESSAGES[code] : "操作结果未确认，请刷新核对；不会自动重发。";
}

async function response(path: string, signal: AbortSignal, init: RequestInit = {}): Promise<Response> {
  const result = await workspaceFetch(path, { ...init, signal });
  if (!result.ok) {
    const payload: unknown = await result.json().catch(() => null);
    throw new PublicClientError("validation", errorMessage(isRecord(payload) && isRecord(payload.detail) ? payload.detail.code : undefined));
  }
  return result;
}

export function parseSkill(value: unknown): SkillItem {
  if (!isRecord(value) || typeof value.id !== "string" || typeof value.name !== "string" || typeof value.description !== "string" ||
    typeof value.builtin !== "boolean" || typeof value.enabled !== "boolean" || typeof value.revision !== "number" || typeof value.version !== "string") {
    throw new PublicClientError("protocol", "技能响应无效，请刷新核对。");
  }
  return { id: value.id, name: value.name, description: value.description, builtin: value.builtin, enabled: value.enabled,
    revision: value.revision, version: value.version, ...(typeof value.content === "string" ? { content: value.content } : {}) };
}

export async function readModels(signal: AbortSignal): Promise<ModelCatalog> {
  const payload: unknown = await (await response("/workspace/models", signal)).json();
  if (!isRecord(payload) || !Array.isArray(payload.items) || (payload.default_model !== "flash" && payload.default_model !== "pro")) throw new PublicClientError("protocol", "模型目录暂不可用。");
  const items = payload.items.map((item: unknown): ModelOption => {
    if (!isRecord(item) || (item.id !== "flash" && item.id !== "pro") || typeof item.label !== "string" || typeof item.available !== "boolean") {
      throw new PublicClientError("protocol", "模型目录暂不可用。");
    }
    return { id: item.id, label: item.label, available: item.available };
  });
  if (items.length !== 2 || new Set(items.map(item => item.id)).size !== 2) throw new PublicClientError("protocol", "模型目录暂不可用。");
  return { items, defaultModel: payload.default_model === "pro" ? "pro" : "flash" };
}

export async function readSkills(signal: AbortSignal): Promise<SkillItem[]> {
  const payload: unknown = await (await response("/skills", signal)).json();
  if (!isRecord(payload) || !Array.isArray(payload.items)) throw new PublicClientError("protocol", "技能目录暂不可用。");
  return payload.items.map(parseSkill);
}

export async function readSkill(id: string, signal: AbortSignal): Promise<SkillItem> {
  return parseSkill(await (await response(`/skills/${encodeURIComponent(id)}`, signal)).json());
}

export async function setSkill(item: SkillItem, enabled: boolean, signal: AbortSignal): Promise<SkillItem> {
  return parseSkill(await (await response(`/skills/${encodeURIComponent(item.id)}`, signal, { method: "PATCH",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled, expected_revision: item.revision }) })).json());
}

export async function deleteSkill(id: string, signal: AbortSignal): Promise<void> {
  await response(`/skills/${encodeURIComponent(id)}`, signal, { method: "DELETE" });
}

export async function uploadSkill(file: File, signal: AbortSignal): Promise<SkillItem> {
  const data = new FormData(); data.set("file", file);
  return parseSkill(await (await response("/skills", signal, { method: "POST", body: data })).json());
}

export async function createSkill(file: File, model: ModelChoice, operationId: string, signal: AbortSignal, onChunk: (chunk: string) => void): Promise<CreatorResult> {
  const data = new FormData(); data.set("file", file); data.set("model", model); data.set("client_operation_id", operationId);
  const result = await response("/skills/creator/stream", signal, { method: "POST", body: data });
  let previewBytes = 0;
  for await (const event of readSseEvents(await requireEventStream(result), signal)) {
    if (!isRecord(event)) continue;
    if (event.code === "auth_required" || isRecord(event.error) && event.error.code === "auth_required") {
      window.dispatchEvent(new Event(AUTH_EXPIRED_EVENT));
    }
    if (event.error !== undefined) throw new PublicClientError("terminal_error", errorMessage(isRecord(event.error) ? event.error.code : undefined));
    if (event.done === true) {
      if (typeof event.filename !== "string" || !/^[a-z0-9][a-z0-9-]{0,63}\.md$/.test(event.filename) ||
          typeof event.content !== "string" || typeof event.checksum !== "string" || !/^[a-f0-9]{64}$/.test(event.checksum)) {
        throw new PublicClientError("protocol", "未收到有效技能产物，请重新核对。");
      }
      if (new TextEncoder().encode(event.content).byteLength > 64 * 1024) throw new PublicClientError("protocol", "技能产物超过大小限制。");
      const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(event.content));
      const checksum = Array.from(new Uint8Array(digest), value => value.toString(16).padStart(2, "0")).join("");
      if (signal.aborted) throw new DOMException("Aborted", "AbortError");
      if (checksum !== event.checksum) throw new PublicClientError("protocol", "技能产物校验失败。");
      return { filename: event.filename, content: event.content, checksum };
    }
    if (typeof event.chunk === "string") {
      previewBytes += new TextEncoder().encode(event.chunk).byteLength;
      if (previewBytes > 64 * 1024) throw new PublicClientError("protocol", "技能预览超过大小限制。");
      onChunk(event.chunk);
    }
  }
  throw new PublicClientError("disconnected", "转换完成状态未确认，没有保存或启用技能。");
}
