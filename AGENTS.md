# 仓库操作规则

## 公共仓库文档禁令（硬约束）

本仓库是 public repo，以下规则优先于任何“按需写文档”的冲动：

1. **禁止文档目录**：不得新建或保留 `docs/`、`doc/`、`documentation/`、`deliverables/`、`reports/`、`notes/`、`decisions/`、`history/` 等文档或过程目录。
2. **禁止生成文字材料**：使用说明、执行模型、API 文档、报告、评审记录、笔记等文字文件（`.md` / `.rst` / `.pdf` / `.docx` 等）不得在本仓库生成或提交。机器验证的临时输出只能写入被忽略的 `.runtime/`，不作为文档维护。
3. **README.md 只做入口**：仅保留标题与一句话定位。`AGENTS.md` 只放规则与路由。
4. **文字材料唯一归属**：所有文字材料必须维护在私有仓库 `nooshare-design` 的 `implementation-docs/nooshare-backtest/` 下；本仓库只引用，不复制。
5. **违规处理**：发现本仓库出现上述目录或文字材料，不就地修订——先迁移到 nooshare-design，再删除本地文件，并更新下方索引表。

### 文字材料索引（nooshare-design）

| 材料 | 权威位置（nooshare-design） |
|------|------------------------------|
| 执行模型 | `implementation-docs/nooshare-backtest/execution-model.md` |
| 使用说明 | `implementation-docs/nooshare-backtest/usage.md` |
| README 原文归档 | `implementation-docs/nooshare-backtest/README-full.md` |
| 系统设计（需求、领域模型、逻辑架构、跨仓职责、架构决策） | `architecture/backtest-implementation.md`、`architecture/adr-0002-product-and-backtest-boundaries.md` |
| 产品与回测边界 | `architecture/adr-0002-product-and-backtest-boundaries.md`、`architecture/matrix.md` |

通过 `workspace.yaml` 定位 nooshare-design 的 checkout 路径后读取上述文件。

## 实现规则

- 所有回测必须走 `nooshare_backtest.engine.run` 唯一内核；新增行为先加可手算的 Fixture 测试。
- 结果必须可复现、可序列化；模拟成交假设随结果和报告一起输出。
- 不提交凭据、密钥或含敏感数据的样例。
