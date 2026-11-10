# V3.5 评测与验收索引

- 状态：D01（2026-11-10）
- 规则：工程门、Live 模型门、Live GitHub 写门、真实 Agent 门、安全门分开记录；`NOT_RUN` 不得改写为 `PASS`。每卡的完整命令、退出码与证据路径见对应 `accept.json`。
- 公共证据：`v3/docs/v35/accept/<CARD>/accept.json`；原始日志在 `v3/evidence/`（`.gitignore`，不入库）。

## 1. 门禁总表

| 卡 | 主题 | 工程门 | Live 模型门 | Live GitHub 写门 | 真实 Agent 门 | 安全门 | 验收记录 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| V35-B00 | 冻结 V3.4 与唯一实施真相 | PASS | NOT_RUN | NOT_RUN | NOT_RUN | NOT_RUN | [accept](./accept/V35-B00/accept.json) |
| V35-U01 | Task / Run Ledger 与状态迁移 | PASS | NOT_RUN | NOT_RUN | NOT_RUN | NOT_RUN | [accept](./accept/V35-U01/accept.json) |
| V35-U02 | Finding → RepairHandoff 交接 | PASS | NOT_RUN | NOT_RUN | NOT_RUN | NOT_RUN | [accept](./accept/V35-U02/accept.json) |
| V35-R01 | 结构化模型补丁提供方 | PASS | NOT_RUN | NOT_RUN | NOT_RUN | NOT_RUN | [accept](./accept/V35-R01/accept.json) |
| V35-R02 | 真实源文件与 Docker Sandbox | PASS | NOT_RUN | NOT_RUN | NOT_RUN | NOT_RUN | [accept](./accept/V35-R02/accept.json) |
| V35-R03 | Audit & Repair 多轮编排 | PASS | NOT_RUN | NOT_RUN | NOT_RUN | NOT_RUN | [accept](./accept/V35-R03/accept.json) |
| V35-R04 | Draft PR 发布接入 Outbox | PASS | NOT_RUN | NOT_RUN | NOT_RUN | NOT_RUN | [accept](./accept/V35-R04/accept.json) |
| V35-R05 | 统一 Web / CLI / API | PASS | NOT_RUN | NOT_RUN | NOT_RUN | NOT_RUN | [accept](./accept/V35-R05/accept.json) |
| V35-M01 | 跨阶段持久经验闭环 | PASS | NOT_RUN | NOT_RUN | NOT_RUN | NOT_RUN | [accept](./accept/V35-M01/accept.json) |
| V35-M02 | Context Budget / Compaction / 恢复 | PASS | NOT_RUN | NOT_RUN | NOT_RUN | NOT_RUN | [accept](./accept/V35-M02/accept.json) |
| V35-X01 | 按需启用 R4 Experts | PASS | NOT_RUN | NOT_RUN | NOT_RUN | NOT_RUN | [accept](./accept/V35-X01/accept.json) |
| V35-E01 | 端到端真实仓库评测 Harness | PASS | NOT_RUN | NOT_RUN | NOT_RUN | PASS | [accept](./accept/V35-E01/accept.json) |
| V35-E02 | Memory / Compaction / Expert 消融 | PASS | **FAIL** | NOT_RUN | **FAIL** | PASS | [accept](./accept/V35-E02/accept.json) |
| V35-E03 | 端到端安全、并发与灾难恢复 | PASS | NOT_RUN | NOT_RUN | NOT_RUN | PASS | [accept](./accept/V35-E03/accept.json) |
| V35-D01 | 产品说明、演示、部署与最终验收 | PASS | **FAIL（聚合）** | NOT_RUN | **FAIL（聚合）** | PASS | [accept](./accept/V35-D01/accept.json) |

聚合口径：任一卡真实执行并失败时，D01 的对应 Live 门为 `FAIL`，不能因其它卡未执行而标 `NOT_RUN` 或 `PASS`。

## 2. E01：评测 Harness

| 项目 | 结果 | 证据 |
| --- | --- | --- |
| 开发集 fixture 端到端 | 4/4 `pass`，`success_rate=1.0`，`failure_reasons={}` | [accept](./accept/V35-E01/accept.json) |
| adversarial 多轮 | 首补丁公开测试失败，第二轮修复成功；`attempts=2`、`model_calls=2` | `evidence/v35/E01/20261110T070535Z-e01-dev-fixture/` |
| 断点恢复 | 复制检查点后 `provider_calls=0`；在途调用中断返回 `REPAIR_UNKNOWN_PROVIDER_OUTCOME` 并停止，不重复调用 | 同上 `resume-probe/` |
| 评分签名 | `e128666e9271c0ed5fd506922d7aceeae581acb706c892a43d838f0a7f51bbae` | `summary.json` |
| Holdout | 默认加载为空；仅显式 `--evaluate-holdout` 可见；未在开发路径执行 | [accept](./accept/V35-E01/accept.json) 的 deviations |
| 门禁 | 28 passed / 0 failed / 0 skipped；`source_sha256` 前后一致 | `evidence/V35-E01/20261110T070316Z-8c2d051f/` |

复现：

```bash
make verify TASK=V35-E01
make demo
```

## 3. E02：四臂消融

| 项目 | 结果 | 证据 |
| --- | --- | --- |
| fixture 网格 | 16/16 执行并通过（4 任务 × A/B/C/D） | [accept](./accept/V35-E02/accept.json) |
| 上下文 Token（A/B/C/D） | 614.25 / 840.5 / 677.75 / 829.75 | `evidence/v35/E02/20261110T073951Z-e02-dev-fixture/` |
| 上下文丢失率（C/D） | 0.583333；A/B 为 0 | 同上 |
| live 探针 | 4 臂全部 `PATCH_TRUNCATED`，`passed=0` | `evidence/v35/E02/20261110T072906Z-knownbug-live/` |
| live 成本 | 12 次调用、637310 micros（deepseek-chat） | [accept](./accept/V35-E02/accept.json) |
| 结论 | `INCONCLUSIVE`，无赢家，配置不改，历史保留 | `v3/config/v35-ablation.json` |

`live_agent_gate=FAIL` 是真实能力失败，不是基础设施错误，也不升级为 `PASS`。

复现：

```bash
make verify TASK=V35-E02
uv run --frozen python scripts/run_eval.py --ablation --mode fixture --suite dev
```

## 4. E03：安全、并发与灾难恢复

| 项目 | 结果 | 证据 |
| --- | --- | --- |
| 场景登记 | 6 类 35 条，每条含 node id、失败点、观测状态、恢复结果 | [accept](./accept/V35-E03/accept.json) |
| 实测覆盖 | 34 条 `COVERED` + 1 条 `NOT_RUN`（真实 PostgreSQL 服务重启） | `evidence/v35/E03/20261110T081039Z-e03-scenarios/` |
| 工程门 | 36 passed / 0 failed / 0 skipped（含 V3.4 18 条回归） | `evidence/V35-E03/20261110T080855Z-022f3184/` |
| 安全门 | `PASS`；越权写 0、未知副作用盲重试 0 | 场景报告 `security_gate=PASS` |
| Live 门 | 模型/GitHub 写/Agent 均 `NOT_RUN`；故障在真实传输边界注入 | [accept](./accept/V35-E03/accept.json) |

复现：

```bash
make verify TASK=V35-E03
uv run --frozen python scripts/run_e03.py
```

## 5. D01：文档、演示与聚合验收

| 项目 | 结果 | 证据 |
| --- | --- | --- |
| 产品说明 | 快速启动、双入口、生命周期、授权、契约、差异表、成本与安全、边界 | [PRODUCT.md](./PRODUCT.md) |
| 90 秒演示 | 7 步分镜 + Web 入口 + 失败排查 | [DEMO.md](./DEMO.md) |
| 示例仓库 | 固定 Commit 快照复制与 digest 校验，不联网、不调用模型 | `scripts/demo_v35.py` |
| 聚合门 | 工程 `PASS`；Live 模型 `FAIL`（E02）；GitHub 写 `NOT_RUN`；Agent `FAIL`（E02）；安全 `PASS` | [accept](./accept/V35-D01/accept.json) |

复现：

```bash
make doctor
make demo-repo
make demo
make verify TASK=V35-D01
```

## 6. 证据目录约定

```text
v3/evidence/v35/<CARD>/<UTC>-<RUN_ID>/     # 原始命令日志与不可变报告
v3/evidence/<TASK>/<UTC>-<ATTEMPT>/        # scripts/verify.py 的 JUnit 与源码指纹
v3/docs/v35/accept/<CARD>/accept.json       # 入库的公开验收记录
```

报告目录禁止覆盖；失败样本与 `NOT_RUN` 原样保留。截图不作为验收证据，验收以命令退出码、JUnit、不可变报告与源码指纹为准。
