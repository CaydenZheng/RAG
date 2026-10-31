import { PUBLIC_MESSAGES, PublicClientError } from "./errors";
import { isRecord } from "./sse";

// 后端明确返回的公开错误码 -> 用户可读文案；未列入的仍按状态码兜底。
const RESPONSE_MESSAGES: Readonly<Record<string, string>> = Object.freeze({
  context_too_large: "资料无法完整放入本轮上下文，请拆分文件。",
  attachment_not_ready: "附件不存在、已删除或尚未解析完成，请刷新核对。",
  retrieval_failed: "检索服务暂时不可用，请稍后重试。",
  query_capacity_exceeded: "服务繁忙，请稍后重试。",
  skill_not_selected: "所选技能不可用，请刷新技能列表后重试。",
  skill_selection_limit: "单轮选择的技能过多，请减少后重试。",
  conversation_busy: "该会话正在生成，请停止或稍后重试。",
  conversation_conflict: "会话已变化、正在生成或该回合已提交；请刷新历史核对，不要重发同一轮。",
});

async function publicErrorCode(response: Response): Promise<string> {
  try {
    const body: unknown = await response.json();
    const detail = isRecord(body) ? body.detail : undefined;
    const code = isRecord(detail) ? detail.code : detail;
    return typeof code === "string" ? code : "";
  } catch {
    return "";
  }
}

export async function requireEventStream(response: Response): Promise<ReadableStream<Uint8Array>> {
  if (response.ok && response.body) return response.body;

  if (response.status === 401 || response.status === 403) {
    throw new PublicClientError("auth_required", "登录或安全校验已失效，请重新登录后重试。");
  }

  const code: string = [409, 422, 429, 503].includes(response.status)
    ? await publicErrorCode(response)
    : "";
  const specific: string | undefined = Object.hasOwn(RESPONSE_MESSAGES, code) ? RESPONSE_MESSAGES[code] : undefined;
  if (specific) throw new PublicClientError(response.status === 503 ? "service_unavailable" : "validation", specific);

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
    throw new PublicClientError("service_unavailable", PUBLIC_MESSAGES.serviceUnavailable);
  }
  throw new PublicClientError("http", PUBLIC_MESSAGES.disconnected);
}
