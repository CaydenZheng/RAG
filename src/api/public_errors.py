"""Stable public errors for RAG HTTP and SSE responses."""

PUBLIC_ERRORS = {
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
