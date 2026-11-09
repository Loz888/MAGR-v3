# A03 — Finding Store & Defect Discovery

- 状态: 工程门 PASS（12 tests）
- 依赖: A01 / A02
- 代码: `v3/src/mgar_v3/findings/`
- 测试: `v3/tests/test_v33_findings.py`

## 1. 目标与边界

A03 把「扫描器警告」和「模型怀疑」统一转换为可追踪的候选缺陷，并在进入后续修复流程前
用机械条件守住证据：同一条警告不重复计数，模型自述不能把候选缺陷升级为 `reproduced`。

明确不做：

- 不执行扫描器（A02 已负责）；A03 只消费 `ScanReport`。
- 不执行测试或复现命令（A04 负责）；`test_run` 证据由外部验证器提供。
- 不写数据库，不新增迁移；A03 验收只要求纯领域行为，存储以进程内 Store 与可序列化
  `FindingReport` 交付，后续卡片在其上持久化。
- 不允许 Agent 自己宣布验证成功：`InvestigationProposal` 即使声称 `reproduced`，也只会
  被拒绝并保留为 `suspected`。

## 2. Finding 契约

`Finding`（`v33-finding-v1`）必需字段：

| 字段 | 含义 |
| --- | --- |
| `finding_id` | 由 Run、仓库、Commit、去重键决定的 SHA-256 |
| `run_id` / `repository` / `commit` | 固定身份；仓库名规范化小写 |
| `category` | 扫描器或调查来源的类别（如 `vet`、`lint`、`test_failure`、`dependency`） |
| `classification` | `defect` / `quality` / `risk`，区分真实错误、质量建议与供应链风险 |
| `severity` | `error` / `warning` / `info` |
| `confidence` | 0–1，来源置信度 |
| `source_locations` | 至少一个仓库相对位置 |
| `evidence_ids` | 指向 `FindingEvidence` 的机械证据 |
| `reproduction_status` | `not_attempted` / `attempted` / `static_only` / `reproduced` / `not_reproduced` |
| `suggested_fix` | 修复建议，`fix_proposed` 时必须非空 |
| `lifecycle_status` | 生命周期状态 |
| `uncertainty` | 必填非空，明确当前结论的不确定部分 |
| `root_cause` / `fix_risk` / `dedupe_key` / `origin` | 根因线索、修复风险、去重键、来源 |

`FindingEvidence` 记录 `kind`、`source_uri`、`description`、`content_hash` 与位置。
`kind` 决定它能支撑哪一步：

- `scanner` / `static_analysis` / `investigation` / `manual`：可支撑 `confirmed_static`。
- `test_run`：唯一能支撑 `reproduced` 与 `not_reproduced` 的运行时证据。
- `test_run` / `patch_verification`：可支撑 `patch_verified`。

## 3. 去重语义

去重键 = `规范化路径 | 规范化规则 id | 规范化根因线索`：

- 同一路径、同一规则、同一根因：合并为一个 Finding，位置与证据累加，
  `duplicates_merged` 加一；同一 Scanner 警告不会变成多个独立 Bug。
- 同一路径、同一规则但根因不同：保留为两个 Finding，供后续调查区分。
- 同一根因但路径不同：保留为两个 Finding。

重复候选合并时取更高严重度、更高分类优先级（`defect > risk > quality`）、更高置信度和
更保守的 `fix_risk`；`finding_id` 保持不变。

## 4. 生命周期门

允许的状态迁移：

```text
suspected -> investigating
investigating -> reproduced | confirmed_static | not_reproduced
reproduced | confirmed_static | not_reproduced -> fix_proposed
fix_proposed -> patch_verified
patch_verified -> awaiting_approval
```

机械门：

| 目标状态 | 条件 |
| --- | --- |
| `reproduced` | 至少一条 `test_run` 证据 |
| `confirmed_static` | 任一静态/人工证据 |
| `not_reproduced` | 至少一次 `test_run` 尝试记录 |
| `fix_proposed` | `suggested_fix` 非空 |
| `patch_verified` | `test_run` 或 `patch_verification` 证据 |
| `awaiting_approval` | 保留已验证证据 |

跳步、回退或重复迁移均返回 `FINDING_TRANSITION_INVALID`；证据不足返回
`FINDING_EVIDENCE_REQUIRED`；缺少修复建议返回 `FINDING_FIX_REQUIRED`；
同一证据 id 被不同内容复用返回 `FINDING_EVIDENCE_CONFLICT`。

## 5. 发现与覆盖

`discover_scan_report(run_id, report)`：

1. 只读取 `status == completed` 的扫描结果，避免把跳过/失败当成“没有问题”。
2. 每个 `ScanFinding` 生成候选：确定性证据 id、`scan://` 来源、`scanner` 证据、
   非空不确定性说明。
3. 分类：`vet` / `test_failure` / `compile_error` / `security` / `bug` / `race` 等为
   `defect`；`dependency` / `ci` / `supply_chain` / `configuration` 为 `risk`；
   其余（如 `lint`）为 `quality`，严重度为 `error` 时视为 `defect`。
4. 返回 `FindingStore` 与 `ReviewCoverage`。

`ReviewCoverage` 保存分母和未扫描范围：

- `files_considered` / `files_scanned` / `unscanned_files`；
- `scanned_languages` / `unsupported_languages`；
- `scanner_statuses`；
- `unscanned_scopes`，例如 `language:Rust`、`files:4`、
  `scanner:dependency:failed:SCANNER_FAILED`、`scanner:go-race:skipped:RACE_CHECK_DISABLED`。
  `python-pytest` 的 `NO_TESTS` 不记为未扫描（仓库确实没有测试）。
- `complete` 仅在扫描报告完整、无未扫描文件且无不支持语言时为真。

## 6. 排序与报告

`FindingStore.findings()` 排序键：

1. 严重度：`error > warning > info`；
2. 可复现性：`patch_verified/awaiting_approval > reproduced/fix_proposed >
   confirmed_static > investigating > suspected > not_reproduced`；
3. 修复风险：`low < medium < high`（低风险优先）；
4. `finding_id` 保证稳定。

`FindingReport` 包含排序后的 Findings、证据、覆盖范围、接收候选数与合并数；
`model_dump_json()` 对相同输入逐字节一致，可回读校验。

## 7. 测试覆盖

`uv run --frozen pytest tests/test_v33_findings.py -q`（12 passed）：

- 契约字段与 8 个生命周期状态齐全；
- Go vet 与失败测试产生两个独立 Finding；
- 同一路径/规则/根因的重复警告合并为一个 Finding，位置与证据累加；
- 根因线索参与去重，路径不同仍独立；
- Ruff 质量建议、测试失败真实缺陷、依赖/CI 风险三类可区分；
- 无证据的模型猜测不能进入 `reproduced`（提案被拒，直接迁移返回证据错误）；
- 完整生命周期链路；静态确认与未复现各自需要证据；
- 按严重度、可复现性、修复风险排序；
- 覆盖范围显式列出不支持语言、未扫描文件、失败/跳过的扫描器；
- 报告确定性、JSON 往返一致；
- 非法身份与证据 id 内容冲突 fail closed。
