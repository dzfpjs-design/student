# 家庭学习伙伴

本仓库包含桌面客户端、本地学习服务和自动化测试。家长配置的模型地址、密钥、家庭资料和需单独授权的素材不放在公开仓库中。

构建及恢复私有资源请参阅 [PRIVATE_RESOURCES.md](PRIVATE_RESOURCES.md) 和 [Windows 发布说明](docs/development/windows-release-runbook.md)。知识图谱及教材审核状态见 [知识图谱文档](docs/knowledge_graph/)。

维护者签名工具是单独恢复的私有 macOS arm64 文件；原维护机 Keychain 内的签名密钥不会随工具或仓库迁移。

当前版本仍有已知测试缺口：在迁移前工作区与公开候选上运行 `npx vitest run src/main/household-setup-defaults.test.ts src/renderer/src/SessionWorkspaceTaskListening.test.tsx`，两边均为 51 项失败、49 项通过，失败用例名称一致。这些失败尚未解决；Windows 真机及真实家庭场景也未由本次迁移验收。

模型路由测试已针对本次专用讲题端点的契约更新合成配置；自动测试通过并不代表真实云端或 Windows 实机通过。
