"""
本地 JSON Lines Trace 日志 — 记录每次查询的完整链路耗时和关键指标。

用法:
    from src.infra.tracer import tracer

    trace = tracer.start_trace(query_id="abc", query="What is RRF?")
    tracer.add_span(trace, "rewrite", latency_ms=320, variants=3)
    tracer.add_span(trace, "hybrid_retriever", latency_ms=42, vector_hits=20, bm25_hits=18)
    tracer.finish_trace(trace, answer="RRF stands for...", answer_len=280)

输出: logs/traces.jsonl（每行一条 JSON）
"""

import json
import time
from pathlib import Path
from typing import Any, Optional
from loguru import logger

TRACE_DIR = Path("logs")
TRACE_FILE = TRACE_DIR / "traces.jsonl"


class TraceLogger:
    """本地 JSON Lines trace 记录器"""

    def __init__(self):
        TRACE_DIR.mkdir(parents=True, exist_ok=True)

    def start_trace(self, query_id: str, query: str) -> dict:
        """开始一次查询追踪"""
        return {
            "query_id": query_id,
            "query": query,
            "nodes": [],
            "start_ts": time.time(),
        }

    def add_span(
        self,
        trace: dict,
        node: str,
        latency_ms: float = 0,
        **kwargs,
    ):
        """记录一个节点的耗时和关键指标"""
        span = {"node": node, "latency_ms": round(latency_ms, 2)}
        span.update(kwargs)
        trace["nodes"].append(span)

    def finish_trace(
        self,
        trace: dict,
        answer: str = "",
        **kwargs,
    ):
        """完成追踪，写入 JSON lines 文件"""
        elapsed = (time.time() - trace["start_ts"]) * 1000
        record = {
            "query_id": trace["query_id"],
            "query": trace["query"],
            "total_ms": round(elapsed, 2),
            "nodes": trace["nodes"],
            "answer_len": len(answer),
        }
        record.update(kwargs)

        with open(TRACE_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

        logger.info("Trace written: query_id={}, total_ms={:.1f}, nodes={}",
                     trace["query_id"], elapsed, len(trace["nodes"]))


# 全局单例
tracer = TraceLogger()
