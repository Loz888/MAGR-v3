# E/F 第二版修复档案 · 2026-11-06

**E/F 路由修复完成，真实路由均为36/36；正常任务 E7/8、F8/8，仍保留一项固定 Worker 输出失败。** 工程98项通过，真实8/8次运行完整、安全记录PASS。原D/E/F首轮报告、原A/B/C87/90及三项FAIL全部保留，不重评、不覆盖。

## 问题与修复

| 问题 | 证据与原因 | 本次处理 |
|---|---|---|
| 初始路由错误 | v1 E7项、F4项；模型自由生成动作和UUID，出现deliver/null及跨Issue目标 | 从公开事件和PG订阅生成合法候选，模型仅回选event_key/candidate_id，服务端解析预绑定目标 |
| 级联输入失败 | 初始启动失败后22项后续路由不符 | 修复初始准入；缺失主Run仍拒绝，绝不默默替建任务 |
| 状态检查与写入间隙 | v1检查和ingest跨事务；属于结构风险，未声称已实测出越权成功 | 原持锁连接内同一外层事务重核Issue、Run、epoch、revision、当前actor，再ingest/绑定/完成；异常整体回滚 |
| 已知格式错误无纠正 | v1直接失败 | 每批最多两个独立单调用slot；仅已结算格式/候选错误允许一次纠正；unknown/provider/budget/fencing不重试 |
| 恢复收据哈希为空 | v1读取不存在的usage_calls.response_hash | 读取原生model_response_recorded.safe_payload.content_hash，并在事务外核对CAS；旧空字段收据保留 |

[问题登记](issue-register.json)、[完整处理顺序](process.json)、[11个历史错误复现](historical-11-reproductions.json)、[ADR0015](../../adr/0015-coordination-candidate-contract.md)、[任务卡](../../../../v31-upgrade/tasks/V31-R6-COORD-REPAIR.md)。历史复现只验证新契约拒绝旧非法输出，并在同一公开快照上用受控合法选择解析正确start，新增付费调用0。

候选生成器不读取人工操作表、Oracle或e01/e02答案。它根据同Issue唯一主订阅、显式非空调查请求、模糊寻址及无关文本确定合法动作。本工作负载每个事件只有一个合法候选，模型实质执行复制选择，**改善主要来自软件约束，不能解释为模型推理能力提升**。产品原冷启动Router提示未改。

## 修复过程与验收

1. 保存旧报告及原A/B/C逐文件哈希，读取原11个非法初始输出及22项级联失败。
2. 实现严格选择DTO、候选绑定、同事务重核与回滚、独立纠正slot和已结算恢复收据。
3. 第一轮97项测试全部通过，但运行期间源码变化，验收器正确标记FAIL；该次记录[原样保留](engineering-invalidated-first-attempt.json)。稳定源码后98项通过，0失败/错误/跳过，[工程收据](engineering.json)。lint通过，111个模块类型检查通过。
4. 98项包括真实PG/Redis/LangGraph、跨Issue拒绝、revision/epoch变化、当前actor撤销、写入后故障回滚、未知首次/纠正请求不重发、双重格式错误上限、预算先于HTTP、原全部11项非法输出以及两个真实SIGKILL恢复案例。第二次有效响应后SIGKILL用受控模型完成，不能当作真实模型第二次纠正证据。
5. 源码提交`ff6353d`后冻结新版本`0d727c2d62b6310b131d5ab0d2a7235ba03734497c260b230c2fc21e969b7fa0`。独立授权与预算组，未借用v1剩余11次。矩阵为两重复×来源缓存冷/预热×E/F，共8次；原Worker/Verifier/来源/独立Oracle不变。
6. 真实运行60次调用，第一次候选选择全部合约有效，没有实际纠正调用。F首个有效已结算响应后真实SIGKILL，恢复复用同一响应，新增调用/费用均为0；旧epoch心跳被拒绝。
7. 原独立Oracle逐项判定，原PG账本核账。证据构建器曾误用passed字段及把Markdown当JSON，已修正读取器；未改执行源码、判定器或调用模型，首次构建失败日志留在私有索引中。

## 真实复验与剩余失败

| 版本 | E正常 | F正常 | E路由 | F路由 | 每臂负例 |
|---|---:|---:|---:|---:|---:|
| v1历史 | 1/8 | 4/8 | 15/36 | 24/36 | 12/12 |
| v2本次 | 7/8 | 8/8 | 36/36 | 36/36 | 12/12 |

这是开发者可见的同一小型合成题集上的前后修复复验，不是盲测；D未重跑，其历史7/8不能充当同期控制。没有预先冻结新的业务全通过门槛，也不宣称全项目零失败。

**保留FAIL：E r0/warm e01。** 期望完整行`Observation: The documented cache TTL is 90 seconds.`，固定Worker的statement和quote都输出`The documented cache TTL is 90 seconds.`。原生Verifier接受来源中的相同子串，所以Engine状态为succeeded；独立Oracle要求完整行，故业务判定FAIL。初始路由与两条后续输入均正确且消费完成。详见[完整残余失败](v2-residual-failure.json)。

没有放宽Oracle、补前缀、重发这一请求或修改旧成绩。后续若修复共享Worker，应采用完整引用目录/行号选择等协议，另冻结跨臂复验。本轮不改变固定Worker，避免在E/F协调修复中混入Worker变更。原D的Worker失败以及原A/B/C三项失败同样保留。

## 费用、证据与交付

本次60次、峰值保守US$0.005721；累计750次、US$0.311513；unknown与预占均为0。独立上限68次、US$0.208896，剩余8次不自动追加。计费是逐调用向上取整的冻结峰值上界，不是供应商发票金额。

[完整真实报告](../../experiments/coordination-v2/README.md)、[逐事件/任务/模型回执和恢复](../../experiments/coordination-v2/)、[旧报告](../../experiments/coordination-v1/README.md)、[43个历史文件哈希](historical-files-unchanged.json)。原始密钥、数据库连接、完整私有授权、原始供应商CAS、进程日志与数据库备份不公开；公开材料仅包含合成输入、业务生成内容、随机运行标识、用量元数据及哈希。

用户本轮明确允许修复结束后公开推送上述报告及脱敏证据至`https://github.com/Loz888/MAGR-v3`。发布源码/报告不等同运行GitHub业务评论、PR或合并。最终推送提交与远端核对记录随交付收据保存。
