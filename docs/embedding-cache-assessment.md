# Embedding 缓存评估

- 评估日期：2026-09-15
- 代码基线：`2173f11`
- 评估工具：`scripts/assess_embedding_cache.py`
- 数据边界：只输出聚合统计，不记录或输出 query、文档正文

## 结论

暂缓实施 query Embedding 缓存，也暂缓复用 legacy 文档向量。本次只增加可重复执行的评估工具，没有增加缓存层或缓存接口。

query 缓存缺少完整的生产 Embedding 调用记录支持。即使存在 `sessions.db`，其中的用户轮次也会被 Agent 共用和历史压缩，只能作为不完整代理；评测报告中的重复率只能说明开发评测 workload，二者都不能替代真实命中率观测。 本次不设置任意百分比阈值；后续只有同时具备完整调用命中率和部署侧 CPU／SLO 价值阈值，才重新考虑实施。legacy 文档文本虽然与当前分块完全重合，但旧 collection 缺少模型 revision、归一化方式和前处理版本，无法证明向量与当前运行配置兼容。

## 聚合结果

| 测量项 | 结果 | 判断 |
| --- | ---: | --- |
| 生产 query | `data/sessions.db` 不存在 | 不可测量，不实施 query 缓存 |
| development 评测 Embedding 输入 | 61 次，47 个唯一值，14 次重复（22.95%） | 热调用估算仅可少算约 0.89 秒，且仅作开发 workload 参考 |
| 当前语料 | 300 篇文档，25,663 个分块 | 本地 wiki 语料 |
| 当前语料内部重复 | 243 个重复分块（0.95%） | 热调用估算单次重建仅节省约 15.4 秒 |
| legacy 文本重合 | 25,420 / 25,420 个唯一分块（100%） | 理论约 26.9 分钟热调用，但 provenance 不合格，安全收益为 0 |
| 安全可复用 legacy 向量 | 0 | 缺少兼容 provenance |
| 本地 Embedding 成本 | 无按次 API 价格；完整重建约 27.1 分钟热调用 | CPU 时间未折算为货币成本 |
| 合成文本冷启动 | 810.9 ms | 单次本机测量 |
| 合成文本热调用 | 均值 63.4 ms，P95 64.0 ms（3 次） | 单次本机测量 |

计算时间按 63.390 ms 的单文本热调用均值线性估算，只用于量级判断；它不等同于批处理吞吐、CPU 货币价格或生产 SLO 价值。

## 向量身份要求

后续若单独实施内容寻址的文档向量复用，缓存 key 必须同时绑定：

- Embedding 模型与明确 revision；
- 归一化方式；
- 文本前处理／分块版本；
- 文本内容 SHA-256；
- 向量维度。

当前运行配置没有固定模型 revision，也没有把归一化行为固化为可比较的版本身份；legacy collection 只记录了余弦空间和 768 维，未记录模型、revision、归一化与前处理版本，因此不能安全复用。

## 不改变

现有 LLM 精确缓存不在本项修改范围内。它仍由 `CACHE_MAX_ENTRIES` 控制容量，并通过已有的 `cache_hit` 追踪字段提供命中观测；本次没有用温度参数等理由移除它，也没有新增其他缓存层。

## 复现

默认测量不会加载模型：

```powershell
$env:ADMIN_API_KEY = 'local-assessment-only'
uv run --no-sync --offline --no-env-file python scripts/assess_embedding_cache.py
```

可选的小规模延迟基准只使用固定合成文本，不使用历史 query 或文档正文：

```powershell
$env:ADMIN_API_KEY = 'local-assessment-only'
$env:HF_HUB_OFFLINE = '1'
$env:TRANSFORMERS_OFFLINE = '1'
$env:OMP_NUM_THREADS = '1'
$env:MKL_NUM_THREADS = '1'
$env:TOKENIZERS_PARALLELISM = 'false'
uv run --no-sync --offline --no-env-file python scripts/assess_embedding_cache.py --benchmark --benchmark-iterations 3
```

这里的占位管理密钥只用于满足现有全局配置校验，不会写入 `.env`，评估命令也不会启动 HTTP 服务。

如需重新决策，应先在实际 `embed_single` 调用边界积累一段只含聚合计数的生产观测；只有真实命中率和节省的本地计算足够时再实施 query 缓存。文档向量复用应在模型 revision 和完整构建身份可追溯后作为独立改动评估。
