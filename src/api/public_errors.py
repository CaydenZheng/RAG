"""Stable public errors for RAG HTTP and SSE responses."""

PUBLIC_ERRORS = {
    "account_quota_exceeded": "今日模型使用额度已达上限，请明日再试或联系管理员",
    "account_rate_limited": "请求过于频繁或同时运行过多，请停止或稍后重试",
    "private_session_not_found": "会话不存在或不可访问",
    "conversation_busy": "该会话正在生成，请停止或稍后重试",
    "conversation_conflict": "会话模式冲突或数量已达上限，请新建不同会话或清理历史",
    "auth_required": "请先登录",
    "auth_invalid_credentials": "账号或密码不正确",
    "auth_invalid_request": "登录请求格式无效",
    "auth_password_invalid": "请使用不同的 12 至 128 字符密码",
    "auth_csrf_rejected": "请求安全校验失败，请刷新后重试",
    "auth_password_change_required": "请先修改初始密码",
    "auth_role_required": "当前账号没有此操作权限",
    "auth_transport_required": "登录需要安全连接",
    "auth_rate_limited": "登录尝试过于频繁，请稍后重试",
    "auth_body_too_large": "登录请求超过大小限制",
    "auth_busy": "登录服务繁忙，请稍后重试",
    "auth_unavailable": "登录服务尚未启用或暂不可用",
    "admin_auth_required": "需要有效的管理员凭据",
    "mcp_oauth_not_configured": "该 MCP Server 未配置 OAuth",
    "mcp_oauth_callback_not_pending": "当前没有待处理的 OAuth 授权",
    "mcp_oauth_callback_invalid": "OAuth 回调无效",
    "mcp_oauth_authorization_cancelled": "OAuth 授权已取消",
    "mcp_elicitation_not_pending": "当前没有可响应的 MCP 交互请求",
    "mcp_elicitation_response_invalid": "MCP 交互响应无效",
    "mcp_elicitation_response_too_large": "MCP 交互响应超过大小限制",
    "tool_approval_not_pending": "当前没有可处理的工具审批请求",
    "tool_approval_response_invalid": "工具审批响应无效",
    "tool_approval_response_too_large": "工具审批响应超过大小限制",
    "query_capacity_exceeded": "服务繁忙，请稍后重试",
    "request_timeout": "请求处理超时，请稍后重试",
    "query_failed": "查询处理失败，请稍后重试",
    "retrieval_failed": "检索服务暂时不可用，请稍后重试",
    "answer_generation_failed": "回答生成失败，请稍后重试",
    "internal_server_error": "服务内部错误，请稍后重试",
}


def public_error(code: str) -> dict[str, str]:
    """Return a fresh payload without internal exception details."""
    return {"code": code, "message": PUBLIC_ERRORS[code]}
