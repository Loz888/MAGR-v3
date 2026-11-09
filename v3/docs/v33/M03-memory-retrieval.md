# M03 — Hybrid Memory Retrieval

- 状态: 工程门 PASS（12 tests）
- 依赖: M02（Confirmed Memory 写入）、M01（`knowledge.memory_records` 与 `MemoryStore`）
- 迁移: 无（复用 `v3/migrations/0021_memory_schema.sql`）
- 代码: `v3/src/mgar_v3/memory/retrieval.py`
- 测试: `v3/tests/test_v33_memory_retrieval.py`

## 1. 目标与边界

M03 让新任务在开始调查前自动找到与当前仓库、代码路径和问题类型相关的历史经验，
并保证这些经验不会被误当成当前代码事实：

1. 权限过滤先于打分：project / repository / actor 在 SQL 层过滤，越权记录不进入候选集；
2. 同仓库、同 Commit 优先；Commit 作用域记忆必须携带 `current` / `historical` 版本状态；
3. BM25 词法检索 + 可选 Dense 向量检索，通过 RRF 融合，可选 Rerank；
4. 排序同时考虑来源有效性、相关性与时间信息；
5. 过期（stale）记忆仍可返回，但标记 `requires_reverification=True`，
   明确不允许直接替代重新读取当前代码；
6. 无向量模型或向量模型不可用时降级为词法检索，并在结果中说明降级原因；
7. 每条命中都返回来源 Evidence、来源 URI、可信度与验证状态。

明确不做：

- 不新增数据库表或迁移；
- 不建立持久向量索引：Dense 在当前权限范围内的候选集（≤64 条）上即时计算；
- 不做记忆写入、冲突合并或摘要压缩（分别属于 M02 / 后续卡）；
- 不读取 Artifact 中的原始摘要文本，检索文本来自记忆身份、类型载荷与 Evidence 来源。

## 2. 工具接口

`MemoryRetriever`（`v3/src/mgar_v3/memory/retrieval.py`）：

| 工具 | 说明 |
| --- | --- |
| `search_memory(query, scope, top_k)` | 在当前权限范围内做 BM25 + 可选 Dense 的混合检索 |
| `read_memory(memory_id)` | 按 M01 权限门读取单条记忆；撤销/被替代记录默认不可见 |
| `get_prior_findings(path, symbol)` | 返回 Evidence 指向该路径、或 subject 命中该符号的历史发现 |
| `get_repository_knowledge(commit)` | 返回仓库/项目级知识 + 指定 Commit 的经验，并标注版本状态 |

`search_memory` 参数：

- `query`: 1–2000 字符；
- `scope`: `project` / `repository` / `commit`，为空表示当前权限范围内全部作用域；
- `commit`: 与 `scope="commit"` 搭配；其他作用域携带 Commit 视为歧义错误；
- `memory_kinds`: 可选的类型过滤；
- `top_k`: 1–32。

错误码：

| 错误码 | HTTP | 含义 |
| --- | --- | --- |
| `MEMORY_QUERY_INVALID` | 400 | 查询为空或超长 |
| `MEMORY_BOUND_INVALID` | 400 | `top_k` 超出 1–32 |
| `MEMORY_SCOPE_INVALID` | 400 | Commit 与作用域组合不合法 |
| `MEMORY_ACTOR_DENIED` | 403 | Actor 不在允许列表 |
| `MEMORY_RERANK_INVALID` | 502 | Reranker 返回的分数向量不合法 |

## 3. 候选加载与权限

检索从 `MemoryStore.recall(include_stale=True)` 开始：

- SQL 固定 `project_id` / `repository` / `actor` 三个条件；
- 仅返回 `active` 与 `stale`；`revoked` 与 `superseded` 不进入候选；
- working 记忆仍受过期时间约束；
- 候选上限 64 条，按创建时间倒序。

因此无关项目、跨仓库记录、撤权记录在打分之前就已被排除；`read_memory` 同样经过
M01 的 `MemoryStore.get` 权限门。Actor 不在 `allowed_actors` 时直接拒绝，而不是返回空结果。

## 4. 检索文本与混合排序

每条记忆的可检索文本由以下字段拼接（上限 8192 字符）：

- `memory_kind`、`subject_key`；
- 类型载荷：Episodic 的 `event_kind`、Semantic 的 `subject/predicate/object_value`、
  Procedural 的 `task_key`；
- Evidence 的 `source_uri` 列表。

排序流程：

1. **BM25**：复用 `mgar_v3.rag.rank.bm25`，得分归一到 `[0,1]`；
2. **Dense**（可选）：复用 `Embedder` 协议，对查询与候选择文本计算余弦相似度；
3. **RRF**：词法与向量排名通过 `mgar_v3.rag.rank.rrf` 融合；
4. **Rerank**（可选）：自定义 `Reranker` 协议，对融合后的候选重新打分；
5. **最终分数**：

```text
score = relevance
      × (0.5 + 0.5 × confidence)
      × source_validity
      × recency
      × version_affinity
```

其中：

- `source_validity`：`verified=1.0`、`supported=0.8`、`unverified=0.5`、`disputed=0.2`，
  有 Evidence / Artifact 时各加 0.05，封顶 1.0；
- `recency = 1 / (1 + 天数 / 90)`，按整天计算，保证同日记录排序稳定；
- `version_affinity`：`current=1.0`、`repository=0.92`、`project=0.9`、
  `historical=0.8`、`stale=0.7`。

同分时依次按版本优先级、创建时间倒序、`memory_id` 排序，保证确定性。

## 5. 版本状态与重新验证

`version_status(record, commit)`：

| 条件 | 版本状态 | `requires_reverification` |
| --- | --- | --- |
| `status='stale'` | `stale` | 是 |
| Commit 作用域且 `commit` 等于查询 Commit | `current` | 否 |
| Commit 作用域但 Commit 不同（或查询未给 Commit） | `historical` | 是 |
| 仓库作用域 | `repository` | 否 |
| 项目作用域 | `project` | 否 |

因此：

- 旧 Commit 的记忆永远是 `historical`，不会被标记为当前版本事实；
- 查询未显式给出 Commit 时，所有 Commit 作用域记忆都是 `historical`；
- 过期记忆虽然可被召回，但 `requires_reverification=True`，调用方必须先重新读取当前代码。

## 6. 词法降级

- 未注入 `Embedder`：模式为 `lexical_fallback`；
- 注入的 `Embedder` 抛出 `DomainError`（例如本地向量模型未安装）或 `ValueError`：
  同样降级为 `lexical_fallback`，并在 `degraded_reason` 中返回错误码；
- 注入 Reranker 时模式变为 `lexical_reranked` 或 `hybrid_reranked`；
- `get_repository_knowledge` 不使用文本查询，模式为 `structured`。

## 7. 返回结构

`MemorySearchResult`：

- `query`、`commit`、`retrieval_mode`、`dense_model`、`reranker_model`、`degraded_reason`；
- `hits`。

每条 `MemoryHit` 返回：

- 身份：`memory_id`、`memory_kind`、`scope`、`subject_key`、`commit`；
- 版本：`version_status`、`requires_reverification`、`status`；
- 来源：`evidence_ids`、`source_uris`、`verification_status`、`confidence`；
- 分数构成：`relevance`、`lexical_score`、`dense_score`、`source_validity`、`recency`、`score`；
- `created_at`。

## 8. 测试与验收

验收命令：

```bash
uv run --frozen pytest tests/test_v33_memory_retrieval.py -q
```

测试覆盖：

1. 相关记忆被命中，并返回 Evidence、来源 URI、可信度与版本状态；
2. 无关项目记录不泄露，未授权 Actor 被拒绝；
3. 撤权记忆不出现在检索结果，`include_inactive` 可读其撤销状态；
4. 被替代版本不再返回，历史仍可读取；
5. 旧 Commit 记忆标记 `historical` 且排在 `current` 之后；未给 Commit 时全部为 `historical`；
6. 过期记忆返回 `stale` 且要求重新验证，绝不标记为 `current`；
7. 仓库/项目/Commit 知识的版本状态与 `get_repository_knowledge` 过滤正确；
8. 无向量模型与向量模型失败时的词法降级；
9. 可选 Rerank 重新排序融合结果；
10. 相同文本下来源有效性决定排序，时间衰减函数单调；
11. `get_prior_findings` 按 Evidence 路径与 subject 符号匹配；
12. `top_k`、空查询与作用域组合的边界校验。

结果：`12 passed`，Ruff / mypy strict 通过。
