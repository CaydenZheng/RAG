import { useEffect, useState } from "react";

import { isRecord } from "../api/sse";

export type ServiceState = "checking" | "ready" | "degraded" | "starting" | "unavailable";

const SERVICE_STATES = new Set<ServiceState>([
  "ready",
  "degraded",
  "starting",
  "unavailable",
]);

export function useServiceStatus(): ServiceState {
  const [serviceState, setServiceState] = useState<ServiceState>("checking");

  useEffect(() => {
    const controller = new AbortController();
    void (async () => {
      try {
        const response = await fetch("/ready", {
          headers: { Accept: "application/json" },
          cache: "no-store",
          signal: controller.signal,
        });
        const body: unknown = await response.json().catch(() => null);
        const reported = isRecord(body) && typeof body.status === "string" ? body.status : "";
        if (SERVICE_STATES.has(reported as ServiceState)) {
          setServiceState(reported as ServiceState);
        } else {
          setServiceState(response.ok ? "ready" : "unavailable");
        }
      } catch {
        if (!controller.signal.aborted) setServiceState("unavailable");
      }
    })();
    return () => controller.abort();
  }, []);

  return serviceState;
}
