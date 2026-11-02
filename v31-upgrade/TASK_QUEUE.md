# V3.1 增量任务队列

原始 34 卡保持在 v3-upgrade/，不重写原 TODO 与历史。新卡映射表示采用对应契约，不表示原卡整体完成。

| 任务 | 内容 | 状态 | 工程门禁 | live 门禁 |
|---|---|---|---|---|
| V31-R0-01 | 独立仓库、V2 基线与架构边界 | DONE | PASS | NOT_APPLICABLE |
| V31-R0-02 | 锁定环境、PG/Redis、MCP 适配与验收框架 | DONE | PASS | NOT_APPLICABLE |
| V31-R0-03 | 事件、授权、工作项、执行与动作持久化契约 | DONE | PASS | NOT_APPLICABLE |
| V31-R1-01 | 确定性调查、Verifier、审批与统一出口工程闭环 | DONE | PASS | NOT_RUN |
| V31-R1-BUDGET-02 | 候选最多 3 次调用、累计预算 USD 2 | DONE | PASS | PASS |
| V31-R1-LIVE | V3 Issue #1 真实模型、审批与结果评论验收 | DONE | NOT_APPLICABLE | PASS |
| V31-R2 | Mailbox 与显式 intake/replan | DONE | PASS | NOT_RUN |
| V31-R3 | 受限 Router LLM 与多任务 | TODO | NOT_RUN | NOT_RUN |
| V31-R4 | RAG 与多专家 | TODO | NOT_RUN | NOT_RUN |
| V31-R5 | 受限修复与 Draft PR | TODO | NOT_RUN | NOT_RUN |
| V31-R6 | Watchdog、主动性与发布评测 | TODO | NOT_RUN | NOT_RUN |

2026-11-02 用户授权真实调用与 GitHub 推送；沿用最多 3 次物理调用、累计 USD 2 的限制。R0 与 R1 工程复验通过，R1 真实闭环 PASS 后才启动 R2。实际调用 2 次，保守计费 USD 0.001745，结果评论一条；模型落库与远程写回执两个 SIGKILL 恢复点通过。

公开验收摘要：v3/docs/acceptance/r1-live-20261102.json。原始失败与运行日志保留在本地 evidence/，不随公开源码发布。全项目仍为 ENGINEERING_INCOMPLETE。

R2 工程改造已通过：完整回归 112 项，任务验收 57 项，均无失败或跳过；四个真实 SIGKILL 故障点通过。R2 使用受控模型和远端，未新增真实付费调用；R1 账本仍为 2 次、保守 USD 0.001745。R2 摘要见 v3/docs/acceptance/r2-engineering-20261102.json。R3–R6 保持 TODO。
