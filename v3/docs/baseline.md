# V3.1 基线记录

检查日期：2026-11-01。V3 工作区 `/workspace/MAGR-v3`，origin `https://github.com/Loz888/MAGR-v3.git`；开始时本地 work 分支没有提交，git ls-remote origin 成功且无 ref。GitHub 插件随后确认仓库 size=0、default_branch=main。

V2 位于相邻目录 `/workspace/-MGAR-v2-`，实际 HEAD `fe7e04a480142b59394b1aabf91515cd9d44334c`，与方案固定基线一致。开始和复核时 tracked 工作区均无改动。

首次 `bash scripts/test.sh --offline` 退出 1：合成测试数据库未启动。保留首次日志和原始 go.json；启动既有 V2 Compose 测试容器后，完全相同命令退出 0，682 个 Go 测试、453 个 Python 测试通过，无 skip/xfail。此为工程回归，不重写 V2 真实模型失败基线。

V3 使用 Python 3.12.14、独立 .venv/uv.lock、独立 mgar_v3 数据库与 Compose 项目、55433/56379 回环端口。V2 的依赖、迁移、冻结数据和历史证据均未改。

本会话授权覆盖代码实现、必要本地环境和非付费测试。用户另明确授权在 V3 建立验收 Issue，已创建 #1。未发起聊天模型请求；模型目录查询为只读元数据操作。真实业务、代码 push 和生产发布均未宣称完成。

原包布局调整依据见 ADR 0001。V3.1 业务账本权威与独立 Thread 模型覆盖原包相应契约；原包文件原样保留。
