# V3.1 增量任务队列

原始 34 卡保持在 v3-upgrade/，不重写原 TODO 与历史。新卡映射表示采用对应契约，不表示原卡整体完成。

| 任务 | 内容 | 状态 | 工程门禁 | live 门禁 |
|---|---|---|---|---|
| V31-R0-01 | 独立仓库、V2 基线与架构边界 | DONE | PASS | NOT_APPLICABLE |
| V31-R0-02 | 锁定环境、PG/Redis、MCP 适配与验收框架 | DONE | PASS | NOT_APPLICABLE |
| V31-R0-03 | 事件、授权、工作项、执行与动作持久化契约 | DONE | PASS | NOT_APPLICABLE |
| V31-R1-01 | 确定性调查、Verifier、审批与统一出口工程闭环 | DONE | PASS | NOT_RUN |
| V31-R1-BUDGET-02 | 候选最多 3 次调用、累计预算 USD 2 | DONE | PASS | NOT_RUN |
| V31-R1-LIVE | V3 Issue #1 真实模型、审批与结果评论验收 | BLOCKED | NOT_APPLICABLE | NOT_RUN |
| V31-R2 | Mailbox 与显式 intake/replan | TODO | NOT_RUN | NOT_RUN |
| V31-R3 | 受限 Router LLM 与多任务 | TODO | NOT_RUN | NOT_RUN |
| V31-R4 | RAG 与多专家 | TODO | NOT_RUN | NOT_RUN |
| V31-R5 | 受限修复与 Draft PR | TODO | NOT_RUN | NOT_RUN |
| V31-R6 | Watchdog、主动性与发布评测 | TODO | NOT_RUN | NOT_RUN |

原实施授权与验收记录保留。2026-11-02 用户将候选上限调整为最多 3 次调用、累计 USD 2；本次仅准备配置，不发起真实模型调用或外部发布。

R1-LIVE 的 Issue #1 已创建，真实模型与结果评论仍为 NOT_RUN。执行时复用已有覆盖授权，检查匹配新 profile 哈希的运行配置和凭据；R1-LIVE 未通过之前，R2–R6 保持 TODO。
全项目状态 ENGINEERING_INCOMPLETE；首批 R0/R1 工程验收 PASS。
