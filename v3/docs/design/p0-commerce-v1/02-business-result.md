# P0-B：统一业务结果判定设计

状态：DESIGN_COMPLETE；检查器、附加记录与投影尚未实施。此规格只服务[P0-A](01-commerce-workflow.md)和已知失败的新版本回归。

## 1. 四种状态各自保留来源

| 维度 | 权威来源 | 含义 |
|---|---|---|
| 执行 | 原app.runs.status/outcome、epoch、input_revision | 本Run是否完成流程、暂停、等待或失败 |
| 业务判定 | 新不可变business_result | 当前规格下的证据、输出和有界业务接受条件是否通过 |
| 审批 | 原Action、approval/decision、准确payload_hash和期限 | 用户是否批准了当前完整提案 |
| 交付 | 原Action/Outbox/发布子步骤与远端回执 | 是否确实交付对应目标，或结果仍未知 |

`run.status=succeeded`不能直接生成业务PASS。引用是来源中的子串也不能自动满足“完整行”契约。审批通过不等于Draft PR已创建，Draft PR已创建不等于合并或上线。

保留原数据库枚举和原API语义；新增Case结果接口提供统一视图。旧记录若没有新业务结果，显示“历史结果未按本契约评估”，不能默认PASS，也不改原判定。

## 2. 有界规格与判定结果

每个task spec固定公开接受规格hash、知识/数据/代码引用和任务目标。独立业务检查器为可信确定性代码，不读取隐藏答案来决定线上输出，不接受模型自报PASS或模型自报权限。

结果枚举：

| business_status | 条件与产品含义 |
|---|---|
| passed | 本stage所有必需检查均passed，允许的可选检查按规格为not_applicable |
| failed | 已拿到完整可判定材料，但结构、引用、数值或测试不符合规格 |
| needs_input | 所需来源缺失、数据冲突或口径无法在本规格下确定，列出缺项 |
| denied | 当前任务或来源权限被拒绝；隐藏受限正文和可利用定位信息 |
| unknown | 已发出的模型/外部结果无法确认，或关键检查结果无法核清；保留预占并对账 |

未产生结果时展示`not_evaluated`，它不作为已完成business_result存储。预期拒绝/不足案例可在独立评测里记“处理正确”，产品仍显示denied/needs_input，不伪装成“调查成功”。

结果契约为[result.schema.json](contracts/result.schema.json)。包含Case/Run/input/epoch/规格hash、stage、报告/候选哈希、四组检查、原因码、受控事实与来源引用。结果本身不可变。一次显式复核或新输入形成新的result_id，当前指针只指向当前版本；不覆盖旧失败。

## 3. 四组必需检查

`scope`、`evidence`、`contract`、`acceptance`各有status和reason_codes。检查状态为passed/failed/unknown/not_run/not_applicable；本切片四组都是必需，passed结果不能用not_applicable跳过其中任何一组。

### 3.1 scope

检查当前project/repository/actor、同Case/Run来源、公开工具能力、Mandate/profile、输入revision和epoch。提交时必须与原Engine当前行匹配；失配返回STALE_RESULT/FENCED并留下拒绝审计，不能提交一个“最新业务结果”覆盖现有结果。

原任务不支持的请求返回denied；schema已坏的输入返回422，不能让模型替客户端修权限。预算/unknown沿用原账本检查，结果状态不构成退款或新调用许可。

### 3.2 evidence

保留原Verifier对实际读取、工具/assignment归属、当前ACL、CAS hash、来源版本、SQL typed cell和TTL的检查。追加本契约声明的完整引用单元及来源一致性检查。

采用服务端实际来源目录：每个quote_ref绑定source_id、content_hash、unit_kind、精确行区间和unit_hash。知识/代码完整行保留原始空格、标点和`Observation:`等前缀；多行单元由接受规格预先允许。目录必须从本Run实际可读内容生成，不能从Oracle/人工答案生成。目录绑定输入版本与来源hash，不允许只凭旧quote_id复用。

模型选择quote_ref，服务端精确渲染statement/quote所需内容。自由解释文字单独标为explanation，不当作已验证事实。若仍采用自由引用输出，必须逐字符合单元规则；缺前缀、statement/quote不一致均failed，不使用模糊匹配、自动补字或静默改写来救成PASS。

模型线上DTO沿用现有有界Answer/wire风格，仅接收报告类型建议、规范values、短quote_id选择及explanation；修复阶段仅接收原受限function_body。模型不填写business_status、checks、actor、Run UUID或审批信息。短ID经绑定本Run/输入/来源的服务端目录解析成结果Schema中的完整quote_ref，不能让模型自行构造哈希或定位。实施时抽取可复用的目录/DTO适配部分，不能把benchmark Oracle、case标签或隐藏答案引入线上检查器。

ACL撤销先阻止目录读取和输出，不能先渲染敏感正文再给denied。代码引用固定到具体commit/blob；后续main变化不能偷偷改变已引用内容。

### 3.3 contract

用严格DTO/Schema检查准确键集合、必需来源角色、规范数值/布尔字符串、目标类型和补丁字段。解析失败不是“证据不足”，记录OUTPUT_CONTRACT_FAILED，并保留原模型输出hash。

金额只接收有限十进制字符串，不接受JSON浮点、NaN、Infinity或隐式舍入。金额按元保留两位，SQL原始OID与原始文本单独留在回执；转为Go整数分必须精确。布尔事实沿用当前Answer的`"true"/"false"`编码，展示层可转换成中文。

numeric_receipt_hash指向可信计算回执，至少记录Run/input/spec、实际读取ID/result_hash、观察时间、每个operand的row/column/OID/原值、公开formula_id和Decimal结果。检查器独立读取typed cell重算，不把模型自己提供的operands或计算结果当成权威。Schema中的hash只是引用，哈希格式通过不能替代实际字节与来源验证。

如使用有界纠正，次数、阶段profile和预算必须预先固定；每次已知失败单列，不覆盖原结果。未知/超时/费用缺失不进入纠正路径。该设计不增加原协议的调用上限，具体许可由未来冻结执行方案提供。

### 3.4 acceptance

调查stage在注册的三种合成业务模板中核对：时间窗、paid过滤、行数与唯一订单数、按公开口径去重的金额、事件金额和差额；每个事实都要绑定实际SQL typed cell或可信Decimal计算回执。行数达到上限、结果截断或同order_id的paid金额冲突不能给完整核账PASS。

源码根因解释的自然语言不具有自动证明。事实区只放有回执的数值/引用；推测放在“待验证假设”。只有固定源码的沙箱回归已复现问题时，才能标记“缺陷已复现”。通过调查契约不自动意味着所有因果解释都正确。

修复提案stage进一步要求：原AST范围通过、准确diff/tree/workspace_hash与公开/独立测试回执一致；两个测试组均通过、无跳过/截断/超时且输入/源码版本未变。独立测试失败必须failed；语法正确或公开测试通过不能代替业务通过。

两类stage分别存储result；不能用调查报告PASS代替修复测试PASS。未来P0评测Oracle对冻结夹具独立计算预期结果并运行隐藏测试，不与业务检查器共享“直接返回预期答案”的实现；隐藏期望值/测试源码/详细失败日志不外发给模型。

## 4. 结果提交、审批与交付的原子边界

1. 事务外读取已授权来源、执行模型/SQL/沙箱；将原始输出、渲染报告、检查细节写CAS。任何模型费用先经原账本结算；unknown先停。
2. Engine短事务中重核Case revision、Run/input/epoch、当前ACL及精确Artifact/候选绑定。只使用原持锁连接；不能换连接绕锁。
3. 调查业务通过：将原commit_report拆成事务外的Artifact准备/校验段和原Engine内部SQL-only提交段，公开入口保持兼容。当前入口即使传stored_report_hash也会read CAS，不能直接在外层事务调用。未来同一短事务追加业务结果、执行SQL-only只读报告完成段、推进当前结果指针；任何一个失败都回滚SQL，Artifact可保留但不产生可交付索引。
4. 调查业务failed/needs_input/denied：存结果并按明确原因结束/等待，不创建结果发布Action。原commit_report的Verification字段只表达原机械校验，不能把它改造成整个业务结果的代用品；需要显式适配入口。
5. 修复提案必须先有同版本passed结果，再调用原候选/Action协议；Action payload绑定business_result_hash、spec_hash、candidate/tree/test hashes，用户审阅这些准确内容。该绑定改变payload即产生新提案/新审批。
6. 发送前和每个发布子步骤前重核当前结果、输入/来源/授权、原target_write_grant/fencing_token和审批期限。业务检查器不持发布工具，不自行写GitHub。
7. 已发送后出现新输入/取消/撤权，晚到回执只记录远端事实；不能恢复授权或把新版本显示为已交付。未知外部写只读对账，不重发。

外部Action状态不能与本地事务原子提交到GitHub；原Outbox+未知对账协议仍负责这个边界。不能宣称业务结果表实现了跨系统exactly-once。

## 5. 展示投影与可操作条件

投影契约为[view.schema.json](contracts/view.schema.json)，示例见[views.json](examples/views.json)，有序规则见[status-rules.json](status-rules.json)。投影携带as_of，来自当前授权查询快照，不作为新的状态权威。

无Case访问权的HTTP请求直接403，不返回完整投影；仍有Case权限但某来源撤权时可返回脱敏denied状态。原始Artifact在事务外读取，返回正文前再重核当前ACL；不能在业务事务内读文件，也不能仅凭旧PASS跳过权限检查。

主要状态优先级为：当前权限拒绝→未核清结果→旧版本/来源不适用→取消→执行失败→业务失败→缺资料→未评估/执行中→已验证报告/待修复/待审批/发布中/已交付。具体匹配字段和缺省行为见JSON规则。

原Run正在发布时优先显示“发布中”，暂停显示“已暂停”，审批过期单列；通用“执行中”不能遮住这些可操作状态。编辑补丁后input_revision变化，即使Case spec未变，旧业务结果也必须不适用。

所有按钮独立计算守卫，不能只判断主状态标签：

- request_repair：调查passed、当前版本有效、无活跃primary、无未知、独立修复许可匹配。
- approve：当前修复提案passed、Action准确payload相符、许可/来源/输入有效、期限未过。
- dispatch：approve全部条件加上原Outbox、当前主订阅、目标写权与fencing条件。
- show_artifact：当前actor仍有该来源权限；旧PASS不豁免撤权。

这些额外授权事实从服务器读取，不能由CaseView里的`business_status=passed`推定。默认不满足守卫；投影中的按钮是提示，所有写接口再次重查。

结果接口使用Cache-Control: no-store；SSE通知用于刷新视图，不使前端缓存成为授权依据。业务记录、API响应和页面都保留同一result_id/checker_version，便于追溯“执行成功但业务失败”的准确判定。

目标report只有当前调查passed、原只读Run已完成时显示“调查通过”。目标draft_pr只有当前修复提案passed、远端完整对象精确核对且原Action成功时显示“Draft PR已交付”。旧PR及历史失败保留在时间线；不自动关闭GitHub Issue，不自动合并PR，不声称上线缺陷消失。

## 6. 存储与兼容设计

拟新增`app.business_results`：result_id、case_id/case_revision、stage、run_id/input_revision/execution_epoch、spec_hash、result_hash→CAS、checker_version、created_at。主键result_id，唯一(case_id,case_revision,stage,run_id,input_revision,execution_epoch,spec_hash,report_or_candidate_hash,checker_version)，同键不同结果拒绝。

当前结果索引按(case_id,case_revision,stage)保存result_id，Engine条件更新；旧结果只追加不UPDATE/DELETE。当前索引与Case版本推进的事务约束避免晚到旧结果覆盖。源撤权/过期/新输入通过当前适用性计算阻断，不改写旧结果的原始时间点结论。

app.runs、app.verdicts、app.actions、旧Benchmark Oracle和已有输出文件保持兼容。原status=succeeded仍代表原执行成功；新业务结果API和工作台展示四维状态。旧消费者不被悄悄重定义，新增消费者必须使用P0投影。

## 7. 已知失败与新版本验收

| 原记录 | 新设计应验证 | 历史处理 |
|---|---|---|
| v6 C cross-05根输出契约错误 | 错根字段不能提交PASS；有界纠正失败后保留OUTPUT_CONTRACT_FAILED | 原FAIL不改 |
| v6 B repair-02/03漏paid过滤 | 公开/独立测试均为硬门槛，保留失败候选，不进入可发布状态 | 两项原FAIL不改 |
| v2 E r0/warm e01漏Observation前缀 | 新目录精确渲染完整行；旧坏输出作为拒绝样本，不能事后补前缀重评 | 原FAIL不改 |
| v1 D statement与quote不一致 | 非等值引用不能通过契约；精确单元选择避免自由复制错误 | 原FAIL不改 |

需要同时覆盖：同数字不同来源、Decimal边界、半截/过期SQL、撤权与ACL竞态、输入变更、旧epoch、报告PASS但PR未知、审批过期、编辑后复用旧审批、故障回滚、取消后晚回执，以及Run成功但业务失败的UI/API一致性。所有这些均为P0结果契约的验收，不扩展其他能力。

详见[验收计划](acceptance-plan.json)。本次仅完成设计一致性检查；没有修复/运行以上历史失败，也没有新增模型或GitHub业务结果。
