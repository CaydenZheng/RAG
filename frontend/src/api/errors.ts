export type PublicErrorCode =
  | "aborted"
  | "disconnected"
  | "http"
  | "protocol"
  | "reset"
  | "service_unavailable"
  | "terminal_error"
  | "validation";

export class PublicClientError extends Error {
  readonly code: PublicErrorCode;

  constructor(code: PublicErrorCode, message: string) {
    super(message);
    this.name = "PublicClientError";
    this.code = code;
  }
}

export const PUBLIC_MESSAGES = Object.freeze({
  agentFailed: "Agent 处理失败，请稍后重试。",
  disconnected: "连接意外中断，本轮内容未保存为成功回答。",
  invalidRequest: "输入内容不符合要求，请检查后重试。",
  protocol: "服务返回了无法识别的数据，本轮内容未保存。",
  ragFailed: "回答生成失败，请稍后重试。",
  resetFailed: "无法清除当前会话，请稍后重试。",
  serviceUnavailable: "服务尚未就绪，请稍后重试。",
});

export function isAbortFailure(error: unknown, signal: AbortSignal): boolean {
  return (
    signal.aborted ||
    (error instanceof DOMException && error.name === "AbortError") ||
    (error instanceof Error && error.name === "AbortError")
  );
}

export function toPublicMessage(error: unknown): string {
  return error instanceof PublicClientError
    ? error.message
    : PUBLIC_MESSAGES.disconnected;
}
