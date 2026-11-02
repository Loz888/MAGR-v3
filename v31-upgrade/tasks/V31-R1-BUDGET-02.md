# V31-R1-BUDGET-02 · 候选最多 3 次调用、总预算 USD 2

状态以 TASKS.json 和 TASK_QUEUE.md 为准。依赖：V31-R1-01。

用户指定候选上限：max_model_calls=3、max_cost_micros=2000000。同步 profile、哈希、预检、PG 次数限制和运行说明。费用为三次合计上限；旧预算保留 1 次，未知用量与恢复结果不得绕过次数或费用限制。

验证真实 PG 并发、费用先耗尽、跨 Run 累计、已结算不返还次数、旧授权失效与旧迁移升级。执行 make -C v3 verify TASK=V31-R1-BUDGET-02，保留原始日志与退出码。

本任务只准备候选配置，不触发真实模型请求、GitHub 回写、push 或后续阶段。
