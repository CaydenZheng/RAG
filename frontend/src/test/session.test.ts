import { describe, expect, it, vi } from "vitest";

import { resetAndRotateSession, SESSION_KEYS } from "../api/session";

describe("session reset", () => {
  it.each([200, 404])("rotates only after HTTP %s", async (status) => {
    localStorage.setItem(SESSION_KEYS.rag, "old-session");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status })));

    const next = await resetAndRotateSession("rag", "old-session");

    expect(next).not.toBe("old-session");
    expect(localStorage.getItem(SESSION_KEYS.rag)).toBe(next);
    expect(fetch).toHaveBeenCalledWith(
      "/session/reset?session_id=old-session",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("keeps the old ID when the network request fails", async () => {
    localStorage.setItem(SESSION_KEYS.agent, "old-agent-session");
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("network detail")));

    await expect(resetAndRotateSession("agent", "old-agent-session")).rejects.toMatchObject({
      code: "reset",
    });
    expect(localStorage.getItem(SESSION_KEYS.agent)).toBe("old-agent-session");
  });

  it("keeps the old ID for a non-404 HTTP failure", async () => {
    localStorage.setItem(SESSION_KEYS.agent, "old-agent-session");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status: 500 })));

    await expect(resetAndRotateSession("agent", "old-agent-session")).rejects.toMatchObject({
      code: "reset",
    });
    expect(localStorage.getItem(SESSION_KEYS.agent)).toBe("old-agent-session");
  });
});
