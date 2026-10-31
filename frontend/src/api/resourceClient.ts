import { response } from "./workspaceClient";
import { isRecord } from "./sse";
import { PublicClientError } from "./errors";

export interface AttachmentItem {
  id: string; client_upload_id: string; name: string; suffix: string; bytes: number; checksum: string;
  state: "parsing" | "ready" | "failed"; error_code: string | null; page_count: number;
}
export interface KnowledgeDocument { id: string; name: string; checksum: string; index_version: string; indexed_at: string; updated_at: string | null }
export interface KnowledgePageData { items: KnowledgeDocument[]; next_cursor: string | null; index_version: string }
export interface KnowledgeExcerpt { id: string; name: string; text: string; excerpt_only: true }

function attachment(value: unknown): AttachmentItem {
  if (!isRecord(value) || typeof value.id !== "string" || typeof value.client_upload_id !== "string" || typeof value.name !== "string" ||
    typeof value.suffix !== "string" || typeof value.bytes !== "number" || typeof value.checksum !== "string" || typeof value.page_count !== "number" ||
    !["parsing", "ready", "failed"].includes(String(value.state)) || (value.error_code !== null && typeof value.error_code !== "string")) {
    throw new PublicClientError("protocol", "附件状态无效，请刷新核对。");
  }
  return {id:value.id, client_upload_id:value.client_upload_id, name:value.name, suffix:value.suffix, bytes:value.bytes,
    checksum:value.checksum, page_count:value.page_count, error_code:value.error_code,
    state:value.state === "ready" ? "ready" : value.state === "parsing" ? "parsing" : "failed"};
}

function attachmentPath(conversationId: string): string { return `/conversations/${encodeURIComponent(conversationId)}/attachments`; }

export async function readAttachments(id: string, signal: AbortSignal): Promise<AttachmentItem[]> {
  const payload: unknown = await (await response(attachmentPath(id), signal)).json();
  if (!isRecord(payload) || !Array.isArray(payload.items)) throw new PublicClientError("protocol", "附件目录无效。");
  return payload.items.map(attachment);
}

export async function uploadAttachment(id: string, uploadId: string, file: File, signal: AbortSignal): Promise<AttachmentItem> {
  const body = new FormData(); body.set("file", file); body.set("client_upload_id", uploadId);
  return attachment(await (await response(attachmentPath(id), signal, {method:"POST", body})).json());
}

export async function deleteAttachment(id: string, aid: string, signal: AbortSignal): Promise<void> {
  await response(`${attachmentPath(id)}/${encodeURIComponent(aid)}`, signal, {method:"DELETE"});
}

export async function downloadAttachment(id: string, aid: string, signal: AbortSignal): Promise<Blob> {
  return (await response(`${attachmentPath(id)}/${encodeURIComponent(aid)}/content`, signal)).blob();
}

export async function readKnowledge(signal: AbortSignal, query = "", cursor?: string): Promise<KnowledgePageData> {
  const payload: unknown = await (await response(query ? "/knowledge/documents/search" : `/knowledge/documents${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ""}`, signal,
    query ? {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({title_query:query, ...(cursor ? {cursor} : {})})} : {})).json();
  if (!isRecord(payload) || !Array.isArray(payload.items) || typeof payload.index_version !== "string" ||
    (payload.next_cursor !== null && typeof payload.next_cursor !== "string")) throw new PublicClientError("protocol", "知识库目录无效。");
  const items = payload.items.map((value: unknown): KnowledgeDocument => {
    if (!isRecord(value) || typeof value.id !== "string" || typeof value.name !== "string" || typeof value.checksum !== "string" ||
      typeof value.index_version !== "string" || typeof value.indexed_at !== "string" || (value.updated_at !== null && typeof value.updated_at !== "string")) throw new PublicClientError("protocol", "知识库文档信息无效。");
    return {id:value.id,name:value.name,checksum:value.checksum,index_version:value.index_version,indexed_at:value.indexed_at,updated_at:value.updated_at};
  });
  return {items, index_version:payload.index_version, next_cursor:payload.next_cursor};
}

export async function readKnowledgeExcerpt(item: KnowledgeDocument, signal: AbortSignal): Promise<KnowledgeExcerpt> {
  const value: unknown = await (await response(`/knowledge/documents/${encodeURIComponent(item.id)}?version=${encodeURIComponent(item.index_version)}`, signal)).json();
  if (!isRecord(value) || value.id !== item.id || typeof value.name !== "string" || typeof value.text !== "string" || value.excerpt_only !== true) throw new PublicClientError("protocol", "来源内容无效。");
  return {id:item.id,name:value.name,text:value.text,excerpt_only:true};
}
