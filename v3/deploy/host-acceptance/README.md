# R6 独立 Linux/systemd 节点验收

这是受控测试虚拟机的部署与整节点恢复验收，不把云容器里的进程重启当作宿主验收。虚拟机使用独立 Linux 内核、PID1 systemd、持久 ext4 块设备、PG16/pgvector、Redis、Docker overlay2、两个服务账号和原发布单元。虚拟机没有网卡，付费和外部发布关闭。

输入为已核对源码包、Python3.12锁定环境和私有 PG/全局角色/CAS/Reader 恢复副本。所有私有值只进入隔离副本，不复制模型/GitHub凭据。当前验收绑定R6 v6原账633次、299317微美元，测试生成请求为零。宿主、两个原数据库容器和原数据卷不作为故障对象。

## 固定条件

- QEMU10.0.13 TCG，3GiB内存、2个vCPU，无NIC，安装材料通过只读9p共享盘复制；共享盘无供应商凭据。
- Debian12、Linux6.1.0-54-amd64；overlay模块与该内核来自同一个经APT/官方包校验的Debian包。模块/内核/镜像SHA256写验收证据。
- PostgreSQL必须显式UTF8；开启fsync、synchronous_commit、full_page_writes。Redis与服务目录的属主分别还原。
- Engine使用mgar用户，Watchdog使用不同用户和不同0600配置；API和数据库只监听环回，原systemd单元保持NoNewPrivileges/ProtectSystem保护。
- 安装Docker静态组件和固定R5沙箱镜像，使用overlay2；禁止VFS造成根盘层复制膨胀。Docker不创建网络桥或iptables规则。AppArmor在本测试内核中关闭，原沙箱非root、无网络、只读根、cap-drop、seccomp、PID/内存/CPU限制保持。
- 仅修改本次创建的VM磁盘和暂存目录；镜像/角色/日志含私有数据，不上传公开仓库。

`bootstrap.sh`仅为一次性测试镜像初始化入口，不能装到用户生产主机作为init。它修正无特权导出文件属主，安装源码/依赖，创建服务用户和UTF8私有PG副本。`mgar-prepare.service`在数据库与Docker就绪后恢复角色/数据库/CAS；角色SQL由root读取，经stdin送给postgres，保持文件0600。

## 执行

先构建私有镜像并准备路径；全新镜像至少有3GiB逻辑容量，所在文件系统必须有足够实际空间。不要清理原卷或通用Docker镜像来挤空间。内核、initrd、Docker/沙箱镜像和私有恢复副本应先校验SHA256。第一次复制完成后，安装共享盘仅作为只读材料，不承担业务持久化。

```bash
.venv/bin/python deploy/host-acceptance/run_vm.py \
  --tools /path/to/extracted-qemu-tools \
  --image /private/path/mgar-node.raw \
  --kernel /path/to/vmlinuz-6.1.0-54-amd64 \
  --initrd /path/to/initrd.img-6.1.0-54-amd64 \
  --share /private/path/installation-share \
  --evidence .local/host-acceptance/UNIQUE-ATTEMPT
```

证据目录必须是新目录，禁止覆盖失败。工具路径内含QEMU、SeaBIOS和动态库；镜像必须已配好原服务单元及这三个验收单元。启动器只终止自己创建的QEMU进程；不会停止云执行环境或原数据库。

## 实际断言

1. PID1为systemd、虚拟化为qemu；用户、权限、环回端口、无网卡、原633次账本/17迁移/PG刷盘设置；以mgar用户执行真实非root/禁网Go沙箱，再执行真实HTTP fixture工作流和报告。
2. PG中分别写入已提交探针和保持未提交的探针；确认后强制SIGKILL整台QEMU。新虚拟机必须有不同boot_id，PG自动崩溃恢复，已提交状态、报告、CAS与去重保留，未提交行消失。
3. systemctl正常整机重启后再次检查自动启动与原数据。
4. 独立Engine进程持有唯一写锁，在隔离仓库下建立明确controlled、从未发送的预占。强制断电后由原API启动恢复：预占变unknown且次数/金额不清零，Run暂停、epoch递增，旧Worker拒绝、未知请求不能恢复重发。原633次真实账本保持不变。
5. pg_dump恢复到第二个UTF8库，保留全局Reader角色和只读权限、未知预占与CAS；由真实systemd API连接恢复库，检查原报告和去重。

只有所有断言实际通过才生成PASS。失败日志保留；维护启动、修复缓存或首次失败不能计作通过。结果范围仅为该隔离虚拟机，不覆盖用户生产宿主、物理存储掉电或多节点HA。

## TCG 缓存准备与恢复验证

TCG 首次编译 Go 测试标准库依赖可能超过原45秒边界。`warm_test_cache.py` 在离线可信准备阶段构建 `testing/internal/testdeps`；审计 DockerDiff 仅允许 Go 缓存和本地编译统计目录，保留编译器、可信入口及原运行限制。新镜像只用于测试VM，单独固定 image_id；不能覆盖原云端沙箱锁或改写模型评测成绩。

```bash
.venv/bin/python deploy/host-acceptance/warm_test_cache.py \
  --base-lock .local/r5/sandbox-lock.json \
  --output-lock /private/path/host-sandbox-lock.json
```

共享盘 `private/` 还必须包含与镜像锁 `astcheck_sha256` 匹配的可信 `astcheck` 二进制；初始化检查哈希后安装给服务账号。最后的 `runtime-assets` 阶段以实际服务用户验证允许函数体修改、拒绝导入修改，同时检查恢复库探针和CAS实际字节。首次省略该资产的问题保留在修复档案。

Reader 的正式实现通过连接选项与事务启用只读，而不是依赖角色默认值；恢复验收必须使用 `DataReader`，并额外验证关闭会话只读后数据库 ACL 仍拒绝 INSERT。角色、私有Reader配置、SQL与CAS必须一起保留。监督器等待正常关机/重启时持续读取串口，防止QEMU控制台背压拖住关机，最多300秒；沙箱执行上限仍为45秒。只有各阶段、4个不同boot_id及最终正常关机均确认后才写PASS。

已执行验收的旧阶段不因修复监督器而重放。失败、原始日志哈希、精确续跑位置与新旧脚本哈希均在 [宿主修复档案](../../docs/repairs/R6-HOST-20261105/README.md) 中记录。
