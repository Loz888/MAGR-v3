# ADR 0003：R1 的业务范围与快照

首版调查限定为对一个已授权来源进行直接引文式回答。独立 Verifier 核对读取账本、Run/Project 归属、原文哈希、定位和精确引文；不宣称机械检查证明语义根因。

单个 Run 的模型步骤至多一次物理调用，LangGraph 固定 read_source → investigate → verify。profile 的累计次数由配置限制；2026-11-02 候选改为最多 3 次、合计 USD 2，见 ADR 0004。没有 Router LLM、专家 DAG 生成或自由工具循环。

用户选定独立 V3 仓库为验收目标，创建 #1 作为源与结果目标。因仓库尚无 commit，首个 case 可读取 Issue 标题/正文，以 SHA-256 冻结来源；发布前重新读取并比较。文件来源则必须指定 40 位 commit。

Run 保存输入 revision。新增事件持久入 Mailbox，不能自动被视为已读或已处理；R1 阻止旧输入产生动作。显式 intake/replan 留待 R2。

PG checkpoint 和业务账本分别提交。恢复可创建新的 Run attempt 重放只读调查；既有 Action/审批/outbox 保持权威。缺失 checkpoint 不等于允许重发已产生的动作。取消不撤销已发出的远端请求，迟到回执只记录历史结果，不复活 Run。

若已有已知模型结果，则在同一 Run 以新 epoch 续跑 checkpoint，避免改变已绑定的证据身份。模型结果与费用结算原子保存；checkpoint 尚未记录该节点时，也可从调用账本复用结果。未知调用或已知但无有效结构化结果的失败均不能自动重发。
LangGraph 使用同步 checkpoint；业务提交前再次运行独立机械核验，checkpoint 中的旧 verdict 不充当当前业务真相。Run 的每次恢复和 epoch 变化追加到 run_events，不覆盖历史事件。

费用用整数微美元。每个授权 profile 的调用次数和费用由 PG 原子限制，预占覆盖完整配置上下文的价格上界；未知用量不释放预占和次数。Redis 原子限流不可用时，暂停新外部调用。
