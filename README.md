# MGAR V3.1

事件驱动的持续研发 Agent，按 V3.1 架构分阶段实施。

应用与运行命令位于 `v3/`；新任务状态位于 `v31-upgrade/TASK_QUEUE.md`。
原始 34 卡执行包和完整实验日志保留在本地开发工作区；公开仓库发布实现、测试、配置模板、任务卡和验收摘要。

2026-11-02 已依次通过 R0/R1、R2 和 R3 门禁。R3 交付时 150 项 V3 测试通过，0 失败、0 跳过；R3 任务验收 89 项通过。R3 增加受限 Router、批处理与版本校验、并行工单、同权限上下文召回，以及 Web/CLI/GitHub 共用的工单视图。

截至 R3 交付时，真实模型累计调用 3 次，保守计费 USD 0.002456。R1 发布一条结果评论；R3 只在两个冻结的合成事件上验证真实 Router，并通过结果落库后的 SIGKILL 恢复，不新增评论。完整项目仍为 ENGINEERING_INCOMPLETE，R4 真实跨源验收首轮双方各 4/5，live 为 PARTIAL。R5 已实现受限修复及真实适配，最终发布验收进行中；R6 尚未开始。
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

R5 最新实际验收：三次真实模型调用的原始 diff 校验均失败；确定性重算 hunk 行数后，在独立恢复 Run 重放第一份已结算结果，公开测试和 28 个独立隐藏案例通过，无新增调用。完整 Draft PR 提案等待审批，真实发布与响应丢失恢复尚未执行。累计 29 次、USD 0.028549；R5 为 IN_PROGRESS/PARTIAL。见 [R5 验收摘要](v3/docs/acceptance/r5-live-20261103.json) 和 [使用说明](v3/docs/r5-runbook.md)。

R5 规范化与重放工程完整回归：248 项通过，0 失败、0 错误、0 跳过；lint、类型检查和前端语法通过。真实发布验收仍待完整提案审批。

R5 最终验收通过：249 项工程回归通过；真实 Draft PR [#2](https://github.com/Loz888/MAGR-v3/pull/2) 经接收后 SIGKILL 只读恢复成功，未合并。原始模型试验失败保留，规范化重放单列通过。累计费用仍 USD 0.028549；R6 将从确定性 Watchdog 开始。详见 [真实报告](v3/docs/r5-live-report.md)。
