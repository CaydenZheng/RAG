# ContextBuilder 同步执行评估

- 评估日期：2026-09-15
- 代码基线：`f2ed78f`
- 评估工具：`scripts/assess_context_builder.py`
- 输入边界：只使用固定合成文本，不读取或输出生产会话内容

## 结论

暂缓将 `ContextBuilderNode` 改为 `AsyncNode`，也暂缓把历史读取或 Token
计数移交线程池。当前实现虽然在结构上会占用异步 RAG 的事件循环线程，
但三次本机测量中，默认检索场景的事件循环停顿 P95 为
`0.890–0.988 ms`，最大支持候选数场景为 `1.447–1.972 ms`，均低于本次
使用的 `10 ms` 本地筛查阈值。

`10 ms` 仅用于发现是否存在明显的本地事件循环阻塞，不是生产 SLO。
本次没有生产并发与 SQLite 锁竞争数据，因此结论只适用于当前受控负载，
不能解释为所有部署环境都不会阻塞。

## 测量结果

每次运行包含 200 次同步总耗时测量和 200 次事件循环探针测量；共重复三次。
历史中预存 20 条消息，按当前实现读取最近 6 条。文本长度固定为 512 字符。

| 场景与指标 | P50 范围 | P95 范围 |
| --- | ---: | ---: |
| 默认检索（5 个候选）：历史读取 | 0.323–0.331 ms | 0.458–0.481 ms |
| 默认检索（5 个候选）：整轮 Token 计数 | 0.295–0.302 ms | 0.362–0.403 ms |
| 默认检索（5 个候选）：ContextBuilder 总耗时 | 0.652–0.693 ms | 0.849–0.906 ms |
| 默认检索（5 个候选）：事件循环停顿 | 0.674–0.680 ms | 0.890–0.988 ms |
| 最大检索（20 个候选）：历史读取 | 0.327–0.346 ms | 0.493–0.579 ms |
| 最大检索（20 个候选）：整轮 Token 计数 | 0.720–0.762 ms | 0.865–1.039 ms |
| 最大检索（20 个候选）：ContextBuilder 总耗时 | 1.145–1.189 ms | 1.477–1.702 ms |
| 最大检索（20 个候选）：事件循环停顿 | 1.137–1.236 ms | 1.447–1.972 ms |

最大候选数下 Token 计数成本按预期增加，但其 P95 仍约为 1 ms；SQLite
历史读取 P95 始终低于 0.6 ms。当前数据不支持为这两部分引入线程切换成本和
更复杂的异步节点生命周期。

## 方法

评估工具通过真实 `ContextBuilderNode.exec()`、真实 `SessionStore` SQLite
查询和真实 `tiktoken` 编码执行合成负载。它在同步调用前用
`loop.call_soon()` 安排一个已就绪回调，该回调实际得到执行前的等待时间即为
事件循环停顿。离线测试另注入 20 ms 历史读取延迟，验证该探针能够稳定检出
同步阻塞，而不只是记录一个永远通过的计时值。

## 不改变

- 不修改在线 RAG 编排、ContextBuilder 预算与上下文选择行为；
- 不把毫秒级 SQLite 查询或 Token 计数包装进线程池；
- 不增加常驻性能日志或记录用户历史正文；
- 不把本机筛查阈值写入生产配置。

## 复现

```powershell
$env:ADMIN_API_KEY = 'local-assessment-only'
$env:OPENAI_API_KEY = 'offline-assessment'
$env:HF_HUB_OFFLINE = '1'
$env:TRANSFORMERS_OFFLINE = '1'
$env:PYTHONHASHSEED = '0'
uv run --no-sync --offline --no-env-file python scripts/assess_context_builder.py --iterations 200
```

如需保存机器可读结果，可追加
`--output .test-tmp/context-builder-assessment.json`。评估命令不会启动服务或加载
模型权重。

## 重新评估条件

当生产观测显示事件循环停顿超过实际 SLO、会话数据库出现锁竞争，或历史与
候选规模上限提高时，应在对应部署环境重跑该工具。只有届时 P95 达到明确的
部署阈值，才将同步 SQLite 读取与必要 CPU 工作迁移到线程池，并重新验证并发
请求公平性；不要仅因节点位于 `AsyncFlow` 中就进行形式化异步改造。
