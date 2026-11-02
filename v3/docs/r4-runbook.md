# R4 知识与专家调查

R4 默认使用 `r4-experts-v1` 图；旧 r1/r2 图继续按原版本恢复。默认专家与向量为明确标记的 fixture，只演示协议。真实中文检索可在本机 CPU 上运行，不消耗模型 API 调用。

## 在 Windows / WSL 更新并运行

先停止正在运行的 `make dev`（Ctrl+C）。在你自己克隆的目录执行；以下路径以此前推荐的 `~/projects/MAGR-v3` 为例：

```bash
cd ~/projects/MAGR-v3
git pull --ff-only origin main
cd v3
make setup
make deps-up
make migrate
make data-demo
make rag-models
make rag-train
MGAR_V3_RAG_MODE=local make rag-demo
make doctor
MGAR_V3_GRAPH_VERSION=r4-experts-v1 MGAR_V3_RAG_MODE=local MGAR_V3_R4_TOOL_BASE_URL=http://127.0.0.1:18080 make dev
```

首次克隆方式与 Python 3.12、uv、Docker Desktop 的准备见 [应用说明](../README.md)。`/workspace/...` 是云端开发路径，不会自动出现在你的 WSL。若仓库克隆到了其他位置，用自己的路径。

`rag-models` 下载约 78 MB 的官方 spaCy 中文模型，校验锁定的 SHA256；不调用 DeepSeek。`rag-train` 在独立的 16 个开发样本对上训练线性 pairwise 重排器。该模型是轻量基线，不能当作 Transformer 或商业重排服务。`rag-demo` 仅索引 `config/r4-corpus.json` 明确列出的十二篇合成资料，问题与答案文件不进入索引。

`data-demo` 创建合成订单表及独立只读数据库角色，凭据只写入本机 `.local/data-reader.json`（0600）。它需要本地开发数据库账号有建 schema/角色权限；不使用生产数据库。资料准备命令与服务共用 Engine 单写者锁，因此应在启动服务之前运行。再次运行会轮换本地 reader 密码，不改变固定数据内容。

浏览器打开 `http://127.0.0.1:18080`，输入本机 `.env` 的 `MGAR_V3_LOCAL_TOKEN`。可以：

- 上传 Markdown、UTF-8 文本、Python 和有文字层的 PDF；Web/API 单次请求限制使文件上限约 45 KB。索引器的独立原始文件上限为 2 MB，扫描 PDF 明确拒绝。
- 查看入库任务及失败原因、检索并读取实际原文、撤销或恢复资料访问。
- 提交“核对支付事件数和去重订单数，并检查资料与代码中的统计口径”等调查。Knowledge、Repo、Data 三个专家并行，最多补两轮证据，共享 36 次只读工具调用和 90 秒期限。
- 查看已核验观察、假设、冲突、缺失证据和下一步。缺失或冲突时等待补充，不创建发布动作。完整报告继续沿用逐条内容审批和统一 Outbox 出口。
- 仅在 Ticket 已结束且报告通过机械核验后，预览并确认准确摘要；确认前没有记忆。摘要可撤销，原始来源撤销后也不能继续召回。

默认专家为确定性协议实现：Repo 读取仓库中明确标注的合成代码，Data 固定核对 paid 事件数、去重订单数及事件金额（3、2、32.00）。该演示不代表任意自然语言 SQL 或真实业务根因分析。配置真实中文检索并不会自动把专家切换成付费模型。

## 验证与接口

```bash
make lint
make typecheck
make verify TASK=V31-R4
make test
make rag-eval
make r4-local
```

`rag-eval` 创建并删除自己的临时数据库，实际运行本地中文向量、pgvector、中文 BM25、RRF 和学习重排，输出 `.local/r4-eval/result.json`。冻结测试问题不参与训练；报告保留未命中案例。不能用 fixture 的通过数量替代这一语义验收。

`r4-local` 在自己的临时数据库中，把真实本地检索、HTTP MCP、只读 SQL 和专家图串进同一个 Run；专家与 Repo 仍为合成资料演示，报告只创建待审批动作，不批准或发布。结果保存在 `.local/r4-local/`。

真实 MCP HTTP 地址为 `/mcp/knowledge/` 和 `/mcp/data/`。它们只接受 Engine 给当前 Run/epoch/角色签发的短期能力令牌；本机 Web 访问令牌不能代替专家令牌。HTTP 路径和直接调用路径在报告中明确记录。两种路径都使用相同的服务端权限核查。工具不具备发布权限。

Web/CLI 共用 API。新增入口包括 `/api/v3/knowledge/documents`、`knowledge/jobs`、`knowledge/search`、`runs/{id}/resolution`、`context/{kind}/{id}` 和 `memories`。`POST /api/v3/runs` 可显式给 `knowledge_snapshot_ids`（最多两个）；缺省时在计划阶段固定当前快照。相同 client_request_id 改变快照会被拒绝。分页 continuation 绑定身份、对象、哈希和有效期，每次继续读取都重新鉴权。

## 真实付费验收状态

此前三次物理调用已用满，累计保守费用 USD 0.002456；本次新增付费调用为零。完整 R4 付费跨源实验、业务 Oracle 和单 Agent 对照均为 **NOT_RUN**，见 [待执行清单](../config/r4-live-manifest.json)。本地检索语义验收与真实 MCP/SQL 不等同完整业务 live 门禁。

已提供 DeepSeek 专家适配路径，但 R1 授权不能复用于 R4。未来执行需要明确增加调用额度、独立 R4 profile/授权、固定知识快照/模型配置/数据哈希及真实 GitHub commit，并继续由预算账本、只读工具能力与审批控制。本次没有生成可生效的新付费授权，也没有新增 GitHub 结果评论。
