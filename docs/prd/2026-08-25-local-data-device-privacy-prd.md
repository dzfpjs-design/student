# 本地数据、设备与隐私PRD

日期：2026-08-25  
版本：v0.1  
状态：D1-D14研发基线

## 1. 目标

本PRD定义家庭版软件的SQLite数据模型、本地文件、设备接口、传感器生命周期、隐私控制、备份恢复、删除和审计要求。v0.1以家庭电脑为系统记录来源，模型API只处理完成当前任务所需的最小数据。

## 2. 权威状态

| 状态 | 权威来源 | 说明 |
|---|---|---|
| 当前孩子 | 本地`active_child` | 切换前必须保存当前会话 |
| 每日会话 | SQLite`daily_session` | UI状态由数据库恢复 |
| 作业与计划 | SQLite`homework_item`、`plan_item` | 模型只生成候选，不直接成为权威状态 |
| 学习证据 | SQLite`learning_evidence` | 文件只作为证据附件 |
| 传感器状态 | 本地设备管理器 | 云端不得覆盖本地关闭状态 |
| 家庭规则 | SQLite`family_rule` | 修改带版本和审计 |
| 模型与策略 | `model_call`、`policy_version` | 每次输出引用实际版本 |
| 待处理任务 | SQLite`job_queue` | 报告、重试和清理可恢复 |

## 3. 数据库选型

v0.1采用SQLite WAL模式。所有关键状态写入使用事务，外键检查开启。数据库迁移必须版本化并可回滚；应用启动时先完成迁移和完整性检查，再允许孩子开始会话。

## 4. 核心表

### 4.1 身份与规则

| 表 | 核心字段 |
|---|---|
| `family_profile` | family_id、region、timezone、created_at |
| `child_profile` | child_id、nickname、grade、curriculum_region、data_dir、status |
| `family_rule` | rule_id、scope、value_json、version、effective_at、updated_by |
| `app_setting` | key、value_json、updated_at |

约束：`child_profile.data_dir`唯一；删除孩子档案必须先进入删除队列，不允许直接级联丢失审计记录。

### 4.2 设备与会话

| 表 | 核心字段 |
|---|---|
| `device_profile` | device_id、device_type、system_id_hash、capabilities_json、last_seen_at、status |
| `device_binding` | device_type、system_id_hash、label、capabilities_json、verified_at、updated_at |
| `device_binding_event` | event_id、bindings_json、created_at、client_action_id |
| `device_qualification_run` | run_id、mode、status、expected_seconds、两类绑定身份哈希、metrics_json、failure_code、started_at、completed_at、幂等操作ID |
| `device_health` | health_id、device_id、online、in_use、error_code、captured_at |
| `daily_session` | session_id、child_id、status、state、started_at、ended_at、last_checkpoint_at |
| `session_transition` | transition_id、session_id、from_state、to_state、reason、occurred_at |
| `sensor_lease` | lease_id、session_id、device_id、capability、status、expires_at |
| `sensor_release_verification` | verification_id、session_id、child_id、result、当次使用/已重开设备类别、设备管理器、Windows隐私指示、物理指示灯、Windows版本、verified_at、幂等操作ID |
| `maintenance_run` | run_id、job_type、run_date、status、started_at、completed_at、error_code、error_message、details_json |

### 4.3 课堂、作业与计划

| 表 | 核心字段 |
|---|---|
| `classroom_recall` | recall_id、session_id、subject、summary、candidate_nodes_json、confidence、confirmation_status、source_refs_json |
| `homework_item` | homework_id、child_id、session_id、subject、task_type、title、range_text、due_at、source_asset_id、ocr_confidence、confirmation_status |
| `daily_plan` | plan_id、child_id、plan_date、status、total_minutes、constraint_snapshot_json、version |
| `plan_item` | plan_item_id、plan_id、homework_id、subject、task_type、title、expected_minutes、actual_minutes、priority、status、completion_evidence_type、completion_checklist_json、completion_note |
| `daily_reflection` | reflection_id、session_id、child_id、learned_text、difficult_text、next_action、skipped、created_at |

约束：同一`child_id + plan_date`允许多版本计划，但仅一个`active`版本。孩子确认后的作业修改必须写入变更历史。

### 4.4 学习与复测

| 表 | 核心字段 |
|---|---|
| `problem_record` | problem_id、child_id、subject、asset_id、prompt_text、knowledge_nodes_json、source_type、verification_status |
| `learning_attempt` | attempt_id、problem_id、session_id、answer_summary、correctness、duration_seconds、hint_count、independence_level |
| `diagnosis_hypothesis` | hypothesis_id、attempt_id、mistake_type、confidence、evidence_refs_json、status |
| `learning_evidence` | evidence_id、child_id、knowledge_node_id、attempt_id、evidence_type、strength、occurred_at |
| `mastery_state` | child_id、knowledge_node_id、mastery_probability、forgetting_parameter、hint_dependency、confidence、next_review_at、version |
| `review_task` | review_id、child_id、source_evidence_id、review_type、due_at、status、result_evidence_id |

主键：`mastery_state`使用`child_id + knowledge_node_id`复合主键。更新必须记录前后值和算法版本。

### 4.5 知识库

| 表 | 核心字段 |
|---|---|
| `source_registry` | source_id、source_type、title、source_ref、license_status、effective_from、content_hash |
| `knowledge_node` | node_id、node_type、subject、grade、title、description、source_id、version、review_status |
| `knowledge_edge` | edge_id、from_node_id、to_node_id、relation_type、confidence、source_id |
| `textbook_index` | textbook_id、publisher、edition、isbn、grade、volume、subject、child_scope_json |
| `textbook_anchor` | anchor_id、textbook_id、unit、lesson、page_start、page_end、knowledge_nodes_json |

### 4.6 语音、观察与介入

| 表 | 核心字段 |
|---|---|
| `voice_turn` | turn_id、session_id、intent、confidence、transcript_summary、tool_calls_json、result、started_at、ended_at |
| `observation_event` | event_id、session_id、state_type、confidence、duration_seconds、shadow_mode、model_version、created_at |
| `intervention_record` | intervention_id、session_id、trigger_type、trigger_ref、stage、message_summary、policy_version、child_response、corrected_at |
| `device_incident` | incident_id、child_id、session_id、device_type、error_code、message、status、occurred_at、resolved_at |
| `privacy_state_change` | change_id、session_id、scope、from_state、to_state、source、occurred_at |

普通语音默认不保存完整转写，`transcript_summary`只保留完成任务所需摘要。视觉事件不包含原始帧。

### 4.7 模型、报告与运维

| 表 | 核心字段 |
|---|---|
| `model_call` | request_id、child_id、session_id、request_type、provider、model_version、prompt_version、latency_ms、cost_units、verification_status、safety_status、created_at |
| `policy_version` | policy_id、policy_type、version、config_json、status、created_at、rolled_back_from |
| `parent_report` | report_id、child_id、report_type、period_start、period_end、facts_json、interpretations_json、recommendations_json、status |
| `parent_feedback` | feedback_id、report_id、executed、effectiveness、stress_rating、notes_summary |
| `job_queue` | job_id、job_type、payload_ref、status、attempt_count、next_attempt_at、last_error_code |
| `audit_event` | audit_id、actor_type、action、target_type、target_id、result、occurred_at |
| `backup_manifest` | backup_id、db_version、file_count、checksum、created_at、restore_tested_at |

## 5. 索引与一致性

- `daily_session(child_id, started_at desc)`。
- `plan_item(plan_id, status, priority)`。
- `homework_item(child_id, due_at, confirmation_status)`。
- `learning_evidence(child_id, knowledge_node_id, occurred_at desc)`。
- `review_task(child_id, status, due_at)`。
- `observation_event(session_id, created_at)`。
- `model_call(session_id, created_at)`。
- `session_draft(session_id, section)`唯一约束；只允许报到、课堂口述、作业、学习、复盘和Agent输入六类区域。
- `job_queue(status, next_attempt_at)`。

每个客户端动作带`client_action_id`，数据库建立唯一索引实现幂等。文件写入采用临时文件、校验和和原子重命名，避免数据库已提交但证据文件不完整。

未提交表单不得只存在React内存或浏览器缓存。前端防抖写入本地`session_draft`，单份JSON上限12KB；提交成功或阶段转换时在服务端事务中清除，异常重启后随会话恢复。草稿按会话关联孩子，必须进入授权导出、完整备份和“全部历史”删除。

## 6. 本地文件布局

```text
app-data/
  database/
    family.sqlite3
  children/
    <child-id>/
      captures/
      evidence/
      pet-speaking/
      exports/
  cache/
    ocr/
    model/
    media-buffer/
  backups/
  logs/
```

日志不得记录完整题目图片、语音原文、API密钥和孩子敏感表达。图片文件名使用随机ID，不包含姓名、学校或班级。

## 7. 设备适配接口

### HSC-DEV-001 统一设备对象

设备管理器对摄像头、麦克风和高拍仪提供`list`、`open`、`preview`、`capture`、`health`、`release`和`reconnect`。厂商专有能力通过适配器封装，业务层不得依赖厂商SDK返回格式。

### HSC-DEV-002 高拍仪

高拍仪作为独立`document_camera`设备，不与观察摄像头混用默认配置。拍摄保存原始图、校正图和质量结果之间的引用关系，重拍后旧图进入待清理状态而不是立即覆盖。

### HSC-DEV-003 摄像头

观察摄像头使用低帧率本地处理。独立网络摄像头断开视频流不代表物理关闭，界面必须区分“应用已停止读取”和“摄像头已物理关闭”。

### HSC-DEV-004 设备能力画像

首次接入为每个实物设备建立`device_profile`并填写`capabilities_json`，至少记录：设备角色、厂商与型号、硬件版本、系统枚举名、USB VID/PID或网络标识脱敏值、驱动版本、支持的分辨率/FPS、接入协议、PTZ能力、音频能力、稳定性结果和最近验证时间。序列号只保存不可逆哈希，不进入模型上下文。

Windows桌面主进程使用无参数、只读的`windows_pnp_cim_v1`盘点查询获取Camera/Image类设备的系统名称、厂商和签名驱动信息。原始PNP InstanceId只在主进程内短暂存在，返回渲染器前只保留SHA-256和非唯一的VID/PID；IPC不接受命令、路径或查询参数。浏览器枚举名与Windows名称只在唯一精确/包含匹配时自动填充；多个“USB Camera”等歧义候选一律不自动套用。

家长需在能力档案中核对厂商、型号、驱动、协议、VID/PID或脱敏网络身份、PTZ结论和音频结论。`MediaStreamTrack.getCapabilities()`暴露pan/tilt/zoom只记为“浏览器约束候选”，不等于软件控制已验证；同组音频输入也只是候选。两台档案不完整时可运行快速排障，但即使三项稳定性记录均通过，总验收仍必须保持`pending`。原始序列号、`pnp_device_id`或未哈希的`instance_id`字段被后端拒绝，单个档案JSON不得超过50KB。

当前采购基线为高拍仪京东SKU `10184877516348`（2026-08-26网页款式“汇博士X90L 4K教学展台”）和云台摄像头京东SKU `100311808116`（网页型号“汇博士YT100”）。应用在档案页显示该采购记录供到货核对，但SKU、网页型号和宣传能力都不作为设备能力判断依据；正式档案只接受铭牌、Windows枚举和实测结论。

接入优先级为：标准UVC/本地RTSP、ONVIF或公开厂商SDK、厂商软件本地导出目录、人工文件导入。云台只支持厂商App控制时，应用不得模拟已获得PTZ控制，可将摄像头固定在学习位并继续提供无软件跟随的观察能力。

### HSC-DEV-005 双视频设备角色绑定

家长首次接入时，必须显式把两台不同的视频设备分别绑定为`camera`和`document_camera`。应用依次打开设备完成快速验证，保存不可逆设备身份哈希、显示名称、实际能力和验证时间；显示名称变化不得改变身份。绑定更新以`client_action_id`幂等并保留事件快照，应用重启后仍有效。

孩子端只可按当前角色绑定的精确设备ID打开。设备缺失、身份不匹配、被占用或运行中断开时，必须停止该能力、释放轨道并记录故障；不得回退到系统默认设备，也不得把另一角色设备作为替代。家长界面需区分“已绑定并验证”“应用正在读取”和“应用当前未读取”。一次快速验证不替代双路30分钟和单设备60分钟稳定性验收。

两台设备允许按到货顺序逐台验证和持久化。只有观察摄像头时，可先完成该角色绑定、重启恢复和观察摄像头60分钟稳定性测试；双路快速检查、双路并发和正式总验收仍要求两台不同设备。后续添加高拍仪不得清除或改写已绑定摄像头，换绑任一角色时也不得复用另一角色的身份。

观察摄像头的“设备取流稳定”和“场景足以观察”是两个独立结论。每个本地分析结果必须返回场景质量、当前会话人物基线与`observation_ready`；太暗、过亮或持续模糊时，孩子端显示设备仍已连接但当前取景不可用于观察，并提供本地放大预览及具体调整动作。即使画质合格，本会话也必须先在孩子常坐位置检测到一次正面人脸，之后连续无人画面才允许形成`away`候选；人物基线未建立时持续空画面只能返回`unknown`，不能区分孩子离开与镜头对错位置。隐私暂停、服务重启和会话结束会清除内存基线，恢复后须重新建立。家长运行面板只汇总最近24小时结构化状态样本的可用比例和最新原因，不保存原始帧。60分钟无冻结不能单独解除观察有效性门禁；实际座位布置必须能产生`present`、`writing_like`和`away`候选，并保留`unknown`作为低质量或证据不足的结果。

浏览器媒体身份使用同一应用来源下的`deviceId`不可逆哈希，不把可能跨浏览器会话轮换的`groupId`并入权威身份；`groupId`只用于同组音频能力判断。绑定后必须关闭并重启Electron，再直接打开精确设备验证身份稳定，不能只检查数据库仍有一条绑定记录。长时观察与设备验收的BrowserWindow关闭后台节流，并同时持有显示防休眠锁；否则窗口被遮挡时的计时和采样不能作为稳定性证据。

### HSC-DEV-006 设备资格测试

家长端提供`quick_dual`、`dual_concurrency`、`camera_stability`和`document_stability`四种测试，规定时长分别为60秒、30分钟、60分钟和60分钟。测试按角色精确打开当前绑定设备，每秒同时检查媒体时间、浏览器新帧回调和轨道状态，记录实际分辨率、FPS、采样数、冻结数、最长连续冻结和中断原因。每秒仅在内存中生成32×18灰度指纹，只持久化“内容变化/低变化”计数；像素、指纹和连续帧均不保存、不上传。静止作业纸的低变化不等于视频流冻结，只用于人工解读现场。

`quick_dual`只用于到货后的快速排障，不计入发布门禁。当前绑定设备仅在后三项均有通过记录时显示正式通过；换绑后旧证据保留但不再适用于当前设备，较新的正式失败会撤销对应门禁，取消或意外中断不覆盖历史通过记录。`device_qualification_v2`要求后端同时复核服务端墙钟持续时长、规定角色、至少98%的每秒采样、前进/冻结计数自洽、新帧回调、轨道结束状态、实际分辨率/FPS和最长连续冻结；服务端时长不足、采样不足、计数矛盾或能力证据缺失均不得形成`passed`。通过记录保存策略版本和服务端复核时长，家长界面用人可理解的证据摘要展示，不显示内部ID或原始JSON。测试期间禁止开始学习、换绑或启动第二项测试；取消、失败和完成均释放所有轨道。Electron在长时测试期间阻止显示休眠并在任何结束路径释放锁。

## 8. 传感器生命周期

```text
closed
-> requested
-> active
-> privacy_paused
-> stopping
-> released
```

每次打开传感器必须创建有限时长`sensor_lease`并绑定会话。进入`completed`、应用退出、孩子切换、隐私暂停或租约超时均触发释放。释放后通过操作系统设备状态验证，不仅更新界面变量。孩子请求安静时不关闭被明确启用的本地观察，但`snooze_until`作为会话权威状态跨重启抑制所有主动提醒；隐私暂停仍立即停止实际音视频轨道并释放租约。

完成页按会话展示打开、活动、已释放租约及最后释放原因。活动数非零必须显示故障提示；活动数为零仅证明本地服务记录已收口，不得自动把`os_release_verified`设为true。系统级证据必须来自目标Windows上的设备管理器/隐私指示、摄像头指示灯和其他应用重新打开测试。

家长中心仅在目标Windows安装版允许为具体已完成会话追加`session_release_verification_v1`复核。服务端从租约表推导当次实际使用的设备类别；只有租约全部释放、设备管理器正常、Windows隐私指示停止、物理指示灯已熄灭或设备无指示灯，且所有当次使用设备都在其他应用重新打开，才写入`passed`。记录为追加式，最新失败会撤销该会话的通过展示；不代表其他会话或未来占用永久正常。该人工复核与Windows版本证据进入本地导出、备份和全部历史删除，不进入云模型上下文。

## 9. 上传边界

默认可以上传：脱敏OCR文本、单道题的必要裁切、结构化计划请求、低敏学习证据摘要和家长报告所需事实摘要。

默认不得上传：连续视频、连续音频、无关家庭成员画面、整本教材、完整作业册、学校和班级标识、孩子敏感倾诉原文以及未授权PET口语录音。

每次上传记录`request_id`、数据类别、对象引用、脱敏动作、模型提供商和删除策略。模型API返回内容不直接写入权威表，必须经过验证和业务确认。

讲题文本在本地执行`cloud_text_privacy_v1`：替换已知孩子称呼、电话、身份证、邮箱和明确标注的姓名/学校/班级/地址；保留公式、数字关系和必要题意。同一请求最多使用2条同学科课堂摘要，每条最多240字；跨学科回忆、敏感背景和与当前任务无关的历史不进入提示。自伤、侵害或家庭暴力倾诉只走本地安全动作，模型调用审计标记`transmitted=false`，不得把原文写入审计。

跨服务商降级和数学独立核验必须复用相同或更严格的脱敏结果，不能因“核验”身份绕过隐私网关。家长可见脱敏数量、类型、实际上下文条数和阻断状态，但不可见原始问题、回答、请求ID或上下文JSON。

## 10. 留存与删除

| 数据 | 默认策略 |
|---|---|
| 连续视频 | 不落盘 |
| 普通语音临时文件 | 转写完成或会话结束后删除 |
| 高拍仪作业图片 | 本地保留90天，可配置；形成长期证据时保留裁切或摘要 |
| PET口语样本 | 单独授权，本地保留至家长删除或目标结束 |
| 任务与学习证据 | 本地长期保留，支持按孩子导出和删除 |
| 模型请求日志 | 保留180天，内容只存摘要和元数据 |
| 审计事件 | 保留1年，删除数据后保留不含内容的操作摘要 |

删除流程为`requested -> scanning -> deleting -> verifying -> completed|failed`。删除完成后验证数据库引用、文件、缓存、报告派生内容和待重试任务；失败可重试并显示剩余对象。任何单条采集删除或按范围删除都会使包含删除前内容的普通备份和升级前快照失效并从磁盘清除，随后生成`post_deletion`恢复点；家长界面必须立即替换恢复列表，不能允许旧恢复点把已删除内容带回。

## 11. 备份与恢复

- 独立本地维护任务每日创建数据库一致性快照；启动后补做当日任务，应用跨午夜持续运行时继续检查。
- 每日学习结束后额外创建恢复点；当日已有完整恢复点时，自动维护记录复用关系而不重复创建。
- 保留最近7个每日备份和最近4个周备份。
- 备份包含数据库、已授权保留的加密媒体、逐文件校验和和版本号。
- 备份标记`manual`、`session_completed`、`daily_automatic`、`restore_recovery_point`或`post_deletion`来源。`post_deletion`只在旧恢复点全部失效后创建，代表删除后的新基线。
- 每日备份和保留期清理的执行中、完成、失败状态必须持久化；任务中崩溃后启动时回收并重试。家长可看到不含内部路径或原始日志的失败原因并立即重试。
- API密钥不进入普通备份。
- 普通备份用于同一Windows安装内回滚；媒体主密钥不进入备份。跨电脑迁移必须另行提供由家长恢复口令保护的密钥迁移包，在该能力完成前不得承诺换机恢复原始媒体。
- D13必须完成一次全量恢复演练，并验证两个孩子的数据、文件引用和未完成任务。

## 12. 安全要求

- API密钥存入操作系统密钥库或本地受保护配置，不写入Git和日志。
- 家长设置与孩子模式分离，重要删除和规则变更需要家长验证。家长PIN使用PBKDF2加盐哈希，明文不落盘，解锁令牌只保存在进程内存且30分钟失效。
- 连续5次PIN错误后锁定5分钟，锁定截止时间写入SQLite并跨应用/服务重启保持；家长中心显示以后端状态为准的秒级倒计时并禁用输入。锁定到期后失败计数归零，下一次错误必须重新拥有完整5次尝试预算，不能因历史计数立即再次锁定。
- 本地HTTP服务仅监听回环地址，使用会话令牌防止其他本地网页调用。
- 文件导出默认脱敏，并清楚列出将导出的数据类别。
- 依赖和模型版本可追溯，升级失败可回滚上一版本。
- `child_safety_event`只保存R1-R3风险级别、类别、确定性家长指引、待确认/已确认状态和时间，不复制孩子原话；家长中心接口不得返回对应`agent_turn.transcript`。
- 安全事件按孩子隔离，进入孩子数据导出、备份和“语音、OCR与Agent记录”/全部历史删除；删除时先清理安全事件，再清理其引用的Agent回合。
- 家长确认只表示已看到并准备线下接手，不得被系统解释为风险解除、专业评估完成或紧急服务已经联系。

## 13. 验收

1. 两个孩子交替完成任务，数据库和文件目录无串档。
2. 应用和电脑重启后恢复未完成会话、计划和证据。
3. 高拍仪拔出后显示离线，重新连接后无需重建任务。
4. 会话结束后摄像头和麦克风在操作系统中不再被应用占用。
5. 删除一个孩子的指定作业图片后，缓存、报告引用和待处理任务同步清理。
6. 从备份恢复后，任务、知识状态、图片引用和报告能够打开。
7. 模型API请求日志可查看用途、延迟、成本和数据类别，但不包含密钥和完整敏感原文。
8. 两台视频设备绑定后重启仍保持角色；拔出任意一台时不会打开另一台替代，历史设备档案不会覆盖当前绑定状态。
9. 无需家长点击即可生成当日恢复点；同日重复检查不重复备份；失败、家长重试和备份中崩溃后的启动恢复均有持久化证据。
10. 设备资格测试结果跨重启保留；运行中换绑、开始学习和并行测试被拒绝；重启后的活动测试显示中断，渲染器遗留测试可由家长结束；未跑满真实时长不得显示正式通过。
11. R0-R3固定用例在本地完成分级，R1-R3家长提醒不含原话、可确认并跨重启保留，导出和删除覆盖该记录。
12. 家长PIN连续5次输错后锁定5分钟，刷新或重启不能提前解除；锁定期内报告和设置不可见；到期后的首次错误显示还剩4次，而不是立即再次锁定。
