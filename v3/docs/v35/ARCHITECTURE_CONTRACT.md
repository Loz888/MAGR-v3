# RepoSage / MAGR-v3 V3.5 架构契约（B00 审计结论）

- 基线：`6cf67f404d8575d5e02f0dd0edfcbfb67a512ddc`
- 状态：B00 完成。本文件是 V3.5 所有卡的共同前提；后续卡只能新增能力，不得默默改变这里列出的权威边界与不变量。
- 证据：`v3/evidence/v35/B00/<run>/`（调用点清单、grep 日志、测试清单）。

---

## 1. 状态所有权

| 状态 | 唯一权威 | 存储 | 说明 |
| --- | --- | --- | --- |
| Business Task / Finding / Approval / Publication（Workbench） | PostgreSQL `app.workbench_*` | `workbench_tasks`、`workbench_events`、`workbench_findings`、`workbench_memory`（迁移 `0022`） | 由 `WorkbenchStore` 直接写入；目前尚无 Epoch/Fence，只有 `revision` |
| Run / Ticket / Mandate / Budget / Usage / Action / Approval / Outbox | PostgreSQL | `coordination/engine.py` 管理的 `app.runs` 等表 | Engine 是唯一全局业务写者；`begin_run` 递增 `execution_epoch`（`engine.py:425`） |
| 单 Run 图状态 | LangGraph Checkpointer | Checkpoint 存储 | 只保存图节点状态，不承载业务事实 |
| 可重建缓存 | Redis | 缓存键 | 丢失后可重建，不作为证据 |
| 修复工作区 | 临时 Worktree（mkdtemp）+ `git` 树哈希 | 本地文件系统 | `repair/worktree.py`，不改宿主仓库 |
| 内容寻址产物 | Artifact CAS | `app.artifacts` + `.local/artifacts/<hash[:2]>/<hash>` | 证据与补丁内容按 SHA-256 寻址 |

结论：V3.5 不得新建平行状态机；Workbench 的新状态必须继续写 PostgreSQL，并在 U01 收敛到 Engine 授权的事务边界与 Epoch Fence。

---

## 2. 现有真实调用链

### 2.1 Live Audit（真实只读）

```text
CLI mgar workbench --action create（cli.py:311）
  → POST /api/v3/workbench/tasks（api.py:540）
    → WorkbenchService.create（service.py:227）
      → _authority_check：live 且 mode!=audit 直接 403（service.py:902）
      → RepositoryAnalysisService.create（repository_analysis/service.py:183）
        → authorization()：读取 MGAR_V3_REPO_ANALYSIS_AUTHORIZATION_PATH（service.py:137）
        → validate_scope(repository, limits) + 只读凭据存在性检查（service.py:189-205）
        → 只读 MCP 解析 Commit（tools.resolve_commit）
        → RepositoryAnalysisStore.create
      → WorkbenchStore.create_task（workbench/store.py:25）→ status=auditing + audit_run_id（service.py:275）
  → 外部 Runner 执行分析 Run（RepositoryAnalysisService.execute，service.py:260）
  → GET /api/v3/workbench/tasks/{id}（api.py:557）
    → view()（service.py:795）→ _sync_live_audit（service.py:931）
      → _import_audit（service.py:950）：claims → WorkbenchFinding，含 evidence_id / quote / content_hash
      → _store_memory（service.py:1017）→ app.workbench_memory
```

缺口：live audit 只到 Finding；此链路**没有任何** Handoff、Patch、Test、Approval、Publication 步骤。

### 2.2 Fixture Repair（固定样例，非真实修复）

```text
WorkbenchService.create（fixture，service.py:284-308）
  → run(task)（service.py:312）
    → _start_audit（service.py:334）→ repo.create + repo.execute + _import_audit（service.py:365-367）
      → _fixture_findings（service.py:1077）：固定 dispatcher 缺陷 + 固定 reproduction_plan
    → _auto_repair（service.py:369）或 POST .../reproduce → _reproduce（service.py:397）
      → fixture-only 门禁（service.py:399-404）
      → _run_tests（service.py:1226）：临时目录写 FIXTURE_FILES，宿主 `sys.executable -m pytest`（service.py:1234）
    → POST .../repair → _prepare_repair（service.py:467,472）
      → _run_tests(patched=True) → seal_bundle（service.py:539）
      → ApprovalRecord(status=pending, payload_hash=...)
  → POST .../approve（api.py:619）→ approve()（service.py:590）
    → 校验 actor / stage / payload_hash（APPROVAL_STALE）
    → PublicationApproval.grant
    → _InMemoryRemote（service.py:639）→ DraftPRPublisher.publish（service.py:645）
    → PublicationRecord(remote="in_memory_fixture", pull_request_url="fixture://...")（service.py:649-661）
    → status=completed
```

### 2.3 独立 R04（结构化补丁代理，未接入 Workbench）

```text
tests / 未来编排器 → RepairAgent.run（repair/repair_agent.py）
  → 基线复现（SandboxExecutor + 可信 Go/Python Adapter）
  → provider.propose → PatchPlan（FileEdit 列表，无 Shell 文本）
  → PatchWorktree.apply_patch（SHA/CAS、路径与保护文件校验）
  → 公开测试 + 隐藏验证（隐藏测试不进模型上下文）
  → RepairProposal（public_receipt / verification_receipt / diff / changed_files）
```

事实：`repair_agent.py` 在生产源码中只被自身导入；`WorkbenchService` 未使用它。

### 2.4 独立 R05（发布骨架）

```text
scripts/r5_live.py（操作员分阶段脚本）
  → LivePermit（profile= r5-repair-v1，action_type=draft_pr）
  → LiveDraftMCPTransport（repair/publication.py:195）
    → Engine Action/Approval/Outbox → Dispatcher
    → GitHub MCP：create_branch → push_files → create_pull_request（仅 Draft，不 merge）
```

另一套 `repair/pr_delivery.py` 的 `MCPReadTransport` / `MCPWriteTransport`（pr_delivery.py:351,437）实现了读写分离与三步回执，但生产代码没有接线，只有测试使用。R05 的真实发布范围被 `r5-repair-v1` 限制在单一示例仓库与单一 Go 函数路径。

---

## 3. 固定 fixture 组件与宿主执行的**全部**可达入口

| 组件 | 定义 | 可达入口 | 当前是否可能触达真实任务 |
| --- | --- | --- | --- |
| `FIXED_DISPATCHER` | `workbench/service.py:79` | `_fixture_files`（`service.py:1270`）→ `_run_tests`（1226）与 approve 的 `_InMemoryRemote` 初始树（639-643）；`_fixture_findings` 引用其内容（1078） | 否。`_fixture_files` 只在 fixture 修复/复现路径被调用 |
| `_InMemoryRemote` | `workbench/service.py:148` | 仅 `approve()` 内实例化（`service.py:639`） | 目前仅 fixture 任务能走到 `waiting_approval`。但 `approve()` 自身没有 `execution_mode` 检查，属纵深防御缺口（见 G3） |
| 宿主 `pytest` | `_run_tests`（`service.py:1226`，命令在 1234） | `_reproduce`（424）、`_prepare_repair`（497）；`repair()`（467）与 `_auto_repair`（369）间接调用 | 否。`_reproduce` 有 fixture-only 门禁（399）；`_prepare_repair` 依赖已复现 Finding，而该状态只能由 fixture 复现产生 |
| `_safe_env` | `service.py:92` | 仅 `_run_tests`（1243） | 宿主环境白名单；不注入密钥，但仍是宿主执行，不是容器 |

---

## 4. 运行模式与授权标注（B00 必填）

| 规范名称 | 现有机制 | 授权产物 | 当前覆盖范围 | 状态 |
| --- | --- | --- | --- | --- |
| `fixture` | `execution_mode="fixture"` | 无（本地固定 fixture） | Workbench 调查 + 修复 + 内存发布 | 已实现，仅工程证据 |
| `live_readonly` | `execution_mode="live"` 且 `mode="audit"` | `AnalysisAuthorization`（`approved_by`、`expires_at`、`allowed_repositories`、预算）+ GitHub 只读凭据 | 真实仓库只读调查 | 已实现；真实运行记录见 V3.2 live 报告 |
| `live_repair_authorized` | **不存在于 Workbench**；R05 有 `RepairAuthorization` + `LivePermit` + `r5-repair-v1` | 时间/actor/仓库/预算绑定的 Repair 授权 | 仅 R05 单一示例路径 | Workbench：`NOT_RUN`（403 拦截） |
| `live_publication_authorized` | R05 `LiveDraftMCPTransport`（`publication.py:195`）+ `LivePermit.action_type=draft_pr` | PublicationApproval（payload_hash/tree_hash/diff_sha）+ Engine Action/Outbox | 仅 R05 受审 profile；Workbench 无 | Workbench：`NOT_RUN` |

规则：任何模式不得只凭布尔值（如 `allow_external_write=true`）隐式放权；必须有可校验的授权文件、范围、预算、有效期与审批哈希。

---

## 5. 唯一契约（V3.5 统一）

以下定义以现有模型为准；V3.5 各卡只能扩展字段，不能改变语义或另立同名模型。

### 5.1 Task

- 对应：`app.workbench_tasks`（`migrations/0022_workbench.sql:5`）+ `WorkbenchStore`。
- 必备字段：`task_id`、`thread_id`、`parent_task_id`、`request_key`、`request_digest`、`mode`、`repository`、`resolved_commit`、`execution_mode`、`coverage`、`limits`、`status`、`stage`、`audit_run_id`、`repair_run_id`、`revision`。
- 不可被模型/Memory 修改：`repository`、`resolved_commit`、`execution_mode`、`coverage`、`limits`、`request_digest`、`mode`。
- 现有状态：`queued / auditing / ready / reproducing / repairing / waiting_approval / publishing / completed / failed / paused / cancelled`（`domain.py:77`）；V3.5 目标状态机见 U01，需要前向迁移（从 `0023` 起）。

### 5.2 Finding

- 对应：`WorkbenchFinding`（`workbench/domain.py:321`）与 V3.3 `findings/model.py`。
- 必备字段：`finding_id`、`stage`、`severity`、`classification`、`confidence`、`risk_score`、`source{uri,evidence_id,quote,content_hash}`、`reproduction_plan`、`reproduction{receipt}`、`suggested_fix`、`fix`、`approval`、`publication`。
- 阶段只允许 `suspected → reproduced → patch_verified → waiting_approval → pr_created`（`domain.py:61-75`）。
- 不可修改：`finding_id`、`repository/commit`（由 Task 约束）、`source.evidence_id/content_hash`、已存在的 `reproduction.receipt`、已批准的 `approval.payload_hash`。

### 5.3 RepairHandoff

- 对应：`repair/handoff.py:180`（`RepairHandoff`）+ `RepairHandoffBuilder`（229）。
- 必备字段：`handoff_id`（内容寻址）、`repository`、`base_commit`、`finding_id`、`parent_run_id`、`parent_report_hash`、`evidence[]`、`reproduction`、`objective`、`allowed_paths`、`execution_budget`、`authorization`、`authorization_hash`、`execution_mode`、`graph_version`、`read_only`、`finding_lifecycle`。
- 不可修改：以上全部；任何变化都会改变 `handoff_id`，必须重新审批。当前 Workbench 尚未产生真实 Handoff（G5）。

### 5.4 PatchProposal

- 对应：`repair/repair_agent.py:151`（`RepairProposal`），输入 `PatchPlan`（74）与 `FileEdit`。
- 必备字段：`proposal_id`、`repository`、`objective`、`language`、`plan_summary`、`base_tree`、`tested_tree`、`diff`、`changed_files[]`、`public_receipt`、`verification_receipt`、`attempts_used`、`verified`。
- 不可修改：`base_tree`、`tested_tree`、`diff`、两份 `TestReceipt`、`proposal_id`。`verified=True` 只能由独立验证产生。

### 5.5 TestReceipt

- 对应：`workbench/domain.py:178`（`TestReceipt`）与 `repair/sandbox_executor.py` 的 `TestReceipt`。
- 必备字段：`receipt_id`、`command`、`exit_code`、`passed`、`tests[]`、`skipped`、`duration_ms`、`log_sha256`、`output_excerpt`（沙箱回执另含 image/image_id/policy）。
- 不可修改：`receipt_id`、`log_sha256`、`exit_code`、`passed`、测试集合。

### 5.6 Approval

- 对应：`workbench/domain.py:220`（`ApprovalRecord`）与 `repair/pr_delivery.py:109`（`PublicationApproval`）。
- 必备字段：`status`、`payload_hash`、`requested_at`、`actor`、`approved_at`、`approval_id`；发布审批还绑定 `repository`、`base_sha`、`tree_hash`、`diff_sha256`。
- 不可修改：`payload_hash`、`approval_id`、`approved_at`、`actor`。Payload 任何变化都必须使旧审批失效（`APPROVAL_STALE`）。

### 5.7 Publication

- 对应：`workbench/domain.py:237`（`PublicationRecord`）与 `repair/pr_delivery.py:182`（`PublicationOutcome` / `DraftPullRequest`）。
- 必备字段：`status`、`reason`、`steps[]`、`pull_request`、`pull_request_url`、`remote`、`published_at`。
- 不可修改：已写入的 `steps[]` 回执、`pull_request.number/head_sha/base_sha`。未确认的外部写必须记为 `unknown` 并只读对账（R04）。

---

## 6. 缺口与风险矩阵

| ID | 缺口 | 源码位置 | 现有测试覆盖 | 影响 | 关闭卡 |
| --- | --- | --- | --- | --- | --- |
| G1 | Live Repair 被 403 拦截，通用修复未接入 Workbench | `workbench/service.py:318-323,902-908` | `tests/test_v34_repository_api.py:625`（仅断言被拒） | 真实任务闭环不可用 | U02 + R03 |
| G2 | Fixture 修复依赖固定 `src/dispatcher.py` 与宿主 `pytest` | `service.py:79,1226-1276` | `tests/test_v34_repository_api.py` | 不能作为真实修复证据 | R02 |
| G3 | `approve()` 无 `execution_mode` 门禁，且发布不经 Outbox | `service.py:590-687` | 仅 fixture happy path | 纵深防御不足，无法对账/恢复 | R04 |
| G4 | R04 `RepairAgent` 无生产调用者 | `repair/repair_agent.py`（仅测试导入） | `tests/test_v34_repair_agent.py`（9 项） | 结构化补丁能力闲置 | R01 + R03 |
| G5 | Workbench Finding 未生成 `RepairHandoff` | `service.py:950` vs `repair/handoff.py:229` | `tests/test_v34_repair_handoff.py`（8 项） | Finding→修复无合法交接 | U02 |
| G6 | `pr_delivery` 的 MCP 读/写传输未接线 | `repair/pr_delivery.py:351,437`（仅测试） | `tests/test_v34_pr_delivery.py`（8 项） | Workbench 只能内存发布 | R04 |
| G7 | Workbench 状态写入绕过 Engine，无 Epoch/Fence | `workbench/store.py:25-221` | 无并发/旧 Worker 测试 | 并发与恢复正确性无保证 | U01 |
| G8 | 修复/发布 Memory 仅进程内 | `repair/repair_agent.py`、`pr_delivery.py:234` | 对应单测 | 经验不能跨 Run 复用 | M01 |
| G9 | 上下文压缩为同步确定性操作 | `context/compactor.py` | `tests/test_v33_context_compaction.py`（10 项） | 长任务成本/稳定性未验证 | M02 |
| G10 | E01 评测非完整 Agent Loop | `quality_eval/v34.py` | `tests/test_v34_evaluation.py`（18 项） | 不能据此宣称端到端能力 | E01 |
| G11 | 任务状态缺少 `waiting_input / exporting` 等显式阶段 | `migrations/0022_workbench.sql:21`、`workbench/domain.py:77` | 无 | 目标状态机无法表达 | U01 |
| G12 | CLI/Web 未区分 Suspected/Reproduced/Patch Verified 等证据层级 | `web/workbench.js`、`api.py:533-670` | `tests/test_v34_repository_api.py` | 用户可能误解“建议补丁”为已修复 | R05（V35） |

风险排序（高→低）：G1/G2/G3（真实性与发布安全）→ G7/G5（状态与交接正确性）→ G4/G6（能力接线）→ G8/G9（经验与上下文）→ G10/G12（评测与呈现）。

---

## 7. V3.5 不变量

1. PostgreSQL 是唯一业务事实源；Engine 仍是外部写入与全局状态的唯一授权边界；LangGraph 只持有图状态；Redis 可重建。
2. 任务/修复的权威字段（`base_commit`、`allowed_paths`、`budget`、`epoch`、`approval_hash`、`execution_mode`）不可被 LLM、RAG、Memory 修改。
3. 外部写入必须经 `Action → Approval(payload_hash) → Outbox → Dispatcher`；结果未知先只读对账，禁止盲目重试。
4. 没有证据不等于成功：`suspected → reproduced → patch_verified → waiting_approval → pr_created` 不得跳级；`fixture` 通过不得记作 `live` 通过。
5. README/Issue/PR/RAG/Memory 一律是不可信数据；Prompt 过滤只是辅助，Tool Gateway 必须独立授权。
6. 修复执行只在受限 Docker（R03 固定镜像、禁网、非 root、只读根）中进行；宿主 `pytest` 不得用于 Live Repair。
7. 隐藏验证不可进入 RepairProvider 上下文；公共测试通过与独立验证通过是两份回执。
8. 工程门与 live 门分开记录；`NOT_RUN` 永远不得改写为 PASS；失败日志保留。
