# 行为纵向建模与本地 OCR 集成说明

## 已接入的主应用流程

- `home_study_agent.behavior_model.BehaviorModelService`：把低敏可观察事件写入主 `agent.db` 的受管表，生成带置信度、缺失观测和替代解释的概率状态；提供回放、日级特征/基线、带冷却和每日上限的干预建议、孩子接受/拒绝及后续效果记录。旧 `behavior-model.db` 只在启动时以可恢复事务迁入主库；碰到同主键不同内容会失败且不写完成标记，成功后原导入源归档。若完成迁移后同路径出现新的 live sidecar，它会隔离为本地 quarantine 文件并写入审计，绝不静默删除或再次导入。家长删除 `media`/`all_history` 时会同时擦除归档与隔离副本中的该孩子数据，旧主库备份恢复也不能借原边车库复活已删记录。备份、恢复、导出和孩子范围删除均覆盖主库行为数据。日级特征覆盖在座/离座、书写、阅读、切换、求助、卡顿/分心/疲劳候选、计划偏差和孩子 check-in；六天以上才显示“近期/此前”的计数趋势，始终标注为可观察事件而非诊断。
- `/v1/sessions/{session_id}/ocr` 保持原有前端 API 契约，但现在先进入 `CpuScheduledMediaAdapter` 的本地 CPU 队列和 `LocalOcrService`，再由既有 `LocalMediaAI` 将**同一次**管线结果写入唯一的 `capture_artifact` 系统记录，不会启动第二个 OCR 引擎。原始画面的遮挡/过曝/阴影质量门会与管线的页检测、透视、模糊/反光提示合并。`details.local_pipeline` 保存引擎、队列、页质量、题块和人工复核边界；既有前端 OCR 上传、候选题编辑、知识点选择和错题保存路径无需换接口。
- `/v1/sessions/{session_id}/observation-frame` 的持久化观察事件会进入 `BehaviorAppIntegration`；计划项开始/完成/改期及 `/observation/correct` 的孩子纠正也进入行为时间线。OCR 完成与 ASR 完成也会写入同一会话时间轴，但事件只含 artifact/voice ID、用途、时长、置信和人工复核状态，绝不复制页面文字、转写文本、图像或音频。家长中心的“学习过程证据”面板按需读取回放和基线；这些读取与干预决定 API 都需要 `X-HSC-Parent` PIN 会话，且不会将状态解释为情绪、人格或诊断。
- 观察设备打开时，孩子端先写入无媒体的会话资格记录（取景质量、人物基线、原因、尝试次数）；在该写入成功且本次帧同时满足照明/清晰度与人物基线前，预览可以保留，但不会提交 `observation-frame`、写入长期行为事件或提示孩子。暗、过亮、冻结与无首帧只保存结构化失败原因并释放设备，不保存原始帧。资格、暂停、重试与恢复均随 `agent.db` 持久化；恢复会重新建立基线。家长运行状态只显示聚合资格、近七日纠正和**当前仍生效**的自动影子模式，不显示内部 ID、帧或历史已解除告警。
- 实时提醒只走一条链：`ObservationEngine` 只在本机派生、低帧率地持久化观察事实与证据资格，`BehaviorAppIntegration` 只检查温和模式、活动任务和孩子主动暂停，之后由 `BehaviorModelService.decide_intervention()` 独占冷却、频率上限、个体化措辞和反馈样本。它不再从实时观察写 `agent.db` 的 `intervention_record`；该表仅为旧版本审计/迁移读取，不能再当提醒预算。返回给孩子端的可追踪 `intervention_id` 不会被界面展示，孩子可选择“愿意 / 稍后 / 这类提醒少一点”；反馈写入独立行为库，十五分钟内可跨刷新/服务重启恢复，之后明确过期且不会补问。
- 孩子或家长纠正本地观察时，原事件和概率状态仍会保留并标记为“已回撤”，但会同步撤回由同一事件触发的未完成提醒，并将其排除出个体化反馈样本、基线与下一日报告。同一纠正 action 只可重放相同事件和文字；改内容或跨事件复用会被拒绝。纠正后的 30 分钟停止条件从纠正写入时刻开始计算，提醒撤回状态也随主库持久化。父母报告只通过 `report_safe_context()` 读取权威结构：`behavior_context_authoritative`、`behavior_intervention_feedback`（`intervention_accepted`/`intervention_declined`）与 `behavior_model_summary`；候选和反馈只给稳定不透明引用、来源、时间、置信度和模型版本，不复述纠正原话、不泄露数据库 ID，也不把候选状态写成学习结论。提醒效果要求作用域内 `client_action_id` 与规范化 payload 完全一致才可重放；跨孩子、跨会话、跨提醒或改内容均拒绝。观察事件还会记录产生它的本地模型版本；手动/语音来源沿用相同事件链，仅保存来源、置信度和最小结构化字段，不保存图像、音频或转写文本。
- `home_study_agent.local_ocr.LocalOcrService`：本机内存图像处理；稳定帧由 `vision_pipeline.FrameStabilityGate` 提供。作业页质量/页检测、对比度和阴影归一化、OCR、题块候选、队列、取消、超时、缓存、健康检查和无模型失败都可重复验证。
- 两个模块不上传原始图像、音频或视频；OCR 持久化仅保存哈希、派生文字/区域/质量度量。原始资料保存仍由既有 `LocalMediaAI` 的本地 AES-GCM 流程控制。

## 主分支合入顺序与冲突点

1. 先合入 `behavior_model/`、`local_ocr/`、`vision_pipeline/` 和 `evaluation/`；它们使用三个独立 SQLite 文件，不需要修改共享 schema 或数据库迁移版本。
2. 再合入 `local_media_ai.py` 的只读 artifact 取回方法、`observation_engine.py` 的 `state_event_id` 返回，以及 `app.py` 的服务初始化、恢复、现有 OCR/观察/计划路由和新增行为 API。冲突集中于 `create_app`、lifespan、exception handlers、计划项路由和 OCR 路由；应以当前主分支的新路由为基线，逐段重放本补丁，不能覆盖他人 `app.py` 改动。
3. 前端继续调用未变化的 `/ocr`、`/observation-frame`、`/observation/correct`、候选题与错题 API；核心闭环不需要接口迁移。`DocumentCameraCapture` 已将高拍仪理想预览请求升至 4K（仅取用户确认的单帧），并把 `details.local_pipeline.manual_review` 转为“需人工确认”的文字提示；不显示内部 JSON，也不把手写或公式说成已自动判对。家长中心新增按需加载的“学习过程证据”面板，显示候选状态、替代解释、个人基线、日级可观察趋势、提醒冷却和孩子反馈；不显示原始画面、音频、或调试 JSON。
4. 高拍仪服务必须先向现有 `device_registry` 取得短期 `document_camera` lease；将**已复制**的帧交给 `FrameStabilityGate`，不要让该模块自行打开相机。主任务使用设备时返回 `device_busy` 并允许排队/取消。
5. 启动时建立 `CpuResourceQueue(Settings.data_dir / "cpu-resource-queue.db")` 和 `CpuScheduledMediaAdapter`：行为、ASR、OCR 分别预约 1/2/3 个 CPU 线程，合计不超过 6；先让行为/ASR完成，OCR 排队或运行时将观察分析降至 0.25FPS，绝不降低预览或抢开设备。服务重启调用 `recover_orphaned()` 恢复遗留 reservation。所有引擎的默认路由是 CPU；OpenVINO、DirectML、NPU 只能作为明确可选加速，失败立即回退 CPU。
6. 高拍仪只以 `single_frame` 进入 OCR，最大 4K；视觉行为调用必须经 `BehaviorEventAdapter.from_vision_signal`，输入不高于 640x640/2FPS（正常上限 0.5FPS）。`LocalMediaAI.transcribe` 的调用方先入 ASR 队列，仍使用其现有的 2 线程本地 Sherpa-ONNX 路径。

### 高拍仪原图删除而保留派生记录

原图默认不保存。家长曾明确开启本地加密原图留存时，可调用受家长令牌保护的 `DELETE /v1/parent/capture-artifacts/{artifact_id}/raw-media`（确认词“确认删除”）只删除受管加密原图：OCR 派生文本、质量提示、人工校对、错题和知识链均保留。操作具有 action-id 幂等、失败后可通过既有删除重试接口恢复；重启后仍会完成遗留文件清理。接口绝不回传本地路径或原图。该独立分支已用模拟稳定页验证“质量结果 → 本地 OCR → 校对确认 → 错题候选/保存 → 删除原图 → 重启后派生记录仍在”，并覆盖模拟文件被占用后的重试；这证明本地软件链，不代表 Fic760x 物理采集成功。
7. OCR 完成后，将派生的 `lines` 写入既有 `capture_artifact.details_json`，再调用已有 `MistakeCaptureService.prepare`。低置信度、双页、手写/公式场景必须进入可编辑人工确认，不能自动入错题本。

| 共享文件 | 本分支适配点 | 合入冲突处理 |
| --- | --- | --- |
| `services/local_agent/src/home_study_agent/app.py` | 服务初始化、OCR/观察/计划事件适配与行为 API | 只重放行为/OCR 的局部初始化和路由；以主分支同名路由为准，绝不整文件覆盖。 |
| `local_media_ai.py` | 接收已完成的本地 OCR 结果，作为唯一 artifact 写入者 | 保留主分支的媒体加密/质量字段，再传入 `prepared_result`，防止双推理。 |
| `observation_engine.py` | 将持久化观察事件 ID 交给行为适配器 | 保留主分支采集节流和隐私暂停逻辑。 |
| `src/renderer/src/ParentCenter.tsx` | 按需“学习过程证据”面板 | 只加入懒加载组件；不要重排其他家长中心卡片。 |
| `src/renderer/src/api.ts`、`types.ts` | 行为回放/基线类型与 API 函数 | 追加类型和方法，保留主分支现有导出及错误处理。 |
| `src/renderer/src/styles.css` | 证据面板和 OCR 人工复核提示样式 | 追加命名空间类，不修改共享布局令牌。 |

## 验证边界

- 已在离线合成帧/事件的自动化测试中覆盖持久化重启、跨重启基线、回放、缓存、取消、引擎崩溃回退、超时隔离/隔离引擎、无模型、模糊页、可能双页、手写/公式人工复核标记、设备忙和队列恢复。2026-08-28 的完整本地代理回归有确定退出状态：`259 passed`（1 个 LibreSSL 环境警告）；主集成分支的 OCR/CPU/公式专项回归为 `21 passed`。
- `scripts/run_observation_quality_benchmark.py --output <aggregate.json>` 以生成的无身份帧与确定性检测信号走生产 `/observation-frame` 入口，输出 `present`、`writing_like`、`away`、`unknown`、`paused_like` 的混淆矩阵、unknown/误介入率、缺失源、P95 耗时、纠正回撤及自动退回影子模式证据。它不保存帧、不报告身份或嵌入，也不是人脸检测、注意力、情绪或心理状态准确率。暗/过亮/模糊帧不会形成运动基线或进入长期模型；暂停后的帧明确返回 `OBSERVATION_PAUSED`。2026-08-28 合成 API 基线为 unknown 率 0.25、误分类率 0、误介入率 0、P95 19.3ms、原始媒体 artifact 0；L523 当前过暗，仍缺真实硬件质量验收，不能把这条离线基线外推为家庭摄像头表现。
- `scripts/run_local_ocr_offline_benchmark.py` 已在临时目录跑过生成页基准；`scripts/run_local_ocr_real_benchmark.py` 已可根据未纳入版本控制的本地 manifest 对已保存、非敏感离线帧输出纯聚合指标，报告不含原图或识别文本。加 `--app-e2e` 时，只有清单显式 `end_to_end` 的、带人工校正题干和知识点查询的样本才会在隔离的本地应用数据库跑到错题保存；它另报告人工复核建项/确认/跳过数量、OCR 与应用入口失败桶、端到端耗时、会话完成、重启恢复和原图零留存率。清单要求人工操作却没有实际低置信或专项模型证据时，明确记为 `manual_review` 失败，绝不伪造手写、公式或红笔结果。评测数据库强制关闭原始媒体保存、云端文本/音频和模型改进权限。它验证工作流，不作为 OCR、手写或公式准确率。清单尚无家长批准样本时，它会明确输出 `awaiting_parent_approved_local_samples`，各准确率为 `null` 或 `not_evaluated`，不会伪造通过。没有在工作树或主目录找到可使用的本地非敏感高拍仪样本。因此当前仅有印刷/故障管线路径结果，手写、公式和批改符号准确率仍为**未评测**，不能引用印刷 OCR CER 代替。
- 已用本分支隔离服务的真实 HTTP API 完成一次无家庭数据的持久化闭环：本地公共公式图片 → OCR（人工复核必需）→ 候选题生成 → 人工修正 → 错题保存 → 知识薄弱点；重启服务后同一 `mistake_id`、弱点和空待处理队列仍存在。该验证证明适配层的系统记录与重启恢复，不证明高拍仪物理采集、中文、手写或公式准确率。
- 前端类型检查与完整 Vitest 回归已通过：23 files、109 passed、1 skipped。该验证使用已有的主目录依赖目录的临时只读链接，测试后已移除链接；没有安装、升级或提交依赖。
- 已在主服务中完成两次 `document_camera` 180 秒短租约登记与释放；早期终端 ffmpeg 与 Electron 隐藏采集适配器均在物理流打开阶段失败/超时，未写入原始图像，且会话复核为 0 个活动 lease。该路径不作为物理采集成功证据。2026-08-28 已在可见的主 Electron 学习会话中实际点击“打开高拍仪”：渲染器返回 `BOUND_DEVICE_NOT_FOUND`，随后只读 macOS `SPCameraDataType` 枚举仅有观察摄像头 `L523`，未枚举先前绑定的 `Fic760xUsbCamera (eba4:1303)`。返回首页后，真实窗口明确显示“观察已暂停”，两类摄像头均为“应用当前未读取”，因此本轮没有遗留应用级采集占用。硬件阻塞是**高拍仪当前未被操作系统枚举**，而不是等待授权、OCR 失败或设备忙；没有申请权限、没有抢占人体追踪摄像头、没有保存原始图像。为防 Chromium 完整重启轮换 `deviceId` 造成同一设备误报丢失，`findBoundVideoDevice` 已加入唯一精确规范化标签回退；哈希仍为主证明，标签为空/重复/不精确时保持拒绝并要求家长重新绑定。待高拍仪重新被 macOS 枚举后，只能在可见主窗口以单帧复测稳定帧、裁切、OCR、人工修正、候选题/错题/知识链与重启恢复。
- 目前 RapidOCR 覆盖印刷中英数字；在 MathWriting 500 条渲染公式上的真实 CPU 基线为 500/500 运行、P95 0.262s、峰值约 3.70GB、严格 LaTeX/结构匹配 0.0。该 provider 明确是**非公式专用** OCR 基线，证明不可替代公式识别。官方 Pix2Text 1.1.5 的 MFR-1.5 ONNX CPU provider 已按固定 revision `1cef9f0bdcd6a4c63df7de1311fb0894593340cc` 下载到工作树外，10 文件/119,665,593 bytes 的 SHA-256 清单已提交；在 `HF_HUB_OFFLINE=1`/`TRANSFORMERS_OFFLINE=1` 下对 MathWriting 500 张真实预测全部完成、零失败、冷启动 2.169s、P50 0.095s、P95 0.278s、峰值 717,701,120 bytes。但严格 LaTeX/结构匹配仅 0.026，规范化 CER 4.4868、token error 3.9078（`symbols` split 严格匹配 0.0），**不达自动公式录入门槛**，只能触发人工公式编辑，不能外推为拍摄作业、中文手写或整页检测能力。MFD-1.5 尚未下载/实测。手写与数学结构识别仍是数据/本地模型迭代阻塞项，不能以印刷 CER 代替。方向阶段目前保守地标为 `unverified_0_degrees`，需要引入本地方向分类器后才可自动旋转，不能把它声称为已验证的自动方向能力。
- 依赖与权重的授权边界见 `LOCAL_OCR_DEPENDENCIES.md`：RapidOCR/ONNX Runtime 的本机包元数据已记录，但 PP-OCR ONNX 权重的独立再分发许可证仍是发布前核验项，不能由库许可证替代。

## 共享评测资源（只读，不随产品发布）

共享资源根目录由运行命令显式传入，不能复制进应用数据目录或发行包：

```powershell
$env:PYTHONPATH = 'src'
python scripts/run_shared_ocr_asset_benchmarks.py `
  --assets-root C:\hsc_eval_assets\ocr `
  --data-dir C:\HSC-eval\shared-runtime `
  --math-runtime-report C:\HSC-eval\mathwriting-pix2text-mfr.json `
  --output C:\HSC-eval\shared-ocr-report.json
```

`mathwriting-pix2text-mfr.json` 必须先由 `run_mathwriting_formula_benchmark.py --provider pix2text_mfr_onnx --model-dir <已校验本地缓存> --model-manifest model_manifests/pix2text-mfr-1.5.json` 生成。共享 runner 只会接受 cohort 与 500 条数量相符、并明确 `implicit_network_download: false` 的本地聚合报告；缺失、错集或不证明离线策略时仍输出 `not_evaluated_invalid_local_formula_runtime_report`，不会把别的公式结果混入。

- `mendeley_exam_marks`：50 对批改前后页逐页按相对路径配对，执行仿射/相位相关对齐和“新增红墨”差分组件生成；每个候选带相对页 ID、raw-page 像素 bbox、面积和 tick/cross/circle **候选**，没有逐组件人工真值，不能报告为批改符号分类准确率。它是 CC BY 4.0 的英语大学试卷，不能代表中文 K-9。
- `mathwriting_excerpt`：500 个 InkML 渲染公式独立报告。`run_mathwriting_formula_benchmark.py` 提供可选的 CPU Pix2Tex adapter（显式 checkpoint）、官方 Pix2Text MFR-1.5 ONNX adapter（显式 model dir + SHA manifest，`local_files_only`）和标明非公式模型的 RapidOCR 下限；只有传入本地 `id`/`latex` 预测 JSONL 后才计算去空白后的严格 token 相等、规范化 CER 与 LaTeX token error，它们都不是符号数学等价判断。`eval/results/mathwriting_pix2text_mfr_1_5_cpu_20260828.json` 记录 MFR 的真实低质量结果，不代表公式能力。该集为 CC BY-NC-SA 4.0，不能随产品再分发，也不代表拍摄作业准确率。
- `scut_public_samples`：4 个 HCCDoc 行裁剪和 1 个 SCUT-EPT 样例只做公开样例 smoke。低于 80px 的行裁剪仅在评测内存中上采样，不改变高拍仪页面 API 的 80px 输入门槛；不报告中文手写 CER。完整数据集须按其非商业研究/申请条款取得。
- `family_document_camera`：只接受本机、脱敏的南京四年级/初三高拍仪页，按印刷、手写答案、公式、批改符号分别报告；不能用上述任一公开集替代。高拍仪目前未被 macOS 枚举，尚无本轮可报告页，不得填充准确率。

已生成的只含样本 ID、聚合指标、候选 bbox 和失败样本 ID 的本地基线报告为 `eval/results/shared_ocr_assets_20260827.json`：50 对均已对齐（49 仿射、1 相位相关），501 个新增红墨候选；SCUT 5/5 完成 smoke（非准确率）。`eval/review_templates/mendeley_marks_20260827.json` 是 24 个候选/13 页的人工真值审查包，`scripts/prepare_mendeley_mark_review.py` 可本地写出 raw/对齐 corrected/差分叠加图及候选裁剪；在全部页面真值和候选判定填写前，P/R 必须保持 `pending_manual_review`。MathWriting 的实际下限见 `eval/results/mathwriting_rapidocr_cpu_baseline_20260827.json` 与实测但不合格的 `eval/results/mathwriting_pix2text_mfr_1_5_cpu_20260828.json`。

### 家庭本地代表性基线（不提交样本）

`scripts/run_local_ocr_real_benchmark.py` 只接受工作树外、已脱敏且家长批准的本地 manifest。它按四年级/初三分别输出 **印刷中文、印刷英文、印刷数字、手写字段、公式结构、批改符号** 六类 cohort；印刷 CER 只能出现在对应文字类，绝不会填充手写、公式或批改指标。`LocalOcrService` 的 RapidOCR 路径没有手写、公式或批改专用 provider 时，即使已有本地人工真值，也会明确输出 `not_evaluated_missing_local_specialist_provider`；人工真值仅标记为可供后续专用模型评测，不能被当作模型准确率。

```powershell
$env:PYTHONPATH = 'src'
python scripts/run_local_ocr_real_benchmark.py C:\HSC-eval\family-manifest.json `
  --data-dir C:\HSC-eval\runtime `
  --output C:\HSC-eval\aggregate-only.json
```

2026-08-28 在只读共享资产上的复跑结果是：批改页 50 对对齐率 1.0、501 个新增红墨**候选**（非 P/R）；MathWriting MFR-1.5 500 条覆盖率 1.0、严格/结构匹配 0.026（不合格，人工公式编辑降级）；SCUT 中文手写 5/5 仅 smoke（无 CER 真值）。没有获准的家庭高拍仪页面，所以六个家庭 cohort 全部保持未评测，不能用这些公开数据或普通印刷结果替代。

### 公式区域专用路由（默认关闭、绝不自动填题）

设置 `HSC_LOCAL_FORMULA_PROVIDER=pix2text_mfr_onnx`、`HSC_LOCAL_FORMULA_MODEL_DIR` 与 `HSC_LOCAL_FORMULA_MODEL_MANIFEST` 后，服务只对已由印刷 OCR 行中数学符号识别出的局部区域调用已校验的本地 MFR-1.5 ONNX CPU provider；缺少任意本地资产时不联网下载，而是记录 provider 不可用并创建人工公式编辑项。`HSC_LOCAL_FORMULA_PROVIDER=rapidocr_baseline` 仅可作普通 OCR 对照，同样绝不作为公式结构识别。

每次路由只保存 bbox、候选 LaTeX、provider 尝试状态、CPU 路径、进程 RSS 观测和区域延迟；不保存额外原图。当前 `auto_accept_enabled=false`，因为 500 条 MathWriting 的严格/结构匹配只有 2.6%，因此即使 provider 返回候选也不会改写题目或自动入错题，必须由家长/孩子在持久人工公式编辑队列确认。手写与批改符号仍没有共享 provider 或质量声明。

公式 provider 在 Windows-compatible `spawn` 独立进程运行，不在不可终止的线程中执行。每次运行有 5 秒默认超时、单并发、1.5GB 子进程 RSS 阈值、连续两次超时/崩溃/内存越限后的 60 秒冷却；取消、超时、崩溃或越限都会终止并回收子进程，主印刷 OCR 继续可用且不写 candidate。PyInstaller Windows 入口调用 `multiprocessing.freeze_support()`，构建命令显式包含 worker hidden imports；父进程意外退出时 macOS/Linux watchdog 与 Windows kill-on-close Job Object 都会回收公式子进程。worker factory 只接受代码内 allowlist key，不接受任意 module/factory 字符串。

macOS 的 spawn harness 已覆盖 worker 成功、硬超时和父进程被终止后的回收；**目标 Windows/PyInstaller EXE 尚未实机验收**，不能用静态 gate 或 macOS 结果替代。

当前有两层候选：`printed_line_contains_math_symbols` 是文字证据充分的区域，可调用本地 provider 并进入人工公式编辑；`layout_math_candidate_unverified` 仅根据页面墨迹／字形布局发现疑似表达式，**不是**页面公式检测或结构识别。后者被隔离为有上限的 `layout_diagnostics`：`diagnostic_only=true`、`unverified=true`，不调用 provider、不创建家长复核项、不改变 OCR 文本或错题／知识点链；诊断分析超过 200 万像素会先下采样，单页最多保留 8 个候选且最小区域为 100×20 像素。`run_shared_ocr_asset_benchmarks.py` 可重复输出其公开资产触发统计；这些计数不是 precision／recall 或公式准确率。两者都不能当作手写或批改符号能力。

可只运行此诊断统计（不载入任何公式模型）：`PYTHONPATH=services/local_agent/src .venv/bin/python services/local_agent/scripts/run_formula_layout_diagnostic_benchmark.py --assets-root <本机用户目录>/hsc_eval_assets/ocr --output services/local_agent/eval/results/formula_layout_diagnostics_20260828.json`。

- Windows 最低线和验收判定由 `evaluation.runtime_benchmark.assess_minimum_pc`、`scripts/collect_minimum_pc_environment.py` 及 `scripts/evaluate_minimum_pc_runtime.py --require-collected-environment` 固化：双摄 60 分钟、10 秒 ASR P95≤2 秒、单页 OCR P95≤5 秒、稳态 CPU≤65%、内存峰值≤12GB、2 小时无持续增长。采集器在目标 Windows 主机实测 CPU 线程、内存、NVMe、GPU 枚举和 AVX2 证据，不接受当前 Mac 冒充结果。当前只有离线逻辑验证；最低档 Windows x64 实机尚未跑出可接受证据。
