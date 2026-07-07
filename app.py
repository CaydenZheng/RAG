"""
FastAPI 服务入口。

端点:
  GET  /               搜索页面
  GET  /health          健康检查
  POST /query           检索问答
  POST /upload          上传文档（触发增量索引）
  POST /agent/chat      Agent 对话（Plan-Execute-Observe）
  POST /agent/reset     重置 Agent 会话
  GET  /agent/memory/{id}  查看 Agent 记忆（调试）

用法:
    python app.py
    uvicorn app:app --host 0.0.0.0 --port 8000
"""

import sys
import threading
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

import time
import uuid
import chromadb
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from loguru import logger

from config.settings import settings
from flow import get_offline_flow, get_online_flow
from flow_agent import get_agent_flow, get_agent_reset_flow


# ================================================================
# 搜索页面 HTML（零依赖，纯内联）
# ================================================================

SEARCH_PAGE_HTML = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>RAGFlow</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #f5f5f5; color: #333; }
  .container { max-width: 800px; margin: 0 auto; padding: 40px 20px; }
  .header { text-align: center; margin-bottom: 32px; }
  .header h1 { font-size: 28px; font-weight: 700; color: #1a1a2e; }
  .header p { color: #888; margin-top: 4px; font-size: 14px; }
  .search-box { display: flex; gap: 10px; margin-bottom: 24px; }
  .search-box input { flex: 1; padding: 14px 18px; border: 2px solid #e0e0e0; border-radius: 12px; font-size: 15px; outline: none; transition: border .2s; }
  .search-box input:focus { border-color: #4f46e5; }
  .search-box button { padding: 14px 28px; background: #4f46e5; color: #fff; border: none; border-radius: 12px; font-size: 15px; font-weight: 600; cursor: pointer; transition: background .2s; }
  .search-box button:hover { background: #4338ca; }
  .search-box button:disabled { background: #a5b4fc; cursor: not-allowed; }
  .loading { text-align: center; color: #888; display: none; margin-bottom: 20px; }
  .loading.active { display: block; }
  .result { display: none; }
  .result.active { display: block; }
  .answer-card { background: #fff; border-radius: 12px; padding: 24px; margin-bottom: 20px; box-shadow: 0 1px 3px rgba(0,0,0,.08); }
  .answer-card h2 { font-size: 16px; color: #4f46e5; margin-bottom: 12px; }
  .answer-text { line-height: 1.75; white-space: pre-wrap; font-size: 15px; }
  .sources-title { font-size: 16px; font-weight: 600; color: #1a1a2e; margin-bottom: 12px; }
  .source-card { background: #fff; border-radius: 10px; padding: 16px; margin-bottom: 10px; box-shadow: 0 1px 3px rgba(0,0,0,.06); border-left: 3px solid #4f46e5; }
  .source-meta { display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; font-size: 13px; }
  .source-ref { font-weight: 700; color: #4f46e5; }
  .source-file { color: #888; }
  .source-score { background: #eef2ff; color: #4f46e5; padding: 2px 10px; border-radius: 20px; font-weight: 600; font-size: 12px; }
  .source-text { font-size: 13px; color: #555; line-height: 1.6; }
  .latency { text-align: center; color: #aaa; font-size: 12px; margin-top: 16px; }
  .error { background: #fef2f2; color: #dc2626; padding: 16px; border-radius: 10px; display: none; }
  .error.active { display: block; }
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>&#128269; RAGFlow</h1>
    <p>基于 PocketFlow 的高级 RAG 系统 &middot; 混合检索 + Rerank</p>
  </div>

  <div class="search-box">
    <input id="queryInput" type="text" placeholder="输入问题，例如：What is the half-life of actinium?" autofocus
           onkeydown="if(event.key==='Enter')search()">
    <button id="searchBtn" onclick="search()">搜索</button>
  </div>

  <div id="loading" class="loading">&#9203; 检索中，请稍候...</div>
  <div id="error" class="error"></div>

  <div id="result" class="result">
    <div class="answer-card">
      <h2>&#128172; 答案</h2>
      <div id="answer" class="answer-text"></div>
    </div>

    <div class="sources-title">&#128214; 引用来源（共 <span id="sourceCount">0</span> 条）</div>
    <div id="sources"></div>

    <div class="latency">&#9201; 耗时 <span id="latency">0</span> ms</div>
  </div>
</div>

<script>
async function search() {
  const q = document.getElementById('queryInput').value.trim();
  if (!q) return;

  const btn = document.getElementById('searchBtn');
  const loading = document.getElementById('loading');
  const result = document.getElementById('result');
  const error = document.getElementById('error');

  btn.disabled = true;
  loading.classList.add('active');
  result.classList.remove('active');
  error.classList.remove('active');

  try {
    const resp = await fetch('/query', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query: q })
    });

    if (!resp.ok) {
      const err = await resp.json();
      throw new Error(err.detail || '查询失败');
    }

    const data = await resp.json();

    // 显示答案
    document.getElementById('answer').textContent = data.answer;
    document.getElementById('sourceCount').textContent = data.sources.length;
    document.getElementById('latency').textContent = data.latency_ms;

    // 渲染引用来源
    const sourcesDiv = document.getElementById('sources');
    sourcesDiv.innerHTML = data.sources.map(s => `
      <div class="source-card">
        <div class="source-meta">
          <span class="source-ref">[${s.ref}]</span>
          <span class="source-file">${s.source || 'unknown'}</span>
          <span class="source-score">${s.score.toFixed(4)}</span>
        </div>
        <div class="source-text">${s.text.slice(0, 300)}${s.text.length > 300 ? '...' : ''}</div>
      </div>
    `).join('');

    result.classList.add('active');
  } catch (e) {
    error.textContent = '\u26a0\ufe0f ' + e.message;
    error.classList.add('active');
  } finally {
    btn.disabled = false;
    loading.classList.remove('active');
  }
}
</script>
</body>
</html>"""


app = FastAPI(
    title="RAGFlow",
    description="Advanced RAG system based on PocketFlow",
    version="0.1.0",
)


# ================================================================
# 启动预热：Reranker 预加载 + BM25 冷启动重建
# ================================================================

@app.on_event("startup")
def startup():
    """服务启动时预热模型 + 异步重建 BM25"""

    # 1. 预加载 Reranker（后台线程，避免阻塞第一个请求）
    def _preload_reranker():
        try:
            logger.info("⏳ Preloading reranker model: {}", settings.rerank_model)
            from sentence_transformers import CrossEncoder
            CrossEncoder(settings.rerank_model, max_length=512, device="cpu")
            logger.info("✅ Reranker model ready")
        except Exception as e:
            logger.warning("Reranker preload failed: {}", e)

    threading.Thread(target=_preload_reranker, daemon=True).start()

    # 2. 从 ChromaDB 异步重建 BM25（索引未就绪时自动降级为纯向量）
    def _rebuild_bm25():
        try:
            from src.utils.bm25_store import bm25_store
            persist_dir = str(settings.chroma_path.resolve())
            client = chromadb.PersistentClient(
                path=persist_dir,
                settings=chromadb.config.Settings(anonymized_telemetry=False),
            )
            collection = client.get_collection("rag_collection")
            if collection.count() == 0:
                logger.info("ChromaDB is empty, skipping BM25 rebuild")
                return

            # 拉取全部文档
            all_data = collection.get()
            texts = all_data["documents"] or []
            chunk_ids = all_data["ids"] or []

            logger.info("⏳ Rebuilding BM25 from {} ChromaDB chunks...", len(texts))
            bm25_store.build(texts, chunk_ids)
            logger.info("✅ BM25 ready: {} docs", len(texts))
        except Exception as e:
            logger.warning("BM25 rebuild failed (will use vector-only): {}", e)

    threading.Thread(target=_rebuild_bm25, daemon=True).start()


# ================================================================
# GET / — 搜索页面
# ================================================================

@app.get("/", response_class=HTMLResponse)
def index():
    return SEARCH_PAGE_HTML


# ================================================================
# 请求/响应模型
# ================================================================

class QueryRequest(BaseModel):
    query: str = Field(..., description="用户查询")
    top_k: int = Field(default=5, description="最终返回的文档数量")
    filter: dict | None = Field(default=None, description="元数据过滤条件，如 {'category':'design_pattern'}")


class QueryResponse(BaseModel):
    query_id: str
    answer: str
    sources: list[dict]
    latency_ms: float


class AgentChatRequest(BaseModel):
    session_id: str = Field(default="", description="会话 ID，空则自动生成")
    message: str = Field(..., description="用户消息")


class AgentChatResponse(BaseModel):
    session_id: str
    answer: str
    tool_calls: list[dict] = []
    iterations: int = 0
    latency_ms: float = 0.0
    error: str = ""


# ================================================================
# 端点
# ================================================================

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/query", response_model=QueryResponse)
def query(req: QueryRequest):
    """检索问答"""
    query_id = uuid.uuid4().hex[:12]
    start = time.time()

    from src.infra.tracer import tracer
    trace = tracer.start_trace(query_id, req.query)

    shared = {
        "query": req.query,
        "filter": req.filter,
    }

    try:
        flow = get_online_flow()
        flow.run(shared)
    except Exception as e:
        logger.error("Query failed: {}", e)
        tracer.finish_trace(trace, answer="", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))

    latency = (time.time() - start) * 1000
    answer = shared.get("answer", "")

    # 记录 trace：从 shared store 读取各节点埋下的指标
    tracer.add_span(trace, "rewrite",
                    variants=len(shared.get("queries", [])))
    tracer.add_span(trace, "hybrid_retriever",
                    candidates=len(shared.get("candidates", [])))
    tracer.add_span(trace, "reranker",
                    kept=len(shared.get("retrieved_chunks", [])))
    tracer.add_span(trace, "generator",
                    answer_chars=len(answer))
    tracer.finish_trace(trace, answer=answer, sources=len(shared.get("sources", [])))

    return QueryResponse(
        query_id=query_id,
        answer=answer,
        sources=shared.get("sources", []),
        latency_ms=round(latency, 1),
    )


@app.post("/upload")
async def upload(file: UploadFile = File(...)):
    """上传文档，触发增量索引重建"""
    # 保存文件到 data/raw/
    raw_dir = settings.raw_dir
    raw_dir.mkdir(parents=True, exist_ok=True)
    file_path = raw_dir / file.filename

    content = await file.read()
    file_path.write_bytes(content)
    logger.info("File saved: {}", file_path)

    # 全量重建索引
    flow = get_offline_flow()
    shared = {}
    flow.run(shared)

    info = shared.get("index_info", {})
    return {
        "status": "indexed",
        "chunks": info.get("chunks_count", 0),
        "fingerprint": info.get("fingerprint", ""),
    }


# ================================================================
# 启动
# ================================================================

# ================================================================
# Agent 端点
# ================================================================

@app.post("/agent/chat", response_model=AgentChatResponse)
def agent_chat(req: AgentChatRequest):
    """Agent 对话端点 — Plan-Execute-Observe 循环"""
    import uuid
    session_id = req.session_id or uuid.uuid4().hex[:12]
    start = time.time()

    flow = get_agent_flow()
    shared = {
        "session_id": session_id,
        "user_message": req.message,
    }

    try:
        flow.run(shared)
    except Exception as e:
        logger.error("Agent chat failed: {}", e)
        raise HTTPException(status_code=500, detail=str(e))

    latency = (time.time() - start) * 1000

    return AgentChatResponse(
        session_id=session_id,
        answer=shared.get("answer", ""),
        tool_calls=shared.get("tool_calls", []),
        iterations=shared.get("iterations", 0),
        latency_ms=round(latency, 1),
        error=shared.get("agent_error", ""),
    )


@app.post("/agent/reset")
def agent_reset(session_id: str):
    """重置 Agent 会话 — /new 命令"""
    flow = get_agent_reset_flow()
    shared = {"session_id": session_id}
    flow.run(shared)
    return {"status": "ok", "message": shared.get("answer", "Session reset.")}


@app.get("/agent/memory/{session_id}")
def agent_memory(session_id: str):
    """查看 Agent 会话记忆（调试用）"""
    from src.agent.memory import memory_manager
    history = memory_manager.load_history(session_id)
    long_term = memory_manager.long_term_memory
    return {
        "session_id": session_id,
        "long_term_memory": long_term[:500],
        "history_turns": len(history),
        "history": [{"role": t.role, "content": t.content[:200]} for t in history[-10:]],
    }


if __name__ == "__main__":
    import uvicorn
    logger.info("Starting RAGFlow server on http://0.0.0.0:8000")
    logger.info("  RAG endpoint:  POST /query")
    logger.info("  Agent endpoint: POST /agent/chat")
    uvicorn.run(app, host="0.0.0.0", port=8000)
