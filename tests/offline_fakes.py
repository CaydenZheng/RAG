"""Small explicit model substitutes; no generated facts or downloaded weights."""

from collections import deque
from collections.abc import AsyncIterator
from copy import deepcopy
from typing import Any

import numpy as np
import pytest


class ScriptedLLM:
    def __init__(self) -> None:
        self.responses: deque[str] = deque()
        self.calls: list[dict[str, Any]] = []

    def chat(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        self.calls.append({"messages": deepcopy(messages), **kwargs})
        if not self.responses:
            pytest.fail("Configure a response for each expected LLM call", pytrace=False)
        return self.responses.popleft()

    async def chat_async(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        return self.chat(messages, **kwargs)

    async def chat_stream_async(
        self, messages: list[dict[str, str]], **kwargs: Any
    ) -> AsyncIterator[str]:
        yield self.chat(messages, **kwargs)


class FixedEmbedder:
    """Exercise the real LLMClient embedding adapter with explicit unit vectors."""

    def __init__(self) -> None:
        self.vectors: dict[str, list[float]] = {}
        self.calls: list[list[str]] = []

    def encode(self, texts: list[str], **kwargs: Any) -> np.ndarray:
        self.calls.append(list(texts))
        return np.asarray([self.vectors[text] for text in texts], dtype=float)

    def get_sentence_embedding_dimension(self) -> int:
        return len(next(iter(self.vectors.values())))
