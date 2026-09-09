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

import json
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from config.settings import settings
from src.api.client_identity import ClientIdentityMiddleware, scope_request_session
from src.api.schemas import (
    AgentChatRequest,
    AgentChatResponse,
    QueryRequest,
    QueryResponse,
)
from src.api.startup import warm_up_runtime
from src.llm import llm_client
from src.orchestration.agent import get_agent_flow, get_agent_reset_flow
from src.orchestration.rag import (
    get_offline_flow,
    get_online_flow,
    get_retrieval_flow,
)
from src.web.pages import (
    AGENT_PAGE_HTML,
    PAGE_SECURITY_HEADERS,
    SEARCH_PAGE_HTML,
    STATIC_DIR,
)

app = FastAPI(
    title="RAGFlow",
    description="Advanced RAG system based on PocketFlow",
    version="0.1.0",
)


app.add_middleware(ClientIdentityMiddleware)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.router.add_event_handler("startup", warm_up_runtime)


# ================================================================
# GET / — 搜索页面
# ================================================================

@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(SEARCH_PAGE_HTML, headers=PAGE_SECURITY_HEADERS)


@app.get("/agent", response_class=HTMLResponse)
def agent_page():
    return HTMLResponse(AGENT_PAGE_HTML, headers=PAGE_SECURITY_HEADERS)


# ================================================================
# 端点
# ================================================================

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/query", response_model=QueryResponse)
async def query(req: QueryRequest, request: Request):
    """检索问答（异步）"""
    session = scope_request_session(
        request, req.session_id, "rag", allow_empty=True
    )
    query_id = uuid.uuid4().hex[:12]
    start = time.time()

    from src.infra.tracer import tracer
    trace = tracer.start_trace(query_id, req.query)

    shared = {
        "query": req.query,
        "session_id": session.storage_id,
        "filter": req.filter,
    }

    try:
        flow = get_online_flow()
        await flow.run_async(shared)
    except Exception as e:
        logger.error("Query failed: {}", e)
        tracer.finish_trace(trace, answer="", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))

    latency = (time.time() - start) * 1000
    answer = shared.get("answer", "")

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
    import asyncio as _asyncio

    from src.infra.uploads import save_upload

    file_path = await save_upload(file, settings.raw_dir, settings.max_upload_bytes)
    logger.info("File saved: {}", file_path)

    # 离线索引是 CPU 密集型（embedding），放入线程池避免阻塞事件循环
    def _run_indexing():
        flow = get_offline_flow()
        shared = {}
        flow.run(shared)
        return shared.get("index_info", {})

    loop = _asyncio.get_event_loop()
    info = await loop.run_in_executor(None, _run_indexing)

    return {
        "status": "indexed",
        "chunks": info.get("chunks_count", 0),
        "fingerprint": info.get("fingerprint", ""),
    }


# ================================================================
# 流式查询端点
# ================================================================

@app.get("/query/stream")
async def query_stream(
    request: Request,
    query: str,
    session_id: str = "",
    top_k: int = 5,
):
    """
    流式检索问答（SSE）。

    1. 先执行检索管线（异步）获取上下文
    2. 再流式输出 LLM 生成结果
    3. 生成完成后发送 sources 和最终标志

    输出格式（SSE）:
        data: {"chunk": "文本增量"}
        data: {"done": true, "sources": [...], "session_id": "..."}
    """
    import asyncio

    from src.infra.prompt_manager import prompt_manager
    from src.infra.session_store import session_store

    session = scope_request_session(
        request, session_id, "rag", allow_empty=True
    )

    # --- 阶段 1: 检索 ---
    shared = {"query": query, "session_id": session.storage_id}
    try:
        retrieval_flow = get_retrieval_flow()
        await retrieval_flow.run_async(shared)
    except Exception as e:
        logger.error("Retrieval failed: {}", e)
        raise HTTPException(status_code=500, detail=str(e))

    context = shared.get("context", "")
    sources = shared.get("sources", [])

    # 加载会话历史
    history = []
    if session.storage_id:
        history = session_store.get_recent_history(session.storage_id, limit=6)

    # --- 阶段 2: 流式生成 ---
    prompt_config = prompt_manager.get_prompt_config("answer_generation")
    messages = prompt_manager.render_chat_messages(
        "answer_generation", query=query, context=context)

    # 注入会话历史（带 token 预算保护）
    if history:
        history_msgs = [{"role": t["role"], "content": t["content"]} for t in history]
        # 控制历史消息不超过模型上下文窗口的 40%
        from src.utils.token_counter import count_tokens
        max_history_tokens = int(settings.max_context_tokens * 0.40)
        truncated = []
        token_sum = 0
        for h in reversed(history_msgs):
            t = count_tokens(h["content"])
            if token_sum + t > max_history_tokens:
                break
            truncated.insert(0, h)
            token_sum += t
        messages[1:1] = truncated
        logger.debug("Injected {} history turns (~{} tokens)", len(truncated), token_sum)

    async def event_stream():
        # 立即发送连接建立事件，确保浏览器识别 SSE 已就绪
        yield "retry: 3000\n\n"
        await asyncio.sleep(0)  # 强制刷新

        full_answer = []
        try:
            async for chunk in llm_client.chat_stream_async(
                messages,
                temperature=prompt_config["temperature"],
                max_tokens=prompt_config["max_tokens"],
            ):
                full_answer.append(chunk)
                yield f"data: {json.dumps({'chunk': chunk}, ensure_ascii=False)}\n\n"
                await asyncio.sleep(0)  # 强制事件循环刷新，防止 uvicorn 缓冲
        except Exception as e:
            logger.error("Stream generation failed: {}", e)
            yield f"data: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"
            return

        answer = "".join(full_answer)

        # 保存会话
        if session.storage_id:
            session_store.append_exchange(session.storage_id, query, answer)

        # 发送结束信号（含 sources）
        yield f"data: {json.dumps({'done': True, 'answer': answer, 'sources': sources, 'session_id': session.public_id}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ================================================================
# 启动
# ================================================================

# ================================================================
# 会话端点
# ================================================================

@app.post("/session/reset")
def session_reset(request: Request, session_id: str):
    """重置当前客户端的 RAG 会话。"""
    from src.infra.session_store import session_store

    session = scope_request_session(request, session_id, "rag")
    if not session_store.clear(session.storage_id):
        raise HTTPException(
            status_code=404,
            detail="Session not found",
            headers={"Cache-Control": "no-store"},
        )
    return {"status": "ok", "session_id": session.public_id}


@app.get("/session/{session_id}")
def session_detail(request: Request, response: Response, session_id: str):
    """查看当前客户端的 RAG 会话历史。"""
    from src.infra.session_store import session_store

    session = scope_request_session(request, session_id, "rag")
    history = session_store.get_history(session.storage_id, limit=50)
    response.headers["Cache-Control"] = "no-store"
    if not history:
        raise HTTPException(
            status_code=404,
            detail="Session not found",
            headers={"Cache-Control": "no-store"},
        )
    return {
        "session_id": session.public_id,
        "total_turns": len(history),
        "history": history,
    }


# ================================================================
# Agent 端点
# ================================================================

@app.post("/agent/chat", response_model=AgentChatResponse)
async def agent_chat(req: AgentChatRequest, request: Request):
    """Agent 对话端点 — Plan-Execute-Observe 循环（异步版）"""
    session = scope_request_session(
        request, req.session_id or uuid.uuid4().hex, "agent"
    )
    start = time.time()

    # 使用 asyncio.to_thread 避免同步 flow 阻塞事件循环
    import asyncio
    loop = asyncio.get_event_loop()

    def _run_sync():
        flow = get_agent_flow()
        shared = {"session_id": session.storage_id, "user_message": req.message}
        try:
            flow.run(shared)
        except Exception as e:
            shared["agent_error"] = str(e)
        return shared

    shared = await loop.run_in_executor(None, _run_sync)

    latency = (time.time() - start) * 1000

    return AgentChatResponse(
        session_id=session.public_id,
        answer=shared.get("answer", ""),
        tool_calls=shared.get("tool_calls", []),
        iterations=shared.get("iterations", 0),
        latency_ms=round(latency, 1),
        error=shared.get("agent_error", ""),
    )


@app.get("/agent/chat/stream")
async def agent_chat_stream(
    request: Request,
    message: str,
    session_id: str = "",
):
    """
    Agent 流式对话端点（SSE）。

    实时推送 Agent 思考过程：planning → tool_call → tool_done → chunk → done。

    输出格式:
      data: {"step": "planning", "iteration": 1}
      data: {"step": "tool_call", "tool": "search_knowledge_base", "params": {...}}
      data: {"step": "tool_done", "tool": "search_knowledge_base", "success": true}
      data: {"chunk": "..."}          ← 最终答案逐词输出
      data: {"done": true, ...}
    """
    import asyncio as _asyncio
    session = scope_request_session(
        request, session_id or uuid.uuid4().hex, "agent"
    )

    from src.agent.harness import agent_harness

    async def event_stream():
        yield "retry: 3000\n\n"
        await _asyncio.sleep(0)

        async for event in agent_harness.run_async_stream(
            session.storage_id, message
        ):
            if event.startswith("data: "):
                payload = json.loads(event.removeprefix("data: "))
                if payload.get("done"):
                    payload["session_id"] = session.public_id
                    event = f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            yield event

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.post("/agent/reset")
def agent_reset(request: Request, session_id: str):
    """重置当前客户端的 Agent 会话。"""
    session = scope_request_session(request, session_id, "agent")
    flow = get_agent_reset_flow()
    shared = {"session_id": session.storage_id}
    flow.run(shared)
    if not shared.get("session_found", False):
        raise HTTPException(
            status_code=404,
            detail="Session not found",
            headers={"Cache-Control": "no-store"},
        )
    return {"status": "ok", "message": shared.get("answer", "Session reset.")}


@app.get("/agent/memory/{session_id}")
def agent_memory(request: Request, response: Response, session_id: str):
    """查看当前客户端的 Agent 会话记忆。"""
    from src.agent.memory import memory_manager

    session = scope_request_session(request, session_id, "agent")
    history = memory_manager.load_history(session.storage_id)
    response.headers["Cache-Control"] = "no-store"
    if not history:
        raise HTTPException(
            status_code=404,
            detail="Session not found",
            headers={"Cache-Control": "no-store"},
        )
    long_term = memory_manager.long_term_memory_for_session(session.storage_id)
    return {
        "session_id": session.public_id,
        "long_term_memory": long_term[:500],
        "history_turns": len(history),
        "history": [{"role": t.role, "content": t.content[:200]} for t in history[-10:]],
    }


if __name__ == "__main__":
    import uvicorn
    logger.info("Starting RAGFlow server on http://0.0.0.0:8000")
    logger.info("  RAG endpoint:        POST /query")
    logger.info("  RAG Stream endpoint: GET  /query/stream")
    logger.info("  Session endpoint:    POST /session/reset  GET /session/{id}")
    logger.info("  Agent endpoint:      POST /agent/chat")
    logger.info("  Agent Stream:        GET  /agent/chat/stream")
    uvicorn.run(app, host="0.0.0.0", port=8000)
