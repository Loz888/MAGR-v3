# R02 — Generic Worktree & Patch Editor

- 状态: 工程门 PASS（8 tests）
- 依赖: R01（Allowed Paths 与授权作用域）
- 迁移: 无
- 代码: `v3/src/mgar_v3/repair/worktree.py`
- 测试: `v3/tests/test_v34_patch_workspace.py`

## 1. 目标与边界

R02 提供语言无关的隔离 Worktree 与补丁编辑器。它从调用方给出的基础文件树
在私有临时目录中创建副本，允许一次原子地修改多个文件、新增源文件与测试文件，
并在每次保存前后计算 Git 风格 Tree Hash。

明确不做：

- 不读取、不写入宿主机真实项目目录；Worktree 根目录永远是 `mkdtemp` 产物；
- 不执行构建、测试或发布（R03/R04/R05）；
- 不依赖旧 R5 的 `orders.go`、固定测试名或商城业务字段；
- 不支持二进制文件编辑；Worktree 内容必须是 UTF-8 文本。

## 2. 工具面

`PatchWorktree` 暴露任务书建议的五个工具：

| 工具 | 方法 | 行为 |
| --- | --- | --- |
| `read_worktree_file` | `read_worktree_file(path)` | 返回内容、SHA-256、字节数与行数；拒绝符号链接与超限读取 |
| `apply_patch` | `apply_patch(edits)` | 原子应用多文件 update/create/delete，返回前后 Tree Hash |
| `get_diff` | `get_diff()` | 相对基线的统一 diff，含 `diff --git` 头与新增/删除标记 |
| `revert_patch` | `revert_patch()` | 撤销最近一次补丁并恢复其前置 Tree Hash；支持逐层回退 |
| `get_changed_files` | `get_changed_files()` | 返回每个变更文件的增删行数、字节数与哈希 |

`apply_patch` 接收 `FileEdit` 列表：`update` 与 `delete` 必须携带读取时的
`expected_sha256`，`create` 不允许携带前置哈希；同一路径在一次补丁中只能出现
一次。所有编辑先在内存中完成校验与统计，全部通过后才写盘；任一写入失败会
恢复旧内容并返回 `PATCH_IO`。

## 3. 配额

`PatchLimits` 均可配置，默认值偏保守：

| 配额 | 默认 | 说明 |
| --- | --- | --- |
| `max_files` | 8 | 单次补丁最多变更文件数 |
| `max_changed_lines` | 400 | 单次补丁新增 + 删除行数 |
| `max_total_bytes` | 65 536 | 单次补丁写入的总字节数 |
| `max_file_bytes` | 65 536 | 单个文件上限（读取与写入共用） |
| `max_deleted_files` | 0 | 默认禁止删除；可按仓库需要放宽 |
| `allow_new_files` | true | 关闭后拒绝 create |
| `protected_globs` | 见下 | 受保护配置匹配规则 |

默认受保护配置：`.git/**`、`.github/**`、`.env*`、密钥文件、`Dockerfile`、
`docker-compose*.yml`、`go.mod`/`go.sum`、`pyproject.toml`、`requirements*.txt`、
`package.json` 与各类锁文件。命中返回 `PATCH_PROTECTED`。

隐藏路径（任一段以 `.` 开头）在路径校验阶段即被拒绝，因此 `.git/config`、
`.github/workflows/*` 不会进入配额或受保护判断。

## 4. 路径与符号链接

每个路径必须：

1. 是仓库相对路径，不允许绝对路径、`~`、反斜杠、盘符冒号与控制字符；
2. 不含 `..` 或空段（`validate_manifest_path`）；
3. 属于 R01 `handoff.allowed_paths`，否则 `PATCH_PATH_DENIED`；
4. 写入时逐段检查现有路径是否为符号链接；父目录解析后必须仍在 Worktree 根内。

`PatchWorktree.from_handoff(handoff, files)` 直接继承 R01 的 Allowed Paths，
避免调用方另行维护一份写入白名单。

## 5. Tree Hash 与回滚

`tree_hash()` 复用 R5 的 Git Tree 算法（`repair/workspace.py::tree`），对路径与
文件内容做确定性哈希。每次 `apply_patch` 返回：

- `tree_before`：补丁前工作树哈希；
- `tree_after`：补丁后工作树哈希；
- `patch_id`：`tree_before + tree_after + edits` 的内容摘要。

`revert_patch()` 只在当前树仍等于最近补丁的 `tree_after` 时执行，恢复该补丁
记录的完整前置文件映射并弹出回执；连续回滚会依次回到更早的树，耗尽后返回
`PATCH_REVERT_EMPTY`。任何外部改动造成的哈希漂移返回 `PATCH_STALE`。

## 6. 拒绝矩阵

| 条件 | 错误码 | HTTP |
| --- | --- | --- |
| 空补丁或无内容变化 | `PATCH_EMPTY` | 400 |
| 同一路径重复编辑 | `PATCH_FORMAT` | 400 |
| 非 UTF-8/含 NUL 文本 | `PATCH_FORMAT` | 415 |
| 路径越界、未授权、符号链接、隐藏路径 | `PATCH_PATH_DENIED` | 403 |
| 受保护配置文件 | `PATCH_PROTECTED` | 403 |
| 删除数量超限 | `PATCH_DELETE_DENIED` | 403 |
| 新文件被禁用 | `PATCH_CREATE_DENIED` | 403 |
| 文件数/行数/字节超限 | `PATCH_LIMIT` | 413 |
| 前置哈希不匹配 | `PATCH_STALE` | 409 |
| 创建已存在文件 | `PATCH_CONFLICT` | 409 |
| 更新/删除不存在的文件 | `PATCH_NOT_FOUND` | 404 |
| 无补丁可撤销 | `PATCH_REVERT_EMPTY` | 409 |
| 写盘失败或读回不一致 | `PATCH_IO` | 500 |

## 7. 隔离性

Worktree 根目录由 `tempfile.mkdtemp(prefix="mgar-patch-")` 创建，可选的
`worktree_root` 只指定父目录。构造时只写入调用方显式提供的 `files`，
不读取宿主机项目；测试用哨兵文件确认宿主目录在整个补丁与回滚过程中未被修改，
并在 `with` 退出后确认 Worktree 已被删除。

## 8. 测试与验收

验收命令：

```bash
uv run --frozen pytest tests/test_v34_patch_workspace.py -q
```

覆盖：

1. Python 四文件补丁（两改两增）应用、diff、变更清单与完整回滚；
2. Go 多文件补丁复用同一编辑器，diff 不含任何旧 R5 演示文件；
3. 文件数、行数、字节数、删除与新文件开关逐项生效；
4. 路径穿越、绝对路径、隐藏 Git 路径、未授权路径、受保护配置分别拒绝；
5. 符号链接目录内的写入与读取被拒绝，链接目标文件保持不变；
6. 过期前置哈希、重复创建、缺失更新/删除、空补丁与空回滚分别拒绝；
7. 宿主项目哨兵文件不被修改，Worktree 退出后清理；
8. R01 Handoff 的 Allowed Paths 可直接作为 Worktree 写入作用域。

结果：`8 passed`；Ruff 与 mypy strict 通过。
