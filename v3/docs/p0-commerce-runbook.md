# 电商核账工单与独立业务判定

本轮把两项[P0设计](design/p0-commerce-v1/README.md)接入实际应用。范围为三个固定合成场景、一个只读调查阶段及一个独立的 Go 函数修复阶段。实施与验收记录见[修复档案](repairs/P0-20261106/README.md)。原设计稿保留其冻结时的状态，当前状态以实施档案为准。

## 启动受控演示

在自己的克隆目录 `MAGR-v3/v3/` 执行。若工作台已经运行，先停止该工作台进程，再进行来源注册；同一数据库只能有一个 Engine。

```bash
make deps-up
make migrate
make sandbox-build
uv run --frozen mgar commerce-setup
make dev
```

`commerce-setup` 只注册合成知识文档、三组独立只读 SQL 数据及当前 Git 提交中的自有 Go 示例。私有只读连接配置位于 `.local/commerce/`，权限为0600；API不返回连接串。注册可重复执行，不重新生成三个已有模板。源码来自注册时的 Git 提交，未提交的工作区改动不会成为调查来源。

打开 `http://127.0.0.1:18080`，输入本机 `.env` 中的工作台令牌。使用“电商营收核账”面板，选择重复支付、状态过滤或退款状态，填写独立 Issue 编号，选择仅报告或修复提案。时间窗在页面按本地时区展示，服务端使用带时区的半开区间。

fixture模式没有真实模型调用和真实GitHub发布。修复会实际启动无网络、只读根文件系统的Docker/Go沙箱；必须先准备固定镜像。修复提案可审阅，发布远端仅在显式注入受控MCP测试服务时才可用。不要把本地演示回执当成GitHub PR。

## 工作流

1. Case固定模板、来源、窗口、目标、actor和spec hash；客户端不能传入任意SQL、仓库、角色、模型预算或判定结果。
2. 原R4图通过实际工具读取知识、SQL和源码。模型只选择服务端来源目录中的短引用ID；数值由实际typed SQL行和Decimal计算。
3. 独立检查器验证来源、完整引用单元、数值及输出结构。业务结果与原只读Run完成在同一短事务提交，文件读取和模型调用位于事务外。
4. 用户请求修复时，原R5创建独立Run/Mandate/profile，绑定准确调查结果hash。调查的只读权限不升级为写权限。
5. 原Go AST范围、公开测试、28个独立子案例及本Case实际订单窗口回归全部通过，才产生修复业务结果和完整Action提案。函数体响应经确定性转换产生diff，原模型响应保持不变并保留转换来源。
6. 用户审阅完整提案后按准确payload hash审批。每个发布子步骤前重查结果、输入、来源、权限、期限和fence。未知远端写只读对账；不自动重发。

业务结果为`passed/failed/needs_input/denied/unknown`；执行、审批、交付各保留原账本。没有新检查器结果的旧Run显示未评估，不从`run.succeeded`推断业务通过。旧结果和Case规格只追加，窗口变化或修复阶段的新业务消息创建新Case版本；旧PR与失败仍可追溯。

## CLI与API

```bash
uv run --frozen mgar commerce-create --template duplicate_paid --issue 9101 \
  --start 2026-11-04T16:00:00Z --end 2026-11-05T16:00:00Z --goal draft_pr
uv run --frozen mgar commerce-list
uv run --frozen mgar commerce-result CASE_UUID
uv run --frozen mgar commerce-repair CASE_UUID --revision 1 --result-hash RESULT_SHA256
uv run --frozen mgar commerce-message CASE_UUID --revision 1 --body '补充需要核对的事实'
```

`CASE_UUID`与`RESULT_SHA256`使用上一条实际响应的值。审批、拒绝、补丁编辑和只读对账继续使用原CLI及工作台入口。

| 接口 | 用途 |
|---|---|
| GET `/api/v3/commerce/templates` | 已注册、脱敏模板 |
| POST/GET `/api/v3/commerce/cases` | 创建、列出当前actor的工单 |
| GET `/api/v3/commerce/cases/{id}` | 规格与全部阶段链接 |
| GET `/api/v3/commerce/cases/{id}/result` | 四维投影、当前业务结果、完整提案 |
| GET `/api/v3/commerce/cases/{id}/artifacts/{hash}` | 权限重核后的报告、数值回执或引用正文 |
| POST `/api/v3/commerce/cases/{id}/messages` | 幂等后续消息及显式窗口/目标变化 |
| POST `/api/v3/commerce/cases/{id}/repair` | 绑定当前调查hash的独立修复请求 |

全部接口复用本地Token认证，读取响应使用`Cache-Control: no-store`。后续消息需要`expected_case_revision`；结构化改变通过`changes.window`或`changes.goal`提交。真实模式改变窗口需要匹配的新注册许可，不能静默复用旧授权。

## 真实模式与验证

真实模板由可信操作者通过`Engine.commerce.register`注册。manifest包含当前project/repository/actor、公开来源hash、知识snapshot/config hash、私有reader路径、固定GitHub Snapshot，以及`live.investigation`和`live.repair`各自的profile/authorization文件引用；修复另需snapshot文件引用。注册同名同scope的不同manifest会拒绝，不能重置已有预算。HTTP不提供任意注册或提权入口。

真实模式要求源码为固定GitHub commit/tree且路径匹配；R4许可匹配知识、数据和检索配置，R5许可匹配同一源码、Issue和独立预算组。Case与原Mandate固定各阶段profile hash，换文件内容不能更换模型或预算。原生模型账本、仓库US$2上限和unknown停止规则继续生效。已有许可余额不能自动转移给新实验；实际运行前冻结具体profile、数据、源码与逐调用限额。

P0另使用独立共享grant，当前有界实验最多18次、US$0.055296；所有费用仍记录在原usage_calls，unknown不退款或重置。收费/外部写部署开关仅识别字面量`1`，开关本身不授予许可。真实发布需配置固定官方MCP二进制及读写身份；可显式使用运行环境已有`GH_TOKEN`，不把凭据写入模型输入或公开报告。

```bash
make lint
make typecheck
make verify TASK=V31-P0-IMPLEMENT
```

注册工程测试使用真实PostgreSQL、Redis、Go/Docker，模型HTTP及GitHub发布为受控服务。只有单列的真实模型与GitHub回执才能证明真实业务验收。此切片不扩展通用SQL、多文件修复、团队登录、开放规划、额外检索/盲测/负载或生产HA。
