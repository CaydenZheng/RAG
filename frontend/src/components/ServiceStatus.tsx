import type { ServiceState } from "../app/useServiceStatus";

const LABELS: Readonly<Record<ServiceState, string>> = Object.freeze({
  checking: "检查服务",
  ready: "服务可用",
  degraded: "部分能力降级",
  starting: "服务准备中",
  unavailable: "服务暂不可用",
});

export function ServiceStatus({ state, onRefresh }: {
  state: ServiceState;
  onRefresh: () => void;
}): React.JSX.Element {
  const canRefresh = state === "degraded" || state === "starting" || state === "unavailable";
  return (
    <div className="service-status" data-state={state}>
      <span className="service-status-label" role="status" aria-live="polite">
        <span className="status-dot" aria-hidden="true" />
        <span>{LABELS[state]}</span>
      </span>
      {canRefresh && (
        <button type="button" onClick={onRefresh}>重新检查服务</button>
      )}
    </div>
  );
}
