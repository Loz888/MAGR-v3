# R6 监控、有限主动性与评测运行说明

R6 工程实现已补齐；最新 v6 真实 90 项评测质量门槛 PASS，全项目发布阻塞仍在，不能标 LIVE_READY。当前结果、费用与交付范围见 [交付报告](r6-delivery.md)。下述默认 Watchdog 仍为 observe；主动策略需要管理员对完整 hash 明确激活。

## 本机运行

在自己的克隆目录执行，例如 WSL 的 `~/projects/MAGR-v3/v3`，不是云端 `/workspace` 路径。

```bash
git pull --ff-only origin main
make setup
make deps-up
make migrate
make watchdog-setup
```

setup 生成独立私有令牌文件 `.local/watchdog/client.json` 与 `.local/watchdog/server.env`，权限 0600，不显示令牌、不覆盖已有配置。已有文件时保留它们；需要另一套配置可用 `scripts/setup_watchdog.py --directory .local/watchdog-other`。

在服务终端加载专用服务端令牌后启动：

```bash
set -a
. .local/watchdog/server.env
set +a
make dev
```

在另一个同目录终端运行一次巡检：

```bash
make watchdog-once
```

此入口用最小环境启动独立进程，仅从私有客户端配置读取本机 API 地址及受限令牌，不提供数据库、模型或 GitHub 凭据。默认未配置专用令牌时，服务端的 Watchdog 入口关闭。客户端只接受环回 HTTP，不跟随重定向。需要持续巡检时运行：

```bash
env -i PATH="$PATH" LANG=C.UTF-8 "$PWD/.venv/bin/python" -m mgar_v3.recovery.watchdog --config .local/watchdog/client.json
```

每 30 秒观察一次；Ctrl+C 停止。没有异常时 signals/created 为零；重复观察同一事实 created 为零。服务不可用时返回 unavailable、saved=false，不能把未持久化的请求说成已保存。每次最多 256 项，partial=true 代表覆盖不完整。

如果请求已发出但回执丢失，则显示 saved=null、submission_outcome=unknown；它可能已经保存，不能当作零次提交。后续使用同一观察键去重确认，不控制 Run 或重放外部写。

## 审阅和处理

巡检令牌只能读取安全快照和提交建议，不能处理建议。管理员通过现有本机令牌使用相同 API：

```bash
uv run --frozen mgar recovery-requests
uv run --frozen mgar recovery-decision REQUEST_ID --decision wait
uv run --frozen mgar recovery-decision REQUEST_ID --decision escalate
uv run --frozen mgar recovery-decision REQUEST_ID --decision pause
uv run --frozen mgar recovery-decision REQUEST_ID --decision cancel
```

这些决定由人显式选择，不是建议逐条执行。事实或 epoch 已变化时旧请求变为 stale，不控制 Run；unknown 只允许 wait/escalate。暂停/取消沿用 Engine 权限和事务路径，旧 Worker 提交会被拒绝，不会重新发送未知远端写。

API：受限 `GET /api/v3/watchdog/snapshot`、`POST /api/v3/watchdog/recovery-requests`；管理 `GET /api/v3/recovery-requests`、`POST /api/v3/recovery-requests/{id}/decision`。

## 验收和剩余工作

```bash
make lint
make typecheck
make verify TASK=V31-R6-01
```

首个切片历史验收为 82 项通过；后续完整工程基线 295 项、核账/恢复专项 61 项通过（有重叠）。心跳、队列/依赖健康、主动策略、投机预算、三臂对照及部署检查均已实施。原始首轮 90 项质量 FAIL；后续修复和 v6 最新实测、调用及费用见交付报告。

见 [工程摘要](acceptance/r6-observe-engineering-20261103.json) 和 [ADR 0011](adr/0011-observe-only-watchdog.md)。

## R6 后续工程

已补充独立执行心跳、Mailbox/Router/Outbox 积压、PG/Redis/MCP 健康、主动策略和独立只读投机预算。运行完整门禁使用 `make verify TASK=V31-R6`；首切片历史 82 项不代表当前完整回归数量。最新结果以 [交付报告](r6-delivery.md) 为准。

Watchdog 每次巡检先调用 `GET /api/v3/watchdog/health`；网络探测在业务事务外，由 Engine 保存状态。未配置 MCP 明确显示 unconfigured。PG 断开需恢复数据库连接并重启 Engine，不能换连接绕过单写者锁。Run 心跳每 5 秒、租约 30 秒，不等于取得业务进展。积压阈值 16、无进展 120 秒、等待输入/审批 3600 秒，均只提交建议。依赖恢复后再故障使用新 generation。

管理员 API 提供：

- `POST /api/v3/proactive/policies`：完整 Policy JSON，创建时 observe。
- `GET /api/v3/proactive/policies`：最近 200 项及独立预算用量。
- `POST /api/v3/proactive/policies/{policy_hash}/activation`：`{"policy_hash":"完整哈希","mode":"auto"}` 或 revoked。
- `POST /api/v3/proactive/events`：类型化、已认证的 CI/依赖更新/PR 评论事件。

Policy 必填 repository、kinds、allowed_actors、source_ref（40 位 commit）、question、带时区 expires_at；可设 issue_number、required_labels。默认最多 1 Run、8 次读取、0 付费调用；允许范围分别为 10/200/3，费用最多 20,000 微美元。fixture 策略不允许声明付费额度。live 必须与服务端已有付费 profile 完全匹配，开启环境变量不构成授权。固定 source_ref 适合预先批准的调查；新 commit 需要新策略，不会自动跟随 main。

只有签名的 workflow_run completed failure/timed_out 自动映射 CI 事件；dependency_update 和 pr_comment 目前需要认证入口的事件适配器，没有实现所有平台的无人值守监听。保持 observe 运行、核对误报和预算后，再由管理员明确激活具体 hash。当前云端主库没有激活任何生产自动策略。

## 最终评测

题集、执行器和独立判定均在 `eval/final-v1/` 与 `src/mgar_v3/benchmark/`。具体冻结目录、费用和结果在 [交付报告](r6-delivery.md)。准备工作不付费：

```bash
uv run --frozen python scripts/final_eval.py prepare --directory .local/final-eval-NEW --source-ref FULL_GITHUB_COMMIT
uv run --frozen python scripts/final_eval.py preflight --directory .local/final-eval-NEW
uv run --frozen python scripts/final_eval.py plan --directory .local/final-eval-NEW
```

需要先安装官方 GitHub MCP、真实本地 RAG 模型与训练重排器（见 R4/R5 使用说明），并配置本机只读 GitHub 认证。prepare 建立实际知识快照和独立 SQL 只读角色，冻结 GitHub commit/tree/文件，不调用模型。目录内 reader.json 含本机私有连接信息，不能提交或分享整个目录。

授权文件必须绑定实际 manifest_hash、approved_by、带时区 expires_at、max_new_calls、max_new_cost_micros、allow_paid=true、allow_external_write=false。此文件只在收到新增付费许可后生成；不复用已耗尽授权。正式入口还要求显式执行开关：

```bash
uv run --frozen python scripts/final_eval.py run --directory .local/final-eval-NEW --authorization .local/final-eval-NEW/authorization.json --execute-paid
uv run --frozen python scripts/final_eval.py report --directory .local/final-eval-NEW
```

遇到未知计费停止，剩余 NOT_RUN；同一目录禁止静默重跑。模型/Oracle/源码变化需新冻结版本，重试不能覆盖旧失败。no_reranker、no_evidence_repair、no_verifier 各需独立冻结及新增授权，不能侵入生产权限校验。性能统计固定每臂 30、正常 25、负例 5，包含 Wilson 区间与配对描述统计；不能据小样本强称多 Agent 更好。

## 部署与备份

源码交付包由显式目录清单生成，含 uv.lock，不含 `.env`、`.local`、模型、凭据或历史证据：

```bash
uv run --frozen python scripts/delivery.py bundle --output .local/release/mgar-v3-r6.tar.gz
uv run --frozen python scripts/delivery.py smoke --output .local/deploy-check --postgres-container mgar-v3-postgres-1
```

smoke 创建自己的两套临时数据库和源码/虚拟环境，检查真实 HTTP、PG 连接丢失、SIGKILL 恢复和 pg_dump/psql 还原，结束只删除本次生成的临时库。脚本需要本机 Docker、现有 V3 PostgreSQL 管理连接和 Python 3.12；测试自身不启用付费或发布。它不是生产数据库恢复脚本。

生产部署可参考 `deploy/systemd/`：Engine 和 Watchdog 使用不同系统用户，server.env 与客户端配置分别 0600，源码可读、`.local` 只允许 Engine 写。示例固定 `/opt/mgar/v3`，应按本机路径调整；保持单 Engine 实例和环回监听，不把 API/数据库直接暴露到公网。没有在本环境启用宿主 systemd 或开放外网服务。

升级前停止接收新任务并等待在途网络请求结束，保留 unknown 项，不重发。停止应用，使用现有 PostgreSQL 管理工具制作完整备份并另外备份 `.local/artifacts`、模型/配置及私有许可，验证备份后执行 `make migrate`、`make doctor`，再启动应用/Watchdog。迁移不可原地降级；回退时用原版本源码、新数据库恢复经验证备份并恢复匹配 CAS。Redis 可重建，不作为业务权威。不要用本机演示的 `make setup` 覆盖生产凭据。

本轮最终执行使用 17 个迁移和 `benchmark_budgets` 专用组额度；旧通用预算上限不放宽。首次启动的预算约束失败没有产生模型调用，保留在原冻结目录；修复后的新冻结目录为 `.local/final-eval-v2`。命令中的目录应使用交付报告列出的最新版本。授权范围包括该固定题集向 DeepSeek 官方 completion 接口传递合成/公开证据，不包含凭据、连接串或隐藏判定数据。完整故障范围见 [覆盖矩阵](r6-failure-matrix.md)。

## 聚合核账与显式恢复

两笔未知预占已依据用户提供的完整供应商小时账单核清费用上界，记录见 [核账清单](acceptance/r6-usage-reconciliation-20261103.json)。不重发，响应丢失仍记失败；逐请求 token 未填造。随后完成原来的 66 项，原财务组未重置。当前 90 项评测已结束，不应继续运行原目录。

显式 Supervisor 仅允许执行监督、经审计的账单处理和本地 I/O 初始化保护变化；生成请求、价格和 Oracle 的一致性经过 AST 与源码归档检查。任何新的策略改进需新的 manifest；旧失败保留，不能重置总次数或把同版失败替成成功。

源代码包不含实验财务账本；换机器继续实验前必须恢复原 PostgreSQL 账本与对应 CAS/私有配置，不能创建空库当作费用和次数重置。真实数据库恢复还需保存全局角色/SQL reader 授权和私有 reader 配置；fixture smoke 不覆盖全部生产角色恢复。全局角色备份和私有 reader 配置不能发布 GitHub。

恢复请求按项目隔离。本轮作用域 project_id 在核账清单中，默认 orders-demo 视图不会显示其他项目。云端实验数据库不会随 GitHub 源码自动同步到 WSL。
