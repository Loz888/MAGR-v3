# ADR 0005：R4 混合检索、证据能力与专家图

R3 工程与限定 Router 验收已通过。R4 在单 Run 内增加有界专家协作，Router 不选择专家步骤。旧 r1/r2 图版本保留；新 r4 图使用固定输入版本、知识快照和最多三个专家，所有分支终结后合并，最多两轮补证据。单写者、epoch、审批和未知外部写对账保持不变。

知识入库先持久化原始 Artifact 和任务，再在业务事务外解析、嵌入；完成后以 CAS 检查 active snapshot 并原子发布新快照。失败不切换索引。旧 Run 固定快照，ACL 撤销仍立即影响旧引用和缓存。中文 BM25 与 pgvector 先过滤相同 project/repository/actor/ACL/snapshot，再 RRF 融合和学习模型重排。Redis 只保存可重建的检索引用，命中后重新校验权限。

本地真实向量使用官方 GitHub 发布的 zh_core_web_md 3.8.0：预训练中文词向量平均、300 维、L2 归一化和 cosine 距离，记录官方包 SHA256。它是轻量中文向量基线，不宣称 Transformer 效果。重排采用独立训练集学习并冻结的 pairwise linear ranking 模型，训练问题与冻结评测问题分离，记录数据及参数哈希。协议测试使用明确标记的 fixture 向量；真实语义验收单独运行，不把 fixture 召回率冒充模型效果。本地计算不产生新的付费 API 调用。

Data 工具沿用 V2 审计过的封闭 SELECT AST 思路，独立实现于 V3；不引用 V2 管理 DSN。合成数据位于独立 schema，Data 连接使用没有应用表权限的独立只读角色，并叠加只读事务、超时、行数、字节和函数白名单。数字引用必须绑定实际 SQL 结果的 typed cell。

Knowledge/Repo/Data 的工具能力由 Engine 针对 Run/epoch/task/role 签发，不能让模型自报身份。Knowledge 与 Data 提供独立 FastMCP HTTP 应用，跨进程验收使用真正 MCP 协议；直接调用路径明确记录 transport。专家不得持有发布工具。源文本、SQL 和外部事件均视为数据。

完整内容 Spill 到不可变 Artifact，Inline 保留引用、定位和短摘要；分页 token 绑定 actor、目标、哈希、偏移与有效期，每次读继续检查当前权限。ContextEdge 有界遍历且逐跳授权。Ticket 摘要仅在已结束、机械验证通过、用户对准确摘要哈希确认后进入记忆；引用来源撤销或记忆撤销后不能继续召回。

本会话三次付费调用额度已耗尽，R4 不新增付费调用或 GitHub 评论。工程、真实本地检索、MCP/SQL 与完整付费调查 live 分别记录；后者未运行就保持 NOT_RUN。R5/R6 不在本次范围。
