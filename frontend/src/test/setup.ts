import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

afterEach(() => {
  if (typeof document !== "undefined") cleanup();
  if (typeof document !== "undefined") {
    delete document.documentElement.dataset.theme;
    delete document.documentElement.dataset.themePreference;
  }
  if (typeof localStorage !== "undefined") localStorage.clear();
  vi.unstubAllGlobals();
});
