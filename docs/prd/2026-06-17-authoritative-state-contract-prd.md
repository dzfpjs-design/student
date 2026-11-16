# 权威状态源与端云边契约 PRD

日期：2026-06-17
来源：五专家设计评审 P0-2

## 1. 目标

定义 App Session、家长规则、授权、隐私暂停、设备租约、离线缓存、补传和删除的权威状态源，避免平板、底座、吊坠和云端各自判断。

## 2. 状态权威表

| 状态 | 权威源 | 副本 | 离线 TTL | 冲突策略 |
|---|---|---|---:|---|
| family/account | 云端 Identity | 平板、底座 | 7 天 | 云端胜出 |
| child_profile | 云端 Profile | 平板、底座 | 7 天 | 云端胜出，本地只读 |
| guardian_rule | 云端 Rule Service | 平板、底座 | 24 小时 | 版本号高者胜，撤回/禁用优先 |
| consent_snapshot | 云端 Consent Service | 平板、底座 | 24 小时 | 撤回优先，过期则降级 |
| privacy_pause | 最近本地硬状态优先 | 云端记录审计 | 立即生效 | 任一端暂停即全局暂停 |
| app_session | 平板 Agentic Runtime | 云端、底座 | 当前会话 | 平板为执行权威，云端为审计权威 |
| device_lease | 底座 Device Manager | 平板 | 30-300 秒 | 过期即无效，不自动续约 |
| local_event | 产生端 | 云端摘要 | 24 小时以上 | client_event_id 去重 |
| deletion_request | 云端 Compliance | 平板、底座 | 永不过期直到完成 | 删除优先于分析和训练 |
| model_version | 云端 Model Registry | 平板、底座、吊坠 | 7 天 | 发布 manifest 约束兼容 |

## 3. 版本字段

所有跨端状态必须包含：

```json
{
  "state_id": "uuid",
  "state_type": "app_session|rule|consent|privacy|lease|delete",
  "version": 12,
  "owner": "cloud|tablet|dock|pendant",
  "issued_at": "timestamp",
  "expires_at": "timestamp|null",
  "supersedes": "uuid|null",
  "policy_version": "string",
  "consent_snapshot_id": "uuid",
  "trace_id": "uuid"
}
```

## 4. 能力租约

平板调用底座高敏能力前必须获得租约。

```json
{
  "lease_id": "uuid",
  "capability": "camera_capture|gimbal_move|audio_context|pendant_sync",
  "scope": ["desk_scan"],
  "session_id": "uuid",
  "request_source": "tablet_agent",
  "nonce": "base64",
  "issued_at": "timestamp",
  "expires_at": "timestamp",
  "policy_hash": "sha256",
  "consent_snapshot_id": "uuid",
  "privacy_decision_id": "uuid",
  "signature": "base64"
}
```

规则：

- 租约 TTL 默认 30 秒，预览类最长 300 秒。
- 租约不可转让，不可跨 session 使用。
- 隐私暂停、授权撤回、会话结束立即使租约失效。
- 底座必须校验 mTLS 设备身份、nonce、TTL、scope、session_id、policy_hash。

## 5. 离线冲突策略

| 场景 | 行为 |
|---|---|
| 家长撤回授权但平板离线 | 平板继续使用本地 TTL 内低敏能力；高敏能力过期即禁用 |
| 底座离线 | 平板禁用底座云台/麦阵列能力，保留本地学习与手动拍题 |
| 云端不可用 | 使用最近稳定计划，不生成新报告，不上传高敏数据 |
| 删除请求下发后底座离线 | 底座联网后优先执行删除，再补传低敏摘要 |
| 规则版本冲突 | 禁用/更严格规则胜出 |
| 设备时间漂移 | 云端时间校正，保留 device_time 和 server_time |

## 6. 审计事件

必须新增或统一：

- `state_conflict_resolved`
- `lease_issued`
- `lease_rejected`
- `lease_revoked`
- `privacy_state_changed`
- `offline_policy_applied`
- `deletion_priority_applied`

## 7. 验收

- 所有高敏能力调用都有 `lease_id`、`consent_snapshot_id`、`privacy_decision_id`。
- 离线撤回授权后，高敏能力在 TTL 到期后不可用。
- 任一端隐私暂停后，新的云台、摄像头、麦克风租约全部拒绝。
- 删除请求不会被离线补传覆盖。
