# 家庭学习伙伴

本仓库包含桌面客户端、本地学习服务和自动化测试。家长配置的模型地址、密钥、家庭资料和需单独授权的素材不放在公开仓库中。

构建及恢复私有资源请参阅 [PRIVATE_RESOURCES.md](PRIVATE_RESOURCES.md) 和 [Windows 发布说明](docs/development/windows-release-runbook.md)。知识图谱及教材审核状态见 [知识图谱文档](docs/knowledge_graph/)。

维护者签名工具是单独恢复的私有 macOS arm64 文件；原维护机 Keychain 内的签名密钥不会随工具或仓库迁移。

当前版本仍有已知测试缺口：迁移前两份目标测试共有 51 项失败。启动夹具修复后，定向复验剩余 47 项失败，均为待迁移的旧分段语音测试；不能据此宣称语音功能通过。Windows 真机及真实家庭场景也未由本次迁移验收。

模型路由测试已针对本次专用讲题端点的契约更新合成配置；自动测试通过并不代表真实云端或 Windows 实机通过。

本地原生任务集成测试默认在 `npm test` 中**跳过**，Vitest 会显示跳过数量；跳过不代表验证通过。它需要已恢复的测试私有资源、Node 依赖以及已安装 `services/local_agent` 依赖的 Python 虚拟环境。准备完成后运行 `npm run test:integration:native-task`；默认使用项目 `.venv` 的 Python，也可通过 `HSC_TEST_PYTHON` 指定解释器。该命令显式开启门控；Python 缺失、后端启动失败或测试失败时返回非零。测试仅使用临时合成数据、loopback 后端与 SQLite，不调用真实云模型，也不验证实麦或 Windows 设备。

例如，在 macOS/Linux 上可用以下命令准备本地服务依赖；Windows 下使用 `.venv\Scripts\python.exe` 执行相同的 `pip install` 命令：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e './services/local_agent[dev]'
npm run test:integration:native-task
```
