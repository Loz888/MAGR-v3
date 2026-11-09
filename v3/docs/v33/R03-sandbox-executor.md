# R03 — General Sandbox Test Executor

- 状态: 工程门 PASS（10 tests）
- 依赖: R02（隔离 Worktree；R03 只接收显式文件树，不读取宿主项目）
- 迁移: 无
- 代码: `v3/src/mgar_v3/repair/sandbox_executor.py`
- 镜像: `v3/deploy/Dockerfile.sandbox-r03`、`v3/deploy/r03/bootstrap.py`、`v3/deploy/r03/runner.py`
- 构建: `v3/scripts/build_r03_sandbox.py`（`make sandbox-build`）
- 测试: `v3/tests/test_v34_sandbox_executor.py`

## 1. 目标与边界

R03 把 R5 的单业务、单语言沙箱替换为通用执行器：第一阶段同时支持 Go 与
Python，在固定镜像的隔离容器中运行测试，并给出可审计回执。

明确不做：

- 不把 GitHub Token、模型 API Key、数据库口令或任何宿主环境变量注入容器；
- 默认不提供网络，构建与运行阶段都使用 `--network=none`；
- 不接受模型生成的 shell 文本；命令只来自可信适配器的固定 argv；
- 不在无法安全安装依赖时强行执行测试，而是返回等待状态；
- 不读取宿主项目目录，容器没有 bind mount。

## 2. 固定镜像

`make sandbox-build` 运行 `scripts/build_r03_sandbox.py`：

1. 检查本地已存在固定基础镜像 `golang:1.27.1-bookworm`（Go 1.27.1 + Python 3.11）；
2. 解析基础镜像 ID，与构建源码哈希一起写入 `.local/r03/sandbox-lock.json`；
3. 以 `--network=none` 构建 `mgar-v3-sandbox-r03:local`；
4. 在镜像内校验 `/opt/mgar/bootstrap.py` 与 `/opt/mgar/runner.py` 的 SHA-256；
5. 探测并记录 `go_version` 与 `python_version`。

源码哈希与镜像 ID 均匹配时直接复用已有镜像，因此重复验收不会重新构建或拉取。
R5 镜像仍可通过 `make sandbox-build-r5` 单独构建。

## 3. 执行流程

`SandboxExecutor.run(files, adapter)`：

1. 计算输入文件树的 Git Tree Hash；
2. 调用适配器的依赖准备阶段；若返回 `waiting`，直接返回
   `waiting_for_dependencies` 回执，不创建容器；
3. 将文件树打包为受限 tar，经 stdin 交给镜像内的可信 bootstrap；
4. 以固定策略 `docker create`，校验 `docker inspect` 后启动；
5. 按超时、输出上限读取日志，结束后删除容器；
6. 适配器解析结构化事件，产生 `passed`、`build_failed`、`test_failed`、
   `timeout`、`sandbox_error` 或 `no_tests` 回执。

bootstrap 只接受可见的仓库相对路径、普通文件与受限大小，逐字节写入
`/workspace`（容器内 tmpfs），随后 `execv` 适配器命令，不解释 shell 文本。

## 4. 适配器

`GoAdapter`：

- 命令固定为 `go test -count=1 -json -vet=off -buildvcs=false ./...`；
- 解析 `go test -json` 事件，区分测试失败与编译失败（`# package` /
  `[build failed]`，或退出非零且没有任何测试运行）；
- `go.mod` 存在第三方 `require` 且无 `vendor/modules.txt` 时进入等待。

`PythonAdapter`：

- 命令固定为 `/usr/bin/python3 /opt/mgar/runner.py --root /workspace`；
- 镜像内 runner 仅使用标准库，发现 `test_*.py`，支持 `unittest.TestCase`
  与无参 `test_*` 函数，逐测试输出 JSON；
- 导入/收集失败记为 collection error → `build_failed`；断言失败 → `test_failed`；
- AST 扫描第三方 import，并识别 `requirements.txt` 与 `pyproject.toml`
  的 `project.dependencies`；命中即进入等待。

已知边界：第一阶段不执行依赖安装，也不支持 pytest 插件。需要第三方依赖的
仓库必须等到提供受控离线依赖包的后续阶段；这是安全默认值，而不是失败重试。

## 5. 容器策略

每次执行由 `docker inspect` 实际校验，任一不符即 `SANDBOX_POLICY`：

| 维度 | 固定值 |
| --- | --- |
| 网络 | `none` |
| 根文件系统 | 只读；仅 `/workspace` 与 `/tmp` 两个 tmpfs |
| 用户 | `65532:65532`，非 root |
| 能力 | `--cap-drop=ALL`、`no-new-privileges` |
| IPC | `none`，`--init` |
| CPU | `--cpus`（默认 1.0，可配 0.25–4） |
| 内存 | `--memory` 与 `--memory-swap` 相等（默认 512 MB） |
| 进程数 | `--pids-limit`（默认 96） |
| 磁盘 | `/workspace` 默认 64 MB，`/tmp` 默认 512 MB |
| 挂载 | 无 bind mount、无 volume、无设备 |
| 环境变量 | 仅适配器白名单，值逐一核对；密钥形态变量为零 |

宿主环境只用于调用 Docker CLI，且剔除 `DOCKER_HOST` 等变量；容器环境完全由
适配器构造。测试在宿主设置 `GITHUB_TOKEN`、`DEEPSEEK_API_KEY` 等哨兵后，
容器内断言这些变量不存在。

## 6. 回执

`TestReceipt` 记录：语言、适配器、状态、准备回执、镜像引用与镜像 ID、
执行命令、测试集合、通过/失败/跳过数、退出码、耗时、日志 SHA-256 与字节数、
是否截断、输入 Tree Hash、实际容器策略与错误信息。`receipt_id` 是上述内容
（除耗时外）的确定性摘要。

`waiting_for_dependencies` 回执记录依赖清单与原因，`command` 为空，表示没有
执行任何测试；`sandbox_error` 回执覆盖 Docker 不可用、容器创建失败、策略外
异常退出、runner 崩溃与输出截断。

## 7. 测试与验收

```bash
make sandbox-build
uv run --frozen pytest tests/test_v34_sandbox_executor.py -q
```

覆盖：

1. Python 测试在真实固定镜像中通过，回执含命令、镜像、日志哈希、Tree Hash
   与完整容器策略；
2. Go 测试使用同一执行器通过；
3. 容器内网络不可达；
4. 宿主哨兵文件不可见，宿主凭据不进入容器；
5. Python 收集失败与 Go 编译失败归为 `build_failed`，断言失败归为 `test_failed`；
6. 超时被终止并返回 `timeout`；
7. 沙箱进程被杀死归为 `sandbox_error`；
8. Docker 不可用返回 `sandbox_error` 回执；
9. Python/Go 第三方依赖进入等待而非安装；
10. 空输入与路径穿越被拒绝。

结果：`10 passed`；Ruff 与 mypy strict 通过。
