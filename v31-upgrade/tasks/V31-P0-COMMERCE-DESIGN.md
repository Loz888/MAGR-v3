# V31-P0-COMMERCE-DESIGN · 电商工单闭环设计

状态：DESIGN_COMPLETE；实现与运行验收：NOT_STARTED / NOT_RUN。

用户范围：只完成上一轮能力清单中的两个P0设计，其余能力不改进。本卡对应“完整电商工单闭环”，与结果判定卡一起交付；不启动R7，不新增模型、实验或GitHub业务操作。

已有基础：R4知识/受限单表SQL/GitHub调查、R2 Mailbox、R5纯函数沙箱/审批/Draft PR、R6预算和恢复。将其整合为“研巡”验证原型的设计，不将分散通过记录描述成业务端到端已通过。

交付：[总入口](../../v3/docs/design/p0-commerce-v1/README.md)、[闭环设计](../../v3/docs/design/p0-commerce-v1/01-commerce-workflow.md)、[任务契约](../../v3/docs/design/p0-commerce-v1/contracts/task.schema.json)、[验收矩阵](../../v3/docs/design/p0-commerce-v1/acceptance-plan.json)。

设计完成条件：明确用户、三个正常场景、两个不足/越权场景；给出调查与修复两个Run的关联、权限/预算隔离、数据和接口草案、工作台视图、重复/追加/取消/未知结果处理、分阶段实施顺序和可核对验收标准。

边界：保持现有本地orders单表与封闭SELECT语法、一个Go纯函数体、单Engine持锁写入、原Action/Outbox与人工审批；不扩展通用SQL、跨文件/多语言修复、复杂Router、团队登录或生产HA。设计稿中的新接口/表尚未存在，不生成产品迁移。

验证：仅检查文档链接、JSON Schema、设计样例和计划数值一致性；不以文档检查替代代码或真实业务验收。原A/B/C87/90及三项失败、D/E/F历史和费用账本保持原样。
