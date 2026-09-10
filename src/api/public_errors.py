"""Stable public errors for RAG HTTP and SSE responses."""

PUBLIC_ERRORS = {
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
