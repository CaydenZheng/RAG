import type { AttachmentItem, KnowledgeDocument, KnowledgeExcerpt, KnowledgePageData } from "../api/resourceClient";

const files = new Map<string, AttachmentItem[]>();
const uploads = new Map<string, AttachmentItem>();
function check(signal: AbortSignal): void { if (signal.aborted) throw new DOMException("Aborted", "AbortError"); }

export async function readAttachments(id: string, signal: AbortSignal): Promise<AttachmentItem[]> {
  check(signal); return [...(files.get(id) ?? [])];
}
export async function uploadAttachment(id: string, uploadId: string, _file: File, signal: AbortSignal): Promise<AttachmentItem> {
  check(signal);
  const key = `${id}:${uploadId}`;
  const previous = uploads.get(key); if (previous) return previous;
  const existing = files.get(id) ?? [];
  if (existing.length >= 5) throw new Error("请先删除模拟附件。");
  // Never read user bytes or include their filename in this public demo.
  const item: AttachmentItem = {id:crypto.randomUUID(),client_upload_id:uploadId,name:"模拟短文档.txt",suffix:"txt",bytes:80,
    checksum:"a".repeat(64),state:"ready",page_count:0,error_code:null};
  files.set(id, [...existing, item]); uploads.set(key, item); return item;
}
export async function deleteAttachment(id: string, aid: string, signal: AbortSignal): Promise<void> {
  check(signal); files.set(id, (files.get(id) ?? []).filter(item => item.id !== aid));
}
export async function downloadAttachment(_id: string, _aid: string, signal: AbortSignal): Promise<Blob> {
  check(signal); return new Blob(["预设模拟附件，不包含上传文件内容。"], {type:"text/plain"});
}

const document: KnowledgeDocument = {id:"demo-handbook",name:"示例使用手册.md",checksum:"b".repeat(64),index_version:"demo-index-v1",indexed_at:"2026-10-30T00:00:00Z",updated_at:null};
export async function readKnowledge(signal: AbortSignal, query = ""): Promise<KnowledgePageData> {
  check(signal); return {items:!query || document.name.includes(query) ? [document] : [], next_cursor:null,index_version:"demo-index-v1"};
}
export async function readKnowledgeExcerpt(_item: KnowledgeDocument, signal: AbortSignal): Promise<KnowledgeExcerpt> {
  check(signal); return {id:document.id,name:document.name,text:"预设示例：知识库为共享只读目录，聊天附件仅属于当前会话。",excerpt_only:true};
}
