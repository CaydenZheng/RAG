import { isRecord } from "./sse";
import type { CompletionMetadata } from "./types";

export function completionMetadata(value: Record<string, unknown>): CompletionMetadata {
  return {
    requestedModel: value.requested_model === "flash" || value.requested_model === "pro" ? value.requested_model : undefined,
    usedModel: typeof value.used_model === "string" ? value.used_model : undefined,
    attachments: Array.isArray(value.attachments) ? value.attachments.filter(isRecord).flatMap(item =>
      typeof item.id === "string" && typeof item.name === "string" && typeof item.checksum === "string"
        ? [{id:item.id, name:item.name, checksum:item.checksum, deleted:item.deleted === true}] : []) : undefined,
    skills: Array.isArray(value.skills) ? value.skills.filter(isRecord).flatMap(skill =>
      typeof skill.id === "string" && typeof skill.name === "string" && typeof skill.version === "string"
        ? [{ id: skill.id, name: skill.name, version: skill.version }] : []) : undefined,
  };
}
