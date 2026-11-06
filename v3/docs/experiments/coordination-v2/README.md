# E/F 候选契约真实复验 · v2

北京时间：2026-11-06T10:24:08.934119+08:00 至 2026-11-06T10:25:10.648853+08:00。原始JSON保留UTC时间。

**8/8次真实运行完整，E/F路由各36/36，负例各12/12；正常任务E7/8、F8/8。** E的一项固定Worker完整行引用失败保留FAIL，不能把Engine成功状态当作业务通过。原A/B/C87PASS、3FAIL和v1全部结果保持原样。

| 方案 | 正常任务 | 负例 | 路由 | 原文引用有效 | Router / Worker调用 | 全部调用 | 峰值保守USD |
|---|---:|---:|---:|---:|---:|---:|---:|
| E | 7/8 | 12/12 | 36/36 | 8/8 | 32 / 8 | 40 | 0.003494 |
| F | 8/8 | 12/12 | 36/36 | 8/8 | 12 / 8 | 20 | 0.002227 |

引用有效检查来源/原文子串，不代替完整行输出契约。E剩余一项statement和quote均漏`Observation:`前缀，原Verifier接受但原独立Oracle判FAIL；[完整复现](../../repairs/COORD-20261106/v2-residual-failure.json)。本轮实际60个物理请求均有唯一供应商响应回执，unknown/预占0；[费用](finance.json)、[角色费用](role-costs.json)、[脱敏模型回执](provider-receipts.json)。累计750次、保守US$0.311513。

## 冻结条件与边界

- 执行源码`ff6353d`；manifest `0d727c2d62b6310b131d5ab0d2a7235ba03734497c260b230c2fc21e969b7fa0`。[完整manifest](manifest.json)、[独立授权来源与范围收据](user-approval-receipt.json)。本轮68次/US$0.208896共享硬上限，104个单调用备选profile，未转用旧授权余额；累计仍采用更低的US$2硬限制。
- 两重复×本地Worker来源缓存cold/warm×E/F，八次运行；每臂正常8、负例12、路由36。共80次物理投递、72个逻辑事件、16个正常任务。原事件、合成来源、真实LangGraph R2 Worker、并发2、独立Oracle、原Verifier均不变。
- E每条获准且非重复事件调用模型；F四条已订阅后续输入直接进入Engine/Mailbox，冷阶段按预定批处理。F四次合计16个直接投递；相对E少20次Router：16次来自直接投递，4次来自首批双事件合并。本轮没有失败启动导致的后续阻塞，不能将v1中那部分“少调用”算作效率提升。
- 修复采用公开快照生成合法候选，模型仅输出event_key/candidate_id；一个事件只有一个合法候选。动作由准入策略约束，因此结果改善不证明LLM协调推理提升。此处是实验准入适配器，产品冷启动Router原提示未改。
- 模型统一deepseek-flash，8192上下文/最多512输出、thinking disabled；峰值输入0.3/输出1.2USD每百万。temperature和模型seed未显式指定；种子20261105仅控制运行顺序，供应商内部revision和缓存不声称固定。
- cold/warm只控制本实验Worker来源Redis键；PG和供应商缓存未强制冷却。CLI/Web/GitHub为冻结输入来源标签，未声称真实Webhook已送达，未向GitHub业务评论/PR发布结果。
- 原D未重跑，其v1正常7/8仅为历史参照。小型、开发者可见、非盲测题集，两重复不足以作总体优越性或生产质量结论。原v1报告及全部新旧失败不修改。

## 逐项证据与恢复

[八次原始运行](trials/)、[独立结果](results.json)、[16个正常任务](normal-task-outcomes.json)、[72个事件](event-outcomes.json)、[80次物理投递](physical-deliveries.json)、[全部候选选择尝试](candidate-attempts.json)、[进程退出与续跑](execution.json)、[验收摘要](acceptance.json)。全部分母包含失败，无未运行样本冒充通过。

真实运行的44次Router选择均首次有效，额外纠正调用0；最多一次纠正的行为另由受控模型+真实PG回归验证。仅已结算输出格式/候选错误可进入新slot；unknown/provider/budget/fencing不重试。

F r0/cold在真实已结算响应后、事件落地前SIGKILL退出-9。显式恢复读取同一typed result，原call/response记录各1，复用新增请求/费用均为0，旧epoch心跳拒绝。恢复后剩余4次冻结请求正常计入同一新授权。[恢复收据](recovery.json)的响应哈希来自原生事件并核对CAS，已修复旧收据读取不存在列的问题。第二次纠正后的SIGKILL只在受控模型工程测试中发生，不能声称本轮真实模型发生过该纠正。

所有全局写入仍由Engine原持锁PG连接完成；产品17个迁移、原ABC绑定约束和未知调用规则不变。每臂4个重复投递被去重，丢失事件/越权效果/Action业务发布均为0。epoch检查证据包含真实恢复旧心跳拒绝及受控并发回归，不能据此声称生产整节点HA已验证。

## 延迟

单位秒；P50/P95为排序线性插值。来自同一PG事件及用量记录，不含Python导入和进程冷启动。内部报告耗时包含Engine成功但业务Oracle失败的那一项，不能称为全部业务成功耗时。

| 指标 | E P50 / P95（n） | F P50 / P95（n） |
|---|---:|---:|
| 事件接收到分派/拒绝 | 1.299815 / 2.829654（36） | 0.013039 / 1.435371（36） |
| 工作消息到Worker读取/输入 | 2.422807 / 5.128129（24） | 0.057138 / 1.887305（24） |
| Router预占到结算 | 1.046563 / 1.565581（32） | 0.935437 / 1.170190（12） |
| 任务到Engine内部报告 | 8.375863 / 9.355599（8） | 2.396738 / 3.472157（8） |

此工作负载下F调用较少、投递更快；不据一次15/16的小样本宣称F一般业务质量更好。Recall@5和隐藏补丁测试不适用于这组固定来源调查任务，原ABC代码修复成绩保持不变。

[修复档案与未解决问题](../../repairs/COORD-20261106/README.md)、[ADR0015](../../adr/0015-coordination-candidate-contract.md)、[旧D/E/F完整报告](../coordination-v1/README.md)、[私有证据哈希索引](private-evidence-index.json)。公开材料保留合成证据、输出、回执元数据和哈希；密钥/DSN/私有授权/原始CAS及日志不公开。

复现准备：按仓库runbook启动原PG/Redis并迁移；`make verify TASK=V31-R6-COORD-REPAIR`只运行受控模型测试。新的真实实验应使用全新目录执行`python scripts/coordination_eval.py freeze --repair-ef --directory <new-dir> --source-ref <commit>`，再依据独立有效许可显式execute；不要覆盖或重跑本轮目录。报告命令只读取已冻结记录，不调用模型。
