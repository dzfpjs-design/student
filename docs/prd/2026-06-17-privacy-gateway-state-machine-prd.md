# Privacy Gateway 状态机 PRD

日期：2026-06-17
来源：五专家设计评审 P0-3

## 1. 目标

把“隐私暂停”和“原始音视频默认不上云”从原则变成可验证工程状态机。

## 2. Privacy State

| 状态 | 含义 | 允许采集 | 允许上传 |
|---|---|---|---|
| `normal` | 授权内正常工作 | 允许必要采集 | 只上传低敏摘要或授权材料 |
| `local_only` | 本地处理模式 | 允许本地采集 | 禁止原始媒体上传 |
| `paused` | 用户暂停 | 禁止摄像头/麦克风采集 | 禁止新增上传 |
| `hardware_blocked` | 物理遮挡/静音 | 驱动层停止或空数据 | 禁止上传 |
| `sensitive_rejected` | 敏感过滤拒绝 | 当前帧/片段拒绝 | 只记录拒绝原因 |
| `degraded` | 某链路不可验证 | 降级到更严格模式 | 禁止高敏上传 |

## 3. 端到端暂停链路

隐私暂停必须按以下顺序执行：

1. 本地入口接收：平板按钮、语音、底座按键、家长端。
2. 底座 Device Manager 设置 `privacy_pause_requested`。
3. Camera Service 停止 V4L2 capture。
4. Audio Service 停止 ALSA capture 或硬件 mute。
5. 清空或冻结 ring buffer。
6. Privacy Gateway 撤销未完成租约。
7. Local Event Store 冻结高敏上传队列。
8. 状态灯/屏幕 UI 更新。
9. 写入 `privacy_state_changed` 和 `privacy_decision`。

预算：

| 环节 | P95 预算 |
|---|---:|
| 本地输入到服务收到 | 50ms |
| 服务到驱动 stop/mute | 100ms |
| buffer 清理/冻结 | 50ms |
| UI/状态灯 ACK | 100ms |
| 总计 | <= 300ms |

## 4. privacy_decision 数据模型

建议新增逻辑表或事件：

```json
{
  "privacy_decision_id": "uuid",
  "request_id": "uuid",
  "source": "tablet|dock|pendant|cloud",
  "capability": "camera|audio|pendant_snippet|homework_image",
  "input_summary": {
    "media_type": "image|audio|event",
    "duration_ms": 0,
    "resolution_bucket": "none|low|medium|high"
  },
  "checks": {
    "hardware_state": "open|covered|muted",
    "consent_snapshot_id": "uuid",
    "policy_version": "string",
    "sensitive_scene": "pass|reject|unknown",
    "third_party_presence": "none|possible|unknown"
  },
  "action": "allow_local|allow_upload_summary|allow_upload_media|reject|delete",
  "retention_until": "timestamp|null",
  "created_at": "timestamp"
}
```

## 5. 队列规则

- `paused` 状态下不得创建新的高敏上传任务。
- 进入 `paused` 前已在队列中的高敏任务必须冻结并重新评估。
- 删除请求优先级高于补传。
- `sensitive_rejected` 不保存原始帧，只保存拒绝原因。

## 6. 验收

- UI 显示暂停后，底座采集 pipeline 不再产生新 frame/audio buffer。
- 断网暂停后，恢复联网不会补传暂停期间高敏数据。
- 每次上传都有对应 `privacy_decision_id`。
- `privacy_decision.action=reject` 的媒体对象不存在可访问原文。
