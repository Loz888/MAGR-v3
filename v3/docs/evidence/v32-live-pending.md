# V3.2 live 验收执行记录

日期：2026-11-09
结论：**真实 DeepSeek 与 GitHub 调用已执行**。三个公开仓库成功产出逐字可核验报告；
异常场景逐项执行并留存原始日志。总费用 243,745 micros（约 0.244 USD）。

## 环境与授权

- `.env`：`MGAR_V3_ALLOW_PAID=1`、有效 `MGAR_V3_DEEPSEEK_API_KEY`、
  `MGAR_V3_GITHUB_READ_TOKEN`（账号 `Loz888`，只读，core 4999 / search 30）、
  `MGAR_V3_REPO_ANALYSIS_AUTHORIZATION_PATH=.local/repo-analysis-authorization.json`。
- 授权：`approved_by=Loz888`、`expires_at=2026-12-09T23:59:59+08:00`、
  `allowed_repositories=["*"]`、模型 `deepseek-flash`、上限 10 次模型调用 /
  1,000,000 micros / 120,000 输入 Token / 600 秒。每个新 Run 独立预算行，不复用旧许可、
  不重置账本。
- 服务：`uvicorn mgar_v3.api:create_app --factory --host 127.0.0.1 --port 18081`，
  日志 `docs/evidence/v32-live-server-p4*.log`。
- 账本汇总：`docs/evidence/v32-live-ledger-summary.txt`。

## 三个公开仓库

| # | 仓库 | 任务 | 终态 | 调用 | 费用（micros） | 证据 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `pallets/flask` | 请求进入应用后的处理链路 | `succeeded` | 8 model / 17 tool / 84,694 tokens | 38,452 | `v32-live-flask-report-08.md`、`v32-live-flask-status-08.json` |
| 2 | `encode/httpx` | `Client`→transport 分层与 `_send_single_request` | `succeeded`（第 2 次尝试） | 6 model / 10 tool / 44,031 tokens | 18,423 | `v32-live-httpx-report-02.md`、`v32-live-httpx-status-02.json` |
| 3 | `psf/requests` | `Session.request`→`HTTPAdapter.send` | `succeeded` | 5 model / 8 tool / 35,448 tokens | 12,493 | `v32-live-requests-report.md`、`v32-live-requests-status.json` |

验收判据全部满足：每条关键结论含 `github://owner/repo@<40 位 commit>/path#Lx-Ly`、
固定 Commit 的 blob 链接、Evidence ID 与逐字原文；无 fixture 标记；模型与工具调用
次数非零；报告哈希与证据落库。

第 1 次 httpx 尝试 `c79e8866` 因模型最终 JSON 被 `max_tokens=4096` 截断而
`failed`/`MODEL_INVALID_RESPONSE`；修复提示词与重试策略后第 2 次成功。

## 异常场景执行结果

| 场景 | 构造 | 观测 | 证据 |
| --- | --- | --- | --- |
| 输入 Token 预算 | `--input-tokens 4096` | `waiting_input` / `partial_report` / `INPUT_TOKEN_BUDGET` | `48eb4ed8`，`v32-live-ex-ctxlimit-status.json` |
| 模型调用预算 | `--model-calls 1` | `waiting_input` / `partial_report` / `BUDGET_EXHAUSTED` | `68ffc1e1`，`v32-live-ex-calllimit-status.json` |
| 目标符号不存在 | 要求定位 `NonExistentWidgetFactory` | 报告明确 `insufficient`，列出三次空搜索，不编造调用链 | `5b851571`，`v32-live-ex-symbol-missing-report.md` |
| 仓库不存在 | 不存在的 `owner/repo` | 创建阶段 `REPOSITORY_NOT_FOUND`，未创建 Run | `v32-live-ex-repo-missing.log` |
| 私有仓库 | `Loz888/durable-coding-agent-runtime` | 创建阶段 `REPOSITORY_NOT_PUBLIC` | `v32-live-ex-private.log` |
| 非仓库 URL | `/issues/1` 路径 | `INVALID_INPUT` | `v32-live-ex-url-invalid.log` |
| 固定 Commit 不存在 | 40 个 0 | 创建阶段 `MCP_TOOL_FAILED`（上游拒绝），未创建 Run | `v32-live-ex-bad-commit.log` |
| 文件不存在 | `read_file` 不存在的路径 | `GITHUB_NOT_FOUND`，observation 带上游原文 | `v32-live-ex-file-missing.log` |
| 路径不精确 | 读 `requests/api.py`（真实为 `src/requests/api.py`） | `TOOL_PATH_NOT_EXACT` 并返回候选路径，模型可改路径 | 同上 |
| GitHub 限流 | 连续真实搜索直至配额耗尽 | 第 11 次返回 `GITHUB_RATE_LIMITED`，含 Retry 提示 | `v32-live-ex-rate-limit.log` |
| 提示注入 | README 内含 “ignore previous instructions” | 报告把注入文本作为不可信数据引用，决策未改变 | `3902de1f`，`v32-live-ex-injection-report.md` |
| 暂停 | 运行中 `repo-pause` | Run 进入 `paused`，Epoch 递增 | `v32-live-ex-pause-cmd-03.log` |
| 恢复 | `repo-resume` | 有未结算调用时 `RECOVERY_BLOCKED`；修复后从 checkpoint 继续 | `v32-live-ex-resume-cmd-04.log`、`70445a7b` |
| 取消 | 运行中 `repo-cancel` | `cancelled` | `8c8197c8`，`v32-live-ex-cancel-status.json` |
| 同 Thread 追问 | `repo-message` | 新 Run 固定同一 Commit，父证据 ID 映射保留，10 model 调用成功 | `4f88020a`，`v32-live-ex-followup-report-02.md` |

## 本轮修复（均由真实运行暴露）

1. `read_file` 支持 `start_line`/`end_line`，解决长文件 observation 6,000 字符截断
   导致无法引用目标函数的问题。
2. 无效/截断模型响应最多 3 次同轮重试；重试附紧凑输出约束；失败也记账。
3. 提示词明确 `insufficient` 必须 `report=null`，压缩报告上限以适配 4,096 输出上限。
4. `follow_up`：修正 `mandate_id` 归属查询、dict row 取值、mandate 插入顺序三处缺陷。
5. `resume` 支持 `v32-repo-v1` 图从 checkpoint 恢复。
6. GitHub 错误映射：`does not exist`/`does not point to a file` → `GITHUB_NOT_FOUND`；
   MCP 路径候选提示 → `TOOL_PATH_NOT_EXACT`。
7. 数据库 503 增加服务端异常日志。

## 已知限制

- `deepseek-flash` 在 `max_tokens=4096` 下仍可能返回空白内容或缺失 `action`；
  3 次重试后仍失败时显式 `failed`，不重发、不伪称成功。
- 暂停时长计入 `deadline_at` 墙钟；长时间暂停后恢复会以 `DEADLINE_EXCEEDED` partial 结束。
- GitHub 代码搜索只索引默认分支；证据读取仍按固定 Commit 精确校验。
- 机械核验只保证引用存在、逐字一致、行号在证据范围内，不证明语义正确。
- “上游分支前进导致版本变化”未能真实复现：实验期间 Flask `main` 未前进，追问 Run
  固定了同一 Commit `49943b5a…`；版本固定能力由显式 Commit 校验与错误 Commit 拒绝覆盖。
