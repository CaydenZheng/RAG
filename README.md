# RAGFlow — RAG 管线 + Agent 智能体系统

> 以 [PocketFlow](https://github.com/The-Pocket/PocketFlow)（100 行 LLM 框架）为编排引擎，实现混合检索 + Rerank 的 RAG 管线，并进一步升级为具备工具调用、记忆管理和安全防护的 Agent。

---

## 目录

- [1. 项目定位](#1-项目定位)
- [2. Agent 升级](#2-agent-升级)
  - [2.1 Agent Harness 设计](#21-agent-harness-设计)
  - [2.2 Agent 端点](#22-agent-端点)
- [3. 架构总览](#3-架构总览)
- [4. 技术选型与复用策略](#4-技术选型与复用策略)
- [5. 项目结构](#5-项目结构)
- [6. 子任务拆分与开发计划](#6-子任务拆分与开发计划)
- [7. 核心设计细节](#7-核心设计细节)
  - [7.8 Prompt Engineering](#78-prompt-engineering)
  - [7.9 多轮会话服务](#79-多轮会话服务)
  - [7.10 异步编排](#710-异步编排)
  - [7.11 流式响应](#711-流式响应)
- [8. 评估方案](#8-评估方案)
- [9. 工程落地](#9-工程落地)
- [10. 快速开始](#10-快速开始)

---

## 1. 项目定位

**RAG 管线**：用最轻的框架（PocketFlow 100 行），做最扎实的 RAG。

**核心原则**：
- **聚焦 4 层核心**：摄入 → 检索 → 生成 → 评估，每层做深不做宽
- **复用优先**：不重复造轮子，能用成熟开源库的一律复用，手写只在编排逻辑和关键决策点
- **数据说话**：4 组消融实验 + 8 条 Agent Benchmark
- **工程意识**：缓存失效、降级兜底、一致性保证

---

## 2. Agent 升级

在 RAG 管线基础上，进一步升级为具备自主规划能力的 Agent（`src/agent/`），新增 4 个核心模块：

| 模块 | 文件 | 功能 |
|---|---|---|
| **Agent 循环** | `harness.py` | Plan → Execute → Observe 循环，LLM JSON 规划 + 最大 5 轮迭代，支持同步 + **异步流式(SSE)** 两种模式 |
| **Hook 管线** | `hooks.py` | 9 个生命周期事件 + 正则模式匹配，日志/限流/审计/黑名单阻断解耦 |
| **结构化记忆** | `memory.py` | 两层记忆（长期偏好 + 短期历史），三级压缩（截断→硬截断→LLM 摘要） |
| **工具安全** | `tools.py` | 三级审批（白名单/灰名单/黑名单）+ 参数校验 + 30s 去重，内置 **4 个工具**：`search_knowledge_base`、`calculator`、`get_weather`、`search_web` |



| 端点 | 方法 | 功能 |
|---|---|---|
| `/agent` | GET | Agent 对话 Web UI（实时展示思考过程） |
| `/agent/chat` | POST | Agent 对话（Plan-Execute-Observe） |
| `/agent/chat/stream` | GET | Agent 流式对话（SSE，实时推送规划→工具调用→答案） |
| `/agent/reset` | POST | 重置会话记忆 |
| `/agent/memory/{id}` | GET | 查看会话记忆 |

**评测结果**：8 条 Benchmark（规划/安全/记忆/多步推理）通过率 100%，6 项单元测试全部通过。详见 [`AGENT_IMPROVEMENTS.md`](AGENT_IMPROVEMENTS.md)。

**内置工具**：

| 工具 | 功能 | 安全等级 |
|---|---|---|
| `search_knowledge_base` | 检索本地知识库（复用 RAG 管线） | WHITELIST |
| `calculator` | 安全数学计算（受限 eval + 白名单） | WHITELIST |
| `get_weather` | 查询实时天气（wttr.in 免费 API） | WHITELIST |
| `search_web` | 搜索互联网（DDG，优先 duckduckgo_search 库，5s 超时后走 HTML fallback） | GRAYLIST |

### 2.1 Agent Harness 设计

Agent 核心循环控制器（`src/agent/harness.py`）实现了经典的 **Plan → Execute → Observe** 自主推理模式，是 Agent 的"大脑"。

#### 核心循环

```
                     ┌──────────────┐
                     │  用户输入     │
                     └──────┬───────┘
                            ▼
              ┌─────────────────────────┐
              │  1. 加载记忆 + 构建 messages │
              │  (长期偏好 ⊕ 近期历史 ⊕ 当前消息)│
              └────────────┬────────────┘
                           ▼
              ┌─────────────────────────┐
              │  2. LLM Planner (JSON)  │◄──── 最大 5 轮迭代
              │  {action, tool_name,    │
              │   tool_params, reasoning}│
              └────────────┬────────────┘
                           ▼
                   ┌───────┴───────┐
                   │  action 类型？ │
                   └───┬───────┬───┘
               tool_call     final_answer
                   │               │
                   ▼               ▼
    ┌──────────────────────┐  ┌──────────┐
    │ 3a. Hook 管线检查    │  │ 返回答案  │
    │  → 日志/限流/黑名单  │  │ 更新记忆  │
    │  → 阻断则跳过执行    │  └──────────┘
    ├──────────────────────┤
    │ 3b. 工具安全执行     │
    │  → 三级审批          │
    │  → 参数校验          │
    │  → 30s 去重          │
    ├──────────────────────┤
    │ 3c. 结果注入消息列表 │
    └──────────┬───────────┘
               │
               └────→ 回到步骤 2
```

#### Planner Prompt 设计

Planner 的 system prompt 采用极简结构：工具描述（JSON Schema）+ 输出格式约束 + 关键规则：

```
You are an AI Agent. Your ENTIRE response must be a single JSON object.

## Available Tools
[{name, description, params: [{name, type, description, required}], ...}]

## CRITICAL: Response Format
Tool call:  {"action":"tool_call","tool_name":"<name>","tool_params":{...},"reasoning":"<why>"}
Final answer: {"action":"final_answer","answer":"<answer>","reasoning":"<summary>"}

## CRITICAL Rules
1. 事实类问题 → 必须调用 search_knowledge_base，禁止凭自身知识
2. 数学计算 → calculator
3. 天气 → get_weather
4. 实时/当前事件 → search_web
5. 仅闲聊时可跳过工具直接回答
6. 绝不编造信息
7. search_web 结果必须列出标题和完整 URL
8. 引用来源
```

**设计要点**：
- **JSON-only 输出**：严禁 Planner 输出自然语言前缀/后缀，三级 JSON 解析兜底（代码块提取 → 正则匹配 → 全文降级）
- **工具描述 JSON Schema 化**：让 LLM 理解每个工具的参数类型、是否必填、默认值
- **强制检索约束**：规则 1 解决 LLM 过度依赖自身知识而不调用 RAG 的常见问题
- **工具路由**：规则 3-4 确保 Agent 根据问题类型自动选择合适的工具
- **低温决策**：Planner temperature=0.1，保证决策稳定可复现

#### Hook 管线架构

```
Agent 生命周期
  │
  ├── SESSION_START  ──→ LoggingHook（记录所有事件）
  ├── PRE_PLANNING
  ├── POST_PLANNING
  ├── PRE_TOOL_USE   ──→ RateLimitHook（30次/分钟限流, priority=10）
  │                  ──→ BlacklistBlockHook（正则匹配 delete_*/exec/sudo, priority=5）
  │                  ──→ AuditHook（灰名单工具审计, priority=20, pattern 限定）
  ├── POST_TOOL_USE
  ├── PRE_GENERATION
  ├── POST_GENERATION
  ├── SESSION_END
  └── ON_ERROR
```

**核心设计**：
- **优先级排序**：priority 越小越先执行，Blacklist(5) > RateLimit(10) > Audit(20) > Logging(1000)
- **阻断即停**：任一 Hook 设置 `ctx.blocked=True` 后，后续 Hook 不再执行，核心循环跳过该工具
- **正则模式过滤**：AuditHook 配置 `pattern=r"read_file|web_search"`，只对敏感工具做审计，避免审计风暴
- **与核心循环解耦**：Hook 管线是独立模块，新增监控/合规需求无需改动 `harness.py`

#### 工具安全体系

```
三级审批流程：
  ┌──────────────┐
  │ 工具调用请求  │
  └──────┬───────┘
         ▼
  ┌──────────────┐
  │ 1. 参数校验   │ → 类型 + 必填检查，不符合即拒绝
  └──────┬───────┘
         ▼
  ┌──────────────┐     WHITELIST → 自动执行（search_knowledge_base, calculator）
  │ 2. 安全等级   │─── GRAYLIST  → 审计日志 + 执行（read_file, web_search）
  └──────┬───────┘     BLACKLIST → 直接阻断（delete_*, execute_code, rm, sudo）
         ▼
  ┌──────────────┐
  │ 3. 去重检查   │ → 同一 session 30s 内相同调用指纹 → 拒绝
  └──────┬───────┘
         ▼
  ┌──────────────┐
  │ 4. 执行+重试  │
  └──────────────┘
```

### 2.2 Agent 端点

---

## 3. 架构总览

```
┌──────────────────────────────────────────────────────────────┐
│                        用户 / API                            │
└─────────────────────────┬────────────────────────────────────┘
                          │
┌─────────────────────────▼────────────────────────────────────┐
│                     FastAPI 服务层（异步）                     │
│   POST /upload    POST /query    GET /query/stream (SSE)     │
│   GET /agent (UI)  POST /agent/chat  GET /agent/chat/stream │
│   POST /session/reset  GET /session/{id}                     │
└─────────────────────────┬────────────────────────────────────┘
                          │
┌─────────────────────────▼────────────────────────────────────┐
│              PocketFlow AsyncFlow 编排层                       │
│                                                               │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐        │
│  │ Offline Flow │  │ Online Flow  │  │ Retrieval    │        │
│  │ 文档摄入建索引 │  │ 查询→检索→生成│  │ Flow(流式用) │        │
│  └──────────────┘  └──────────────┘  └──────────────┘        │
└─────────────────────────┬────────────────────────────────────┘
                          │
┌─────────────────────────▼────────────────────────────────────┐
│                     基础设施层 (infra)                         │
│  LLM Client (sync+async) · Cache (SQLite) · Tracer           │
│  Prompt Manager (YAML) · Session Store (SQLite) · Fallback   │
└──────────────────────────────────────────────────────────────┘
```

### 在线查询数据流（异步）

```
Query ──→ QueryRewriter ──→ HybridRetriever ──→ Reranker ──→ ContextBuilder ──→ Generator
              │  (async)         (sync)            (sync)        (sync)           (async)
           改写+原query        ┌─────┴─────┐                                  ↓ 流式SSE输出
                          Vector(ChromaDB)  BM25                         [session_id]
                          元数据过滤        分词索引                       ↓ 保存会话历史
```

> **混合编排**：PocketFlow 的 `AsyncFlow` 自动识别节点类型——涉及 LLM 调用的 `Rewrite`/`Generator` 走 `await` 异步路径，检索/重排等毫秒级节点保持同步，无需全部改造。

### 离线索引数据流

```
Raw Docs ──→ DocLoader ──→ DocDeduplicator ──→ Chunker ──→ Embedder ──→ IndexBuilder
  (PDF/MD/TXT)  (markitdown)   (hash去重)     (语义分块)  (OpenAI/bge)  (ChromaDB+BM25)
```

---

## 4. 技术选型与复用策略

| 模块 | 选型 | 复用来源 | 复用程度 |
|---|---|---|---|
| **LLM 编排框架** | PocketFlow | 核心 100 行 `__init__.py` | 100% 复用 |
| **文档解析** | `markitdown` (微软) | pip install | 90% 复用 |
| **文本分块** | `langchain-text-splitters` (仅子包) | 取 splitters，不引入 LangChain | 80% 复用 |
| **向量存储** | `ChromaDB` | 文件型，SQLite 底层，自带持久化+元数据过滤 | 95% 复用 |
| **Embedding** | OpenAI / `sentence-transformers` (bge) | PocketFlow `tool-embeddings` 模式 | 100% 复用 |
| **关键词检索** | `rank-bm25` | 纯 Python 零依赖 | 100% 复用 |
| **中文分词** | `jieba` | pip install | 100% 复用 |
| **Rerank** | `FlagEmbedding` (bge-reranker-base) | CPU 可跑，~200ms/条 | 100% 复用 |
| **LLM 调用** | `litellm` 或 OpenAI SDK | 多 Provider 统一接口 | 90% 复用 |
| **LLM 缓存** | SQLite 自建精确缓存 | 手写轻量实现 | 手写 |
| **追踪监控** | `langfuse` | PocketFlow `tracing/core.py` 100% 复用 | 100% 复用 |
| **评估框架** | `ragas` | 7+ RAG 专用指标 | 90% 复用 |
| **API 服务** | `FastAPI` + `uvicorn` | 标准选型 | 100% 复用 |
| **配置管理** | `pydantic-settings` + `.env` | pip install | 100% 复用 |
| **日志** | `loguru` | 结构化日志 | 100% 复用 |
| **Prompt 管理** | 自建 YAML + Git 版本控制 | 参考 promptfoo 理念 | 手写 |

**核心手写部分**（~15 个 PocketFlow Node + Flow 编排 + 工具封装）：
- 节点编排：`ChunkerNode`, `EmbedderNode`, `HybridRetrieverNode`, `RerankerNode`, `QueryRewriterNode`, `ContextBuilderNode`, `GeneratorNode` 等
- 混合检索融合算法（RRF）
- 容错降级链
- 缓存失效逻辑
- Prompt 模板设计

---

## 5. 项目结构

```
ragrag/
│
├── README.md                     # 本文件
├── requirements.txt              # 依赖清单
├── .env.example                  # 环境变量模板
├── config/
│   └── settings.py               # pydantic-settings 配置类
│
├── prompts/                      # Prompt 版本管理
│   ├── v1/
│   │   ├── query_rewrite.yaml    #   查询改写
│   │   ├── answer_generation.yaml#   答案生成
│   └── v2/                       #   后续迭代版本
│
├── data/                         # 测试数据
│   ├── raw/                      #   原始文档（Wikipedia 文章 ~300 篇）
│   ├── chroma/                   #   ChromaDB 持久化目录 (gitignore)
│   └── testset/                  #   自建评估测试集 (QA pairs)
│       └── generated_test.json   #   LLM 自动生成 50 条 QA
│
├── src/
│   ├── __init__.py
│   │
│   ├── core/                     # PocketFlow 节点库
│   │   ├── __init__.py
│   │   ├── ingestion.py          #   DocLoader, DocDeduplicator, Chunker
│   │   ├── indexing.py           #   Embedder, IndexBuilder (ChromaDB + BM25)
│   │   ├── retrieval.py          #   QueryRewriter, HybridRetriever, Reranker
│   │   ├── generation.py         #   ContextBuilder, Generator
│   │   └── evaluation.py         #   RagasEvaluator（PocketFlow Node）
│   │
│   ├── llm/                      # LLM 调用层
│   │   ├── __init__.py
│   │   ├── client.py             #   统一 LLM 接口 (litellm/OpenAI)
│   │   └── cache.py              #   SQLite 精确缓存（含知识库版本指纹）
│   │
│   ├── infra/                    # 基础设施
│   │   ├── __init__.py
│   │   ├── tracer.py             #   本地 JSON Lines trace 日志
│   │   ├── prompt_manager.py     #   YAML Prompt 加载与管理
│   │   ├── session_store.py      #   SQLite 会话存储（多轮对话历史）
│   │   └── fallback.py           #   降级链（DeepSeek → Ollama → 原文兜底）
│   │
│   ├── agent/                    # Agent 模块
│   │   ├── __init__.py
│   │   ├── harness.py            #   Agent 核心循环（Plan-Execute-Observe）
│   │   ├── hooks.py              #   Hook 事件拦截管线（日志/限流/审计/阻断）
│   │   ├── memory.py             #   两层记忆 + 三级压缩
│   │   └── tools.py              #   工具注册 + 三级安全审批 + 去重
│   │
│   └── utils/                    # 工具函数
│       ├── __init__.py
│       ├── rrf.py                #   RRF 融合纯函数（可单测）
│       ├── token_counter.py      #   tiktoken 精确计数
│       └── bm25_store.py         #   BM25 索引封装（读写同步、冷启动降级）
│
├── flow.py                       # PocketFlow 顶层编排（Offline / Online / Eval）
├── flow_agent.py                 # Agent Flow 编排
├── app.py                        # FastAPI 入口（含 Agent 端点）
│
├── scripts/
│   ├── build_index.py            # 离线索引构建脚本
│   ├── run_eval.py               # 离线评估（消融实验 + RAGAS + 检索指标）
│   ├── download_wiki.py          # Wikipedia 文章下载
│   └── generate_testset.py       # LLM 自动生成测试集
│
├── tests/
│   ├── test_smoke.py             # 冒烟验证
│   ├── test_ragas.py             # RAGAS 导入检查
│   ├── test_rrf.py               # RRF 融合公式单元测试
│   └── test_agent.py             # Agent Benchmark + 单元测试
│
├── test.py                       # 端到端指标验证脚本
├── test2.py                      # tracer 模块验证脚本
```

---

## 6. 子任务拆分与开发计划

### Phase 1：基础设施搭建（Day 1-2）

| 子任务 | 内容 | 依赖 |
|---|---|---|
| 1.1 项目骨架 | 目录结构、`requirements.txt`、`.env`、`config/settings.py` | 无 |
| 1.2 LLM Client | 统一 `chat()` / `embed()` 接口，封装 litellm + retry + timeout | 1.1 |
| 1.3 Prompt Manager | YAML 加载、版本切换、模板渲染 | 1.1 |
| 1.4 Token Counter | tiktoken 封装，支持多种模型 | 1.1 |

### Phase 2：离线索引管线（Day 3-5）

| 子任务 | 内容 | 依赖 |
|---|---|---|
| 2.1 DocLoader | 多格式解析（markitdown 封装），输出统一 `{text, metadata}` | 1.1 |
| 2.2 DocDeduplicator | 内容 MD5 去重，同一文档幂等上传 | 1.1 |
| 2.3 Chunker | 语义分块（Markdown 按标题 / 通用递归字符），overlap、chunk_id | 2.1 |
| 2.4 Embedder | 批量 embedding，支持 OpenAI / bge 双后端 | 1.2 |
| 2.5 IndexBuilder | ChromaDB 写入 + BM25 同步构建，元数据过滤字段 | 2.3, 2.4 |
| 2.6 Offline Flow | 串联 2.1→2.5，PocketFlow 编排，`build_index.py` 脚本 | 2.1-2.5 |

### Phase 3：在线检索管线（Day 6-9）

| 子任务 | 内容 | 依赖 |
|---|---|---|
| 3.1 QueryRewriter | LLM 改写查询，保留原 query 兜底 | 1.2, 1.3 |
| 3.2 BM25 Store | BM25 索引封装，运行时同步增删，冷启动异步重建+自动降级 | 2.5 |
| 3.3 HybridRetriever | 向量检索 + BM25 检索 → RRF 融合，元数据过滤，返回候选集 | 3.2 |
| 3.4 Reranker | bge-reranker 精排，首尾截断适配 max_length，可配置开关 | 3.3 |
| 3.5 ContextBuilder | 按分数降序，逐 chunk 累加 token，达预算停止，完整 chunk 不截断 | 3.4, 1.4 |
| 3.6 Generator | 带引用标记的答案生成，模板来自 Prompt Manager | 1.2, 1.3, 3.5 |
| 3.7 Online Flow | 串联 3.1→3.6，PocketFlow 编排 | 3.1-3.6 |

### Phase 4：基础设施加固（Day 10-11）

| 子任务 | 内容 | 依赖 |
|---|---|---|
| 4.1 LLM Cache | SQLite 精确缓存，key = `hash(model + messages + params + 知识库指纹)` | 1.2 |
| 4.2 Tracer | Langfuse 集成，trace 每次查询的检索/生成全链路 | 1.1 |
| 4.3 Fallback Chain | LLM 重试(3次)→Provider降级→原文兜底，检索降级(BM25→纯向量→报错) | 1.2 |
| 4.4 FastAPI 接口 | `/upload`, `/query`, `/query/stream`, 全局异常处理 | 3.7 |

### Phase 5：评估系统（Day 12-13）

| 子任务 | 内容 | 依赖 |
|---|---|---|
| 5.1 测试集构建 | 自建 50 条 QA pairs（手动标注 golden answers），存入 `data/testset/` | 无 |
| 5.2 Ragas 集成 | 接入 ragas 评估，计算 Faithfulness/Context Precision/Recall/Answer Correctness | 3.7 |
| 5.3 消融实验 | 4 组对照：纯向量 / 纯 BM25 / 混合融合 / 混合+Rerank，输出对比报告 | 5.2 |
| 5.4 Eval Flow | PocketFlow 编排评估流程，`run_eval.py` 一键执行 | 5.1-5.3 |

---

## 7. 核心设计细节

### 7.1 混合检索与 RRF 融合

```
候选集 = VectorRetrieval(query, top_k=20)
       ∪ BM25Retrieval(query, top_k=20)

RRF score(chunk) = Σ 1 / (k + rank_i)    # i ∈ {vector, bm25}, k 可配置(默认60)

融合后取 Top-20 送入 Reranker
```

- RRF 的 k 值在 `config/settings.py` 中可配置，评估脚本中做网格搜索确定最优值
- 元数据过滤（日期/来源/类型）在各自检索阶段前置执行，减少无效计算

### 7.2 查询改写保底策略

```
检索输入 = 原 query ∪ 改写 query₁ ∪ 改写 query₂
         ↓ 各自检索
         ↓ 合并去重
         ↓ RRF 融合 → Rerank
```

- 改写失败时自动跳过，不阻塞主流程
- 查询改写可通过 API 参数 `rewrite=false` 关闭

### 7.3 上下文截断规则

```
1. chunk 按 Rerank 分数降序排列
2. 预留 token 预算给 system prompt + user query（默认占总预算 30%）
3. 从高到低逐个累加完整 chunk 的 token 数
4. 触及预算上限时停止，留 5% 余量防抖动
5. 绝不拆分单个 chunk（保证引用标记完整性）
```

### 7.4 Rerank 长度适配

- bge-reranker-base 最大输入长度：512 tokens
- 超长 chunk 采用**首尾保留截断**：前 256 token + 后 256 token
- 保证 chunk 的首尾关键信息不因硬截断丢失

### 7.5 BM25 一致性保证

```
统一写入入口：DocumentStore.add(doc)
  ├── ChromaDB.add(chunks)
  └── BM25Index.add(chunks)    ← 同步写入，保证一致

统一删除入口：DocumentStore.delete(doc_id)
  ├── ChromaDB.delete(doc_id)
  └── BM25Index.delete(doc_id) ← 同步删除
```

**冷启动流程**：
```
1. 服务启动
2. 同步：加载 ChromaDB（毫秒级）
3. 异步：后台重建 BM25 索引（数秒到数十秒）
4. 重建期间 → 关键词检索自动降级为纯向量检索
5. 重建完成 → 恢复混合检索
```

### 7.6 缓存失效

```
缓存 Key = hash(model + messages + params + knowledge_base_fingerprint)

knowledge_base_fingerprint = hash(all_document_ids + doc_versions)
```

- 任意文档增删 → fingerprint 变化 → 所有旧缓存自动失效
- 不采用语义缓存（GPTCache）——避免答案过时和上下文不匹配的风险

### 7.7 降级链路

```
LLM 调用降级：
  OpenAI → Azure OpenAI → 本地 Ollama → 原文兜底
    ↓         ↓               ↓              ↓
  重试3次   重试3次         重试3次      返回检索原文片段
  (tenacity, 指数退避)                  + 提示语

检索降级：
  混合检索 → 纯向量检索 → 返回错误
     ↓           ↓            ↓
  BM25异常    向量异常     全部不可用
  自动跳过    (罕见)

原文兜底格式：
  "当前生成服务暂时不可用，以下是检索到的最相关内容供参考：\n\n[1] ...\n[2] ..."
```

### 7.8 Prompt Engineering

Prompt 是 LLM 应用中最容易被忽视但影响最大的组件。本项目在 Agent Planner 和 RAG 生成两个关键环节做了精细的 Prompt 设计。

#### 版本管理与 A/B 测试

```
prompts/
├── v1/
│   ├── query_rewrite.yaml      # 查询改写模板
│   └── answer_generation.yaml  # 答案生成模板
└── v2/                         # 后续迭代版本
```

- YAML 文件包含完整配置：`version`, `model`, `temperature`, `max_tokens`, `system`, `user_template`
- 通过 `PROMPT_VERSION=v2` 环境变量一键切换，支持 A/B 对比
- Git 版本控制，每次 Prompt 修改有完整 diff 历史

#### 答案生成 Prompt 设计

```yaml
system: |
  You are a helpful technical documentation assistant.

  Rules:
  1. For factual/knowledge questions, only use information from the given context.
  2. Cite sources using the chunk reference numbers, e.g. [1], [2].
  3. If the context is insufficient, say so clearly.
  4. If the user asks about the conversation itself (e.g., "what did I just ask?"),
     answer from conversation history above — no context needed.
  5. Use conversation history to resolve pronouns ("it", "they") and follow-ups.
  6. Be concise but complete.

user_template: |
  ## Context Snippets
  {context}

  ## Question
  {query}
```

**设计要点**：
- **引用强约束（规则 2）**：每个 chunk 以 `[N]` 标记，LLM 必须输出带编号的引用，这是 Faithfulness > 0.96 的关键
- **诚实性约束（规则 1,3）**：明确要求"不知道就说不知道"，避免幻觉
- **多轮感知（规则 5）**：告诉 LLM 对话历史的存在和用途，使其能理解代词和追问
- **结构化分隔**：`## Context Snippets` / `## Question` 用 Markdown 标题分隔，让 LLM 明确区分检索内容和用户问题

#### Planner Prompt 设计

Agent Planner 的 Prompt 遵循 **JSON-first** 原则：

1. **输出格式绝对约束**：`Your ENTIRE response must be a single JSON object — no text before or after`
2. **双 action 模型**：`tool_call` 和 `final_answer` 两种 action，简单明确
3. **工具描述注入**：将 `ToolRegistry` 的工具列表 JSON Schema 化后注入 prompt，LLM 知道每个工具的参数名、类型、是否必填
4. **反幻觉约束**：`For ANY factual/知识类 question, you MUST call search_knowledge_base. NEVER answer from your own knowledge.`
5. **三级 JSON 解析兜底**：
   - 优先：提取 Markdown 代码块 ` ```json ... ``` `
   - 其次：正则匹配 `{"action":...}` 模式
   - 兜底：全文降级为 `final_answer`，避免崩溃

### 7.9 多轮会话服务

从"一问一答的搜索框"升级为"可持续对话的 RAG 助手"。

#### 存储设计

```sql
-- SQLite 表结构（零额外依赖，与 cache.py 模式一致）
CREATE TABLE sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,     -- 会话标识
    role TEXT NOT NULL,           -- "user" | "assistant"
    content TEXT NOT NULL,        -- 消息内容
    timestamp REAL NOT NULL,
    metadata TEXT DEFAULT '{}'
);
CREATE INDEX idx_session_id ON sessions(session_id);
CREATE INDEX idx_session_time ON sessions(session_id, timestamp);
```

#### 对话历史注入

```
每次查询的 messages 构建流程：

  [system prompt]                    ← 基础指令
  [user: "什么是 RRF"]                ┐
  [assistant: "RRF 是 Reciprocal..."] ├─ 从 SQLite 加载最近 6 条
  [user: "它的 k 值默认是多少"]        ┘  （3 轮对话）
  [user: context + query]            ← 当前问题 + 检索到的文档
```

#### 端点

| 端点 | 方法 | 功能 |
|---|---|---|
| `/query` | POST | 查询（传入 `session_id` 启用多轮记忆） |
| `/query/stream` | GET | 流式查询（同样支持 `session_id`） |
| `/session/reset` | POST | 清除指定会话的历史 |
| `/session/{session_id}` | GET | 查看会话历史（调试用） |

#### 前端集成

- 页面首次加载时自动通过 `localStorage` 生成/恢复 `session_id`
- "新会话"按钮清除 localStorage 并生成新 ID
- 所有 `/query` 请求自动携带 `session_id`，用户无感知

### 7.10 异步编排

将同步阻塞的 RAG 管线升级为异步并发架构。

#### 改造策略：只改造 I/O 密集节点

```
节点异步化判断矩阵：

  QueryRewriterNode    → ✅ AsyncNode（LLM 调用，3-5s）
  HybridRetrieverNode  → ❌ 保持 Node（毫秒级检索）
  RerankerNode         → ❌ 保持 Node（CPU 推理，同步更简单）
  ContextBuilderNode   → ❌ 保持 Node（纯内存操作）
  GeneratorNode        → ✅ AsyncNode（LLM 调用，3-5s）
```

#### PocketFlow AsyncFlow 混合编排

```python
# AsyncFlow 自动识别节点类型，无需全部改造
class AsyncFlow(Flow, AsyncNode):
    async def _orch_async(self, shared, params=None):
        while curr:
            if isinstance(curr, AsyncNode):
                last_action = await curr._run_async(shared)  # 异步
            else:
                last_action = curr._run(shared)               # 同步
            curr = self.get_next_node(curr, last_action)
```

#### 并发收益

```
改造前（同步，单线程排队）：
  请求1 ──→ [检索50ms] ──→ [LLM 5s 阻塞══════════] ──→ 返回
  请求2 ──→ [检索50ms] ──→ [LLM 5s 阻塞══════════] ──→ 返回
  总耗时: 10s+

改造后（异步，事件循环并发）：
  请求1 ──→ [检索] ──→ [LLM 5s══════] ──→ 返回
  请求2 ──→ [检索] ──→ [LLM 5s══════] ──→ 返回
              ↑ 请求2 在请求1 等 LLM 时并发执行检索
  总耗时: ~5.5s
```

#### LLM Client 双模式

```python
class LLMClient:
    # 同步（兼容 Agent harness 等旧代码）
    def chat(self, messages, ...) -> str: ...

    # 异步（供 AsyncNode 使用）
    async def chat_async(self, messages, ...) -> str: ...

    # 异步流式（供 SSE 端点使用）
    async def chat_stream_async(self, messages, ...) -> AsyncGenerator[str]: ...
```

### 7.11 流式响应

基于 SSE（Server-Sent Events）的 Token 级流式输出，实现 ChatGPT 式的逐字显示体验。

#### 两阶段架构

```
GET /query/stream?query=什么是RRF&session_id=abc

  ┌─ 阶段 1: 检索（~50ms，异步）─────────────────────┐
  │ RetrievalFlow.run_async(shared)                  │
  │   Rewriter(async) → Hybrid(sync) → Rerank(sync)  │
  │   → ContextBuilder(sync)                        │
  └─→ shared["context"] + shared["sources"]          │
                                                     │
  ┌─ 阶段 2: 流式生成（逐 token SSE）─────────────────┐
  │ async for chunk in llm_client.chat_stream_async():│
  │   yield f"data: {{\"chunk\": \"Reciprocal\"}}\n\n"│
  │   yield f"data: {{\"chunk\": \" Rank\"}}\n\n"     │
  │   ...                                            │
  │   yield f"data: {{\"done\": true, \"sources\": [...]}}\n\n"│
  └─ 保存 session ──────────────────────────────────┘
```

#### 为什么分两阶段

- 检索阶段必须完整执行才能获得 context（无法流式化）
- 生成阶段天然适合流式（LLM 逐 token 输出）
- 分两阶段避免检索失败时已经开始流式输出的尴尬

#### SSE 响应格式

```
data: {"chunk": "RRF"}
data: {"chunk": "（Reciprocal"}
data: {"chunk": " Rank"}
data: {"chunk": " Fusion"}
...
data: {"done": true, "answer": "完整答案文本", "sources": [...], "session_id": "abc123"}
```

#### 前端消费

```javascript
// 标准 EventSource API，零依赖
const evtSource = new EventSource('/query/stream?query=什么是RRF');
evtSource.onmessage = (e) => {
  const data = JSON.parse(e.data);
  if (data.chunk) answerEl.textContent += data.chunk;  // 逐字追加
  if (data.done) {
    evtSource.close();
    renderSources(data.sources);  // 渲染引用
  }
};
```

---

## 8. 评估方案

### 8.1 评估指标（双轨制）

**检索层**（纯规则计算，秒出，不依赖 LLM）：

| 指标 | 含义 | 计算方式 |
|---|---|---|
| Hit Rate@5 / @10 | top-K chunk 是否命中 ground truth 关键词 | token 重叠率 ≥ 30% |
| MRR | 第一个相关 chunk 排名的倒数均值 | 1 / rank_first_relevant |

**生成层**（RAGAS LLM judge，DeepSeek 评判）：

| 指标 | 含义 |
|---|---|
| Context Precision | 检索到的文档中，相关文档的排位是否靠前 |
| Context Recall | 检索到的文档是否覆盖了答案所需的全部信息 |
| Faithfulness | 生成的答案是否完全基于提供的上下文（不编造） |
| Answer Relevancy | 生成的答案是否与问题相关 |

### 8.2 消融实验结果（50 条 Wikipedia QA）

| 实验组 | Context Precision | Context Recall | Faithfulness | Answer Relevancy | MRR |
|---|---|---|---|---|---|
| A. 纯向量 | 0.731 | 0.830 | 0.967 | 0.803 | — |
| B. 纯 BM25 | 0.743 | 0.813 | 0.971 | 0.806 | — |
| C. 混合融合 (RRF) | 0.823 | **0.921** | 0.983 | 0.850 | — |
| **D. 混合 + Rerank** | **0.856** | **0.921** | 0.961 | **0.866** | — |

**关键结论**：
- RRF 融合是召回覆盖率的决定性一跳：Context Recall 从 0.83 跳到 0.92（+11pp）
- Reranker 让相关文档排得更前：Context Precision 从 0.82 提升到 0.86（+4pp）
- 所有组 Faithfulness > 0.96，引用机制有效抑制幻觉

### 8.3 消融实验（4 组对照）

| 实验组 | 配置 |
|---|---|
| A. 纯向量 | 仅 ChromaDB 向量检索 → 生成 |
| B. 纯 BM25 | 仅关键词检索 → 生成 |
| C. 混合融合 | 向量 + BM25 → RRF 融合 → 生成 |
| D. 混合 + Rerank（最终方案） | C + bge-reranker 精排 → 生成 |

### 8.4 运行评估

```bash
# 构建索引
python scripts/build_index.py

# 运行消融实验（50 题 × 4 组，包含检索指标 + RAGAS，约 15 分钟）
python scripts/run_eval.py --testset data/testset/generated_test.json
```

---

## 9. 工程落地

### 9.1 全链路 Trace（本地 JSON Lines）

`src/infra/tracer.py` 实现轻量级 JSON Lines trace，不依赖外部服务：
- 每次查询写入 `logs/traces.jsonl` 一条 record
- 记录：query_id、总耗时、各节点指标（variants/candidates/kept/answer_chars）
- 异常时同样写入（含 error 字段），支持故障回溯

### 9.2 配置管理

```bash
# .env.example
OPENAI_API_KEY=sk-xxx
OPENAI_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-chat
LOCAL_EMBEDDING_MODEL=BAAI/bge-base-en-v1.5
RERANK_MODEL=BAAI/bge-reranker-base

OLLAMA_BASE_URL=http://localhost:11434        # 可选：本地降级

CHROMA_PERSIST_DIR=./data/chroma
CACHE_DB_PATH=./data/cache.db
PROMPT_VERSION=v1

# RRF 参数
RRF_K=60
VECTOR_TOP_K=20
BM25_TOP_K=20
RERANK_TOP_K=5

# Token 预算
MAX_CONTEXT_TOKENS=4096
SYSTEM_RESERVE_RATIO=0.30
CONTEXT_BUFFER_RATIO=0.05
```

### 9.3 Prompt 版本管理

```yaml
# prompts/v1/answer_generation.yaml
version: "1.0"
model: "gpt-4o-mini"
temperature: 0.3
max_tokens: 1024
system: |
  你是一个专业的文档检索助手。请严格基于提供的上下文回答问题。
  每个上下文片段以 [N] 标记来源，回答时请标注引用编号。
  如果上下文不足以回答问题，请明确说明。
user_template: |
  ## 上下文
  {context}

  ## 问题
  {query}

  ## 要求
  - 回答中标注引用来源，如 [1]、[2]
  - 不要编造上下文中没有的信息
```

运行时通过 `PROMPT_VERSION` 环境变量切换版本，支持 A/B 对比。

### 9.4 日志

使用 `loguru`，结构化日志输出：
```python
logger.info("Retrieval completed", extra={
    "query_id": "abc123",
    "vector_hits": 20,
    "bm25_hits": 20,
    "fused_hits": 20,
    "rerank_hits": 5,
    "latency_ms": 320
})
```

---

## 10. 快速开始

### 环境准备

```bash
# 克隆项目
git clone <repo-url>
cd ragrag

# 安装依赖
pip install -r requirements.txt

# 配置环境变量
cp .env.example .env
# 编辑 .env 填入你的 API Key

# 准备测试文档（放入 data/raw/）
cp /path/to/your/docs/*.pdf ./data/raw/
```

### 构建索引

```bash
python scripts/build_index.py --data-dir ./data/raw
# 输出: ✅ Indexed 156 chunks from 12 documents
```

### 启动服务

```bash
python app.py
# FastAPI 运行在 http://localhost:8000
#   RAG 端点:        POST /query
#   流式端点:        GET  /query/stream
#   会话端点:        POST /session/reset  GET /session/{id}
#   Agent 端点:      POST /agent/chat
#   Agent 流式:      GET  /agent/chat/stream
#   Agent UI:        GET  /agent
```

### API 调用

```bash
# 普通查询（一问一答）
curl -X POST http://localhost:8000/query \
  -H "Content-Type: application/json" \
  -d '{"query": "PocketFlow 的 Node lifecycle 是什么？"}'

# 响应
{
  "query_id": "a1b2c3d4e5f6",
  "answer": "PocketFlow 的 Node 生命周期包括三个阶段：prep、exec、post... [1][2]",
  "sources": [
    {"chunk_id": "doc1_chunk3", "text": "...", "score": 0.94, "ref": 1},
    {"chunk_id": "doc1_chunk5", "text": "...", "score": 0.87, "ref": 2}
  ],
  "latency_ms": 3421.5
}

# 多轮对话（传入 session_id）
curl -X POST http://localhost:8000/query \
  -H "Content-Type: application/json" \
  -d '{"query": "什么是 RRF？", "session_id": "my-session"}'

curl -X POST http://localhost:8000/query \
  -H "Content-Type: application/json" \
  -d '{"query": "它的 k 值默认是多少？", "session_id": "my-session"}'
# LLM 会从历史中知道"它"指的是 RRF

# 流式查询（SSE，逐 token 输出）
curl -N http://localhost:8000/query/stream?query=什么是RRF\&session_id=my-session
# 输出（每行实时到达）：
#   data: {"chunk":"RRF"}
#   data: {"chunk":"（Reciprocal"}
#   data: {"chunk":" Rank"}
#   ...
#   data: {"done":true,"answer":"完整文本...","sources":[...]}

# 查看会话历史
curl http://localhost:8000/session/my-session

# 清除会话
curl -X POST "http://localhost:8000/session/reset?session_id=my-session"
```

### Agent API

```bash
# Agent 对话（自主规划 + 工具调用）
curl -X POST http://localhost:8000/agent/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "What is the half-life of actinium?"}'

# Agent 流式对话（SSE，实时推送思考过程）
curl -N "http://localhost:8000/agent/chat/stream?message=search%20web%20for%20latest%20AI%20news&session_id=my-agent"
# 输出:
#   data: {"step":"planning","iteration":1}
#   data: {"step":"tool_call","tool":"search_web","params":{...}}
#   data: {"step":"tool_done","tool":"search_web","success":true}
#   data: {"chunk":"Here"}
#   data: {"chunk":" are"}
#   ...
#   data: {"done":true,"answer":"...","iterations":2}

# 天气查询
curl -N "http://localhost:8000/agent/chat/stream?message=what's%20the%20weather%20in%20Tokyo&session_id=w1"

# 多步推理
curl -X POST http://localhost:8000/agent/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "查一下 actinium 半衰期，然后算出对应多少天"}'

# Web UI（可视化思考过程）
# 浏览器访问 http://localhost:8000/agent

# 查看会话记忆
curl http://localhost:8000/agent/memory/abc123

# 重置会话
curl -X POST "http://localhost:8000/agent/reset?session_id=abc123"
```

### 运行评估

```bash
# RAG 消融实验
python scripts/run_eval.py --testset ./data/testset/ground_truth.json --ablation

# Agent Benchmark + 单元测试
python tests/test_agent.py
python tests/test_agent.py --unit-only
```

---

## License

MIT
