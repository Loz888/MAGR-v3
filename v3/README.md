# V3.1 本地执行

当前源码存放于 `/workspace/MAGR-v3/v3`，仓库 origin 为 `https://github.com/Loz888/MAGR-v3.git`。
本地 `.env`、`.local`、依赖环境与凭据不进入 Git；提交与推送状态以 Git 历史和最新验收记录为准。

R0/R1 工程门禁 PASS：88 个 V3 测试，0 失败、0 跳过；V2 原有 682 个 Go 和 453 个 Python 测试复验通过。
R1 真实模型与 GitHub 结果评论验收 PASS：2 次真实调用，保守计费 USD 0.001745，一条结果评论。首个结构错误响应未发布；第二次成功，并通过模型落库及远程回执两个 SIGKILL 恢复实验。详情见 `docs/acceptance/r1-live-20261102.json`。完整 V3.1 仍为 ENGINEERING_INCOMPLETE。

在本目录执行 `make setup`、`make deps-up`、`make migrate`、`make doctor`，再运行 `make dev`。
应用监听 `http://127.0.0.1:18080`；开发 API 文档位于 `/docs`。
`make setup` 只生成缺失的本地 `.env`（权限 0600）；不会覆盖已有配置或显示凭据。
数据库调试端口 55433，Redis 调试端口 56379，均仅绑定回环。停止依赖用 `make deps-stop`，保留数据卷。

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
新评论会持久保存；R1 不静默纳入新输入，输入版本变化会阻止发布。显式 intake/replan 属于 R2。

## 真实执行准备

真实模式需要冻结的 live profile 与匹配的本地 authorization，及环境开关的字面值 `1`。
费用采用整数微美元、PG 原子预占与结算；次数和费用在同一 profile 下跨 Run 累计。当前候选最多 3 次调用、合计不超过 USD 2；任一上限先耗尽即停止。未知用量保留预占和调用次数，不自动重发。
已知的结构化模型结果与费用结算同事务保存；崩溃后复用账本和 checkpoint，恢复时重新核验证据，旧 epoch 不能借用新执行者权限。
读取和写入分别配置 `MGAR_V3_GITHUB_READ_TOKEN`、`MGAR_V3_GITHUB_WRITE_TOKEN`，只在本机环境中设置；不要粘贴到聊天或提交到 Git。
聊天中已连接的 GitHub 插件不会自动给独立运行的应用提供凭据。
运行时加载 `MGAR_V3_LIVE_PROFILE_PATH` 与 `MGAR_V3_LIVE_AUTHORIZATION_PATH`。普通 API 客户端不能提交自己的授权对象。
所有发布仍需要对具体 Action 的 payload hash 审批；输入变化、取消、过期写权或未知结果不会被 resume 绕过。

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
```

verify 在本地 v3/evidence/ 保存原始输出、真实退出码、JUnit 与源代码摘要；这些原始文件不纳入公开推送，公开验收摘要见 docs/。未知任务、缺失测试、零收集、跳过和超时均不会通过。
工程测试使用真实 PostgreSQL、Redis、LangGraph checkpoint 和跨进程 MCP；聊天模型和外部 GitHub 业务使用明确标记的替身。
R1 真实闭环通过后开始 R2，R3–R6 保持 TODO。

官方 GitHub MCP 可通过 `make install-github-mcp` 安装锁定的 Linux x86_64 二进制；安装和每次运行都会校验 SHA256。设置 `MGAR_V3_GITHUB_MCP_BINARY` 指向该文件。只读进程仅暴露 `issue_read,get_file_contents`，写入进程仅暴露 `add_issue_comment`。显式设置 `MGAR_V3_GITHUB_USE_GH_TOKEN=true` 可引用宿主机已有 GitHub 凭据；分别配置的读写 token 优先，不把模型或数据库凭据交给 MCP 子进程。

`scripts/r1_live.py` 是操作员分阶段实验工具，要求已批准的授权文件；`model-crash` 需要显式 `--execute-paid`，`send-crash` 需要操作员检查完整 `proposal.md` 后提供准确的 `--approved-payload-hash`。两个阶段故意 SIGKILL，分别用 `resume` 和 `reconcile` 恢复。每次实际调用都记入 profile 共享账本；不要重复执行已完成的真实实验或修改授权来清零预算。
