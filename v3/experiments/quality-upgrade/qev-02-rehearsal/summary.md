# 质量评测摘要 qev-02-rehearsal

> 本文件由 `scripts/quality_eval.py summarize` 生成；数字可由 `cases.jsonl` 复算。

- 记录数：90，唯一 Case：30
- 数据集 Hash：`9ce3bc4762aacd2d738f8dc79ecea19a9a9e1f5f483b71e65d8b43319d9d36f9`
- 配置 Hash：`75774ed08b3a5702da1e5101c1b998f5ef22c4f37bbbb63fcf813741d5539983`
- 汇总摘要 Hash：`fbaee9d9b8a9181adc4b097502081c2c901bad92c29f53d19b6f88b5d8e4beee`
- 实验 Manifest：`423f2d3bcec8a93f599b5b4e386c990a139fcacd8ab42b97dcfdb805117ceac8`

成功率分母仅包含 `PASS` + `FAIL`；`NOT_RUN` 与 `UNKNOWN` 单独计数，不计入分子或分母。

| 臂 | PASS | FAIL | NOT_RUN | UNKNOWN | 成功率（分子/分母） | 95% CI |
|---|---:|---:|---:|---:|---|---|
| A | 30 | 0 | 0 | 0 | 30/30 = 1.0000 | [0.8865, 1.0000] |
| B | 28 | 2 | 0 | 0 | 28/30 = 0.9333 | [0.7868, 0.9815] |
| C | 29 | 1 | 0 | 0 | 29/30 = 0.9667 | [0.8333, 0.9941] |

## 失败分类

| 臂 | failure_kind | 数量 |
|---|---|---:|
| B | business | 2 |
| C | business | 1 |

## NOT_RUN 原因

| 臂 | 原因 | 数量 |
|---|---|---:|
| — | 无 | 0 |

## 延迟（P50/P95，按 cache_state 分列，不混合）

| 臂 | 缓存状态 | 样本数 | P50 (ms) | P95 (ms) |
|---|---|---:|---:|---:|
| A | none | 30 | 1704.2 | 27593.7 |
| B | none | 30 | 3802.5 | 29471.8 |
| C | none | 30 | 3737.4 | 29719.8 |

## 成本与调用

| 臂 | model_calls | prompt_tokens | completion_tokens | cost_micros | 每完成用例成本（分子/分母） |
|---|---:|---:|---:|---:|---:|
| A | 27 | 29296 | 3628 | 13151 | 13151/30 = 438.4 |
| B | 75 | 45146 | 9568 | 25058 | 25058/30 = 835.3 |
| C | 60 | 41465 | 8511 | 22678 | 22678/30 = 755.9 |
