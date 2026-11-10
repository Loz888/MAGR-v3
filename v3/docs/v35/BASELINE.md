# V3.5 基线冻结（V35-B00）

- 状态：B00 完成（基线冻结 / 架构审计）
- 基线 Commit：`6cf67f404d8575d5e02f0dd0edfcbfb67a512ddc`（`main`，跟踪 `origin/main`）
- 冻结日期：2026-11-10
- 适用范围：V3.5 全部任务卡的唯一实施起点。`v3/docs/v33/`、`v3/docs/repairs/`、E01/E02 历史证据只读，不回写。

## 1. 基线现场

`v3/` 与仓库根目录的实测状态（完整日志见 `evidence/v35/B00/<run>/01-git-baseline.log`）：

| 命令 | 结果 |
| --- | --- |
| `git status --short`（B00 开始时） | 空；冻结后仅本卡自身的 `config/verification.json` 注册与 `docs/v35/` 新增 |
| `git rev-parse HEAD` | `6cf67f404d8575d5e02f0dd0edfcbfb67a512ddc` |
| `git log -5 --oneline` | `6cf67f4` → `6724c71` → `f41cb63` → `5170b41` → `ff5f8ba` |
| `git branch -vv` | `* main 6cf67f4 [origin/main]` |

B00 开始时 HEAD 与任务书给定基线一致，没有需要做影响评估的新提交。

## 2. 环境与迁移

| 项 | 实测值 | 来源 |
| --- | --- | --- |
| OS / 工具链 | WSL2 Ubuntu-24.04；`uv --frozen`；Python 3.12 | `v3/pyproject.toml`、`v3/AGENTS.md` |
| 依赖环境 | `/tmp/mgar-v3-venv`（Linux venv 不放在 `/mnt/c`） | V3.3 B00 契约 |
| PostgreSQL | 容器 `mgar-v3-postgres-1`，`127.0.0.1:55433` | `v3/Makefile`、`v3/deploy` |
| Redis | 容器 `mgar-v3-redis-1`，`127.0.0.1:56379` | 同上 |
| 迁移文件 | 22 个（`0001`…`0022`），head `0022_workbench.sql` | `v3/migrations/` |
| 验证注册表 | `v3/config/verification.json`（本卡注册 `V35-B00`） | `v3/scripts/verify.py` |

`make migrate` 与 `make doctor` 的实测结果记录在 `accept/B00/accept.json` 的 `tests[]` 与证据日志中。

## 3. 基线能力事实（源码可追踪）

| 能力 | 现状 | 源码位置 | 测试 |
| --- | --- | --- | --- |
| Workbench Live Audit | 已实现：`mode=audit` + `execution_mode=live`，需 `AnalysisAuthorization` profile 与 GitHub 只读凭据；Commit 由只读工具解析 | `workbench/service.py:227,902`；`repository_analysis/service.py:183` | `tests/test_v34_repository_api.py`（15 项）；V3.2 live 报告 |
| Workbench Fixture Audit | 已实现：固定 fixture 仓库与 fixture 模型 | `service.py:334,1077` | 同上 |
| Workbench Fixture Repair | 已实现但为固定样例：`FIXED_DISPATCHER`、临时目录内宿主 `pytest`、内存 GitHub remote | `service.py:79,148,1226,1270` | 同上 |
| Workbench Live Repair | **不可用**：创建与运行均返回 `WORKBENCH_LIVE_REPAIR_PROFILE_REQUIRED`（403） | `service.py:318-323,902-908` | `tests/test_v34_repository_api.py:625` |
| Workbench 发布 | 仅内存 remote，PR URL 为 `fixture://...`；无 Outbox/Action 记录 | `service.py:590-687` | 同上 |
| R04 RepairAgent | 独立库：`RepairAgent.run`、`PatchPlan`、`RepairProposal`；**无生产调用者**，仅测试导入 | `repair/repair_agent.py:74,151,185` | `tests/test_v34_repair_agent.py`（9 项） |
| R05 发布 | 两条路径：`pr_delivery` 的 MCP 读/写传输（仅测试接线）；`publication.py` 的 `LiveDraftMCPTransport` 受 `r5-repair-v1` 单一 profile 限制 | `repair/pr_delivery.py:351,437,512`；`repair/publication.py:21,195` | `tests/test_v34_pr_delivery.py`（8 项） |
| Memory（调查） | Workbench 审计后可召回 M05 记忆并落 `app.workbench_memory` | `service.py:1017`；`repository_analysis/memory_loop.py:139` | `tests/test_v33_memory_agent.py`（6 项） |
| Memory（修复/发布） | 仅进程内 `MemoryEpisodeLog` / `MemoryPublicationLog`，不落持久存储 | `repair/repair_agent.py`、`repair/pr_delivery.py:234` | 对应 R04/R05 测试 |
| E01 评测 | 固定快照、主要单次模型调用，不是统一 Agent Loop；A/B/C 18 任务成功数 9/10/10 | `quality_eval/v34.py` | `tests/test_v34_evaluation.py`（18 项） |
| E02 安全/恢复 | 18 个安全、失败与恢复场景通过；不含真实 GitHub 写 | `tests/test_v34_security_recovery.py` | 18 项 |

## 4. 不可声称的结论（基线边界）

1. 不能声称 Workbench 已支持真实仓库修复或真实 Draft PR 发布。
2. 不能把 fixture 修复、内存 remote、宿主 `pytest` 的结果当作 live 证据。
3. 不能声称 R04/R05 已接入统一 Workbench 链路；它们目前是独立能力与独立测试。
4. 不能把 E01 的固定快照单次调用成绩当作完整 Agent Loop 的评测成绩。
5. 不能声称修复/发布 Episodic Memory 已持久化；当前只有进程内日志。

## 5. B00 证据

- 原始日志：`v3/evidence/v35/B00/<UTC_TIME>-<RUN_ID>/`（`v3/evidence/` 被 `.gitignore` 排除，不入库）。
  - `01-git-baseline.log` 在注册本卡之后采集，因此会显示本卡自身的 `config/verification.json` 与 `docs/v35/`；HEAD 仍为冻结基线。
- 验收摘要：`v3/docs/v35/accept/V35-B00/accept.json`。
- 审计结论：`v3/docs/v35/ARCHITECTURE_CONTRACT.md`。
- 任务队列：`v3/docs/v35/TASK_QUEUE.md`。

## 6. 基线变更政策

- 本卡不修改业务代码、不引入依赖、不调用付费模型、不写 GitHub。
- 后续卡若改变基线事实（迁移 head、能力状态），必须在该卡 `accept.json` 中记录新的 `base_commit` 与偏差。
- 如出现基线之后的新提交，先做影响评估再执行下一卡；不得覆盖用户未提交修改。
