# MGAR V3.1

事件驱动的持续研发 Agent，按 V3.1 架构分阶段实施。

应用与运行命令位于 `v3/`；新任务状态位于 `v31-upgrade/TASK_QUEUE.md`。
原始 34 卡执行包和完整实验日志保留在本地开发工作区；公开仓库发布实现、测试、配置模板、任务卡和验收摘要。

2026-11-02 已依次通过 R0/R1、R2 和 R3 门禁。R3 交付时 150 项 V3 测试通过，0 失败、0 跳过；R3 任务验收 89 项通过。R3 增加受限 Router、批处理与版本校验、并行工单、同权限上下文召回，以及 Web/CLI/GitHub 共用的工单视图。

截至 R3 交付时，真实模型累计调用 3 次，保守计费 USD 0.002456。R1 发布一条结果评论；R3 只在两个冻结的合成事件上验证真实 Router，并通过结果落库后的 SIGKILL 恢复，不新增评论。完整项目仍为 ENGINEERING_INCOMPLETE，R4 真实跨源验收首轮双方各 4/5，live 为 PARTIAL。R5 已通过受限修复与真实 Draft PR 恢复验收；R6 工程实现已补齐，90 项真实最终评测已完成，候选质量门槛为 FAIL。
本项目使用独立仓库，不修改相邻 V2 的源码、锁文件、迁移或历史证据。

验收入口：[结果评论](https://github.com/Loz888/MAGR-v3/issues/1#issuecomment-6568469661)。最新摘要见 `v3/docs/acceptance/r1-live-20261102.json`；首批历史见 `v3/docs/first-batch-status.md`。

R2 实现与验收边界见 `v3/docs/adr/0002-r2-mailbox.md` 和 `v3/docs/acceptance/r2-engineering-20261102.json`。

R3 实现与验收见 `v3/docs/adr/0003-r3-router.md` 和 `v3/docs/acceptance/r3-20261102.json`。启动后在 `http://127.0.0.1:18080` 访问本地任务工作台。

## 本地运行

先把仓库克隆到自己的机器。`/workspace/MAGR-v3` 是开发云环境路径，不是 Windows/WSL 上的安装位置。
需要 Git、Make、uv、Python 3.12 和可用的 Docker Compose V2；Windows/WSL2 的依赖安装步骤见 [应用 README](v3/README.md)。

```bash
(
set -e
mkdir -p "$HOME/projects"
if [ ! -d "$HOME/projects/MAGR-v3/.git" ]; then
    git clone https://github.com/Loz888/MAGR-v3.git "$HOME/projects/MAGR-v3"
fi
cd "$HOME/projects/MAGR-v3"
git pull --ff-only
cd v3
uv python install 3.12
docker info >/dev/null
docker compose version
make setup
make deps-up
make migrate
make doctor
make dev
)
```

默认仅监听 `127.0.0.1:18080`，执行 fixture 模式。详细用法与实际验证范围见 `v3/README.md`。


R4 新增知识快照、中文混合检索、三专家图、只读 SQL 和确认摘要。更新到本机并运行真实本地检索，见 [R4 使用说明](v3/docs/r4-runbook.md)；工程与实际验证范围见 [验收摘要](v3/docs/acceptance/r4-20261102.json)。本轮新增付费调用为零。


R4 最新实际验收：三个正常、两个不足/越权案例已完成模型与单 Agent 对照；一次多专家恢复通过，原始首轮失败保留。工程完整回归 196 项通过。结果和费用见 [R4 真实实验报告](v3/docs/r4-live-report.md)。

R5 最终验收通过：249 项工程回归通过，真实 [Draft PR #2](https://github.com/Loz888/MAGR-v3/pull/2) 在接收后 SIGKILL 的只读恢复中成功对账，未合并。原始三次模型格式/定位失败保留；只规范化 hunk 行数后的重放单列通过。累计 29 次调用、USD 0.028549。见 [真实报告](v3/docs/r5-live-report.md)。

## R6 最新结果

监控、有限主动性、独立只读预算和三臂评测已实现。真实 90 项全部执行：A 15/30、B 15/30、C 16/30，质量门槛 FAIL；不能标 LIVE_READY。本轮 195 次调用、保守 US$0.087980，累计 224 次、US$0.116529，无未知预占。原始失败、进程中断和账单核对均保留。

工程完整基线 295 项、追加核账/恢复专项 61 项通过（有重叠）。最新范围见 [R6 交付报告](v3/docs/r6-delivery.md)。项目仍为 ENGINEERING_INCOMPLETE，V2 另有历史价格证据过期检查失败，未修改 V2。
