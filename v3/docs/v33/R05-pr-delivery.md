# R05 — Generic Draft PR Publication

- 状态: 工程门 PASS（11 tests）
- 依赖: R04（迭代修复与独立验证）、R02（Worktree 与补丁基线）、R03（固定沙箱）
- 迁移: 无
- 代码: `v3/src/mgar_v3/repair/pr_delivery.py`
- 测试: `v3/tests/test_v34_pr_delivery.py`

## 1. 目标与边界

R05 把一份已通过双阶段验证的 Repair Proposal 变成可审查、可批准的发布对象：

1. 在任何写入之前审查完整 diff 与全部变更文件；
2. 审批绑定 repository、base commit、tested tree 与 payload hash；
3. diff 或任一输入版本变化后旧审批立即失效；
4. GitHub 读令牌与写令牌分离，只读传输没有写方法；
5. 无写权限时不尝试写入，导出完整 patch 作为回退；
6. 首次远端写入前再次校验 base 分支仍指向审批的 commit；
7. branch → commit → Draft PR 分三步写入并分别记录回执；
8. 写后结果未知时只做只读对账，不重放任何写；
9. 只创建 Draft PR，本模块不存在合并操作；
10. 成功与失败原因都写入 Episodic Memory。

明确不做：

- 不修改旧 R5 `repair/publication.py` 与 Engine/Outbox 路径，R05 是独立的通用发布层；
- 不实现 GitHub 合并、评论、标签、强制推送或文件删除；
- 不把 fixture 工程验证包装成真实 GitHub 写权限验收；真实写入需要单独的
  R5 write profile 与人工授权；
- 不支持二进制文件、删除文件或超过 16 个文件的发布批次。

## 2. 数据流

| 对象 | 作用 |
| --- | --- |
| `PublicationFile` | 仓库相对路径 + 文本内容；拒绝绝对路径、`..`、隐藏段、反斜杠与冒号 |
| `ReviewBundle` | 人工审查的完整对象：仓库、base 分支与 commit、tested tree、proposal 编号、分支、标题/正文、完整 diff、文件列表与 payload hash |
| `ReviewReceipt` | 审查回执：diff SHA-256、文件列表、字节数、增删行数，`complete=True` |
| `PublicationApproval` | 内容寻址审批：actor、仓库、base、tree、payload、diff 摘要与批准时间共同决定 `approval_id` |
| `PublicationStep` | 三步写入的逐条回执：`branch`、`commit`、`pull_request`，状态为 `succeeded` 或 `already_present` |
| `DraftPullRequest` | 读回并校验过的 Draft PR：编号、draft、标题正文、head/base 引用与 SHA、仓库 |
| `PublicationOutcome` | 最终状态、原因、审批编号、审查回执、步骤回执、PR 与回退 patch |
| `PublicationEpisode` | 写入 Episodic Memory 的发布结果记录 |

`seal_bundle` 是唯一生成 `payload_hash` 的入口：对仓库、base、tree、proposal、
分支、标题、正文、完整 diff 与按路径排序的文件内容计算 SHA-256 摘要。
`ReviewBundle` 在构造时重新计算并校验，因此任何字段被替换都会失败。

## 3. 审批绑定与失效

`PublicationApproval.grant` 计算：

```
approval_id = SHA-256(actor, repository, base_sha, tree_hash,
                      payload_hash, diff_sha256, approved_at)
```

发布前的 `_authorize` 会同时校验：

- `approval_id` 是否等于按字段重算的内容摘要（防止审批被拼接或篡改）；
- 审批与 bundle 的 repository、base commit、tested tree、payload hash 是否一致；
- 审批记录的 diff 摘要是否等于当前 diff 的 SHA-256。

任一不符即抛出 `DomainError("APPROVAL_STALE", ..., 409)`，发生在任何传输调用
之前，因此不会产生任何远端写入。diff 微调、重新测试后的 tree 变化或 base
commit 前移都要求重新审查与重新批准。

## 4. 读写传输分离

| 传输 | 能力 | 说明 |
| --- | --- | --- |
| `MCPReadTransport` | `branch_sha`、`commit_blobs`、`find_draft` | 只读工具 `get_commit`、`list_pull_requests`、`pull_request_read`，协议上不存在写方法 |
| `MCPWriteTransport` | `create_branch`、`push_files`、`create_draft_pull_request` | 构造时必须显式传 `scope="write"`，否则立即 `GITHUB_WRITE_SCOPE_REQUIRED`（403） |

`DraftPRPublisher` 同时注入读、写两个 transport。测试使用两个独立的
`ReadOnlyGitHubMCP` 配置，对应 `settings.github_read_token` 与
`settings.github_write_token` 的分离；只读凭据无法通过构造变成写者。

写权限缺失时（`writer.scope != "write"`）不进行任何远端调用，返回
`write_permission_required` 并把完整 `bundle.diff` 放进 `outcome.patch`。

## 5. 发布状态机

`publish` 的顺序固定：

1. `review(bundle)`：diff 必须包含每个文件的 `b/<path>` 段，统计增删行；
2. `_authorize`：审批绑定校验；
3. 写权限检查；不足则导出 patch；
4. 读取 base 分支 SHA；不等于审批的 `base_sha` 时返回 `base_changed`，
   零写入；
5. `branch`：
   - 不存在 → `create_branch`，读回必须指向审批的 base，记 `succeeded`；
   - 已指向 base → `already_present`；
   - 已指向内容完全匹配的 commit → branch 与 commit 均为 `already_present`，跳过重写；
   - 指向其他 commit → `outcome_unknown`，不覆盖外部分支；
6. `commit`：`push_files` 后读回分支头，逐个文件比较 Git blob SHA 与
   `blob(content)`，不匹配即视为未知结果；
7. `pull_request`：
   - 已存在且 draft、标题正文、head/base 引用与 SHA、仓库全部匹配 →
     `already_present`；
   - 已存在但不匹配 → `outcome_unknown`；
   - 不存在 → `create_draft_pull_request`，读回编号与全部字段校验后记 `succeeded`；
8. 三步全部完成 → `published`，`pull_request.draft` 必为 `True`。

`DraftPRPublisher` 没有任何合并方法；测试同时断言模块导出中不存在 `merge`。

## 6. 未知结果与只读对账

任何写调用抛出异常（网络中断、MCP 响应损坏、fixture 的 `lose_response`）都会
被归类为 `outcome_unknown`，保留已完成的步骤回执并立即返回，不重试。

`reconcile` 只使用读 transport：

- 分支不存在 → 无法证明完成，`outcome_unknown`；
- 分支仍指向 base → 只有 branch，`outcome_unknown`；
- 分支头内容与审批文件不符 → `outcome_unknown`；
- commit 匹配但找不到精确的 Draft PR → `outcome_unknown`；
- 三者在远端都精确匹配 → `published`，三步均为 `already_present`。

对账过程不调用任何写方法，因此重复对账不会产生新的 branch、commit 或 PR。

## 7. Episodic Memory

`DeliveryMemorySink` 协议与存储解耦；默认 `MemoryPublicationLog` 在进程内保存。
`publish` 与 `reconcile` 的每个出口都会写入一条 `PublicationEpisode`：

- `published`：附 PR 编号与 `draft=True`；
- `write_permission_required`：附回退原因；
- `base_changed`：要求重新测试与重新审批；
- `outcome_unknown`：附异常类型与信息，供后续只读对账。

`episode_id` 由仓库、proposal、分支、base、tree、payload、状态、原因与 PR 编号
共同摘要，成功与失败都可追溯。

## 8. 测试与验收

验收命令：

```bash
uv run --frozen pytest tests/test_v34_pr_delivery.py -q
```

覆盖：

1. 已批准修复在真实 stdio MCP fixture 仓库中创建真实 Draft PR：三步回执、
   `writes == ["branch", "commit", "pull_request"]`、PR `draft=True`、Memory
   记录 `published`，随后只读对账返回 `published` 且写入列表不变；
2. diff、tested tree、base commit 任一变化后旧审批 `APPROVAL_STALE`，零写入；
3. base 分支前移时返回 `base_changed`，零写入；
4. 只读凭据返回 `write_permission_required`，导出完整 patch，不调用写入；
5. fixture 在 branch 或 pull_request 写后丢失响应：第一次 `outcome_unknown`，
   对账后分别保持未知或恢复 `published`，写入列表保持不变（不重放）；
6. 外部分支指向不匹配 commit 时返回 `outcome_unknown`，不覆盖；
7. 不完整 diff 被 `DELIVERY_DIFF_INCOMPLETE` 拒绝；越权路径、隐藏路径、
   错误 payload hash 与 base 同名分支在构造阶段被拒绝；
8. `MCPWriteTransport` 未显式声明写 scope 时拒绝；只读传输不具备写方法。

结果：`11 passed`；Ruff 与 mypy strict 通过。

## 9. 已知边界

- 真实 GitHub 写入需要单独的 write profile、写权限令牌与人工授权；本卡的自动化
  验收只覆盖受控 fixture MCP 与真实 Draft PR 语义；
- `commit_blobs` 依赖 `get_commit.files[].sha`（Git blob SHA-1），对超过 100 个
  文件的提交需要分页，本卡限制单次发布最多 16 个文件；
- 不支持文件删除、重命名检测与二进制内容；
- 只读对账不修复不一致对象，发现冲突一律保留 `outcome_unknown` 交由人工处置。
