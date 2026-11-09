# U01 — Unified Repository Workbench

- 状态: 工程门 PASS（15 tests）
- 依赖: M05（任务起点记忆召回）、R05（Draft PR 审批与发布）、M01–M04、R01–R04
- 迁移: `0022_workbench.sql`（任务、时间线、Findings、Memory 四张表）
- 代码: `v3/src/mgar_v3/workbench/{domain,store,service}.py`、`v3/src/mgar_v3/api.py`、`v3/src/mgar_v3/cli.py`
- 页面: `v3/web/workbench.{html,css,js}`
- 测试: `v3/tests/test_v34_repository_api.py`

## 1. 目标与边界

U01 把 Audit、Issue Repair、Audit & Repair 三种任务模式统一到同一个
Web / CLI / API 入口，并保证每个结论都能回到来源与执行回执：

1. 输入公开 GitHub 仓库 URL，固化解析后的 commit；
2. 显式声明扫描范围（全仓库或指定路径），并回报实际扫描/未扫描文件与语言；
3. 审计产出按风险排序的 Findings，单个 Finding 带来源 URI、Evidence、引文与内容摘要；
4. 复现必须由真实 `pytest` 子进程在未修复树上失败；
5. 补丁验证必须由同一测试在修复后通过，并记录 diff 与前后回执；
6. Memory 命中只作为参考展示，附来源，不改变工具、仓库、预算、审批或沙箱；
7. 人工审批绑定 `payload_hash`；发布只创建 Draft PR，工作台不存在合并操作；
8. 任务可 Pause / Resume / Cancel，历史任务可在同一 thread 内追问；
9. UI 严格区分 `Suspected`、`Reproduced`、`Patch Verified`、`Waiting Approval`、
   `PR Created`，并把“建议修复”和“已验证修复”分开显示。

明确不做：

- 不放宽 R05 的 Live 写权限：Live 修复与发布仍需要 R05 profile 与人工授权；
- 不接触真实 GitHub：fixture 任务使用内存 remote，PR 地址为 `fixture://`；
- 不合并、不评论、不强制推送；`PublicationRecord.merged` 固定为 `False`；
- 不把 Memory 命中当作事实或权限，不因命中跳过证据门禁；
- 不修改 M01–M05、R01–R05 的既有行为，只组合它们的公开能力。

## 2. 三种模式

| 模式 | 审计 | 自动修复 | 说明 |
| --- | --- | --- | --- |
| `audit` | 是 | 否 | 产出 Findings，全部停留在 `Suspected` |
| `issue_repair` | 否 | 是 | 以 Issue 为单一来源播种 Finding，直接复现与补丁验证 |
| `audit_and_repair` | 是 | 是 | 审计后选择风险最高且带复现计划的 Finding 进入修复 |

三种模式共用同一 API、CLI 与页面；CLI 通过本地 API 调用，和未来 Web 客户端
走完全相同的鉴权与校验路径。`issue_repair` 必须提供 `issue_number`，其他模式
的 `task` 至少 3 个字符；Live 模式只允许 Audit 自动启动修复流程。

创建请求使用 `client_request_id` 做幂等：相同请求返回同一任务，同一 ID 配不同
内容返回 `REQUEST_ID_REUSED`（409）。

## 3. 扫描覆盖

`CoverageRequest` 支持 `scope=repository` 与 `scope=paths`：

- `include_paths` / `exclude_paths` 按仓库相对路径校验，拒绝绝对路径、`..`、
  隐藏段、反斜杠与冒号；
- fixture 审计回报 `files_considered`、`files_scanned`、`unscanned_files`、
  `languages` 与 `complete`；
- 审计产出的 `evidence_id`、`source_uri` 与 `content_hash` 直接复用 M05 的证据
  注册表，不做二次编造。

## 4. 五阶段证据门禁

```
Suspected ──失败测试回执──> Reproduced ──通过测试回执 + diff──> Patch Verified
    └──────────────── 审批绑定 payload_hash ──> Waiting Approval ──Draft PR 三步回执──> PR Created
```

- `FixRecord` 只有在“修复前失败、修复后通过”两份回执命令与测试节点一致、
  `diff_sha256` 匹配、文件列表与 bundle 一致时才可构造；
- `derive_stage` 从原始证据反推阶段，`WorkbenchFinding` 构造时强制
  `stage == derive_stage(...)`；
- `advance_finding` 只允许单步前进，任何跳级返回 `FINDING_STAGE_INVALID`（409）；
- `suggested_fix` 与 `fix` 是两个字段：只有建议时 `fix_state=proposed_not_verified`、
  `verified=False`、`suggested_fix_is_not_applied=True`，阶段仍是 `Suspected`；
- 未复现的 Finding 调用 repair 返回 `FINDING_NOT_REPRODUCED`（409），
  不产生 diff。

## 5. Agent 执行时间线

`app.workbench_events` 追加式记录 `task`、`audit`、`agent`、`reproduce`、
`repair`、`approval`、`publication`、`control`、`follow_up` 阶段的事件，
包含状态、摘要与结构化 payload（receipt ID、log 摘要、PR 步骤等）。

- Audit 期间展示 M05 的 agent 工具步骤（最多 20 条）；
- 复现、补丁验证、审批、发布分别落事件，前端按顺序渲染；
- Pause / Resume / Cancel 同时写入 `control` 事件，并尽力同步底层分析运行；
- 审批后展示 branch → commit → pull_request 三步回执。

## 6. Memory 命中与来源

审计完成后调用 M05 的 `recall_task`，把结果存入 `app.workbench_memory`：

- `retrieval_mode`（如 `lexical` / `hybrid`）与 `follow_up` 标记；
- `advisories`、`plan_hints`、`confirmed_facts`、`excluded` 四组命中；
- 每条命中保留来源 URI、Evidence、置信度与验证状态。

页面明确标注“Memory 仅作参考，不改变权限与证据门禁”。子任务追问继承同一
thread 与仓库、commit、覆盖范围，并使用 follow-up 模式的 Memory 召回。

## 7. 审批与发布

复用 R05 的发布契约：

1. `seal_bundle` 生成审查对象与 `payload_hash`；
2. 审批请求必须携带当前 `payload_hash`，不匹配返回 `APPROVAL_STALE`（409）；
3. 审批通过后 `PublicationApproval.grant` 绑定 actor、仓库、base、tree 与 payload；
4. `DraftPRPublisher` 使用内存读/写 remote，依次写 branch、commit、Draft PR，
   每步记录回执；
5. 结果写入 `PublicationRecord`：`status=published`、`merged=false`、
   `pull_request.draft=true`、`pull_request_url=fixture://github/<repo>/pull/<n>`；
6. 任何写失败保持 `Waiting Approval`，不把失败包装成已发布。

真实 GitHub 写入仍由 R05 的 profile 与写令牌控制，U01 不读取也不放宽这些凭据。

## 8. 控制与追问

| 操作 | 允许状态 | 结果 |
| --- | --- | --- |
| `run` | queued / ready / failed | 启动或继续执行 |
| `pause` | queued / auditing / reproducing / repairing | `paused` |
| `resume` | paused | `queued` |
| `cancel` | 未完成或未取消 | `cancelled`，并尽力取消底层运行 |

非法转换返回 `TASK_TRANSITION_INVALID`（409）。历史追问创建同 thread 的子任务，
`parent_task_id` 指向父任务；父任务存在 Audit 运行时复用 M05 的 follow-up 运行。
子任务完成审计后同样展示 Findings、来源、覆盖与 Memory。

## 9. Web / CLI / API

| 面 | 入口 |
| --- | --- |
| Web | `GET /workbench`（静态页面，CSP 限制脚本来源） |
| API | `GET /api/v3/workbench/profile`、`POST/GET /api/v3/workbench/tasks`、`.../{id}`、`.../run`、`.../findings/{fid}`、`.../diff`、`.../reproduce|repair|approve`、`.../messages`、`.../{pause|resume|cancel}` |
| CLI | `mgar workbench --action create|list|status|run|finding|reproduce|repair|diff|approve|pause|resume|cancel|message` |

所有 API 使用 `X-MGAR-Token` 本地鉴权；页面只通过同一 API 读取，不做任何
客户端侧的状态推断。

## 10. 测试与验收

验收命令：

```bash
uv run --frozen pytest tests/test_v34_repository_api.py -q
```

覆盖：

1. 五阶段标签集合、`suggested_fix` 不推进阶段、跳级被拒绝、完整证据链逐级到达
   `PR Created`；
2. `FixRecord` 拒绝“修复前也通过”的回执；无审批的发布回执被拒绝；
3. 风险排序：error/defect 在 warning/risk 与 info/quality 之前；
4. Audit：Findings 风险排序、单一来源（URI/Evidence/引文/摘要）、覆盖统计、
   Memory 命中、时间线、任务列表；
5. 复现 → 修复 → Diff 审查 → 错误 payload hash 被拒 → 正确审批生成 Draft PR，
   任务完成且 `merged=false`；
6. 建议修复在未复现时 repair 返回 409 且 diff 不存在；
7. Issue Repair 从 Issue 播种并自动完成补丁验证，停在人工审批；
8. Audit & Repair 自动选择最高风险 Finding 并停在 `Waiting Approval`；
9. Pause / Resume / Cancel 状态机与同 thread 追问；
10. 模式、仓库 URL、覆盖路径、`client_request_id` 幂等与冲突、Live 修复 403。

结果：`15 passed`；Ruff 与 mypy strict 通过。

## 11. 已知边界

- fixture 修复只覆盖 `src/dispatcher.py` 的单一示例缺陷；真实仓库修复仍由
  R01–R04 的授权与沙箱链路完成；
- Audit 的 Live 路径复用 M05，不新增网络读取能力；
- 发布使用内存 remote，不产生真实 GitHub 对象；
- 任务暂停只在步骤边界生效，不会中断正在运行的 pytest 子进程；
- 同一 thread 的追问共享父任务的仓库与 commit，不支持跨仓库追问。
