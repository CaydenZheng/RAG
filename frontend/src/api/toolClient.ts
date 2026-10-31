import { response } from "./workspaceClient";
import { isRecord } from "./sse";
import { PublicClientError } from "./errors";

export interface CatalogTool { name: string; description: string; source: "native" | "mcp"; provider: string | null; category: string; available: boolean }
export interface CatalogServer { id: string; status: string; transport: string; toolCount: number }
export interface ToolCatalog { tools: CatalogTool[]; servers: CatalogServer[] }

export async function readToolCatalog(signal: AbortSignal): Promise<ToolCatalog> {
  const value: unknown = await (await response("/agent/tools", signal)).json();
  if (!isRecord(value) || !Array.isArray(value.tools) || !Array.isArray(value.mcp_servers)) throw new PublicClientError("protocol", "工具目录无效，请重新读取。");
  const tools = value.tools.map((item: unknown): CatalogTool => {
    if (!isRecord(item) || typeof item.name !== "string" || (item.source !== "native" && item.source !== "mcp") ||
      (item.provider !== null && typeof item.provider !== "string") || typeof item.category !== "string" || typeof item.available !== "boolean") throw new PublicClientError("protocol", "工具目录无效，请重新读取。");
    return {name:item.name, description:typeof item.description === "string" ? item.description.slice(0,500) : "用途暂未提供。",
      source:item.source, provider:item.provider, category:item.category, available:item.available};
  });
  const servers = value.mcp_servers.map((item: unknown): CatalogServer => {
    if (!isRecord(item) || typeof item.id !== "string" || typeof item.status !== "string" || typeof item.transport !== "string" || typeof item.tool_count !== "number") throw new PublicClientError("protocol", "Provider 状态无效。");
    // Free provider messages, addresses and OAuth state are not projected.
    return {id:item.id, status:item.status, transport:item.transport, toolCount:item.tool_count};
  });
  return {tools, servers};
}
