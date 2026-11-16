# C04/C05 活动发起来源交接

## 状态与范围

本项仅完成只读分析，`initiation_binding_v1` 尚未实现。V7 固定回归期间只写本文，不改 src/tests、不跑测试。V7 失败优先处理，其后恢复此项；C04 整体仍为 partial。

已冻结证据：C04 局部 API/消费者组合 21 passed，日志 `/tmp/hsc-c04-api-consumer-final.log`；自有清单 `/tmp/hsc-c04-owned-freeze.sha256`。这些不是发起来源功能的验证证据。

后续所有权：`companion_activity_outcome.py`、`agent_runtime.py` 仅实际 activity 创建/来源绑定段、新专测。不改 app/repository/privacy 或 C07 goal_program/goal_roadmap/official_goal_rules；C04 core 消费者如需最小接线，先报告具体段并取得授权。

下列源码路径均相对 `services/local_agent/src/home_study_agent/`；行号为本次只读核对位置。

## 已有事实与根因

| 来源 | 真实字段与读取位置 | 当前缺口/限制 |
| --- | --- | --- |
| 原用户 turn | `agent_turn.agent_turn_id,child_id,session_id,transcript,intent,action_json,tool_status,tool_error_code,created_at,client_action_id`；`action_json` 可含 `voice_input_id,voice_input_binding` | 不能把模型 arguments、回复或 `intent` 标签当作原话/真实主体证明 |
| 原执行 | `agent_step.step_id,plan_id,capability_name,capability_version,arguments_json,idempotency_key,status,error_code,result_json`；`agent_plan.agent_turn_id,objective_id,context_manifest_json,status` | executor 约350行已将真实 step 的 `idempotency_key` 传给 `start_companion_activity`；可精确等值查原 step，不拆 key 猜 ID |
| 到达快照 | `learning_input_arrival.arrival_id,child_id,session_id,client_action_id,utterance,snapshot_json,pending_json,created_at`；manifest 的 `learning_input_arrival_id` | 必须核原 owner/key/完整原话；到达快照不是活动发起分类 |
| 原 voice | `voice_capture_binding` 的 `voice_input_id,child_id,session_id,client_action_id,status,created_at,question_reference_json,question_validation`；关联 `voice_input.transcript,status` 和 `voice_transcription_action.status` | 复用 `voice_capture_binding.resolve_voice_capture(c,session_id=...,child_id=...,voice_input_id=...,transcript=...)`（51行），核归属、完成及原文，仅折叠空白；不要求保存 raw 音频，不证明声纹身份 |
| agent 建议出箱 | `agent_proactive_prompt.prompt_id,child_id,session_id,source_type,source_id,message_text,status,created_at,expires_at,acknowledged_at,delivery_attempts,last_attempt_at`；Runtime 1201/2058/2357行 | `delivered` 不等于已播放；ack 也不证明孩子是在回应它。`behavior_intervention` 可追到 `intervention` 的原事件、消息、feedback/status；不能按消息关键词定因果 |
| 目标/历史 | `agent_objective.objective_id,child_id,session_id,objective_type,status,phase,success_criteria_json,updated_at`，以及原 plan/turn | 本次未找到可直接复用的通用持久 `active_intent → offer_response` 关联。执行 objective 不必是原学习 goal；不得取当前/latest goal 代替原绑定 |
| 活动及返回 | `companion_activity` 的原 child/session/type/topic、`paused_plan_item_id,started_at,ended_at,client_action_id`；`companion_activity_outcome.source_snapshot_json,source_fingerprint`；既有 outcome event | Runtime 2390行创建，outcome `open` 164行冻结来源，但未冻原 turn/voice/offer；`_return_verification` 421行的 `activity_initiated_by` 当前明确为 unknown |

## 最小完整接线

1. **创建事务内绑定。** Runtime 根据本次真实 step key 查询 `companion.start_activity` 的原 step/plan/turn，核 child/session、原 action 参数、实际 paused task、运行/取消权限。旧兼容 API 或非 Agent 入口缺原证明仍可按既有规则启动，但来源为 unknown，不猜最近 turn。取消后不能创建新活动。
2. **复用快照，不新建表。** 在新活动对应 `source_snapshot_json` 增加补充字段 `initiation_binding_v1`，保留既有生命周期 fingerprint 语义。拟保存：`child_id/session_id/source_kind/source_id/source_version`、`turn_id/step_id/action_version`、`arrival_id`、`voice_input_id/voice_binding_version/transcript_hash`、原 `goal_binding/task_binding`、前置来源版本及明确 `offer_binding`。只引用/哈希，不复制原话、音频、题面、图谱或提示正文。
3. **同事务、稳定回放。** 活动和 outcome/opened 事件一起提交；优先给 `open` 显式 borrowed connection，初始化在事务外，不扩大现有吞 commit/rollback 代理。旧 key 读原完成回执，不按新历史重分类或补绑定；旧活动缺该字段保持 unknown。不得为恢复删除旧事实。
4. **当前只读投影。** 新 `initiation_proof` 返回 `classification/status/reason/binding_version/evidence_refs`，限定 local-only。重核原来源后才给有限 `session_bound_request` 证明；原声/turn/来源撤回、删除、错孩、绑定被改或明确取消则降级 unknown。正常任务完成、goal completed、时间过去不等于撤回，不撤销真实活动或 resume receipt。
5. **贯穿现有消费者。** outcome `_detail`（342行）及 `project_companion_activity_evidence`（637行）附带当前发起证明；ChildModel `_companion_balance`（890行）已有 `outcome_evidence` 通路，不另造学习/习惯事实。C04 `goal_confirmation._automatic` 的 fatigue 段（171行附近）后续仅在获授权后读取同一活动/同一 resume episode 的合格发起证明，不能与别次活动的返回拼接。保留现有独立性、覆盖和持续性门槛。

## 分类与不可推断边界

- `child_request`：真实非 continuation 的直接请求，模型可路由活动意图；原话、主体上下文、action、来源由宿主独立核验。还须有完整可解释的前置上下文，排除明确未解决提议或不明建议。只表示记录中的儿童会话请求来源，不等于声纹认证或心理自主性。
- `response_to_agent_offer`：必须有宿主冻结的明确 offer-response 关联，并核该 offer 的实际来源/版本；不能用“前面出现过提示”或 `acknowledged_at` 代替回应关系。
- `parent_request`：只接受鉴权宿主产生的 provenance。现有 `open(actor='parent')` 字符串、模型 `initiated_by`、请求自声明 observer 都不是认证。
- `unknown`：历史缺失、前置自由回复可能包含建议但无结构化关联、仅排队未明确交付、原声不匹配、旧记录无绑定等。不能根据短时间沉默、最后一条提示、关键词“我要/休息”、模型置信度、计时结束或孩子自报洗成主动证明。
- **需要宿主补的精确缺口：** 现有普通 capture 冻结的是 typed question reference，不是 offer-response。若当前记录不足，先报 app/前端宿主接线；不在 owned service 中伪造该关系，不因此禁止普通活动启动。该关联属于本项必要接线，不新增泛化意图库。

## V7 后实际链路与反例

新专测使用隔离库、合成 PCM、受控 recognizer/ModelGateway MockTransport；不付费、不用真实家庭媒体、不 mock 掉整个来源 resolver 或手改 step 为 running 冒充执行。

主链：`POST /v1/sessions/{sid}/asr` → 携原 `voice_input_id` 的 `POST /v1/sessions/{sid}/agent-turn` → `POST /v1/agent-turns/{tid}/execute` 创建活动 → 自然活动结束/明确恢复请求经真实 dispatcher → 原 task 的 resume receipt → 原绑定题的新作答 → 读取家长 longitudinal-model 的 `companion_balance.outcome_evidence`。计时恢复阶段仍无 verified return；新可靠原任务证据后才验证返回，发起分类独立判断。

必须覆盖：

1. 无未解决/不明前置提议的明确原请求：活动、原 turn/voice/task/goal 绑定可追溯；只给有限来源证明，不写 mastery/习惯成功。
2. 明确 offer 回应、出箱未播、已 ack 但无回应关联、前置自由回复：分别准确分类或 unknown，不因超时/沉默变 child_request。
3. 模型伪造 `initiated_by/actor/offer_id`、复制他孩 voice、改原话或原 action：不得认证；父请求只有鉴权宿主来源才成立。
4. 创建写事务前取消/换孩/换 task：无新活动；已创建后的源撤回仅撤当前资格，保留真实 activity/resume 历史。
5. 丢响应、后端重启、同 key 重试：一个活动/一个 opened 事件，同一不可变来源；历史无快照不回填。
6. 恢复后无答案、自报、提示暴露、无关题：不得增加独立返回/行为资格；真正同任务可靠新答案可进入当前投影。
7. 原 voice/turn/题源撤回或删除后再次读取 ChildModel：来源或返回资格分别降级，不恢复已删内容、不影响另一孩。正常 completed 的历史仍有效。
8. 沿真实宿主 export/delete 检查新字段随既有 outcome 生命周期处理；不新增 DELETE 禁止触发器。主负责宿主权限/隐私接线，测试不能绕开其门槛。

可复用前置：`tests/test_companion_verified_return.py` 的 `returned/open_resume/saved_answer`、`test_behavior_goal_return_proof.py`；它们证明服务/真实回执层，不能替代上述全 HTTP 发起来源链。实施后先定向红绿，再冻结交主；设备声学、真实孩子和长期结果继续单独标未验证。
