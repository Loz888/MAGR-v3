# V31-R4-LIVE · 真实跨源调查与单 Agent 对照

状态 DONE（实验已执行）。工程 PASS；live PARTIAL；依赖 V31-R4。

本卡执行用户明确要求的三个正常及两个不足/越权案例，使用真实 DeepSeek、锁定 GitHub 代码、真实本地检索、HTTP MCP 和 PostgreSQL 只读角色。独立 Oracle 与模型隔离，输入和判定标准在请求前冻结。

首轮多专家和单 Agent 各 4/5，总体 8/10；保持原始 FAIL，不改变 Oracle。多专家金额案例因空来源结论重试耗尽单任务额度，之后将专家问题收窄为本来源观察，在相同输入下完成一次有界恢复。多专家最终有 3/3 正常及 2/2 负例通过记录；单 Agent 定义案例的结构化去重字段为 null，失败保留。不将恢复混入首轮比较，也不宣称多专家优于单 Agent。

新增授权为最多 24 次、累计 USD 2。本轮 23 次、保守 USD 0.024569；此前账本未重置，累计 26 次、USD 0.027025，无未知调用或新增业务评论。恢复继续共享原授权，不能重发未知模型请求。

工程：`make lint`、`make typecheck`、`make verify TASK=V31-R4`（119 项）、`make test`（196 项）均通过。注册工程测试使用受控模型替身，真实结果来自独立付费实验。

[业务报告](../../v3/docs/r4-live-report.md) · [验收摘要](../../v3/docs/acceptance/r4-live-20261102.json)。原始数据、授权及响应保留在本地 ignored .local/r4-live-20261102/trial/。R5/R6 不在本卡范围。
