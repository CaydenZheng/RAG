# RAGFlow — 基于 PocketFlow 的高级 RAG 系统

> 以 [PocketFlow](https://github.com/The-Pocket/PocketFlow)（100 行 LLM 框架）为编排引擎，实现混合检索 + Rerank 的 RAG 管线，覆盖从文档摄入到答案评估的完整链路。

---

## 目录

- [1. 项目定位](#1-项目定位)
- [2. 架构总览](#2-架构总览)
- [3. 技术选型与复用策略](#3-技术选型与复用策略)
- [4. 项目结构](#4-项目结构)
- [5. 子任务拆分与开发计划](#5-子任务拆分与开发计划)
- [6. 核心设计细节](#6-核心设计细节)
- [7. 评估方案](#7-评估方案)
- [8. 工程落地](#8-工程落地)
- [9. 快速开始](#9-快速开始)

---

## 1. 项目定位

**一句话**：用最轻的框架（PocketFlow 100 行），做最扎实的 RAG。

**核心原则**：
- **聚焦 4 层核心**：摄入 → 检索 → 生成 → 评估，每层做深不做宽
- **复用优先**：不重复造轮子，能用成熟开源库的一律复用，手写只在编排逻辑和关键决策点
- **数据说话**：4 组消融实验证明每一步优化的价值
- **工程意识**：缓存失效、降级兜底、一致性保证

**明确不做**：多租户、分布式部署、流式缓存、A/B 平台

---

## 2. 架构总览

```
┌─────────────────────────────────────────────────────────┐
│                      用户 / API                          │
└────────────────────────┬────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────┐
│                    FastAPI 服务层                         │
│   POST /upload    POST /query    GET /query/stream       │
└────────────────────────┬────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────┐
│                PocketFlow 编排层 (flow.py)                 │
│                                                          │
│  ┌──────────────┐    ┌──────────────┐    ┌────────────┐ │
│  │ Offline Flow │    │ Online Flow  │    │ Eval Flow  │ │
│  │ 文档摄入建索引 │    │ 查询检索生成   │    │ 离线评估    │ │
│  └──────────────┘    └──────────────┘    └────────────┘ │
└────────────────────────┬────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────┐
│                   基础设施层 (infra)                       │
│  LLM Client · Cache · Tracer(Langfuse) · Prompt Manager │
└─────────────────────────────────────────────────────────┘
```

### 在线查询数据流

```
Query ──→ QueryRewriter ──→ HybridRetriever ──→ Reranker ──→ ContextBuilder ──→ Generator
              │               ┌─────┴─────┐
           改写+原query        │            │
                          Vector(ChromaDB)  BM25
                          元数据过滤        分词索引
```

### 离线索引数据流

```
Raw Docs ──→ DocLoader ──→ DocDeduplicator ──→ Chunker ──→ Embedder ──→ IndexBuilder
  (PDF/MD/TXT)  (markitdown)   (hash去重)     (语义分块)  (OpenAI/bge)  (ChromaDB+BM25)
```

---

## 3. 技术选型与复用策略

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

## 4. 项目结构

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
│   │   └── fallback.py           #   降级链（DeepSeek → Ollama → 原文兜底）
│   │
│   └── utils/                    # 工具函数
│       ├── __init__.py
│       ├── rrf.py                #   RRF 融合纯函数（可单测）
│       ├── token_counter.py      #   tiktoken 精确计数
│       └── bm25_store.py         #   BM25 索引封装（读写同步、冷启动降级）
│
├── flow.py                       # PocketFlow 顶层编排（Offline / Online / Eval）
├── app.py                        # FastAPI 入口
│
├── scripts/
│   ├── build_index.py            # 离线索引构建脚本
│   ├── run_eval.py               # 离线评估（消融实验 + RAGAS + 检索指标）
│   ├── download_wiki.py          # Wikipedia 文章下载
│   └── generate_testset.py       # LLM 自动生成测试集
│
├── tests/
│   ├── test_smoke.py             # 冒烟验证（LLM/Embedding/Token计数）
│   ├── test_ragas.py             # RAGAS 导入检查
│   └── test_rrf.py               # RRF 融合公式单元测试（7 用例）
│
├── test.py                       # 端到端指标验证脚本
├── test2.py                      # tracer 模块验证脚本
```

---

## 5. 子任务拆分与开发计划

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

## 6. 核心设计细节

### 6.1 混合检索与 RRF 融合

```
候选集 = VectorRetrieval(query, top_k=20)
       ∪ BM25Retrieval(query, top_k=20)

RRF score(chunk) = Σ 1 / (k + rank_i)    # i ∈ {vector, bm25}, k 可配置(默认60)

融合后取 Top-20 送入 Reranker
```

- RRF 的 k 值在 `config/settings.py` 中可配置，评估脚本中做网格搜索确定最优值
- 元数据过滤（日期/来源/类型）在各自检索阶段前置执行，减少无效计算

### 6.2 查询改写保底策略

```
检索输入 = 原 query ∪ 改写 query₁ ∪ 改写 query₂
         ↓ 各自检索
         ↓ 合并去重
         ↓ RRF 融合 → Rerank
```

- 改写失败时自动跳过，不阻塞主流程
- 查询改写可通过 API 参数 `rewrite=false` 关闭

### 6.3 上下文截断规则

```
1. chunk 按 Rerank 分数降序排列
2. 预留 token 预算给 system prompt + user query（默认占总预算 30%）
3. 从高到低逐个累加完整 chunk 的 token 数
4. 触及预算上限时停止，留 5% 余量防抖动
5. 绝不拆分单个 chunk（保证引用标记完整性）
```

### 6.4 Rerank 长度适配

- bge-reranker-base 最大输入长度：512 tokens
- 超长 chunk 采用**首尾保留截断**：前 256 token + 后 256 token
- 保证 chunk 的首尾关键信息不因硬截断丢失

### 6.5 BM25 一致性保证

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

### 6.6 缓存失效

```
缓存 Key = hash(model + messages + params + knowledge_base_fingerprint)

knowledge_base_fingerprint = hash(all_document_ids + doc_versions)
```

- 任意文档增删 → fingerprint 变化 → 所有旧缓存自动失效
- 不采用语义缓存（GPTCache）——避免答案过时和上下文不匹配的风险

### 6.7 降级链路

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

---

## 7. 评估方案

### 7.1 评估指标（双轨制）

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

### 7.2 消融实验结果（50 条 Wikipedia QA）

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

### 7.3 消融实验（4 组对照）

| 实验组 | 配置 |
|---|---|
| A. 纯向量 | 仅 ChromaDB 向量检索 → 生成 |
| B. 纯 BM25 | 仅关键词检索 → 生成 |
| C. 混合融合 | 向量 + BM25 → RRF 融合 → 生成 |
| D. 混合 + Rerank（最终方案） | C + bge-reranker 精排 → 生成 |

### 7.4 运行评估

```bash
# 构建索引
python scripts/build_index.py

# 运行消融实验（50 题 × 4 组，包含检索指标 + RAGAS，约 15 分钟）
python scripts/run_eval.py --testset data/testset/generated_test.json
```

---

## 8. 工程落地

### 8.1 全链路 Trace（本地 JSON Lines）

`src/infra/tracer.py` 实现轻量级 JSON Lines trace，不依赖外部服务：
- 每次查询写入 `logs/traces.jsonl` 一条 record
- 记录：query_id、总耗时、各节点指标（variants/candidates/kept/answer_chars）
- 异常时同样写入（含 error 字段），支持故障回溯

### 8.2 配置管理

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

### 8.3 Prompt 版本管理

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

### 8.4 日志

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

## 9. 快速开始

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
```

### API 调用

```bash
# 查询
curl -X POST http://localhost:8000/query \
  -H "Content-Type: application/json" \
  -d '{"query": "PocketFlow 的 Node  lifecycle 是什么？", "top_k": 5}'

# 响应
{
  "answer": "PocketFlow 的 Node 生命周期包括三个阶段：... [1][2]",
  "sources": [
    {"chunk_id": "doc1_chunk3", "text": "...", "score": 0.94},
    {"chunk_id": "doc1_chunk5", "text": "...", "score": 0.87}
  ],
  "trace_id": "langfuse-trace-xxx"
}
```

### 运行评估

```bash
python scripts/run_eval.py --testset ./data/testset/ground_truth.json --ablation
```

---

## License

MIT
