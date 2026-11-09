# M01 — Memory Schema & Lifecycle

- 状态: 工程门 PASS（13 tests）
- 依赖: `ContextStore` / `knowledge.memories`（旧契约）
- 迁移: `v3/migrations/0021_memory_schema.sql`
- 代码: `v3/src/mgar_v3/memory/`
- 测试: `v3/tests/test_v33_memory_schema.py`

## 1. 目标与边界

M01 在旧 `knowledge.memories` 与 `ContextStore` 之上建立新版 Memory 数据契约：

1. Working / Episodic / Semantic / Procedural 四类记忆共享统一身份模型；
2. 身份包含 Project / Repository / Commit 作用域、Actor、类型与稳定 Subject Key；
3. 每条记忆引用原始摘要哈希、Evidence 与 Artifact；
4. 记录可信度与验证状态；
5. Supersede / Revoke / Stale 是显式生命周期动作，不覆盖历史；
6. 每次读写都做 Project / Repository / Actor 权限校验；
7. 旧 Memory 原样保留，可继续读取，并可显式导入为新契约记录。

明确不做：

- 不删除、不改写 `knowledge.memories`；
- 不自动把旧记忆批量回填到新表（避免隐式覆盖与不可审计迁移）；
- 不引入向量召回或模型调用；本卡只定义数据契约与访问门。

## 2. 统一身份模型

`knowledge.memory_records` 是唯一身份表，四个类型共用：

```text
(project_id, repository, actor, memory_kind, scope_kind, scope_key, subject_key)
```

- `memory_kind`：`working` / `episodic` / `semantic` / `procedural`
- `scope_kind`：`project` / `repository` / `commit`
- `scope_key` 由作用域自动决定：Project 为 `project_id`，Repository 为 `repository`，Commit 为 40 位 SHA
- Commit 作用域必须提供 `commit_sha`；Project / Repository 作用域不允许钉 Commit
- 同一身份最多一条 `active` 或 `stale` 记录（部分唯一索引 `memory_one_current_identity`）

类型专属载荷存放在窄表，避免把四类结构塞进同一张物理表：

| 类型 | 明细表 | 必填引用 |
| --- | --- | --- |
| Working | `knowledge.memory_working` | `run_id`、`expires_at` |
| Episodic | `knowledge.memory_episodic` | `ticket_id`、`run_id`、事件类型与时间 |
| Semantic | `knowledge.memory_semantic` | subject / predicate / object |
| Procedural | `knowledge.memory_procedural` | `task_key`、recipe Artifact、成功/失败计数 |

## 3. 证据、工件与可信度

公共字段：

- `summary_hash`：原始摘要 Artifact 哈希，外键指向 `app.artifacts(content_hash)`；
- `artifact_hashes`：附加 Artifact 引用数组（逐个校验存在）；
- `evidence_ids`：Evidence UUID 数组（逐个校验存在且属于同一 Project / Repository）；
- `confidence`：`numeric(4,3)`，范围 `[0,1]`；
- `verification_status`：`unverified` / `supported` / `verified` / `disputed`。

`verified` 必须至少有一个 Evidence；`supported` 允许只有 Artifact 支撑；没有任何引用时默认 `unverified`。

## 4. 生命周期

| 动作 | 语义 | 允许的前置状态 |
| --- | --- | --- |
| `create` | 新建当前版本 | 同一身份无 active/stale 记录 |
| `supersede` | 新建替代版本，旧记录标记 `superseded` 并写入 `superseded_by` | `active` / `stale` |
| `revoke` | 标记 `revoked`，记录原因与时间 | `active` / `stale` |
| `mark_stale` | 标记 `stale`，记录原因与时间 | `active`（重复标记幂等） |

不可静默覆盖：

- 内容列（身份、摘要哈希、证据、工件、可信度、验证状态、创建时间）由触发器禁止 UPDATE；
- `DELETE` 被触发器拒绝，只能 Revoke；
- `superseded` / `revoked` 为终态，不能改回其他状态；
- `superseded_by` 外键可延迟到事务提交，保证「旧记录指向同一事务内新建的替代记录」。

`MemoryStore` 同时提供：

- `get`：默认只返回 `active` / `stale`，历史记录需 `include_inactive=True`；
- `current`：按身份返回当前版本（含 `stale`，过期 Working 除外）；
- `history`：按身份返回全部版本；
- `recall`：只返回 `active`，过期 Working 自动排除；
- `legacy`：读取旧 `knowledge.memories` 中未撤销的记忆。

## 5. 权限与作用域

所有公开方法先解析 Actor：

- Actor 不在 `allowed_actors` → `MEMORY_ACTOR_DENIED`（403）；
- Draft 的 Actor 与调用 Actor 不一致 → 拒绝；
- Project / Repository 与当前 `Settings` 不一致 → `MEMORY_SCOPE_DENIED`（403）；
- 读取记录时统一过滤 `project_id`、`repository`、`actor`；
- 跨仓库、跨项目、跨 Actor 读取一律以 `MEMORY_NOT_FOUND`（404）返回，避免泄露存在性；
- Evidence、Run、Ticket 必须属于同一 Project / Repository，否则拒绝。

## 6. 旧记忆兼容

旧表保持原样：

- `MemoryStore.legacy()` 读取旧表未撤销记录，映射为 `episodic` / repository 作用域 / `legacy=True`；
- `MemoryStore.get(old_id)` 在旧表命中时返回同样的规范化记录；
- `MemoryStore.import_legacy(old_id)` 显式生成一条新契约记录，`legacy_memory_id` 唯一指向旧记录；
- 导入幂等；旧记录的 `summary_hash`、`evidence_ids`、`revoked_at` 不会被修改；
- 旧记录已撤销时，`legacy()` 不返回，`get` / `import_legacy` 返回未找到。

`ContextStore.resolve("memory", ...)` 未改动，旧读取路径继续可用。

## 7. 测试与验收

验收命令：

```bash
make migrate
uv run --frozen pytest tests/test_v33_memory_schema.py -q
```

测试覆盖：

1. 0021 迁移新增表且不破坏旧表，重复迁移为空操作；
2. 四类记忆共享身份模型并可按类型召回；
3. Commit 作用域必填、可过滤，Project / Repository 作用域拒绝钉 Commit；
4. 跨仓库、跨项目读写被拒绝；
5. 跨 Actor 读取被拒绝，非法 Actor 被拒绝；
6. 旧记忆可读、可导入、可幂等导入，旧表内容不变；
7. 已撤销旧记忆不可见、不可导入；
8. 同身份重复创建冲突；Supersede 保留历史；直接 UPDATE / DELETE 被触发器拒绝；
9. Revoke 隐藏召回但保留历史；
10. Stale 保留但不再召回，可被 Supersede；
11. Working 过期后不再召回；
12. 缺失 Artifact / Evidence、跨作用域 Evidence、类型引用不一致均被拒绝；
13. `verified` 缺 Evidence 被拒绝，非法可信度/验证状态被模型拒绝。

结果：`13 passed`，Ruff / mypy strict 通过。
