import { fileURLToPath } from "node:url";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Only the Pages build substitutes the network clients. The local build is unchanged.
export default defineConfig({
  base: "/RAG/",
  envDir: false,
  publicDir: false,
  plugins: [react(), {
    name: "pages-security-policy",
    transformIndexHtml: {
      order: "post",
      handler: () => [{
        tag: "meta",
        attrs: {
          "http-equiv": "Content-Security-Policy",
          content: "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; font-src 'self'; connect-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'",
        },
        injectTo: "head-prepend",
      }, {
        tag: "meta",
        attrs: { name: "referrer", content: "no-referrer" },
        injectTo: "head-prepend",
      }],
    },
  }],
  resolve: {
    alias: [{
      find: /^\.\.\/api\/resourceClient$/,
      replacement: fileURLToPath(new URL("./src/demo/resources.ts", import.meta.url)),
    }, {
      find: /^\.\.\/api\/workspaceClient$/,
      replacement: fileURLToPath(new URL("./src/demo/workspace.ts", import.meta.url)),
    }, {
      find: /^\.\.\/api\/(ragClient|agentClient|interactionClient|session)$/,
      replacement: fileURLToPath(new URL("./src/demo/client.ts", import.meta.url)),
    }],
  },
  build: {
    outDir: "../pages-dist",
    emptyOutDir: true,
    sourcemap: false,
    modulePreload: false,
  },
});
