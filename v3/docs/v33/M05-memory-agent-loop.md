# M05 — Memory-aware Agent Loop

- 状态: 工程门 PASS（6 个 M05 测试 + 35 个回归测试）
- 依赖: A04、M04（Context 压缩）、M03（Hybrid Memory Retrieval）、M02（候选与确认）
- 迁移: 无（复用 0021 的 `knowledge.memory_*`）
- 代码: `v3/src/mgar_v3/repository_analysis/memory_loop.py`
- 接入: `v3/src/mgar_v3/repository_analysis/agent.py`、
  `v3/src/mgar_v3/repository_analysis/service.py`、
  `v3/src/mgar_v3/context/builder.py`（`MemoryNote.downweighted`）
- 测试: `v3/tests/test_v33_memory_agent.py`

## 1. 目标与边界

M05 让已经落库的记忆真正参与模型执行，而不是只在数据库里可检索。核心约束是
**记忆只提供参考内容**：工具清单、允许仓库、Commit、预算、用户审批和沙箱策略
全部来自不可变的 `AnalysisSpec` 与 `PermissionSnapshot`，任何召回内容都不能改变它们。

明确不做：

- 不新增数据库表或迁移；
- 不改变 M02 的候选确认规则，也不绕过 `MemoryWriter` 的校验；
- 不把记忆正文当作指令执行，也不把记忆中的工具/预算字段合并进运行时；
- 不让 Working Memory 进入跨任务召回（它只服务当前运行）。

## 2. 七个 Memory 使用节点

| # | 节点 | 实现位置 | 行为 |
| --- | --- | --- | --- |
| 1 | 任务开始前召回相关仓库知识 | `AnalysisMemory.recall_task()` | 文本检索 + 结构化仓库/Commit 知识合并，权限前置过滤 |
| 2 | 规划调查路线时参考历史经验 | `RepoAnalysisAgent._payload()` → `prior_experience` | 只把非降权、置信度 ≥ 0.5 的记忆转成 `[reference-only]` 提示 |
| 3 | 工具调用后更新 Working Memory | `AnalysisMemory.record_working()` | 每轮工具调用后按 `run:<run_id>` 生成/替代 working 记录 |
| 4 | Context 过长时触发 Compaction | M04 `ContextBuilder` | 每次模型调用前自动压缩；记忆段与 Evidence 段保持固定 |
| 5 | 遇到已知失败时检索历史尝试 | `AnalysisMemory.recall_failures()` | 按 `error_code + tool + failure` 检索同 Commit 的历史失败 |
| 6 | 验证完成后产生 Memory Candidate | `AnalysisMemory.after_verification()` | 由 `execution_candidate(investigation_completed, success/failure)` 生成并走 M02 校验 |
| 7 | 后续追问复用确认事实与准确 Commit | `recall_task(follow_up=True)` → `confirmed_facts` | 只采纳 `version_status=current` 且带 Evidence 的记忆，并显式携带 `@ commit` |

## 3. 召回与过滤

`recall_task()` 合并两路结果：

1. `MemoryRetriever.search_memory(query=task, commit=spec.commit)`：BM25/Dense/RRF
   的相关性检索；
2. `MemoryRetriever.get_repository_knowledge(commit)`：结构化返回同仓库、同 Commit
   的知识，保证追问或改写措辞时仍能召回。

合并后按 `memory_id` 去重（同分保留先到者），再执行以下规则：

| 条件 | 处理 | 原因码 |
| --- | --- | --- |
| `verification_status = disputed` | 排除 | `DISPUTED_EVIDENCE` |
| `verification_status = unverified` | 排除 | `UNVERIFIED` |
| 正文或 subject 命中提示注入模式 | 排除 | `PROMPT_INJECTION` |
| 正文或 subject 声称可修改工具/仓库/预算/审批/沙箱 | 排除 | `AUTHORITY_CLAIM` |
| `version_status = historical/stale` 或 `requires_reverification` | 保留但降权 | — |
| `memory_kind = working` | 不进入跨任务召回 | — |

降权记忆仍会以 `downweighted=true`、`requires_reverification=true` 出现在
`relevant_memories` 中，但不会生成 `prior_experience` 提示；排序时排在当前记忆之后。
这样"过期但可能有用"的信息可见，而不会被当作当前事实直接使用。

`ContextBuilder` 的 `MemoryNote` 增加 `downweighted` 字段，保证模型侧能区分
"当前经验"与"仅作参考的旧经验"。

## 4. Working Memory

`record_working()` 在每个工具节点结束后执行：

1. 取本轮新产生的观察（最多 20 条），记录 tool、reference、evidence_id、error_code；
2. 将摘要写入内容寻址 Artifact，并在 `app.artifacts` 登记；
3. 以 `scope=commit`、`subject_key=run:<run_id>` 创建 working 记忆；
4. 若同一 run 已存在 working 记录，则用 `supersede` 替代，保证同一运行只有一条当前工作记忆；
5. 过期时间默认 2 小时，过期后不参与 `current()` 查询。

Working Memory 写入失败不会中断分析：`DomainError` 被捕获，运行继续，避免辅助记忆
影响主流程。

## 5. 验证完成后的 Memory Candidate

`RepoAnalysisAgent.final` 在 `finish_success` 之后调用 `after_verification()`：

- Trigger: `investigation_completed`；
- Outcome: 验证通过 `success`，验证失败 `failure`；
- Subject: `investigation:<task-slug>:<commit[:12]>`；
- Summary: `task / outcome / commit / conclusion`，写入 Artifact 并作为 `summary_hash`；
- Evidence: 当前运行的全部 Evidence ID；
- Artifact: 报告哈希 + Evidence 内容哈希；
- Provenance: 有 Evidence 时 `mechanical`，否则 `agent_reported`；
- Verification: 有 Evidence 时 `verified`，否则 `supported`。

候选随后调用 `MemoryWriter.validate()`：

- 校验不通过 → 返回 `rejected` 与原因码，不写库；
- 需要外部确认 → 返回 `requires_confirmation`，不写库；
- 可自动确认 → `MemoryWriter.write()` 写入并返回 `confirmed`。

验证失败的运行也会产生 `failure` 候选，用于后续"已知失败"检索；但只有通过
Evidence 校验的内容才会以 `verified` 状态入库。

## 6. Memory Poisoning 防护

防护分为四层：

1. **输入过滤**：`AUTHORITY_CLAIM` 与 `PROMPT_INJECTION` 在进入模型前排除；
2. **结构隔离**：`memory_policy` 明确声明记忆不能修改工具、仓库、Commit、预算、
   审批和沙箱；
3. **权限不变量**：`ContextBuilder` 的 `PermissionSnapshot` 由 `AnalysisSpec` 与
   `TOOL_NAMES` 构造，记忆只进入 `ContextRequest.memories`，没有代码路径把记忆字段
   合并进 `allowed_tools`、`budgets`、`repository` 或 `commit`；
4. **提交侧校验**：M02 的 `MemoryWriter` 继续检查秘密、提示注入、推断内容和争议内容，
   并执行语义/程序性/高风险确认门。

`tests/test_v33_memory_agent.py` 构造了同时声称"增加工具、放开仓库、取消审批、
关闭沙箱"的投毒记忆与提示注入记忆，断言二者被排除，且模型看到的工具清单、
预算、仓库和 Commit 与 `AnalysisSpec` 完全一致。

## 7. 追问与 Commit 精确性

追问运行复用 `parent_run_id` 与既有 `prior_report`。`recall_task()` 在
`spec.parent_run_id` 非空时额外生成 `confirmed_facts`：

- 只采纳 `version_status = current` 的记忆；
- 必须带 Evidence；
- 文本显式携带 `@ <commit>`；
- 最多 3 条，避免挤占 Context 预算。

其他 Commit 的记忆仍可被检索，但会被标记为 `historical` 并要求重新验证。

## 8. 仓库作用域

`MemoryStore` 的读写作用域绑定 `Settings.repository`。分析对象不一定是配置中的
默认仓库，因此 `AnalysisMemory` 为每个 `spec.repository` 构造一个视图：

- 复用原 `Settings` 的 project、actor、数据库与 Artifact 配置；
- 仅把 `repository` 替换为当前分析仓库；
- 缓存视图，避免每次工具调用重建 Store/Retriever/Writer。

这样记忆不会跨仓库串用，也不需要修改 M01 的存储契约。

## 9. 测试与验收

`v3/tests/test_v33_memory_agent.py`（6 个测试，真实 PostgreSQL，fixture 模型无网络）：

1. 第二个任务实际召回并复用第一个任务的已验证经验；
2. 追问复用 `current` 确认事实且携带准确 Commit；
3. 投毒/注入记忆被排除，工具、仓库、预算、审批与沙箱策略不变；
4. 未验证记忆被过滤，过期/历史记忆明确降权且不进入规划提示；
5. 工具失败后检索到历史失败尝试并注入 `known_failures`；
6. 工具调用后 Working Memory 被创建并引用 Evidence。

验收命令：

```bash
uv run --frozen pytest tests/test_v33_memory_agent.py -q
uv run --frozen python scripts/verify.py --task M05
```

## 10. 偏差说明

- **Compaction 不在本卡重复实现**：M05 直接复用 M04 的 `ContextBuilder`，每次模型调用
  前已自动压缩；M05 只保证记忆段在压缩后仍存在。
- **Working Memory 不参与跨任务召回**：避免把未验证的运行中笔记当作仓库知识。
- **历史失败候选也入库**：验证失败的运行会产生 `failure` 候选，但只有带 Evidence
  的候选才标记为 `verified`，符合"错误记忆被明确降权"的要求。
- **全仓 Ruff/mypy 存在与本卡无关的历史告警**：按既有卡片惯例，只对本卡新增/修改文件
  执行 Ruff，对新增 `memory_loop.py` 与 `context/builder.py` 执行 mypy strict。
