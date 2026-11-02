# MGAR V3.1

事件驱动的持续研发 Agent，按 V3.1 架构分阶段实施。

应用与运行命令位于 `v3/`；新任务状态位于 `v31-upgrade/TASK_QUEUE.md`。
原始 34 卡执行包和完整实验日志保留在本地开发工作区；公开仓库发布实现、测试、配置模板、任务卡和验收摘要。

2026-11-02 R0/R1 工程与真实闭环验收通过：88 项 V3 测试，0 失败、0 跳过；真实模型调用 2 次，保守计费 USD 0.001745，GitHub 结果评论一条，两个 SIGKILL 恢复点通过。完整项目仍为 ENGINEERING_INCOMPLETE；通过 R1 后开始 R2。
本项目使用独立仓库，不修改相邻 V2 的源码、锁文件、迁移或历史证据。

验收入口：[结果评论](https://github.com/Loz888/MAGR-v3/issues/1#issuecomment-6568469661)。最新摘要见 `v3/docs/acceptance/r1-live-20261102.json`；首批历史见 `v3/docs/first-batch-status.md`。

## 本地运行

```bash
cd v3
make setup
make deps-up
make migrate
make doctor
make dev
```

默认仅监听 `127.0.0.1:18080`，执行 fixture 模式。详细用法与实际验证范围见 `v3/README.md`。
