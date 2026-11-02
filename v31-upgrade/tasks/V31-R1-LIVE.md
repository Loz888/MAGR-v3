# V31-R1-LIVE · V3 Issue #1 真实模型、审批与结果评论验收

依赖：V31-R1-01。状态以 ../TASKS.json 和 ../TASK_QUEUE.md 为准。

## 范围

使用用户指定 V3 仓库的测试 Issue #1，真实读取冻结来源、模型回答、独立验证、人工审批和一条结果评论。2026-11-02 候选上限改为最多 3 次物理模型调用、累计费用不超过 USD 2；单个 Run 仍只包含一个模型步骤，未知调用不自动重发。保留费用与外部回执，不能将创建 Issue 代替真实业务完成。

## 验收

mgar live-preflight --profile config/live-r1-proposed.json；获得已覆盖授权后才执行真实 case

证据位于 v3/evidence/V31-R1-LIVE/。工程与 live 状态分开记录，保留失败尝试；不以 skip、零测试或固定返回 PASS 代替验收。

2026-11-02 依据用户明确授权执行并通过，详情见 v3/docs/acceptance/r1-live-20261102.json。首个已计费响应结构失败并保留，第二次通过。完整结果由代理操作员检查并按会话委托审批；没有声称用户手动点击审批。MCP 读写进程工具隔离，共用现有 GH_TOKEN 凭据引用。
