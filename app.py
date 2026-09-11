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

from fastapi import (
    FastAPI,
    File,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from config.settings import settings
from src.agent.harness import agent_harness
from src.api.client_identity import ClientIdentityMiddleware, scope_request_session
from src.api.observability import RequestTracingMiddleware
from src.api.public_errors import public_error
from src.api.reliability import RAGRequestReliabilityMiddleware
from src.api.schemas import (
    AgentChatRequest,
    AgentChatResponse,
    IndexJobResponse,
    QueryRequest,
    QueryResponse,
    parse_metadata_filter_json,
)
from src.api.startup import warm_up_runtime
from src.api.streaming import iter_answer_sse
from src.core.agent_runtime import AgentRuntime
from src.core.generation import AnswerInput, answer_service
from src.core.index_jobs import (
    IndexCommand,
    IndexJobConflictError,
    validate_job_id,
)
from src.core.knowledge import (
    DEFAULT_RETRIEVAL_MODE,
    DEFAULT_RETRIEVAL_TOP_K,
    MAX_RETRIEVAL_TOP_K,
    RetrievalMode,
)
from src.infra.index_jobs import get_index_jobs
from src.infra.uploads import read_upload, validate_document_filename
from src.orchestration.rag import (
    get_online_flow,
    get_retrieval_flow,
)
from src.web.pages import (
    AGENT_PAGE_HTML,
    PAGE_SECURITY_HEADERS,
    SEARCH_PAGE_HTML,
    STATIC_DIR,
)

agent_runtime: AgentRuntime = agent_harness

app = FastAPI(
    title="RAGFlow",
    description="Advanced RAG system based on PocketFlow",
    version="0.1.0",
)


app.add_middleware(RAGRequestReliabilityMiddleware)
app.add_middleware(ClientIdentityMiddleware)
app.add_middleware(RequestTracingMiddleware)
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
    query_id = request.state.request_id
    started_at = time.perf_counter()

    shared = {
        "query": req.query,
        "session_id": session.storage_id,
        "top_k": req.top_k,
        "filter": req.filter,
        "retrieval_mode": req.retrieval_mode,
    }

    try:
        flow = get_online_flow()
        await flow.run_async(shared)
    except Exception as exc:
        from src.infra.tracer import tracer

        tracer.record_error("query_failed")
        logger.error("Query {} failed: {}", query_id, type(exc).__name__)
        raise HTTPException(
            status_code=503,
            detail=public_error("query_failed"),
        ) from exc

    answer = shared.get("answer", "")

    return QueryResponse(
        query_id=query_id,
        answer=answer,
        sources=shared.get("sources", []),
        warnings=shared.get("warnings", []),
        index_version=shared.get("index_version", "legacy"),
        latency_ms=round((time.perf_counter() - started_at) * 1000, 1),
    )


def _submit_index_job(
    command: IndexCommand,
    idempotency_key: str | None,
) -> dict:
    try:
        return get_index_jobs().submit(
            command, idempotency_key=idempotency_key
        ).to_dict()
    except IndexJobConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/upload", response_model=IndexJobResponse, status_code=202)
async def upload(
    file: UploadFile = File(...),
    replace: bool = Query(default=False),
    idempotency_key: str | None = Header(
        default=None, alias="Idempotency-Key"
    ),
):
    """Queue an idempotent document create or replacement."""
    document = await read_upload(file, settings.max_upload_bytes)
    return _submit_index_job(
        IndexCommand.upload(
            document.filename,
            document.content,
            replace=replace,
        ),
        idempotency_key,
    )


@app.delete(
    "/documents/{filename}",
    response_model=IndexJobResponse,
    status_code=202,
)
def delete_document_from_index(
    filename: str,
    idempotency_key: str | None = Header(
        default=None, alias="Idempotency-Key"
    ),
):
    """Queue an idempotent document deletion and index rebuild."""
    try:
        validate_document_filename(filename)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _submit_index_job(IndexCommand.delete(filename), idempotency_key)


@app.post(
    "/index/rebuild",
    response_model=IndexJobResponse,
    status_code=202,
)
def rebuild_index(
    idempotency_key: str | None = Header(
        default=None, alias="Idempotency-Key"
    ),
):
    """Queue a full rebuild of the current document set."""
    return _submit_index_job(IndexCommand.rebuild(), idempotency_key)


@app.post(
    "/index/rollback",
    response_model=IndexJobResponse,
    status_code=202,
)
def rollback_index(
    version_id: str = "",
    idempotency_key: str | None = Header(
        default=None, alias="Idempotency-Key"
    ),
):
    """Queue activation of a retained version or the previous version."""
    return _submit_index_job(
        IndexCommand.rollback(version_id), idempotency_key
    )


@app.get("/index/jobs/{job_id}", response_model=IndexJobResponse)
def index_job_status(job_id: str):
    """Return the persistent state of one index job."""
    try:
        job = get_index_jobs().status(validate_job_id(job_id))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if job is None:
        raise HTTPException(status_code=404, detail="Index job not found")
    return job.to_dict()


# ================================================================
# 流式查询端点
# ================================================================

@app.get("/query/stream")
async def query_stream(
    request: Request,
    query: str,
    session_id: str = "",
    top_k: int = Query(
        default=DEFAULT_RETRIEVAL_TOP_K,
        ge=1,
        le=MAX_RETRIEVAL_TOP_K,
    ),
    retrieval_mode: RetrievalMode = DEFAULT_RETRIEVAL_MODE,
    filter_json: str | None = Query(default=None, alias="filter"),
):
    """
    流式检索问答（SSE）。

    1. 先执行检索管线（异步）获取上下文
    2. 再流式输出 LLM 生成结果
    3. 生成完成后发送 sources 和最终标志

    输出格式（SSE）:
        data: {"event": "chunk", "query_id": "...", "done": false, "chunk": "..."}
        data: {"event": "done", "query_id": "...", "done": true, ...}
        data: {"event": "error", "query_id": "...", "done": true, "error": {...}}
    """
    started_at = time.perf_counter()
    query_id = request.state.request_id
    session = scope_request_session(
        request, session_id, "rag", allow_empty=True
    )

    try:
        metadata_filter = parse_metadata_filter_json(filter_json)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    shared = {
        "query": query,
        "session_id": session.storage_id,
        "top_k": top_k,
        "filter": metadata_filter,
        "retrieval_mode": retrieval_mode,
    }
    try:
        retrieval_flow = get_retrieval_flow()
        await retrieval_flow.run_async(shared)
    except Exception as exc:
        from src.infra.tracer import tracer

        tracer.record_error("retrieval_failed")
        logger.error("Retrieval {} failed: {}", query_id, type(exc).__name__)
        raise HTTPException(
            status_code=503,
            detail=public_error("retrieval_failed"),
        ) from exc

    answer_input = AnswerInput.from_shared(shared)
    sources = shared.get("sources", [])
    warnings = shared.get("warnings", [])

    return StreamingResponse(
        iter_answer_sse(
            request=request,
            service=answer_service,
            answer_input=answer_input,
            sources=sources,
            warnings=warnings,
            index_version=shared.get("index_version", "legacy"),
            public_session_id=session.public_id,
            query_id=query_id,
            started_at=started_at,
        ),
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
    """Run the shared asynchronous Agent runtime."""
    session = scope_request_session(
        request, req.session_id or uuid.uuid4().hex, "agent"
    )
    result = await agent_runtime.execute(session.storage_id, req.message)
    payload = result.to_dict()
    payload["session_id"] = session.public_id
    return AgentChatResponse(**payload)

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
    session = scope_request_session(
        request, session_id or uuid.uuid4().hex, "agent"
    )

    async def event_stream():
        yield "retry: 3000\n\n"
        async for event in agent_runtime.events(
            session.storage_id, message
        ):
            payload = event.to_dict()
            if payload.get("done"):
                payload["session_id"] = session.public_id
            yield (
                "data: "
                + json.dumps(payload, ensure_ascii=False)
                + "\n\n"
            )

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
def agent_reset(request: Request, session_id: str) -> dict[str, str]:
    """重置当前客户端的 Agent 会话。"""
    session = scope_request_session(request, session_id, "agent")
    if not agent_runtime.reset_session(session.storage_id):
        raise HTTPException(
            status_code=404,
            detail="Session not found",
            headers={"Cache-Control": "no-store"},
        )
    return {"status": "ok", "message": "Session reset."}


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
