# 质量评测摘要 v34-audit-repair

> 本文件由 `scripts/quality_eval.py summarize` 生成；数字可由 `cases.jsonl` 复算。

- 记录数：54，唯一 Case：18
- 数据集 Hash：`1db67439cd0bf461ffdac863bf44ed5343f2d7616ac11e4254e79a9d94f83c8f`
- 配置 Hash：`d03e5ef4a7677c4e66d0de408139d21f7de8c95bad583af26439110334d94fec`
- 汇总摘要 Hash：`fa2b45f6cc34c1a11d7302ff579e6433ccba6d9a1a6cd0290f24dd9b35143ac3`
- 实验 Manifest：`acb16657c0a53c72998ffeecd7fc5d932c4a2c751dd0173cf8697d67732e703e`

成功率分母仅包含 `PASS` + `FAIL`；`NOT_RUN` 与 `UNKNOWN` 单独计数，不计入分子或分母。

| 臂 | PASS | FAIL | NOT_RUN | UNKNOWN | 成功率（分子/分母） | 95% CI |
|---|---:|---:|---:|---:|---|---|
| A | 9 | 9 | 0 | 0 | 9/18 = 0.5000 | [0.2903, 0.7097] |
| B | 10 | 8 | 0 | 0 | 10/18 = 0.5556 | [0.3372, 0.7544] |
| C | 10 | 8 | 0 | 0 | 10/18 = 0.5556 | [0.3372, 0.7544] |

## 失败分类

| 臂 | failure_kind | 数量 |
|---|---|---:|
| A | business | 9 |
| B | business | 8 |
| C | business | 8 |

## NOT_RUN 原因

| 臂 | 原因 | 数量 |
|---|---|---:|
| — | 无 | 0 |

## 延迟（P50/P95，按 cache_state 分列，不混合）

| 臂 | 缓存状态 | 样本数 | P50 (ms) | P95 (ms) |
|---|---|---:|---:|---:|
| A | none | 18 | 39441.1 | 94778.4 |
| B | none | 18 | 48924.0 | 92763.6 |
| C | none | 18 | 36314.8 | 98588.5 |

## 成本与调用

| 臂 | model_calls | prompt_tokens | completion_tokens | cost_micros | 每完成用例成本（分子/分母） |
|---|---:|---:|---:|---:|---:|
| A | 18 | 260197 | 153589 | 262348 | 262348/18 = 14574.9 |
| B | 18 | 261868 | 159092 | 269453 | 269453/18 = 14969.6 |
| C | 18 | 260782 | 146205 | 253666 | 253666/18 = 14092.6 |

## V3.4 真实公开仓库数据集（E01）

- 仓库数：4；任务数：18（dev 15 / holdout 3）
- 任务类型：review 12，repair 3，negative 3
- 语言覆盖：python 12，go 6
- Holdout 未参与开发：pflag-ipv4-mask-ipv6, pflag-negative-no-unsafe, requests-encode-files-getattr（used_in_development=False）
- 实时臂状态：`EXECUTED`
- 真实调用：54 次，记录 54 条（执行 54，NOT_RUN 0），保守计费 785467 micros（USD 0.785467）
- Tokens：输入 782847，输出 458886

| 臂 | 记录数 | 执行 | NOT_RUN | PASS | FAIL |
|---|---:|---:|---:|---:|---:|
| A | 18 | 18 | 0 | 9 | 9 |
| B | 18 | 18 | 0 | 10 | 8 |
| C | 18 | 18 | 0 | 10 | 8 |

| 臂 | Precision@K | Recall@Budget | 未验证误报 | Citation | Chain | Coverage |
|---|---:|---:|---:|---:|---:|---:|
| A | 0.6000 | 0.6000 | 0 | 1.0000 | 1.0000 | 0.8000 |
| B | 0.6667 | 0.6667 | 0 | 1.0000 | 1.0000 | 0.8000 |
| C | 0.6667 | 0.6667 | 0 | 1.0000 | 1.0000 | 0.8000 |

| 臂 | 修复用例 | 复现成功 | 补丁可应用 | 独立测试通过 | Draft PR 正确 | 回归失败 |
|---|---:|---:|---:|---:|---:|---:|
| A | 3 | 3 | 0 | 0 | 0 | 3 |
| B | 3 | 3 | 0 | 0 | 0 | 3 |
| C | 3 | 3 | 0 | 0 | 0 | 3 |

- 配对完整用例：18/18
- 正确率（无 Memory / 仅检索 / 完整 Memory+压缩）：0.5000 / 0.5556 / 0.5556
- 输入 Token 相对 A 的增量（B / C）：92.8333 / 32.5000
- 成本相对 A 的增量 micros（B / C）：394.7222 / -482.3333
- C 相对 B 的上下文 Token 缩减：60.3330

- 离线 Harness 自检（scope=harness_conformance_not_model_capability）：执行 15，PASS 15，FAIL 0
- 该自检只证明判定管线与固定答案一致，不是模型能力成绩，也不计入 A/B/C 成功率。

未测量的指标：

- `memory_recall_at_k`：live runner has no labeled memory-relevance cases
- `stale_rejection`：live runner has no labeled stale-memory cases
- `contamination_rate`：live runner has no labeled contaminated-memory cases
- `repeated_tool_call_reduction`：single-shot runner issues one model call per arm, so there are no tool-call loops
