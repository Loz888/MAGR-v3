# MAGR-v3 V3.5 任务队列

- 基线：`6cf67f404d8575d5e02f0dd0edfcbfb67a512ddc`（2026-11-10）
- 架构前提：[ARCHITECTURE_CONTRACT.md](./ARCHITECTURE_CONTRACT.md) · [BASELINE.md](./BASELINE.md)
- 规则：一次会话只执行一张 `READY` 卡；从上一卡可复现的产物继续；每卡单独提交并生成 `accept/<CARD_ID>/accept.json`。
- V3.5 卡号统一使用 `V35-` 前缀，与 `v3/docs/v33/`、`v3/docs/repairs/` 的历史卡区分；历史文档只读。

| 卡 | 主题 | 依赖 | 状态 | 工程门 | live 门 |
| --- | --- | --- | --- | --- | --- |
| V35-B00 | 冻结 V3.4 并建立唯一实施真相 | 无 | DONE | PASS | NOT_APPLICABLE |
| V35-U01 | 统一 Task / Run Ledger 与状态迁移 | B00 | DONE | PASS | NOT_RUN |
| V35-U02 | 统一 Finding → RepairHandoff 真实交接 | U01 | DONE | PASS | NOT_RUN |
| V35-R01 | 结构化模型补丁提供方 | U02 | DONE | PASS | NOT_RUN |
| V35-R02 | 真实仓库源文件与 Docker Sandbox 统一执行 | R01 | DONE | PASS | NOT_RUN |
| V35-R03 | 统一 Audit & Repair 多轮编排 | R02 | DONE | PASS | NOT_RUN |
| V35-R04 | 真实 Draft PR 发布接入 Outbox/Dispatcher | R03 | DONE | PASS | NOT_RUN（写权限独立审批） |
| V35-R05 | 统一 Web / CLI / API 产品交互 | R04 | DONE | PASS | NOT_RUN |
| V35-M01 | 跨阶段持久经验闭环 | R03 | DONE | PASS | NOT_RUN |
| V35-M02 | Context Budget、Compaction 与恢复一致性 | M01 | DONE | PASS | NOT_RUN |
| V35-X01 | 按需启用 R4 Experts | R05、M02 | DONE | PASS | NOT_RUN |
| V35-E01 | 重建端到端真实仓库评测 Harness | R05、M01 | DONE | PASS | NOT_RUN |
| V35-E02 | Memory / Compaction / Expert 消融实验 | E01、M02、X01 | DONE | PASS | FAIL |
| V35-E03 | 端到端安全、并发与灾难恢复 | R04、M02、E01 | DONE | PASS | NOT_RUN |
| V35-D01 | 产品说明、演示脚本、部署与最终验收 | 前述必须卡 | DONE | PASS | FAIL（模型/Agent）/ NOT_RUN（写） |

## 依赖与放行

```text
B00 ── U01 ── U02 ── R01 ── R02 ── R03 ── R04 ── R05 ── X01
                                  │        │
                                  └─ M01 ── M02 ── X01
                                  │
                                  └─ E01 ── E02 ── E03 ── D01
```

- 当前无 `READY` 卡：V35 全部必须卡已完成。E02 fixture 工程门 `PASS`，live 四臂因输出长度上限全部 `PATCH_TRUNCATED`，`live_agent_gate=FAIL` 且结论 `INCONCLUSIVE`（无赢家，配置不改）。E03 工程/安全门 `PASS`，35 条场景 34 条通过、1 条真实 PostgreSQL 重启 `NOT_RUN`。D01 文档、90 秒演示与聚合验收已完成：工程/安全门 `PASS`，Live 模型与 Agent 门聚合为 `FAIL`（继承 E02），Live GitHub 写门 `NOT_RUN`。真实 Agent 门仍未通过，不得对外宣称已验收。
- 任何卡的依赖未通过验收时，该卡标记 `BLOCKED`，不得假装 PASS。

## 每卡交付物

1. 代码与测试（`tests/test_v35_<card>.py`），注册 `v3/config/verification.json` 的 `V35-<CARD>` 条目。
2. `v3/docs/v35/cards/V35-<CARD>.md` 状态更新与实现说明。
3. `v3/docs/v35/accept/V35-<CARD>/accept.json`（`engineering_gate` / `live_model_gate` / `live_github_write_gate` / `live_agent_gate` / `security_gate` 分开）。
4. `v3/evidence/v35/<CARD>/<UTC_TIME>-<RUN_ID>/` 原始日志（`.gitignore`，不入库）。

## 门禁口径

- 工程门：卡内 pytest + 相关回归 + `ruff` + `mypy` + `make doctor` + `git diff --check`，全部 exit 0 且无 skipped。
- Live 模型门：必须单独授权文件、预算、有效期；未授权一律 `NOT_RUN`。
- Live GitHub 写门：必须独立审批并绑定 `payload_hash`；未授权一律 `NOT_RUN`，不得用 fixture PR 冒充。
- 真实 Agent 门（`live_agent_gate`）：仅当 V35-E01 的强制验收协议六条全部满足时可为 `PASS`；未执行真实模型实验一律 `NOT_RUN`。`engineering_gate = PASS` 不蕴含 `live_agent_gate = PASS`，不得以 fixture/ScriptedProvider 结果宣称真实 Agent 能力已验收。
- 安全门：越权写 0 次、未知副作用 0 次盲重试；由 E03 汇总验证。
