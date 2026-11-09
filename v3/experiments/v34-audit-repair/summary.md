# 质量评测摘要 v34-audit-repair

> 本文件由 `scripts/quality_eval.py summarize` 生成；数字可由 `cases.jsonl` 复算。

- 记录数：54，唯一 Case：18
- 数据集 Hash：`29b14ee695cb6c5d9bc8254744cdd1fec6d78deffd7ae2161750601bdd2230ee`
- 配置 Hash：`62119c069fca7f17ad8afb3f5aa52affac0f2ab9a19edebcabe78d5ff1cd9c32`
- 汇总摘要 Hash：`f8d0a2feaa35410861f2db040c0caf0f6e620da490b73abf280ca8ef7fa82775`
- 实验 Manifest：`c63c439943db0b2e4e4498e84c6da70c4ed8a2bfddbf8446cca8317c3dd876f2`

成功率分母仅包含 `PASS` + `FAIL`；`NOT_RUN` 与 `UNKNOWN` 单独计数，不计入分子或分母。

| 臂 | PASS | FAIL | NOT_RUN | UNKNOWN | 成功率（分子/分母） | 95% CI |
|---|---:|---:|---:|---:|---|---|
| A | 0 | 0 | 18 | 0 | 0/0 = n/a | n/a |
| B | 0 | 0 | 18 | 0 | 0/0 = n/a | n/a |
| C | 0 | 0 | 18 | 0 | 0/0 = n/a | n/a |

## 失败分类

| 臂 | failure_kind | 数量 |
|---|---|---:|
| — | 无 | 0 |

## NOT_RUN 原因

| 臂 | 原因 | 数量 |
|---|---|---:|
| A | live A/B/C execution requires explicit paid-model authorization and GitHub draft-PR write authorization; neither was granted for this run, so no model, tool or network call was made | 18 |
| B | live A/B/C execution requires explicit paid-model authorization and GitHub draft-PR write authorization; neither was granted for this run, so no model, tool or network call was made | 18 |
| C | live A/B/C execution requires explicit paid-model authorization and GitHub draft-PR write authorization; neither was granted for this run, so no model, tool or network call was made | 18 |

## 延迟（P50/P95，按 cache_state 分列，不混合）

| 臂 | 缓存状态 | 样本数 | P50 (ms) | P95 (ms) |
|---|---|---:|---:|---:|

## 成本与调用

| 臂 | model_calls | prompt_tokens | completion_tokens | cost_micros | 每完成用例成本（分子/分母） |
|---|---:|---:|---:|---:|---:|
| A | 0 | 0 | 0 | 0 | 0/0 = n/a |
| B | 0 | 0 | 0 | 0 | 0/0 = n/a |
| C | 0 | 0 | 0 | 0 | 0/0 = n/a |

## V3.4 真实公开仓库数据集（E01）

- 仓库数：4；任务数：18（dev 15 / holdout 3）
- 任务类型：review 12，repair 3，negative 3
- 语言覆盖：python 12，go 6
- Holdout 未参与开发：pflag-ipv4-mask-ipv6, pflag-negative-no-unsafe, requests-encode-files-getattr（used_in_development=False）
- 实时臂状态：`NOT_RUN`；原因：live A/B/C execution requires explicit paid-model authorization and GitHub draft-PR write authorization; neither was granted for this run, so no model, tool or network call was made

| 臂 | 记录数 | NOT_RUN | PASS | FAIL |
|---|---:|---:|---:|---:|
| A | 18 | 18 | 0 | 0 |
| B | 18 | 18 | 0 | 0 |
| C | 18 | 18 | 0 | 0 |

- 离线 Harness 自检（scope=harness_conformance_not_model_capability）：执行 15，PASS 15，FAIL 0
- 该自检只证明判定管线与固定答案一致，不是模型能力成绩，也不计入 A/B/C 成功率。

未执行的指标：

- `finding_precision_at_k`
- `recall_at_budget`
- `unverified_false_positives`
- `source_citation_validity`
- `call_chain_correctness`
- `bug_reproduction_success`
- `patch_validation_success`
- `independent_test_pass`
- `regression_failures`
- `draft_pr_correctness`
- `memory_recall_at_k`
- `stale_rejection`
- `contamination_rate`
- `token_reduction`
- `repeated_tool_call_reduction`
- `correctness_with_memory`
- `correctness_without_memory`
- `retrieval_cost`
