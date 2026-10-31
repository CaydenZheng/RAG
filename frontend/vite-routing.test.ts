// @vitest-environment node

import { createServer as createHttpServer } from "node:http";
import type { AddressInfo } from "node:net";

import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";
import { createServer as createViteServer, type ViteDevServer } from "vite";

import viteConfig from "./vite.config";

describe("Vite development routing", () => {
  let backend: ReturnType<typeof createHttpServer>;
  let vite: ViteDevServer;
  let origin = "";
  const backendPaths: string[] = [];
  let resetConnection = false;

  beforeAll(async () => {
    backend = createHttpServer((request, response) => {
      backendPaths.push(request.url ?? "");
      if (resetConnection) {
        request.socket.destroy();
        return;
      }
      response.writeHead(200, { "Content-Type": "application/json" });
      response.end(JSON.stringify({ proxied: true }));
    });
    await new Promise<void>((resolve) => backend.listen(0, "127.0.0.1", resolve));
    const backendAddress = backend.address() as AddressInfo;
    const backendTarget = `http://127.0.0.1:${backendAddress.port}`;
    const configuredProxy = viteConfig.server?.proxy ?? {};
    const proxy = Object.fromEntries(
      Object.entries(configuredProxy).map(([path, options]) => [
        path, { ...(typeof options === "string" ? {} : options), target: backendTarget },
      ]),
    );

    vite = await createViteServer({
      ...viteConfig,
      configFile: false,
      // This fixture exercises HTML routes and real proxy failures;
      // module execution is covered by the browser suites.
      optimizeDeps: { noDiscovery: true, include: [] },
      server: {
        ...viteConfig.server,
        host: "127.0.0.1",
        port: 0,
        strictPort: false,
        preTransformRequests: false,
        proxy,
      },
    });
    await vite.listen();
    const viteAddress = vite.httpServer?.address() as AddressInfo;
    origin = `http://127.0.0.1:${viteAddress.port}`;
  });

  afterAll(async () => {
    await vite?.close();
    if (backend?.listening) {
      await new Promise<void>((resolve, reject) => {
        backend.close((error) => error ? reject(error) : resolve());
      });
    }
  });

  it("serves the Agent page itself and proxies only Agent API requests", async () => {
    const page = await fetch(`${origin}/agent`);
    const pageHtml = await page.text();

    expect(page.status).toBe(200);
    expect(pageHtml).toContain('<div id="root"></div>');
    expect(pageHtml).toContain("/@vite/client");
    expect(new URL(page.url).pathname).toBe("/agent");
    expect(backendPaths).not.toContain("/agent");
    for(const path of ["/tools","/settings"]){const page=await fetch(`${origin}${path}`);expect(page.status).toBe(200);expect(await page.text()).toContain("/@vite/client");expect(backendPaths).not.toContain(path);}

    const api = await fetch(`${origin}/agent/chat/stream`, { method: "POST" });
    expect(await api.json()).toEqual({ proxied: true });
    expect(backendPaths).toContain("/agent/chat/stream");
    const skillsPage = await fetch(`${origin}/settings/skills`);
    expect(await skillsPage.text()).toContain("/@vite/client");
    expect(backendPaths).not.toContain("/settings/skills");
    expect(await (await fetch(`${origin}/skills`)).json()).toEqual({proxied:true});
  });

  it("forwards the complete query without changing values", async () => {
    const path = "/query/stream?query=public%20question%26more&conversation_id=PUBLIC_CONVERSATION&client_turn_id=PUBLIC_TURN";
    const response = await fetch(`${origin}${path}`);
    expect(await response.json()).toEqual({ proxied: true });
    expect(backendPaths.at(-1)).toBe(path);
  });

  it.each(["reset", "unreachable"] as const)("redacts real Vite %s errors for every proxy route", async (fault) => {
    const lines: string[] = [];
    const output = vi.spyOn(console, "error").mockImplementation((...args: unknown[]) => {
      lines.push(args.map(String).join(" "));
    });
    try {
      resetConnection = fault === "reset";
      if (fault === "unreachable") {
        await new Promise<void>((resolve, reject) => backend.close((error) => error ? reject(error) : resolve()));
      }
      for (const route of Object.keys(viteConfig.server?.proxy ?? {})) {
        const response = await fetch(`${origin}${route}/PUBLIC_PATH?query=PUBLIC%20QUESTION&conversation_id=PUBLIC_CONVERSATION&client_turn_id=PUBLIC_TURN&token=PUBLIC_TOKEN`);
        expect(response.status).toBe(500);
        await response.text();
      }
      const logs = lines.join("\n");
      expect(logs).not.toMatch(/PUBLIC|QUESTION|conversation_id|client_turn_id|token=|Error:|\bat /);
      expect(logs).toContain(fault === "reset" ? "ECONNRESET" : "ECONNREFUSED");
      expect(logs).toContain("/query");
      expect(logs).toContain("/agent/oauth");
    } finally {
      output.mockRestore();
      resetConnection = false;
    }
  });

  it("preserves unrelated Vite diagnostics", () => {
    const output = vi.spyOn(console, "error").mockImplementation(() => undefined);
    try {
      vite.config.logger.error("PUBLIC_BUILD_ERROR");
      expect(output).toHaveBeenCalledWith("PUBLIC_BUILD_ERROR");
    } finally {
      output.mockRestore();
    }
  });
});
