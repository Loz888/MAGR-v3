# MGAR V3.1 · 持续研发 Agent

面向 GitHub 工单的事件驱动研发 Agent 原型，支持持续调查、跨知识库／SQL／代码取证、受限函数修复，以及经过人工审批的可恢复交付。

项目重点是让模型调用、任务状态、工具证据和外部写入形成可追溯的工程闭环。底层使用 DeepSeek；任务协调、授权、预算、判定与恢复由应用实现。

**[完整技术报告](v3/docs/TECHNICAL_REPORT.md) · [PDF下载](v3/docs/reports/20261106/MGAR-V3.1-Technical-Report.pdf) · [本地运行](v3/README.md) · [文档导航](v3/docs/README.md) · [P0产品设计](v3/docs/design/p0-commerce-v1/README.md)**

## 当前状态

截至北京时间2026-11-06：工程核心已实现，并完成各阶段限定验收；电商产品方向“研巡”的两项P0只完成设计，尚未实施。其他P1/P2改进按用户范围不开展。

| 内容 | 最新证据 | 适用范围 |
|---|---|---|
| 三臂真实模型评测 | A **30/30**、B **28/30**、C **29/30**，合计 **87/90** | 30个冻结任务×3配置，开发者可见的修复回归；原三项FAIL保留 |
| E/F协调复验 | 路由各 **36/36**；正常E **7/8**、F **8/8**；负例各12/12 | 固定合成负载，单候选准入；E一项Worker引用失败保留 |
| E/F最新工程验收 | **98项通过**，0失败/错误/跳过 | 真实PG/Redis/LangGraph，受控模型；与历史测试有重叠 |
| 部署与故障恢复 | 独立Linux/systemd虚拟机范围PASS | 整虚拟节点强杀、重启、PG/角色/CAS恢复；生产HA未验收 |
| 电商工单、统一业务判定 | **DESIGN_COMPLETE** | 设计契约和样例检查通过；实现/工程/真实验收未启动 |

单Agent在该三臂题集中表现最好，尚未建立多Agent优势。以上数字分别属于不同实验，不能合并成一个通过率。最近结案账本累计750次真实模型调用、峰值保守US$0.311513，未知/预占均为0；费用口径是上界核算，非供应商精确发票。

## 架构

![MGAR V3.1架构：Engine管理全局业务状态，LangGraph编排Run，所有外部写经审批与Outbox](v3/docs/assets/architecture.svg)

- **PostgreSQL权威状态**：Engine通过持久会话与advisory lock管理全局业务写入；Thread、Mandate、Ticket、Run、Assignment分别建模。
- **LangGraph任务编排**：单Run内执行调查、验证和补证据；Mailbox在安全点接入后续消息，输入版本和execution epoch拦截陈旧执行。
- **跨源证据**：知识快照、中文混合检索、只读SQL和固定GitHub源码；引用绑定实际读取、权限、内容哈希及定位。
- **可控交付**：受限Go函数补丁经AST检查、Docker沙箱和独立测试；完整提案按准确hash审批，经Action/Outbox/Dispatcher发布。
- **恢复与预算**：已结算有效模型结果可复用；未知调用/远端写保留账目并对账；Watchdog只提交受限恢复建议。

## 已实现的能力

| 能力 | 实现与边界 | 阅读入口 |
|---|---|---|
| 持续工单 | Web/CLI/GitHub入口、Mailbox、暂停/取消/恢复、版本化输入 | [R2设计](v3/docs/adr/0002-r2-mailbox.md) |
| 受限路由与多任务 | 已订阅消息直达，冷启动有界路由；模型提案由Engine重核 | [R3设计](v3/docs/adr/0003-r3-router.md) |
| 跨源调查 | 知识/代码专家、受限orders单表SQL、引用验证、已确认摘要 | [R4真实对照](v3/docs/r4-live-report.md) |
| 代码修复 | 一个获准Go纯函数体，隔离测试，完整补丁审批与Draft PR | [R5真实闭环](v3/docs/r5-live-report.md) |
| 巡检与恢复 | 心跳、依赖/积压观察、有限只读策略、未知结果停止 | [R6运行说明](v3/docs/r6-runbook.md) |

“研巡”拟将这些基础能力整合为电商收入异常工单闭环。当前[两项P0设计](v3/docs/design/p0-commerce-v1/README.md)规定调查/修复Run交接及执行、业务、审批、交付四维状态；新Case接口和业务结果表尚未实现。

## 技术栈

Python 3.12 · FastAPI / Pydantic · LangGraph / PostgreSQL Checkpoint · PostgreSQL 16 / pgvector · Redis · MCP · DeepSeek · Docker / Go · Linux / systemd

中文检索采用BM25、300维预训练词向量、RRF与成对线性重排。Python依赖由[uv.lock](v3/uv.lock)锁定，容器及MCP/检索模型固定版本或摘要；详见[技术选型与版本](v3/docs/TECHNICAL_REPORT.md#technology)。

## 快速启动

前提：Git、Make、uv、Python 3.12、可用的Docker Engine及Compose V2。Windows用户先完成[WSL2说明](v3/README.md#wsl2-首次启动)。首次运行会下载锁定依赖和容器镜像。

```bash
git clone https://github.com/Loz888/MAGR-v3.git
cd MAGR-v3/v3
uv python install 3.12
make setup
make deps-up
make migrate
make doctor
make dev
```

在确认上一步成功后执行下一步。打开`http://127.0.0.1:18080`；工作台令牌取自本机生成的`v3/.env`，不要公开。默认fixture模式不调用付费模型或发布GitHub业务结果；真实模式需要匹配的有效profile、预算与授权。

`/workspace/MAGR-v3`是开发云环境路径，本机应进入自己的克隆目录。停止依赖使用`make deps-stop`，保留数据卷。完整命令、演示与故障处理见[应用README](v3/README.md)。

## 结果、问题与复现

- [三臂最终结果与原三项失败](v3/docs/repairs/R6-20261103/README.md)：数值计算、输出契约、权限判定及受限修复的多轮失败与修复。
- [D/E/F首轮完整报告](v3/docs/experiments/coordination-v1/README.md)：D7/8、E1/8、F4/8；失败与级联输入问题原样保留。
- [E/F修复后报告](v3/docs/experiments/coordination-v2/README.md)：E7/8、F8/8；F相对E的Router调用32→12，减少62.5%，其中16次来自订阅直达、4次来自批处理。
- [E/F修复档案](v3/docs/repairs/COORD-20261106/README.md)：11项非法初始提案、事务重核、有界纠正、真实SIGKILL恢复与剩余Worker问题。
- [独立VM部署与恢复](v3/docs/repairs/R6-HOST-20261105/README.md)：实际环境、失败过程、恢复断言与范围。

本项目保留失败、NOT_RUN、原始分母、冻结manifest及来源哈希。公开库包含合成输入、输出、脱敏回执和索引；密钥、DSN、私有授权、原始账单/数据库备份不公开。详细方法、限制与经验见[完整技术报告](v3/docs/TECHNICAL_REPORT.md)。

## 仓库导览

```text
v3/src/mgar_v3/   Engine、图、工具、路由、修复、恢复及评测实现
v3/web/           现有本地任务工作台
v3/migrations/    17个显式产品迁移
v3/tests/         工程测试；真实PG/Redis，受控模型与外部远端
v3/eval/          冻结题集、独立判定与公开实验结果
v3/docs/          技术报告、ADR、实验、修复档案与P0设计
v3/deploy/        依赖容器、systemd与隔离VM验收资产
v31-upgrade/     增量任务卡与带时间范围的历史状态
```

历史阶段记录保留在[任务队列](v31-upgrade/TASK_QUEUE.md)与[旧版首页](https://github.com/Loz888/MAGR-v3/blob/2155556cb1c1288f08b5b3abebe55f2a7112253d/README.md)。当前项目介绍以本页和技术报告为入口，历史文件中的状态按其记录时间理解。
