# RepoSage / MGAR V3.3–V3.4 基线架构与实施契约

- 状态: B00 完成（基线冻结）
- 基线 Commit: `e98e494f58f8844931d614d563cc6a43a6cf0ed5`
- 数据库迁移 head: `0020_repository_analysis.sql`
- 记录时间: 2026-11-09（UTC+8）
- 适用范围: 本文件是 V3.3（仓库理解）与 V3.4（代码修复）所有任务卡的共同前提。后续任务卡只能新增能力，不得默默改变本文件列出的不变量。

---

## 1. 基线与运行环境

| 项 | 实测值 | 说明 |
| --- | --- | --- |
| 代码基线 | `e98e494` | 即 V3.2 交付后 `main` 的 HEAD；`git status --short` 为空 |
| 迁移版本 | 20 个（`0001`…`0020`） | `make migrate` 输出 `{'applied': []}` |
| PostgreSQL | `pgvector/pgvector:0.8.1-pg16-bookworm` | 容器 `mgar-v3-postgres-1`，`127.0.0.1:55433` |
| Redis | `redis:7.4.7` | 容器 `mgar-v3-redis-1`，`127.0.0.1:56379` |
| Python | 3.12.16（uv 管理） | `pyproject.toml` 要求 `>=3.12,<3.13` |
| uv | 0.13.7（WSL2） | Windows 侧无 uv/make/docker，验收命令统一在 WSL2 执行 |
| 依赖环境 | `/tmp/mgar-v3-venv` | `uv sync --frozen`；不把 Linux venv 放进 `/mnt/c` |
| 验证注册表 | `v3/config/verification.json` | 已注册 `B00`；`make verify TASK=<卡号>` |

Windows 侧原有的系统 Python 3.13 仅用于此前的临时手工调用，不属于本项目受支持的运行时；所有正式验收以 WSL2 + uv + 3.12 为准。

---

## 2. 现有模块与职责

| 模块 | 职责 | 持久化 | 对 V3.3/3.4 的复用价值 |
| --- | --- | --- | --- |
| `coordination/engine.py` | Run/Ticket/Mandate 生命周期、唯一业务写者、预算、usage 记账、Action/Approval/Outbox、fencing | `app.runs` `app.tickets` `app.mandates` `app.budgets` `app.authorization_budgets` `app.usage_calls` `app.actions` `app.approvals` `app.outbox` | 直接复用：所有新 Run 必须走同一事务与预算记账 |
| `repository_analysis/` | V3.2 只读仓库分析：固定 Commit、只读 GitHub MCP、Reason→Tool→Observation、证据报告 | `app.repo_analysis_runs` `app.repo_tool_calls` `app.repo_analysis_claims` | 直接复用：工具网关、Evidence、报告校验、Thread 追问 |
| `knowledge/` | 文档上传/切块/快照/检索、记忆与上下文边 | `knowledge.documents` `knowledge.document_versions` `knowledge.chunks` `knowledge.snapshots` `knowledge.snapshot_chunks` `knowledge.active_snapshots` `knowledge.ingestion_jobs` `knowledge.search_traces` `knowledge.context_edges` `knowledge.memories` | 复用快照与记忆表；V3.3 需新增仓库语义层 |
| `context/` | 上下文装配与 `ContextStore` 封装 | `app.evidence`、`app.artifacts` | 复用 Evidence/artifact 读取语义 |
| `repair/` | R5 A1 修复闭环：工作区、沙箱、测试、Draft PR 提案 | `app.repairs*` 系列表 | 复用工作区/沙箱/发布传输骨架 |
| `effects/` | 外部写入传输与派发 | `app.actions` `app.outbox` | 见 §2.1，写能力必须按 action_type 精确授权 |
| `mcp/` | 只读 GitHub MCP 客户端与工厂 | 无 | 复用：`github_client(..., tool_group="analysis")` |
| `api.py` / `cli.py` / `web/` | 入口 | 无 | 复用：新增 Run 类型沿用现有错误码与 SSE/轮询约定 |

### 2.1 写边界现状（关键）

- `effects/github.py::GitHubMCPTransport` 只支持 `add_issue_comment`，作用域绑定 `LivePermit.profile`（repository/issue_number/action_type/expected_source_hash）。
- `repair/publication.py::DraftMCPTransport` 已实现 `create_branch`、`push_files`、`create_pull_request` 三步分段日志与恢复查找，但**显式拒绝真实写**：构造时要求本地 stdio fixture（无 token），否则抛 `R5_WRITE_PROFILE_REQUIRED`。
- `LivePermit.action_type` 仅允许 `issue_comment` 与 `draft_pr`，且 `draft_pr` 仅在 `r5-repair-v1` 下合法，绑定单一 Go 订单函数路径。
- `repository_analysis` 使用独立的 `AnalysisAuthorization`（`approved_by`/`expires_at`/`allowed_repositories`），与 `LivePermit` 不共用 profile。

结论：**V3.3 的分析能力可完全复用 V3.2 只读链路；V3.4 的真实发布必须新增受审查的发布 profile 与 Transport，不能放宽现有 `DraftMCPTransport` 的 fixture 限制。**

---

## 3. 数据关系与状态机

### 3.1 核心关系

```text
Ticket(1) ── (1)Mandate ── (n)Budget / AuthorizationBudget
   │
   └─(n) Run ── (n) UsageCall（每次模型调用；reserved/settled micros）
        │
        ├─(n) Evidence（run_id, source_uri, source_version, content_hash, start_line/end_line, tool_call_id）
        ├─(n) RepoToolCall（tool, args_hash, status, result_hash, result_version, evidence_id, error_code）
        ├─(n) Claim（section, statement, evidence_id, quote, start/end_line）
        └─(n) Action(type=draft_pr/issue_comment) ──(0..1) Approval(payload_hash)
                                                └─(1) Outbox → Dispatcher → Transport
```

`app.evidence` 与 `app.repo_analysis_claims` 通过 `evidence_id` 关联；`app.repo_tool_calls.result_hash` 指向 CAS 内容寻址产物 `.local/artifacts/<hash[:2]>/<hash>`。`app.usage_calls.status` 为 `reserved|known|settled` 等，`result` 保存模型原始响应；未结算记录会阻止 resume（`RECOVERY_BLOCKED`）。

### 3.2 Run 状态机（当前实现）

`queued → running → succeeded | partial(waiting_input) | failed | cancelled`，暂停为 `paused`（`repo-pause` 递增 `execution_epoch`），恢复重生成为 `queued` 并沿用同一 `thread_id` 与固定 Commit。

分析 Run 的 `stop_reason` 取值包括 `INPUT_TOKEN_BUDGET`、`BUDGET_EXHAUSTED`、`DEADLINE_EXCEEDED`、`MODEL_INVALID_RESPONSE`、`GITHUB_RATE_LIMITED` 等；部分结束一律 `outcome=partial_report` + `waiting_input`，不得标记为成功。

---

## 4. 不变量（后续任务卡不得违反）

1. **仓库与版本固定**：分析对象始终是 `owner/repo` + 40 位 Commit；分支名只能用于解析 Commit。
2. **只读与写入分离**：仓库读取只能经只读 MCP 白名单；任何外部写入必须落 `app.actions` 并取得与 `payload_hash` 精确匹配的 `approval`。
3. **单写者**：PostgreSQL 为唯一事实源；Engine 是 Run 状态的唯一全局业务写者；LangGraph 只持有图状态。
4. **预算不可重置**：费用按 Run 记账，失败与无效响应也计费；不得为通过验收而清零或复用过期许可。
5. **外部输入即数据**：README、Issue、PR 评论、代码注释中的指令不改变 Agent 决策契约。
6. **证据可机械核验**：`github://owner/repo@commit/path#Lx-Ly`、逐字 quote、行号区间必须与 CAS 内容一致；机械核验不证明语义正确。
7. **工程门与 live 门分开**：fixture 通过不等于真实能力通过，live 记录必须单列。
8. **失败必须留存**：失败尝试的日志与原因不得删除或覆盖。

---

## 5. V3.3/3.4 复用点与新增点（B00 结论）

### 5.1 复用

- 工具网关与错误码：`repository_analysis/tools.py`（`GITHUB_NOT_FOUND`、`GITHUB_RATE_LIMITED`、`GITHUB_FORBIDDEN`、`TOOL_PATH_NOT_EXACT`、`TOOL_RANGE_OUTSIDE_FILE`）。
- 证据与报告：`report.py::verify_report`、Evidence ID、行区间读取 `read_file(path, start_line, end_line)`。
- 预算与恢复：`reserve_repository_call`/`settle_call`、Epoch 递增、checkpoint 恢复白名单 `v32-repo-v1`。
- Thread 追问：父 Run 证据映射复制（`prior_evidence_map`）。
- 发布骨架：`repair/publication.py` 的三步分段日志、`verify_head`、部分发布后的只读查找。

### 5.2 新增（按任务卡逐张实现，不在此提前落地）

- 仓库语义层（模块/入口/调用链元数据）与 PR 历史、Issue 归因。
- 计划/调查计划产物、Claim/矛盾/未知项结构、Context Compiler、增量读与断点续读。
- 修复补丁生成（不依赖 VCS）、补丁应用与测试、真实 Draft PR 发布 profile、PR CI 校验与自动修正循环。
- 新的 `verification.json` 卡号与新表迁移（编号从 `0021` 起）。

---

## 6. 任务卡与验收记录规范

### 6.1 目录约定

```text
v3/docs/v33/<卡号>-<主题>.md          # 设计与实现说明（入库）
v3/docs/v33/accept/<卡号>/accept.json # 验收摘要（入库）
v3/evidence/<卡号>/<UTC时间>-<卡号>/   # 原始日志（.gitignore，不入库）
```

`evidence/` 已在 `.gitignore` 中排除；`docs/v33/accept/` 入库以便审阅。原始日志通过 `accept.json` 的相对路径引用。

### 6.2 `accept.json` 必填字段

| 字段 | 含义 |
| --- | --- |
| `card`, `title`, `commit` | 卡号、标题、验收时代码 HEAD |
| `environment` | OS、Python、uv、PG/Redis 镜像与端口、是否 WSL2 |
| `commands[]` | `argv`、`exit_code`、`duration_seconds`、`log`（相对路径）、`status` |
| `engineering_gate` | `PASS`/`FAIL`，仅由工程命令决定 |
| `live_gate` | `NOT_RUN`/`NOT_APPLICABLE`/`PASS`/`FAIL`，真实付费调用单独列 |
| `deviations[]` | 环境偏差、临时绕过、失败尝试及原因 |
| `artifacts[]` | 生成的报告/哈希/表清单 |

### 6.3 判定规则

- 任一验收命令非零退出，`engineering_gate=FAIL`。
- pytest 出现 `skipped` 视为未覆盖（`verify.py` 同样按失败处理），不得记 PASS。
- `verify.py` 会比对验收前后 `source_fingerprint`，验收期间改动源码即 `source_changed_during_verification`。
- live 门只在用户明确授权下执行，并记录授权文件哈希、预算与累计费用。

---

## 7. B00 验收结果

证据目录：`v3/evidence/B00/20261109T062247Z-b00/`

| 命令 | 结果 |
| --- | --- |
| `git status --short` | 空；HEAD `e98e494f58f8844931d614d563cc6a43a6cf0ed5` |
| `make deps-up` | exit 0，postgres/redis healthy |
| `make migrate` | exit 0，`{'applied': []}`（已在 0020） |
| `make doctor` | exit 0，`{"postgres": "PASS", "redis": "PASS", "live": "NOT_RUN"}` |
| `uv run --frozen pytest tests/test_v32_repository_analysis.py -q` | 13 passed |
| `uv run --frozen pytest tests/test_v32_repository_analysis_db.py -q` | 4 passed |

失败尝试（已保留，未删除）：两次 `make deps-up` 因 `.env` 缺少 `MGAR_V3_DB_PASSWORD` 与 `.env` BOM 导致解析为空而 exit 2；对应日志在 `evidence/B00/20261109T062124Z-b00` 与 `20261109T062217Z-b00`。最终运行仅在进程环境内由现有 DSN 推导该变量，**未修改 `.env`**。

---

## 8. 已知偏差与风险

1. `.env` 早于 compose 的 `MGAR_V3_DB_PASSWORD` 约定，`make deps-up` 依赖外部注入；长期修复应在 `scripts/bootstrap.py` 补齐该键（B00 不改，避免覆盖既有凭据）。
2. `.env` 带 UTF-8 BOM，任何 shell 解析都需先剥离；已写入验收脚本。
3. Windows 原生缺少 `make`/`docker`/`uv`，B00 验收在 WSL2 完成；README 的 Windows 路径需在后续卡中补齐。
4. `DraftMCPTransport` 仅 fixture 可用；V3.4 真实发布必须先完成发布 profile 的审查与授权，不得复用 R1/R4 许可。
5. GitHub 代码搜索只覆盖默认分支；V3.3 需以固定 Commit 的目录/文件读取补齐证据链。
