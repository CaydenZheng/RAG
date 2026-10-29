import { PUBLIC_MESSAGES, PublicClientError } from "./errors";

export function requireEventStream(response: Response): ReadableStream<Uint8Array> {
  if (response.status === 401 || response.status === 403) {
    throw new PublicClientError("auth_required", "登录或安全校验已失效，请重新登录后重试。");
  }
  if (response.status === 409) {
    throw new PublicClientError("validation", "会话已变化、正在生成或该回合已提交；请刷新历史核对，不要重发同一轮。");
  }
  if (response.status === 429) {
    throw new PublicClientError("service_unavailable", "请求频率、并发或今日模型额度达到上限，请稍后再试或联系管理员。");
  }
  if (response.status === 422) {
    throw new PublicClientError("validation", PUBLIC_MESSAGES.invalidRequest);
  }
  if (response.status === 503) {
    throw new PublicClientError(
      "service_unavailable",
      PUBLIC_MESSAGES.serviceUnavailable,
    );
  }
  if (!response.ok || !response.body) {
    throw new PublicClientError("http", PUBLIC_MESSAGES.disconnected);
  }
  return response.body;
}
