# V3.5 90 秒演示脚本

- 状态：D01（2026-11-10）
- 目标：用一条真实链路说明 V3.5 不是一次性 Prompt：自然语言任务 → 固定 Commit 只读调查 → Finding/Evidence → 有授权修复 → 测试回执 → Review Bundle → 受控 Draft PR。
- 前提：已完成 `make setup`、`make deps-up`、`make migrate`、`make doctor`；Docker 可用。全部命令在 `v3/` 执行，默认零付费调用、零 GitHub 写入。

## 分镜

### 0:00–0:10 任务与边界

展示 `v3/docs/v35/PRODUCT.md` 第 1 节与终端：

```bash
make doctor
```

口播：PostgreSQL/Redis 是真实依赖；`live` 一栏是 `NOT_RUN`，因为演示不发起付费调用。下面每个成功声明都能在报告里找到证据。

### 0:10–0:20 示例仓库（固定 Commit，不联网）

```bash
make demo-repo
```

生成 `.local/demo-v35/example-repo`（`pallets/click` 固定 Commit 的字节级快照）与 `DEFECT.md`。脚本校验快照 SHA-256，只复制文件，不调用模型、不访问网络。

缺陷不是伪造的：固定 Commit 上真实存在 “参数名 `help` 与自动 help option 存储名冲突” 的缺陷，公开测试可复现。

### 0:20–0:55 调查 → 修复 → 测试回执 → Review Bundle

```bash
make demo
```

`scripts/run_eval.py` 驱动真实链路，依次发生：

1. 读取固定 Commit 快照，生成 `RepairHandoff`（含证据、复现回执、授权、允许路径、预算）。
2. 在固定 R03 Docker 镜像（`network none`、非 root）中运行基线与公开测试，必须得到 `test_failed` 才算复现。
3. Provider 返回结构化 `PatchPlan`（不允许 Shell）；R02 `PatchWorktree` 校验路径、preimage SHA 与补丁限额。
4. 第一次补丁只改一半，公开测试失败；失败回执进入下一轮，第二次补丁通过公开测试。
5. 独立隐藏验证通过；生成 sealed Review Bundle（diff、tree、测试回执、隐藏结果哈希），任务停在 `waiting_approval`。
6. 评测明确禁止发布：`publication.executed=false`、`correct=true`。

### 0:55–1:15 证据在哪里

终端会打印 `gate/live_agent_gate/executed/passed`。报告目录形如：

```text
.local/demo-v35/report-<UTC>/
  results.json                 # 任务级结果与失败原因
  summary.json                 # 聚合、成本、延迟、score_signature
  tasks/v35-adversarial-help-name-retry/
    ledger/                    # 每次尝试的检查点与产物
    resume-probe/              # 中断恢复：provider_calls=0，未知外部结果 fail-closed
```

口播要点：

- `attempts=2`、`usage.model_calls=2`、只改 `src/click/core.py`、0 无关/越界变更。
- 首轮失败被保留，没有被最终成功覆盖。
- 中断恢复复制检查点后 `provider_calls=0`；对“调用在途时进程中断”返回 `REPAIR_UNKNOWN_PROVIDER_OUTCOME` 并停止，宁可不对账也不重复调用。
- `gate=engineering_gate`，`live_agent_gate=NOT_RUN`；fixture 不冒充 live。
- `score_signature` 只覆盖决策字段，重跑可复核。

### 1:15–1:30 受控 Draft PR（可选分支）

真实 GitHub 写入在演示中不执行（`live_github_write_gate=NOT_RUN`）。受控发布用本地 fixture MCP 远端演示，链路与真实发布相同（Action → Approval(payload_hash) → Outbox → Dispatcher → 分支/提交/Draft PR 三步回执）：

```bash
make sandbox-build
uv run --frozen python scripts/r5_demo.py            # 创建并执行修复 Run，打印 action payload_hash
uv run --frozen python scripts/r5_demo.py --run <run_id> --approve-payload-hash <hash>
```

口播：审批必须绑定完整 payload 的准确 hash；回执来自 `fixture://` 远端，页面不会渲染成 GitHub 链接；未知结果只读对账，不重放。真实 Draft PR 需要独立写授权与审批，当前记录为 `NOT_RUN`。

## Web 入口（同样的链路）

```bash
make dev
```

打开 `http://127.0.0.1:18080/workbench`，用 `v3/.env` 的 `MGAR_V3_LOCAL_TOKEN` 认证：

1. 选择“只调查”或“允许尝试修复”，填写仓库 URL 与自然语言任务。
2. 任务页查看固定 Commit、扫描范围、Finding 证据、时间线与 `usage/failure/pending`。
3. 点击“加载完整 Diff”，核对 `payload_hash` 后才可审批；审批提交的就是已审查的 hash。
4. fixture 发布只显示 fixture 回执，不显示 GitHub 链接。

CLI 与 Web 调同一 API，可用 `mgar workbench --action status --task-id <id>` 交叉验证同一视图。

## 演示边界（必须口播）

- 本演示不调用付费模型、不写真实 GitHub；`live_model_gate` / `live_github_write_gate` / `live_agent_gate` 均为 `NOT_RUN`。
- E02 的真实 live 四臂已执行并失败（`PATCH_TRUNCATED`），保留为 `FAIL/INCONCLUSIVE`，不用本演示替代。
- Holdout 未在开发路径执行；真实 PostgreSQL 服务重启、真实 GitHub Draft PR 写入仍是 `NOT_RUN`。
- 任何 `NOT_RUN` 都不得改写为 `PASS`；工程门通过不等于真实 Agent 能力通过。

## 失败排查

| 现象 | 处理 |
| --- | --- |
| `make demo` 报 Docker 不可用 | 恢复 Docker Desktop/WSL 集成，`docker info` 成功后重试 |
| 报告目录已存在 | 报告不可覆盖；删除旧 `.local/demo-v35/report-*` 或换时间戳 |
| `make demo-repo` 报 digest 不匹配 | 检查工作区快照是否被修改；不要手改 `experiments/v35-harness` 资产 |
| Web 401 | 从 `v3/.env` 重新读取 `MGAR_V3_LOCAL_TOKEN`，不要发到聊天 |
| fixture 发布没有 GitHub 链接 | 预期行为；只有 `remote=github` 的回执才有链接 |
