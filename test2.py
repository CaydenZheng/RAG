"""
tracer 模块验证 — 写入两条模拟 trace，确认 logs/traces.jsonl 正常产生。

用法:
    python test2.py
"""

import sys
import json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from src.infra.tracer import tracer
from loguru import logger

# ================================================================
# 模拟两次查询
# ================================================================

# 模拟查询 1：正常流程
trace1 = tracer.start_trace("a1b2c3d4", "What is gradient descent?")
tracer.add_span(trace1, "rewrite", variants=2)
tracer.add_span(trace1, "hybrid_retriever", candidates=20)
tracer.add_span(trace1, "reranker", kept=5)
tracer.add_span(trace1, "generator", answer_chars=450)
tracer.finish_trace(trace1, answer="Gradient descent is an optimization algorithm...", sources=5)

# 模拟查询 2：异常流程（LLM 调用失败）
trace2 = tracer.start_trace("e5f6g7h8", "Explain quantum computing")
tracer.add_span(trace2, "rewrite", variants=1)
tracer.add_span(trace2, "hybrid_retriever", candidates=15)
tracer.finish_trace(trace2, answer="", error="LLM timeout after 3 retries")

# ================================================================
# 打印结果
# ================================================================

trace_path = Path("logs/traces.jsonl")
assert trace_path.exists(), "Trace file not created!"

lines = trace_path.read_text(encoding="utf-8").strip().split("\n")
print(f"OK: logs/traces.jsonl has {len(lines)} records\n")

for line in lines[-2:]:
    record = json.loads(line)
    print(f"  query_id={record['query_id']}, total_ms={record.get('total_ms','N/A')}, "
          f"nodes={len(record['nodes'])}, error={record.get('error','none')}")

print("\nAll tracer checks passed")
