# MAGR-v3 V3.5 产品说明

- 状态：D01（2026-11-10）
- 适用版本：V3.5，基线 `6cf67f404d8575d5e02f0dd0edfcbfb67a512ddc`
- 共同前提：[ARCHITECTURE_CONTRACT.md](./ARCHITECTURE_CONTRACT.md)（状态所有权、唯一契约、8 条不变量）
- 相关入口：[90 秒演示](./DEMO.md) · [评测结果索引](./EVALUATION_INDEX.md) · [任务队列](./TASK_QUEUE.md)

V3.5 把“自然语言任务 → 固定 Commit 调查 → Finding/Evidence → 授权修复 → 测试回执 → Review Bundle → 受控发布”收敛成一条可复核的产品链路。所有成功声明都必须带证据；`fixture` 只算工程证据，`live` 必须单独授权并留下调用、费用与授权记录。

## 1. 快速启动（一键本地）

前提：Git、Make、uv、Python 3.12、Docker Engine 与 Compose V2。Windows 先完成 [WSL2 首次启动](../README.md#wsl2-首次启动)。

```bash
git clone https://github.com/Loz888/MAGR-v3.git
cd MAGR-v3/v3
uv python install 3.12
make setup        # 锁定依赖 + 生成本机 .env（0600，不覆盖已有值）
make deps-up      # Docker Compose：PostgreSQL 55433 / Redis 56379，仅绑定回环
make migrate      # 显式 SQL 迁移
make doctor       # PostgreSQL / Redis 真实探测；live 不发起付费调用
make dev          # Web: http://127.0.0.1:18080
```

Web 入口：`/`（首页）、`/workbench`（任务工作台）、`/repo`（仓库分析）、`/docs`（API Schema）。工作台令牌取自本机 `v3/.env` 的 `MGAR_V3_LOCAL_TOKEN`，只放请求头，不要公开。停止依赖用 `make deps-stop`（保留数据卷）。

## 2. Web / CLI 双入口

Web 与 CLI 调用同一组 `/api/v3/workbench` 路由，输出同一个任务视图，不存在第二套语义。

| 动作 | Web | CLI |
| --- | --- | --- |
| 创建任务（只调查 / 允许修复） | `/workbench` 表单 | `mgar workbench --action create --repo-url ... --mode audit_and_repair --task '...'` |
| 查看状态 / 时间线 / 证据 | 任务详情页 | `mgar workbench --action status --task-id <id>` |
| 查看 Finding 与完整 Diff | “加载完整 Diff”按钮 | `mgar workbench --action diff --task-id <id> --finding-id <fid>` |
| 审批（绑定 payload_hash） | 审查 Diff 后按钮才可用 | `mgar workbench --action approve --task-id <id> --finding-id <fid> --payload-hash <hash>` |
| 发布 / 对账 | 同一 API | `mgar workbench --action publish|reconcile ...` |
| 暂停 / 恢复 / 取消 / 追加消息 | 任务页按钮 | `mgar workbench --action pause|resume|cancel|message ...` |
| 仓库只读调查 | `/repo` | `mgar repo-analyze ...` |

`fixture` 发布回执永远是 `fixture://`，页面不渲染成 GitHub 链接；只有 `remote=github` 且 URL 以 `https://github.com/` 开头的回执才显示链接。

## 3. 任务生命周期

```text
queued → auditing → ready → reproducing → repairing → waiting_approval → publishing → completed
                                                     └──────────────→ failed / paused / cancelled
```

- 任务状态唯一权威在 PostgreSQL `app.workbench_*`；Run/Ticket/Action/Approval/Outbox 由 Engine 管理。
- Finding 阶段只允许 `suspected → reproduced → patch_verified → waiting_approval → pr_created`，由验证回执与审批推进，模型与 Memory 不能改写。
- LangGraph Checkpoint 只保存单 Run 图状态；Redis 只做可重建缓存；修复工作区是临时 Worktree；证据与补丁按 SHA-256 存入 Artifact CAS。
- 依赖缺失（缺 profile、缺只读凭据、缺沙箱）落在 `waiting_input`，不算失败；缺 GitHub 写授权停留在 `waiting_approval`，不伪造发布。

## 4. 授权模型

| 模式 | 授权产物 | 当前范围 | 状态 |
| --- | --- | --- | --- |
| `fixture` | 无（本地固定样例） | 调查 + 修复 + 内存/本地 MCP 发布 | 已实现，仅工程证据 |
| `live_readonly` | `AnalysisAuthorization`（actor、仓库、预算、有效期）+ GitHub 只读凭据 | 真实仓库固定 Commit 只读调查 | 已实现；历史真实运行见 V3.2 报告 |
| `live_repair_authorized` | `RepairAuthorization` + `LivePermit`（时间/actor/仓库/预算） | R05 受限路径；Workbench 通用修复仍 403 | Workbench：`NOT_RUN` |
| `live_publication_authorized` | `PublicationApproval`（payload_hash/tree_hash/diff_sha）+ Engine Action/Outbox | 仅 R05 受审 profile；只创建 Draft PR，不 merge | Workbench：`NOT_RUN` |

规则：任何模式都不能用一个布尔开关隐式放权；授权必须绑定 actor、范围、预算、有效期与审批哈希。外部写一律走 `Action → Approval(payload_hash) → Outbox → Dispatcher`；未知结果只读对账，禁止盲重试。

## 5. 唯一契约（摘要）

完整字段与不可修改项见 [ARCHITECTURE_CONTRACT.md](./ARCHITECTURE_CONTRACT.md#5-唯一契约v35-统一)。

| 契约 | 关键字段 | 不可修改 |
| --- | --- | --- |
| Task | `task_id/thread_id/mode/repository/resolved_commit/execution_mode/coverage/limits/request_digest/status/stage/revision` | 仓库、Commit、模式、范围、预算、请求摘要 |
| Finding | `finding_id/stage/severity/classification/confidence/risk_score/source/evidence/reproduction/suggested_fix/fix/approval/publication` | 证据 ID、内容哈希、已产生回执、已批准哈希 |
| RepairHandoff | `handoff_id/repository/base_commit/finding_id/parent_run_id/evidence/reproduction/objective/allowed_paths/budget/authorization/handoff_id` | 全部（任一变化都产生新 ID，需重新审批） |
| PatchProposal | `proposal_id/base_tree/tested_tree/diff/changed_files/public_receipt/verification_receipt/attempts_used/verified` | tree、diff、两份回执、`proposal_id` |
| TestReceipt | `receipt_id/command/exit_code/passed/tests/skipped/duration_ms/log_sha256` | 回执 ID、日志哈希、退出码、测试集合 |
| Approval | `status/payload_hash/requested_at/actor/approved_at/approval_id` | `payload_hash/approval_id/approved_at/actor` |
| Publication | `status/reason/steps/pull_request/pull_request_url/remote/published_at` | 已写回执与 PR 编号/head/base SHA |

## 6. 与 V3.2 / V3.3 / V3.4 的能力差异

| 能力 | V3.2 | V3.3 | V3.4 | V3.5 |
| --- | --- | --- | --- | --- |
| 调查入口 | GitHub 仓库只读分析 | 本地工作区扫描与 Finding 生命周期 | Workbench 统一 API/CLI/Web | 同左，Finding 直接进入 Handoff |
| 状态权威 | Run + Checkpoint | Findings 文件/内存 | Workbench PostgreSQL | PostgreSQL 单一事实源，前向迁移到 `0023+` |
| 修复链路 | 无 | 缺陷验证器（真实 pytest） | 受限 Go 函数修复 + R04 结构化补丁（未接线） | 统一 `RepairHandoff → PatchProposal → Sandbox → Review Bundle`，真实接入 Workbench |
| 沙箱 | 无 | 本机进程 rlimit | 固定 R03 Docker 镜像 | 同一固定镜像，`network none`、非 root，真实仓库源文件快照 |
| 发布 | 无 | 无 | fixture 内存远端 + R05 骨架 | Action/Outbox/Dispatcher，Draft PR 三步回执与只读对账（真实写入 `NOT_RUN`） |
| 跨任务记忆 | 无 | 记忆 schema 与写入门 | Workbench 任务内动机记忆 | M01 跨阶段经验 + M02 Compaction 不变量 |
| 专家 | 无 | 无 | R4 三专家（知识/仓库/数据） | X01 按需只读专家，主 Agent 决策 |
| 安全/恢复 | 基础 | 文件与进程边界 | 18 类安全恢复场景 | E03 35 条场景、6 类，覆盖注入/逃逸/并发/灾备/陈旧/外部写 |
| 评测 | 真实仓库只读报告 | 离线扫描 | P0 固定场景 | E01 Harness（4 任务 + 1 Holdout）、E02 四臂消融、E03 安全门 |

## 7. Live / Fixture 区分

| 维度 | `fixture` | `live` |
| --- | --- | --- |
| 模型 | ScriptedProvider，无网络、零费用 | 真实 provider，必须有授权文件 + 预算/次数/有效期 |
| 外部读 | 固定样例或本地快照 | 真实 GitHub 只读凭据 |
| 外部写 | 内存/本地 MCP 远端，回执 `fixture://` | 独立写凭据 + `PublicationApproval`，只创建 Draft PR |
| 计入的门 | `engineering_gate` | `live_model_gate` / `live_github_write_gate` / `live_agent_gate` |
| 未执行时 | 正常记录 | 必须写 `NOT_RUN`，不得用 fixture 冒充 |

报告目录不可覆盖；`engineering_gate=PASS` 不蕴含任何 `live` 门 PASS。E02 的 live 四臂是真实失败（`PATCH_TRUNCATED`），保留为 `FAIL`，不因工程门通过而降级或抹除。

## 8. 成本模型

- 所有真实调用必须携带授权：`max_calls`、`max_cost_micros`、`expires_at`、仓库/actor 范围；进程级预占与结算，额度耗尽立即停止。
- 费用以整数微美元记账；未知用量保留预占和调用次数，不自动重发；已结算结果可复用，不重复付费。
- fixture 路径零费用、零外部网络；Demo 与 `make verify` 均为零费用。
- 已知真实测量：E02 live 探针 12 次 `deepseek-chat` 调用、637310 micros，四臂全部失败（模型 8192 输出 token 上限小于完整文件体积），结论 `INCONCLUSIVE`，无赢家、配置不改。

## 9. 安全模型

1. PostgreSQL 是唯一事实源；不得新建平行状态机。
2. 权威字段（仓库、Commit、模式、范围、预算、证据哈希、审批哈希）不可被 LLM/Memory 修改。
3. 外部写必须 `Action → Approval(hash) → Outbox → Dispatcher`；未确认结果只读对账，禁止盲重试。
4. 证据分层不得跳级；`verified=True` 只能由独立验证产生。
5. README/Issue/PR/RAG/Memory 都是不可信数据，不得进入系统指令或扩权。
6. 修复只在受限 Docker 内执行：固定镜像 digest、`network none`、非 root、无宿主凭据、路径/Hook/.git 防护。
7. 隐藏验证永不进入 Provider 上下文；公开与隐藏双通过才允许生成提案。
8. 工程门与 live 门分开记录；`NOT_RUN` 不得改写为 `PASS`。

## 10. 已实现与已知边界

已实现：

- Workbench 只读调查、Finding/Evidence、复现、修复、完整 Diff 审查、审批与受控发布（fixture 远端）。
- R01-R05 统一契约与接线；E01 Harness 真实驱动 `AuthorizedRepairOrchestrator + SnapshotRepairRuntime + R03 SandboxExecutor + R02 PatchWorktree`。
- E03 35 条安全/并发/灾备场景（34 条实测覆盖，1 条 `NOT_RUN`）。
- Web/CLI/API 同一任务视图；CLI `publish/reconcile` 与 Web 使用同一端点。

边界（不得宣称已验收）：

- Workbench 通用 `live_repair_authorized` 仍被 403 拦截，只有 R05 受审路径可做真实修复发布。
- 真实 GitHub 写入未执行：`live_github_write_gate=NOT_RUN`；所有 fixture PR 都不是 GitHub 链接。
- 真实 Agent 能力未通过：E02 live 四臂全部 `PATCH_TRUNCATED`，`live_agent_gate=FAIL`，结论 `INCONCLUSIVE`。
- E01 Holdout 未在开发路径执行；E03 真实 PostgreSQL 服务重启为 `NOT_RUN`；生产多节点 HA 未验收。
- 仓库级 `ruff`/`mypy` 仍有历史遗留错误，卡片按 scoped 检查验收。

## 11. 可复现命令

```bash
# 工程门（零费用；真实 PostgreSQL/Redis）
make doctor
make demo                      # 单任务 adversarial fixture 评测（真实 Docker）
make verify TASK=V35-E01       # 评测 Harness
make verify TASK=V35-E02       # 消融与 M02/X01 回归
make verify TASK=V35-E03       # 安全/并发/灾备
make verify TASK=V35-D01       # 本文档、演示、索引与聚合验收

# 生成示例仓库（固定 Commit 的 click 快照，不联网、不调用模型）
uv run --frozen python scripts/demo_v35.py --out .local/demo-v35

# 受控 Draft PR（fixture MCP 远端，需先 make sandbox-build）
uv run --frozen python scripts/r5_demo.py
uv run --frozen python scripts/r5_demo.py --run <run_id> --approve-payload-hash <hash>

# live 评测（必须显式授权 + 自带 provider 工厂；未授权或缺少工厂只写 NOT_RUN）
uv run --frozen python scripts/run_eval.py --suite dev --mode live \
  --authorization .local/live-authorization.json --provider your_module:your_provider_factory
# E02 四臂 live 消融使用同一授权文件：
uv run --frozen python scripts/run_eval.py --ablation --mode live --suite dev \
  --authorization .local/v35-e02-live-authorization.json
```

所有原始日志保存在 `v3/evidence/v35/<CARD>/<UTC>-<RUN_ID>/`（`.gitignore`，不入库）；公开可复核记录是各卡 `accept.json` 与 [评测结果索引](./EVALUATION_INDEX.md)。
