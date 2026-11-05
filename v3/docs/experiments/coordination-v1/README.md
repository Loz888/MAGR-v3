# D/E/F 全局协调对照

当前状态：工程80项PASS，新版本已冻结，真实模型实验NOT_RUN，尚无D/E/F完成率或节省比例结论。原A/B/C的87/90及三项失败原样保留。

| 方案 | 协调方式 | 固定Worker |
|---|---|---|
| D | 按冻结人工操作表创建/指定Run；不调用模型Router，不计人的思考时间 | 原真实LangGraph R2、原Verifier、相同模型/来源/单调用上限 |
| E | 每条获准且非重复事件先由同一模型Router决策，不批处理 | 同上 |
| F | 先检查真实primary订阅，后续事件直达原Engine/Mailbox；冷事件按预定阶段批处理 | 同上 |

实验为独立的协调准入适配器，复用产品Engine、事务、Mailbox、Graph和账本；E/F共用支持已有Run投递的受限JSON提示，和产品只处理冷启动的Router提示有差别。不能把本实验称作未经改动的产品Router性能复验。

两次重复×Worker来源缓存冷/预热，共四个配对区组，每区组三臂按种子20261105随机排序，共12次。每次两个正常任务、四条后续消息、无关/模糊/越权事件和一次重复投递（十次物理投递、九个逻辑事件）。D采用预先写好的人工操作表；E/F模型输入不含该表或独立Oracle。

固定Worker在真实读取安全边界接收后续输入，然后基于同一个合成来源作一次真实模型引用调查。每臂正常任务8项、负例12项、路由36项，未执行项保留NOT_RUN，不缩分母。没有检索搜索或代码修复任务，Recall@5与隐藏补丁测试为N/A；原A/B/C的相关指标单独引用。

本地缓存仅覆盖Worker来源；冷区组只删除本次Redis键，预热开销单列。PG与模型供应商缓存不强制清空，供应商缓存命中按实际usage记录。记录事件至分派、开始/输入消费、模型Router、任务完成P50/P95，以及角色分别的调用/token/费用和batch大小。

一个预定F区组在真实Router响应已结算、业务事件尚未投递时SIGKILL本实验进程。监督器核对原调用已知结果和固定源码后，显式续跑同一Run并拒绝旧epoch；原响应不得再次请求，剩余冻结调用继续计入同一预算。unknown保留预占并停止，不重发、不自动退款；不对主机或原数据库注入故障。工程另用受控HTTP服务验证实际PG unknown、越权目标、预算耗尽和来源引用篡改，不能计作真实模型成绩。

预计也是硬上限68次（Router44、Worker24）；每次8192上下文、最多512输出，按当前已审阅峰值输入0.3/输出1.2USD每百万计，最大US$0.208896。费用为保守上界，不冒充供应商精确账单。旧授权绑定原manifest，本次真实执行需要单独匹配的新许可；仅环境开关不能启用。

外发仅包括本题集中的合成事件、合成来源、受限规则和本次测试Run标识，目的地为DeepSeek官方 `https://api.deepseek.com/v1/chat/completions`。不发送Oracle、人工操作表、真实凭据、连接串、账单、私有代码或客户数据。不创建GitHub业务评论、PR或合并。

## 执行

```bash
make lint
make typecheck
make verify TASK=V31-R6-COORD
.venv/bin/python scripts/coordination_eval.py freeze \
  --directory .local/coordination-v1-UNIQUE --source-ref COMMITTED_SOURCE_SHA
# 审阅具体manifest/题集/外发范围后提供匹配的独立授权文件；提案默认allow_paid=false。
.venv/bin/python scripts/coordination_eval.py execute \
  --directory .local/coordination-v1-UNIQUE \
  --authorization .local/coordination-v1-UNIQUE/authorization.json --execute-paid
.venv/bin/python scripts/coordination_eval.py report --directory .local/coordination-v1-UNIQUE
```

禁止覆盖执行目录或静默重开整个实验。执行器与独立判定分别保存逐事件记录、模型结果、来源和费用；完整报告应包含所有12次及故障/续跑证据、原A/B/C文件哈希核对。新模型失败按实际记录，不修改旧成绩。

[工程验收](engineering.json)、[日志哈希](engineering-log-hashes.json)、[开发问题与修复](development-issues.json)、[ADR0014](../../adr/0014-coordination-comparison.md)。

冻结源码 `8bba1637ddada3129d13b09bfe6d88ec28b2f856`；manifest `9df391cdb86900b4c8e1f83968c211ea3025f3a51e40b2ad855b61a41f9b4d29`。见 [冻结收据](freeze.json)、[完整冻结清单](../../../eval/reports/coordination-v1-manifest.json)、[默认不启用付费的授权提案](../../../eval/reports/coordination-v1-authorization.proposed.json)。最多新增US$0.208896，基于原保守US$0.299317，累计上界US$0.508213，低于原仓库US$2硬上限。
