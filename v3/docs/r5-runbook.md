# R5 受限修复工程

当前可复现自有 Go 示例的真实隔离测试、完整补丁审批及本地 MCP Draft PR 协议闭环。补丁提供者和发布远端均为 fixture；真实付费模型、真实 GitHub 源与 Draft PR 的组合尚未接入验收，R5 整体仍在进行。

## WSL 更新和准备

在你自己的克隆目录执行。下面以 `~/projects/MAGR-v3` 为例；云端 `/workspace` 路径不会出现在你的 WSL。

```bash
cd ~/projects/MAGR-v3
git pull --ff-only origin main
cd v3
make setup
make deps-up
make migrate
make sandbox-build
make doctor
```

需要 Python 3.12、uv、可访问的 Docker Engine 和 Linux Go >=1.23。若 PATH 的 `go` 不是编译器，使用 `MGAR_V3_GO=/你的/go/bin/go make sandbox-build`。构建从已安装的本机 Go 工具链生成 V3 独立镜像，Docker 构建不联网，不修改 V2。镜像锁、辅助程序与示例 Git 对象位于 `.local/r5/`。更换可信 Go 源或镜像后须重建并重新测试。

## 单机完整协议演示

先停止 `make dev`，让演示程序持有唯一 Engine 锁。它只创建本地 Ticket，Issue 编号是本地关联键，不会创建 GitHub Issue。

```bash
make -s r5-demo > .local/r5-review.json
```

输出包含 run_id、完整 diff、基础提交、公开/隐藏测试回执及 payload_hash。原始缺陷必须实际失败，候选通过独立测试才出现 Action。审阅文件后，用其中的准确值执行：

```bash
uv run --frozen python scripts/r5_demo.py --run RUN_ID --approve-payload-hash PAYLOAD_HASH
```

这一步只向本机 `.local/r5/remote-RUN_ID.json` 的 stdio MCP 协议远端记录分支、提交和 Draft PR。它不访问 GitHub、不合并代码。相同对象成功后不会再次发布；结果未知时只进行读取对账，部分对象保留等待检查。

## Web 和 CLI

```bash
make dev
# 另一个终端，仍在你的 v3 目录
uv run --frozen mgar repair --issue 5002
uv run --frozen mgar repair-detail RUN_ID
uv run --frozen mgar actions RUN_ID
uv run --frozen mgar approve RUN_ID --action ACTION_ID --payload-hash PAYLOAD_HASH
uv run --frozen mgar reject RUN_ID --action ACTION_ID --payload-hash PAYLOAD_HASH
uv run --frozen mgar revise RUN_ID --action ACTION_ID --payload-hash PAYLOAD_HASH --diff-file patch.diff
```

浏览器打开 `http://127.0.0.1:18080`，用 `.env` 中的本机令牌连接。选择“受限修复演示”，可审阅完整补丁、基础提交、到期时间和测试记录。修改编辑框会禁用旧提案的批准按钮，必须先提交重测。默认服务仅生成、审批 Action；没有配置 Draft Transport 时，批准后保持 ready，不会借用评论发布器写 PR。本地演示程序可在服务停止后领取此 ready 动作。

API 包括 `POST /api/v3/repairs`、`GET /api/v3/runs/{id}/repair`、作用域内的测试 Artifact，以及 Action 的 reject/revise。沿用统一 approvals 与只读 reconcile API。fixture 与 live 显式区分：当前 live 入口返回 R5_PROFILE_REQUIRED；R4 剩余额度不用于 R5。

## 验证

```bash
make lint
make typecheck
make verify TASK=V31-R5
make test
```

验收使用真实 PostgreSQL、Go、Docker、LangGraph checkpoint 和 stdio MCP；不允许 SQLite 或跳过测试。包括原始缺陷、正常补丁、公开测试过拟合、路径/AST 攻击、权限、编辑、取消、超时/输出上限、三类响应丢失，以及四个真实 SIGKILL 点。原始构建失败与测试日志保存在本机 `.local/` 和 `evidence/`；公开摘要单独发布。
