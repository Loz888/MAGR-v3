# E01 — Real Repository Evaluation

- 状态: 工程门 PASS（`tests/test_v34_evaluation.py` 18 tests；离线 Harness 自检 15/15）
- Live 门: **NOT_RUN**（未授权付费模型调用与 GitHub 写入，A/B/C 全部未执行）
- 依赖: QEV-02（记录/Manifest/Validate/Summarize）、U01（工作台）、R05（发布契约）
- 代码: `v3/src/mgar_v3/quality_eval/v34.py`、`v3/scripts/quality_eval.py`
- 实验: `v3/experiments/v34-audit-repair/`
- 测试: `v3/tests/test_v34_evaluation.py`

## 1. 目标与边界

E01 要证明新增的审计、修复与记忆能力在**独立、可复现的真实公开仓库**上成立，
而不是复用固定订单场景。本卡片交付：

1. 4 个不同结构的真实公开仓库、固定 commit 的逐文件快照与 SHA-256；
2. 12 个 review、3 个 repair、3 个 negative（其中 3 个 holdout）共 18 个任务；
3. 每个 review/repair 任务都有可核验答案：真实 upstream bug commit、行号区间、
   原文 quote hash、真实 upstream fix patch 与 fix commit；
4. 3 个 repair 任务有真实 upstream patch + 独立 regression test，已验证
   buggy 失败 → fixed 通过 → 应用 reference patch 后与 fixed 文件逐字节一致；
5. A/B/C 配对记录、判定口径与汇总产物；
6. 离线 Harness 自检（scope=`harness_conformance_not_model_capability`）。

明确不做：

- 不调用任何真实模型，不产生 token、成本或延迟数字；
- 不向 GitHub 写入、不创建真实 Draft PR；
- 不把离线 Harness 自检当作模型能力成绩，也不计入 A/B/C 成功率；
- 不在未授权时把任何 live 用例填成 PASS。

## 2. 数据集

| repo | 语言 | license | 固定 commit | 任务 |
| --- | --- | --- | --- | --- |
| `pallets/click` | python | BSD-3-Clause | `3cbcf9b` `6d5bac5` `8c1a0a7` `bb695aa` `8abb77a` + 3 个 fixed commit | review 4、negative 1、repair 3 |
| `psf/requests` | python | Apache-2.0 | `bc7dd0f` `1b40fdd` `661970d` | review 2、negative 1、holdout 1 |
| `spf13/pflag` | go | BSD-3-Clause | `f8a9906` `d26a4dc` `2084408` `c966cfe` | review 2、holdout 2 |
| `golang/example` | go | BSD-3-Clause | `7a4eec5` | review 1 |

快照来自 GitHub codeload tarball，文件逐字节复制；`repositories.json` 记录每个
commit 下每个文件的 SHA-256。`validate --check-files` 会重算并比对。

### 2.1 Dev 任务（15，参与离线自检）

| task_id | 类型 | 缺陷位置 | 真实 fix |
| --- | --- | --- | --- |
| `click-sentinel-identity` | review | `_utils.py:18-19` | `4295457` copy/deepcopy/pickle of Sentinel |
| `click-short-help-abbreviation` | review | `utils.py:86-96` | `f67c2bb` short help abbreviation |
| `click-help-name-collision` | review | `core.py:1168-1174` | `b67832c` parameter named help |
| `click-show-envvar-sequence` | review | `core.py:3271-3275` | `8c73ff1` show_envvar sequence |
| `requests-content-type-parameter` | review | `utils.py:517-525` | `f0198e6` malformed Content-Type |
| `requests-netrc-empty-default` | review | `utils.py:233-237` | `4791422` empty netrc entry |
| `pflag-unknown-flag-non-interspersed` | review | `flag.go:1277-1285` | `9a98e1e` non-interspersed unknown flag |
| `pflag-count-preserve-on-error` | review | `count.go:19-21` | `7d0f377` preserve count on error |
| `pflag-native-int-overflow` | review | `int.go:17`、`uint.go:17` | `70f7745` native int width |
| `goexample-weave-newline` | review | `weave.go:87` | `ca27133` missing newline |
| `click-negative-no-subprocess` | negative | `core.py` | 禁止模式不存在 |
| `requests-negative-no-dynamic-eval` | negative | `utils.py` | 禁止模式不存在 |
| `click-sentinel-repair` | repair | 同 sentinel | `4295457` |
| `click-short-help-repair` | repair | 同 short help | `f67c2bb` |
| `click-help-name-repair` | repair | 同 help name | `b67832c` |

### 2.2 Holdout（3，未参与开发与离线自检）

| task_id | 类型 | 缺陷位置 | 真实 fix |
| --- | --- | --- | --- |
| `requests-encode-files-getattr` | review | `models.py:237-244` | `6f205ff` `__getattr__` file wrappers |
| `pflag-ipv4-mask-ipv6` | review | `ipmask.go:56` | `a044bc7` reject IPv6 as IPv4 mask |
| `pflag-negative-no-unsafe` | negative | `string.go` | 禁止 `unsafe`/`uintptr` |

Holdout 与 dev 的 task_id 不相交，`harness-selftest.json` 明确记录
`holdout_executed=false`；`test_holdout_is_isolated_from_development` 守护该边界。

## 3. 答案与判定

每个 review finding 的答案包含：

- `file` / `symbol` / `category` / `start_line` / `end_line`；
- `quote` 原文与 `quote_sha256`（构建时从固定快照重算）；
- `call_chain`（≥2 个完全限定符号）；
- `fix_evidence.buggy_tokens`（必须出现在 patch 的 context/removed 行）与
  `fixed_tokens`（必须出现在 added 行）。

`v34.validate_bundle(..., check_files=True)` 会校验：仓库/commit 为 40 位十六进制、
快照文件存在且 hash 一致、quote 区间越界即失败、fix patch 必须包含两侧 token、
repair 的 buggy/fixed 快照与测试文件存在、dev/holdout 不重叠。

Review 判定口径：

| 指标 | 口径 |
| --- | --- |
| `finding_precision_at_k` | 前 K 条命中的 gold 数 / 实际前 K 条数 |
| `recall_at_budget` | 前 K 条命中的 gold 数 / gold 总数 |
| `unverified_false_positives` | 非 gold 且 quote 无效的 finding 数 |
| `source_citation_validity` | 有效 quote 数 / findings 数 |
| `call_chain_correctness` | 命中 gold 且调用链完全一致的 finding 数 / findings 数 |
| `scan_coverage` | 声明已扫描的 scope 文件数 / scope 可扫描文件数 |
| `passed` | recall=1、误报=0、citation=1、call chain=1、coverage=1、findings≤budget |

Repair 判定为二元门：`bug_reproduction_success`、`patch_validation_success`、
`independent_test_pass`、`draft_pr_correctness` 全部为真且
`regression_failures=0` 才 PASS；同时记录 `latency_ms`、`model_calls`、
`tool_calls`、`cost_micros`、prompt/completion tokens。

Negative 判定：返回空列表即 PASS；任何 finding 必须有可验证 quote，否则
`unverified_false_positives` 增加且 PASS 失败。

记忆指标（`grade_memory`）：`recall_at_k`、`stale_rejection`、
`contamination_rate`、`token_reduction`、`repeated_tool_call_reduction`、
`retrieval_cost`；`correctness_delta` 按 `case_id` 配对 A 与 C，输出
`with_memory` / `without_memory` / `difference_pp`。

## 4. A/B/C 设计与记录

| 臂 | 记忆 | 压缩 |
| --- | --- | --- |
| A | disabled | disabled |
| B | retrieval only | disabled |
| C | retrieval + writes | enabled |

同一 `case_id`、同一固定 commit、同一模型、同一预算、同一判定器；
`pairing_key = (case_id, repeat_index)`。18 个任务 × 3 臂 = 54 条记录，
`pair_coverage` 要求三臂完整且无散落记录。

`cases.jsonl` 中 54 条记录全部为：

- `mode=real_model_live`、`status=NOT_RUN`、`failure_kind=null`、`latency_ms=null`；
- `model_calls=0`、`usage_source=not_applicable`、`cost_micros=0`；
- `not_run_reason` 明确写明缺少 `paid_model_calls` 与 `github_draft_pr_writes` 授权。

`test_all_live_arms_are_not_run` 断言不存在任何 PASS/FAIL，防止把未执行包装成通过。

## 5. 离线 Harness 自检

`harness-selftest.json` 只跑 15 个 dev 任务：

- review：重新计算 quote hash、检查 fix patch 两侧 token、运行 4 个行为验证器
  （sentinel identity、short help 缩写、`help` 参数冲突、Content-Type 参数）；
- repair：buggy 快照测试失败 → fixed 快照通过 → 应用 reference patch 后与
  fixed 文件逐字节一致 → patched 测试通过；
- negative：禁止模式在快照中不存在。

结果 15 PASS / 0 FAIL，`scope=harness_conformance_not_model_capability`，
`holdout_executed=false`。这证明判定管线与固定答案一致，**不是**模型能力成绩。

## 6. 复现

```bash
cd v3
uv run --frozen pytest tests/test_v34_evaluation.py -q
uv run --frozen python scripts/quality_eval.py validate --experiment experiments/v34-audit-repair --root . --check-files
uv run --frozen python scripts/quality_eval.py summarize --experiment experiments/v34-audit-repair
uv run --frozen python scripts/verify.py --task E01
```

`validate` 校验 manifest、记录一致性、v34 资产 hash 与答案证据；`summarize`
生成 `summary.json` / `summary.md` 并附加 `v34` 段。全部产物由
`cases.jsonl`、`dataset.json`、`manifest.json` 复算。

## 7. 偏差与后续

- Live A/B/C 未执行：缺少付费模型与 GitHub 写入授权；获得逐次明确授权后，
  用同一 `pairing_key` 填入真实记录，`NOT_RUN` 才可变为 PASS/FAIL。
- 离线自检不覆盖模型行为，只覆盖判定器、答案与补丁应用；
- 3 个 repair 任务的独立测试由本轮编写，未复用 upstream 测试，避免答案泄漏；
- 快照只保留任务相关文件，不是整仓克隆；`repositories.json` 记录的是快照内文件。
