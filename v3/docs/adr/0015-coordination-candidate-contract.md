# ADR 0015：E/F 实验路由候选契约

状态：实施。用户要求再次修复 E/F，并授权修复后公开报告及脱敏证据。

v1 的 E/F 让模型同时生成动作和 Run UUID。真实运行出现11个非法初始决定，导致22项后续路由失败。v1 保留，不重评。

第二版 Engine 从公开事件与同 Issue 订阅快照构造合法候选；模型仅回传 event_key/candidate_id。候选绑定保留在进程/冻结 PG 快照内，不把 UUID 的准确复制交给模型。服务端根据唯一主订阅、显式非空 investigate、模糊寻址及无关输入确定 deliver/start/clarify/ignore；无 Oracle/人工答案输入。这是确定性准入约束，不证明 LLM 推理提升。

所有提交在原持锁连接的同一外层事务中重查 Issue/Run/epoch/revision/当前 actor，并调用原 ingest、只读绑定和实验完成日志；异常全部回滚。模型、Redis、来源文件与 Artifact 操作保持事务外。

仅确认结算的 MODEL_INVALID_RESPONSE/COORD_SELECTION_INVALID 可使用一个独立新 control Run 纠正；批次最多两次，单 slot 最多一次，全部共享独立68次预算。不复用旧授权剩余额度，不修改原 Engine 的未知/恢复约束。planned SIGKILL 可在第二个有效响应之后发生：已记录结算拒绝的第一 slot 跳过，第二 slot 复用 PG typed result，旧 epoch 必须 fenced。

修复恢复收据的 response_hash 来源：原 usage_calls 无此字段，改读 model_response_recorded.safe_payload.content_hash，并在事务外核对 CAS。v1 的空字段收据及后补原生回执不改。

本次仅复测 E/F 的8次合成工作负载，Worker/Verifier/独立 Oracle 不变。原 D7/8 为非同期历史参照，原 A/B/C87/90与三项FAIL永久保留。部署、生产、多节点或通用代码修复质量不由此实验扩展。
