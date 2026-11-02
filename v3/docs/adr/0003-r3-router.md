# ADR 0003：有界 Router 与多任务连续性

依赖的 R1 live 和 R2 工程门禁已经通过。R3 只处理冷启动、澄清和有限任务关联；订阅直通、显式调查命令和 Run 内图继续沿用 R1/R2。Router 没有 Shell、MCP、调查或外部写工具，输出只是一组待 Engine 校验的提案。

Engine 的 Routing 子服务使用同一持锁连接和事务，不成为第二写者。批次冻结事件、Thread revision、目标 Run epoch、Mandate、policy 和上下文哈希；模型只输出有限动作及候选 ID，程序构造版本化 RouterDecision。单批最多 16 个事件、每 Thread 4 条、总上下文 64 KiB；任一决策失效则整批拒绝并保留审计和待路由输入。已知模型结果可恢复；unknown 用量不得重发。

新任务必须有来自获准用户的显式请求。关联只创建可追溯的 context 链接，不迁移已有 Run 的 Assignment，也不授予对其他目标的写权。召回最多三个同项目、同仓库、同 actor、同执行模式的候选；读取链接逐跳重查当前权限。暂停/恢复/取消提案只接受与明确控制命令相符的目标，Web/CLI 的直接控制不必调用 Router。

公开支持 ignore、create_ticket、request_clarification、reply_system 和受控 pause/resume/cancel；observer 仅内部测试入口开放，投递进入独立只读收件箱，不污染 primary Mailbox 的序号或输入版本。unsubscribe/handback 在本阶段拒绝。系统回复使用程序模板，仍需 Action hash 审批，并经统一 Outbox/Dispatcher；不会绕过已用尽的 R1 评论额度。

路由纠正保留原始事件及订阅快照，只允许重开尚未产生工具/模型结果或远程动作的路由，先取消旧提案，再记纠正原因和新 revision。无法安全重开的执行不被后台改派。

真实 Router 使用独立配置与授权，费用进入既有 authorization_budgets。共享预算不重置：本会话上限仍为累计三次物理调用、USD 2，R1 已使用两次。工程测试使用真实 PostgreSQL 和受控模型；真实 Router 语义验收、业务 live 与工程门禁分别报告。R4 及后续阶段不在本次范围。
