# 首批改造交付：R0 与 R1 工程基础

代码位置：本次云端工作区 `/workspace/MAGR-v3/v3`。任务组：`/workspace/MAGR-v3/v31-upgrade`。
验收 Issue：https://github.com/Loz888/MAGR-v3/issues/1。
2026-11-01 首批交付时源码未提交、未 push，仅创建了测试 Issue。后续提交与验收状态以 Git 历史及相应日期的证据为准。

## 已实现

- 独立 Python 3.12 项目、依赖锁、PG/pgvector/Redis 与实际验收入口。
- Engine 单写者、Trigger/Thread/Mandate/Ticket/Run、原子 Mailbox 投递和订阅快照。
- 确定性显式命令、持久化 LangGraph、内容寻址证据、独立直接引文核验。
- 完整 payload/hash/输入版本审批、目标写权、fencing、Action/outbox、统一 Dispatcher。
- 结果未知先对账、取消后迟到回执不复活 Run、七个 SIGKILL 提交点恢复场景。
- 整数预算、全局 profile 次数上限、未知用量保留、已知结果与费用同事务保存及恢复复用。
- 共用 Engine 的 API/CLI、GitHub webhook 验签、Host/Origin/本机凭据/请求大小校验、SSE 历史补发和授权证据下载。
- 官方 MCP SDK 的只读、发布与对账适配；真实模型的受限适配与零请求 preflight。

## 真实验证结果

| 验证 | 结果 |
|---|---|
| V31-R0-02 | 15 测试通过 |
| V31-R0-03 | 16 测试通过 |
| V31-R1-01 | 42 测试通过 |
| 合计 | 73 通过，0 失败，0 跳过 |
| Ruff / mypy | 通过 |
| PG 重启 | 事件、Mailbox、报告与 checkpoint 保留 |
| Redis 停机 | PG 真相可读；新外部调用阻止；已恢复 Redis |
| V2 完整离线回归 | Go 682、Python 453 通过，0 跳过；V2 tracked 文件无变化 |

原始日志、JUnit、真实退出码、组件模式与源代码摘要位于 `v3/evidence/`。
三张工程卡验证同一源代码摘要：`cfd58d55312ec7a89f0006e2964d21bd6bba4877f582119a63c6c9bb4d99864d`。
首次 V2 测试因原合成数据库停止而失败，日志保留；启动原测试容器后复测通过。开发过程中的其他失败尝试也保留。

## 启动

在本次云端工作区终端执行：

```bash
cd /workspace/MAGR-v3/v3
make setup
make deps-up
make migrate
make doctor
make dev
```

`make dev` 监听当前运行机器的 `127.0.0.1:18080`，并非自动公开部署。CLI 示例见上一级 README.md。
依赖容器已启动；数据卷保留，停止用 make deps-stop。没有生产数据库写入、自动合并或公开部署。

## 尚未通过的门禁

完整项目状态 ENGINEERING_INCOMPLETE；R1 live 为 NOT_RUN，COORDINATION_READY 未验收。
工程测试中的 PG/Redis/LangGraph/MCP 协议是真实组件；聊天模型与远端 GitHub 服务为明确标记的替身。创建真实 Issue 仅是测试准备。
没有发起付费聊天模型请求，没有发布业务结果评论。API 模型目录和公开价格页只读查询单独记录。

首批交付时的候选为目标 V3 Issue #1、一次 deepseek-flash 调用、费用上限 USD 1；完整上下文按冻结峰值价格预占 315802 微美元。2026-11-02 用户已将候选上限调整为最多 3 次、合计 USD 2，当前配置见 config/live-r1-proposed.json，变更依据见 ADR 0004。本报告的 73 项测试仍指首批历史验收。
费用授权、应用 GitHub 读写凭据及匹配的环境开关尚未具备。聊天已连接 GitHub 不等于独立运行的应用已持有凭据；凭据应配置到环境，不写聊天、代码或日志。
依据 v3-upgrade/EXECUTION_RULES.md：本包不授予付费或远程写入权限；用户本次另外授权的 Issue 创建已执行，未扩展为模型费用或任意发布授权。

R1-LIVE 是后续阶段硬门禁，R2–R6 保持 TODO。Verifier 当前验证直接引文，不证明复杂根因；RAG、多专家、Router LLM、长期记忆、自动修复与 Watchdog 均未宣称实现。
