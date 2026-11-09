# A04 — Independent Defect Verifier

- 状态: 工程门 PASS（12 tests）
- 依赖: A02 / A03
- 代码: `v3/src/mgar_v3/verification/`
- 测试: `v3/tests/test_v33_defect_verifier.py`

## 1. 目标与边界

A04 让「候选缺陷」经过机械验证，而不是由模型宣布：

1. 记录基线测试状态，并绑定到准确的 Commit 与环境指纹；
2. 在受限沙箱中执行复现命令，产生结构化执行回执；
3. 区分环境错误、依赖缺失、真实测试失败、无测试与超时；
4. 只有 A04 自己生成的回执才能把 Finding 推进到 `reproduced`；
5. 纯静态风险使用静态确认口径，永远不声称运行时复现。

明确不做：

- 不修复代码（A05+）。
- 不新增扫描器规则（A02）。
- 不写数据库；回执以 JSONL 追加日志保存，可回放审计。
- 不把「Agent 提交的成功声明」当作证据。

## 2. 环境指纹

`VerificationEnvironment.create()` 固定：

| 字段 | 含义 |
| --- | --- |
| `repository` / `commit` | 被验证的仓库与 Commit |
| `sandbox` | `local` / `docker` |
| `os` / `python` | 操作系统与 Python 版本 |
| `tool_versions` | 关键工具版本（如 pytest） |
| `image` | Docker 镜像（本地模式为 None） |
| `network_enabled` | 默认 false；复现不允许联网 |

`environment_id` 为以上内容的 SHA-256。回执与环境不一致时不能被接受。

## 3. 执行回执

`ExecutionReceipt` 记录：

- `receipt_id`：对命令、退出码、输出摘要、Commit、环境等做 SHA-256；
- `command` 与 `reproduce_command`（`shlex.join`）；
- `exit_code`、`timed_out`、`truncated`、`duration_seconds`；
- `stdout_sha256` / `stderr_sha256` 与最多 2000 字符的输出摘要；
- `classification`、`sandbox`、`commit`、`environment_id`。

`ReceiptJournal` 是验证器持有的追加式 JSONL：

- 相同 `receipt_id` 不重复写入；
- `owns(receipt_id)` 是接受外部回执的唯一依据；
- 记录损坏时 fail closed（`RECEIPT_JOURNAL_CORRUPT`）。

## 4. 结果分类

`classify_execution(argv, result)`：

| 分类 | 判定 |
| --- | --- |
| `timeout` | 沙箱超时 |
| `no_tests` | pytest 退出码 5 |
| `passed` | 退出码 0 |
| `environment_error` | 命令不存在、权限/只读/磁盘/内存、Docker 不可用，或 pytest 2/3/4 且无真实失败特征 |
| `missing_dependency` | `ModuleNotFoundError`、`No module named`、Go 模块无法解析、离线代理等 |
| `test_failure` | pytest 退出码 1，或收集/语法错误，或 Go 测试非零退出 |
| `command_error` | 未知命令非零退出，不能归因于缺陷 |

顺序固定：超时 → 无测试 → 通过 → 环境 → 依赖 → 真实失败 → 未知。

## 5. 验证流程

`DefectVerifier.verify(plan)`：

1. 纯静态风险（`classification == "risk"`）或 `plan.static_only`：直接拒绝，
   返回 `STATIC_ONLY_CONFIRMATION_REQUIRED`。
2. 记录（或复用）基线；基线为环境/依赖/超时/未知错误时返回
   `baseline_unhealthy`，Finding 状态不变。
3. `suspected` → `investigating`。
4. 在沙箱执行复现命令，生成回执并写入日志。
5. 若声明了 `expected_failure_signature`，输出必须包含它；否则返回
   `signature_mismatch`，Finding 保持 `investigating`。
6. `test_failure` → 附加 `test_run` 证据并迁移到 `reproduced`。
7. `passed` → 附加 `test_run` 证据并迁移到 `not_reproduced`。
8. 其他分类不改变生命周期，只保留回执与 `attempted` 复现状态。

基线为 `test_failure` 时允许继续（仓库本来就红），但 `baseline_dirty=true`；
环境不可用时不允许把任何失败归因于候选缺陷。

## 6. 静态风险口径

`verify_static(finding_id, evidence, rationale)`：

- 证据 kind 必须是 `scanner` / `static_analysis` / `investigation` / `manual`；
- `rationale` 必须非空；
- 结果只到 `confirmed_static`，`reproduction_status=static_only`；
- `test_run` 证据不能用于静态确认（`STATIC_EVIDENCE_REQUIRED`）。

## 7. 反自我确认

`submit_receipt(receipt)`：

- Commit 或环境指纹不一致 → `RECEIPT_BINDING_MISMATCH`；
- 回执不在验证器日志中 → `AGENT_SELF_VERIFICATION_DENIED`，状态不变；
- 只有验证器自己生成的回执返回 `recorded_existing`，不会重复迁移。

A03 的策略保持不变：模型提案最多到 `investigating`，`reproduced` 只能来自
A04 的机械回执。

## 8. 汇总

`summary()` 输出基线回执、分类、Finding 总数、已复现/未复现/未验证数量：

- `zero_defects`：无 Finding 且基线 `passed` 或 `no_tests`；
- `complete`：存在健康基线且没有 `suspected` / `investigating`。

## 9. 测试覆盖

`uv run --frozen pytest tests/test_v33_defect_verifier.py -q`（12 passed）：

- 基线回执绑定 Commit、环境与命令，JSONL 已落盘，仓库无 `__pycache__`/`.pytest_cache`；
- 真实失败测试 → `reproduced`，带 `test_run` 证据，源文件未被修改；
- 通过测试 → `not_reproduced`；
- 失败特征不匹配 → `signature_mismatch`，不冒充已复现；
- 环境错误 / 依赖缺失 → 仅记录回执，不改变 Finding；
- 静态风险只能静态确认，`test_run` 证据被拒绝；
- 伪造回执被拒（`AGENT_SELF_VERIFICATION_DENIED`），Commit 不匹配被拒；
- 普通仓库基线通过返回 `zero_defects`；
- 基线不健康时阻止复现；
- 分类函数覆盖超时、无测试、依赖缺失、真实失败与未知命令；
- 回执确定可复现，日志幂等。
