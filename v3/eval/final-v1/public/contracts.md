# contract-0 2026-final-v1
订单服务操作契约：支付重试次数上限为 4。配置键 retry_limit。这是冻结版本的业务约束，不能从其他版本推断。

# contract-1 2026-final-v1
订单服务操作契约：订单去重窗口分钟为 17。配置键 dedupe_minutes。这是冻结版本的业务约束，不能从其他版本推断。

# contract-2 2026-final-v1
订单服务操作契约：接口缓存寿命秒为 53。配置键 cache_ttl。这是冻结版本的业务约束，不能从其他版本推断。

# contract-3 2026-final-v1
订单服务操作契约：接口分页条数上限为 80。配置键 page_limit。这是冻结版本的业务约束，不能从其他版本推断。

# contract-4 2026-final-v1
订单服务操作契约：只读查询超时秒为 7。配置键 timeout_seconds。这是冻结版本的业务约束，不能从其他版本推断。

# contract-5 2026-final-v1
订单服务操作契约：审计记录保留天数为 45。配置键 retention_days。这是冻结版本的业务约束，不能从其他版本推断。

# contract-6 2026-final-v1
订单服务操作契约：批次工作项上限为 12。配置键 batch_limit。这是冻结版本的业务约束，不能从其他版本推断。

# contract-7 2026-final-v1
订单服务操作契约：排队预警秒数为 75。配置键 warning_seconds。这是冻结版本的业务约束，不能从其他版本推断。

# contract-8 2026-final-v1
订单服务操作契约：重试退避秒数为 6。配置键 backoff_seconds。这是冻结版本的业务约束，不能从其他版本推断。

# contract-9 2026-final-v1
订单服务操作契约：订单金额审批上限分为 250000。配置键 amount_ceiling。这是冻结版本的业务约束，不能从其他版本推断。

# 收入口径 2026-final-v1
订单事件只计算 paid。每个 order_id 按 event_id 最早的一次 paid 计算去重金额；之前的 cancelled 不影响后来的 paid。事件金额逐事件相加，去重订单金额按订单计算。必须核查代码有没有进行去重。