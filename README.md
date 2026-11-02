# MGAR V3.1

事件驱动的持续研发 Agent，按 V3.1 架构分阶段实施。

应用与运行命令位于 `v3/`；新任务状态位于 `v31-upgrade/TASK_QUEUE.md`。
原始 34 卡执行包和完整实验日志保留在本地开发工作区；公开仓库发布实现、测试、配置模板、任务卡和验收摘要。

2026-11-02 R0/R1 工程与真实闭环验收通过后，完成 R2 工程改造。当前 112 项 V3 测试通过，0 失败、0 跳过；R1 真实模型调用 2 次，保守计费 USD 0.001745，GitHub 结果评论一条，两个真实 SIGKILL 恢复点通过。R2 增加安全点处理后续输入、暂停/恢复/取消、持久只读任务和四个进程故障恢复实验；R2 工程使用受控模型与远端，不新增真实费用。R3–R6 尚未实施，完整项目仍为 ENGINEERING_INCOMPLETE。
本项目使用独立仓库，不修改相邻 V2 的源码、锁文件、迁移或历史证据。

验收入口：[结果评论](https://github.com/Loz888/MAGR-v3/issues/1#issuecomment-6568469661)。最新摘要见 `v3/docs/acceptance/r1-live-20261102.json`；首批历史见 `v3/docs/first-batch-status.md`。

R2 实现与验收边界见 `v3/docs/adr/0002-r2-mailbox.md` 和 `v3/docs/acceptance/r2-engineering-20261102.json`。

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
