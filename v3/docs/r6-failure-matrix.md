# R6 故障与权限覆盖矩阵

> 2026-11-05 更新：三项发布阻塞已在用户批准的独立 Linux/systemd VM 范围内实测通过；R6 状态 ACCEPTED_ISOLATED_VM。下文历史结果与当时限制保留。实际部署、两次整节点 SIGKILL、正常重启和 PG/角色/CAS 恢复见 [完整宿主修复档案](repairs/R6-HOST-20261105/README.md)。不覆盖物理存储掉电、生产主机或多节点 HA。

工程证据使用真实 PostgreSQL，模型/远端故障使用明确标注的受控协议；实际付费质量评测独立记录。原始中断、失败、完整重跑各保留独立目录，不把部分测试成功当成全量通过。

| 场景 | 检查与证据入口 | 实际模式及边界 |
|---|---|---|
| Worker 不更新租约、旧 epoch 更新/提交 | test_r6_runtime.py、test_coordination.py | 真实 PG；5 秒心跳线程和 30 秒租约，进展事件独立 |
| Mailbox/Router/Outbox 积压 | test_r6_runtime.py | 真实 PG 队列，Watchdog 只能写恢复建议，不能派发 |
| Redis 中断和恢复 | test_actual_redis_stop_keeps_pg_work_and_recovers_without_dispatch | 独立真实 Redis 容器 stop/start、固定环回端口，主库工单保留 |
| PG 会话丢失、API 进程中断 | scripts/delivery.py smoke、test_recovery.py | 临时数据库真实 pg_terminate_backend 和 SIGKILL；重启后报告与去重保留 |
| MCP 不可用/超时 | test_mcp.py、test_r4_mcp.py、test_r6_watchdog.py | 真实 stdio/HTTP 受控服务器；不破坏生产 GitHub 服务 |
| 模型超时/截断/坏结构、迟到返回 | test_models.py、test_router_model.py、test_r5_model_recovery.py | 受控 HTTP 故障与真实 PG 账本；unknown 预占保留，禁止重发 |
| Router/调查未知计费 | test_router_model.py、test_models.py | 任一模型账本未知都阻止新付费；Watchdog 在 Run 取消后仍能看到未知项 |
| 审批过期、旧提案/旧输入、取消 | test_effects.py、test_duplex.py、test_r5_review_renewal.py | 真实 PG 事务/fencing；同哈希人工续签与原 Action 分开保留 |
| 发布已接收但本地丢回执 | R5 真实报告、test_r5_recovery.py | 历史真实 Draft PR #2 只读 SIGKILL 恢复；本轮最终评测不写 GitHub |
| 客户端重连 | test_api.py、test_cli.py | HTTP/CLI 与 SSE 游标重放；没有实测浏览器进程崩溃或宿主重启 |
| 跨 Run 引用、撤销 ACL、缓存来源变化 | test_pipeline.py、test_r4_context.py、test_r4_knowledge.py | 真实 PG/Redis，来源哈希、ACL 重核；本地预训练检索另外做真实预检 |
| 提示注入、事件重复、只读权限 | test_r6_policies.py、test_r5_repair.py | 事件正文不能创建许可；影子 Thread、独立预算；只读结果不创建 Action/Outbox |
| 路径/类型/导入/测试篡改、任意命令 | test_r5_repair.py、test_final_evaluation.py | 真实无网络、非 root、无宿主挂载 Docker/Go；AST 限定纯函数体 |
| 最终预算 270 次与旧授权隔离 | test_final_270_call_grant_uses_isolated_budget_and_preserves_legacy_limits | 专用 benchmark_budgets；通用账本数据库上限仍为 100，普通许可最多 3、R4 许可最多 24 |
| 新安装、升级幂等、备份恢复 | scripts/delivery.py smoke | 新源码目录/独立锁定虚拟环境、17 个迁移、pg_dump/psql 恢复到第二个新库 |

本环境使用 VFS Docker 存储，曾因并行重测试导致磁盘耗尽并使两个测试 PG 容器退出。中断日志保留；已迁移可重建缓存/工具/模型到临时盘并保留原路径链接，只清理具体已停止的临时沙箱，不执行通用 prune，不删除数据库卷。恢复后重新核对主库迁移和完整历史费用账本，最终结果以完整重跑为准。

没有实测整台数据库节点重启、宿主 systemd 开机恢复、公网入口或全面生产负载。systemd 文件是部署模板。本矩阵不构成全面生产安全或高可用证明；最终业务质量是否过线由独立 Oracle 的实际 90 项结果决定。
