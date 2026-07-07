"""
Token 计数器 — 用于上下文窗口管理。

目前用 tiktoken cl100k_base 近似（DeepSeek 和多数模型 token 分布接近），
误差在 5% 以内，不影响截断安全性（已通过 CONTEXT_BUFFER_RATIO 留余量）。
"""

import tiktoken

# cl100k_base = GPT-4 / GPT-3.5-turbo 的编码，与 DeepSeek 近似
_ENCODING = tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    """返回文本的 token 数（近似）。"""
    return len(_ENCODING.encode(text))


def count_tokens_batch(texts: list[str]) -> list[int]:
    """批量计数。"""
    return [count_tokens(t) for t in texts]
