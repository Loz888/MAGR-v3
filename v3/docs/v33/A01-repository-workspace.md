# A01 — Repository Workspace & Inventory

- 状态: 工程门 PASS（22 tests）；live 门 PASS（两个公开仓库固定 Commit 哈希一致）
- 依赖: B00
- 代码: `v3/src/mgar_v3/repository_analysis/workspace.py`
- 测试: `v3/tests/test_v33_workspace.py`

## 1. 目标与边界

A01 把 V3.2 的「只读 MCP 探索」扩展为「固定 Commit 的本地隔离源码快照 + 可复现仓库清单」。
仓库身份与 Commit 仍由 V3.2 URL Resolver（`parse_repository_url`）与 `validate_commit` 固定，
模型与仓库内容都不能改变它。

明确不做：

- 不执行仓库代码、构建脚本、测试、Git Hook 或子模块脚本。
- 不运行 `git checkout`，因此不会触发 `.gitattributes` 过滤器（LFS、自定义 filter）。
- 不访问网络以外的写入路径；工作区只属于单个 Run。

## 2. 公共 API

```python
RepositoryWorkspace(
    root: Path,                 # 服务器持有的工作区根目录（如 .local/repo-workspaces）
    *,
    repository: str,            # owner/name，规范化小写
    commit: str,                # 40 位 SHA，V3.2 已解析
    remote: str,                # 规范化 GitHub URL；测试/固定件允许本地仓库
    run_id: str,                # 单 Run 隔离键
    limits: WorkspaceLimits | None = None,
).prepare() -> RepositoryManifest
```

- 工作区目录 = `<root>/<sha256(workspace_version, repository, commit, run_id)[:20]>/`
  - `repo/`：隔离 Git 工作树（对象库 + 物化文件）
  - `workspace.json`：Run 归属标记（repository/commit/remote_kind/manifest_hash）
  - `empty-hooks/`：`core.hooksPath` 的空目录，确保任何 Git 命令都不执行仓库 Hook
- 相同 Run 重复 `prepare()` 复用同一目录并重建清单；若标记与请求不一致则 `WORKSPACE_CONFLICT`。
- 远程校验：`https://github.com/<owner>/<repo>` 必须与 `repository` 完全一致，否则
  `WORKSPACE_REMOTE_MISMATCH`；`file://` 或绝对本地目录仅用于固定件测试。

## 3. 克隆与物化流程

1. `git init --template=<empty>`（空模板，不落 sample hooks）。
2. `git remote add origin <remote>`；`git fetch --depth=1 origin <commit>`，
   若服务器拒绝 SHA fetch，则回退为 `+refs/heads/*:refs/remotes/origin/*` 后再次校验。
3. `git rev-parse --verify <commit>^{commit}` 必须等于请求 Commit，否则 `WORKSPACE_COMMIT_NOT_FOUND`。
4. `git symbolic-ref HEAD refs/heads/mgar-workspace` + `git update-ref`，形成固定本地引用；
   `git read-tree <commit>` 建立与 Commit 一致的索引（用于 Git 状态判断）。
5. `git ls-tree -r -z --long <commit>` 取得全量清单与 blob 大小。
6. 用常驻 `git cat-file --batch` 只读管道逐文件取出 blob，按路径校验后直接写盘；
   每个 blob 重新计算 `sha1("blob <size>\0" + bytes)` 并与 Git 对象 ID 对比，
   不一致即 `WORKSPACE_BLOB_MISMATCH`。写入不使用 checkout/filter/hook。
7. `git status --porcelain --untracked-files=no` 记录工作树是否与索引一致（有排除项时为 false）。

Git 调用统一注入安全环境：

- `GIT_CONFIG_NOSYSTEM=1`、`GIT_CONFIG_GLOBAL=/dev/null`：拒绝系统/用户 Git 配置劫持。
- `GIT_TERMINAL_PROMPT=0`：禁止凭据交互。
- `GIT_ALLOW_PROTOCOL=https:file`：禁止 ssh/git 等未声明协议。
- `GIT_LFS_SKIP_SMUDGE=1`、`GIT_OPTIONAL_LOCKS=0`。
- `-c core.hooksPath=<empty-hooks>`、`core.fsmonitor=false`、`submodule.recurse=false`、
  `fetch.recurseSubmodules=false`。

## 4. Repository Manifest

`RepositoryManifest` 字段：

| 字段 | 含义 |
| --- | --- |
| `manifest_version` | `v33-repository-manifest-v1` |
| `repository` / `commit` / `tree` | 固定身份与 Git tree SHA |
| `limits` | 本次扫描的容量上限（参与哈希） |
| `git` | `head`、`ref`、`clean`（Git 状态） |
| `files[]` | 每个已扫描文件的 `path`、`mode`、`size`、`blob` |
| `directories[]` | 目录的文件数、字节数、直接子目录数 |
| `languages` | 语言 → 字节数（扩展名/特殊文件名映射） |
| `inventory` | `dependency_manifests`、`ecosystems`、`ci_configs`、`test_directories` |
| `exclusions[]` | 未纳入扫描的路径与原因 |
| `coverage` | 分母 `tree_entries`、`scanned_files`、`excluded_entries`、`scanned_bytes` |
| `manifest_hash` | 对以上内容（除自身）做 canonical JSON SHA-256 |

清单不含时间戳、主机路径、Run ID 或远端地址，因此相同 `commit + limits` 在任何 Run、
任何工作区根目录都产生相同的 `manifest_hash`。

### 4.1 生态识别

- 依赖清单：`go.mod`/`go.sum`/`go.work`、`pyproject.toml`/`setup.py`/`requirements*.txt`、
  `package.json` + 锁文件、`Cargo.toml`、`pom.xml`/Gradle、`Gemfile`、`composer.json`、
  `pubspec.yaml` 等；归纳为 `go/python/node/rust/java/ruby/php/dart`。
- CI：`.github/workflows/*.yml|yaml`、`.gitlab-ci.yml`、`.circleci/config.yml`、
  `.buildkite/**`、`Jenkinsfile`、Azure/Travis/Drone/Bitbucket/Cloud Build 等。
- 测试目录：路径中出现 `test`、`tests`、`__tests__`、`spec`、`specs` 的目录。

## 5. 安全与限额

- 路径校验 `validate_manifest_path`：拒绝绝对路径、`~`、反斜杠、冒号、空段、`.`、`..`、
  `.git` 段、控制字符与非法 UTF-8；命中记为 `PATH_DENIED`，不中止整份清单。
- 符号链接（mode `120000`）记为 `SYMLINK` 且不物化；子模块/gitlink（`160000`）记为
  `SUBMODULE` 且不递归、不执行其脚本。
- 物化时逐级检查目录非符号链接，最终 `Path.resolve()` 必须位于工作树内
  （`WORKSPACE_SYMLINK_DENIED` / `WORKSPACE_PATH_DENIED`）。
- 容量上限 `WorkspaceLimits`：`max_files`（默认 5000）、`max_total_bytes`（默认 50MB）、
  `max_file_bytes`（默认 8MB）；超限文件记为 `FILE_COUNT_LIMIT`、`TOTAL_BYTES_LIMIT`、
  `FILE_TOO_LARGE`，分母与排除原因全部保留。

## 6. 测试覆盖

`uv run --frozen pytest tests/test_v33_workspace.py -q`（22 passed）：

- 两个结构不同的固定件仓库（Go / Python+Node）各自生成可复现 Manifest；
- 同一 Commit 在独立工作区产生完全一致的 `manifest_hash`、文件、目录、语言与清单；
- 符号链接与 gitlink 被排除且不落盘；
- `../escape`、绝对路径、`.git` 路径、控制字符等恶意路径被拒绝，注入的恶意 tree 条目无法逃逸；
- 文件数/单文件/总量上限与 `coverage` 分母一致；
- 篡改工作区文件后再次 `prepare()` 可恢复内容且哈希不变；
- 远端身份不匹配、非法 repository/commit/run_id、标记冲突均 fail closed；
- Manifest 不包含主机路径、时间戳或工作区标记文件名。
