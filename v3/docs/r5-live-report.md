# R5 真实修复与 Draft PR 验收

2026-11-03，真实发布及恢复闭环通过；包含显式续签的最终工程回归 249 项通过，0 失败、0 错误、0 跳过。原始模型试验失败与后续恢复验证分别报告，整个项目仍为 ENGINEERING_INCOMPLETE。

## 冻结输入与原始失败

- 自有仓库 Loz888/MAGR-v3，Issue #1；仅允许修改 `v3/examples/repair-target/orders.go` 中的纯函数。
- 基础提交 `c6f072e5994cb57fedd0bad796478b005870c7b6`，完整基础 tree `ceea02604d7cdd4af3b9bc4d6cf8fc8b5466ae22`。
- 冻结 profile `807d8c5e68601cb945b28c35cc80de92ab764b0e898d3725445f7c3fc986925e`。
- 实际三次 DeepSeek 输出分别被 PATCH_FORMAT、PATCH_FORMAT、PATCH_STALE 拒绝，没有候选测试或发布。原始实验为 FAIL，不把后续结果回填为首轮成功。

## 不新增调用的恢复验证

确定性规范化仅把第一份结果的 hunk 头 `@@ -7,8 +7,16 @@` 改成 `@@ -7,9 +7,16 @@`。代码、路径及起始位置均未修改。原始 Run 到期后，在独立 Run 使用同一冻结 profile 重放已结算结果；不延长旧 Run，不清空调用计数，没有网络或付费回退。

| 实际验证 | 结果 |
|---|---|
| 原始公开测试 | exit 1，缺陷复现 |
| 规范化后公开 Go 测试 | exit 0 |
| 独立 Oracle 隐藏测试 | 28/28，exit 0 |
| 模型结算后 SIGKILL | 已结算结果恢复复用，无重复调用 |
| GitHub PR 接收后 SIGKILL | exit -9；重启后只读找到精确对象，无重复发布 |

镜像以 SHA256 固定；实际沙箱非 root、断网、只读根文件系统、无宿主挂载、无凭据，保留资源及时间上限。

## 获批发布与真实对象

用户明确批准完整提案后创建 [Draft PR #2](https://github.com/Loz888/MAGR-v3/pull/2)，保持 Draft，未合并。

- 完整 payload hash：`ea5145bb41da599d4f6789b21f8f2d56daf50519a5af2a2f5bf5e8e5a75f1dd3`。
- Head：`8c1163b4edf16ef27bcebf97f41e1b28b4b6a9a5`；唯一父提交为冻结基础提交。
- 完整候选 tree：`225486485923c602efd450fb6cc9c92e5c25b125`；与测试、审批及真实 Git 对象一致。
- 唯一变化文件的 blob：`c8be0ddd3804185edc79924baa62b317b99fb46d`。
- Outbox 仅派发一次；branch/commit/pull_request 三个子步骤均 succeeded，只有一条 action_reconciled 事件。

批准到达时原提案的一小时有效期已结束。依据本轮明确人工批准，追加独立审阅记录：02:38:05 UTC 审阅，有效至 03:38:05 UTC。原 Action 内容、原到期时间及完整哈希不变。默认过期审批仍拒绝；显式续签不批准、发送或重试任何动作。见 [ADR 0010](adr/0010-explicit-review-renewal.md)。

## 次数、费用与范围

R5 实用 3/3 次调用，均已结算，用冻结峰值价格保守计费 US$0.001524，预留和未知均为零。历史累计 29 次、US$0.028549，低于 US$2；本次发布及重放新增调用为零。发布许可在成功对账后关闭。

这证明一个受限纯函数任务的真实恢复闭环；不代表任意仓库修复能力或独立修复测试集成绩。R4 live 仍为 PARTIAL，R6 完整对照评测尚未执行。

完整机器摘要见 [r5-live-20261103.json](acceptance/r5-live-20261103.json)。原始模型输出、私有授权、原始失败与恢复日志保存在本地 `.local/`、`evidence/`，不进入公开源码。
