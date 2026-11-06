# V3.1 增量任务队列

D/E/F协调补验已完成12/12次：正常D7/8、E1/8、F4/8，负例各12/12；本轮57次、保守US$0.006475，累计690次、US$0.305792，未知/预占0。执行与安全记录PASS，实测模型失败保留；原A/B/C87/90及三项FAIL不变。见 [完整实测报告](../v3/docs/experiments/coordination-v1/README.md)。

2026-11-05 最新验收：R6 在用户批准的独立 Linux/systemd 测试虚拟机范围内 DONE。V2 价格检查工程门禁、实际 systemd 部署、整虚拟节点断电/重启及备份恢复均 PASS。v6 真实模型成绩保持 87/90，A/B/C 为 30/28/29；C 正常 24/25、负例 5/5，质量门槛 PASS。累计 633 次、保守 US$0.299317，本次新增模型调用 0；原始失败全部保留。此结论不覆盖用户生产宿主、物理存储掉电或多节点 HA。 [三项门禁与完整修复过程](../v3/docs/repairs/R6-HOST-20261105/README.md)。 [完整档案](../v3/docs/repairs/R6-20261103/README.md)

原始 34 卡保持在 v3-upgrade/，不重写原 TODO 与历史。新卡映射表示采用对应契约，不表示原卡整体完成。

| 任务 | 内容 | 状态 | 工程门禁 | live 门禁 |
|---|---|---|---|---|
| V31-R0-01 | 独立仓库、V2 基线与架构边界 | DONE | PASS | NOT_APPLICABLE |
| V31-R0-02 | 锁定环境、PG/Redis、MCP 适配与验收框架 | DONE | PASS | NOT_APPLICABLE |
| V31-R0-03 | 事件、授权、工作项、执行与动作持久化契约 | DONE | PASS | NOT_APPLICABLE |
| V31-R1-01 | 确定性调查、Verifier、审批与统一出口工程闭环 | DONE | PASS | NOT_RUN |
| V31-R1-BUDGET-02 | 候选最多 3 次调用、累计预算 USD 2 | DONE | PASS | PASS |
| V31-R1-LIVE | V3 Issue #1 真实模型、审批与结果评论验收 | DONE | NOT_APPLICABLE | PASS |
| V31-R2 | Mailbox 与显式 intake/replan | DONE | PASS | NOT_RUN |
| V31-R3 | 受限 Router LLM 与多任务 | DONE | PASS | PASS |
| V31-R4 | RAG 与多专家 | DONE | PASS | PARTIAL |
| V31-R4-LIVE | 真实跨源调查与单 Agent 对照 | DONE | PASS | PARTIAL |
| V31-R5 | 受限修复与 Draft PR | DONE | PASS（249 项） | PASS（恢复闭环） |
| V31-R6 | Watchdog、主动性与发布评测 | DONE（独立 systemd VM 范围） | PASS（99项专项＋实际VM验收） | PASS（v6质量87/90） |
| V31-R6-HOST | V2价格工程检查、宿主部署与整节点恢复 | DONE（用户批准的测试VM） | PASS（V2全量＋7阶段VM断言） | NOT_APPLICABLE（新增模型调用0） |
| V31-R6-01 | 确定性观察与恢复请求首个切片 | DONE | PASS（82 项） | NOT_RUN |
| V31-R6-COORD | D/E/F全局协调对照补验 | DONE（12/12完整实测报告） | PASS（80项；另有受控PG补充） | EXECUTED（正常D7/E1/F4，各8项） |

2026-11-02 用户授权真实调用与 GitHub 推送；沿用最多 3 次物理调用、累计 USD 2 的限制。R0 与 R1 工程复验通过，R1 真实闭环 PASS 后才启动 R2。实际调用 2 次，保守计费 USD 0.001745，结果评论一条；模型落库与远程写回执两个 SIGKILL 恢复点通过。

公开验收摘要：v3/docs/acceptance/r1-live-20261102.json。原始失败与运行日志保留在本地 evidence/，不随公开源码发布。全项目仍为 ENGINEERING_INCOMPLETE。

R2 工程改造已通过：完整回归 112 项，任务验收 57 项，均无失败或跳过；四个真实 SIGKILL 故障点通过。R2 使用受控模型和远端，未新增真实付费调用；R1 账本仍为 2 次、保守 USD 0.001745。R2 摘要见 v3/docs/acceptance/r2-engineering-20261102.json。R3 后续状态见下；R4–R6 保持 TODO。

R3 工程与限定真实 Router 验收通过：完整回归 150 项、任务验收 89 项，均无失败或跳过；四个 Router SIGKILL 提交点恢复通过。两个事先冻结的合成场景只调用一次真实 Router，重启复用结果；累计三次调用、保守 USD 0.002456，未新增 GitHub 评论。公开摘要：v3/docs/acceptance/r3-20261102.json。该 live 门禁仅覆盖合成事件上的 Router，不等同生产业务评测。R4–R6 保持 TODO。


R4 工程门禁已通过：完整回归 187 项、注册验收 103 项，0 失败、0 跳过，新增四个真实 SIGKILL 恢复点通过。真实本地中文向量/学习重排与 PG 混合检索在十二个冻结改写问题上 Recall@5 为 11/12，漏召回样本保留；同一 Run 的真实本地检索、HTTP MCP、只读 SQL 组合也通过。专家与 Repo 的组合实验使用明确标记的合成资料协议实现，完整付费跨源业务 live、业务 Oracle 和单 Agent 对照仍为 NOT_RUN。本轮新增付费调用与 GitHub 评论均为零，账本仍为 3/3、保守 USD 0.002456。摘要见 v3/docs/acceptance/r4-20261102.json，使用说明见 v3/docs/r4-runbook.md。R5/R6 保持 TODO。


R4 真实实验已执行：三个正常和两个不足/越权案例各运行多专家/单 Agent。首轮双方各 4/5（总计 8/10），原始失败保留；修复专家问题范围后，失败的多专家案例在相同输入与 Oracle 下恢复通过，多专家有 3/3 正常及 2/2 负例通过记录。恢复不混入首轮对照成绩，不能宣称多专家胜出。用户追加授权新增最多 24 次、累计 USD 2；本轮实用 23 次、保守 USD 0.024569，累计 26 次、USD 0.027025，无未知调用、无新增业务评论。完整回归 196 项、R4 专项 119 项通过。live 仍标 PARTIAL，项目为 ENGINEERING_INCOMPLETE。详见 v3/docs/r4-live-report.md 和 v3/docs/acceptance/r4-live-20261102.json。R5/R6 保持 TODO。


R5 首轮受控工程通过：注册验收覆盖完整回归 229 项，0 失败、0 跳过，四个真实 SIGKILL 点与三类响应丢失验证通过。新增真实 Docker/Go 隔离测试、独立隐藏 Oracle、完整 diff 审批及编辑重测、Draft PR 三步 MCP 持久协议。本地完整演示已通过。补丁提供者和发布远端均为 fixture；真实 GitHub source/model/write profile 与适配器尚未完成，R5 整体为 IN_PROGRESS，live 为 NOT_RUN。未新增付费调用、真实业务评论或 PR，累计仍 26 次、保守 USD 0.027025。R6 保持 TODO。摘要：v3/docs/acceptance/r5-engineering-20261102.json；运行方式：v3/docs/r5-runbook.md。


R5 真实适配工程补齐：完整回归 245 项全部通过，新增官方 MCP/Git 元数据核对、真实模型适配及累计预算组、生产 Draft PR 适配和显式继续未发送子步骤。六个 SIGKILL 案例通过，历史 profile 哈希和旧快照兼容。真实源码只读探测通过；工程中的模型/发布协议仍为受控服务，未新增付费调用或业务 PR。真实模型与 PR 最终验收待冻结独立许可；R5 为 IN_PROGRESS，R6 不提前启动。摘要 v3/docs/acceptance/r5-native-engineering-20261102.json。


2026-11-03 R5 冻结真实实验消耗三次 DeepSeek 调用，原始 diff 格式/定位检查全部失败，保留 FAIL。补充只重算 hunk 行数的确定性规范化后，独立 Run 在同一 profile 下重放第一份已结算结果，公开测试及 28 个隐藏子案例通过，未延长原 Run 期限、未新增模型调用。累计 29 次、USD 0.028549，R5 本轮 USD 0.001524。最终 Draft PR 完整 payload 等待用户审批，真实发布及回执丢失恢复尚未执行。完整回归 248 项通过，0 失败、0 错误、0 跳过；lint、typecheck 和前端语法通过。R5 保持 IN_PROGRESS/PARTIAL，R6 仍为 TODO。见 v3/docs/acceptance/r5-live-20261103.json 和 ADR 0009。


用户批准完整提案后，真实 Draft PR #2 已创建并通过接收后 SIGKILL 的只读恢复。远端父提交、完整 tree、唯一变化文件均匹配完整获批 payload；Outbox 派发一次，未合并。原提案过期后以同一 payload_hash 的显式人工审阅记录续签，原 Action 不变；新增迁移 0012。最终工程回归发现两处旧迁移清单断言未更新，已保留失败并修正重跑。全部门禁通过后才开始 R6。详见 v3/docs/r5-live-report.md。


R5 最终门禁已通过：249 项回归、lint/typecheck/前端语法均通过；真实 Draft PR #2 的完整树和只读 SIGKILL 恢复通过。原始三次模型格式/定位失败保留 FAIL，后续不新增调用的规范化重放单列 PASS。累计仍 29 次、USD 0.028549，无未知或预留。R5 标 DONE，仅覆盖实测纯函数；R6 可开始工程，完整项目仍为 ENGINEERING_INCOMPLETE。


R5 全部门禁通过后已开始 R6。首个切片 V31-R6-01 注册验收 82 项通过，含 16 项新增真实 PG/独立进程/HTTP 检查；专用限权令牌、Engine 唯一写者恢复请求、事实重核、去重、显式人工决定、epoch 防护及事务回滚通过。追加迁移 0013，未新增模型调用或业务发布，累计仍 USD 0.028549。R6 主任务保持 IN_PROGRESS/PARTIAL，心跳、积压/依赖健康、主动策略、只读投机预算、完整 A/B/C 与部署故障矩阵仍待完成。见 v3/docs/r6-runbook.md。

2026-11-03 R6 后续工程已实现：独立心跳、队列/依赖健康、有限主动策略、只读影子 Run 和独立预算、最终 A/B/C 框架、独立 Oracle、真实 Docker/Go、新安装和备份恢复。完整工程基线 288 项通过；追加显式续跑 52 项及 I/O 故障 59 项通过（有重叠），最终源码复验见最新机器摘要。最终预算使用专用表，迁移至 0017，普通和旧试验许可不放宽。

用户批准新增最多 US$2、累计 US$4，并明确 DeepSeek completion 接口及合成/公开数据外发范围。实际采用更低的 270 次、US$1.658880、累计 US$2 硬限制。真实评测已有 24/90 项状态：10 PASS、13 FAIL、1 UNKNOWN；66 NOT_RUN。53 次有用量回执、两笔无回执预占 US$0.012288，触发停止及真实恢复请求，不重发、不退款、不伪结算。累计已结算保守费用 US$0.053146，含预占最大 US$0.065434。原始失败、OOM 中断、续跑和未知均保留。R6 仍 IN_PROGRESS，live PARTIAL，quality NOT_PASSED，不标 LIVE_READY，不进入 R7。

V2 代码/迁移/依赖/历史证据保持原样。Python 453 通过，Go 680 通过、同一七天官方价格证据过期检查及父测试失败，不能宣称本次 V2 全量通过。最新工程、交付和核账记录见 v3/docs/r6-delivery.md。

R6 本轮最终结果：工程完整基线 295 项通过、追加聚合核账/恢复专项 61 项通过（有重叠）；三臂 90 项全部执行，A 15/30、B 15/30、C 16/30，总 46 PASS、44 FAIL。C 正常 13/25、负例 3/5、引用有效率 98%，未达到冻结门槛。C 1/5 补丁过隐藏测试，但整体修复任务仍因字段契约失败，任务成功 0/5；不能混称两种指标。两笔未知使用用户提供的完整人民币账单聚合核账，记录费用上界 US$0.000572，逐请求 token 未编造、响应丢失仍 FAIL、没有重发。其余 66 项按原顺序续跑。本轮 195 次、US$0.087980，累计 224 次、US$0.116529，预占为零。工程实现 COMPLETE/PASS，完整评测 EXECUTED/FAIL，R6 总体验收仍 IN_PROGRESS，项目 ENGINEERING_INCOMPLETE，不进入 R7。

2026-11-05 R6修复档案与完整回归：原始46/90及44失败不改；中间候选35/90后关闭（23PASS/12FAIL/55NOT_RUN，68次）；final-v4完整90项74PASS/16FAIL，C24/30但正常19/25未达20，仍FAIL。全部历史468次、US$0.237912，无未知/预占。本次修复244/270。后续wire协议工程91项通过，真实模型复验待新冻结调用增量批准；不自动进入R7。
