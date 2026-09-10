# RAG 评测数据

本目录中的现有问答由 AI 生成，人工基本没有检查。它们是开发候选，不是可信的 golden set，也不能单独支撑模型质量结论。

| 文件 | 样本数 | 用途 | 当前审核状态 |
|---|---:|---|---|
| generated_test.json | 50 | 旧数据迁移后的开发集 | unverified |
| review_queue_v1.json | 3 | 多跳、无答案、对抗覆盖候选 | unverified |
| final_v1.json | 0 | 可用于最终报告和发布门槛的冻结集 | 仅允许 human_verified |
| manifest.json | — | 语料、文件、内容版本和划分目录 | 自动校验 |

旧数据的生成模型、生成时间和精确 Prompt 版本没有留存，因此统一记录为 unknown 或 legacy_unknown。其来源文章由词汇重叠自动回溯，association 为 automated_inference；这能恢复可审查线索，不能替代人工确认。

每条样本保留稳定 ID、问题、答案或拒答预期、任务类型、数据划分、来源文件及 SHA-256、原文证据、生成 provenance、审核状态和内容派生的 dataset_version。manifest.json 还固定了本地 Wikipedia 语料版本与每个数据文件的哈希。

当前开发候选覆盖：

- 事实、实体、数字和时间问题；
- Ada Lovelace 与 Abraham Lincoln 出生年份的跨文档多跳问题；
- 带错误前提、应当拒答的 Ada Lovelace 问题；
- 查询中包含注入指令的 aardvark 对抗问题。

后三条也是 AI 编写的公开语料候选，仍需人工检查措辞、答案、证据和难度。

## 人工审核与 final 晋级

审核者应逐条完成以下操作：

1. 打开所有 source_documents，确认 SHA 未变化，问题可由指定语料回答。
2. 检查 evidence_quotes 是充分证据，答案没有遗漏、歧义或语料外知识。
3. 检查 task_types；无答案样本必须使用 expected_behavior: abstain 和 ground_truth: null。
4. 将通过的样本复制到 final_v1.json，把 split 改为 final，并填写真实的 reviewer 与 reviewed_at，状态改为 human_verified。
5. 重新计算数据和 catalog 版本并运行校验。任何未经人工确认的记录都会被 final 规则拒绝。

自动检查只验证结构、哈希、证据存在性、版本和划分规则。它不会判断问题是否自然、答案是否完整，也不等同于人工审核。

离线校验命令：

    python scripts/validate_eval_dataset.py
    pytest tests/offline/test_eval_datasets.py -q

新增 AI 数据由 scripts/generate_testset.py 写入独立的 generated_candidates.json，默认固定抽样 seed，并记录模型、Prompt 版本、时间、来源和生成上下文。Natural Questions 导入脚本会跳过缺少短答案的记录，不再把问题本身错误地当作答案。
