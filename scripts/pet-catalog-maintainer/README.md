# 离线维护已审核的 PET 原音模型与价格目录

这是项目维护者工具，不是产品运行时依赖，也不是阿里云官方签发工具。它不会抓取网页、续期、创建密钥、导出私钥、启用模型或代替家长同意。

本目录包含已审 Swift 源码和固定 macOS arm64 编译产物。仅在当前已建专用 Keychain 密钥的维护机上使用。其他机器或重建工具前，必须重新审阅源码与生成物、安排公开根轮换并验证两端；不要自行运行底层 binary 的 `bootstrap` 或自动信任它返回的公钥。专用密钥缺失或 Keychain 拒绝访问时停止，不创建替代密钥。

## 审核并准备公开 payload

人工核对官方模型、价格、计量和协议资料，保留公开证据及 SHA，再准备符合生产 `pet_audio_catalog.py` 完整合同的 canonical ASCII JSON。字段、固定 endpoint/region/protocol、模型快照、有效日期、整数向上计价都由原生产合同校验。日期与费率必须是审核事实；本工具绝不改写它们。首次公开包保存在 `resources/pet-audio-catalog/initial-reviewed-catalog.signed.json`，原有效期为 `2026-10-24T05:04:30Z` 至 `2026-11-23T05:04:30Z`，不可因复制或重建延长。

从仓库根目录，用项目既有 venv（不安装新依赖）：

```sh
.venv/bin/python -I -B scripts/pet-catalog-maintainer/publish-reviewed.py --payload /absolute/public/reviewed-payload.json --output /absolute/public/new-catalog.signed.json
```

默认仅预验，不访问 Keychain、不输出签名包。预验使用一次内存合成密钥调用完整生产验签合同，不把该合成根作为产品信任根。现有源、binary 或合同与固定审核 SHA 不符、路径含别名、输出已存在、合同或日期无效时停止。

人工确认审核后，再显式加 `--sign`：

```sh
.venv/bin/python -I -B scripts/pet-catalog-maintainer/publish-reviewed.py --payload /absolute/public/reviewed-payload.json --output /absolute/public/new-catalog.signed.json --sign
```

包装器只向固定已审核 signer 传递原 payload stdin，调用一次，最多等待 20 秒，不自动重试。超时报告 `SIGNER_RESULT_UNKNOWN_NO_RETRY`，不发布输出，也不宣称签名自然完成；先人工调查，不再调用 signer。私钥只由该 signer 在 Keychain/内存内使用。返回包必须通过生产默认编译公开根验签，且 payload 逐字等于输入，才以同目录临时文件、fsync、原子 no-clobber 发布。失败只清理由本次独占创建且身份仍一致的临时文件；不删除或覆盖已有输出。若发布已发生但最终目录同步失败，会报告失败并保留公开输出，先检查该输出，不用覆盖或重试掩盖不确定性。

工具不激活目录。产品中仍须家长 PIN → 原生选择文件 → 可读预览 → 原 CAS 保存 → 新的独立同意；旧 grant、已发工作结算、DPAPI 反回滚栅栏与原撤权门均不变。签名仅说明项目维护者审核了该公开合同，不是模型质量、音素/韵律评分准确率或儿童效果证明。
