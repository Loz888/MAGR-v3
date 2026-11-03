# R6 首个切片：只观察的 Watchdog

R5 已完成限定真实恢复验收。R6 当前交付确定性巡检、恢复请求及显式人工决定；完整 R6 尚未完成，不标 LIVE_READY。本切片不调用模型、不自动启动调查或发布。

## 本机运行

在自己的克隆目录执行，例如 WSL 的 `~/projects/MAGR-v3/v3`，不是云端 `/workspace` 路径。

```bash
git pull --ff-only origin main
make setup
make deps-up
make migrate
make watchdog-setup
```

setup 生成独立私有令牌文件 `.local/watchdog/client.json` 与 `.local/watchdog/server.env`，权限 0600，不显示令牌、不覆盖已有配置。已有文件时保留它们；需要另一套配置可用 `scripts/setup_watchdog.py --directory .local/watchdog-other`。

在服务终端加载专用服务端令牌后启动：

```bash
set -a
. .local/watchdog/server.env
set +a
make dev
```

在另一个同目录终端运行一次巡检：

```bash
make watchdog-once
```

此入口用最小环境启动独立进程，仅从私有客户端配置读取本机 API 地址及受限令牌，不提供数据库、模型或 GitHub 凭据。默认未配置专用令牌时，服务端的 Watchdog 入口关闭。客户端只接受环回 HTTP，不跟随重定向。需要持续巡检时运行：

```bash
env -i PATH="$PATH" LANG=C.UTF-8 "$PWD/.venv/bin/python" -m mgar_v3.recovery.watchdog --config .local/watchdog/client.json
```

每 30 秒观察一次；Ctrl+C 停止。没有异常时 signals/created 为零；重复观察同一事实 created 为零。服务不可用时返回 unavailable、saved=false，不能把未持久化的请求说成已保存。每次最多 256 项，partial=true 代表覆盖不完整。

如果请求已发出但回执丢失，则显示 saved=null、submission_outcome=unknown；它可能已经保存，不能当作零次提交。后续使用同一观察键去重确认，不控制 Run 或重放外部写。

## 审阅和处理

巡检令牌只能读取安全快照和提交建议，不能处理建议。管理员通过现有本机令牌使用相同 API：

```bash
uv run --frozen mgar recovery-requests
uv run --frozen mgar recovery-decision REQUEST_ID --decision wait
uv run --frozen mgar recovery-decision REQUEST_ID --decision escalate
uv run --frozen mgar recovery-decision REQUEST_ID --decision pause
uv run --frozen mgar recovery-decision REQUEST_ID --decision cancel
```

这些决定由人显式选择，不是建议逐条执行。事实或 epoch 已变化时旧请求变为 stale，不控制 Run；unknown 只允许 wait/escalate。暂停/取消沿用 Engine 权限和事务路径，旧 Worker 提交会被拒绝，不会重新发送未知远端写。

API：受限 `GET /api/v3/watchdog/snapshot`、`POST /api/v3/watchdog/recovery-requests`；管理 `GET /api/v3/recovery-requests`、`POST /api/v3/recovery-requests/{id}/decision`。

## 验收和剩余工作

```bash
make lint
make typecheck
make verify TASK=V31-R6-01
```

首个切片注册验收 82 项通过，0 失败/错误/跳过，含 16 项新检查；真实 PG、独立进程、HTTP、限权、去重、人工 fencing、事务回滚及服务不可用均覆盖。完整 R6 仍待：独立心跳与队列/依赖健康、受控主动策略、只读投机及独立预算、完整 A/B/C 对照和部署故障矩阵。累计模型费用保持 US$0.028549，没有新增调用授权。

见 [工程摘要](acceptance/r6-observe-engineering-20261103.json) 和 [ADR 0011](adr/0011-observe-only-watchdog.md)。
