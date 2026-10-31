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
  detail: string;
  refresh: () => void;
}

export function useServiceStatus(): ServiceStatusController {
  const [serviceState, setServiceState] = useState<ServiceState>(IS_PAGES_DEMO ? "demo" : "checking");
  const [requestSequence, setRequestSequence] = useState(0);
  const [detail,setDetail]=useState("");

  const refresh = useCallback((): void => {
    setServiceState("checking");
    setDetail("");
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
          if(reported!=="ready"){
            const components=isRecord(body)&&isRecord(body.components)?body.components:{};
            const failed=(key:string):boolean=>isRecord(components[key])&&components[key].status!=="ready";
            setDetail(failed("embedding")?"本地模型尚未就绪，请等待初始化或由管理员检查模型文件与资源。":failed("index")?"知识库索引未就绪，请由管理员准备有效版本化索引。":failed("reranker")?"精排暂不可用，请稍后重新检查。":"部分服务尚未就绪，请稍后重新检查；外部工具可能不可用。");
          }else setDetail("");
        } else {
          setServiceState(response.ok ? "ready" : "unavailable");
          setDetail(response.ok?"":"无法确认服务状态，请检查本地后端是否启动。");
        }
      } catch {
        if (!controller.signal.aborted){setServiceState("unavailable");setDetail("无法连接本地后端，请确认服务已启动并重新检查。");}
      }
    })();
    return () => controller.abort();
  }, [requestSequence]);

  return { state: serviceState, detail, refresh };
}
