# 语音能力全量执行契约矩阵

日期：2026-09-07；2026-09-23按当前源码复核。原表范围为当时 registry 的76个 capability，当前 `DEFAULT_CAPABILITIES` 已有98个。本表旧行只能作为历史接线依据，不能继续称“全量矩阵”。

## 2026-09-23 当前注册表差异

当前源码比原表多22个正式能力：`companion.activity_feedback`、`diagnostic.cancel`、`learning.review_revisit`、`learning.review_schedule`、`learning_inputs.cancel_revision`、`learning_inputs.confirm_revision`、`learning_inputs.propose_revision`、`learning_inputs.review_reclaimed_scope`、`learning_inputs.review_unplaced`、`learning_inputs.select_target`、`mistake.repair_question`、`native_writing.submission_status`、`native_writing.submit_retained`、`oral.independent_check`、`oral.retry`、`pet.listening_control`、`task.resume`、`tutor.playback_control`、`writing.confirm_original`、`writing.independent_check`、`writing.read_original`、`writing.revise_original`。

其中 `learning_inputs.propose_revision` 已正式支持作业/课堂记录的 `add/update/delete/undo`，并配套目标选择、确认、取消和跨重启流程；下方“任意修改与删除未实现”的旧结论已失效。静态存在仍不证明98项均通过真实模型、物理设备和Windows端到端验收，完整能力矩阵须在当前固定候选上重新生成执行证据。

本表的“存在”只证明实际源码分支与入口，不能代替真实模型、持久化或 Windows 用户旅程验收。旧待修复标记已按当前源码与已结束日志核验；保留尚未闭合的具体缺口，不以静态覆盖或局部测试冒充全量通过。

S=AgentCapabilityExecutor 服务端执行；C=SessionWorkspace 设备/界面桥；M=既有 tutor/model 执行。`any` 表示 registry 不受页面阶段限制，仍须满足真实会话、授权、设备和任务前置条件。

| Capability | Action | 执行分支 | 阶段 | 验收结论/具体缺口 |
|---|---|---|---|---|
| conversation.respond | none | [S 存在](<本机用户目录>/Documents/学生教育/services/local_agent/src/home_study_agent/agent_capability_executor.py:143) | any | 真实分支存在；全量业务 E2E 未逐项重跑。 |
| companion.start_activity | start_companion_activity | [S 存在](<本机用户目录>/Documents/学生教育/services/local_agent/src/home_study_agent/agent_capability_executor.py:278) | any | 真实分支存在；全量业务 E2E 未逐项重跑。 |
| companion.end_activity | end_companion_activity | [S 存在](<本机用户目录>/Documents/学生教育/services/local_agent/src/home_study_agent/agent_capability_executor.py:296) | any | 真实分支存在；全量业务 E2E 未逐项重跑。 |
| controlled_content.play | start_controlled_content | [C 存在](<本机用户目录>/Documents/学生教育/src/renderer/src/SessionWorkspace.tsx:1796) | study, break | 分支存在；设备/子组件实际完成与失败回执须端到端验收。 |
| controlled_content.stop | stop_controlled_content | [C 存在](<本机用户目录>/Documents/学生教育/src/renderer/src/SessionWorkspace.tsx:1820) | any | 本轮解除停止动作的页面阶段限制；启动授权不变。 |
| controlled_game.play | start_controlled_game | [C 存在](<本机用户目录>/Documents/学生教育/src/renderer/src/SessionWorkspace.tsx:1834) | study, break | 分支存在；设备/子组件实际完成与失败回执须端到端验收。 |
| controlled_game.stop | stop_controlled_game | [C 存在](<本机用户目录>/Documents/学生教育/src/renderer/src/SessionWorkspace.tsx:1955) | any | 前端已 await 真实进程关闭，缺接口/关闭异常不再吞错；关闭后保存结束记录。Windows 游戏进程退出本轮未实测。 |
| learning.explain | ask_tutor | [M/C 存在](<本机用户目录>/Documents/学生教育/src/renderer/src/SessionWorkspace.tsx:1726) | any | 模型与题目上下文路径存在；不能把讲解作为掌握。 |
| learning.evaluate_response | ask_tutor | M：app execute 特殊评价路径 | any | 模型与题目上下文路径存在；不能把讲解作为掌握。 |
| learning.request_response | none | [S 存在](<本机用户目录>/Documents/学生教育/services/local_agent/src/home_study_agent/agent_capability_executor.py:194) | any | 已核对前端按 server 执行位置派发，不再因 action=none 跳过；服务端保存待答状态与重放结果，不凭请求更新掌握。 |
| learning.capture_question | request_capture | [C 存在](<本机用户目录>/Documents/学生教育/src/renderer/src/SessionWorkspace.tsx:1900) | any | 分支存在；设备/子组件实际完成与失败回执须端到端验收。 |
| mistake.open | open_mistake_book | [S 存在](<本机用户目录>/Documents/学生教育/services/local_agent/src/home_study_agent/agent_capability_executor.py:520) | any | 服务端真实恢复错题焦点；旧前端路径仅导航后置，非当前 server 路径。 |
| planning.describe | describe_plan | [S 存在](<本机用户目录>/Documents/学生教育/services/local_agent/src/home_study_agent/agent_capability_executor.py:276) | any | 真实分支存在；全量业务 E2E 未逐项重跑。 |
| planning.generate | generate_plan | [S 存在](<本机用户目录>/Documents/学生教育/services/local_agent/src/home_study_agent/agent_capability_executor.py:307) | homework_capture | homework_capture 为计划输入冻结前置条件；不能仅为可达跳过数据与计划一致性。 |
| task.start | start_next_task | [S 存在](<本机用户目录>/Documents/学生教育/services/local_agent/src/home_study_agent/agent_capability_executor.py:424) | study | 真实分支存在；全量业务 E2E 未逐项重跑。 |
| task.pause | pause_active_task | [S 存在](<本机用户目录>/Documents/学生教育/services/local_agent/src/home_study_agent/agent_capability_executor.py:444) | study | 真实分支存在；全量业务 E2E 未逐项重跑。 |
| task.complete | complete_active_task | [S 存在](<本机用户目录>/Documents/学生教育/services/local_agent/src/home_study_agent/agent_capability_executor.py:465) | study | server 逐项确认绑定原话及 task started_at；旧前端 complete_active_task 已明确报 unavailable，不再自动拼 checklist。无 runtime 路径不冒充完成。 |
| session.transition | transition | [S 存在](<本机用户目录>/Documents/学生教育/services/local_agent/src/home_study_agent/agent_capability_executor.py:315) | any | 真实分支存在；全量业务 E2E 未逐项重跑。 |
| profile.switch | switch_child_profile | [C 存在](<本机用户目录>/Documents/学生教育/src/renderer/src/SessionWorkspace.tsx:1979) | any | 已接 beforeActivate 四字段回执及服务端目标 child/session/切换审计；前端先保存稳定交接标识，读失败/刷新用原请求恢复，不留在取消的旧会话。专项证据见下，Windows 切换旅程未逐项验收。 |
| observation.pause | pause_observation | [C 存在](<本机用户目录>/Documents/学生教育/src/renderer/src/SessionWorkspace.tsx:2154) | any | 前端实际停止读取/释放，后台核对 camera 租约与 paused 保存；首次回执增加 server/turn 时间边界。Windows 关闭摄像头检查通过。 |
| observation.resume | resume_observation | [C 存在](<本机用户目录>/Documents/学生教育/src/renderer/src/SessionWorkspace.tsx:2157) | any | 直接幂等执行并验证首帧/租约/enabled，不先查询 status。Windows 新协议已选 resume，但 OS NotReadable/Abort 导致执行 failed，尚未取得真实首帧。 |
| observation.correct | correct_observation | [C 存在](<本机用户目录>/Documents/学生教育/src/renderer/src/SessionWorkspace.tsx:2065) | any | 分支存在；设备/子组件实际完成与失败回执须端到端验收。 |
| checkin.update_context | update_planning_context | [S 存在](<本机用户目录>/Documents/学生教育/services/local_agent/src/home_study_agent/agent_capability_executor.py:320) | check_in | 真实分支存在；全量业务 E2E 未逐项重跑。 |
| recall.add | add_recall | [S 存在](<本机用户目录>/Documents/学生教育/services/local_agent/src/home_study_agent/agent_capability_executor.py:332) | check_in, classroom_recall, homework_capture | 真实分支存在；全量业务 E2E 未逐项重跑。 |
| recall.add_batch | add_recall_batch | [S 存在](<本机用户目录>/Documents/学生教育/services/local_agent/src/home_study_agent/agent_capability_executor.py:336) | check_in, classroom_recall, homework_capture | 真实分支存在；全量业务 E2E 未逐项重跑。 |
| recall.none | declare_no_recall | [S 存在](<本机用户目录>/Documents/学生教育/services/local_agent/src/home_study_agent/agent_capability_executor.py:346) | classroom_recall | 真实分支存在；全量业务 E2E 未逐项重跑。 |
| homework.add | add_homework | [S 存在](<本机用户目录>/Documents/学生教育/services/local_agent/src/home_study_agent/agent_capability_executor.py:348) | check_in, classroom_recall, homework_capture | 真实分支存在；全量业务 E2E 未逐项重跑。 |
| homework.add_batch | add_homework_batch | [S 存在](<本机用户目录>/Documents/学生教育/services/local_agent/src/home_study_agent/agent_capability_executor.py:355) | check_in, classroom_recall, homework_capture | 真实分支存在；全量业务 E2E 未逐项重跑。 |
| homework.none | declare_no_homework | [S 存在](<本机用户目录>/Documents/学生教育/services/local_agent/src/home_study_agent/agent_capability_executor.py:368) | homework_capture | 真实分支存在；全量业务 E2E 未逐项重跑。 |
| planning.reorder | reorder_plan | [S 存在](<本机用户目录>/Documents/学生教育/services/local_agent/src/home_study_agent/agent_capability_executor.py:310) | plan_review | plan_review 为当前计划修改边界；运行中更改需要独立计划失效机制，本轮未伪放开。 |
| break.begin | begin_break | [S 存在](<本机用户目录>/Documents/学生教育/services/local_agent/src/home_study_agent/agent_capability_executor.py:420) | study | 真实分支存在；全量业务 E2E 未逐项重跑。 |
| break.resume | resume_study | [S 存在](<本机用户目录>/Documents/学生教育/services/local_agent/src/home_study_agent/agent_capability_executor.py:422) | break | 真实分支存在；全量业务 E2E 未逐项重跑。 |
| task.reschedule | reschedule_current_task | [S 存在](<本机用户目录>/Documents/学生教育/services/local_agent/src/home_study_agent/agent_capability_executor.py:452) | study | 真实分支存在；全量业务 E2E 未逐项重跑。 |
| diagnostic.start | start_diagnostic | [C 存在](<本机用户目录>/Documents/学生教育/src/renderer/src/SessionWorkspace.tsx:2057) | study | 最终读源确认主线程已将接收器 false 改为抛 unavailable，不再落入 completed。这处最新修补的专项结果本表尚未核验，不套用更早组测试。 |
| diagnostic.submit | submit_diagnostic_answer | [C 存在](<本机用户目录>/Documents/学生教育/src/renderer/src/SessionWorkspace.tsx:2098) | study | 保存/接收器未确认时已抛 unavailable，走 failed 而非通用 completed；真实独立作答/评分效果不能由分支存在证明。 |
| pet_vocabulary.start | start_pet_vocabulary_review | [C 存在](<本机用户目录>/Documents/学生教育/src/renderer/src/SessionWorkspace.tsx:1957) | study | 分支存在；设备/子组件实际完成与失败回执须端到端验收。 |
| pet_vocabulary.submit | submit_pet_vocabulary_answer | [C 存在](<本机用户目录>/Documents/学生教育/src/renderer/src/SessionWorkspace.tsx:1963) | study | 分支存在；设备/子组件实际完成与失败回执须端到端验收。 |
| pet_vocabulary.cancel | cancel_pet_vocabulary_review | [C 存在](<本机用户目录>/Documents/学生教育/src/renderer/src/SessionWorkspace.tsx:1991) | study | 分支存在；设备/子组件实际完成与失败回执须端到端验收。 |
| exam.start | start_exam_simulation | [S 存在](<本机用户目录>/Documents/学生教育/services/local_agent/src/home_study_agent/agent_capability_executor.py:370) | study | 真实分支存在；全量业务 E2E 未逐项重跑。 |
| exam.submit | submit_exam_answer | [S 存在](<本机用户目录>/Documents/学生教育/services/local_agent/src/home_study_agent/agent_capability_executor.py:400) | study | 真实分支存在；全量业务 E2E 未逐项重跑。 |
| exam.pause | pause_exam_simulation | [S 存在](<本机用户目录>/Documents/学生教育/services/local_agent/src/home_study_agent/agent_capability_executor.py:400) | study | 真实分支存在；全量业务 E2E 未逐项重跑。 |
| exam.resume | resume_exam_simulation | [S 存在](<本机用户目录>/Documents/学生教育/services/local_agent/src/home_study_agent/agent_capability_executor.py:400) | study | 真实分支存在；全量业务 E2E 未逐项重跑。 |
| exam.cancel | cancel_exam_simulation | [S 存在](<本机用户目录>/Documents/学生教育/services/local_agent/src/home_study_agent/agent_capability_executor.py:400) | study | 真实分支存在；全量业务 E2E 未逐项重跑。 |
| mistake.review | review_current_mistake | [S 存在](<本机用户目录>/Documents/学生教育/services/local_agent/src/home_study_agent/agent_capability_executor.py:215) | study | 真实分支存在；全量业务 E2E 未逐项重跑。 |
| mistake.diagnose | diagnose_current_mistake | [S 存在](<本机用户目录>/Documents/学生教育/services/local_agent/src/home_study_agent/agent_capability_executor.py:226) | study | 真实分支存在；全量业务 E2E 未逐项重跑。 |
| mistake.submit_response | submit_mistake_answer | [S 存在](<本机用户目录>/Documents/学生教育/services/local_agent/src/home_study_agent/agent_capability_executor.py:251) | study | 真实分支存在；全量业务 E2E 未逐项重跑。 |
| reflection.update | update_reflection | [C 存在](<本机用户目录>/Documents/学生教育/src/renderer/src/SessionWorkspace.tsx:2136) | reflection | 保留 reflection 前置；接收器 false 已抛 unavailable，不再落入 completed。Windows 本轮未逐项重跑。 |
| reflection.complete | complete_reflection | [C 存在](<本机用户目录>/Documents/学生教育/src/renderer/src/SessionWorkspace.tsx:2138) | reflection | 接收器未确认保存已抛 unavailable，原复盘数据保留；收到命令不代表日报已保存。Windows 本轮未逐项重跑。 |
| document.capture_question | request_capture | [C 存在](<本机用户目录>/Documents/学生教育/src/renderer/src/SessionWorkspace.tsx:1900) | any | 分支存在；设备/子组件实际完成与失败回执须端到端验收。 |
| document.monitor_control | control_document_monitor | [C 存在](<本机用户目录>/Documents/学生教育/src/renderer/src/SessionWorkspace.tsx:2162) | study | 持续监控服务仍限 study，pause/resume 严格回执，inspect 等待页面处理。空/旧上下文的本地当前页请求已改各阶段 request_capture(observation_only=true)，不进入连续监控云视觉回退。 |
| behavior.feedback | behavior_intervention_feedback | [S 存在](<本机用户目录>/Documents/学生教育/services/local_agent/src/home_study_agent/agent_capability_executor.py:166) | any | 本轮改 server 真保存；cloud 曾仅发刷新事件。legacy 复用已保存 action 防重复。 |
| privacy.pause_all | privacy_pause | [C 存在](<本机用户目录>/Documents/学生教育/src/renderer/src/SessionWorkspace.tsx:2019) | any | 分支存在；设备/子组件实际完成与失败回执须端到端验收。 |
| behavior.reset_personalization | reset_behavior_personalization | [C 存在](<本机用户目录>/Documents/学生教育/src/renderer/src/SessionWorkspace.tsx:2028) | any | 分支存在；设备/子组件实际完成与失败回执须端到端验收。 |
| recall.review_pending | review_pending_recall | [C 存在](<本机用户目录>/Documents/学生教育/src/renderer/src/SessionWorkspace.tsx:1741) | classroom_recall | 无候选或 unknown 候选要求确认时已抛 unavailable；允许的确认/拒绝仍 await 实际 reviewRecall 保存。 |
| homework.update_duration | update_last_homework_minutes | [C 存在](<本机用户目录>/Documents/学生教育/src/renderer/src/SessionWorkspace.tsx:1662) | homework_capture | 分支存在；设备/子组件实际完成与失败回执须端到端验收。 |
| homework.review_carryover | review_first_carryover | [C 存在](<本机用户目录>/Documents/学生教育/src/renderer/src/SessionWorkspace.tsx:1670) | homework_capture | 分支存在；设备/子组件实际完成与失败回执须端到端验收。 |
| document.confirm_ocr | confirm_ocr_text | [C 存在](<本机用户目录>/Documents/学生教育/src/renderer/src/SessionWorkspace.tsx:1928) | any | 分支存在；设备/子组件实际完成与失败回执须端到端验收。 |
| document.correct_ocr | correct_ocr_text | [C 存在](<本机用户目录>/Documents/学生教育/src/renderer/src/SessionWorkspace.tsx:1936) | any | 分支存在；设备/子组件实际完成与失败回执须端到端验收。 |
| document.retry_capture | retry_capture | [C 存在](<本机用户目录>/Documents/学生教育/src/renderer/src/SessionWorkspace.tsx:1900) | any | 分支存在；设备/子组件实际完成与失败回执须端到端验收。 |
| document.cancel_capture | cancel_capture | [C 存在](<本机用户目录>/Documents/学生教育/src/renderer/src/SessionWorkspace.tsx:1944) | any | 分支存在；设备/子组件实际完成与失败回执须端到端验收。 |
| diagnostic.replay | replay_diagnostic | [C 存在](<本机用户目录>/Documents/学生教育/src/renderer/src/SessionWorkspace.tsx:2095) | study | 无正在展示的题目/接收器 false 已抛 unavailable，不再伪 completed。 |
| tutor.record_feedback | record_tutor_feedback | [C 存在](<本机用户目录>/Documents/学生教育/src/renderer/src/SessionWorkspace.tsx:2101) | any | 已放开 any；非 study 绑定 global/latest tutor 的本会话、孩子与30分钟新鲜度，用稳定反馈ID保存，失败报 unavailable。全局四项前端专项已通过。 |
| tutor.more_hint | more_tutor_hint | [C 存在](<本机用户目录>/Documents/学生教育/src/renderer/src/SessionWorkspace.tsx:2113) | any | 已放开 any；全局提示携带真实 parent_turn_id/稳定请求ID，后端检查父孩子/会话/题干，在 request_hash 前继承 OCR 确认。过时/他人前轮被拒绝；见专项日志。 |
| mistake.complete_remediation | complete_mistake_remediation | [S 存在](<本机用户目录>/Documents/学生教育/services/local_agent/src/home_study_agent/agent_capability_executor.py:238) | study | 真实分支存在；全量业务 E2E 未逐项重跑。 |
| mistake.activity_control | control_mistake_activity | [S 存在](<本机用户目录>/Documents/学生教育/services/local_agent/src/home_study_agent/agent_capability_executor.py:272) | study | 真实分支存在；全量业务 E2E 未逐项重跑。 |
| mistake.dialogue | continue_mistake_dialogue | [S 存在](<本机用户目录>/Documents/学生教育/services/local_agent/src/home_study_agent/agent_capability_executor.py:520) | study | 真实分支存在；全量业务 E2E 未逐项重跑。 |
| mistake.capture_control | control_mistake_capture | [C 存在](<本机用户目录>/Documents/学生教育/src/renderer/src/SessionWorkspace.tsx:2056) | study | 分支存在；设备/子组件实际完成与失败回执须端到端验收。 |
| speech.stop | stop_speech | [C 存在](<本机用户目录>/Documents/学生教育/src/renderer/src/SessionWorkspace.tsx:2067) | any | 执行本地取消播放，不表示暂停任务或关闭麦克风。 |
| speech.repeat | repeat_response | [S 存在](<本机用户目录>/Documents/学生教育/services/local_agent/src/home_study_agent/agent_capability_executor.py:146) | any | 本轮改 server 重播真实已完成回复，排除回声/静默/旧重播；强制本地 TTS。 |
| onboarding.resume | resume_voice_tutorial | [C 存在](<本机用户目录>/Documents/学生教育/src/renderer/src/SessionWorkspace.tsx:1497) | any | 分支存在；设备/子组件实际完成与失败回执须端到端验收。 |
| voice.configure_wake_word | configure_wake_word | [C 存在](<本机用户目录>/Documents/学生教育/src/renderer/src/SessionWorkspace.tsx:1671) | any | 流程开启回执只承认待确认，不声称保存；确认后桌面真实保存。Windows 改名确认保存及重载保留名字已通过。 |
| device.status | query_device_status | [C 存在](<本机用户目录>/Documents/学生教育/src/renderer/src/SessionWorkspace.tsx:1666) | any | client 当前 track/frame/错误/monitor 查询；历史仅 last_known，不推断电源。首次状态回执绑定server时间±30秒及turn时间；Windows 两类设备状态查询通过，不等于已有画面。 |
| document.set_occlusion_reminders | set_document_occlusion_reminders | [S 存在](<本机用户目录>/Documents/学生教育/services/local_agent/src/home_study_agent/agent_capability_executor.py:188) | any | server 按孩子从成功 agent_step 持久投影；前端读取 runtime 应用偏好，不放宽质量门。Windows 关闭提醒及后端重启保留偏好通过。 |
| session.snooze | snooze | [C 存在](<本机用户目录>/Documents/学生教育/src/renderer/src/SessionWorkspace.tsx:2074) | any | 前端空操作但 orchestrator 已先保存 snooze_until；不是由前端 Promise 完成持久化。 |
| session.request_end | request_end | [C 存在](<本机用户目录>/Documents/学生教育/src/renderer/src/SessionWorkspace.tsx:2078) | any | 存在未处理任务时只核对请求，不能宣称 session 已结束。 |
| safety.find_parent | find_parent | [C 存在](<本机用户目录>/Documents/学生教育/src/renderer/src/SessionWorkspace.tsx:1894) | any | 只提示联系家长，不自动联系；本轮回执明确 parent_contact_prompted，修 urgent 别名。 |

## 注册表外的用户功能边界

- 家长鉴权、数据导出/删除/恢复、设备绑定、模型与费用配置、受保护目标和课程审核：没有可让孩子直接执行的 capability；不得把任意 parent API 暴露给模型。前端家长入口可由用户发起，但仍须真实家长验证。本轮 parent.request_access 尚未落地，属于已知覆盖缺口，不宣称全功能已完成。
- 高拍仪实时监控目前实际服务/接收器仅 study；签到时读取页面应走全阶段拍题，不能由模型仅改变 stage 或口头声明已开启。
- 具体作业/回忆条目的增删改撤已由 `learning_inputs.propose_revision` 及选择/确认/取消能力承载，不再归类为未实现；仍须在固定候选中验证任意自然表达、歧义选择、来源变化、跨孩隔离和重启恢复，不能用注册表存在代替端到端通过。

## 已运行证据

以下是独立运行的证据，不相加冒充一次全套通过；本次仅更新文档，未重跑这些测试。

- 后台最新主线程定向：200 passed、4 默认付费 probe skipped，58.75 秒。[日志](/tmp/hsc-voice-final-targeted.log)。
- 设备回执 freshness 专项：108 passed，13.62 秒；包含两小时前快照、未来时间、新 turn 旧帧、合理时钟偏移、已完成回放不重写。[日志](/tmp/hsc-device-freshness-final.log)。
- 父 Tutor/OCR 授权专项组：15 passed；父归属守卫先继承确认ID再计算请求哈希，省略ID不能丢掉原 OCR 确认边界。[日志](/tmp/hsc-parent-voice-tests.log)。
- 前端全局 Tutor 四项核心案例（真实父ID、稳定反馈ID、他人/过时拒绝）已通过；后续相关三文件组102 passed。[日志](/tmp/hsc-global-tutor-tests.log)。合成输入与测试替身不代表 Windows 真题教学效果。
- profile 交接恢复 helper 11 passed、相关五文件组121 passed，涵盖预先持久化、丢回包/读失败恢复、超时与迟到结果。[helper日志](/tmp/hsc-profile-helper.log)、[相关组日志](/tmp/hsc-profile-targeted.log)。先前失败日志不作为最终通过证据。
- 已核验前端 server 派发、普通任务完成旧分支拒绝、真实游戏关闭 await，以及诊断回答/重播、复盘和回忆候选的失败分支。诊断启动的 false->completed 缺口在最终读源时已由主线程改为抛 unavailable；该最新修补尚不附会更早的通过日志。

## 真实模型上下文

- [test_agent_device_planning_context.py](<本机用户目录>/Documents/学生教育/services/local_agent/tests/test_agent_device_planning_context.py) 显式启用付费 probe 后，真实 DeepSeek 经现有 ModelGateway、planner校验与 model_call 审计运行，最终4 passed，32.11秒。[日志](/tmp/hsc-device-planner-context-cloud-final.log)。
- 使用独立合成库，保留仍 active 的旧命名目标及修改唤醒词、请求确认、孩子确认、保存成功四轮上下文，不读取真实家庭库。
- 原话“你把摄像头也打开”及礼貌开启请求均选 observation.resume；关闭请求选 observation.pause；现状问句选 device.status。三项操作均建新 execution 目标，不要求额外确认，不把查询当开关前置。
- 本机普通定向101 passed、4默认付费skip与上述实付费4项分列。[日志](/tmp/hsc-device-planner-context-unit.log)。首轮四项语义断言通过但测试清理跨事件循环报错，改用 TestClient portal 后四项完整通过；共两轮调用，不把首轮当成功测试。
- 四个上下文样例通过不代表所有自然表达永不误判，更不证明物理设备已打开。

## Windows 边界

- 依据主线程最新实机反馈：此前七项通过为改名确认保存、重载保留名字、摄像头状态查询、高拍仪状态查询、关闭摄像头、关闭遮挡提醒、后端重启保留偏好。新源码实机规划复测又通过 camera resume 选择，现为8项检查通过。
- camera_resume_completed、actual_camera_frame 仍未通过。实际 turn 为 failed / CLIENT_DEVICE_ACTION_FAILED，而非虚假 completed；独立 getUserMedia 同样报 OS NotReadable/Abort，尚待解除设备占用并核验真实首帧。
- Windows 证据由主线程执行并反馈，使用合成转录接入真实界面、服务及桌面保存，不冒充真实麦克风/连续儿童对话验收。本worker未部署或操作真实家庭数据。
- 完整后端全套未完成，设备失败未解除，76能力并未逐项取得 Windows E2E、两孩子长期使用或学习结果证据；全产品仍 partial，不标 complete。
