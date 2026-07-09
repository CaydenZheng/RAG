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
import json
import chromadb
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field
from loguru import logger

from config.settings import settings
from flow import get_offline_flow, get_online_flow, get_retrieval_flow
from flow_agent import get_agent_flow, get_agent_reset_flow
from src.llm import llm_client


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
  .search-box button.stop { background: #dc2626; display: none; }
  .search-box button.stop:hover { background: #b91c1c; }
  .search-box button.stop.active { display: inline-block; }
  .loading { text-align: center; color: #888; display: none; margin-bottom: 20px; }
  .loading.active { display: block; }
  .result { display: none; }
  .result.active { display: block; }
  .answer-card { background: #fff; border-radius: 12px; padding: 24px; margin-bottom: 20px; box-shadow: 0 1px 3px rgba(0,0,0,.08); }
  .answer-card h2 { font-size: 16px; color: #4f46e5; margin-bottom: 12px; }
  .answer-text { line-height: 1.75; font-size: 15px; white-space: pre-wrap; word-break: break-word; }
  .answer-text.streaming::after { content: '|'; color: #4f46e5; animation: blink 1s step-end infinite; }
  @keyframes blink { 50% { opacity: 0; } }
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
    <p>基于 PocketFlow 的高级 RAG 系统 &middot; 混合检索 + Rerank &middot; <a href="#" onclick="newSession()" style="color:#4f46e5">🔄 新会话</a> &middot; <a href="/agent" style="color:#4f46e5">🤖 Agent 模式</a></p>
  </div>

  <div class="search-box">
    <input id="queryInput" type="text" placeholder="输入问题，例如：What is the half-life of actinium?" autofocus
           onkeydown="if(event.key==='Enter')search()">
    <button id="searchBtn" onclick="search()">搜索</button>
    <button id="stopBtn" class="stop" onclick="stopSearch()">⏹ 停止</button>
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
// 会话管理
const SESSION_KEY = 'ragflow_session_id';
let sessionId = localStorage.getItem(SESSION_KEY);
if (!sessionId) {
  sessionId = crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).slice(2, 14);
  localStorage.setItem(SESSION_KEY, sessionId);
}

let currentEventSource = null;

function stopSearch() {
  if (currentEventSource) {
    currentEventSource.close();
    currentEventSource = null;
  }
  document.getElementById('searchBtn').disabled = false;
  document.getElementById('searchBtn').style.display = '';
  document.getElementById('stopBtn').classList.remove('active');
  document.getElementById('loading').classList.remove('active');
  var ael = document.getElementById('answer');
  ael.classList.remove('streaming');
  if (!ael.textContent) {
    ael.textContent = '⏹ 已终止';
  }
  document.getElementById('latency').textContent = '—';
}

function search() {
  const q = document.getElementById('queryInput').value.trim();
  if (!q) return;

  // 终止上一个请求
  stopSearch();

  const btn = document.getElementById('searchBtn');
  const stopBtn = document.getElementById('stopBtn');
  const loading = document.getElementById('loading');
  const result = document.getElementById('result');
  const error = document.getElementById('error');
  const answerEl = document.getElementById('answer');

  btn.style.display = 'none';
  stopBtn.classList.add('active');
  loading.classList.add('active');
  error.classList.remove('active');
  answerEl.textContent = '';
  answerEl.classList.add('streaming');
  document.getElementById('sources').innerHTML = '';
  document.getElementById('sourceCount').textContent = '0';
  // 关键：让结果容器立刻可见，这样 rAF 逐词更新才能被用户感知
  result.classList.add('active');

  const url = '/query/stream?query=' + encodeURIComponent(q) +
              '&session_id=' + encodeURIComponent(sessionId);
  const evtSource = new EventSource(url);
  currentEventSource = evtSource;

  // ====== rAF 队列渲染器 ======
  const TYPING_INTERVAL = 35;  // ms 间隔，控制打字速度（越小越快）
  const chunkQueue = [];
  let fullAnswer = '';
  let animating = false;
  let finished = false;
  let lastRenderTime = 0;
  let firstRender = true;
  const startTime = Date.now();

  function renderFrame(timestamp) {
    // 队列清空 → 停止循环；若流已结束则收尾
    if (chunkQueue.length === 0) {
      animating = false;
      if (finished) finishRendering();
      return;
    }

    // 时间闸门：距上次渲染不足 TYPING_INTERVAL 则跳过本帧
    if (firstRender || timestamp - lastRenderTime >= TYPING_INTERVAL) {
      fullAnswer += chunkQueue.shift();
      answerEl.textContent = fullAnswer;
      lastRenderTime = timestamp;
      firstRender = false;
    }

    requestAnimationFrame(renderFrame);
  }

  function finishRendering() {
    answerEl.classList.remove('streaming');
    evtSource.close();
    currentEventSource = null;
    btn.style.display = '';
    stopBtn.classList.remove('active');
    loading.classList.remove('active');
    result.classList.add('active');
    document.getElementById('latency').textContent = (Date.now() - startTime);
  }

  // ====== SSE 事件处理 ======
  evtSource.onmessage = function(e) {
    const data = JSON.parse(e.data);

    if (data.error) {
      loading.classList.remove('active');
      answerEl.classList.remove('streaming');
      error.textContent = '\u26a0\ufe0f ' + data.error;
      error.classList.add('active');
      btn.style.display = '';
      stopBtn.classList.remove('active');
      currentEventSource = null;
      evtSource.close();
      return;
    }

    if (data.chunk && data.chunk.length > 0) {
      // 首个 chunk 到达 → 流式已启动，隐藏 loading 让用户注意力回到结果区
      if (!animating) {
        loading.classList.remove('active');
      }
      // 保留空格分片（给词间留空），只过滤零长度空串
      chunkQueue.push(data.chunk);
      if (!animating) {
        animating = true;
        requestAnimationFrame(renderFrame);
      }
    }

    if (data.done) {
      finished = true;
      document.getElementById('sourceCount').textContent = data.sources.length;
      document.getElementById('sources').innerHTML = data.sources.map(s => `
        <div class="source-card">
          <div class="source-meta">
            <span class="source-ref">[${s.ref}]</span>
            <span class="source-file">${s.source || 'unknown'}</span>
            <span class="source-score">${s.score.toFixed(4)}</span>
          </div>
          <div class="source-text">${s.text.slice(0, 300)}${s.text.length > 300 ? '...' : ''}</div>
        </div>
      `).join('');
      if (!animating) finishRendering();
    }
  };

  evtSource.onerror = function() {
    // 正常结束：finished 已为 true，finishRendering() 已/将负责收尾 → 什么都不做
    if (finished) return;
    // 异常中断：loading 还在、没有收到任何答案 → 显示错误
    evtSource.close();
    currentEventSource = null;
    loading.classList.remove('active');
    answerEl.classList.remove('streaming');
    btn.style.display = '';
    stopBtn.classList.remove('active');
    error.textContent = '\u26a0\ufe0f 连接中断或查询失败，请稍后重试';
    error.classList.add('active');
  };
}

function newSession() {
  if (confirm('开始新会话？当前对话历史将被清除。')) {
    sessionId = crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).slice(2, 14);
    localStorage.setItem(SESSION_KEY, sessionId);
    document.getElementById('result').classList.remove('active');
    document.getElementById('queryInput').value = '';
    document.getElementById('queryInput').focus();
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

    # 1. 预加载 Reranker（调用模块级单例，确保查询时命中缓存）
    def _preload_reranker():
        try:
            logger.info("⏳ Preloading reranker model: {}", settings.rerank_model)
            from src.core.retrieval import _get_reranker
            _get_reranker()
            logger.info("✅ Reranker model ready")
        except Exception as e:
            logger.warning("Reranker preload failed: {}", e)

    threading.Thread(target=_preload_reranker, daemon=True).start()

    # 2. 预加载 Embedding 模型（触发 llm_client 的延迟加载缓存）
    def _preload_embedding():
        try:
            logger.info("⏳ Preloading embedding model: {}", settings.local_embedding_model)
            from src.llm import llm_client
            _ = llm_client.embedding_dim
            logger.info("✅ Embedding model ready")
        except Exception as e:
            logger.warning("Embedding preload failed: {}", e)

    threading.Thread(target=_preload_embedding, daemon=True).start()

    # 3. 从 ChromaDB 异步重建 BM25（索引未就绪时自动降级为纯向量）
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
# Agent 页面 HTML（零依赖，纯内联）
# ================================================================

AGENT_PAGE_HTML = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>RAGFlow Agent</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #f5f5f5; color: #333; }
  .container { max-width: 800px; margin: 0 auto; padding: 40px 20px; }
  .header { text-align: center; margin-bottom: 24px; }
  .header h1 { font-size: 28px; font-weight: 700; color: #1a1a2e; }
  .header p { color: #888; margin-top: 4px; font-size: 14px; }
  .header a { color: #4f46e5; text-decoration: none; }
  .input-box { display: flex; gap: 10px; margin-bottom: 20px; }
  .input-box input { flex: 1; padding: 14px 18px; border: 2px solid #e0e0e0; border-radius: 12px; font-size: 15px; outline: none; transition: border .2s; }
  .input-box input:focus { border-color: #4f46e5; }
  .input-box button { padding: 14px 24px; border: none; border-radius: 12px; font-size: 15px; font-weight: 600; cursor: pointer; transition: background .2s; }
  #sendBtn { background: #4f46e5; color: #fff; }
  #sendBtn:hover { background: #4338ca; }
  #stopBtn { background: #dc2626; color: #fff; display: none; }
  #stopBtn:hover { background: #b91c1c; }
  #stopBtn.active { display: inline-block; }
  .chat-area { margin-bottom: 16px; }
  .thinking { margin-bottom: 16px; }
  .step { background: #fff; border-radius: 10px; padding: 14px 18px; margin-bottom: 8px; box-shadow: 0 1px 3px rgba(0,0,0,.06); font-size: 14px; display: flex; align-items: flex-start; gap: 10px; }
  .step-icon { font-size: 18px; flex-shrink: 0; }
  .step-content { flex: 1; line-height: 1.6; }
  .step-meta { color: #888; font-size: 12px; margin-top: 4px; }
  .step-detail { background: #f8f9fa; border-radius: 8px; padding: 10px 14px; margin-top: 6px; font-family: monospace; font-size: 12px; white-space: pre-wrap; word-break: break-all; max-height: 120px; overflow-y: auto; cursor: pointer; color: #555; }
  .step-detail.collapsed { max-height: 0; padding: 0; overflow: hidden; }
  .answer-card { background: #fff; border-radius: 12px; padding: 24px; box-shadow: 0 1px 3px rgba(0,0,0,.08); border-left: 3px solid #4f46e5; }
  .answer-card h2 { font-size: 16px; color: #4f46e5; margin-bottom: 12px; }
  .answer-text { line-height: 1.75; font-size: 15px; white-space: pre-wrap; word-break: break-word; min-height: 24px; }
  .answer-text.streaming::after { content: '|'; color: #4f46e5; animation: blink 1s step-end infinite; }
  @keyframes blink { 50% { opacity: 0; } }
  .answer-text.placeholder { color: #bbb; font-style: italic; }
  .loading { text-align: center; color: #888; display: none; margin: 12px 0; }
  .loading.active { display: block; }
  .latency { text-align: center; color: #aaa; font-size: 12px; margin-top: 12px; }
  .error { background: #fef2f2; color: #dc2626; padding: 16px; border-radius: 10px; display: none; margin-bottom: 12px; }
  .error.active { display: block; }
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>🤖 RAGFlow Agent</h1>
    <p>Plan → Execute → Observe 循环 &middot; <a href="#" onclick="newSession()">🔄 新会话</a> &middot; <a href="/">🔍 RAG 搜索</a></p>
  </div>

  <div class="input-box">
    <input id="msgInput" type="text" placeholder="输入消息，例如：search knowledge base for gradient descent" autofocus
           onkeydown="if(event.key==='Enter')send()">
    <button id="sendBtn" onclick="send()">发送</button>
    <button id="stopBtn" onclick="stopAgent()">⏹ 停止</button>
  </div>

  <div id="loading" class="loading">🤔 Agent 思考中...</div>
  <div id="error" class="error"></div>

  <div class="chat-area">
    <div id="thinking"></div>
    <div id="answerCard" class="answer-card" style="display:none">
      <h2>💬 最终答案</h2>
      <div id="answer" class="answer-text"></div>
    </div>
  </div>

  <div class="latency">⏱ 耗时 <span id="latency">—</span> &middot; 迭代 <span id="iterCount">—</span> 轮</div>
</div>

<script>
// 会话管理
const SESSION_KEY = 'ragflow_agent_session';
let sessionId = localStorage.getItem(SESSION_KEY);
if (!sessionId) {
  sessionId = crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).slice(2, 14);
  localStorage.setItem(SESSION_KEY, sessionId);
}

let currentEventSource = null;

function stopAgent() {
  if (currentEventSource) {
    currentEventSource.close();
    currentEventSource = null;
  }
  document.getElementById('sendBtn').style.display = '';
  document.getElementById('stopBtn').classList.remove('active');
  document.getElementById('loading').classList.remove('active');
  var ael = document.getElementById('answer');
  ael.classList.remove('streaming');
  if (!ael.textContent) {
    ael.textContent = '⏹ 已终止';
    document.getElementById('answerCard').style.display = 'block';
  }
}

function send() {
  var msg = document.getElementById('msgInput').value.trim();
  if (!msg) return;

  stopAgent();

  var sendBtn = document.getElementById('sendBtn');
  var stopBtn = document.getElementById('stopBtn');
  var loading = document.getElementById('loading');
  var error = document.getElementById('error');
  var thinking = document.getElementById('thinking');
  var answerEl = document.getElementById('answer');
  var answerCard = document.getElementById('answerCard');

  sendBtn.style.display = 'none';
  stopBtn.classList.add('active');
  loading.classList.add('active');
  error.classList.remove('active');
  error.textContent = '';
  thinking.innerHTML = '';
  answerEl.textContent = '';
  answerEl.classList.add('streaming');
  answerEl.classList.remove('placeholder');
  answerCard.style.display = 'none';
  document.getElementById('latency').textContent = '—';
  document.getElementById('iterCount').textContent = '—';

  var url = '/agent/chat/stream?message=' + encodeURIComponent(msg) +
            '&session_id=' + encodeURIComponent(sessionId);
  var evtSource = new EventSource(url);
  currentEventSource = evtSource;

  // ====== rAF 渲染器 ======
  var TYPING_INTERVAL = 35;
  var chunkQueue = [];
  var fullAnswer = '';
  var animating = false;
  var finished = false;
  var lastRenderTime = 0;
  var firstRender = true;
  var startTime = Date.now();
  var iterCount = 0;

  function renderFrame(timestamp) {
    if (chunkQueue.length === 0) {
      animating = false;
      if (finished) finishRendering();
      return;
    }
    if (firstRender || timestamp - lastRenderTime >= TYPING_INTERVAL) {
      fullAnswer += chunkQueue.shift();
      answerEl.textContent = fullAnswer;
      lastRenderTime = timestamp;
      firstRender = false;
    }
    requestAnimationFrame(renderFrame);
  }

  function linkify(text) {
    // 将纯文本 URL 转为可点击链接（排除空白和 HTML 敏感字符）
    return text.replace(/(https?:\/\/[^\s<>"']+)/g,
      '<a href="$1" target="_blank" rel="noopener">$1</a>');
  }

  function finishRendering() {
    answerEl.classList.remove('streaming');
    // 打字结束，将纯文本 URL 转为可点击的 <a> 链接
    answerEl.innerHTML = linkify(fullAnswer);
    evtSource.close();
    currentEventSource = null;
    sendBtn.style.display = '';
    stopBtn.classList.remove('active');
    loading.classList.remove('active');
    answerCard.style.display = 'block';
    document.getElementById('latency').textContent = (Date.now() - startTime) + ' ms';
    document.getElementById('iterCount').textContent = iterCount;
  }

  // ====== SSE 事件处理 ======
  evtSource.onmessage = function(e) {
    var data = JSON.parse(e.data);

    if (data.error) {
      loading.classList.remove('active');
      answerEl.classList.remove('streaming');
      error.textContent = '⚠️ ' + data.error;
      error.classList.add('active');
      sendBtn.style.display = '';
      stopBtn.classList.remove('active');
      currentEventSource = null;
      evtSource.close();
      return;
    }

    // 思考步骤
    if (data.step === 'planning') {
      loading.classList.remove('active');
      iterCount = data.iteration;
      var div = document.createElement('div');
      div.className = 'step';
      div.innerHTML = '<span class="step-icon">🤔</span><div class="step-content">第 ' + data.iteration + ' 轮规划中…</div>';
      thinking.appendChild(div);
      thinking.scrollTop = thinking.scrollHeight;
      return;
    }

    if (data.step === 'tool_call') {
      var paramsStr = JSON.stringify(data.params, null, 2);
      var div = document.createElement('div');
      div.className = 'step';
      div.innerHTML = '<span class="step-icon">🔧</span><div class="step-content">调用工具: <b>' + escHtml(data.tool) + '</b>' +
        '<div class="step-detail collapsed" onclick="this.classList.toggle(\'collapsed\')">' + escHtml(paramsStr) + '</div></div>';
      thinking.appendChild(div);
      thinking.scrollTop = thinking.scrollHeight;
      return;
    }

    if (data.step === 'tool_done') {
      var icon = data.success ? '✅' : '❌';
      var div = document.createElement('div');
      div.className = 'step';
      div.innerHTML = '<span class="step-icon">' + icon + '</span><div class="step-content">工具 <b>' + escHtml(data.tool) + '</b> ' + (data.success ? '完成' : '失败') + '</div>';
      thinking.appendChild(div);
      thinking.scrollTop = thinking.scrollHeight;
      return;
    }

    // 答案逐词
    if (data.chunk && data.chunk.length > 0) {
      if (!animating) {
        answerCard.style.display = 'block';
        loading.classList.remove('active');
      }
      chunkQueue.push(data.chunk);
      if (!animating) {
        animating = true;
        requestAnimationFrame(renderFrame);
      }
      return;
    }

    // 完成
    if (data.done) {
      finished = true;
      if (data.iterations) iterCount = data.iterations;
      if (!animating) finishRendering();
      return;
    }
  };

  evtSource.onerror = function() {
    if (finished) return;
    evtSource.close();
    currentEventSource = null;
    loading.classList.remove('active');
    answerEl.classList.remove('streaming');
    sendBtn.style.display = '';
    stopBtn.classList.remove('active');
    error.textContent = '⚠️ 连接中断或 Agent 处理失败，请稍后重试';
    error.classList.add('active');
  };
}

function escHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function newSession() {
  if (confirm('开始新会话？当前对话历史将被清除。')) {
    sessionId = crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).slice(2, 14);
    localStorage.setItem(SESSION_KEY, sessionId);
    document.getElementById('thinking').innerHTML = '';
    document.getElementById('answer').textContent = '';
    document.getElementById('answerCard').style.display = 'none';
    document.getElementById('msgInput').value = '';
    document.getElementById('msgInput').focus();
    document.getElementById('latency').textContent = '—';
    document.getElementById('iterCount').textContent = '—';
  }
}
</script>
</body>
</html>"""


# ================================================================
# GET / — 搜索页面
# ================================================================

@app.get("/", response_class=HTMLResponse)
def index():
    return SEARCH_PAGE_HTML


@app.get("/agent", response_class=HTMLResponse)
def agent_page():
    return AGENT_PAGE_HTML


# ================================================================
# 请求/响应模型
# ================================================================

class QueryRequest(BaseModel):
    query: str = Field(..., description="用户查询")
    session_id: str = Field(default="", description="会话 ID，空则不保存历史（一问一答）")
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
async def query(req: QueryRequest):
    """检索问答（异步）"""
    query_id = uuid.uuid4().hex[:12]
    start = time.time()

    from src.infra.tracer import tracer
    trace = tracer.start_trace(query_id, req.query)

    shared = {
        "query": req.query,
        "session_id": req.session_id,
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

    raw_dir = settings.raw_dir
    raw_dir.mkdir(parents=True, exist_ok=True)
    file_path = raw_dir / file.filename

    content = await file.read()
    file_path.write_bytes(content)
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
    from src.infra.session_store import session_store
    from src.infra.prompt_manager import prompt_manager

    # --- 阶段 1: 检索 ---
    shared = {"query": query, "session_id": session_id}
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
    if session_id:
        history = session_store.get_recent_history(session_id, limit=6)

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
        if session_id:
            session_store.add_turn(session_id, "user", query)
            session_store.add_turn(session_id, "assistant", answer)

        # 发送结束信号（含 sources）
        yield f"data: {json.dumps({'done': True, 'answer': answer, 'sources': sources, 'session_id': session_id}, ensure_ascii=False)}\n\n"

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
def session_reset(session_id: str):
    """重置 RAG 会话（清除对话历史）"""
    from src.infra.session_store import session_store
    session_store.clear(session_id)
    return {"status": "ok", "session_id": session_id}


@app.get("/session/{session_id}")
def session_detail(session_id: str):
    """查看 RAG 会话历史（调试用）"""
    from src.infra.session_store import session_store
    history = session_store.get_history(session_id, limit=50)
    return {
        "session_id": session_id,
        "total_turns": len(history),
        "history": history,
    }


# ================================================================
# Agent 端点
# ================================================================

@app.post("/agent/chat", response_model=AgentChatResponse)
async def agent_chat(req: AgentChatRequest):
    """Agent 对话端点 — Plan-Execute-Observe 循环（异步版）"""
    import uuid
    session_id = req.session_id or uuid.uuid4().hex[:12]
    start = time.time()

    # 使用 asyncio.to_thread 避免同步 flow 阻塞事件循环
    import asyncio
    loop = asyncio.get_event_loop()

    def _run_sync():
        flow = get_agent_flow()
        shared = {"session_id": session_id, "user_message": req.message}
        try:
            flow.run(shared)
        except Exception as e:
            shared["agent_error"] = str(e)
        return shared

    shared = await loop.run_in_executor(None, _run_sync)

    latency = (time.time() - start) * 1000

    return AgentChatResponse(
        session_id=session_id,
        answer=shared.get("answer", ""),
        tool_calls=shared.get("tool_calls", []),
        iterations=shared.get("iterations", 0),
        latency_ms=round(latency, 1),
        error=shared.get("agent_error", ""),
    )


@app.get("/agent/chat/stream")
async def agent_chat_stream(
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
    import uuid
    import asyncio as _asyncio

    session_id = session_id or uuid.uuid4().hex[:12]

    from src.agent.harness import agent_harness

    async def event_stream():
        yield "retry: 3000\n\n"
        await _asyncio.sleep(0)

        async for event in agent_harness.run_async_stream(session_id, message):
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
    logger.info("  RAG endpoint:        POST /query")
    logger.info("  RAG Stream endpoint: GET  /query/stream")
    logger.info("  Session endpoint:    POST /session/reset  GET /session/{id}")
    logger.info("  Agent endpoint:      POST /agent/chat")
    logger.info("  Agent Stream:        GET  /agent/chat/stream")
    uvicorn.run(app, host="0.0.0.0", port=8000)
