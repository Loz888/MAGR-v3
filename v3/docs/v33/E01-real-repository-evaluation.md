# E01 — Real Repository Evaluation

- 状态: 工程门 PASS（`tests/test_v34_evaluation.py` 18 tests；离线 Harness 自检 15/15）
- Live 门: **已执行**（`deepseek-flash`，54/54 次真实调用，0 NOT_RUN；A 9/18、B 10/18、C 10/18；
  正式 run 保守计费 USD 0.785467，全部 paid run 累计 USD 0.928086，低于授权上限 USD 2）
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

- 不向 GitHub 写入、不创建真实 Draft PR；
- 不把离线 Harness 自检当作模型能力成绩，也不计入 A/B/C 成功率；
- 不在未执行时把任何 live 用例填成 PASS；
- 真实模型调用只在本轮用户授权（`deepseek-flash`，总预算 USD 2）内执行，
  每次调用记录 prompt/completion tokens、延迟与保守费用。

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
| `call_chain_correctness` | 命中 gold 且答案调用链以 gold symbol 结束（≥2 个完全限定符号）的 finding 数 / 命中 finding 数 |
| `scan_coverage` | 声明已扫描的 scope 文件数 / scope 可扫描文件数 |
| `passed` | recall=1、precision=1、误报=0、citation=1、chain=1、coverage=1、findings≤budget |

`call_chain_correctness` 只要求调用链把缺陷符号放在最内层（最后一项），不要求复现固定的中间帧：
真实上游修复前后的调用路径与外部库帧无法仅凭快照唯一重建，逐字符相等会把“记住合成答案”
误当成“分析正确”。该指标仍作为 `passed` 的必要条件之一，只是判定取值按“最后一项等于
gold symbol 且链长 ≥2”计算。

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

### 4.1 正式 run

- run_id：`live-20261109T122858Z`（产物在 `experiments/v34-audit-repair/runs/` 下）；
- 模型：`deepseek-flash`，`temperature=0`、`response_format=json_object`、
  `max_tokens=16384`；
- 预算：调用前按 worst-case 预留（`chars//3+512` 输入 + 16384 输出），调用后按 provider
  usage 结算；超过 2,000,000 micros 抛 `BudgetExhausted` 并停止，不做超预算调用；
- 正式 run：54 次调用、0 重试、0 transport error、0 NOT_RUN；
- Tokens：输入 782,847，输出 458,886；
- 保守计费：785,467 micros（USD 0.785467），未使用 provider 缓存折扣。

| 臂 | PASS | FAIL | NOT_RUN | 成功率 |
| --- | ---: | ---: | ---: | --- |
| A | 9 | 9 | 0 | 9/18 = 0.5000 |
| B | 10 | 8 | 0 | 10/18 = 0.5556 |
| C | 10 | 8 | 0 | 10/18 = 0.5556 |

| 臂 | prompt tokens | completion tokens | cost micros | 延迟 P50 / P95 (ms) |
| --- | ---: | ---: | ---: | --- |
| A | 260,197 | 153,589 | 262,348 | 39,441 / 94,778 |
| B | 261,868 | 159,092 | 269,453 | 48,924 / 92,764 |
| C | 260,782 | 146,205 | 253,666 | 36,315 / 98,589 |

| 臂 | Precision@K | Recall@Budget | 未验证误报 | Citation | Chain | Coverage |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| A | 0.6000 | 0.6000 | 0 | 1.0000 | 1.0000 | 0.8000 |
| B | 0.6667 | 0.6667 | 0 | 1.0000 | 1.0000 | 0.8000 |
| C | 0.6667 | 0.6667 | 0 | 1.0000 | 1.0000 | 0.8000 |

Repair：每臂 3 个用例，`bug_reproduction_success=3`、`patch_validation_success=0`、
`independent_test_pass=0`、`draft_pr_correctness=0`、`regression_failures=3`。

配对（18/18 三臂齐全）：A/B/C 正确率 = 0.5000 / 0.5556 / 0.5556；
B、C 相对 A 的输入 token 均值增量 = +92.83 / +32.50；成本均值增量 =
+394.72 / −482.33 micros；C 相对 B 的每用例上下文 token 缩减 = 60.33。

### 4.2 逐任务结果

| task_id | 类型 | A | B | C |
| --- | --- | --- | --- | --- |
| `click-sentinel-identity` | review | FAIL | FAIL | FAIL |
| `click-short-help-abbreviation` | review | PASS | PASS | PASS |
| `click-help-name-collision` | review | FAIL | FAIL | FAIL |
| `click-show-envvar-sequence` | review | FAIL | FAIL | FAIL |
| `requests-content-type-parameter` | review | PASS | PASS | PASS |
| `requests-netrc-empty-default` | review | PASS | PASS | PASS |
| `pflag-unknown-flag-non-interspersed` | review | FAIL | FAIL | FAIL |
| `pflag-count-preserve-on-error` | review | PASS | PASS | PASS |
| `pflag-native-int-overflow` | review | FAIL | PASS | PASS |
| `goexample-weave-newline` | review | FAIL | FAIL | FAIL |
| `click-negative-no-subprocess` | negative | PASS | PASS | PASS |
| `requests-negative-no-dynamic-eval` | negative | PASS | PASS | PASS |
| `click-sentinel-repair` | repair | FAIL | FAIL | FAIL |
| `click-short-help-repair` | repair | FAIL | FAIL | FAIL |
| `click-help-name-repair` | repair | FAIL | FAIL | FAIL |
| `requests-encode-files-getattr`（holdout） | review | PASS | PASS | PASS |
| `pflag-ipv4-mask-ipv6`（holdout） | review | PASS | PASS | PASS |
| `pflag-negative-no-unsafe`（holdout） | negative | PASS | PASS | PASS |

失败原因（按实际产物）：

- `click-sentinel-identity`：三臂都返回空 `findings`（扫描覆盖 1.0，未解析失败），是对
  `Sentinel.__copy__/__deepcopy__` 的真实漏报；
- `click-help-name-collision`、`click-show-envvar-sequence`、
  `pflag-unknown-flag-non-interspersed`：三臂都在 reasoning 中耗尽 16384 输出 token，
  `content` 为空，`parse_error=response did not contain a JSON object`，按失败记录且不重试；
- `goexample-weave-newline`：三臂均定位到正确文件/行/类别，但 `symbol` 写成 `main.main`，
  与 gold `internal/cmd/weave.main` 不一致，按误报计；
- `pflag-native-int-overflow`：A 把符号写成 `pflag.(*intValue).Set` / `pflag.(*uintValue).Set`，
  与 gold 的 `pflag.intValue.Set` 不一致（两个 finding 均有效引用但 key 不匹配）；
  B、C 命中 gold；
- 3 个 repair：三臂都给出了补丁，但 hunk 行号/上下文与固定 buggy 快照不匹配，
  `apply_unified_patch` 拒绝，补丁验证、独立测试、回归全部失败；
- 3 个 negative 与 3 个 holdout 任务三臂全部 PASS。

### 4.3 计费台账

| run | model_calls | cost_micros |
| --- | ---: | ---: |
| `smoke-20261109T121518Z` | 3 | 22,126 |
| `smoke-20261109T121656Z` | 3 | 28,527 |
| `live-20261109T122004Z`（预检后中止） | 6 | 57,154 |
| `smoke-20261109T122431Z` | 3 | 34,812 |
| `live-20261109T122858Z`（正式） | 54 | 785,467 |
| 合计 | 69 | 928,086（USD 0.928086） |

`cache_state` 统一记为 `none`（未按调用控制/记录）；provider 报告 54 次中有 37 次
prompt cache 命中，但计费未打折，仍按全价保守记录。

### 4.4 诚实性

`test_live_arms_are_honest` 断言：54 条记录覆盖 A/B/C 与全部 18 个任务；每条执行记录
`model_calls=1`、`usage_source=actual_provider_usage`、延迟/Token/费用为正且与产物文件
的 SHA-256 一致；不存在 `NOT_RUN` 被填成 PASS/FAIL；总费用不超过 2,000,000 micros。

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

- Live A/B/C 已按用户授权（`deepseek-flash`，USD 2 上限）真实执行：54 次调用、0 NOT_RUN，
  失败如实记为 business，未隐藏、未重试；全程未执行 GitHub 写入。
- `live-20261109T122004Z` 是预检 run，6 次调用后人工中止：初版提示词未强制
  完全限定符号与精确 category，答案不合规；修正提示词后重跑正式 run。该 run 的
  57,154 micros 计入总费用。
- 3 次 smoke run（共 9 次调用）用于确认 JSON 输出与 `max_tokens` 上限，计入总费用。
- `max_tokens=16384` 对 3 个 scope 较大的 review 任务仍不够：reasoning 耗尽输出，
  `content` 为空；未提高上限或重试，按失败记录。该限制同时影响三个臂，不改变臂间比较。
- `draft_pr_correctness` 是离线代理（补丁非空、只改 scope 文件、可应用），
  未创建真实 Draft PR，也未调用 GitHub API。
- 记忆指标中 `recall_at_k`、`stale_rejection`、`contamination_rate` 无标注用例，
  `repeated_tool_call_reduction` 不适用（每任务每臂单次调用、无工具循环）；
  `token_reduction` 由 B/C 相对 A 的输入 token 增量与 C 相对 B 的缩减近似反映。
- `call_chain_correctness` 只要求 gold symbol 位于链尾，因为 gold 链含无法从快照
  唯一重建的合成中间帧；该指标仍进入 `passed` 合取。
- 离线自检不覆盖模型行为，只覆盖判定器、答案与补丁应用；
- 3 个 repair 任务的独立测试由本轮编写，未复用 upstream 测试，避免答案泄漏；
- 快照只保留任务相关文件，不是整仓克隆；`repositories.json` 记录的是快照内文件。
