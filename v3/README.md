# V3.1 本地执行

仓库地址为 `https://github.com/Loz888/MAGR-v3.git`。运行命令需要在你本机克隆后的 `MAGR-v3/v3/` 目录执行。
`/workspace/MAGR-v3/v3` 是开发云环境中的路径，在你的 Windows/WSL 上不会自动存在。
本地 `.env`、`.local`、依赖环境与凭据不进入 Git；提交与推送状态以 Git 历史和最新验收记录为准。

R0/R1 验收时 88 个 V3 测试通过；R2 改造后 112 项通过，R3 改造后完整回归 150 项通过，0 失败、0 跳过。V2 原有 682 个 Go 和 453 个 Python 测试复验通过，V2 源码保持不变。
R1 真实模型与 GitHub 结果评论验收 PASS：2 次真实调用，保守计费 USD 0.001745，一条结果评论。首个结构错误响应未发布；第二次成功，并通过模型落库及远程回执两个 SIGKILL 恢复实验。详情见 `docs/acceptance/r1-live-20261102.json`。完整 V3.1 仍为 ENGINEERING_INCOMPLETE。

在本目录执行 `make setup`、`make deps-up`、`make migrate`、`make doctor`，再运行 `make dev`。
应用监听 `http://127.0.0.1:18080`；开发 API 文档位于 `/docs`。
该地址现在提供本地任务工作台，Web、CLI、GitHub 共用工单视图；访问令牌取自本机 `.env` 的 `MGAR_V3_LOCAL_TOKEN`，页面只在内存中使用令牌。
`make setup` 只生成缺失的本地 `.env`（权限 0600）；不会覆盖已有配置或显示凭据。
数据库调试端口 55433，Redis 调试端口 56379，均仅绑定回环。停止依赖用 `make deps-stop`，保留数据卷。

## WSL2 首次启动

在 Windows 上安装并启动 Docker Desktop，在 Settings → Resources → WSL Integration 中启用当前 WSL 发行版；已有可用的 WSL Docker Engine + Compose V2 也可以使用。

在 Ubuntu/WSL 终端安装本机工具和 Python 3.12：

```bash
(
set -e
sudo apt-get update
sudo apt-get install -y git make curl ca-certificates
export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi
uv python install 3.12
)
export PATH="$HOME/.local/bin:$PATH"
```

项目的 Makefile 禁止自动下载 Python，因此首次 `make setup` 之前需要完成上面的 Python 安装。然后克隆、安装依赖并启动；下面的子 shell 在任一步失败时停止，避免 `cd` 失败后在其他目录继续执行：

```bash
(
set -e
mkdir -p "$HOME/projects"
if [ ! -d "$HOME/projects/MAGR-v3/.git" ]; then
    git clone https://github.com/Loz888/MAGR-v3.git "$HOME/projects/MAGR-v3"
fi
cd "$HOME/projects/MAGR-v3"
git pull --ff-only
cd v3
docker info >/dev/null
docker compose version
make setup
make deps-up
make migrate
make doctor
make dev
)
```

已在其他目录克隆的用户可进入自己的仓库根目录更新后执行同样的 `v3/` 启动步骤；无需重新克隆或覆盖已有 `.env`。如果 `docker info` 或 `docker compose version` 失败，先恢复 Docker Desktop/WSL 集成，再继续。

服务启动后保持该终端运行，在 Windows 浏览器打开 `http://127.0.0.1:18080`。工作台访问令牌位于本机 `v3/.env` 的 `MGAR_V3_LOCAL_TOKEN`，不要把令牌发到聊天中。默认演示模式不调用付费模型或向 GitHub 发布。

## fixture 示例

```bash
uv run --frozen mgar run --project orders-demo --issue 1 --question '总结来源中记录的症状'
uv run --frozen mgar status <run_id>
uv run --frozen mgar report <run_id>
uv run --frozen mgar actions <run_id>
uv run --frozen mgar approve <run_id> --action <action_id> --payload-hash <preview_hash>
uv run --frozen mgar watch <run_id>
```

CLI 与 API 共用 Engine。fixture 调查读取合成资料，引用由实际读取、内容哈希和定位检查支持；它不调用聊天模型，不证明根因正确性。
默认没有配置外部 Dispatcher，因此批准的 fixture 动作保留在 outbox，不会发往 GitHub。
新 Run 默认使用 R2 图，在节点边界通过显式 intake/replan 纳入后续评论；已有 R1 Run 保留原图版本。原始 question 保持不变，后续上下文写入不可变输入快照。输入变化会使未发送的旧提案失效，新的结果需要新的 hash 审批；已经发出的动作先等待结果或对账。

```bash
uv run --frozen mgar message <run_id> --body '补充事实与需要核对的内容' --client-request-id followup-1
uv run --frozen mgar pause <run_id>
uv run --frozen mgar resume <run_id>
uv run --frozen mgar cancel <run_id>
uv run --frozen mgar watch <run_id>
```

重复发送同一 client-request-id 和相同内容会返回原回执。暂停/取消先写控制状态，执行在节点边界响应，迟到结果不能恢复已取消的 Run。只读长任务通过持久 job 在线程中运行，完成后原子写入 tool_result 事件和 Mailbox；API 与控制循环保持可用。暂停恢复继续同一 R2 Run，未知模型用量或未知外部写会阻止恢复。

Mailbox 最多 64 条待消费消息、64 KiB，为在途工具预留完成事件空间；快照最多 64 KiB。每个 Run 最多三个不同用户输入版本（初始加两次 replan），每个付费版本有稳定 step key，仍受 profile 总费用和次数限制。七天未消费的过期输入会明确阻塞并保留；超出上下文或 replan 限制也进入 waiting_input，不静默丢弃或自动扩大预算。

## 真实执行准备

真实模式需要冻结的 live profile 与匹配的本地 authorization，及环境开关的字面值 `1`。
费用采用整数微美元、PG 原子预占与结算；次数和费用在同一 profile 下跨 Run 累计。当前候选最多 3 次调用、合计不超过 USD 2；任一上限先耗尽即停止。未知用量保留预占和调用次数，不自动重发。
已知的结构化模型结果与费用结算同事务保存；崩溃后复用账本和 checkpoint，恢复时重新核验证据，旧 epoch 不能借用新执行者权限。
读取和写入分别配置 `MGAR_V3_GITHUB_READ_TOKEN`、`MGAR_V3_GITHUB_WRITE_TOKEN`，只在本机环境中设置；不要粘贴到聊天或提交到 Git。
聊天中已连接的 GitHub 插件不会自动给独立运行的应用提供凭据。
运行时加载 `MGAR_V3_LIVE_PROFILE_PATH` 与 `MGAR_V3_LIVE_AUTHORIZATION_PATH`。普通 API 客户端不能提交自己的授权对象。
所有发布仍需要对具体 Action 的 payload hash 审批；输入变化、取消、过期写权或未知结果不会被 resume 绕过。每个 profile 默认最多一条结果评论，在途和 unknown 占用额度；R1 的额度已经使用。提高 `max_result_comments`（最多三条）会改变 profile 哈希，需要新的匹配授权；不能用 R2 追加输入绕过发布次数限制。

真实配置在 `config/live-r1-proposed.json`，来源为 Issue #1 冻结的标题/正文。运行 `make live-preflight PROFILE=config/live-r1-proposed.json` 只检查配置、不发模型请求；未提供本地授权时退出 2，不影响已经记录的历史验收结果。
2026-11-02 用户将候选范围调整为最多 3 次 `deepseek-flash` 调用，总预算上限 USD 2，最多向 #1 发布一条经审批的结果评论。单个 Run 仍只有一个模型步骤，已知结果复用不额外调用；修改预算不会自动执行或增加发布次数。
扩容次数与总预算都绑定 profile 哈希。数据库按配置保存次数上限；原有 1 次配置保留兼容哈希和限制，扩容后的候选需要匹配新哈希的运行授权配置。
模型目录已验证实际模型名为 `deepseek-flash`；运行环境原有 `deep-seek-flash` 别名不在目录中。候选配置采用实际模型名，未擅自改写运行环境。
开关只接受字面值 `1`；其他环境值关闭权限。环境变量优先于 .env，若运行环境中已有其他值，需要在环境配置中更正。

真实启动顺序：在本机环境配置 GitHub 两类凭据和已批准的本地授权文件 → live-preflight → make dev → 用 API/CLI 提交与候选配置一致的问题（CLI 加 `--execution-mode live`）→ 预览完整 Action → 对准确 hash 审批。
真实问题文本见候选 JSON 的 question 字段。授权文件必须匹配 preflight 输出的 profile_hash、配置的本机 actor、明确 UTC 有效期和实际获准的操作；不要由模型或脚本替用户批准。

## 验证

```bash
make lint
make typecheck
make verify TASK=V31-R0-02
make verify TASK=V31-R0-03
make verify TASK=V31-R1-01
make verify TASK=V31-R2
```

verify 在本地 v3/evidence/ 保存原始输出、真实退出码、JUnit 与源代码摘要；这些原始文件不纳入公开推送，公开验收摘要见 docs/。未知任务、缺失测试、零收集、跳过和超时均不会通过。
工程测试使用真实 PostgreSQL、Redis、LangGraph checkpoint 和跨进程 MCP；聊天模型和外部 GitHub 业务使用明确标记的替身。
R1 真实闭环通过后依次实施并验证 R2、R3 工程，R2 摘要见 `docs/acceptance/r2-engineering-20261102.json`。R2 未额外做真实付费业务评测，R3 仅限定真实 Router 验收；R4 工程已继续实施，范围与门禁见下方；R5/R6 保持 TODO，不声明生产就绪。

## R3 Router 与统一工单

```bash
uv run --frozen mgar event --issue 2 --body '@mgar 调查来源中记录的重复计数症状'
uv run --frozen mgar tickets
uv run --frozen mgar ticket <ticket_id>
uv run --frozen mgar routing
make verify TASK=V31-R3
```

显式 `/mgar investigate` 命令继续直建任务；已有 primary 的后续输入直达 Mailbox，Router 调用为零。冷启动的明确提及进入持久队列，有界批处理生成 RouterDecision，由 Engine 校验整批版本、目标 epoch、Mandate、权限和上下文哈希后提交。多任务默认两个并发执行槽；跨任务最多召回三个同项目、仓库、actor 和模式的近期工单上下文，链接只提供背景，不更改其他任务的计划或写权。observer 仅内部测试开放。

默认 Router 是确定性的演示实现，Worker 仍读取合成资料。真实 Router 配置在 `config/router-live-proposed.json`，必须单独配置 `MGAR_V3_ROUTER_MODE=live`、Router profile 和授权文件；授权不会从网页或普通 API 请求取得。Router 只输出动作枚举与候选 ID，不持有 Bash、MCP 或发布工具。

系统澄清使用程序模板，生成同样需要 hash 审批的 Action，经现有 Outbox/Dispatcher 出口；回复后等待用户输入，补充事实可在同一 Ticket 内继续。Router 故障或调用额度用尽时，既有 Run 与订阅直通继续，未路由输入保留。unknown 用量不重发，已知失败的同版本输入不自动消耗新调用；已知结果在恢复时复用，相同业务上下文下可重建过期的决策信封。

路由纠正接口为 `POST /api/v3/router/corrections`，需本机认证、完整原决策事件 ID、准确的 `expected_thread_revision` 和原因。只重开尚未产生工具/模型结果或远程动作的任务；原始事件、订阅快照和旧 Ticket 保留，旧待发送动作撤销，再记录纠正和新 revision。

R3 真实 Router 验收使用冻结的合成事件，详情见 `docs/acceptance/r3-20261102.json`。只增加一次真实调用，共享原 R1 预算后累计三次、保守计费 USD 0.002456；没有新增 GitHub 评论。合成测试 Run 在校验后取消，原始实验、固定 Oracle 和授权依据保存在 `.local/r3-live-20261102/`，运行日志在本地 `evidence/V31-R3/`。`scripts/r3_live.py` 的 prepare/model-crash/recover 分阶段保留恢复边界，model-crash 故意 SIGKILL。该实验不能清零既有调用额度，也不代表真实业务全流程或生产评测完成。

官方 GitHub MCP 可通过 `make install-github-mcp` 安装锁定的 Linux x86_64 二进制；安装和每次运行都会校验 SHA256。设置 `MGAR_V3_GITHUB_MCP_BINARY` 指向该文件。只读进程仅暴露 `issue_read,get_file_contents`，写入进程仅暴露 `add_issue_comment`。显式设置 `MGAR_V3_GITHUB_USE_GH_TOKEN=true` 可引用宿主机已有 GitHub 凭据；分别配置的读写 token 优先，不把模型或数据库凭据交给 MCP 子进程。

`scripts/r1_live.py` 是操作员分阶段实验工具，要求已批准的授权文件；`model-crash` 需要显式 `--execute-paid`，`send-crash` 需要操作员检查完整 `proposal.md` 后提供准确的 `--approved-payload-hash`。两个阶段故意 SIGKILL，分别用 `resume` 和 `reconcile` 恢复。每次实际调用都记入 profile 共享账本；不要重复执行已完成的真实实验或修改授权来清零预算。


## R4 知识、专家与摘要

R4 完整回归 187 项、专项验收 103 项通过，0 失败、0 跳过。新增混合检索、不可变知识快照、Knowledge/Repo/Data 并行专家、独立证据核验、真实 HTTP MCP、独立只读 SQL 角色、ContextEdge/Spill 与显式确认摘要。旧图和授权哈希兼容保留。

更新、安装本地中文模型、准备合成资料与启动命令统一见 [R4 使用说明](docs/r4-runbook.md)。默认仍为协议演示；本地模型真实检索、工程测试和完整付费业务 live 分开记录，详见 [R4 验收摘要](docs/acceptance/r4-20261102.json)。首批 R4 工程交付未新增付费调用；后续真实实验见下。


## R4 真实跨源验收

已执行三正常、两负例及单 Agent 对照，实际使用 DeepSeek、GitHub 代码、知识检索和 SQL。首轮双方各 4/5；一次有界恢复后，多专家五例均有通过记录，首轮失败保留。本轮新增 23 次调用、保守 USD 0.024569，累计 USD 0.027025。完整回归 196 项、R4 专项 119 项通过。详细结果及限制见 [真实验收报告](docs/r4-live-report.md)；live 标记 PARTIAL，R5 受控工程进展见下方；R6 尚未开始。


## R5 受限修复工程

新增真实隔离 Go 测试、精确 diff/AST 约束、独立隐藏测试、完整补丁审批、编辑后重测，以及分支/提交/Draft PR 三步的持久回执和只读恢复。当前源码、补丁提供者及 MCP 发布远端明确为 fixture；真实模型和 GitHub Draft PR 仍未验收，R5 整体在进行中，R6 保持 TODO。

准备 `make sandbox-build` 后，可用 `make r5-demo` 或 Web/CLI 的 repair 入口。命令、前提和本地输出位置见 [R5 使用说明](docs/r5-runbook.md)，边界见 [ADR 0007](docs/adr/0007-r5-repair-boundaries.md)。

受控工程验收覆盖完整回归 229 项，0 失败、0 跳过；四个真实 SIGKILL 点通过。[公开验收摘要](docs/acceptance/r5-engineering-20261102.json) 单列了工程范围、未完成适配和费用。


## R5 真实适配补全

现已补上 GitHub 固定提交及完整 tree/blob 证明、真实 DeepSeek 补丁适配、跨阶段累计费用约束、官方 MCP Draft PR、部分成功只读对账与显式继续。Web/CLI/API 也支持专用真实配置、完整提案审批及编辑重测。完整回归 245 项，0 失败、0 跳过；包含六个强制终止恢复案例。

本轮工程验收中的模型和写入远端使用受控服务，另有实际 GitHub/MCP 只读源码探测。未新增付费调用或业务 PR；真实验收需要新冻结配置及许可，R5 整体暂为 IN_PROGRESS，R6 保持 TODO。详见 [适配验收](docs/acceptance/r5-native-engineering-20261102.json)、[ADR 0008](docs/adr/0008-r5-native-adapters.md) 和 [运行说明](docs/r5-runbook.md)。
