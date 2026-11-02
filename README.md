# MGAR V3.1

事件驱动的持续研发 Agent，按 V3.1 架构分阶段实施。

应用与运行命令位于 `v3/`；新任务状态位于 `v31-upgrade/TASK_QUEUE.md`。
原始 34 卡执行包和完整实验日志保留在本地开发工作区；公开仓库发布实现、测试、配置模板、任务卡和验收摘要。

2026-11-02 已依次通过 R0/R1、R2 和 R3 门禁。R3 交付时 150 项 V3 测试通过，0 失败、0 跳过；R3 任务验收 89 项通过。R3 增加受限 Router、批处理与版本校验、并行工单、同权限上下文召回，以及 Web/CLI/GitHub 共用的工单视图。

真实模型累计调用 3 次，保守计费 USD 0.002456。R1 发布一条结果评论；R3 只在两个冻结的合成事件上验证真实 Router，并通过结果落库后的 SIGKILL 恢复，不新增评论。完整项目仍为 ENGINEERING_INCOMPLETE，R4 工程与本地检索验证已继续完成；后续真实跨源验收已运行，首轮双方各 4/5，live 为 PARTIAL，R5/R6 尚未实施。
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
