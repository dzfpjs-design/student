# 底座 Runtime、安全启动与 OTA PRD

日期：2026-06-17
来源：五专家设计评审 P0-7、P0-8

## 1. 目标

把底座从“运行若干服务的 Linux 主机”定义为可量产、可恢复、可审计、可安全更新的边缘系统。

## 2. 服务运行时

| 服务 | 启动顺序 | Restart | Watchdog | 降级策略 |
|---|---:|---|---|---|
| device-manager | 1 | always | 5s | 进入 fault |
| local-event-store | 2 | always | 10s | 禁止补传，保留采集暂停 |
| privacy-gateway | 3 | always | 5s | 高敏能力全部拒绝 |
| camera-service | 4 | on-failure | 5s | 禁用云台/拍题 |
| audio-service | 4 | on-failure | 5s | 禁用远场语音 |
| pendant-sync | 5 | on-failure | 10s | 延迟同步 |
| tablet-local-api | 6 | always | 5s | 平板显示底座降级 |
| ota-agent | 7 | on-failure | 30s | 禁止自动更新 |

要求：

- 使用 systemd 或等价 supervisor。
- 每个服务提供 `/healthz` 和 `/readyz`。
- cgroup 限制 CPU/RAM/IO。
- Privacy Gateway 挂掉时默认拒绝所有高敏能力。

## 3. IPC 与日志

- 本机服务间使用 Unix domain socket 或 loopback mTLS。
- 所有请求带 `trace_id`、`request_id`、`session_id`。
- 日志结构化，默认脱敏。
- 诊断包必须家长授权上传。
- crash dump 默认禁用或本地加密保存。

## 4. 安全启动链

```text
BootROM -> SPL -> U-Boot -> kernel -> rootfs -> service bundle -> model/rule bundle
```

要求：

- 每一层签名验证。
- 量产关闭调试口。
- Secure Element 或 TEE 保存设备私钥。
- 证书支持吊销与轮换。
- 反回滚计数防止降级攻击。
- 工厂 provisioning 写入设备证书、序列号、家庭绑定前空状态。

## 5. OTA

推荐采用 RAUC/Mender/SWUpdate 类 A/B 或等价方案。

Bundle manifest：

```json
{
  "bundle_id": "uuid",
  "target": "dock|pendant|tablet|model|rule",
  "version": "1.2.0",
  "requires": {
    "dock_os": ">=1.1.0",
    "privacy_gateway": ">=1.4.0"
  },
  "components": [
    {"name": "camera-service", "version": "1.2.0", "sha256": "..."}
  ],
  "signature": "base64"
}
```

流程：

1. preflight：电量、电源、存储、网络、版本依赖。
2. 下载并校验签名。
3. 写入 inactive slot。
4. 重启或服务切换。
5. health gate 通过后标记 boot success。
6. 失败自动回滚。

验收：

- OTA 断电不变砖。
- 服务回滚时模型和规则也回滚到兼容版本。
- 新版本 P0 隐私测试失败时灰度熔断。
