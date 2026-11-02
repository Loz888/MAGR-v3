# R5 受限修复工程

R5 现包含真实 GitHub 固定提交、DeepSeek 补丁和官方 MCP Draft PR 适配，同时保留本地演示。真实/工程验收分别记录；是否通过以最新公开摘要为准，不能用演示结果代替。

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

API 包括 `POST /api/v3/repairs`、`GET /api/v3/runs/{id}/repair`、作用域内的测试 Artifact，以及 Action 的 reject/revise。沿用统一 approvals 与只读 reconcile API。fixture 与 live 显式区分：没有专用配置和授权时，live 返回 R5_PROFILE_REQUIRED。R4 剩余额度不转用于 R5。

## 验证

```bash
make lint
make typecheck
make verify TASK=V31-R5
make test
```

验收使用真实 PostgreSQL、Go、Docker、LangGraph checkpoint 和 stdio MCP；不允许 SQLite 或跳过测试。包括原始缺陷、正常补丁、公开测试过拟合、路径/AST 攻击、权限、编辑、取消、超时/输出上限、三类响应丢失，以及四个真实 SIGKILL 点。原始构建失败与测试日志保存在本机 `.local/` 和 `evidence/`；公开摘要单独发布。


## 真实 GitHub / DeepSeek 验收

先停止开发服务，以免与操作员程序争夺 Engine 锁。真实目标是本仓库 `v3/examples/repair-target` 的三个 Go 文件；它与内部的固定缺陷 fixture 分开。只修复一个纯函数，不代表任意仓库的自动 Coding Agent。

```bash
make install-github-mcp
uv run --frozen python scripts/r5_live.py prepare --directory .local/r5-live-CASE --issue 1
```

prepare 不调用模型、不创建分支或 PR，只生成冻结 source/profile、计划及全部关闭的授权草案。当前验收工具默认继承本次云端已有的 R1/R3 与 R4 根账本；这些私有账本不会随 git pull 出现在 WSL。缺失时禁止实验，不能伪造旧次数或把累计费用清零。其他部署可使用通用 R5 profile 配置自己的真实前序账本，仍需匹配授权。

操作员获得针对准确 profile hash 的新调用授权后，才保存私有 `authorization.json` 并开启 allow_paid。保留原累计 US$2。不要复制或公开实际模型、GitHub、数据库凭据。

```bash
uv run --frozen python scripts/r5_live.py model --directory .local/r5-live-CASE --execute-paid
```

输出为同目录的 model-result.json、proposal.json、proposal.md 和 proposal.diff，包含完整提案、测试证明及 hash。如果所有候选失败，不会创建可发布提案。费用和次数以 PG 账本为准；未知用量不自动重发。

审阅并批准完整提案后，授权文件才可开启 allow_external_write，并执行：

```bash
uv run --frozen python scripts/r5_live.py publish --directory .local/r5-live-CASE --approved-payload-hash HASH
uv run --frozen python scripts/r5_live.py reconcile --directory .local/r5-live-CASE
```

真实成功将得到 GitHub Draft PR；不合并。所有业务写仍经 Engine Action/Outbox/Dispatcher，源码普通 git push 与业务发布是不同的出口。profile 固定提交之后，不要在发布前推进它的基础分支；变化会阻断发布，需要重新冻结、测试和审批。

模型结算后恢复实验可在首次 model 命令加 `--crash-after-settlement`，预期进程被强制结束，再执行不带该参数的 model 命令。PR 回执恢复实验可在首次 publish 加 `--crash-after-pr-accepted`，再执行 reconcile。它们分别复用已结算结果和只读寻找已创建 PR，不重复付费或写入。

只有分支/提交的部分成功时，只读对账后仍保持未知动作，等待人工检查；确认所有已发步骤成功后，可显式授权剩余未发步骤：

```bash
uv run --frozen python scripts/r5_live.py continue --directory .local/r5-live-CASE --approved-payload-hash HASH
```

若仍有任何已发步骤未知，这个命令会拒绝；不会重试该步骤。Web 对应“只读核对远端结果”和“授权继续未发送步骤”，CLI 对应 `reconcile` 和 `continue-draft`。

## 服务中的真实模式

设置以下路径后再启动服务；同一 profile 的问题、Issue、源码与调用额度由服务端固定。Web 显示当前配置，真实模式默认关闭。

```bash
MGAR_V3_REPAIR_PROFILE_PATH=.local/r5-live-CASE/profile.json \
MGAR_V3_REPAIR_AUTHORIZATION_PATH=.local/r5-live-CASE/authorization.json \
MGAR_V3_REPAIR_SNAPSHOT_PATH=.local/r5-live-CASE/snapshot.json \
MGAR_V3_GITHUB_MCP_BINARY=.local/tools/github-mcp-server/v2.0.3/github-mcp-server \
MGAR_V3_GITHUB_USE_GH_TOKEN=true make dev
```

部署仍需显式启用 `MGAR_V3_ALLOW_PAID=1`；允许业务发布时还需 `MGAR_V3_ALLOW_EXTERNAL_WRITE=1`。它们不能代替私有授权或提案审批。CLI 可用 `mgar repair --execution-mode live --issue NUMBER --question '冻结配置中的准确问题'`；演示仍为默认模式。
