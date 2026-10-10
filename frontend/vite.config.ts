import react from "@vitejs/plugin-react";
import type { Plugin } from "vite";
import { defineConfig } from "vitest/config";

const backendTarget = "http://127.0.0.1:8000";

function localWorkspaceRoutes(): Plugin {
  return {
    name: "local-workspace-routes",
    apply: "serve",
    configureServer(server) {
      server.middlewares.use((request, _response, next) => {
        const [pathname, search = ""] = (request.url ?? "").split("?", 2);
        if (pathname === "/" || pathname === "/agent") {
          request.url = `/static/app/${search ? `?${search}` : ""}`;
        }
        next();
      });
    },
  };
}

export default defineConfig({
  base: "/static/app/",
  plugins: [localWorkspaceRoutes(), react()],
  build: {
    outDir: "../src/web/dist",
    emptyOutDir: true,
    sourcemap: false,
  },
  server: {
    proxy: {
      "/agent/approvals": backendTarget,
      "/agent/chat": backendTarget,
      "/agent/elicitation": backendTarget,
      "/agent/memory": backendTarget,
      "/agent/oauth": backendTarget,
      "/agent/reset": backendTarget,
      "/agent/tools": backendTarget,
      "/docs": backendTarget,
      "/query": backendTarget,
      "/ready": backendTarget,
      "/session": backendTarget,
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: "./src/test/setup.ts",
    clearMocks: true,
  },
});
