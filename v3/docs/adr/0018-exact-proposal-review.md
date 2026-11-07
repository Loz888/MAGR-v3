# ADR 0018：对已获批原提案追加发布前复验

用户于2026-11-07明确批准N01/N02/N03三份完整Draft PR提案。等待期间原业务证据与审批有效期已过。授权仅覆盖原payload_hash、补丁和三个隔离测试base，不扩大模型调用、Issue评论或合并权限。

新增0019迁移和显式operator SDK `commerce.review.recheck`。仅允许尚未审批/入Outbox/开始发布、原Run仍waiting_approval、Case/spec/input/epoch/primary/写权和完整payload均一致的提案。重新读取实际SQL五列数据及聚合、核对原行哈希与Decimal指标、重新校验知识引用/ACL和固定GitHub源码对象，并在原锁定镜像中重跑公开及独立Go测试。任何变化需要新提案，不能复用本次批准。

这是P0结果设计的明确补充：原BusinessResult、fresh_until、Action、原测试与模型回执均不改写；复验另存不可变certificate，绑定准确Action/payload_hash/result_id/Case/input/epoch，沿用新SQL观察的TTL。仅该Action的发布资格可使用新证书，原调查结果不能因此用于新修复。后续失败覆盖此前证书资格。投影显示证书哈希，避免将旧观察伪装成新观察。

复验不自动审批或发布、不调用模型，不延长旧共享付费grant。短事务只做SQL及可变权威重核，文件/网络/Go在事务外。证书通过后仍必须经过原renew_review、准确hash审批、Action/Outbox/Dispatcher及远端完整对象核对；unknown仍只读对账，不自动重发。
