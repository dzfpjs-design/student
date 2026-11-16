# Persona / Voice / Agent 集成点

本任务通过最小适配点接入了 local agent、Electron 安全配置与现有学习会话；OCR 讲题额外建立了独立的本地确认记录表，不改动计划、知识或 OCR 工件的系统记录。自然女声仍须经过真实供应商账户、音色许可和试听验收，未配置时界面只能显示文本，并把系统播报标为临时降级。

1. **安全配置（已接入）**：Electron `safeStorage` 加密保存 relay、阿里云 CosyVoice 或 Qwen-Audio-TTS 的工作空间、区域、模型、音色 ID 和密钥；preload 只给家长页不含密钥的状态和写入/清除/固定文本试听 IPC。`LocalServiceManager` 启动子进程时才注入 `HSC_TTS_*`；bootstrap、浏览器、日志均不含密钥。默认 `disabled`。
2. **本地 TTS API（已接入）**：受 session token 保护的 `POST /v1/sessions/{session_id}/voice/synthesize/stream` 只接受最长 1000 字的回复文字和 UUID，响应为 `uint32LE sampleRate + uint32LE payloadSize + signed-PCM16` 帧流。断开或 `AbortSignal` 会终止请求及播放队列。
3. **供应商边界（已接入，真实声音待选）**：正式支持经过部署审核的 `relay_pcm`、阿里云 Model Studio CosyVoice 官方 duplex WebSocket 适配器（`run-task → continue-task → finish-task`、只收 PCM），以及 Qwen-Audio-TTS 官方 DashScope callback→PCM 适配器。CosyVoice 直连仅允许 `longyingtao_v3`、`longanwen_v3`、`longxiaochun_v3`、`longyingmu_v3`、`longanrou_v3` 五个成人主声；Qwen 仅允许其独立的 `longanlingxin`、`longanlufeng` 成人女声音色，绝不冒充主声。童声/庆祝声无法配置为主声。云端只收到回复文字，禁止音色复刻、录音上传或自定义声音训练。无网/超时返回明确 `TTS_UNAVAILABLE`，UI 保留文本且将系统播报标为临时降级。
4. **缓存与审计（已接入）**：后端只缓存经过部署审核的固定通用短句 PCM 10 分钟、最多 20 项，键不含孩子 ID；个性化/模型生成回复一律不缓存，拒绝敏感文本。`audit_event` 记录 provider、文本长度、首包/总时延、帧数、缓存命中、取消/失败码，绝不写文本、音频或密钥。
5. **声学档案（已接入）**：浏览器本机 localStorage 仅保存同意后生成的短时特征；可删除并重做，不保存/上传原始 WAV。它不是身份认证、考勤或“录一次就训练模型”。若未来迁移到服务端，仍必须新增独立 consent、加密、保留期限与删除 API。
6. **硬件基线（代码具备、实机待验）**：必须在 Windows x64 / AVX2 4 核 8 线程 / 16GB RAM / 无独显执行基准。禁止把 4090、CUDA、3D 数字人、7B 本地模型或大模型 TTS 写为启动条件；NPU/集显只可选加速，CPU 回退仍须测试。

## Agentic OS 工具边界与失败恢复（2026-08-28）

孩子的话先经过本地状态机和 allow-list，模型不会获得任意工具执行权。计划、课堂口述、作业、开始/完成任务、休息、OCR 题目拍摄、学习讲解、观察纠正和隐私暂停均有状态、字段或明确确认限制；完成任务必须是孩子的明确确认。计划、作业、诊断、OCR、观察纠正和会话转场均使用服务端 `client_action_id` 幂等记录；同一轮语音正在处理时，新话只保留最新一句，避免并发写入互相覆盖。云意图与陪伴回复都有有限超时并退回本地澄清，不会把模型输出作为可执行 JSON。

| 孩子意图 | 当前工具/系统记录 | 权限与恢复 |
| --- | --- | --- |
| 课程口述、作业、生成计划 | `add_recall`、`add_homework`、`generate_plan` | 只在对应阶段；写入失败不盲目重放，保留当前文本并让孩子在可见页面重新确认。 |
| 题目讲解、知识提示 | `ask_tutor` | 仅学习阶段且有当前任务；一般本地失败会显示“重试讲解”和“请家长帮忙”。若结果未确认，必须由孩子点击“确认再次请求（可能再次调用模型）”，普通刷新/重试不会重放。 |
| 题目拍摄/OCR | `request_capture` → 现有拍摄/OCR 流程 | 不上传家庭视频；拍摄失败由拍摄面板提供重试，Agent 只请求打开该面板。 |
| 错题复习 | 既有错题本/`MistakePlanActivity` | Agent 不会暗中创建、删除或判定错题；需在错题面板中由孩子确认。 |
| 休息、故事、游戏 | `begin_break` 仅用于学习节奏 | 不从 Agent 自动启动故事或游戏，不用娱乐作主动干预；年龄、时长与家长规则由既有休息/家庭规则控制。 |
| 摄像头状态、隐私 | `privacy_pause`、本地观察状态 | 孩子可随时关闭观察；释放本地设备，文本学习仍可继续。Agent 不读取或外传原始视频。 |
| 找家长、关系/安全边界 | `find_parent` / 本地安全分类器 | 不替代家人、不承诺隐瞒风险；失败恢复中也提供回首页→家长中心的明确路径。 |

讲解工具的失败恢复元数据（turn ID、问题、学科、固定 action ID、OCR confirmation ID、次数、创建时间、是否需要明确再次请求；无音频、无内部错误）与输入草稿共同保存到会话草稿，因此刷新或本地服务重启后仍显示正确的重试/确认/家长帮助入口。创建时间必须能解析且只在 24 小时内有效（最多容忍 5 分钟本机时钟偏差），过期草稿不会永久显示恢复卡。成功收到回复后才清除这项恢复元数据；其余带副作用的工具不自动重试：网络可能在服务端提交后才断开，自动重放会造成重复作业、重复计划或错误完成记录。

### 讲题去重预占与未知结果边界（2026-08-28）

`tutor_request_reservation` 是本地 SQLite 的请求系统记录：action ID 唯一绑定 session、child、学科、规范化题干、提示层级、父 turn、OCR confirmation ID 和请求指纹；不能把同一个 action ID 换到另一题、另一会话、另一孩子或另一份题干确认。`tutor_turn` 同时持久化 request hash 与 OCR confirmation ID；路由会先按 action ID 查已保存 turn，校验完整指纹后补偿为 completed，再解析 OCR，因此“turn 已保存、预占标记尚未写入”的崩溃恢复和确认随后过期都不会再次调用模型。并发相同请求在调用模型前以 `BEGIN IMMEDIATE` claim：首个为 `pending`，其余收到可理解的 `TUTOR_REQUEST_IN_PROGRESS`（可轮询/重试、不会再次调用模型）；完成后返回同一 turn；不同载荷返回 409。

预占只占用很短的 SQLite 事务，不跨模型网络请求。`tutor_request_attempt` 持久记录每次 pending/completed/failed/interrupted；调用 gateway 前先写入 `GATEWAY_SUBMISSION_STARTED`，模型返回后立即改写为 `MODEL_RESULT_RECEIVED_UNPERSISTED`。任何超过两分钟的 pending（包含旧版本没有标记的记录）都可能已越过供应商提交边界，统一转为 `INTERRUPTED_UNKNOWN_OUTCOME`，绝不自动调用第二次模型；两分钟内仍只显示“正在生成”，不会过早判断失败。只有明确在提交前发生的确定性本地失败（例如未配置模型）可自动重试。普通恢复、刷新、“重试本题”以及 Agent 面板的“重试讲解”都会持续收到 409；只有孩子点击独立的“确认再次请求”才在同一 action ID 和完全相同的题干/学科/父 turn/OCR confirmation 上带 `confirm_unknown_outcome_retry=true`，明确承认可能再次调用模型。放弃会保留题干或问题、清掉旧 action 草稿，下一次提问生成新的请求。供应商没有请求幂等键时，外部模型已经响应但本机尚未持久化的窄窗口无法承诺绝对 exactly-once；当前保证的是并发与普通网络重试不重复调用、已保存结果可补偿重放、未知结果不被静默重放。取消、断网或隐私暂停只能停止渲染器等待、音频队列与可中止 HTTP 流，不能谎称已经送达远端的模型请求被撤销。已覆盖两并发请求只进一次 gateway、运行超过 stale 窗口仍不二次调用、提交后重启、提交前本地失败、OCR confirmation 指纹冲突、turn-save 崩溃窗口重启补偿、过期 confirmation 的 completed 重放、普通重试阻断和显式确认后重试。

### OCR 题目→分步提示门禁（2026-08-28）

“这道题不会 / 看一下这题”只会打开本地高拍仪采集，不会直接把 OCR 文字当作题目答案依据。每一次清晰 OCR 也必须先核对；低置信或质量警告会明确标成“人工修正”，孩子可逐字修正后确认。专用 `POST /v1/sessions/{session_id}/tutor-ocr-confirmations` 将 `artifact_id`、当前 child/session、确认文本和 SHA-256 哈希、确认时间、24 小时到期时间及幂等 `client_action_id` 持久化在本地 SQLite 的 `tutor_ocr_confirmation`。同一 action id 只有在会话、工件、规范化文本和确认方式完全相同才会重放，否则返回 409 冲突；“核对无误”必须与规范化 OCR 文字完全一致，任何文字变更只能标为“人工修正”。没有可读 OCR 文字必须重拍，不能以猜测或空白人工输入越过门禁。

讲题只接收 `ocr_confirmation_id`，服务端重新核验该记录未过期、同会话、同孩子且对应 `document_ocr` 工件；模型和保存的 tutor turn 都只使用确认记录中的文本，忽略请求体任意 `question`。图片、base64、摄像头帧和原始文件路径不传给模型。刷新或本地服务重启后，未过期确认仍可解析；渲染器草稿超过 24 小时不恢复。取消讲解或隐私暂停目前只中止渲染器正在等待的请求与播放队列，不能保证已经送达的后端模型调用被远端取消；题干确认会保留，界面如实允许稍后重试。

现有真实路径为：本地 ASR → `/agent-turn` → 受状态机约束的计划/知识/OCR/观察等工具 → `/agent-turn/{id}/outcome` → session TTS stream。TTS 只是输出呈现层，不拥有或篡改计划、知识诊断、OCR、摄像头、错题及报告的系统记录。

## 建议主分支合入顺序

1. 先合入 `agent_tools.py`、状态机约束、端到端测试和本地声音特征模块；确认不改变既有会话状态与审计语义。
2. 合入 `voice_synthesis.py`、`Settings`、session TTS 路由及后端测试；上线时仍保持 `HSC_TTS_PROVIDER=disabled`。
3. 合入 Electron `voice-settings.ts`、`local-service.ts` 环境注入、preload IPC 与家长语音设置页；在 Windows 打包应用确认 safeStorage 可用。
4. 最后启用某一个供应商 relay：附上供应商账户地域、音色 ID、原创/儿童/商用许可、数据处理条款、固定文案试听记录和 30 次延迟/取消基准，再由家长开启。
5. 合入开发期的回环 CORS 限制与动态会话验证：仅允许 `localhost`/`127.0.0.1` 的任意端口，保证并行 Electron 开发服务不会因 Vite 自动换端口失去本地 API；不得放宽到局域网或公网来源。
6. 合入本地儿童声音适配 v2：三段明确同意的环境校准仅保留不可逆统计特征，儿童档案分别存放；信号质量不足时重问而不假称 ASR 置信度；免按键模式遇到声音/环境不确定时只能转为人工确认或文字。家长端以服务端 `ready` 为唯一完成真值；断线保存时，浏览器仅保留 24 小时的特征摘要、词表、强类型麦克风绑定、payload hash 与幂等 action（不含音频），刷新可重试。保存/删除都由 SQLite `BEGIN IMMEDIATE` 串行化：同 action+payload 重放同结果，旧保存无法在删除后复活，新的不同 payload 必须先完成删除再重新校准。删除未被服务端确认时，本机特征不清除且保留无音频的删除 action 供重试。服务端 schema v64 在 v62/v63 校准记录上增补麦克风字段和保存 action 表；Windows 打包 smoke 必须校验 v64，升级快照保留并可恢复，未来 schema 仍拒绝降级打开。
7. 合入 `agentToolRecovery.ts` 与 `SessionWorkspace.tsx` 的讲解失败恢复：仅允许无副作用的讲解重试；若服务端返回 `TUTOR_RESULT_UNCONFIRMED`，恢复卡必须改为“确认再次请求 / 保留问题，改为新请求”，不得显示可自动重放的“重试讲解”。验证草稿刷新/本地服务重启后仍保留该门禁，并在真实 Electron 学习会话故意使讲解服务不可用后截图验收。
8. 合入 `tutor_ocr_confirmation.py`、`tutorOcrEvidence.ts` 和 OCR 题干核对 UI：先部署服务端确认 API 与本地 SQLite 表，再部署渲染器，保持现有 `DocumentCameraCapture` 只在本地 OCR；验证模型请求审计的 context 不含图片或原始文件路径。
9. 合入 `tutor_request_reservation.py`、`tutorRequestRecovery.ts` 与本次 `SessionWorkspace`/Electron 开发验收入口：必须一起启用，避免旧前端生成新 action ID 破坏断线重试语义；再以配置好的真实模型重新跑动态讲题、取消与恢复验收。

## 真实阻塞

- 2026-08-28 对用户明确授权的 token-plan 凭据做了最小、无隐私探测：`/compatible-mode/v1/models` 返回 `qwen-audio-3.0-tts-plus` 和 `qwen-audio-3.0-realtime-plus`；`/audio/speech` 返回 `400 InvalidParameter (url error)`，说明该兼容地址不是 OpenAI TTS REST 端点。其官方 WebSocket 可鉴权并返回 `task-started`，但用待验收主声 `longyingtao_v3` 合成固定句返回 `task-failed` / CosyVoice engine `411`、零音频（首音无、总 4590.6ms）。因此这把凭据目前**不能直接启用**所选主声；未保存、输出或审计该密钥。
- 同一账户改用官方 DashScope `SpeechSynthesizer`、WebSocket `wss://token-plan.cn-beijing.maas.aliyuncs.com/api-ws/v1/inference`、`qwen-audio-3.0-tts-plus` 与其自身的成人女声 `longanlingxin` 后，固定无隐私句真实返回 PCM 24kHz/16-bit 数据 102,300 bytes；SDK 首包 638.9ms、总 1067.8ms。已新增 `qwen_audio_tts` provider，将 SDK callback 流转成既有 framed-PCM 协议；它只能用 Qwen 的独立成人女声音色，绝不冒充或静默替代 `longyingtao_v3`。凭据曾因终端 TTY 回显出现在本轮工具输出，必须立即轮换。
- 已实际播放只读官方 CosyVoice 候选试听，并完成候选角色限制；但没有提供已签约国产 TTS 账户/API key、工作空间、原创女声音色的儿童/商用许可与真实文本流。因此自然女声尚未从 Agent 回复实际合成、试听或选定，不能宣布完成。
- 当前开发机不是 Windows 无独显 4C/8T/16GB 目标机；CPU 基准工具已就绪，但没有目标机的 ASR/TTS 延迟、内存和并发数据。
- 2026-08-28 在隔离 Electron 开发档案（Vite 自动选用 `localhost:5174`）实际启动学习会话、提交动态 Agent 指令，并写回 `agent-turn` outcome；渲染器确实请求了 PCM TTS 路由。未配置云端声音时路由返回 `503 TTS_NOT_CONFIGURED`，界面显示“本机系统播报（非自然女声）；回复文字仍保留”，没有把它伪称自然声。该窗口验证了 CORS、会话、文本保留和显式降级；不等同于 Qwen/CosyVoice 的自然女声播放。
- 同日以临时隔离的本地服务运行新的 30 轮 TTS 交付基准：30/30 为 `503 TTS_NOT_CONFIGURED`，没有 PCM、没有首包/总时延、没有凭据或文本写入报告。这是未配置失败路径的真实证据，绝不是自然女声性能结果；配置新的安全凭据后必须重跑并产出成功/取消/恢复数据。
- 同日以公开 Sherpa-ONNX `test_wavs/0.wav` 实跑 CPU 输入鲁棒性评测：干净、合成 20 dB/10 dB 噪声、固定短反射远场近似和削波五个样本均有本地转写；冷启动首例 408.1ms，其余为 44.6–48.7ms。报告只记录源/转写哈希、字符数、状态和时延，输入与转写均不持久化。它不包含儿童真实语料、不衡量 WER，也不是 Windows 最低配置结果。
- 当前没有新的、由家长安全设置保存的已轮换语音凭据，因此不能合法地执行动态 Qwen PCM 播放、30 轮云端基准或真实云端取消。已泄露的旧凭据不会再使用。
- 已在开发机用真实 Electron 窗口和真实本地 FastAPI 服务检查了会话语音面板及家长自然语音设置页；修复了 1280px 宽度下试听/保存按钮被压成竖排的问题。此证据不替代 Windows 无独显目标机上的声音、麦克风与性能验收。
- 2026-08-28 已在隔离 Electron 档案（`localhost:5174`，无模型密钥）完成真实路径：四年级会话中录入课程/作业、生成计划并进入学习；Agent 口述式提问触发实际 `MODEL_NOT_CONFIGURED`，界面没有展示内部错误，而是显示“重试讲解 / 请家长帮忙”。刷新后草稿和恢复卡仍在；随后刻意终止该隔离本地服务、通过“重启本地服务”重启，再继续会话，恢复卡和原问题仍在。第二次重试显示更明确的家长中心路径。该检查验证 UI、持久化、服务重启和无模型恢复，不代表讲题模型可用或自然云声已配置。
- 本次新增 `HSC_CAPTURE_ACTION=ocr-tutor-recovery` 开发期 Electron 入口：它只生成公开算术图片，经可见的“看一下这题”→导入图片→本地 OCR→人工核对→提问路径验收；不申请摄像头、不读取家庭文件。无模型密钥时必须停在可见的真实恢复卡并刷新验证 action ID 草稿，不能以模拟讲题答案替代。配置真实模型后仍需追加一次可中断的动态模型请求验收；当前未配置状态不能证明远端取消。
