// @vitest-environment node

import { createServer as createHttpServer } from "node:http";
import type { AddressInfo } from "node:net";

import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { createServer as createViteServer, type ViteDevServer } from "vite";

import viteConfig from "./vite.config";

describe("Vite development routing", () => {
  let backend: ReturnType<typeof createHttpServer>;
  let vite: ViteDevServer;
  let origin = "";
  const backendPaths: string[] = [];

  beforeAll(async () => {
    backend = createHttpServer((request, response) => {
      backendPaths.push(request.url ?? "");
      response.writeHead(200, { "Content-Type": "application/json" });
      response.end(JSON.stringify({ proxied: true }));
    });
    await new Promise<void>((resolve) => backend.listen(0, "127.0.0.1", resolve));
    const backendAddress = backend.address() as AddressInfo;
    const backendTarget = `http://127.0.0.1:${backendAddress.port}`;
    const configuredProxy = viteConfig.server?.proxy ?? {};
    const proxy = Object.fromEntries(
      Object.keys(configuredProxy).map((path) => [path, backendTarget]),
    );

    vite = await createViteServer({
      ...viteConfig,
      configFile: false,
      server: {
        ...viteConfig.server,
        host: "127.0.0.1",
        port: 0,
        strictPort: false,
        proxy,
      },
    });
    await vite.listen();
    const viteAddress = vite.httpServer?.address() as AddressInfo;
    origin = `http://127.0.0.1:${viteAddress.port}`;
  });

  afterAll(async () => {
    await vite?.close();
    await new Promise<void>((resolve, reject) => {
      backend?.close((error) => error ? reject(error) : resolve());
    });
  });

  it("serves the Agent page itself and proxies only Agent API requests", async () => {
    const page = await fetch(`${origin}/agent`);
    const pageHtml = await page.text();

    expect(page.status).toBe(200);
    expect(pageHtml).toContain('<div id="root"></div>');
    expect(pageHtml).toContain("/@vite/client");
    expect(new URL(page.url).pathname).toBe("/agent");
    expect(backendPaths).not.toContain("/agent");

    const api = await fetch(`${origin}/agent/chat/stream`, { method: "POST" });
    expect(await api.json()).toEqual({ proxied: true });
    expect(backendPaths).toContain("/agent/chat/stream");
  });
});
