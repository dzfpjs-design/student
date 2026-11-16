# Android 专用设备管控 PRD

日期：2026-06-17
来源：五专家设计评审 P0-5、P0-6

## 1. 目标

证明专用 Android 学习平板“管得住”：孩子不能绕过 Agentic OS、App Session、家长规则、娱乐时长和隐私边界。

## 2. Android 基线

| 项目 | v0.1a 要求 |
|---|---|
| Android 版本 | Android 13+ 优先，ODM 需承诺安全补丁 |
| 管控模式 | Device Owner + 自家 DPC + Agentic Launcher |
| App 模式 | Lock Task + allowlist |
| 调试 | ADB 默认关闭，开发者选项不可达 |
| 安装 | 禁止孩子安装、侧载、卸载 |
| 浏览器 | 不提供开放浏览器，网页进入受控 WebView |

## 3. 绕过面矩阵

| 绕过面 | 策略 | 验收 |
|---|---|---|
| Home/Back/Recents | 默认回 Agentic Launcher | 100 次按键无普通桌面 |
| 通知 PendingIntent | 非白名单通知禁用或拦截 | 通知不能打开外部 App |
| 分享面板 | 禁用系统分享或仅白名单 | 不能跳到浏览器/社交 |
| 文件选择器 | 禁用或沙箱化 | 不能浏览系统敏感目录 |
| WebView 外链 | URL allowlist + shouldOverrideUrlLoading | 外链不能打开浏览器 |
| Deep Link | 只允许 session token 绑定入口 | 无 token 拒绝 |
| ADB/USB 调试 | 量产关闭 | 插 USB 不出现调试授权 |
| Settings | 限制网络、账号、应用、恢复出厂 | 孩子 PIN 不可进入 |
| Safe Boot | 禁用或启动后自动恢复策略 | Safe Boot 不绕过 Launcher |
| Factory Reset | FRP/设备绑定恢复 | 重置后仍需家长重新激活 |
| 无障碍服务 | 禁止第三方开启 | 无法用辅助功能自动点击 |
| 多窗口/PiP | 禁用或受控 | 视频不能悬浮逃逸 |

## 4. App Session Enforcement

所有内容入口必须先创建 App Session：

```json
{
  "session_id": "uuid",
  "activity_type": "learning|story|game|video|rest",
  "content_container": "native|webview|sdk|allowlisted_app",
  "duration_budget_seconds": 1200,
  "policy_version": "string",
  "session_token": "signed-token",
  "expires_at": "timestamp"
}
```

执行规则：

- 无 `session_token` 不允许打开内容。
- 白名单 App 必须 heartbeat。
- 超时后 DPC 强制回 Launcher。
- 外部 Intent 必须被 Session Gate 校验。
- WebView JS Bridge 只能访问当前 session scope。
- SDK 必须实现 start/pause/end/heartbeat 回调。

## 5. 攻击性测试

v0.1a 必须新增：

- `QA-ADV-001` 通知跳转攻击。
- `QA-ADV-002` 分享面板逃逸。
- `QA-ADV-003` WebView 外链逃逸。
- `QA-ADV-004` ADB/开发者选项。
- `QA-ADV-005` Safe Boot/重启 race。
- `QA-ADV-006` 恢复出厂后策略恢复。
- `QA-ADV-007` 白名单 App 无 heartbeat。
- `QA-ADV-008` Session 超时但内容继续播放。

退出标准：所有 ADV 测试 100% 通过。
