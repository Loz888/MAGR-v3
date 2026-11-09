# M02 — Memory Write Gate

- 状态: 工程门 PASS（15 tests）
- 依赖: M01（`knowledge.memory_records` 与 `MemoryStore`）、A03（`Finding`）、A04（补丁验证结果）
- 迁移: 无（复用 `v3/migrations/0021_memory_schema.sql`）
- 代码: `v3/src/mgar_v3/memory/write.py`
- 测试: `v3/tests/test_v33_memory_write.py`

## 1. 目标与边界

M02 把「Agent 刚做完一件事」转换成可控的长期经验，而不是让模型自由写记忆：

1. 只在六个明确执行时机产生候选：源码调查完成、Finding 获得新验证证据、一轮补丁验证失败、补丁通过指定测试、PR 审批结果确定、发布结果确定；
2. 候选必须经过 `Validation` 才能进入 `knowledge.memory_records`；
3. 成功与失败是两条独立经验，身份与事件类型不同，状态不会互相覆盖；
4. 自动写入仅限「有明确来源的执行经历」；跨任务知识、可执行流程、高风险经验必须显式确认；
5. 敏感凭据、Prompt Injection、未经验证的推论在写入门被拒绝；
6. 证据、工件、Run、Ticket 必须真实存在且属于同一 Project / Repository。

明确不做：

- 不调用模型生成或改写记忆内容；
- 不自动把 semantic / procedural 候选写入为当前知识；
- 不在此卡做记忆召回排序或冲突合并（属于后续卡）；
- 不修改 M01 的 schema 或生命周期规则。

## 2. 候选契约

`MemoryCandidate`（`v33-memory-candidate-v1`）字段：

| 字段 | 说明 |
| --- | --- |
| `trigger` | 六个产生时机之一 |
| `outcome` | `success` / `failure` / `neutral` |
| `provenance` | `mechanical` / `agent_reported` / `human` / `inferred` |
| `verification_status` | M01 的 `unverified` / `supported` / `verified` / `disputed` |
| `high_risk` | 高风险经验需要确认 |
| `summary_hash` | 原始摘要 Artifact |
| `artifact_hashes` | 附加 Artifact |
| `evidence_ids` | Evidence UUID |
| `detail` | M01 的类型专属载荷 |
| `summary` | 面向人的摘要，仅用于验证与展示，不落库 |

候选身份由 `resolved_subject` 决定：

```text
{trigger}:{subject_key}:{outcome}
```

Episodic 载荷的 `event_kind` 被覆盖为 `{trigger}:{outcome}`。因此
`patch_tests_passed:success` 与 `patch_verification_failed:failure` 是两条不同的记忆，
即使 `subject_key` 相同也不会互相覆盖。

## 3. 触发时机与 Outcome 矩阵

| Trigger | 允许的 Outcome |
| --- | --- |
| `investigation_completed` | `success` / `failure` |
| `finding_evidence_added` | `success` / `failure` / `neutral` |
| `patch_verification_failed` | `failure` |
| `patch_tests_passed` | `success` |
| `pr_approval_decided` | `success` / `failure` |
| `release_decided` | `success` / `failure` |

模型层直接拒绝不匹配的组合（例如「补丁验证失败」+ `success`），
避免调用方把失败事件写成成功经验。

`candidate_from_finding` 将 A03 Finding 映射为候选：

- `not_reproduced` → `failure`；
- `reproduced` / `confirmed_static` / `patch_verified` → `success`；
- 其余（`suspected`、`investigating`、`fix_proposed`、`awaiting_approval`）→ `neutral`；
- `finding.commit` 决定 commit 作用域；
- 有 Evidence 时 `provenance=mechanical`、`verification_status=verified`，否则为
  `agent_reported` / `supported`；
- `fix_risk=high` 的 Finding 产生高风险经验候选。

## 4. 自动写入与显式确认

`MemoryWriter.validate()` 返回：

```text
CandidateValidation(candidate_id, accepted, requires_confirmation, auto_confirmable, issues)
```

确认策略：

| 候选 | 策略 |
| --- | --- |
| Episodic 执行经历（普通风险） | 可自动确认 |
| Semantic（跨任务知识） | 必须显式确认 |
| Procedural（可执行流程） | 必须显式确认 |
| `high_risk=True`（含高风险 Finding） | 必须显式确认 |

`MemoryConfirmation` 必须满足：

1. `candidate_digest` 精确等于候选摘要，确认不能复用到被篡改的候选；
2. `confirmed_by` 在 `allowed_actors` 中；
3. 确认人不能是候选作者（禁止自证）。

`write()` 的错误码：

| 错误码 | HTTP | 含义 |
| --- | --- | --- |
| `MEMORY_CANDIDATE_REJECTED` | 422 | Validation 未通过 |
| `MEMORY_CONFIRMATION_REQUIRED` | 409 | 需要确认但未提供 |
| `MEMORY_CONFIRMATION_INVALID` | 403 | 摘要不匹配、确认人无权或自证 |
| `MEMORY_ACTOR_DENIED` | 403 | Actor 无权 |
| `MEMORY_SCOPE_DENIED` | 403 | 候选超出当前 Project / Repository |

## 5. 内容过滤

写入前对所有文本字段做正则过滤，错误信息只报告字段名，不回显命中内容：

- 私钥头、AWS Access Key、GitHub Token、`sk-` Key；
- `api_key` / `secret` / `token` / `password` 等赋值形式；
- `Bearer` Token、带密码的数据库连接串；
- Prompt Injection：忽略先前指令、系统提示、角色替换、越狱、隐藏指令等。

`provenance=inferred`、`verification_status=disputed`、`verified` 但无 Evidence、
既无 Evidence 也无 Artifact 的候选一律拒绝。

## 6. 引用校验

Validation 会连接 PostgreSQL 校验：

- `summary_hash` 与每个 `artifact_hashes` 存在于 `app.artifacts`；
- 每个 Evidence 存在，且 `project_id`、所属 Thread 的 Project、Repository 与候选一致；
- Episodic 的 Run、Ticket 存在且属于同一作用域，Run 必须属于该 Ticket；
- Procedural 的 `recipe_hash` Artifact 存在。

伪造 Evidence、跨仓库 Evidence、跨仓库 Run/Ticket 均不能成为 Confirmed Memory。
校验通过后仍由 M01 `MemoryStore` 再校验一次引用与权限，写入门不替代存储层约束。

## 7. 测试与验收

验收命令：

```bash
uv run --frozen pytest tests/test_v33_memory_write.py -q
```

测试覆盖：

1. 成功调查可自动确认，写入后 `verified`、事件类型为 `investigation_completed:success`；
2. 补丁通过（成功）与补丁验证失败（失败）是两条不同记忆，状态与事件类型不混淆；
3. A03 Finding 转换为 commit 作用域候选，成功/未复现映射到不同 Outcome；
4. Semantic 知识未确认被拒绝，确认后可读；
5. Procedural 流程未确认被拒绝，`recipe_hash` 必须真实存在；
6. 高风险经验未确认被拒绝；
7. 确认必须外部、且绑定候选摘要；摘要不符、无权限、自证均被拒绝；
8. 伪造 Evidence、跨仓库 Evidence、跨仓库 Run/Ticket 被拒绝；
9. 缺失 Artifact、未验证推论、Disputed 证据、无来源候选被拒绝；
10. 秘密内容被拒绝且错误信息不回显秘密；
11. Prompt Injection 被拒绝；
12. Trigger 与 Outcome 不匹配在模型层被拒绝；
13. Working 记忆与非法 Actor 被拒绝；
14. PR 审批与发布结果是两条独立经验；
15. 写入后的记忆可通过 M01 `MemoryStore` 读取、`current` 与 `history` 命中。

结果：`15 passed`，Ruff / mypy strict 通过。
