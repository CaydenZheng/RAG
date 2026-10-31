import react from "@vitejs/plugin-react";
import { stripVTControlCharacters } from "node:util";

import { createLogger, type Logger, type Plugin } from "vite";
import { defineConfig } from "vitest/config";

const backendTarget = "http://127.0.0.1:8000";
const proxyRoutes = [
  "/auth", "/conversations", "/workspace", "/skills", "/knowledge/documents", "/openapi.json",
  "/agent/approvals", "/agent/chat", "/agent/elicitation", "/agent/memory",
  "/agent/oauth", "/agent/reset", "/agent/tools", "/docs", "/query", "/ready", "/session",
];

function workspaceLogger(): Logger {
  const logger = createLogger();
  const logError = logger.error;
  const allowedCodes = new Set(["ECONNREFUSED", "ECONNRESET", "ETIMEDOUT", "EPIPE", "ENOTFOUND", "EAI_AGAIN"]);
  logger.error = (message, options): void => {
    const plain = stripVTControlCharacters(message);
    if (!/^(?:http|ws) proxy(?: socket| bypass)? error:/.test(plain)) {
      logError(message, options);
      return;
    }
    const pathname = plain.match(/^http proxy error: ([^\r\n]*)/)?.[1]?.split("?", 1)[0] ?? "";
    const route = proxyRoutes.find((path) => pathname === path || pathname.startsWith(`${path}/`)) ?? "/proxy";
    const code = options?.error && "code" in options.error ? options.error.code : undefined;
    // Vite's default proxy listener also logs: project at the output boundary,
    // never forward its URL, stack or Error object to the underlying logger.
    logError(`dev proxy failed: ${route} ${typeof code === "string" && allowedCodes.has(code) ? code : "PROXY_ERROR"}`, { timestamp: true });
  };
  return logger;
}

function localWorkspaceRoutes(): Plugin {
  return {
    name: "local-workspace-routes",
    apply: "serve",
    configureServer(server) {
      server.middlewares.use((request, _response, next) => {
        const [pathname, search = ""] = (request.url ?? "").split("?", 2);
        if (pathname === "/" || pathname === "/agent" || pathname === "/settings/skills" || pathname === "/knowledge" || pathname === "/tools" || pathname === "/settings") {
          request.url = `/static/app/${search ? `?${search}` : ""}`;
        }
        next();
      });
    },
  };
}

export default defineConfig({
  customLogger: workspaceLogger(),
  base: "/static/app/",
  plugins: [localWorkspaceRoutes(), react()],
  build: {
    outDir: "../src/web/dist",
    emptyOutDir: true,
    sourcemap: false,
  },
  server: {
    proxy: Object.fromEntries(proxyRoutes.map((path) => [path, backendTarget])),
  },
  test: {
    environment: "jsdom",
    setupFiles: "./src/test/setup.ts",
    include: ["src/test/**/*.test.{ts,tsx}", "vite-routing.test.ts"],
    clearMocks: true,
  },
});
