import type { WorkspaceMode } from "../api/types";

export const IS_PAGES_DEMO: boolean = import.meta.env.MODE === "pages";

export function isKnowledgeRoute(): boolean {
  return IS_PAGES_DEMO ? new URLSearchParams(window.location.search).get("view") === "knowledge" : window.location.pathname === "/knowledge";
}

export function knowledgeHref(): string {
  return IS_PAGES_DEMO ? `${import.meta.env.BASE_URL}?view=knowledge` : "/knowledge";
}

export function isSkillsRoute(): boolean {
  return IS_PAGES_DEMO ? new URLSearchParams(window.location.search).get("view") === "skills" : window.location.pathname === "/settings/skills";
}

export function skillsHref(): string {
  return IS_PAGES_DEMO ? `${import.meta.env.BASE_URL}?view=skills` : "/settings/skills";
}

export function currentMode(): WorkspaceMode {
  return (IS_PAGES_DEMO
    ? new URLSearchParams(window.location.search).get("mode") === "agent"
    : window.location.pathname === "/agent") ? "agent" : "rag";
}

export function modeHref(mode: WorkspaceMode): string {
  if (IS_PAGES_DEMO) return `${import.meta.env.BASE_URL}${mode === "agent" ? "?mode=agent" : ""}`;
  return mode === "agent" ? "/agent" : "/";
}
