import { useCallback, useEffect, useState } from "react";

export type ThemePreference = "system" | "light" | "dark";
export type ResolvedTheme = Exclude<ThemePreference, "system">;

interface ThemeState {
  preference: ThemePreference;
  resolved: ResolvedTheme;
  setPreference: (preference: ThemePreference) => void;
}

const THEME_STORAGE_KEY = "ragflow_theme";

function systemTheme(): ResolvedTheme {
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function storedPreference(): ThemePreference {
  try {
    const stored = window.localStorage.getItem(THEME_STORAGE_KEY);
    if (stored === "light" || stored === "dark" || stored === "system") return stored;
  } catch {
    // Theme persistence is optional; the workspace remains usable without storage access.
  }
  return "system";
}

export function useTheme(): ThemeState {
  const [preference, setPreferenceState] = useState<ThemePreference>(storedPreference);
  const [resolved, setResolved] = useState<ResolvedTheme>(() => (
    preference === "system" ? systemTheme() : preference
  ));

  useEffect(() => {
    const media = window.matchMedia?.("(prefers-color-scheme: dark)");
    const applyTheme = (): void => {
      const nextTheme = preference === "system"
        ? (media?.matches ? "dark" : "light")
        : preference;
      document.documentElement.dataset.theme = nextTheme;
      document.documentElement.dataset.themePreference = preference;
      setResolved(nextTheme);
    };

    applyTheme();
    if (preference !== "system" || !media) return undefined;
    media.addEventListener("change", applyTheme);
    return () => media.removeEventListener("change", applyTheme);
  }, [preference]);

  const setPreference = useCallback((nextPreference: ThemePreference): void => {
    setPreferenceState(nextPreference);
    try {
      window.localStorage.setItem(THEME_STORAGE_KEY, nextPreference);
    } catch {
      // The in-memory preference still applies when storage is unavailable.
    }
  }, []);

  return { preference, resolved, setPreference };
}
