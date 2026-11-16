# 私有资源恢复（开发者）

公开源码不包含家庭图片、来源未确认的图像/音频及部分模型权重。构建与测试需要你有权访问项目的私有资源仓库；仅公开仓库本身不是完整的离线安装包。

1. 安装 GitHub CLI，使用拥有私有资源仓库读取权限的账号登录：`gh auth login`。
2. 在公开项目检出目录运行：`node scripts/restore-private-resources.mjs OWNER/PRIVATE_REPOSITORY all`。
3. 运行 `node scripts/check-private-resources.mjs all`，确认界面、测试、原型及模型共 20 项通过。脚本检查私有仓库可见性、资源字节数与固定 SHA-256；文件已存在且内容不同时会停止，不覆盖原有文件。可以重复运行以恢复未完成的项目。
4. 开发界面运行 `npm run dev` 或 `npm run build`；执行前应先运行 `node scripts/check-private-resources.mjs build`。界面测试前运行 `node scripts/check-private-resources.mjs test`；后端完整测试前运行 `node scripts/check-private-resources.mjs backend`，确保离线模型不因缺失而被当作已验收；Windows 打包前运行 `node scripts/check-private-resources.mjs all`。

这 20 个资源包括一张教学场景图、两张纸面测试图像数据、两段文档说明为系统声音合成的测试短音频、九张原型示例图，以及四个 sherpa 双语识别推理文件和两个 BGE 模型文件。部分图像与 sherpa 模型权重的再分发许可尚未核实，因此仅存放私有仓库；不要将其复制到公开 Git 历史或公开 Release。合成签名目录 `src/test-fixtures/pet-reviewed-synthetic.hscatalog` 属于测试数据，由公开的测试用固定密钥生成，不是生产签名密钥。

安装包所需的原子知识库是另一项 1.8 GB 私有资源，不在上述 20 项中。可从私有仓库 `hsc-atomic-baseline-v6-20261116` Release 下载 `atomic-knowledge.db` 及清单，核对大小和清单中的 SHA-256；随后设置 `HSC_ATOMIC_GRAPH_SOURCE` 为已验证数据库路径，再运行 `npm run stage:knowledge:win`。该资源是安装版历史基线，**不是后来完成全部教材复核的知识库**。教材、持续审校工作库及家人数据也不包含在这 20 项中。

恢复脚本仅经过合成错误测试、真实私有 GitHub 下载回读、Mac 本地 Electron 构建和局部测试；目标 Windows 完整安装、离线模型和知识库打包流程仍须逐项验收。模型授权、私有资源访问及新的密钥配置不能由公开克隆自动推断。

维护者目录签名工具的 macOS arm64 可执行文件单独存放在私有 Release，可在原维护机执行 `node scripts/restore-private-resources.mjs dzfpjs-design/student-private maintainer` 恢复并核验。此工具不是运行客户端所必需；恢复脚本会校验固定 SHA-256 并设置执行权限，文件不会进入公开提交。**签名密钥不在该资源或仓库中**，仍须使用原维护机上已存在的专用 Keychain 项；新机器没有密钥时需依照维护者文档审阅并安排信任根轮换，不能声称仅恢复二进制就能签名。
