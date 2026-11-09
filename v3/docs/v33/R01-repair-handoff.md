# R01 — Investigation-to-Repair Handoff

- 状态: 工程门 PASS（8 tests）
- 依赖: A04（真实复现回执）、M05（已验证经验与调查闭环）
- 迁移: 无
- 代码: `v3/src/mgar_v3/repair/handoff.py`
- 测试: `v3/tests/test_v34_repair_handoff.py`

## 1. 目标与边界

R01 把 V3.2 的仓库分析结果接到 R5 的修复能力上，但只允许一座受控的桥：
**只有 A04 已经复现，或在同一 Commit 上静态确认的 Finding，才能创建 Repair Run。**

Handoff 是一个不可变、内容寻址的契约值对象，不是一句"请修复这个仓库"的口头委托。
它携带任务书要求的十项字段，任何一项缺失或越界都会在创建时失败关闭。

明确不做：

- 不复制旧 R5 演示里的 `orders.go`、固定测试名或商城业务字段；
- 不在此卡实现 Worktree、Patch 编辑、修复验证或发布（R02–R05）；
- 不新增数据库迁移；Handoff 是纯值对象，持久化与执行由消费方负责；
- 不允许分析阶段继承来的只读 AnalysisSpec 直接升级为可写修复授权。

## 2. Handoff 十项字段

`RepairHandoff`（`v3/src/mgar_v3/repair/handoff.py`）包含：

| 任务书字段 | 模型字段 | 说明 |
| --- | --- | --- |
| Repository | `repository` | 与 Finding、父分析运行三方一致 |
| Base Commit | `base_commit` | Finding 复现时的 40 位 SHA |
| Finding ID | `finding_id` | 只接受 A04 推进后的 Finding |
| Relevant Evidence | `evidence` | Finding 证据注册表中的全部证据 |
| Reproduction Proof | `reproduction` | 验证器回执或静态确认，二者不可混用 |
| Repair Objective | `objective` | 去空白后的单行目标，长度受限 |
| Allowed Paths | `allowed_paths` | 仓库相对路径，必须覆盖缺陷位置 |
| Execution Budget | `execution_budget` | `AnalysisLimits`，受授权上限约束 |
| Authorization | `authorization` + `authorization_hash` | 显式、限时、限人、限仓库的修复授权 |
| Parent Run | `parent_run_id` + `parent_report_hash` | 已完成的 V3.2 分析与已验证报告 |

附加不变量：`graph_version="r5-repair-v1"`、`read_only=False`、
`finding_lifecycle`（落盘时的生命周期）、`handoff_id`（内容摘要）。

`handoff_id` 对规范化内容做 SHA-256；同样的输入得到同一个 ID，
目标、证据、预算或授权任意一项变化都会产生新的 ID。

## 3. 复现证明

`ReproductionProof` 只有两种合法形态：

1. `kind="reproduced"`：必须来自 A04 的 `test_run` 证据，且
   `source_uri` 为 `receipt://<receipt_id>`，证据内容哈希必须等于回执 ID。
   证明中保留回执 ID、全部测试证据与可重放命令（由回执描述携带）。
2. `kind="static_confirmation"`：只用扫描器/静态分析/调查/人工证据，
   显式禁止携带执行回执——静态确认不得伪装成"已复现"。

Finding 生命周期为 `suspected`、`investigating` 或 `not_reproduced` 时，
不存在可修复目标，创建 Handoff 返回 `HANDOFF_TARGET_UNVERIFIED`；
已经进入 `fix_proposed` 及之后的状态返回 `HANDOFF_TARGET_ALREADY_REPAIRED`。

## 4. 授权与预算

`RepairAuthorization` 是独立于环境开关的人类授权：

- `allow_repair` 必须显式为真；
- `expires_at` 必须带时区且尚未过期；
- `approved_by` 必须属于调用方给出的 `allowed_actors`；
- `repository` 必须命中 `allowed_repositories`（支持 `*`）；
- Handoff 的 `execution_budget` 必须被 `authorization.limits` 完全包含。

父分析运行必须是 `succeeded` 且持有已验证报告；`running`、`waiting_input`
或缺失 `report_hash` 一律返回 `HANDOFF_PARENT_NOT_SETTLED`。

## 5. 路径控制

Allowed Paths 从 Finding 的 `source_locations` 派生；调用方只能扩大范围，
不能缩小到遗漏缺陷位置。每个路径都经过：

1. 绝对路径、`~`、反斜杠、盘符冒号直接拒绝（不会先把 `/etc/passwd`
   归一化成 `etc/passwd` 再放行）；
2. `validate_manifest_path()` 拒绝 `..`、空段、`.git` 与控制字符；
3. `validate_repo_path()` 再做一次仓库路径校验。

任一违规返回 `HANDOFF_PATH_DENIED`。

## 6. 拒绝矩阵

| 条件 | 错误码 | HTTP |
| --- | --- | --- |
| 无显式授权 / 已过期 / 审批人不可信 / 仓库越界 | `HANDOFF_AUTHORIZATION_DENIED` | 403 |
| 修复预算超过授权上限 | `HANDOFF_BUDGET_EXCEEDED` | 403 |
| 路径越界或未覆盖缺陷位置 | `HANDOFF_PATH_DENIED` | 403 |
| 目标未验证或未复现 | `HANDOFF_TARGET_UNVERIFIED` | 409 |
| 目标已进入修复流程 | `HANDOFF_TARGET_ALREADY_REPAIRED` | 409 |
| 父分析与 Finding 仓库不一致 | `HANDOFF_REPOSITORY_MISMATCH` | 409 |
| 父分析与 Finding Commit 不一致 | `HANDOFF_VERSION_MISMATCH` | 409 |
| 父分析未完成或无已验证报告 | `HANDOFF_PARENT_NOT_SETTLED` | 409 |
| 证据为空或引用注册表外证据 | `HANDOFF_EVIDENCE_MISSING` | 409 |
| 复现证明缺失或形态不符 | `HANDOFF_REPRODUCTION_PROOF_MISSING` | 409 |
| 修复目标为空 | `HANDOFF_OBJECTIVE_REQUIRED` | 400 |

## 7. 通用性

`RepairHandoff` 不读取仓库内容，也不假设语言、测试框架或文件布局：

- Python 夹具用真实 `pytest` 复现 `src/calc.py` 的算术缺陷；
- Go 夹具用 `internal/mathx/calc.go` 的静态确认创建同构 Handoff；
- 测试显式断言序列化结果中不出现 `orders.go`、`TestPaidRevenueCents`
  等旧 R5 演示字段。

## 8. 测试与验收

验收命令：

```bash
uv run --frozen pytest tests/test_v34_repair_handoff.py -q
```

覆盖：

1. 真实 `pytest` 复现后创建 Handoff，十项字段齐全、回执可重放、ID 确定；
2. 静态确认创建 Handoff，证明类型为 `static_confirmation` 且无执行回执；
3. Go 仓库使用同一契约，Allowed Paths 来自 Finding 而非硬编码；
4. `suspected` / `investigating` / `not_reproduced` / 已修复目标全部拒绝；
5. Commit 与仓库不匹配分别拒绝；
6. 授权关闭、过期、审批人不可信、仓库越界、预算超限分别拒绝；
7. 父分析未完成、无报告、证据缺失或悬空引用分别拒绝；
8. 路径穿越、绝对路径、`.git` 与缩小范围全部拒绝，合法扩围成功。

结果：`8 passed`；Ruff 与 mypy strict 通过。
