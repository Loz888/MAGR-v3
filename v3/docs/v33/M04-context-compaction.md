# M04 — Context Builder & Compression

- 状态: 工程门 PASS（10 tests + 13 regression tests）
- 依赖: M03（Relevant Memories 可直接消费 `MemoryHit`）
- 迁移: 无
- 代码: `v3/src/mgar_v3/context/builder.py`、`v3/src/mgar_v3/context/compactor.py`
- 测试: `v3/tests/test_v33_context_compaction.py`
- 接入: `v3/src/mgar_v3/repository_analysis/agent.py`

## 1. 目标与边界

M04 替换仓库分析循环中"按长度从最旧开始删除历史"的裁剪策略。原策略在
`_payload()` 中只要序列化后的 history 超限就执行 `history = history[1:]`，
会静默丢掉当前权限、失败尝试、待审批动作、未解决问题和 Evidence 引用。

新策略在每次模型调用前构造 `ContextBundle`：

1. 固定段永远存在，不因预算压力被删除；
2. 近期执行窗口保留最近 N 条观察的完整内容；
3. 更早的观察先去重，再把长输出替换为分段引用，最后折叠为结构化摘要；
4. 重要 Evidence、失败尝试、待审批动作和未解决问题由摘要显式携带；
5. 压缩前后执行一致性校验，任何权限扩大或安全事实丢失都会失败关闭；
6. Artifact 只读且内容寻址，分页支持向前与向后回溯，原始字节不变。

明确不做：

- 不新增数据库表或迁移；
- 不改变模型、工具或 Artifact 的既有语义；
- 不做跨运行持久化摘要，压缩结果只作用于当前模型调用；
- 不在此卡接入实时 Memory 检索（M03 已提供接口，接线留给后续卡）。

## 2. ContextBundle 固定段

`ContextBundle`（`v3/src/mgar_v3/context/builder.py`）包含：

| 段 | 字段 | 是否可压缩 |
| --- | --- | --- |
| System Instructions | `system_instructions` | 否 |
| Task & User Constraints | `task`、`constraints` | 否 |
| Repository | `repository` | 否 |
| Commit | `commit` | 否 |
| Current Plan | `current_plan` | 是（尾部截断） |
| Recent Observations | `recent_observations` | 是（去重/分段/摘要） |
| Verified Evidence | `verified_evidence` | 否（ID 固定保留） |
| Relevant Memories | `relevant_memories` | 是（按预算截断） |
| Failed Attempts | `failed_attempts` | 否 |
| Open Questions | `open_questions` | 否 |
| Pending Approvals | `pending_approvals` | 否 |
| Available Tools | `available_tools` | 否 |
| Remaining Budget | `remaining_budget` | 否 |

另附 `permissions`（当前权限快照）、`compaction`（压缩摘要）与 `budget`
（预算分配结果）。`to_payload()` 输出模型侧段；System Instructions 仍由
system message 传递，不在 user payload 中重复。

`ContextRequest.permissions.repository/commit` 必须与 `repository/commit`
一致，否则抛出 `CONTEXT_SCOPE_MISMATCH`（403），避免把 A 仓库的权限用于 B 仓库。

## 3. Token 预算分配

`ContextBuilder` 按以下顺序分配：

1. 估算固定段 token（含 System Instructions）；
2. 固定段已超过预算：`CONTEXT_BUDGET_EXCEEDED`（413），绝不通过删除约束来"适配"；
3. 剩余预算按 `plan 10%`、`memories 15%`、`observations 75%` 分配；
4. 组装后再整体估算一次；若仍超限，按溢出量下调 observation/plan/memory 预算并重建，最多 3 次；
5. 成功时返回 `estimated_tokens`，保证 `estimated_tokens <= max_tokens`。

`ContextBudgetAllocation` 记录 `max_tokens`、`pinned_tokens`、`plan_tokens`、
`observation_tokens`、`memory_tokens`，便于验收检查预算是否被正确分配。

## 4. 历史观察压缩

`compact_observations()`（`v3/src/mgar_v3/context/compactor.py`）流程：

1. **全局去重**：以 `tool + reference + content + error_code` 的 SHA-256 为身份；
   保留最后一次出现，其余计入 `observations_deduplicated`，其 `evidence_id`
   仍进入摘要的 `evidence_ids`；
2. **近期窗口**：去重后最后 `recent_window` 条保持完整内容；
3. **长文件分段引用**：超出 400 字符的观察替换为"预览 + 分段引用"：
   `SegmentRef` 记录 `reference`、`locator`（行号范围）、`artifact_hash`、
   `total_chars`，并设置 `truncated=True`；重复压缩保留最初的总字符数；
4. **预算收缩**：先压缩近期窗口（预览阶梯 900/500/240/120 字符），
   再从最旧开始丢弃已压缩观察，最后才丢弃较旧近期项并至少保留最新一条
   （可退化为仅保留引用头部）；
5. **结构化摘要**：`CompactionSummary` 记录总数、保留数、去重数、压缩数、省略数，
   并显式携带 `failed_attempts`、`pending_approvals`、`open_questions`、
   `evidence_ids`、`artifact_hashes`、`permission_digest`。

失败尝试识别：优先使用显式 `error_code`；历史数据没有该字段时，
回退识别 `tool_error` 和 `CODE_WITH_UNDERSCORE:` 形式的错误码。

## 5. 压缩前后一致性校验

`ContextInvariants` 是安全投影：

- `permissions`：权限快照；
- `available_tools`：工具集合；
- `failed_attempts`、`pending_approvals`、`open_questions`；
- `evidence_ids`；
- `artifact_hashes`。

`diff_invariants(before, after)` 检查：

| 检查 | 违规标记 |
| --- | --- |
| 权限作用域不变 | `permission_scope_changed:<field>` |
| 只读不可变为可写 | `permission_escalation:read_only` |
| 不得新增工具 | `permission_escalation:tool:<name>`、`tool_escalation:<name>` |
| 预算不得增加或新增 | `permission_escalation:budget:<name>` |
| 失败尝试不得丢失 | `missing_failed_attempt:<value>` |
| 待审批动作不得丢失 | `missing_pending_approval:<value>` |
| 未解决问题不得丢失 | `missing_open_question:<value>` |
| Evidence ID 不得丢失 | `missing_evidence:<id>` |
| Artifact 引用不得丢失或改哈希 | `missing_artifact:<hash>`、`artifact_hash_changed:<hash>` |

`validate_compaction()` 在任一违规时抛出 `CONTEXT_INVARIANT_VIOLATED`（409）。
`ContextBuilder.build()` 每次都用"请求原始状态"与"压缩后 bundle"做校验，
因此压缩不可能引入权限升级或抹掉安全事实。

## 6. Artifact 分页回溯

`ArtifactPaginator` 基于 `ArtifactStore`（内容寻址、只读）：

- 页大小 128–8192 字节，默认 1024；
- 游标为 base64url 的 `{hash, offset}`，携带内容哈希；
  哈希不匹配或非法游标返回 `CONTEXT_CURSOR_INVALID`（403）；
- 每次读取都重新校验 SHA-256，字节被篡改时返回 `ARTIFACT_CORRUPTED`；
- 页面返回 `continuation`（向后）与 `previous`（回溯）游标，
  最后一页 `continuation=None`，首屏 `previous=None`；
- UTF-8 多字节边界自动回退，避免把字符截断。

`verify_artifacts()` 对 bundle 引用的全部 Artifact 哈希重新读取校验，
保证"压缩前后原始 Artifact 不变"。

## 7. Agent 接入

`RepoAnalysisAgent._payload()` 不再按字符数删除历史，而是：

1. 把 `state["history"]` 映射为 `Observation`，Evidence 有值的观察生成 `EvidenceRef`；
2. 用 `spec.limits` 计算 `RemainingBudget` 与剩余输入 token；
3. 构造 `ContextRequest`，调用 `ContextBuilder(max_tokens=remaining, recent_window=6)`；
4. 将 bundle 的 `recent_observations` 作为 `history` 键输出（兼容既有模型协议），
   其余段原样输出；
5. builder 抛出的 `CONTEXT_BUDGET_EXCEEDED` / `CONTEXT_BOUND`
   统一转为 `INPUT_TOKEN_BUDGET` 停止原因，与原行为一致。

工具错误观察新增 `error_code`，`read_file` 观察新增可选 `locator`，
使失败识别与长文件分段引用不依赖文本猜测。

## 8. 测试与验收

验收命令：

```bash
uv run --frozen pytest tests/test_v33_context_compaction.py -q
```

回归命令：

```bash
uv run --frozen pytest tests/test_v32_repository_analysis.py -q
```

测试覆盖：

1. 固定段齐全、预算分配可行、近期窗口保留最新观察；
2. 60 条长观察（每条约 6000 字符）在 3000 token 限制内继续，最新观察仍可定位；
3. 约束、失败尝试、待审批、未解决问题与全部 Evidence ID 压缩后不丢失；
4. 重复观察合并，5 个不同 Evidence ID 全部保留；
5. Artifact 分页可正反向回溯、拼接等于原文、篡改游标与跨源游标被拒绝；
6. 长观察变为分段引用且保留原始总字符数与行号；
7. 压缩不得扩大工具、预算或只读权限；
8. 丢失失败尝试/问题/Evidence/Artifact 时一致性校验失败关闭；
9. Artifact 字节被篡改时校验失败；
10. `RepoAnalysisAgent._payload()` 输出压缩后的 history，且 Evidence 全部保留在摘要中。

结果：`10 passed`；回归 `13 passed`；Ruff 与 mypy strict（新增/修改文件）通过。
