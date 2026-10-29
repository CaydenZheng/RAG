import { isRecord } from "./sse";
import type { CompletionMetadata } from "./types";

export function completionMetadata(value: Record<string, unknown>): CompletionMetadata {
  return {
    requestedModel: value.requested_model === "flash" || value.requested_model === "pro" ? value.requested_model : undefined,
    usedModel: typeof value.used_model === "string" ? value.used_model : undefined,
    skills: Array.isArray(value.skills) ? value.skills.filter(isRecord).flatMap(skill =>
      typeof skill.id === "string" && typeof skill.name === "string" && typeof skill.version === "string"
        ? [{ id: skill.id, name: skill.name, version: skill.version }] : []) : undefined,
  };
}
