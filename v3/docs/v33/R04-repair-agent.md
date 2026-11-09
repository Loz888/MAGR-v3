# R04 — Iterative Repair & Independent Verification

- 状态: 工程门 PASS（9 tests）
- 依赖: R03（通用沙箱测试执行器）、R02（隔离 Worktree 与补丁编辑器）
- 迁移: 无
- 代码: `v3/src/mgar_v3/repair/repair_agent.py`
- 测试: `v3/tests/test_v34_repair_agent.py`

## 1. 目标与边界

R04 把 R01–R03 串成真实的 `Patch → Test → Feedback → Patch` 修复循环：

1. 在交接的基线树上复现公开测试失败；
2. 修复提供方提出修复计划与受控文件编辑；
3. R02 Worktree 校验授权路径、受保护配置、冻结路径与配额后原子应用；
4. 执行公开测试；
5. 执行提供方不可见的独立验证测试；
6. 任一阶段失败都形成结构化反馈并回滚补丁；
7. 在尝试次数、测试次数与墙钟预算内继续；
8. 只有公开测试与独立验证同时通过才生成 Repair Proposal；
9. 每次失败尝试与最终结果写入 Episodic Memory。

明确不做：

- 不执行模型返回的 Shell 文本；测试命令只来自 R03 的可信适配器；
- 不把独立验证文件放进提供方可见的提示或 Worktree；
- 不在基线未复现失败时宣称修复；
- 不负责审批与发布（R05）；
- 不依赖 `orders.go`、固定测试名或商城业务字段。

## 2. 数据流

| 对象 | 作用 |
| --- | --- |
| `RepairRequest` | 仓库、目标、语言、基线文件树、授权路径、冻结路径、独立验证文件与测试名、预算 |
| `RepairPrompt` | 提供方唯一可见的输入：公开文件树、目标、当前尝试序号与历史反馈 |
| `PatchPlan` | 提供方唯一允许返回的形状：计划摘要 + `FileEdit` 列表，不含命令 |
| `PatchResult` | R02 返回的前后 Tree Hash、补丁编号与变更清单 |
| `TestReceipt` | R03 返回的镜像 ID、命令、测试集、退出码、日志哈希与策略 |
| `RepairAttempt` | 单轮计划、补丁、公开测试、独立验证与反馈 |
| `RepairProposal` | 通过双阶段验证的内容寻址候选，含完整 diff 与两份回执 |
| `RepairEpisode` | 写入 Episodic Memory 的失败或最终结果记录 |
| `RepairReport` | 最终状态、停止阶段、原因、基线回执、尝试列表与测试次数 |

`RepairRequest` 要求 `verification_files` 非空，且不得与公开文件树重叠；否则在
构造阶段即拒绝，避免"验证文件就是公开测试"的伪独立。

## 3. 预算

`RepairBudget` 三项均为硬限制：

| 预算 | 默认 | 范围 | 行为 |
| --- | --- | --- | --- |
| `max_attempts` | 3 | 1–10 | 每轮模型提案消耗一次；拒绝补丁也计入 |
| `max_test_runs` | 10 | 1–50 | 基线也计入；每次公开/独立验证各计一次 |
| `deadline_seconds` | 900 | 1–3600 | 用单调时钟计量；超时立即停止 |

停止原因分别映射为 `exhausted`、`budget_exhausted` 与 `deadline_exceeded`，
不会把预算耗尽伪装成修复成功。

## 4. 两阶段测试与隔离

- 公开测试：在 R03 固定镜像内运行提供方可见的测试集；
- 独立验证：同一镜像内运行 `公开文件树 + verification_files`，提供方从未见过
  验证文件；
- 成功条件：验证回执状态为 `passed`，且声明的 `verification_tests` 与公开测试名
  都出现在验证回执的测试集中；
- 验证失败时反馈只写"Independent verification did not pass."，不泄露隐藏测试名、
  失败断言或日志；
- 网络、宿主文件与凭据隔离完全继承 R03 的容器策略与 `docker inspect` 实检。

## 5. 范围检查

补丁在进入测试前经过三层检查：

1. `PatchWorktree` 的 R02 授权路径、隐藏路径、符号链接与受保护配置检查；
2. `frozen_paths`：公开测试等不可变文件即使位于授权路径内也拒绝，错误码
   `REPAIR_FROZEN_PATH`；
3. R02 配额：文件数、变更行数、总字节、单文件字节与删除策略。

范围内变更会记录每个文件的增删行数、字节数与 SHA-256。补丁被拒绝时不执行
任何测试，但仍消耗一次尝试并写入 Memory。

## 6. 状态与阶段

`RepairStatus`：

| 状态 | 含义 |
| --- | --- |
| `succeeded` | 公开测试与独立验证均通过，已生成 Proposal |
| `baseline_not_reproduced` | 基线未以失败测试复现，禁止继续 |
| `waiting_for_dependencies` | 依赖无法离线安全安装，停止等待 |
| `sandbox_error` | 沙箱不可用、超时或基础镜像异常 |
| `exhausted` | 尝试次数内未得到验证通过的候选 |
| `budget_exhausted` | 测试次数预算耗尽 |
| `deadline_exceeded` | 墙钟预算耗尽 |

`RepairStage` 记录停止位置：`baseline`、`patch`、`public_tests`、`verification`。

## 7. Episodic Memory

`EpisodeSink` 协议把修复过程与存储解耦；默认 `MemoryEpisodeLog` 在进程内保存，
生产环境可绑定 M02 `MemoryWriter`。记录时机：

- 每次补丁被拒绝：`patch_rejected`；
- 每次公开测试失败：`public_tests_failed`，附公开失败测试名；
- 每次独立验证失败：`verification_failed`，不附隐藏测试名；
- 基线未复现、依赖等待、沙箱错误、预算/时间耗尽、尝试耗尽；
- 最终成功：`succeeded`，附验证后的 Tree Hash。

`episode_id` 是仓库、目标、语言、状态、尝试序号、树哈希、原因与失败测试的内容
摘要，失败原因因此始终可追溯。

## 8. 测试与验收

验收命令：

```bash
uv run --frozen pytest tests/test_v34_repair_agent.py -q
```

覆盖：

1. Python 缺陷：基线失败 → 第一次错误补丁反馈 → 第二次修复通过公开与隐藏测试，
   Proposal 含镜像 ID、日志哈希与内容寻址编号；
2. 只满足公开测试的伪修复被隐藏验证拒绝，补丁不发布；
3. Go 缺陷使用同一循环与独立验证套件完成修复；
4. 基线已通过时不调用模型、不生成 Proposal；
5. 受保护配置、冻结测试文件与越权路径在测试前分别拒绝，之后仍可完成修复；
6. 连续错误补丁耗尽尝试次数，`proposal` 为空且失败原因进入 Memory；
7. 测试次数预算在验证前停止循环；
8. 第三方依赖触发 `waiting_for_dependencies`，不运行模型；
9. 截止时间与请求形状校验生效。

结果：`9 passed`；Ruff 与 mypy strict 通过。

## 9. 已知边界

- 测试中的提供方是确定性脚本；生产模型必须实现 `RepairProvider` 并输出
  `PatchPlan` JSON，任何自由文本命令都不会被执行；
- 默认 Memory sink 是进程内记录，生产接线需把 `RepairEpisode` 交给 M02；
- 仅当公开测试失败构成可复现基线时才进入修复；仅凭静态推断不得宣称修复；
- 新增文件不能是依赖清单（R02 默认保护），因此无法通过补丁引入新依赖。
