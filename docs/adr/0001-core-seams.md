# ADR 0001：核心接口随真实实现逐步收拢

- 状态：Accepted
- 日期：2026-09-10

## 背景

项目仍由 PocketFlow 的 shared 字典连接部分节点，但查询入口、Agent、评测和索引需要共享稳定的领域语义。一次性预先定义 Query、Evidence、IndexJobs、AgentRuntime 和所有事件，会产生只有旧实现与测试替身的薄封装，也会让后续索引任务和 Agent 状态再次修改接口。

## 决策

核心模块采用小接口和兼容 Adapter，按真实变化点逐步迁移：

1. `KnowledgeSystem.retrieve` 是检索接口，隐藏改写、Dense／BM25、RRF、Rerank 和降级。`KnowledgeRetrievalNode` 只负责把结果写回 PocketFlow shared；HTTP、Agent 和评测不重新实现检索。
2. `AnswerInput` 与 `AnswerService` 是生成接口，普通和流式输出共用输入、引用约束和落盘语义；SSE 只承担传输编码。
3. `IndexVersion` 是索引生命周期的第一个领域类型，包含内容、来源和构建 manifest。索引发布使用版本化 collection 和原子 active 指针，PocketFlow 只调用构建模块。
4. `IndexJobs` 在第 25 项出现真实的 submit/status 与后台实现时定义；`AgentRuntime` 在第 28–29 项统一同步、异步和流式执行时定义。此前不增加只转发调用的空接口。
5. RequestContext、Evidence／EvidencePack、Citation、Answer 和事件类型只在能替换现有字典并被至少两个调用方复用时引入。迁移期间 Adapter 是 shared 字典的唯一转换位置。

## 结果

调用方只学习对应核心接口，PocketFlow 类型不会进入核心领域模型。测试通过同一接口注入 Fake；不测试 Adapter 后面的私有实现。每个后续交付组可以替换旧实现而不修改 HTTP、Agent 或评测调用方，同时避免为了形式一次性重写全部链路。
