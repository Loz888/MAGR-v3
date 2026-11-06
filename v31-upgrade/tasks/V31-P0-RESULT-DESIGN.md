# V31-P0-RESULT-DESIGN · 统一业务结果判定设计

状态：DESIGN_COMPLETE；实现与运行验收：NOT_STARTED / NOT_RUN。

用户范围：只完成两项P0设计；本卡对应“统一的业务结果判定”。不借此实施P1/P2能力、追加模型调用或更改历史成绩。

交付：[结果判定设计](../../v3/docs/design/p0-commerce-v1/02-business-result.md)、[业务结果契约](../../v3/docs/design/p0-commerce-v1/contracts/result.schema.json)、[展示投影契约](../../v3/docs/design/p0-commerce-v1/contracts/view.schema.json)、[状态规则](../../v3/docs/design/p0-commerce-v1/status-rules.json)、[历史问题回归计划](../../v3/docs/design/p0-commerce-v1/acceptance-plan.json)。

设计完成条件：Engine执行、独立业务判定、审批与外部交付分离；严格结构、精确引用目录、Decimal数字与来源绑定；所有提交重核输入版本/epoch/当前授权；缺证据/拒绝/格式失败/未知分开；旧结果不可变，新输入产生新判定；展示与写出使用同一判定规则。

已知输入：原A/B/C三项失败（C cross-05输出契约、B两项修复遗漏paid过滤）、v2 E r0/warm e01完整行前缀丢失，以及v1 D statement/quote不一致。分别纳入新版本回归计划，原结果不重评、不补字改成PASS。

边界：独立判定器为有界业务规格的确定性代码；不引入LLM自评、任意语义正确性声明、通用代码验证或自动上线。旧app.runs.status、verdicts、Action状态及历史Oracle保持兼容。

验证：仅设计一致性检查；实施和工程/真实验收各自保持NOT_RUN。已有R6门禁不因新增设计被重置。
