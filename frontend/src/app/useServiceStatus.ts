import { useCallback, useEffect, useState } from "react";

import { isRecord } from "../api/sse";
import { workspaceFetch as fetch } from "../api/authClient";
import { IS_PAGES_DEMO } from "./runtime";

export type ServiceState = "checking" | "ready" | "degraded" | "starting" | "unavailable" | "demo";

const SERVICE_STATES = new Set<ServiceState>([
  "ready",
  "degraded",
  "starting",
  "unavailable",
]);

interface ServiceStatusController {
  state: ServiceState;
  refresh: () => void;
}

export function useServiceStatus(): ServiceStatusController {
  const [serviceState, setServiceState] = useState<ServiceState>(IS_PAGES_DEMO ? "demo" : "checking");
  const [requestSequence, setRequestSequence] = useState(0);

  const refresh = useCallback((): void => {
    setServiceState("checking");
    setRequestSequence((current) => current + 1);
  }, []);

  useEffect(() => {
    if (IS_PAGES_DEMO) return undefined;
    const controller = new AbortController();
    void (async () => {
      try {
        const response = await fetch("/ready", {
          headers: { Accept: "application/json" },
          cache: "no-store",
          signal: controller.signal,
        });
        const body: unknown = await response.json().catch(() => null);
        if (controller.signal.aborted) return;
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
  }, [requestSequence]);

  return { state: serviceState, refresh };
}
