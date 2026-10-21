import type { WorkspaceMode } from "../api/types";

export const IS_PAGES_DEMO: boolean = import.meta.env.MODE === "pages";

export function currentMode(): WorkspaceMode {
  return (IS_PAGES_DEMO
    ? new URLSearchParams(window.location.search).get("mode") === "agent"
    : window.location.pathname === "/agent") ? "agent" : "rag";
}

export function modeHref(mode: WorkspaceMode): string {
  if (IS_PAGES_DEMO) return `${import.meta.env.BASE_URL}${mode === "agent" ? "?mode=agent" : ""}`;
  return mode === "agent" ? "/agent" : "/";
}
