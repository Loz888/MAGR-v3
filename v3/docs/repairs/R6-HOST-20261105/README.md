# R6 三项发布阻塞验收档案

三项均在用户批准的独立Linux/systemd虚拟机范围内 **PASS**。原V2/V3数据库未作为故障对象；原633次、保守US$0.299317账本不变，新增生成请求0。R6 v6原87/90成绩及3项失败不改。

| 门禁 | 实际证据 | 结果 |
|---|---|---|
| V2价格证据检查 | P7-06J修复历史夹具时钟；688项普通/race Go、453项Python和15子测试、静态门禁；623历史文件哈希不变；当前官方GET价格/模型核验 | PASS |
| 实际宿主部署 | 独立Linux内核/PID1 systemd，真实PG16/Redis/Docker，mgar与watchdog不同用户，权限/环回监听/无NIC；真实受限Go沙箱、HTTP工作流及报告 | PASS |
| 整节点故障/恢复 | 两次SIGKILL整个QEMU、正常整机重启、4个不同boot_id；PG自动WAL恢复，提交保留/未提交回滚，报告/CAS/去重和原账保留；旧epoch拒绝、未知预占4352不清零且不能重发；第二库恢复和Reader只读权限 | PASS |

[机器验收](acceptance.json)、[逐阶段实际结果](host-result.json)、[全部问题与修复](failure-history.json)、[私有原始证据哈希](private-evidence-index.json)。原始日志/数据库/角色/读者配置和VM磁盘留在私有恢复材料，不上GitHub。用户明确选择测试VM；不将此扩张为用户生产服务器、物理存储掉电或多节点HA。

V2提交 `f1318b22e4dc20e510e66bc6251d7f3e4e09982d`。历史官方价格JSON与reviewed_at保持原样；正式付费入口仍按当前时间拒绝过期或未来报价。新官方只读观察作为新证据保存，不能用来重开已消费冻结轮。见 [V2工程记录](v2-engineering.json)、[现价核验](v2-current-official-pricing.json)。V2原模型业务阶段/round3/P8状态不变。

宿主使用QEMU10.0.13 TCG、Debian12/Linux6.1.0-54、3GiB内存、PG16/pgvector、Redis和Docker overlay2；原发布单元保留NoNewPrivileges与ProtectSystem。内核/overlay模块来自同一官方Debian包，hash已核验。虚拟机无NIC、无模型/GitHub凭据，付费与外发关闭。源码生成协议、Oracle和业务门槛不变。

## 完整修复过程

首次ROM路径问题、Redis/私有恢复权限、SQL_ASCII错误、Docker/PATH缺失、VFS空间不足和TCG冷编译超时全部保留。修正为UTF8、正确属主、root私有备份stdin恢复、真实Docker/overlay2、可信标准库依赖预热；未放宽45秒、非root、禁网、只读、cap-drop、内存/CPU/PID限制。预热镜像只改Go缓存和本地编译统计，编译器与可信入口未改；原云端默认镜像/锁文件仍原样。

首次真实宿主阶段已PASS，但串口ANSI字符使监督器解析失败。封存其原始PASS日志后只修复监督解析，继续同一节点，不重做工作流、不清零账本。恢复验收首次绕过正式DataReader连接参数，误判角色应默认只读；查明原库与恢复库写权限均拒绝后，改用正式Reader并额外验证关闭会话只读仍不能INSERT。失败恢复库保留，重新恢复独立新库并比较CAS实际字节和恢复库探针。补齐原镜像锁定的可信AST二进制，以服务用户实测允许函数体修改、拒绝导入变化。正常重启及最后关机的监督等待没有持续读取串口，QEMU控制台背压延迟退出；释放监督连接后原虚拟机均正常退出0。监督等待修复为持续读取串口、最多300秒，另加实际正常重启和关机均退出0的复验，沙箱45秒不变。原超时和监督停止记录保留。维护启动和失败启动不计入验收4个boot_id。

## 执行与交付

源工具见 `deploy/host-acceptance/`，入口和边界见其README。当前所用源码业务快照 `c5ca75cd59fc2206bda1d6486955774350227e04`，完整模型冻结 `61147ae43e2e8f9cfff3b430b9422d27ed7fa609a8e77447a3f30f2c132695d9`。发布修复只涉及部署/验收和V2测试参考时钟，不重新解释旧模型成绩。

验收boot_id：6d747c02-41ea-4465-9633-219c0c46ca27, 2bce3f1f-4035-4bd7-9666-f0d637d85552, 6b770636-aeab-4601-9afd-3f16be1d1830, 63b00ef0-42cb-4cba-8214-e0e5c5fc3de1。全部阶段通过后测试虚拟机正常关机，私有磁盘及恢复包保留；公开源码/档案包附SHA256。
