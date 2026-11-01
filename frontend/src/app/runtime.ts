import type { WorkspaceMode } from "../api/types";

export const IS_PAGES_DEMO: boolean = import.meta.env.MODE === "pages";

export function extraRoute(): "tools" | "settings" | null {
  const value=IS_PAGES_DEMO?new URLSearchParams(window.location.search).get("view"):window.location.pathname.slice(1);
  return value==="tools"||value==="settings"?value:null;
}
export function extraHref(view:"tools"|"settings"): string {return IS_PAGES_DEMO?`${import.meta.env.BASE_URL}?view=${view}`:`/${view}`;}

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

const APPLE: boolean = typeof navigator !== "undefined"
  && /Mac|iPhone|iPad|iPod/.test(navigator.platform || navigator.userAgent);

export const SHORTCUT_LABEL: string = APPLE ? "⌥⇧" : "Ctrl+Alt";

/** 平台安全的组合键：macOS 用 ⌥⇧，其余用 Ctrl/⌘+Alt（这两个组合在系统层未被占用）。 */
export function isShortcut(event: KeyboardEvent, key: string): boolean {
  if (event.key.toLowerCase() !== key) return false;
  if (event.isComposing || event.keyCode === 229 || event.repeat || event.getModifierState("AltGraph")) return false;
  return APPLE
    ? event.altKey && event.shiftKey && !event.metaKey && !event.ctrlKey
    : (event.ctrlKey || event.metaKey) && event.altKey && !event.shiftKey;
}
