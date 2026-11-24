# 仓库操作规则

- 本仓库是 public repo：README 只做入口；实现文档（使用、API、操作）放 `docs/`。
- 系统设计内容（需求、领域模型、逻辑架构、跨仓职责、架构决策）**不在本仓库维护**，只引用 `nooshare-design` 的 `architecture/backtest-implementation.md` 与 `architecture/adr-0002-product-and-backtest-boundaries.md`。
- 不创建 `decisions/`、`history/` 等过程目录；文档平铺，按需建目录。
- 所有回测必须走 `nooshare_backtest.engine.run` 唯一内核；新增行为先加可手算的 Fixture 测试。
- 结果必须可复现、可序列化；模拟成交假设随结果和报告一起输出。
- 不提交凭据、密钥或含敏感数据的样例。
