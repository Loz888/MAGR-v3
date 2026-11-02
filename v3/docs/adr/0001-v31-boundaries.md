# ADR 0001：独立仓库与 V3.1 权威边界

状态：实施。依据：用户在本会话要求按 V3.1 架构任务书开始改造 V3。

检查时 `/workspace/MAGR-v3` 及其 GitHub origin 都没有提交；V2 位于相邻仓库，HEAD 为 fe7e04a480142b59394b1aabf91515cd9d44334c。
因此采用独立 V3 仓库，应用仍置于 v3/，保留原执行包 v3-upgrade/，不修改 V2 根 AGENTS、代码或任务历史。
这是原包“放到 V2 根目录”布局的明确变更，不是已迁移 V2 功能的声明。

V3.1 新任务组 v31-upgrade/ 为增量计划；原 34 卡保持原文与 TODO 状态，新卡标明对应关系。
V3.1 取代原契约的部分：Thread 不等于 Run；Mandate/Ticket/Run/Assignment 分离；Engine 业务账本是全局权威，checkpoint 仅代表图状态；所有外部写经 Action/Outbox；增加单写者、目标写权和 execution epoch。

首批仅进入 R0/R1。R1 live 闭环未通过之前，不实施 Router LLM、多任务高级协调、长期记忆、投机执行或自动修复。
fixture 流程验证协议与恢复，不构成真实模型业务正确性或 LIVE_READY。

Engine 的持久 PG 会话持有 advisory lock，所有全局业务写使用同一连接与短事务；失去连接即失去写入能力，不以另一条连接绕过锁。
网络调用、图执行和 Artifact 文件写入均在业务事务之外。LangGraph 官方 PostgresSaver 使用 checkpoints schema。
Dispatcher 的领取与回执仍由 Engine 记账；未知或超时写入只对账，不自动重试。取消不撤销已经发出的远程请求。
