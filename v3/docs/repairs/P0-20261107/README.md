# P0 原提案复验与真实 Draft PR 交付

本档案接续[2026-11-06实施与真实模型实验](../P0-20261106/README.md)。用户于2026-11-07回复“批准”，对应已展示的三份完整提案。原件固定于[提交34f1c7c](https://github.com/Loz888/MAGR-v3/blob/34f1c7c39808c3ffe729e8e746fa1e8391b087c1/v3/docs/repairs/P0-20261106/draft-pr-review.md)，不替换原payload_hash、补丁或目标。本次不调用模型、不合并、不发布Issue评论。

## 问题与解决过程

| 问题 | 定位与处理 | 验证依据 |
|---|---|---|
| 用户批准到达时证据已过期 | 原SQL观察在北京时间11月6日22:48左右失效；原Action仍proposed、尚无Outbox或远端结果。保留原BusinessResult与fresh_until；新增显式复验证书，绑定准确Action/result/Case/input/epoch | 新证书及实际Go日志单列，不回写旧记录 |
| 直接重跑会增加调用并改变原提案 | 读取原CAS候选；重核知识引用/ACL、固定GitHub全树、实际SQL行与聚合及Decimal指标；在原镜像中重测同一补丁 | 原文件、workspace、tree、镜像、测试清单与获批payload逐项匹配 |
| 过期审批需在原发布协议内续签 | 证书通过后，沿用原renew_review、准确hash审批、Action/Outbox及Dispatcher三步发布；每步仍重核当前权限和输入 | 远端父提交、全树、唯一变更文件、head/base、正文、标题、Draft状态与原提案匹配 |
| 后续失败不能沿用旧证书 | 证书追加而不覆盖；最近一次失败阻止资格，过期证书也拒绝；原业务PASS作为历史事实保留 | 专项测试包含失败、跳过、数据变化、证书过期、actor/hash/input/epoch/ACL及测试期间变更 |
| 重启后临时运行环境失效 | `.venv`和mypy缓存指向已清空的/tmp；按原uv.lock重建Python3.12环境，未改依赖 | lint、typecheck及新建真实PG迁移检查 |
| 沙箱创建时根盘余量很低 | 当前VFS会复制沙箱文件系统；Go测试严格串行。原主库卷、V2数据与固定镜像保留。旧私有备份归档校验后恢复2699个文件；一个无引用V3旧卷的1372个文件完整归档并核对哈希后收存 | 私有备份不公开；该归档未明显改善底层根盘余量，不能写成容量问题已根治 |
| 费用统计口径容易漏掉Router | 单查usage_calls得到758次；仓库统一cost函数同时包含router_calls，完整累计仍759次、US$0.313148 | 发布前后均按同一函数核对，不改历史账本 |

实现见[ADR0018](../../adr/0018-exact-proposal-review.md)、追加迁移0019与`commerce.review`。文件、网络、SQL读源和Go均在业务事务外；落证书时重新核对当前权威。复验SDK只供可信操作者显式调用，不提供任意HTTP提权入口。旧共享付费grant不续期、不重置余额。

## 验收与范围

两项P0在固定范围内已完成实施与验收。三份准确Draft PR均已核对远端父提交、完整tree、唯一变化文件、head/base、标题、正文及Draft状态；三份均保持open/Draft、未合并，未发Issue评论。原Action payload及原业务结果均未改写。

| 检查 | 结果与原始回执 |
|---|---|
| 新增复验门禁 | [13项通过](engineering-review-13.json)，0失败/错误/跳过，含真实PG/Redis、迁移幂等、Go及权限/版本/事务边界 |
| 原P0回归 | [30项通过](engineering-p0-30.json)，0失败/错误/跳过，与13项门禁源码指纹一致 |
| 静态检查 | [lint、126模块typecheck通过](quality.log) |
| 原正常调查/修复提案 | 各3/3，沿用[原真实模型结果](../P0-20261106/live-results.json)，本次不重复调用 |
| 原缺SQL/撤权负例 | 2/2正确处理，各0次模型调用，保留needs_input/denied状态 |
| 新来源与Go复验 | 3/3通过，同一原补丁在原固定镜像中通过公开、28个独立子案例和实际Case窗口测试 |
| 真实交付 | [#3 · N01重复支付](https://github.com/Loz888/MAGR-v3/pull/3)、[#4 · N02状态过滤](https://github.com/Loz888/MAGR-v3/pull/4)、[#5 · N03退款状态](https://github.com/Loz888/MAGR-v3/pull/5)，3/3完成远端核对 |
| 本次调用与费用 | 新增0次、US$0；累计759次、保守US$0.313148，unknown/预占均0；原共享付费grant仍过期 |
| 历史保留 | [239份原文件及30份原P0报告均未变](history-preserved.json)，原ABC 87/90及全部D/E/F失败不改写 |

[机器摘要](acceptance.json) · [完整交付回执](delivery-results.json) · [证据哈希索引](artifact-index.json)。新源码指纹为`510ea02781763ded239b5ddef6c81f7bad3127ca38d2a07d33bd2d2be0618fdf`。主库追加迁移0019，原0018及以前迁移未变。原404项全量回归保留其历史源码范围，没有把本次43项专项当作新全量验收。旧P0注册卡的组件文字仍标18迁移，实际本次新库应用19项，并在复验门禁中断言。

每份证书及真实Go日志位于[evidence/](evidence/)。证书保存实际SQL结果、来源hash、重算指标、原payload/result/input/epoch绑定及新测试回执。原模型实验与获批原件保留在前一日目录，原件中的“待审批”是当时快照；当前交付状态以本档案为准。授权续签仅用于原修复stage的发布，不续期共享模型grant，也不重置任何预算。

这仍是三个固定合成业务场景和受限Go函数的整合验证，不扩大为任意仓库自动修复或生产环境验收。原PDF/HTML保持P0实施前快照，最新实现与交付以本附录为准。
