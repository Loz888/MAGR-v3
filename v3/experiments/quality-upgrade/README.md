# Quality Upgrade Experiments

本目录承载 QEV 系列任务卡的新实验，只追加、不改写既有 `v3/eval/reports`、`v3/docs/acceptance`、`v3/docs/repairs` 与 `.local` 证据。

每个实验遵循统一协议：

- `manifest.json`：实验 ID、Commit、数据集/配置 Hash、Case ID 全集、配对键、授权与环境
- `cases.jsonl`：逐 Case 记录，唯一键 `(experiment_id, case_id, arm, repeat_index)`
- `summary.json` / `summary.md`：分臂统计，含分子/分母/适用范围与 `summary_sha256`
- `comparison.json` / `comparison.md`：配对比较（可选）

## qev-02-rehearsal

- 性质：**格式迁移演练**（`mode=historical_import`），不是新实验成绩，不重新判定 PASS/FAIL。
- 来源：只读重排 `v3/eval/reports/final-v6-answers.jsonl`（90 条答案，30 个 Case × A/B/C）。
- 数据 Hash：`eval/final-v3/cases.json` 的 SHA256（`9ce3bc47…d9d36f9`），与 final-v6 冻结记录一致。
- 配置 Hash：`eval/reports/final-v6-manifest.json` 的 SHA256。
- 生成：`PYTHONPATH=src python scripts/quality_eval_import_v6_rehearsal.py`
- 校验：`PYTHONPATH=src python scripts/quality_eval.py validate --experiment experiments/quality-upgrade/qev-02-rehearsal --root . --check-files`
- 统计：`PYTHONPATH=src python scripts/quality_eval.py summarize --experiment experiments/quality-upgrade/qev-02-rehearsal`
- 比较：`PYTHONPATH=src python scripts/quality_eval.py compare --experiment experiments/quality-upgrade/qev-02-rehearsal --baseline A --candidate C`

本演练不调用模型、不产生费用、不写 GitHub、不代表 QEV-03/04 的 JSON 恢复结论。
