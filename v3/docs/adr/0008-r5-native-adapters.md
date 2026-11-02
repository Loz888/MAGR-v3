# ADR 0008：R5 真实源码、模型与 Draft PR 适配

状态：实现并进入验收。此 ADR 增补 0007 的受控工程范围；不把工程测试替代真实付费与 GitHub 写入验收。

## 源码与测试证明

真实入口使用独立 `r5-repair-v1` profile，固定仓库、Issue、Go 子目录、基础提交、完整 Git tree 和源码快照 SHA256。官方 MCP 读取的三个文件必须与从同一 GitHub 仓库 fetch 的 Git blob 一致。保留全仓库 tree 的 mode/blob 元数据，候选只替换批准的 `orders.go`，因此测试目录外的任何变化都会导致发布 tree 校验失败。

官方 GitHub MCP v2.0.3 的 `get_commit` 省略 parent、tree 和文件 blob。生产适配器先通过 MCP 解析引用，再读取已校验哈希的 Git 对象补齐这些事实，不能假设缺失字段存在。真实 PR 读取结构已用官方只读工具核对。

沙箱只接收冻结子目录的三个 Go 文件；模型只接收公开源码、问题及公开反馈，拿不到完整目录树、隐藏测试、Docker 控制权或凭据。继续保护原始 `fixtures/repair-demo`；真实验收目标放在 `examples/repair-target`，避免修复验收样例影响原有缺陷复现测试。

## 模型与累计账本

真实补丁经 `StructuredModel` 调用 DeepSeek，校验返回模型名、JSON、用量和冻结峰值价格。最多三次物理调用由 profile 共享，不能通过新输入版本增加次数。格式或 diff 错误可在剩余次数内产生新的修复步骤；已知结果重用，未知用量不重发。

迁移 0011 增加累计费用组。组标识由项目和仓库固定，已有前序根账本及累计上限不能通过换配置重置。R1/R3、R4 根支出与所有 R5 profile 的已结算、预留金额统一计入 US$2；旧 R4 子 profile 的后续预留也经过同一检查。组中未知用量会阻止新调用。旧 profile 的 JSON 和哈希保持兼容。

R5 可恢复已经明确结算的失败模型步骤：再次访问该步骤只读原结果，不重发；后续步骤仍受原三次总次数、费用与 Run 期限限制。保留未知调用和未知外部效果对恢复的阻断。模型结算后的 SIGKILL 已纳入测试。

## 发布与部分成功

真实发布仅支持官方锁定 MCP 的 create_branch、push_files、create_pull_request，禁止 merge、force push、任意命令或其他业务写。Dispatcher 按动作类型和 profile 领取，不能消费另一个试验的批准动作。每次写前核对当前基线、epoch、primary、Mandate、目标写权和完整 payload hash；提交后核对父提交、完整 tree、文件 blob 和最终 Draft PR。

响应未知时不会自动继续。只读对账可将已精确确认的分支或提交子步骤记为成功；查无对象不是可重发的证据。操作员显式提交原 payload hash 后，只有“所有已发送步骤均确认成功、后续步骤尚无发送记录”的连续前缀才可继续。执行器跳过已成功步骤，只发送此前未发送的步骤。输入、权限、期限或基线变化仍会阻止继续。Watchdog 无权调用此授权入口。

## 产品与验收

Web/CLI 与 API 共用真实/演示模式、完整补丁审批、人工编辑重测、只读对账和显式继续入口。部署开关不等于调用或发布授权；真实模式需要单独的私有授权文件，写入还需对最终完整提案批准。

真实验收工具 `scripts/r5_live.py` 分离 prepare、model、publish、reconcile、continue。prepare 只读取并冻结，生成的授权草案默认全部关闭；model 必须显式执行付费；publish/continue 必须提供准确的已批准 payload hash。可在成功模型结果结算后或 PR 接收后故意 SIGKILL，再验证恢复不重复请求。

在真实验收通过前，R5 整体仍在进行，R6 不提前启动，系统仍为 ENGINEERING_INCOMPLETE。
