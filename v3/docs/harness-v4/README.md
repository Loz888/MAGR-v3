# MAGR Harness 升级任务书与规划

状态：**PLANNED，仅设计，未开始功能实施**。审计日期：2026-11-10。应用基线：
`1d07285aa27ece9bae4ccbedc3a77ce2fbcbaf3a`。

本规划的目标是让用户选定项目、描述目标后，Agent 能在同一会话内调查、修改文件、
运行验证、根据失败继续修复，最终交付可检查的变更。第一版面向单机管理员，沿用
现有网页与 Python 服务；支持本机项目和 GitHub 工作区。多人托管、远程 Runner、
多 Agent 和桌面客户端分阶段推进。

- [逐项任务书与依赖](TASKS.md)
- [架构、里程碑与排期](ROADMAP.md)
- [验收与评测方案](ACCEPTANCE.md)
- [机器可读任务队列（全部 PLANNED）](plan.json)
- [资料版本与来源](sources.json)

## Harness 在本项目中的含义

Harness 是模型外部的执行系统：模型接口、项目工作区、工具、权限、会话与上下文、
执行循环、验证、恢复、交互和扩展机制共同工作。用户描述的是任务目标，系统负责
组织实际操作，并用文件变更、命令退出码和测试回执说明完成情况。

“能循环调用模型”“有流式聊天”“第二次调用模型检查回答”已是有用基础；通用
编码任务还需要实际编辑、执行与验证的闭环。模型自身的推理能力与 Harness 的
可靠性分别评估；接入另一个模型不能自动补齐工作区和执行能力。

## 当前已经具备什么

| 层面 | 已有实现 | 本次审计结论 |
| --- | --- | --- |
| 网页体验 | 账户登录、网页配置凭据与许可、历史追问、Enter/Shift+Enter | 已接入首页，继续复用 |
| 只读分析 | 固定 GitHub Commit、搜索/阅读/Issue/PR、证据引用 | 已接入首页，属于真实只读链路 |
| 自动推进 | Token/费用/时间预算、重复读取复用、无进展防护、回答核验反馈 | 已实现于仓库分析；不能据此推定旧修复器也已升级 |
| 流式与恢复 | 供应商 SSE、报告草稿、持久事件游标、刷新恢复、checkpoint/epoch | 已有；目前公开草稿主要是 conclusion/structure，不是通用终端/补丁事件协议 |
| 业务基础 | Engine、PostgreSQL、预算预占/结算、CAS、Action/Outbox、哈希审批 | 有明确可复用价值，保持权威边界 |
| 编码组件 | 固定版本快照、受限多文件编辑器、Docker 沙箱、Go/Python 适配、修复反馈、Review Bundle | 已存在于其他链路，覆盖范围有限，尚未成为首页通用编码能力 |
| 上下文与记忆 | 确定性压缩、引用/失败/权限不变量、工作记忆和专家模块 | 已有实现；后台压缩标记为未实现，完整编码闭环效果尚未验收 |
| 验证基础 | 注册验收、故障注入、评测 Harness、分开记录工程/live 门 | 已有基础；历史真实修复失败和未执行项必须保留 |

最近一轮会话中的 `AGENT-UI` 为 61 项通过、`V35-R05` 为 27 项通过；使用真实
PostgreSQL 与受控模型响应。这些是当时源码的工程回归记录，本次规划未重新执行，
也不代表真实通用编码能力通过。V3.5 E02 记录的真实模型/Agent 门仍为 `FAIL`，
真实 GitHub 写门为 `NOT_RUN`；更早 R5/P0 的受限真实 PR 交付只覆盖各自的实验范围。

## 与三个参照产品相比，差距在哪里

参照版本固定在 [sources.json](sources.json)。对 Codex、Claude Code 和 DeepSeek
Harness 的描述来自官方公开资料；本次没有安装运行或进行同题性能对照，不给成功率
排名，也不推断闭源产品的内部实现。

- **Codex**：[官方说明](https://github.com/openai/codex/blob/77fde36ae112437a8ce23e752209099ee04a0d50/README.md)
  覆盖本机 CLI、IDE 和 Web；[公开工具实现](https://github.com/openai/codex/blob/77fde36ae112437a8ce23e752209099ee04a0d50/codex-rs/core/src/tools/handlers/mod.rs) 包含补丁编辑、
  unified exec、计划和交互等能力，另有 AGENTS.md、Skills 与安全机制说明。
- **Claude Code**：[官方说明](https://github.com/anthropics/claude-code/blob/ce6cb8ee39759afb5d8bf2e3fd26582f575e9095/README.md)
  以终端中的项目编码和 Git 工作流为核心，同时提供 IDE
  与 GitHub 使用方式；公开插件说明与更新记录涉及工具、权限、会话恢复和上下文。
- **DeepSeek Harness**：指 DeepSeek 官方的
  [deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness)。它以
  Cordis 实现[可组合插件](https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/docs/architecture.md)，模型、工具注册、会话日志与 Agent Loop 均可配置替换，
  提供 Web、Desktop、headless、SDK、ACP 等入口。官方仍标注 developer preview，
  因此其架构可作为参照，稳定性需要按具体版本实测。

| 能力 | MAGR 当前实际差距 | 优先级 / 任务 |
| --- | --- | --- |
| 同一会话完成编码 | 首页走只读分析；修复、测试和审批属于另一套入口与编排 | P0：H01、H07、H08 |
| 操作用户项目 | 有 GitHub 快照与临时工作树；首页缺少本机目录、脏工作区、工作区归属和冲突处理 | P0：H03 |
| 可靠增量编辑 | 旧修复协议要求完整替换文件；大文件的小修改也可能耗尽输出窗口 | P0：H05 |
| 实际执行与环境 | 有固定沙箱测试；缺少首页可调用的通用命令、依赖准备、服务启动、持续日志及进程控制 | P0：H04、H06 |
| 通用任务验收 | 当前分析核验是引用检查加模型评审；旧修复器要求基线先失败，不能直接覆盖新增功能/正常重构 | P0：H07 |
| 自动编码循环 | 分析已按资源推进，旧 RepairBudget 仍默认 3 次尝试、10 次测试 | P0：H07 |
| 可替换模型 | 分析客户端硬编码 DeepSeek 请求与结构化 action/report；未形成统一工具调用与能力协商接口 | P0：H02 |
| 会话中途协作 | 首页执行时禁用输入；缺少持久收件箱、安全点接收新指示、缺信息时的 ask_user | P0/P1：H09 |
| 完整执行流 | 已有报告 SSE；需要增量补丁、终端 stdout/stderr、权限请求、测试和产物事件 | P0/P1：H06、H09 |
| 长任务上下文 | 有压缩组件；需要把计划、验收缺口、工作树版本、命令状态及分页原文串进编码循环 | P1：H10 |
| 崩溃后的工作恢复 | 有 checkpoint/epoch；完整工作区、子进程、未结算调用和事件的一致恢复尚未验收 | P1：H11 |
| Skills / MCP / 插件生态 | 有固定 MCP 与专家组件；缺少通用注册、版本/生命周期、项目指令和能力收窄契约 | P1：H14 |
| SDK / 自动化 / 远程运行 | CLI 目前主要调用业务 API；缺少统一 headless/SDK 协议和远程 Runner 配对 | P1/P2：H13、H15 |
| 多用户和规模化 | 当前是单实例管理员、Engine 全局单写者；不能直接作为多租户服务 | P2：H16、H18 |
| 能力证据 | 工程回归较丰富；通用真实修复、持出集和真实模型对照证据不足 | P0/P1：H00、H12 |

## 两个已经有证据的阻塞点

1. [V35-E02](../v35/cards/V35-E02.md) 记录：修改 142KB 的 `src/click/core.py`
   时，完整文件输出协议与 8192 输出 Token 上限冲突，四臂全部 `PATCH_TRUNCATED`。
   增加累计 Token 预算不能解决单次输出窗口不足，H05 必须先完成。
2. [RepairAgent](../../src/mgar_v3/repair/repair_agent.py) 在基线不是 `test_failed`
   时返回 `baseline_not_reproduced`。这适用于部分 Bug 复现，新增功能和保持行为的
   重构需要各自的验收契约，不能直接复用这条前置条件。

此外，`WorkbenchService._prepare_repair` 对非 fixture 仍返回
`LIVE_REPAIR_EXECUTION_NOT_ENABLED`。H08 的目标是接入经过授权、可验证的真实编码
运行时，不能仅删除 403 分支。

## 推荐升级方向

保留 Python/FastAPI、Engine/PostgreSQL、CAS、现有预算与发布协议，新增薄的 Harness
接口层，将模型、工具、执行环境和事件解耦。首页与 CLI 使用同一运行时；旧 API
通过适配保持兼容。

H01 先进行有限的 DeepSeek Harness 复用验证：考察其 SDK/插件是否能把每次模型
请求和工具操作送入现有授权、预算与回执入口。满足契约时可以采用它作为执行后端；
达不到时，以现有 Python 实现最小闭环。资料中“可插件化”不等于本项目已经完成
集成，正式选型以验证产物为准。

第一阶段建议交付：**单机、单管理员、Python 与 JS/TS 项目、读/改/执行/验证闭环、
实时日志与 Diff、暂停恢复、当前预算内自动推进、最终可检查的变更产物**。Go 复用
现有适配器做兼容验证。真实 PR 发布继续走原有独立写授权与 Review Bundle。

首批实施顺序：H00 → H01 → H02/H03 → H04 → H05/H06 → H07 → H08。
可演示闭环完成后，通过 H09–H12 的交互、长任务、故障与真实评测门再发布 MVP。
多 Agent、应用商店和桌面打包放在单 Agent 编码质量稳定之后，详见 [排期](ROADMAP.md)。

## 代码审计入口

- 首页运行与追问：[repository_analysis/service.py](../../src/mgar_v3/repository_analysis/service.py)、
  [web/agent.js](../../web/agent.js)
- 只读工具契约：[repository_analysis/domain.py](../../src/mgar_v3/repository_analysis/domain.py)
- 报告核验与防循环：[repository_analysis/agent.py](../../src/mgar_v3/repository_analysis/agent.py)
- 模型与报告流：[model.py](../../src/mgar_v3/repository_analysis/model.py)、
  [streaming.py](../../src/mgar_v3/repository_analysis/streaming.py)
- 快照与编辑：[repository_analysis/workspace.py](../../src/mgar_v3/repository_analysis/workspace.py)、
  [repair/worktree.py](../../src/mgar_v3/repair/worktree.py)
- 修复与沙箱：[repair/repair_agent.py](../../src/mgar_v3/repair/repair_agent.py)、
  [repair/providers/structured.py](../../src/mgar_v3/repair/providers/structured.py)、
  [repair/sandbox_executor.py](../../src/mgar_v3/repair/sandbox_executor.py)
- 接线与业务权威：[workbench/service.py](../../src/mgar_v3/workbench/service.py)、
  [workbench/orchestrator.py](../../src/mgar_v3/workbench/orchestrator.py)、
  [coordination/engine.py](../../src/mgar_v3/coordination/engine.py)
- 现有边界与历史验收：[架构契约](../v35/ARCHITECTURE_CONTRACT.md)、
  [评测索引](../v35/EVALUATION_INDEX.md)
