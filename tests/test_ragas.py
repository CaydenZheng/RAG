import sys

ok = True

print("1. Testing vertexai shim...")
try:
    from langchain_community.chat_models.vertexai import ChatVertexAI
    print("   ✅ vertexai shim OK")
except Exception as e:
    print(f"   ❌ {e}")
    ok = False

print("2. Testing RAGAS import...")
try:
    from ragas import evaluate
    from ragas.metrics import faithfulness, answer_relevancy, context_precision, context_recall
    print("   ✅ RAGAS import OK")
except Exception as e:
    print(f"   ❌ {e}")
    ok = False

print("3. Testing RAGAS LLM wrapper...")
try:
    from ragas.llms import LangchainLLMWrapper
    from langchain_openai import ChatOpenAI
    print("   ✅ LLM wrapper OK")
except Exception as e:
    print(f"   ❌ {e}")
    ok = False

if ok:
    print("\n🎉 All checks passed!")
else:
    print("\n❌ Some checks failed")
    sys.exit(1)
