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
| V31-R3 | 受限 Router LLM 与多任务 | DONE | PASS | PASS |
| V31-R4 | RAG 与多专家 | DONE | PASS | PARTIAL |
| V31-R4-LIVE | 真实跨源调查与单 Agent 对照 | DONE | PASS | PARTIAL |
| V31-R5 | 受限修复与 Draft PR | IN_PROGRESS | PASS（受控范围） | NOT_RUN |
| V31-R6 | Watchdog、主动性与发布评测 | TODO | NOT_RUN | NOT_RUN |

2026-11-02 用户授权真实调用与 GitHub 推送；沿用最多 3 次物理调用、累计 USD 2 的限制。R0 与 R1 工程复验通过，R1 真实闭环 PASS 后才启动 R2。实际调用 2 次，保守计费 USD 0.001745，结果评论一条；模型落库与远程写回执两个 SIGKILL 恢复点通过。

公开验收摘要：v3/docs/acceptance/r1-live-20261102.json。原始失败与运行日志保留在本地 evidence/，不随公开源码发布。全项目仍为 ENGINEERING_INCOMPLETE。

R2 工程改造已通过：完整回归 112 项，任务验收 57 项，均无失败或跳过；四个真实 SIGKILL 故障点通过。R2 使用受控模型和远端，未新增真实付费调用；R1 账本仍为 2 次、保守 USD 0.001745。R2 摘要见 v3/docs/acceptance/r2-engineering-20261102.json。R3 后续状态见下；R4–R6 保持 TODO。

R3 工程与限定真实 Router 验收通过：完整回归 150 项、任务验收 89 项，均无失败或跳过；四个 Router SIGKILL 提交点恢复通过。两个事先冻结的合成场景只调用一次真实 Router，重启复用结果；累计三次调用、保守 USD 0.002456，未新增 GitHub 评论。公开摘要：v3/docs/acceptance/r3-20261102.json。该 live 门禁仅覆盖合成事件上的 Router，不等同生产业务评测。R4–R6 保持 TODO。


R4 工程门禁已通过：完整回归 187 项、注册验收 103 项，0 失败、0 跳过，新增四个真实 SIGKILL 恢复点通过。真实本地中文向量/学习重排与 PG 混合检索在十二个冻结改写问题上 Recall@5 为 11/12，漏召回样本保留；同一 Run 的真实本地检索、HTTP MCP、只读 SQL 组合也通过。专家与 Repo 的组合实验使用明确标记的合成资料协议实现，完整付费跨源业务 live、业务 Oracle 和单 Agent 对照仍为 NOT_RUN。本轮新增付费调用与 GitHub 评论均为零，账本仍为 3/3、保守 USD 0.002456。摘要见 v3/docs/acceptance/r4-20261102.json，使用说明见 v3/docs/r4-runbook.md。R5/R6 保持 TODO。


R4 真实实验已执行：三个正常和两个不足/越权案例各运行多专家/单 Agent。首轮双方各 4/5（总计 8/10），原始失败保留；修复专家问题范围后，失败的多专家案例在相同输入与 Oracle 下恢复通过，多专家有 3/3 正常及 2/2 负例通过记录。恢复不混入首轮对照成绩，不能宣称多专家胜出。用户追加授权新增最多 24 次、累计 USD 2；本轮实用 23 次、保守 USD 0.024569，累计 26 次、USD 0.027025，无未知调用、无新增业务评论。完整回归 196 项、R4 专项 119 项通过。live 仍标 PARTIAL，项目为 ENGINEERING_INCOMPLETE。详见 v3/docs/r4-live-report.md 和 v3/docs/acceptance/r4-live-20261102.json。R5/R6 保持 TODO。


R5 首轮受控工程通过：注册验收覆盖完整回归 229 项，0 失败、0 跳过，四个真实 SIGKILL 点与三类响应丢失验证通过。新增真实 Docker/Go 隔离测试、独立隐藏 Oracle、完整 diff 审批及编辑重测、Draft PR 三步 MCP 持久协议。本地完整演示已通过。补丁提供者和发布远端均为 fixture；真实 GitHub source/model/write profile 与适配器尚未完成，R5 整体为 IN_PROGRESS，live 为 NOT_RUN。未新增付费调用、真实业务评论或 PR，累计仍 26 次、保守 USD 0.027025。R6 保持 TODO。摘要：v3/docs/acceptance/r5-engineering-20261102.json；运行方式：v3/docs/r5-runbook.md。
