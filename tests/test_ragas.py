import sys
from importlib import import_module

ok = True

print("1. Testing vertexai shim...")
try:
    getattr(import_module("langchain_community.chat_models.vertexai"), "ChatVertexAI")
    print("   ✅ vertexai shim OK")
except Exception as e:
    print(f"   ❌ {e}")
    ok = False

print("2. Testing RAGAS import...")
try:
    getattr(import_module("ragas"), "evaluate")
    metrics = import_module("ragas.metrics")
    for metric_name in (
        "faithfulness",
        "answer_relevancy",
        "context_precision",
        "context_recall",
    ):
        getattr(metrics, metric_name)
    print("   ✅ RAGAS import OK")
except Exception as e:
    print(f"   ❌ {e}")
    ok = False

print("3. Testing RAGAS LLM wrapper...")
try:
    getattr(import_module("ragas.llms"), "LangchainLLMWrapper")
    getattr(import_module("langchain_openai"), "ChatOpenAI")
    print("   ✅ LLM wrapper OK")
except Exception as e:
    print(f"   ❌ {e}")
    ok = False

if ok:
    print("\n🎉 All checks passed!")
else:
    print("\n❌ Some checks failed")
    sys.exit(1)
