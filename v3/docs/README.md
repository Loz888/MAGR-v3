# 文档导航

项目是事件驱动持续研发Agent研究原型。电商“研巡”的两项P0已经实施，真实报告与修复提案验收通过；当前交付审批状态见[实施档案](repairs/P0-20261106/README.md)。原技术报告/PDF保留P0实施前快照，新增技术与验证单列附录。

| 阅读目的 | 入口 |
|---|---|
| 快速了解项目 | [仓库首页](../../README.md) |
| 理解技术、设计及失败修复 | [完整技术报告](TECHNICAL_REPORT.md) · [PDF](reports/20261106/MGAR-V3.1-Technical-Report.pdf) · [独立HTML](reports/20261106/MGAR-V3.1-Technical-Report.html) |
| 本机启动和演示 | [应用README](../README.md) · [已验证版本](versions.md) |
| 核对报告数字和证据 | [报告事实索引](reports/20261106/report-data.json) · [文档检查](reports/20261106/review.json) |
| 了解两项P0设计 | [设计入口](design/p0-commerce-v1/README.md) · [闭环](design/p0-commerce-v1/01-commerce-workflow.md) · [统一结果](design/p0-commerce-v1/02-business-result.md) |
| 使用电商工单、核对最新实测 | [运行手册](p0-commerce-runbook.md) · [实施/修复档案](repairs/P0-20261106/README.md) · [完整提案](repairs/P0-20261106/draft-pr-review.md) |
| 查看任务阶段及边界 | [任务队列](../../v31-upgrade/TASK_QUEUE.md) · [ADR目录](adr/) |

## 运行与架构

| 阶段 | 设计 | 操作或验收 |
|---|---|---|
| R0/R1 基础与首个真实交付 | [边界ADR](adr/0001-v31-boundaries.md) | [R1真实验收](acceptance/r1-live-20261102.json) |
| R2 持续任务 | [Mailbox/安全点](adr/0002-r2-mailbox.md) | [工程验收](acceptance/r2-engineering-20261102.json) |
| R3 路由与任务关联 | [Router](adr/0003-r3-router.md) | [限定真实验收](acceptance/r3-20261102.json) |
| R4 跨源调查 | [知识/数据/专家](adr/0005-r4-investigation.md) | [运行](r4-runbook.md) · [真实对照](r4-live-report.md) |
| R5 受限修复 | [修复边界](adr/0007-r5-repair-boundaries.md) · [真实适配](adr/0008-r5-native-adapters.md) | [运行](r5-runbook.md) · [真实PR恢复](r5-live-report.md) |
| R6 监控/有限主动性 | [恢复请求](adr/0011-observe-only-watchdog.md) · [评测](adr/0012-r6-monitoring-proactivity-evaluation.md) | [运行](r6-runbook.md) · [故障矩阵](r6-failure-matrix.md) |
| 协调补验与修复 | [D/E/F设计](adr/0014-coordination-comparison.md) · [候选契约](adr/0015-coordination-candidate-contract.md) | [v1报告](experiments/coordination-v1/README.md) · [v2报告](experiments/coordination-v2/README.md) |
| 两项P0 | [冻结设计](adr/0016-p0-commerce-and-business-results-design.md) · [实施ADR0017](adr/0017-p0-commerce-implementation.md) | [工程/真实验收](repairs/P0-20261106/acceptance.json) · [运行](p0-commerce-runbook.md) |

## 结果与问题档案

| 档案 | 内容 |
|---|---|
| [R6完整修复](repairs/R6-20261103/README.md) | 46/90→74/90→87/90、数值/契约/权限/补丁协议修复、账单核对、原三项FAIL |
| [JSON模式请求事故](repairs/R6-20261103/json-mode-incident.md) | HTTP400、预检缺口、未知预占与严格账单核对 |
| [宿主与恢复](repairs/R6-HOST-20261105/README.md) | V2测试时钟维护、独立systemd VM、整虚拟节点/PG/角色/CAS恢复 |
| [E/F第二版修复](repairs/COORD-20261106/README.md) | 动作/UUID契约问题、级联失败、原子重核、恢复收据及Worker残余FAIL |
| [最终三臂逐项结果](../eval/reports/final-v6-results.json) | 30任务×3配置，87PASS/3FAIL |
| [最终三臂统计](../eval/reports/final-v6-summary.json) | 调用、费用、延迟、引用、置信区间和配对统计；消融NOT_RUN |

旧ADR和交付文件保留当时状态与限制；它们不会因后续修复被改写为“当时已通过”。最新结论应结合上述报告的时间和作用范围读取。
