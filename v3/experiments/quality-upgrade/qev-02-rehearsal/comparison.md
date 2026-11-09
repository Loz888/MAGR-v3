# 配对比较 A vs C

> 本文件由 `scripts/quality_eval.py compare` 生成；仅作配对描述统计。

- 实验：`qev-02-rehearsal`
- 数据集 Hash：`9ce3bc4762aacd2d738f8dc79ecea19a9a9e1f5f483b71e65d8b43319d9d36f9`
- 配置 Hash：`75774ed08b3a5702da1e5101c1b998f5ef22c4f37bbbb63fcf813741d5539983`
- 配对键：all `(case_id, repeat_index)`；总对数 30，可计入 30，排除 0
- 排除原因：无

| 臂 | 成功（分子/分母） | 成功率 | 95% CI |
|---|---|---|---|
| A | 30/30 | 1.0000 | [0.8865, 1.0] |
| C | 29/30 | 0.9667 | [0.8333, 0.9941] |

- 成功率差：-3.33 个百分点（C 减 A）
- 仅 A 成功：1
- 仅 C 成功：0
- 两臂均成功：29
- 两臂均失败：0
- 精确 McNemar 双侧 p：1.000000

## 延迟差（配对 Bootstrap，按 cache_state 分列）

| 缓存状态 | 对数 | 平均差 (ms) | 95% CI (ms) |
|---|---:|---:|---|
| none | 30 | 1244.5 | [294.7, 1906.5] |

- 缓存状态不一致的配对数（不参与延迟比较）：0
- 每用例成本差（C 减 A）：317.6 micros，95% CI [200.2, 424.3]

- 结论字段：`NOT_EVALUATED_IN_QEV-02` — QEV-02 provides measurement infrastructure only; no rollout threshold was predeclared and no recommendation is made here
