# A02 — Scanner Registry & Isolated Language Scanners

- 状态: 工程门 PASS（13 tests）；live 门 PASS（真实 Docker Go 仓库、真实 Ruff/pytest 固定件）
- 依赖: A01（固定 Commit 工作区与 Manifest）
- 代码: `v3/src/mgar_v3/scanners/{domain,sandbox,registry,go,python,dependency,semgrep}.py`
- 测试: `v3/tests/test_v33_scanners.py`

## 1. 目标与边界

A02 把 A01 的只读仓库快照升级为「可声明、可隔离、可复现」的扫描层：每种语言工具都以显式
契约注册，扫描结果区分 `completed / unsupported / skipped / timeout / failed`，不支持的语言
必须显式标记 `unsupported`，不得笼统声称「扫描完成」。

明确不做：

- 不执行未经白名单认可的安装、下载或任意构建命令（无 `pip install`、`go get`、`go mod download`、shell）。
- 不把仓库文件直接交给工具：先物化到一次性快照，源工作区只读且前后哈希校验。
- 不静默跳过缺失工具：必需工具缺失产生 `CAPABILITY_MISSING` 且 `complete=False`。

## 2. 结果契约

`scanners/domain.py` 定义 `v33-scan-result-v1` / `v33-scan-report-v1`：

```python
ScannerSpec(
    id, version, language, required_capabilities,
    timeout, resource_limits, output_schema, description, optional,
)
ResourceLimits(cpu_seconds, memory_bytes, max_output_bytes, max_findings)
ScanFinding(scanner_id, rule_id, category, severity, confidence, message, location)
ScanResult(schema_version, scanner_id, version, language, status, required,
           findings, diagnostics, exit_codes, coverage, reason, truncated, tool_versions)
ScanReport(repository, commit, results, unsupported_languages, complete, coverage, tool_versions)
```

- `ScanLocation(path, line, column, end_line)` 只记录仓库相对路径与行号。
- `ScannerCoverage` 同时记录分母（`files_considered`）与实际扫描数，覆盖率不靠推断。
- `ToolCapabilities.detect()` 探测 docker 镜像、go、ruff、python/pytest、pyright、semgrep 并记录版本；
  测试可注入 `ToolCapabilities` 保持确定性。

注册的 scanner 与必需能力：

| id | 语言 | 必需能力 | 超时 | 说明 |
| --- | --- | --- | --- | --- |
| `go` | Go | go 或固定 Docker Go 镜像 | 180s | `go vet`、`go build`、`go test -json -vet=off` |
| `go-race` | Go | 同上 + cgo | 300s | 独立预算的 `go test -race`，仅 `include_race=True` |
| `python-ruff` | Python | ruff | 120s | JSON 输出，规则码作为 `rule_id` |
| `python-pytest` | Python | python + pytest | 240s | JUnit 解析；exit 5 → `NO_TESTS` |
| `python-pyright` | Python | pyright | 180s | 仅当 `pyrightconfig.json` 或 `[tool.pyright]` 存在 |
| `dependency` | repository | 无（进程内只读） | 60s | 锁文件/固定版本/CI 供应链信号 |
| `semgrep` | repository | semgrep + 规则目录 | 240s | 可选适配器，无规则/无二进制显式 skip |

## 3. 沙箱与命令策略

`scanners/sandbox.py`：

1. `materialize(context)` 只复制 Manifest 内文件，拒绝绝对路径、`..`、`.git`、符号链接；
   每个写入内容重新计算 `sha1("blob <size>\0"+bytes)` 并与 `FileEntry.blob` 对比，不一致
   `SANDBOX_SNAPSHOT_MISMATCH`。快照目录一次性使用，扫描结束整目录删除。
2. `CommandPolicy` 白名单：
   - `go {vet,build,test}[]`、`ruff check`、`pytest`、`python -m pytest`、`semgrep scan`、
     `pyright {--outputjson,--project,-p}`；
   - 拒绝 `pip`, `go get`, `go mod`, `bash`, `rm` 等可执行文件与 `; | & $ \` > <` 等元字符。
3. `LocalSandbox`：`Popen(start_new_session=True)` + POSIX rlimit（CPU、AS、FSIZE、CORE），
   超时 `killpg`，stdout/stderr 落临时文件并按 `max_output_bytes` 截断；环境仅保留
   `PATH/HOME/TMPDIR/PYTHONPATH/PYTHONDONTWRITEBYTECODE/PYTHONHASHSEED/LANG` 与 Go 离线变量。
4. `DockerSandbox`：
   - `docker create --network=none --read-only --user=65532 --cap-drop=ALL
     --security-opt=no-new-privileges --pids-limit=128 --memory=<limit> --cpus=1 --ipc=none --init`；
   - 快照挂载 `/src:rw`（容器内非 root 可读），缓存挂载 `/cache:rw`；
   - `--env GOPATH=/tmp GOCACHE=/cache GOFLAGS=-mod=readonly GOPROXY=off GOSUMDB=off
     GOTOOLCHAIN=local HOME=/tmp TMPDIR=/tmp`，并清空全部代理变量；
   - **启动前 `docker inspect` 复核** NetworkMode/ReadonlyRootfs/Memory/NanoCpus/PidsLimit/
     CapDrop/SecurityOpt/IpcMode/Init，任何偏差翻转为 `SANDBOX_POLICY_VIOLATION`；
   - 超时 `docker kill`，`finally docker rm -f`，容器退出码经 `docker inspect .State.ExitCode` 复核。

Go 优先使用固定镜像 `golang:1.27.1-bookworm`；镜像/CLI 不可用时才回退到本机 `go`。Ruff/pytest
在本机进程沙箱运行（无 Python 固定镜像），但仍受快照与 rlimit 约束。

## 4. 各扫描器行为

### Go

- `go vet ./...` 非零：解析 `path:line:column: message`，`rule=go-vet`，`category=vet`，
  `severity=warning`。
- `go build ./...` 非零：`rule=go-build`，`category=compile_error`，`severity=error`。
- `go test -json -vet=off ./...`（独立 vet 已执行，故测试阶段关闭内置 vet，避免测试因 lint
  中断而掩盖真实测试失败）非零：
  - `Action=build-fail` → `go-build / compile_error`；
  - `Test` + `Action=fail` → `go-test / test_failure`，消息取自失败输出最后一条有效行。
- 任一步超时 → `status=timeout`；沙箱错误 → `status=failed / SANDBOX_ERROR`。
- 所有命令成功执行（无论是否发现问题）→ `status=completed`，`exit_codes` 全量记录。

### Python

- Ruff JSON：`code` 作为 `rule_id`，`category=lint`；`E/F` 前缀视作 error，其余 warning。
- pytest：`--junitxml` + `-p no:cacheprovider`；exit 5 → `skipped / NO_TESTS`（不是通过）；
  exit 2–4 → `failed / PYTEST_ERROR`；`failure`/`error` 测试用例 → `test_failure / error`。
- Pyright：未配置 → `skipped / OPTIONAL_NOT_CONFIGURED`（`required=False`）；已配置但二进制缺失
  → `skipped / CAPABILITY_MISSING`（`required=True`，故 `complete=False`）。
- 真实 Ruff F401 与真实 pytest 失败均在固定件中产生结构化结果，且源工作区未出现
  `__pycache__`、`.pytest_cache` 或文件变更。

### 依赖与 CI（只读）

- `DEP_LOCKFILE_MISSING`：`go.mod` 无 `go.sum`；Python 清单无 `uv.lock/poetry.lock/Pipfile.lock/pdm.lock/requirements*.txt`。
- `DEP_UNPINNED`：requirements 行或 `[project].dependencies` 不含 `==`/`===`/`~=`。
- `CI_PIPE_SHELL`：`curl/wget ... | bash`（error）。
- `CI_UNPINNED_ACTION`：`uses:` 未固定到 40 位 commit SHA。
- 单文件读取上限 256KB，仅读 Manifest 内文件。

### Semgrep

- 规则缺失或二进制缺失：`OPTIONAL_NOT_CONFIGURED` / `OPTIONAL_TOOL_MISSING`，`required=False`。
- 执行时 `--metrics=off --disable-version-check --no-git-ignore`，解析
  `results[].check_id/path/start.line/extra.message/extra.severity`。

## 5. 完整性语义

`ScanReport.complete` 为真当且仅当：

1. `unsupported_languages` 为空（Java/JavaScript/TypeScript/Rust/C/C++/C#/Ruby/PHP/Kotlin/
   Scala/Swift/Dart/Elixir/Haskell/Lua/Shell/Groovy/Vue/Svelte 等显式 `unsupported`，`required=True`）；
2. 所有 `required=True` 的扫描结果为 `completed`；唯一例外是 pytest `skipped/NO_TESTS`
   （无测试可跑时不得谎称通过，但也不阻塞整体完成）；
3. 不存在 `CAPABILITY_MISSING`、`timeout`、`failed`、`SANDBOX_ERROR`。

覆盖率：`files_considered` 为 Manifest 文件数；Go/Python 文件仅在该语言全部必需扫描器就绪后
计入 `files_scanned`，依赖扫描器计入其实际读取的 Manifest/CI 文件数，再做去重上限约束。

## 6. 测试与证据

`v3/tests/test_v33_scanners.py` 共 13 个测试，全部无 skip：

- 契约字段完整、id 唯一、race 独立预算；
- Rust 仓库显式 `unsupported` 且 `complete=False`；
- 工具链全缺时 Go/Ruff/pytest 均 `CAPABILITY_MISSING`，不声称完成；
- 真实 Docker Go 固定件：`go vet` 与失败测试产生 `go-vet` / `go-test`，源工作区哈希不变；
- fake `go` 验证 `go-race` 仅在 `include_race=True` 执行、超时/CPU 预算独立；
- 真实 Ruff F401 + 真实 pytest 失败；无测试仓库 `NO_TESTS`；
- Pyright 未配置不阻塞、配置后 fake 诊断解析；
- 依赖/CI 四类信号与行号；
- fake Semgrep 解析与无规则 skip；
- 命令策略拒绝安装/下载/shell/元字符；
- 快照检测源文件被改写；
- finding 上限截断与报告 JSON 往返。

验收命令：

```bash
make verify TASK=A02
```

原始输出保存在 `v3/evidence/A02/<UTC>-A02/`（gitignore）；结构化验收记录写入
`v3/docs/v33/accept/A02/accept.json`。
