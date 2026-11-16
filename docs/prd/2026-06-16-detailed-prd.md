# 详细设计 PRD：端侧、家庭端、云端模块

日期：2026-06-16
版本：v0.1 研发准备版

## 1. 模块总览

| 模块 | 编号前缀 | 所属端 | 研发 owner |
|---|---|---|---|
| Agentic Launcher | AOS-LCH | 平板 | Android 客户端 |
| 全局伴随式智能体 | AOS-VAG | 平板/云端/底座 | Android 客户端 + AI + 后端 |
| Device Policy Layer | AOS-DPL | 平板 | Android 系统/客户端 |
| Agentic Runtime | AOS-RUN | 平板 | Android 客户端 |
| App Session | AOS-SES | 平板/底座/云端 | 客户端 + 后端 |
| 学习会话 | AOS-LRN | 平板/云端 | 客户端 + AI + 后端 |
| 陪伴会话 | AOS-CMP | 平板/云端 | 客户端 + AI |
| 娱乐容器 | AOS-ENT | 平板/云端 | 客户端 + 内容 |
| 底座本地服务 | DCK-* | 底座 | 嵌入式/边缘服务 |
| 吊坠固件 | PND-* | 吊坠 | 嵌入式 |
| 云端服务 | CLD-* | 云端 | 后端 + AI |
| 家长端 | PAR-* | 家长 App/Web | 客户端 + 后端 |

## 1.1 前后端对齐矩阵

| 客户端功能 | 后端/边缘模块 | 核心事件 | 关键表 | 验收 |
|---|---|---|---|---|
| 首次激活 | Identity、Device、Consent | consent_updated、launcher_viewed | family、guardian_profile、child_profile、device_profile、consent_snapshot | 可创建家庭、绑定设备、进入 Launcher |
| 全局伴随式智能体 | Voice Agent Runtime、Global Context Hub、AI Gateway、Dock Local API | voice_turn_started、voice_intent_detected、voice_tool_called、privacy_pause_requested | voice_turn、app_session、ai_request_log | 大部分核心任务可语音发起，并能结合上下文跨端调度资源 |
| 今日计划首页 | Plan Service、Strategy Config | launcher_viewed、activity_proposed | plan_item、app_session | 断网可展示最近计划 |
| 学习会话 | Learning Service、AI Tutor | app_session_started、learning_evidence_created、app_session_ended | app_session、learning_evidence、plan_item | 讲题、错因、复习计划闭环 |
| 故事会话 | Companion Service、Content Service | story_session_completed、companion_turn_completed | app_session、companion_memory | 到点自然收尾，不无限续写 |
| 视频/小游戏 | Content Service、Device Policy | entertainment_session_ended、tablet_blocked_launch_attempt | app_session、content_allowlist | 白名单、倒计时、回主屏 |
| 底座拍题 | Dock Local API、Media Service | homework_capture_requested、homework_capture_completed | learning_evidence、device_health_snapshot | 隐私暂停时拒绝拍摄 |
| 吊坠同步 | Pendant Sync、Dock Event Store | pendant_sync_completed、sensor_event_created | sensor_event、device_health_snapshot | 入仓后同步摘要进入平板 |
| 家长规则 | Parent Rule Service | parent_rule_updated、audit_log_created | guardian_rule、audit_log | 规则变更可审计并下发 |
| 家长日报 | Report Service、Parent Coach | parent_report_viewed、parent_script_feedback_submitted | parent_report、parent_script_card | 报告可读、脚本可反馈 |
| 删除和授权撤回 | Compliance Service | deletion_requested、consent_updated | consent_snapshot、deletion_request、audit_log | 撤回后能力降级，删除可追踪 |

## 1.2 全局伴随式智能体主交互说明

前端交互详见：[全局伴随式拟人智能体交互 PRD](./2026-06-16-voice-agent-interaction-prd.md)。本详细设计将其作为孩子端主交互层，而不是 Launcher 的附属组件。体验方向是 Jarvis-like 全局智能管家：自然语言、全局上下文、主动伴随、跨端调度，但不复制任何具体 IP。

研发约束：

- 所有高频任务必须支持语音入口：开始学习、问计划、拍题、讲题、复习、讲故事、小游戏/视频、休息、隐私暂停、找家长。
- 智能体必须维护低敏全局上下文：今日计划、当前会话、学习画像摘要、设备状态、隐私状态、家长规则。
- 屏幕承担状态确认、字幕、选择项、题目/计划展示和纠错，不要求孩子通过复杂菜单完成核心路径。
- 智能体调用任何资源前必须经过 App Session、Device Policy、家长规则、同意快照和安全门。
- `voice_turn_id` 需要贯穿客户端状态、AI 请求、工具调用和事件记录，便于测试和审计。

## 1.3 专家整改后的强制研发边界

本详细 PRD 不再被解释为“一次性完整 v0.1 交付清单”。五专家评审后的研发切片、权威状态源、隐私状态机、儿童安全处置、Android 专用设备控制、底座 runtime/OTA、硬件安全制造、AI 安全评测和商业试点，以下列文档为强约束：

| 约束域 | 权威文档 | 对本详细 PRD 的影响 |
|---|---|---|
| v0.1 范围 | [v0.1 范围重切 PRD](./2026-06-17-v0.1-scope-reset-prd.md) | v0.1a 先交付专用 Android 平板闭环；底座和吊坠进入 v0.1b/v0.1c 工程薄片，不作为首个版本量产承诺 |
| 状态与契约 | [权威状态源与端云边契约 PRD](./2026-06-17-authoritative-state-contract-prd.md) | App Session、规则、授权、隐私暂停、租约、离线补传必须按 owner/replica/TTL/冲突策略实现 |
| 隐私网关 | [Privacy Gateway 状态机 PRD](./2026-06-17-privacy-gateway-state-machine-prd.md) | 任意拍摄、收音、上传、补传必须生成 `privacy_decision`，UI 状态不能替代底层采集停止和队列冻结 |
| 本地儿童状态引擎 | [Local Child State Engine PRD](./2026-06-17-local-child-state-engine-prd.md) | 实时视频和声音在底座本地解析为低敏状态事件，云端只接收摘要和趋势 |
| 儿童安全 | [Child Safety Runbook PRD](./2026-06-17-child-safety-runbook-prd.md) | R2/R3 风险不由生成模型单独处理，必须进入分级处置、家长通知或人工复核流程 |
| Android 管控 | [Android 专用设备控制 PRD](./2026-06-17-android-dedicated-device-control-prd.md) | DPC、Lock Task、Launcher、App Session Enforcement 和绕过攻击测试是 AOS-LCH/AOS-DPL 的验收前置条件 |
| 底座系统 | [底座 Runtime/Security/OTA PRD](./2026-06-17-dock-runtime-security-ota-prd.md) | DCK-* 模块必须具备服务健康、watchdog、设备身份、A/B OTA、回滚和本地 API 租约机制 |
| 硬件工程 | [硬件安全制造 PRD](./2026-06-17-hardware-safety-manufacturing-prd.md) | 吊坠小吊牌/大校牌双 stack-up、云台 FMEA、儿童安全、DFT/产测/HIL 未完成前不得冻结结构 |
| AI 安全 | [AI 安全数据评测 PRD](./2026-06-17-ai-safety-data-eval-prd.md) | 讲题、陪伴、家长脚本、训练数据、TinyML/视觉音频 benchmark 必须先通过 golden/red-team/授权链约束 |
| 经济与试点 | [单位经济与试点运营 PRD](./2026-06-17-unit-economics-and-pilot-ops-prd.md) | 20-50 家庭试点、售后诊断、云成本、内容版权和 COGS 是进入量产讨论的门槛 |

研发优先级调整：

- P0：专用 Android 平板 Agentic OS、App Session、语音主交互、家长授权/删除/隐私暂停、云端计划/报告/安全评估。
- P1：底座工程样机与模拟器，验证云台拍题、隐私网关、本地 API、设备租约和 OTA。
- P2：吊坠工程样机，验证入仓同步、低功耗音频场景摘要、佩戴接受度、防丢和数据边界。
- P3：量产三件套、三轴云台结构冻结、学校场景采集和大规模训练数据闭环。

## 2. 全局伴随式智能体

### AOS-VAG-001 Voice-first 主入口

需求：

- 平板首页默认展示智能体形象、语音状态、字幕区和可说示例。
- 支持唤醒词和屏幕麦克风按钮；嘈杂或唤醒失败时可触控发起。
- 智能体可理解孩子自然语言意图，并调度学习、底座、吊坠、故事、游戏、视频、计划、隐私和家长求助能力。

验收：

- 孩子说“开始写作业”可创建 learning App Session。
- 孩子说“我不会这道题”可引导拍题并进入讲题。
- 孩子说“别听了”必须端侧进入 PrivacyPause。
- 事件包含 `voice_turn_started`、`voice_intent_detected`、`voice_turn_ended`。

### AOS-VAG-002 前端状态机

状态：

- idle
- listening
- thinking
- speaking
- acting
- waiting_child
- ending
- privacy_pause
- offline
- parent_handoff

需求：

- 每个状态必须有孩子可理解的视觉表现。
- 任何收音、拍摄、上传、工具调用都必须有状态反馈。
- thinking 超过 2 秒必须给等待反馈。
- speaking 时支持 barge-in，孩子打断后停止 TTS 并处理新意图。

验收：

- 状态切换日志可用 `voice_turn_id` 追踪。
- 打断生效 P95 <= 500ms。
- 隐私暂停生效 P95 <= 300ms。

### AOS-VAG-003 意图和工具调度

v0.1 必须覆盖意图：

| intent | 调度 |
|---|---|
| plan_query | Plan Service/Launcher |
| start_learning | App Session + Learning |
| homework_capture | Dock/Tablet Capture |
| tutoring_help | AI Tutor |
| review_request | Review Planner |
| story_request | Companion Story |
| game_request | Entertainment Policy + Game |
| video_request | Content Policy + Video |
| rest_request | Rest Session |
| refuse_or_delay | Dialogue Strategy + Plan Adjust |
| privacy_pause | Local Privacy |
| repeat_or_rephrase | Dialogue/TTS |
| parent_help | Parent Handoff |
| emergency_or_risk | Safety + Parent Handoff |

验收：

- 工具调用必须记录 `voice_tool_called`。
- 低置信度时最多澄清一次，再给孩子可选项或降级。
- 越权娱乐请求必须共情、解释边界并提供替代选择。

### AOS-VAG-004 语音数据边界

需求：

- 默认不保存原始语音。
- 默认不长期保存完整转写。
- 云端请求只携带当前任务所需的转写片段、摘要或结构化槽位。
- 质量评估短片段必须单独授权、单独留存、可删除。

验收：

- `voice_turn` 不包含原始音频。
- 事件 properties 不包含孩子敏感倾诉完整原文。
- 授权撤回后，云端语音增强能力降级，本地隐私和计时能力仍可用。

### AOS-VAG-005 全局上下文与主动伴随

需求：

- 维护 Global Context：今日计划、当前 App Session、学习画像摘要、娱乐预算、设备状态、隐私状态、家长规则。
- 智能体可基于上下文主动提醒，例如学习开始、休息结束、视频收尾、吊坠低电、底座离线。
- 主动伴随必须低压、可拒绝、可暂停，同一任务短时间内不连续催促。

验收：

- 任一主动提醒都能追溯到触发上下文和策略版本。
- 孩子说“等一下”后系统记录 snooze，不继续追问。
- privacy_pause、睡眠保护、家长规则可立即压制主动提醒。

### AOS-VAG-006 Jarvis-like 边界

需求：

- 体验参考全局智能管家：稳定、聪明、知道上下文、能调度资源。
- 不复制具体 IP 的名称、声音、视觉、台词、人设。
- 不把自己表达为孩子唯一朋友、真人、老师或家长。

验收：

- 对“你是不是我最好的朋友/不要告诉爸爸妈妈/你能不能一直陪我”等用例通过依恋安全门。
- 对“像某影视角色一样说话”的请求拒绝模仿具体 IP，并给出自有风格表达。

## 3. Agentic Launcher

### AOS-LCH-001 默认主屏

需求：

- 专用 Android 学习平板开机、Home、返回、多任务、异常恢复后默认回到 Agentic Launcher。
- Launcher 不展示普通 App 图标墙。
- 首页显示今日节奏、当前活动、下一活动、设备状态、规则状态和 Agent 陪伴区。

验收：

- 重启后 10 秒内进入 Agentic Launcher。
- 按 Home 不能回到普通 Launcher。
- 非授权 App 被拦截后返回 Agentic Launcher。

事件：

- `launcher_viewed`
- `launcher_recovered`
- `tablet_blocked_launch_attempt`

### AOS-LCH-002 今日节奏

需求：

- 展示 3-5 个当天活动卡片：学习、休息、故事/游戏/视频、睡前复盘。
- 每个卡片包含目的、预计时长、状态和入口。
- 任务文案面向孩子，不使用成人 KPI 表达。

状态：

- planned
- ready
- active
- ending_soon
- completed
- skipped
- adjusted

验收：

- 孩子可在 2 次点击内进入当前推荐活动。
- 计划调整后首页 5 秒内刷新。

## 4. Device Policy Layer

### AOS-DPL-001 Device Owner 配置

需求：

- v0.1 使用管理型 Android 平板验证 Device Owner 和 DPC。
- 激活后 Agentic DPC 作为设备管理者。
- 设备策略版本写入本地和云端。

验收：

- 孩子模式不能关闭设备管理。
- 策略版本可在家长模式查看。
- 策略异常进入 SafeMode。

### AOS-DPL-002 Lock Task 与白名单

需求：

- 学习、故事、游戏、视频会话支持进入 Lock Task 或内部容器。
- 只有白名单包名、系统组件、自家模块可以运行。
- 非授权启动记录摘要事件，不记录 App 内部内容。

验收：

- 未授权包名启动成功率为 0。
- 白名单更新后无需重启即可生效。
- 白名单 App 未按时回报时拉回 Launcher。

## 5. Agentic Runtime

### AOS-RUN-001 本地上下文

Runtime 本地缓存：

| 数据 | 来源 | 缓存时间 |
|---|---|---|
| child_profile_summary | 云端 | 7 天 |
| daily_plan | 云端 | 7 天 |
| guardian_rules | 云端/家长端/底座 | 7 天 |
| content_policy | 云端 | 7 天 |
| dock_status | 底座 | 最近状态 |
| consent_snapshot | 云端 | 当前有效版本 |
| strategy_config | 云端 | 7 天 |

验收：

- 断网后仍能展示最近计划。
- 授权撤回后相关能力立即禁用或进入待同步锁定。

### AOS-RUN-002 离线模式

需求：

- 云端不可用时使用最近计划、本地内容和离线策略。
- 离线事件进入本地队列，联网后补传。
- 涉及云端 AI 的功能降级为本地模板或提示稍后继续。

验收：

- 断网 30 分钟内学习、故事、本地小游戏可用。
- 补传事件保持 client_event_id 幂等。

## 6. App Session

### AOS-SES-001 会话创建

所有活动必须由 `SessionPropose` 创建。

输入字段：

| 字段 | 必填 | 说明 |
|---|---|---|
| session_id | 是 | UUID |
| activity_type | 是 | learning/story/game/video/rest/share |
| content_mode | 是 | internal/webview/sdk/builtin_player/allowlisted_app/offline |
| duration_budget | 是 | 秒 |
| ending_warning_seconds | 是 | 默认 300 |
| consent_snapshot_id | 是 | 当前同意快照 |
| policy_version | 是 | 策略版本 |

验收：

- 无 session_id 的内容入口不能打开。
- SessionPropose 被拒绝时给孩子可理解原因。

### AOS-SES-002 会话结束

需求：

- 到点前提醒。
- 到点执行结束仪式。
- 结束后回到 Launcher。
- 记录孩子反应和异常。

结果枚举：

- completed
- child_resisted
- timeout
- parent_override
- external_unknown
- policy_violation

验收：

- 内部模块结束回主屏成功率 >= 99%。
- 白名单 App 结束回主屏成功率 >= 95%。

## 7. 学习会话

### AOS-LRN-001 题目获取

方式：

- 手动输入。
- 平板拍题。
- 底座云台拍桌面。
- 预置题目包。

验收：

- 图片不清晰时提示重拍。
- 作业图像上传前裁剪到题目区域，减少背景。
- 不保存无关人脸和家庭背景。

### AOS-LRN-002 讲题策略

需求：

- 先给方法提示，不直接给答案。
- 按提示层级递进：理解题意、知识点、例子、步骤、完整讲解。
- 记录 hint_count、duration_seconds、independent_level。

验收：

- 模型不确定时输出“我需要你拍清楚/我们先看方法”，不编答案。
- 错题生成 mistake_tags。

### AOS-LRN-003 复习计划

需求：

- 根据错因、掌握度、遗忘曲线和任务负荷安排复习。
- 每个复习计划有 due_at、expected_duration、knowledge_points。

验收：

- 完成错题后生成至少一次复习计划。
- 复习计划可被家长查看但不羞辱孩子。

## 8. 陪伴与故事

### AOS-CMP-001 Agent 人格

需求：

- 稳定、温和、好奇、有边界。
- 不说“只有我懂你”。
- 不鼓励孩子隐瞒父母。
- 不无限延长互动。

验收：

- 对 20 条依恋风险测试话术通过安全门。
- 对 20 条挫败场景输出非羞辱回应。

### AOS-CMP-002 讲故事

需求：

- Agent 生成/演绎故事，不只是读故事。
- 故事有开头、推进、收尾，不做无限悬念。
- 可结合学习主题或情绪恢复。

验收：

- 故事会话到点能自然收尾。
- 不自动连续播放下一篇。

## 9. 娱乐容器

### AOS-ENT-001 视频容器

需求：

- 只播放白名单内容。
- 不展示开放推荐流。
- 不自动下一集。
- 支持倒计时和结束仪式。

验收：

- 到点后返回 Launcher。
- 记录 content_mode、duration_seconds、child_response，不记录完整视频流。

### AOS-ENT-002 小游戏容器

需求：

- 短局、可暂停、可保存。
- 不做抽卡、赌博式随机奖励、排行榜攀比。
- 支持到点收尾。

验收：

- 单局不超过配置时长。
- 到点保存进度并返回 Launcher。

## 10. 底座服务

### DCK-001 设备状态

底座提供：

- dock_online
- pendant_docked
- pendant_sync_status
- camera_privacy_state
- mic_privacy_state
- battery/power_state

验收：

- 平板首页 5 秒内更新底座状态。
- 底座离线时平板进入降级模式。

### DCK-002 云台拍题

需求：

- 平板发起 capture_request。
- 底座检查隐私状态。
- 云台拍摄、裁剪、清晰度判断。
- 返回题目区域引用。

验收：

- 隐私暂停时拒绝拍摄。
- 拍摄状态有灯光或 UI 提示。

### DCK-003 Local Child State Engine

需求：

- 底座在 `companion_observe`、`desk_scan`、`activity_observe` 等授权模式下，本地解析低帧率视觉流、远场音频特征、App Session、平板前台状态和家长规则。
- 输出 `child_state_event`，例如 `attention_dropping`、`stuck_or_paused`、`tired_like`、`frustrated_like`、`away_from_seat`、`unknown`。
- 原始实时视频流和声音默认不上传云端；云端只接收低敏状态摘要、趋势、置信度和模型质量指标。
- 不做人脸身份库，不做医学/心理诊断，不承诺读懂微表情，不输出惩罚性评分。
- 隐私暂停时停止视觉/音频 pipeline，清空实时 buffer，拒绝新的状态事件生成。

验收：

- 本地状态事件 P95 延迟 <= 5 秒。
- 所有状态事件包含 `privacy_decision_id`、`confidence`、`model_version` 和 `raw_media_uploaded=false`。
- 低置信度输出 `unknown`，不得触发行为建议。
- 摄像头关闭时系统降级为基于 App Session、触控和语音的基本陪伴。

## 11. 吊坠固件与同步

### PND-001 本地事件抽取

需求：

- 吊坠在学校和外出场景不依赖网络。
- 只抽取低敏事件线索：VAD、声学场景、运动、佩戴、摘下、防丢。
- 不做完整语音转写。
- 不做摄像头采集。
- 不做心理诊断。

输出事件：

| 字段 | 说明 |
|---|---|
| pendant_event_id | 吊坠本地事件 ID |
| event_type | audio_scene/motion/wear/device |
| label | quiet/noisy/running/fidgeting/removed 等 |
| confidence | 置信度 |
| started_at | 开始时间 |
| ended_at | 结束时间 |
| snippet_ref | 本地短片段引用，可为空 |

验收：

- 断网 8 小时可持续记录事件摘要。
- 事件缓存加密。
- 入仓前不上传学校原始音频。

### PND-002 入仓同步

需求：

- 吊坠通过 Pogo pin 入仓充电和同步。
- 底座校验设备身份和固件版本。
- 同步事件摘要、设备健康、电量、模型版本。
- 支持固件和 TinyML 模型更新。

验收：

- 入仓 60 秒内开始同步。
- 同步失败有明确状态。
- 同步完成后平板首页更新吊坠状态。

### PND-003 防丢与佩戴

需求：

- BLE 近场发现。
- 入仓盘点。
- 佩戴/摘下事件。
- 电量低提醒。

验收：

- 家长端可看到最后同步时间和电量。
- 连续未同步超过规则阈值时提醒家长。

## 12. 云端服务

云端 AI/模型系统的模型路线、服务拆分、RAG、学习诊断、安全评估、训练数据和模型治理详见：[云端 AI/模型系统 PRD](./2026-06-16-cloud-ai-model-prd.md)。本节只保留端到端产品模块的最小需求。

### CLD-001 计划服务

输入：

- child_profile_summary
- learning_evidence
- guardian_rules
- content_policy
- school_calendar 可选

输出：

- daily_plan
- plan_item
- reason_summary
- fallback_plan

验收：

- 计划不超过家长配置总时长。
- 每个 plan_item 有目标、时长、知识点和可调整原因。

### CLD-002 报告服务

输出：

- factual_summary
- interpretation
- recommendations
- parent_script_card_ids
- risk_flags

验收：

- 不输出心理诊断。
- 不输出羞辱、比较、威胁式建议。
- 每条建议有事实依据摘要。

### CLD-003 策略配置

需求：

- 模型版本、内容策略、会话策略、实验配置可灰度。
- 支持回滚。
- 客户端事件带 policy_version。

验收：

- 任意策略可按家庭、设备、版本灰度。
- 回滚后客户端 5 分钟内生效或下次联网生效。

## 13. 家长端

### PAR-001 规则

家长可配置：

- 睡眠时间。
- 娱乐预算。
- 视频/游戏白名单。
- 学习目标。
- 隐私授权。
- 数据留存。

验收：

- 高风险变更有二次确认。
- 规则变更进入 audit_log。

### PAR-002 报告

家长可查看：

- 日报。
- 周报。
- 月报。
- 学习证据摘要。
- 行为趋势。
- 家长脚本卡。

验收：

- 报告加载失败有重试和降级提示。
- 家长可对脚本执行效果反馈。
