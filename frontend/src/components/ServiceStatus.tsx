import type { ServiceState } from "../app/useServiceStatus";

const LABELS: Readonly<Record<ServiceState, string>> = Object.freeze({
  checking: "检查服务",
  ready: "服务可用",
  degraded: "部分能力降级",
  starting: "服务准备中",
  unavailable: "服务暂不可用",
});

export function ServiceStatus({ state }: { state: ServiceState }) {
  return (
    <div className="service-status" data-state={state} role="status" aria-live="polite">
      <span className="status-dot" aria-hidden="true" />
      <span>{LABELS[state]}</span>
    </div>
  );
}
