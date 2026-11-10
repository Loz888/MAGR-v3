# 单页仓库分析 Agent

`/`、`/repo` 和 `/workbench` 使用同一个对话页面。普通分析只需提交 GitHub 仓库
URL 和问题；分支或 Commit 可选，默认使用仓库默认分支。页面默认调用 live 分析，
没有真实配置时展示具体缺项，不会自动切换到合成回答。

## 在自己的电脑启动

需要 Python 3.12、uv、Make、Docker Engine 和 Compose V2。Windows 使用
WSL2 Ubuntu，并启用 Docker Desktop 的 WSL Integration。

在项目的 `v3` 目录依次运行：

```bash
uv python install 3.12
make setup
make deps-up
make migrate
make doctor
make dev
```

浏览器访问 `http://127.0.0.1:18080`。点击左下角“连接服务”，输入本机
`v3/.env` 中 `MGAR_V3_LOCAL_TOKEN` 的值。令牌只保存在浏览器会话中。
模型 Key 和 GitHub 凭据始终配置在服务器端，不需要填入网页。

## 首次配置真实分析

在本机 `.env` 配置以下变量，已有值应保留：

```dotenv
MGAR_V3_ALLOW_PAID=1
MGAR_V3_DEEPSEEK_API_KEY=你的密钥
MGAR_V3_GITHUB_READ_TOKEN=你的GitHub只读凭据
MGAR_V3_REPO_ANALYSIS_AUTHORIZATION_PATH=.local/repo-analysis-authorization.json
```

由操作员参考 `config/repo-analysis-authorization.example.json` 创建本地授权文件：

- `approved_by` 应属于 `MGAR_V3_ALLOWED_ACTORS`；默认账号为 `loz888`。
- `allowed_repositories` 填写实际允许分析的 `owner/repository`。
- 确认 `expires_at` 尚未过期，且明确允许模型调用。
- 设置每次分析的模型轮数、工具次数、费用、上下文与时间上限。
- 模型名称应是供应商实际提供的模型；计费上界应与模型匹配。

修改 `.env` 后重启 `make dev`，再点击连接设置中的“连接并检查配置”。
配置页显示的是凭据与授权的配置状态；实际远端权限或网络错误在提交和执行时报告。
凭据和授权文件不要提交到 Git，也不要发送到聊天中。

更完整的授权 Schema 与 CLI 说明见 [仓库分析运行手册](repo-analysis.md)。

## 使用

1. 输入公开仓库 URL，例如 `https://github.com/pallets/flask`。
2. 输入具体问题，例如“说明请求如何进入应用，指出入口文件和关键调用关系”。
3. 点击“开始分析”，或按 Ctrl/Cmd + Enter。
4. 页面自动显示运行状态、模型与工具次数、源码阅读记录、报告和引用。
5. 完成后在同一输入框追问。追问固定在原仓库和 Commit，继承上一轮报告及证据映射。
6. 左侧切换历史对话；刷新页面可恢复所选对话。分析可暂停、恢复或取消。

当前 Agent 会在授权预算内循环调用模型和只读 GitHub 工具，不能等同于具有任意
终端、编辑和部署能力的完整 IDE Agent。此入口用于仓库分析，不自动修改远端代码。
预算或期限耗尽时会明确展示未完成结果，不把部分报告标为成功。

需要离线验证页面时，在连接设置的“开发与离线验证”中显式选择合成样例。
演示始终标注，不会读取所填仓库，不调用模型。旧业务、修复与审批页面保留在
`/legacy` 和 `/legacy/workbench`，可从同一设置面板进入。

## 本轮验证

注册工程验收命令：`make verify TASK=AGENT-UI`。

- `tests/test_agent_page.py` 验证统一入口、配置缺项、过期授权、缺 Key 提前失败，
  以及 live API → 后台 LangGraph → 源码工具 → 报告 → 带前文追问的完整代码路径。
  使用真实 PostgreSQL、checkpoint、模型客户端与预算账本；GitHub 和模型响应受控，
  不产生真实付费调用。
- Chromium 浏览器验证连接、配置提示、演示运行、自动刷新、追问、页面重载、
  不可信 Markdown 隔离，以及桌面和手机布局。
- 真实 DeepSeek/GitHub 的端到端请求仍需要运行机器配置有效凭据与授权。
