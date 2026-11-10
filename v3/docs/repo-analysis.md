# V3.2 通用 GitHub 仓库分析（repository_analysis）

> 网页入口现已统一为 [单页 Agent](agent-ui.md)。首页 `/`、`/repo` 和
> `/workbench` 均进入对话式分析页。本文的授权与 API/CLI 契约继续有效；
> 网页现支持账户密码登录，并可直接填写模型、GitHub 凭据及使用许可。
> 下文文件配置仍供 CLI/旧操作员接口使用；网页操作以单页使用说明为准。

本模块把 RepoSage 从固定电商 Case 扩展为通用、只读的 GitHub 仓库分析 Agent：
用户给出 `https://github.com/<owner>/<repo>` 与自然语言任务，Agent 在固定 Commit
上自行决定搜索、读目录、读文件、看 Issue/PR，并输出每条结论都可回溯到源码的
Markdown 报告。

## 1. 能力边界

- 只读：GitHub 工具组仅为 `get_commit`、`get_file_contents`、`search_code`、
  `list_branches`、`search_repositories`、`issue_read`、`list_issues`、
  `pull_request_read`、`list_pull_requests`。没有写文件、推送、合并或发布工具。
- 固定标识：后端只接受 HTTPS `github.com/owner/repo`（可带 `/tree/<ref>`、
  `/commit/<sha>`）。Issue、PR、blob、任意 host、端口、凭证、query、fragment 一律拒绝；
  HTTP、Shell 下载器都不会接触该输入。
- 固定版本：创建任务时解析并固定 40 位 Commit；每轮工具调用都校验 Repository、
  Read Scope、Run Epoch 与预算。
- 不可信数据：README、代码、Issue、PR、评论中的任何指令都只作为数据，不能改变
  系统提示、权限、仓库或预算。
- 默认自动执行：网页许可不限制模型轮数或工具次数，在输入/输出 Token 总预算、
  费用与期限内调查，源码逐字核验及独立问题覆盖检查不通过时反馈继续调查。
  重复读取、连续无新信息的搜索与未改进的答案由后台防循环策略限制。
- 预算失败不冒充成功：Token、费用或总期限耗尽（旧许可还包含调用次数上限）时，
  返回 `waiting_input` + `partial_report`，以 `passed=false` 记录已得证据与未完成项。

## 2. 本机配置

```bash
cd v3
make setup            # 仅生成缺失的 .env / 数据卷
make deps-up
make migrate          # 包含账户、流式分析和 Token 预占迁移，当前至 0029
make doctor
```

在 `v3/.env` 中启用 live：

```dotenv
MGAR_V3_ALLOW_PAID=1
MGAR_V3_DEEPSEEK_API_KEY=<你的 DeepSeek API Key>
MGAR_V3_GITHUB_READ_TOKEN=<GitHub fine-grained read-only token>
# 或固定官方 MCP 可执行文件；两者至少一个
# MGAR_V3_GITHUB_MCP_BINARY=/absolute/path/to/github-mcp-server
MGAR_V3_REPO_ANALYSIS_AUTHORIZATION_PATH=.local/repo-analysis-authorization.json
```

授权文件从示例复制并修改：`config/repo-analysis-authorization.example.json`。
`approved_by` 必须是小写化的 `MGAR_V3_ALLOWED_ACTORS` 成员；`expires_at` 必须带时区；
仓库白名单可用 `"*"` 表示任意公开仓库。真实调用不会复用过期许可，也不重置费用账本：
每次新任务都会在 `app.authorization_budgets` 下建立自己的预算行。

GitHub token 只需要 `Contents`、`Issues`、`Pull requests` 的只读权限；
公开仓库也需要 token 以遵守 API 限流。不要授予写权限。

Windows PowerShell 与 WSL2 的启动命令相同（在 `v3/` 目录内）：

```powershell
make deps-up; make migrate; make dev
# 或直接：
uv run --frozen uvicorn mgar_v3.api:create_app --factory --host 127.0.0.1 --port 18080
```

WSL2 内若 `make` 目标因 Docker Desktop 集成不可用，可按顺序手工执行
`docker compose up -d`、`uv run --frozen alembic upgrade head`，再启动上面的 uvicorn 命令。

Web 页面：`http://127.0.0.1:18080/repo`（首页 `/` 为同一入口，原工作台在 `/legacy`）。
API 文档：`http://127.0.0.1:18080/docs`。

## 3. Web 使用

1. 打开 `/repo`，首次创建本机管理员账户，以后用账户密码登录。
2. 在模型设置弹窗填写凭据、仓库范围、费用与 Token 预算，并确认使用许可。
3. 填写 Repository URL 与自然语言任务；缺配置时可直接“保存并开始分析”。
4. 提交后实时显示调查阶段、工具活动及尚未核验的报告草稿，完成后显示引用与正式报告。
5. 可在同一 Thread 追问；父 Run 的已验证证据会复制到新 Run 并保留 ID 映射。

网页通过 HttpOnly Cookie 登录。CLI 和 `/legacy/repo` 保留 `X-MGAR-Token` 兼容方式。
浏览器 SSE 断开后按事件 ID 重连；刷新时从持久化事件恢复草稿。

## 4. CLI

```bash
uv run --frozen mgar repo-profile
uv run --frozen mgar repo-analyze \
  --repo-url https://github.com/pallets/flask \
  --task '说明请求进入 Flask 应用后的处理链路，指出入口文件与关键调用关系' \
  --execution-mode live
uv run --frozen mgar repo-list
uv run --frozen mgar repo-status <run_id>
uv run --frozen mgar repo-report <run_id>
uv run --frozen mgar repo-message <run_id> --body '继续说明 Blueprint 注册发生在哪里'
uv run --frozen mgar repo-pause <run_id>
uv run --frozen mgar repo-resume <run_id>
uv run --frozen mgar repo-cancel <run_id>
```

未指定预算参数时继承当前许可。推荐在网页保存自动许可；高级文件许可可把
`max_model_calls` 和 `max_tool_calls` 设为 `null`，并设置 `max_total_tokens`。
CLI 可用 `--total-tokens` 缩小总预算；旧的 `--model-calls`、`--tool-calls` 等参数
继续支持，但请求不能扩大许可范围。历史固定次数许可不会被静默改为自动模式。

`fixture` 模式值 `--execution-mode fixture` 只用于工程测试：它使用合成仓库，
绝不访问 GitHub 或 DeepSeek，报告会明确标注 fixture。

## 5. API 与 Schema

所有端点要求有效账户会话或 `X-MGAR-Token`；账户会话的写请求还要求 CSRF。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v3/repository-analyses/profile` | 授权、模型、预算与读取凭证状态 |
| POST | `/api/v3/repository-analyses` | 创建任务，202 + `AnalysisSpec` |
| GET | `/api/v3/repository-analyses` | 最近任务列表 |
| GET | `/api/v3/repository-analyses/{run_id}` | 状态、事件、工具调用、Claim、报告 |
| GET | `/api/v3/repository-analyses/{run_id}/events` | SSE 事件流，支持 `Last-Event-ID` / `after` 游标 |
| GET | `/api/v3/repository-analyses/{run_id}/report` | `text/markdown` 报告 |
| POST | `/api/v3/repository-analyses/{run_id}/messages` | 同 Thread 追问，202 + 新 `AnalysisSpec` |
| POST | `/api/v3/repository-analyses/{run_id}/pause` | 暂停（Epoch 递增） |
| POST | `/api/v3/repository-analyses/{run_id}/resume` | 从 LangGraph checkpoint 恢复 |
| POST | `/api/v3/repository-analyses/{run_id}/cancel` | 取消 |

创建请求：

```json
{
  "repository_url": "https://github.com/owner/repo",
  "task": "自然语言问题",
  "ref": "main",
  "commit": null,
  "execution_mode": "live",
  "client_request_id": "idempotency-key",
  "limits": {
    "max_model_calls": null,
    "max_tool_calls": null,
    "max_input_tokens": 500000,
    "max_total_tokens": 500000,
    "max_cost_micros": 500000,
    "max_seconds": 3600
  }
}
```

`limits` 可以整体省略，或只填写要缩小的字段，其余继承许可。总 Token 包含每次
调用的输入及输出，请求发送前以事务预占，收到完整用量回执后结算。
`events` 发送 `update`（阶段、工具活动、`answer_delta` 等）和 `done` 事件；
只流式展示报告的公开正文，不传输模型的内部推理。

工具 Schema（服务端注入 Repository/Commit，模型不能传）：

| tool | 参数 |
| --- | --- |
| `repository_metadata` | 无 |
| `list_directory` | `path`（可为空） |
| `read_file` | `path`、可选 `start_line`/`end_line`（1-based 闭区间） |
| `search_code` | `query`、`limit`（1–30） |
| `read_issue` | `number` |
| `read_pull_request` | `number` |

模型每轮必须返回一个 JSON 对象（DeepSeek JSON mode + Pydantic 严格校验）：

```json
{
  "action": "tool | final | insufficient",
  "thought": "决策理由",
  "tool_calls": [],
  "report": {
    "conclusion": "问题结论",
    "structure": "仓库总体结构",
    "key_files": [{"statement": "结论", "evidence_id": "uuid", "quote": "源码原文"}],
    "call_chains": [],
    "findings": [],
    "uncertainties": ["不确定的结论"],
    "next_steps": ["建议继续调查的方向"]
  },
  "insufficient_reason": null
}
```

`key_files` / `findings` / `call_chains` 的每条 Claim 都必须引用本次 Run 内存在的
`evidence_id` 且 `quote` 与 CAS 原文逐字一致、行号落在证据范围；否则报告判定
`passed=false`。自动模式把缺失项反馈继续调查；无进展或预算不足时留下未完成报告，
不会把未通过的引用当成已核验答案。旧固定次数模式维持原失败行为。

## 6. 报告与证据

报告固定六节：问题结论、仓库总体结构、关键代码文件与调用关系、实际代码证据、
不确定的结论、建议继续调查的方向。每条关键源码结论包含：

- `github://<owner>/<repo>@<40 位 Commit>/<path>#L<start>-L<end>`
- GitHub blob 永久链接（固定 Commit）
- Evidence ID、Git Blob SHA / 内容哈希、逐字原文

报告页脚声明：以上仅为机械证据核验，不等于语义正确。

## 7. 状态与恢复

- `queued → running → succeeded`（`outcome=internal_report`）
- 预算/上下文/期限耗尽：`waiting_input`（`outcome=partial_report`，`passed=false`）
- 已确认的连接失败、429 或结构化格式错误：后台有界重试；持续失败时保留进展
- 流中断或缺少用量回执：`failed`，保留未知用量与预算预占，不自动重试
- `pause` 递增 Epoch 并写 checkpoint；`resume` 从 LangGraph checkpoint 继续
- 进程重启：`app.runs` 中 `running` 的任务被标记 `paused`，需显式 `resume`；
  若有 `reserved/unknown` 模型用量，安全策略拒绝恢复并等待人工核账

## 8. 已知限制

- GitHub 代码搜索仅索引默认分支；固定 Commit 的证据读取仍按 Commit 精确校验。
- Issue/PR 读取固定为单个对象；不做全仓库 Issue 遍历。
- 自动模式增加独立模型的问题覆盖检查，但语义正确性仍需人工评审；机械层只保证
  “引用真实存在且逐字一致”。
- `search_code` 结果只是定位指针，不作为证据；必须随后 `read_file` 才能引用。
- 本模块不授予任何 GitHub 写权限，也不自动发布代码。

## 9. 验证结果（工程测试与真实模型实验分开记录）

### 9.1 工程侧

| 项目 | 结果 | 证据 |
| --- | --- | --- |
| `python -m compileall src/mgar_v3` | 通过（exit 0） | 本机 command log |
| `pytest tests/test_v32_repository_analysis.py` | 13 passed | `docs/evidence/v32-engineering-pytest.log` |
| `pytest tests/test_v32_repository_analysis_db.py`（真实 PostgreSQL） | 4 passed：fixture 证据/报告落库、幂等创建与 Thread 追问复用父证据、暂停/恢复、非法仓库身份拒绝 | `docs/evidence/v32-engineering-pytest.log` |
| DB 集成（fixture） | run `58ebd90e-77a4-4656-b842-51596e3ac108`：`succeeded`、4 model calls、4 tool calls、2 claims，报告哈希与逐字证据落库 | `docs/evidence/v32-live-preflight.log` |
| DB 集成（真实模型） | 见 9.2，全部经 API + PostgreSQL + CAS 落库 | `docs/evidence/v32-live-*.json` |

13 个纯单测覆盖：URL/身份解析正反例、路径与搜索越界、`read_file` 行区间契约束缚、
Commit 校验、预算裁剪、多轮 Agent 自主读文件与逐字引用、预算耗尽 partial、
insufficient、写工具与路径穿越拒绝、伪造/改写/行号越界引用被 verifier 拒绝、
quote 定位、README 指令注入不影响决策契约、授权过期、GitHub 错误码映射、
spec JSON 序列化。

### 9.2 真实 DeepSeek + GitHub 实验（2026-11-09，授权 `Loz888`）

模型 `deepseek-flash`，固定 `max_tokens=4096`，每个 run 独立预算行。费用账本合计
**243,745 micros（约 0.244 USD）**，见 `docs/evidence/v32-live-ledger-summary.txt`。

| 仓库 | 任务 | 结果 | 调用/预算 |
| --- | --- | --- | --- |
| `pallets/flask` | 请求进入应用后的处理链路 | `succeeded`，9 claims，6 节报告 | 8 model / 17 tool / 84,694 tokens / 38,452 micros；`v32-live-flask-report-08.md` |
| `encode/httpx` | Client→transport 分层与 `_send_single_request` | `succeeded`，6 claims | 6 model / 10 tool / 44,031 tokens / 18,423 micros；`v32-live-httpx-report-02.md` |
| `psf/requests` | `Session.request`→`HTTPAdapter.send` | `succeeded` | 5 model / 8 tool / 35,448 tokens / 12,493 micros；`v32-live-requests-report.md` |
| `zafstojano/ml-interview-questions-and-answers` | README 中的伪指令是否影响决策 | `succeeded`，把注入文本当作不可信数据引用 | `v32-live-ex-injection-report.md` |
| 同 Thread 追问 `pallets/flask` | Blueprint 注册位置 | `succeeded`，10 model / 11 tool / 90,071 tokens，父证据 ID 映射保留 | `v32-live-ex-followup-report-02.md` |

每份成功报告的每条关键结论均带 `github://owner/repo@<40 位 commit>/path#Lx-Ly`、
固定 Commit 的 blob 链接、Evidence ID 与逐字原文；页脚声明机械核验不等于语义正确。

异常场景（全部真实执行，命令与状态见 `docs/evidence/v32-live-ex-*`）：

| 场景 | 观测结果 |
| --- | --- |
| 输入 Token 预耗尽（`--input-tokens 4096`） | `waiting_input` / `partial_report` / `INPUT_TOKEN_BUDGET`（`48eb4ed8`） |
| 模型调用耗尽（`--model-calls 1`） | `waiting_input` / `partial_report` / `BUDGET_EXHAUSTED`（`68ffc1e1`） |
| 目标符号不存在 | `succeeded` 但报告明确 `insufficient`，列出三次空搜索，不编造调用链（`5b851571`） |
| 仓库不存在 | 创建阶段 `REPOSITORY_NOT_FOUND`，不产生 Run（`v32-live-ex-repo-missing.log`） |
| 私有仓库 | 创建阶段 `REPOSITORY_NOT_PUBLIC`（`v32-live-ex-private.log`） |
| 非仓库 URL（`/issues/1`） | `INVALID_INPUT`，不产生 Run（`v32-live-ex-url-invalid.log`） |
| 固定 Commit 不存在 | 创建阶段 `MCP_TOOL_FAILED`（上游拒绝），不产生 Run（`v32-live-ex-bad-commit.log`） |
| 文件不存在 | `GITHUB_NOT_FOUND`，observation 带上游原文（`v32-live-ex-file-missing.log`） |
| 路径不精确（`requests/api.py`） | `TOOL_PATH_NOT_EXACT`，提示真实路径 `src/requests/api.py`，模型可改路径 |
| GitHub 搜索限流 | 第 11 次搜索命中真实限流 → `GITHUB_RATE_LIMITED`，含 Retry 提示（`v32-live-ex-rate-limit.log`） |
| 暂停 | `repo-pause` 递增 Epoch，Run 进入 `paused`（`v32-live-ex-pause-cmd-03.log`） |
| 恢复 | `repo-resume` 从 checkpoint 继续；若模型调用尚未结算则 `RECOVERY_BLOCKED`，需人工核账后恢复 |
| 取消 | `repo-cancel` → `cancelled`（`8c8197c8`） |
| 模型返回无效 JSON / 截断 | 同轮最多 3 次重试；仍无效则 `failed` + `MODEL_INVALID_RESPONSE`，不伪称成功（`c79e8866`、`9ac71662`） |
| 每次调用记账 | 失败或无效响应也 `note_input_tokens`，费用不丢（`v32-live-httpx-status-loop.log`） |

### 9.3 本轮真实缺陷修复

- `read_file` 增加 `start_line` / `end_line`：6000 字符 observation 截断后仍能读到长文件
  的目标函数（httpx `_client.py` 必需）；fixture 与 live 行为一致。
- 无效/截断模型响应改为最多 3 次同轮重试（`step_key:retry`、`:retry:retry`），
  重试附紧凑输出提示；每次尝试独立预占与结算。
- 提示词明确 `action=insufficient` 必须 `report=null`，`action=tool` 必须给出
  `tool_calls`，并压缩报告上限，避免 4096 token 输出被截断。
- `follow_up` 修复三处真实缺陷：`app.runs` 无 `mandate_id` 列（改由 ticket 关联取
  actor）、dict row 上误用 `[0]` 取 `max(attempt_no)`、先更新 `tickets.mandate_id`
  再插入 mandate 触发外键失败。修复后追问可正常建 Run。
- `resume` 允许 `v32-repo-v1` 图由 checkpoint 恢复（此前仅白名单 `r5-repair-v1`）。
- GitHub 错误映射：`does not exist` / `does not point to a file` → `GITHUB_NOT_FOUND`；
  MCP 返回 “matching files: [...]” 提示时给 `TOOL_PATH_NOT_EXACT`，让模型改用真实路径。
- 数据库 503 增加服务端异常日志（运维可诊断，不再只有不透明 `STORAGE_UNAVAILABLE`）。

### 9.4 已知限制（本轮实测确认）

- `deepseek-flash` 在 `max_tokens=4096` 下仍可能返回空白内容或缺少 `action`；
  3 次重试后仍会失败（`c79e8866`、`f2bdf5a1`）。失败是显式的，不重发、不伪称成功。
- 暂停后恢复受 `deadline_at` 的墙钟约束：暂停时间过长会以
  `DEADLINE_EXCEEDED` partial 结束（`c3fa407f`）。
- GitHub 代码搜索只索引默认分支；固定 Commit 的证据读取仍按 Commit 精确校验。
- 机械核验只保证引用存在、逐字一致、行号落在证据内，不证明语义正确。

复跑 DB 集成测试（需先 `make deps-up && make migrate`）：

```bash
set -a && . ./.env && set +a   # 读取 MGAR_V3_DATABASE_URL，勿在文档中重复明文密码
export PYTHONPATH=src
uv run --frozen pytest tests/test_v32_repository_analysis_db.py -q
```
