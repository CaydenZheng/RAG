"""测试 P0 骨架：配置加载、LLM 调用、Embedding、Token 计数"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import settings
from src.llm import llm_client
from src.utils.token_counter import count_tokens


def test_config():
    """验证配置加载正确"""
    print("\n=" * 50)
    print("TEST: Config")
    print("=" * 50)
    print(f"  LLM Model:     {settings.llm_model}")
    print(f"  Base URL:      {settings.openai_base_url}")
    print(f"  Embed Model:   {settings.local_embedding_model}")
    print(f"  Rerank Model:  {settings.rerank_model}")
    print(f"  Chroma Dir:    {settings.chroma_path}")
    print(f"  Cache DB:      {settings.cache_db_path_resolved}")
    print(f"  RRF k:         {settings.rrf_k}")
    print(f"  Prompt Ver:    {settings.prompt_version}")
    print(f"  Langfuse:      {'enabled' if settings.langfuse_enabled else 'disabled'}")
    assert settings.llm_model == "deepseek-chat"
    print("  ✅ Config OK")


def test_chat():
    """验证 DeepSeek API 调用"""
    print("\n" + "=" * 50)
    print("TEST: LLM Chat (DeepSeek)")
    print("=" * 50)
    response = llm_client.chat([
        {"role": "user", "content": "Say 'hello' in exactly one word, lowercase."}
    ])
    print(f"  Response: {response}")
    assert len(response) > 0
    print("  ✅ Chat OK")


def test_embedding():
    """验证本地 Embedding"""
    print("\n" + "=" * 50)
    print("TEST: Local Embedding")
    print("=" * 50)
    vec = llm_client.embed_single("hello world")
    print(f"  Dim:    {len(vec)}")
    print(f"  Vec[0]: {vec[0]:.4f}")
    assert len(vec) > 0
    # 验证归一化：向量长度应接近 1
    import math
    norm = math.sqrt(sum(v * v for v in vec))
    assert abs(norm - 1.0) < 0.01, f"Expected norm ≈ 1, got {norm}"
    print(f"  Norm:   {norm:.6f} (should be ~1.0)")
    print("  ✅ Embedding OK")


def test_token_counter():
    """验证 Token 计数"""
    print("\n" + "=" * 50)
    print("TEST: Token Counter")
    print("=" * 50)
    text = "Hello, how are you doing today?"
    cnt = count_tokens(text)
    print(f"  Text:  '{text}'")
    print(f"  Tokens: {cnt}")
    assert cnt > 0
    print("  ✅ Token Counter OK")


if __name__ == "__main__":
    test_config()
    test_chat()
    test_embedding()
    test_token_counter()
    print("\n" + "=" * 50)
    print("🎉 P0: All smoke tests passed!")
    print("=" * 50)
