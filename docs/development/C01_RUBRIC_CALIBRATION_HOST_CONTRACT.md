# C01 自动评分资格宿主合同

状态：核心定向 27 passed / 66.72s，核心源码冻结；宿主生命周期接线继续。本文不是生产校准证书，也不表示 C01 或完整产品完成。
新增文件仅 `rubric_calibration.py`、`rubric_calibration_evidence.py`、专测和本文。
没有改动共享评分器、数据库迁移、HTTP、Runtime、隐私和前端。

## 权限与事实边界

- `rubric_assessment` 仍是模型评分的唯一记录，`rubric_review_event` 仍保存人工标签；不补写 diagnostic correctness，不把 reference 改成 exact。
- 两张新表只保存协议、ID、hash、资格元数据；不保存题面、原答、原图、原音、模型提示或凭据。
- 评分资格只说明本次各维度评分可采用，不能证明作答独立、无提示、迁移、延迟保持，更不代表 PET 证书或全科掌握。
- 当前只有家庭 ParentAuth principal，不能用两个字符串假造独立审核身份。没有实际盲评、身份和留出凭据时明确 `insufficient`。
- 合成夹具只验程序守卫。生产 `holdout.corpus_kind=synthetic` 必须拒绝；测试适配器模拟的身份/版本证明不能安装为生产适配器。
- 只实现文本评分资格；原音声学评价不能拿本协议的文本证书替代。`original_written_text` 必须重新核验既有原稿授权及原稿版本。
- 不扩大模型调用权限。父级文本许可、精确题目/材料许可、原稿用途授权、Runtime 入场、模型预算仍由现生产链决定。

## 初始化与服务 API

```python
initialize(connection)  # 两表DDL，绝不commit/rollback调用者事务
service = RubricCalibrationService(database, authority_resolver=None, clock=None)
service.initialize()  # 独立连接 BEGIN IMMEDIATE + commit

service.freeze(child_id, *, scorer, scope, holdout_receipt_id, client_action_id)
service.record_case(child_id, protocol_id, *, assessment_id, blind_receipt_ids,
                    expected_revision, client_action_id)
service.evaluate(child_id, protocol_id, *, expected_revision, client_action_id)
service.revoke(child_id, protocol_id, *, expected_revision, client_action_id)
service.read(child_id, protocol_id)
service.requirements()
service.list_for_child(child_id, *, limit=50)
service.prepare_score_binding(connection, child_id, *, protocol_id, evidence_id, as_of)
service.revalidate_score_binding(connection, child_id, binding, *, phase, as_of, assessment_id=None)
service.qualified_score_receipt(connection, child_id, assessment_id,
                                *, protocol_id, as_of)
```

所有写命令返回 `{receipt, current}`。`receipt` 是原幂等事件；`current` 总是重新核验的现态。
旧 key 不重新发证，不重复登记，不在撤回后复活。新 key 旧 revision 冲突。
错误为 `ValueError('RUBRIC_CALIBRATION_...')`；未知/跨孩 protocol 为 `KeyError`。
`evaluate` 只比较已保存的预测/标签，绝不调用模型。

`current` 包含 `protocol_id, protocol_hash, revision, status, score_qualified,
blockers, scorer, scope, metrics, expires_at, requirements, local_only=true,
cloud_transmission_allowed=false`。

状态为 `insufficient / ready_for_evaluation / qualified / invalidated / revoked / quarantined`。
`ready_for_evaluation` 不等于已经发证。旧 assessment 缺绑定时不可自动升级。
`requirements/list_for_child` 无 DDL、无写入，也不因为缺协议就创建一条。

`prepare_score_binding` 只为已经 qualified 的协议准备新原答，不为留出样本授学习资格。
返回 `{assessment_binding, certificate_event_id, child_id, source_evidence_id, session_id,
diagnostic_item_id, binding_version, local_only, cloud_transmission_allowed}`。
`assessment_binding` 精确为 `{protocol_id, protocol_hash, scorer_hash, source_version}`，
宿主须将它存入 assessment 的 `payload.calibration_binding`；旧缺字段不升级。
`source_version` 是精确 attempt/evidence/item/node/原稿快照摘要，不是模型推断版本。
`revalidate_score_binding` 在 `dispatch` 返回同 packet；`persist/recover` 必须有原 assessment_id，
核实际 scorer、证书、原答与原稿再返回评分回执。它不授模型调用/题面上传权限。

## 精确评分器字段

`scorer` 必须仅包含以下字段，不能夹带 `qualified` 或降低阈值：

```text
provider, model, model_revision, revision_receipt_id,
prompt_hash, profile_id, profile_version, rubric_hash,
parser_version, projection_version, parameters_hash, modality
```

- 三个 `*_hash` 是 SHA256；`rubric_hash` 为实际完整 dimension 定义列表的规范 JSON hash（包括各标准、0..3 anchors、pass_score）。
- `model_revision` 可以是 null，意味着不具备稳定版本证明，不能获得资格。
- `profile_version` 对齐既有 assessment 的 `rubric_version`。`profile_id` 对齐其 `profile`。
- `modality` 目前仅 `typed_text`（仅文字内容，不证明输入设备）或 `original_written_text`。
- `scope` 精确为 `{subject, grade, task_family, strata:[...]}`；按范围分别校准，不能外推陌生学科、题型或质量层。
- provider `reported_model` 和 `provider_system_fingerprint` 只是报告的标识，不是不可变版本证明。
- 独立版本证明必须绑定当前 endpoint scope。primary/fallback 的实际 provenance 不能混用。

## 构造期可信适配器

```python
authority_resolver(connection, *, child_id, kind, reference_id) -> dict | None
```

这是宿主内部只读依赖，不是 HTTP request 字段。返回前必须核验真实持久记录、真实身份和当前撤回版本；禁止把 body 中的 dict、bool 或 reviewer 名称原样返回。
每种返回都包含 `receipt_id=reference_id, child_id, status='current'`。
任何证明变化须有不可复用版本/事件 hash，撤回再授权不得重用旧证明摘要。
以下 hash 不可由同一待审核模型自证。

### scorer_revision

```text
provider, model, model_revision, endpoint_scope_sha256,
verification_method, evidence_hash, verified_at, expires_at
```

`verification_method` 仅接受 `provider_immutable_revision_attestation` 或 `signed_release_manifest`。
适配器负责校验实际证明来源/签名和语义；核心再次核 exact tuple、时间和 proof hash。
没有这类证明就返回 None，不把普通模型 alias 或任意 metadata 包成上述名称。

### holdout

```text
corpus_kind='observed_original', blind_protocol_version='blind_labels_v1',
provenance_receipt_hash, scope_hash, frozen_at, development_group_hashes,
cases:[{evidence_id, group_hash, stratum, source_ids:[...]}]
```

- `evidence_id` 是已有同孩真实 attempt/evidence；不从本接口录入原答。
- `group_hash` 应由已审来源的题族、共享材料、近重复原稿/版本族归组。不能用随机 ID 把同一原文拆成独立样本。
- 一组只允许一个统计单元；同组不可跨开发/留出集合。同题修订不能增加有效样本数。
- `source_ids` 是完整来源依赖闭包，由现材料/来源 resolver 得到，必须保留精确本孩 source ID，不能只填题干漏材料。
- 核心另存每个本孩 knowledge_source 完整行的 hash；来源改名/许可/审核变化或删除都会使旧协议当前检查失败，不只检查 item 状态。
- 核心冻结每个实际 source snapshot，验证所有实际 item，拒绝已有模型评分的留出原答。
- 原稿、来源许可和人工标签真实性由合法采集/展示记录支持。不能复制系统原子图谱、教材 PDF 或家庭影像到校准 manifest。
- 冻结时缺 proof 的 protocol 不会在后来补 proof 后自动升级，需要显式冻结新 protocol；旧命令只返回历史回执。

### blind_label

```text
identity_authority='independent_reviewer_registry_v1', reviewer_principal_id,
identity_receipt_hash, display_receipt_hash, submission_receipt_hash,
mask_version='blind_no_predictions_or_peer_labels_v1',
protocol_hash, assessment_fingerprint, evidence_id, source_version,
reviewer_id, review_event_id, review_event_hash, displayed_at, submitted_at
```

- `review_event_id` 指既有 `rubric_review_event`；分数只从该表读取，不接受另传 labels。
- core 核同孩、同 assessment、exact event hash、时间、原答/来源/题面版本与完整维度。
- 两个 principal 必须是实际独立认证主体，不是同家庭 principal 的别名。
- 宿主须以真实展示回执证明当时隐藏预测/解释及另一审核人的标签。旧普通审核没有这种历史，不能追补一个 `blind=true`。
- 两标签分歧保持 `LABEL_DISAGREEMENT`；本轮不以多数票或取高分裁决。运营须完成可核独立裁决/新有效盲评流程再提交，不改历史标签。
- 标签撤回或后续修订使旧资格失效；不得对历史数字静默取最新替换。

### assessment_scorer

```text
receipt_id=actual_model_call_id, scorer_hash, protocol_hash,
scope_hash, stratum, model_revision, provenance_hash
```

适配器读取真实运输审计，核 actual provider/revision、parser/projection/prompt/profile/parameters，和原题的确定任务族/质量层；不凭 LLM 猜 task_family。
核心另读 `rubric_assessment.payload.scorer_provenance`，核其完整 hash 和
`provider, requested_model, system_prompt_sha256, parameters_sha256, endpoint_scope_sha256`。
`immutable_revision_verified=false` 的运输元数据必须另有真实 `scorer_revision` 才可能获资格；该 false 字段不被改写。

## 固定策略，不是考试量表校准声明

`rubric_calibration_policy_v1`：每个维度、每个范围层的 0/1/2/3 真标签各至少 50 个独立题族；分数通过线来自固定 rubric 的 2/3，不让 API 重设。
逐标准错误放行/错误拒绝的 Wilson 95% 上界分别不超过 0.05/0.10；覆盖率至少 0.95；严重高估（高出原标签至少 2 级）不得存在；最长有效 30 天。
这些是可版本化工程门槛，不是证明考试精度的法律/官方标准。样本不足时，即便全对、模型 confidence=1，也只能 insufficient。
abstain 不算正确，必须计入覆盖率；不得去掉失败层、漏材料题或只看平均分。
运营不能传更低阈值；改政策/模型/提示/量表/解析器/模态必须新协议，旧证书重核失败。

## 宿主消费与生命周期

- 原 evaluation 路径负责冻结 scorer provenance 和协议绑定，并在物理 POST / persist / response_ready recover 前重核当前许可和原来源。
- `qualified_score_receipt` 返回原 assessment 的维度分数和 passed、证书事件、原 evidence/source hash；不自动产生独立或迁移 evidence。
- 只有证书之后产生的新模型 assessment 可申请运行期资格。留出样本本身不作为孩子学习资格证据。
- 共享 `reviewed_receipt/current_writing_review` 需未来统一“人工或已校准模型”score-authority adapter，而不是伪造 `reviewed_human`。
- `specialized_stage_evidence` 和 `writing_stage_reviews` 的硬人工 scope、直接 `review['passed']` 消费仍待主授权接入；低分也必须能驱动补救，不能只接受正向分。
- 主应用已初始化内核并挂载家长/本地双鉴权只读接口；ChildModel、教学计划的校准资格消费及人工盲评 UI 尚未接入。校准后复用原 replay/reconcile，禁止造第二学习事实库。

```python
export_rows(connection, child_id) -> {'protocols': [...], 'events': [...]}
delete_for_child(connection, child_id) -> int
invalidate_sources(connection, child_id, source_ids, *, erasure_id) -> int
quarantine_after_restore(connection, restore_id) -> int
```

借用同一写事务，以上 hook 均不 commit。先 event 后 protocol 删除，child FK 保底 cascade；JSON 来源/assessment 引用必须由宿主精确闭包处理，不能声称 JSON 自动 FK。
learning/knowledge/media/all_history 导出/计数/删除、单源删除及恢复已接入现有生命周期；单源失效与原 purge/replay 同事务。
restore 对各旧 protocol 追加 quarantine，不 UPDATE 原记录；同恢复重试幂等。历史协议保留审计，但旧 key 不重新激活。
普通重启不撤销当前有效资格，只读重核全部原证据。受限来源仍本地；校准不是 cloud permission。

## 当前证据边界

核心日志 `/tmp/hsc-c01-calibration-core-final.log`：27 passed / 66.72s。
含固定 200 题族统计正向与证书后新 assessment、重启、源撤回、双孩、借事务回滚、并发 CAS、恢复 quarantine、实际参数错配和缺身份反例。
首红缺模块、物理参数错配红及单来源 hash 漏核红均保留；未重跑全后端。

专测采用临时真实 SQLite schema、真实原答和 rubric 表，以及明确的测试专用身份/语料适配器。它不证明具有真实校准语料、独立审核人或不可变 provider 版本。
没有发模型请求、读取正式家庭库、上传媒体或运行已停批任务。
生产缺样本/身份/版本证明时仍 reference/insufficient，但合法形成性教学、孩子修订与独立新题流程不因此停止。

### 生命周期稳定点（2026-09-08）

- `/tmp/hsc-c01-calibration-lifecycle-final.log`：9 passed / 68.50s，exit 0。真实主 app 的四 scope、双孩、local/portable 恢复、单材料源事务故障与原 key 重试、导出文件替换反例；未重复运行此组。
- 随后真实并发 HTTP 红例证实：导出已读旧数据但尚未写文件时，删除可先完成，晚导出会重新留下旧协议。仅将原 `export_child` 发布文件及审计的整个流程包入既有 `external_effect_guard`，与删除使用同一守卫，不新增锁或评分事实库。
- `/tmp/hsc-c01-calibration-lifecycle-affected-final.log`：29 passed / 87.87s，exit 0（session 86084 已退出）。包含新并发导出反例，以及 `test_material_source_deletion_host.py`、`test_curriculum_authority_lifecycle_host.py`、`test_curriculum_authority_privacy.py`、`test_report_source_issue_privacy.py`、`test_rubric_calibration_host_read.py`。这是并发修复后的受影响组合，不表示此前 9 项重跑或全后端通过。
- 受管导出只识别 exports 直接目录、准确 child 文件名、真实导出 manifest 与 `child_data_exported` 审计 SHA；不递归清扫，不删另孩或用户自行另存。删除前重核 SHA/路径，替换或文件操作失败保留原 deletion_job residual，可原 key 重试。
- `git diff --check`（本轮拥有文件）exit 0。核心 27 项对应的三个文件 SHA 未变化。当前 source/tests 冻结，无本任务在跑测试；未验证真实校准质量、生产 authority、Windows 或真实儿童效果。

冻结 SHA-256（源码/测试）：

```text
privacy_lifecycle.py                       11aee69a40f1b65b78913d49924cbc4411658041fe823bdbefdf3d8a32f1023a
knowledge_source_deletion.py               c9ba5cf71139eda082022e8c0a8b314ea4a268dc57cd50043f21124103fab889
rubric_calibration.py                      55389d4291882c0bd7adcd9cc745fa8261bf314c1ecb5b0b4c64a08033a0b4b9
rubric_calibration_evidence.py             d76e95a20f5a9e0bc1f378cc5c909cf0d733adfe9b4db557a824e2223f3c02f8
test_rubric_calibration_lifecycle_host.py   61eae8cffa7735efec51e589ad195308fc727c43b77a1b2a4f20566bf943d49b
test_rubric_calibration.py                 349b7a1780f528f9e9fa4db9419938446de54b428a6ed5c36f104aae98789e7f
```

## 下一阶段：外部基准与家庭漂移（仅合同，未实现）

### 当前实质缺口

`rubric_calibration_evidence.source_snapshot` 通过同孩 `diagnostic_attempt/learning_evidence` 读原答；
`rubric_calibration.freeze` 的所有 case 又要求该 source snapshot。
因此当前合同 **不支持外部公开/专业 benchmark**。不能通过建一个假孩子、假 session 或假作答来绕过。
`rubric_assessment.child_id` 及审核的同孩约束也说明：把公开样本塞进现学习表会污染孩子事实。
27 个程序测试不改变这个产品缺口。让每个家庭逐题答完并双人复核 200 题，不是可接受的日常产品方案。

### 拟议分层

1. 可复用外部校准基准：版本化原始题/材料、原回答、逐标准专业标签、真实独立审核身份与盲评过程，保存来源许可、版本和审计证据。独立于任何孩子学习状态。
2. 评分器资格：固定模型版本、提示、参数、量表与模态在预注册留出集上的真实预测及误判统计，由运营有限次验证，不由每个家庭重跑全套。
3. 家庭适用性和漂移：用已有、当前合法授权的日常原答做范围匹配/质量监测；必要时少量真实盲评抽检，发现反证暂停该家庭/该范围的资格。不能把少量抽检称作重新完成总体校准。
4. 孩子学习结果：仍只消费自己的 `rubric_assessment/attempt/evidence`。外部样本数不进入掌握度、进阶、复习义务、报告中的孩子成绩或参与时长。

### 最小可实施方向

- 新 `corpus_contract_v2` 明确区分 `family_original` 和 `external_benchmark`，不就地升级 v1，不把 foreign source_id 填进同孩 source_snapshot。
- 外部语料及已签名评测包是独立的只读评测资产，不是第二套孩子评分/学习事实库。协议/事件仍只引用 ID、hash、许可/版本和资格元数据；实际孩子的评分仍只在原 assessment 中。
- 外部包须提供：dataset/release/manifest hash、许可证与处理用途、题族/材料/原答去重分组、profile/anchor 映射、预注册开发/留出分割、标签审核凭据和争议裁决版本。
- 预测包须绑定实际 model/scorer provenance、评分前锁定的 manifest、全部尝试/abstain、运行顺序与完整样本清单，不能只导入筛选后的好分数或由调用者填一个 `passed=true`。
- 适配器只能解析已验证的外部专业身份/来源签名，不能把 ParentAuth 家庭 principal 变成多审核人。软件只证明验证链成立，不声称能凭字符串确认现实身份。
- 公共数据公开不等于许可证明确，也不等于独立留出；已被模型训练、调 prompt 或反复挑选的公开题只能作参考。若独立性无法核实，应使用合法的专业保留集/前瞻性新任务，不能替换标签洗来源。
- 外部标签与本项目 rubric 不一致时，需先冻结可解释的标准映射及独立验证；不能用测试集反复调映射后仍称盲测。数学过程/语文/物理解释/PET 写作分别保留真实范围，不作跨科外推。
- 稳定证书可作为各家庭的共同非私密依据，但部署到本孩仍要绑定允许范围、当前策略/来源许可和漂移状态；不能复用另一孩原稿、原音、人工评价或隐私授权。

### 仍需代码与运营工作

需要新的外部评测资产读取/许可与真实性核验、v2 corpus resolver、非家庭评测运行回执适配、证书失效与分发恢复合同；这些当前均未实现。
需确定外部资产与当前 child FK 存储隔离、备份/删除归属及来源撤回范围，不在本轮擅自改中央 schema 或新增评分表。
后续 Host 必须把 qualified score-authority 接到现 ChildModel/教学计划和撤回重算，不能在“能导入包/准备下一题”处结项。

运营成本包括合法语料/标签获取、标准映射、专业复核、受限次数模型评测、版本更新再评和漂移复核。
当前没有经核实的可用语料、预算或不可变模型版本，因此不能给出真实费用或声称外部证书可用。
可复用基准应使一次评测服务多个合适范围的家庭，而不是要求家庭各自支付/完成同样的大量校准。
本窗口只记录此合同，不下载数据、不外调模型、不启动付费/批量任务，也不降低现有门禁。


## 2026-10-26 独立评审身份与原音盲评工作流（待 Root 决策，未实现）

本节仅细化现有 W6 缺口。基线 Main2535/ed006a95：`rubric_audio_projection` 已消费 `original_audio_protocol_v2` 与四种 audio authority，`rubric_calibration_http` 目前只导入外部已签名证明；普通 `LearningRubricReviewPanel` 不是独立盲评界面。部署与投影 registry 仍空。本设计不增加评分公式、阈值或资格，不要求每个家庭重新完成基准校准，也不作为其他监督学习试用的新门。前文文本 v1 及既有普通人工审核保持原义。

### 1. 信任根与真实主体

- 拟议最小认证方式为：**外部受信主体注册证明 + 注册凭据的挑战签名**。注册证明须由另行审核安装的身份信任根签发，绑定稳定的真实主体标识、认证公钥/凭据标识、允许角色/范围、证明版本、有效期与撤销依据。名称、电子邮箱、两个公钥或自报 `independent=true` 都不能证明两个人；独立性、利益冲突及既往看过预测的适用政策由该真实注册方证明，软件只能验证证明链。
- 家长使用原 ParentAuth 授权指定本孩、用途、协议/范围的本地评审访问，并可撤回；这不是独立评审身份。即使同一人更换 key、别名或浏览器，也只能映射同一规范 principal，不能提供两份独立标签。已有家庭 PIN/token 不能创建/签发独立主体。真实可信主体或可用撤销验证缺失时为 `REVIEWER_IDENTITY_UNAVAILABLE`，不得生成本地自签身份填满资格条件。
- 身份信任根和现 `rubric_authority_trust_v1` 的 envelope 签发根职责分开；可由同一受审机构提供，但须分别验证权限。不得通过普通 HTTP 上传公钥即建立信任。私钥不进入 renderer、请求参数、队列、日志或备份；实际签发端的安装/凭据方式由 Root 选择，当前明确 blocked。
- 登录 challenge 由服务器签发并一次消费。服务器从已验证凭据取得 principal，不能信任 body 中的 principal/role。评审 session 仅 loopback、固定 API owner/runtime、principal 与授权范围，存 token 摘要；使用现有有效期/撤销规则，失效后显式重新认证，不延长原 ParentAuth 的30分钟授权来跑长批次。重新认证可找回自己的草稿，但不继承旧媒体能力。

### 2. 有限 API 与状态所有者（提议）

管理授权与评审认证为两个独立权限；评审端不持 ParentAuth token，也没有家长预测/报告读取能力。以下均本机、严格闭字段、`Cache-Control: no-store`，错误只返回固定码。

| 接口 | 最小输入与服务器责任 |
|---|---|
| `POST /v1/parent/children/{child}/rubric-calibration/review-grants` | ParentAuth；选择已核身份/协议与许可用途，返回范围授权回执，不创建评审身份或评分资格 |
| `POST /v1/rubric-review/auth/challenges`、`POST .../sessions` | 已注册 credential 引用及挑战签名；验证根/当前撤销、一次 challenge 和实际 principal；不接受别名作为证明 |
| `GET /v1/rubric-review/work`、`POST .../work/{opaque}/claim` | reviewer session；列当前授权任务，CAS claim，限制同一任务/主体的有效领取；不按预测、失败或分歧筛选样本 |
| `GET .../claims/{claim}`、`PUT .../claims/{claim}/draft` | 同 principal；`expected_revision` + `client_action_id`，保存现有五维人工观察/分值草稿，回传持久 revision；恢复读取同一草稿，不新造作答或执行模型 |
| `POST .../claims/{claim}/display`、`GET .../displays/{opaque}/audio` | 服务器重核当前来源与许可后创建盲评展示；音频 token 绑定原四ID/版本、principal、claim、display、当前session及用途，只能读该原音，无路径参数 |
| `POST .../claims/{claim}/submit`、`POST .../claims/{claim}/release` | submit 只收当前 revision、原观察/分值、一次 submit nonce 和幂等 key；principal/时间/完整来源 binding 由服务器取得。release 保留该主体已保存草稿，不篡改已提交标签 |
| `POST .../review-grants/{grant}/revoke` | ParentAuth 撤回访问；身份根撤销另由原验证器处理，二者均使 claim/media/待提交失效；不删除审计或改写旧签名 |

新增单一领域 owner 拟为 `rubric_blind_review.py`，提供 `list_work/claim/read/save_draft/prepare_display/read_audio/submit/release/revoke`；身份验证拟为 `rubric_reviewer_identity.py`。它们使用原 Database 连接与事务，不复用 CPU worker `_Claim` 或伪造 execution receipt。具体 action/持久状态布局由 Root 定案；本轮不选新DDL，也不把这些调用假设成现有 audit_event 自动支持。

### 3. 可保存、可恢复且一次提交

任务只绑定现有已预注册 holdout 的真实可解析 case；保原 v2 首个原job/当前来源选择，不跳过 blocked、撤回、失败或难样本来改善统计。状态最小为 `available → claimed → draft → submitting → submitted`，另有 `released/revoked/invalidated`；状态与修订追加，claim 过期不抹去人的草稿。

同一个 `BEGIN IMMEDIATE` 中校验 principal/session/grant、claim revision 与完整 source binding，再保存草稿/领取变更。最终一次 nonce 绑定 principal、claim/display、协议/投影、原 source version、execution/raw摘要与待提交 payload 摘要，不能跨人、跨孩、换源或换分值使用。已接受请求丢响应时，同 key+同摘要只返回原持久回执；新 key 不能第二次消费同 display，旧 key 也不能使撤回标签复活。

签名若须跨进程或外部签发：先持久化不可变 `submitting` intent/精确 payload 摘要，释放数据库事务，再由受信签发端按该 intent 幂等签发；回来后另开原事务重核全部现态，调用现 `authority.import_receipt(c, ...)`，把导入、提交回执与 claim 完成原子提交。未知签发结果保留 submitting 并查询该 intent，不盲重签/重复领取；回滚不得把已签的一次能力当成新 nonce 使用。草稿、签发 intent、旧回执与取消/撤回历史均可在正常重启后查询。恢复备份只保历史事实并 quarantine，旧session/claim媒体能力不自动激活。

身份过期、ParentAuth/评审访问授权撤回、原音删除/损坏、材料/模型/投影/协议变化或 restore quarantine，先使当前播放/提交能力失效；草稿保留为失效工作，不自动改绑新音/新标签。展示过期后须显式新展示/重新确认，不回填历史 `displayed_at`。已经及时提交的历史标签仍沿原协议有效期、来源、撤销与恢复规则复核，不能再用展示 TTL 误杀30天协议。新增持久记录必须补齐原单源/媒体/本孩/全历史导出删除与恢复 hooks，不能仅依赖 JSON 引用自动级联。

### 4. 服务器盲 DTO 与原音边界

DTO 由领域 owner 从原 case 与量表生成：匿名任务号、允许的必要题目/权威原文、现有 rubric/anchors、音频帧率/长度、自己的草稿及不透明 revision/display 引用。**不返回预测分、模型解释、ASR、原始数值特征、另一人的标签、资格结果或无关孩子背景**；renderer 隐藏已有 JSON 不能算盲评。专用 `RubricBlindReviewPanel` 与普通 ParentCenter 预测审核分开，后端权限亦隔离；登记的已有预测暴露/冲突不能靠换页面洗成盲评。

音频复用 `pet_speaking_review.read_speaking_review_audio` 的原 WAV/绑定校验及 `audio_projection_source`/schema92同连接当前 held 核验；在同一 media scope 内检查来源，不返回文件路径/密钥，不复制整段音频到队列或签名元数据，不调用云端。显示/读取/最终提交分别重核；撤回阻止后续读与提交、取消请求并销毁对象URL，不能声称能收回已经合法传出的声音。客户端播放结束只证明技术事件，不能证明人已认真听懂或具有专业身份。

### 5. 与现有 signed authority 兼容

仍产出 `rubric_authority_envelope_v1` 的 `audio_blind_display` 与 `audio_blind_label`，沿现 `FIELDS/BINDING/IDENTITY`、`validate_blind_import`、`audio_label_scores` 和 `import_receipt` 消费，不新增 score 库，不把 ordinary rubric review 追补成 blind。服务器从实际已认证主体与展示/提交记录构造 `identity_receipt_hash/display_receipt_hash/submission_receipt_hash` 及时间；不能让客户端自填。label 的 `review/scores` 保现五维逐帧人工观察与短引用限制，不把完整题面/原音/ASR另写进队列或日志。

两规范 principal 必须不同；同一人换key仍相同。分歧沿原 `LABEL_DISAGREEMENT` 保留，不平均、不选高分、不让家长代第二人签。即使两份标签齐全，仍须原 projection/held execution、全部留出、固定POLICY与evaluate才能改变逐维资格；overall/mastery/考试认证边界完全不动。没有实际身份/签发根时 UI 显示未接通，普通人工复核和未评分形成性学习照旧。

### 6. 最小文件面与验收

- 后端：上述两个领域模块；扩展现 `rubric_calibration_http.py` 的授权/盲评 DTO 路由，复用现 authority/audio validators。签发端端口及持久状态接口由 Root 审定后落地，不改生产 registry 或公式。
- 前端：独立 `RubricBlindReviewPanel.tsx`，接现 API client；ParentCenter 只显示范围授权/待办入口。证据就近、逐维帧段选择、自动保存回执、保存失败重试、断线后同主体继续、明确取消/失效，不展示内部ID或JSON。
- 合成真实HTTP/临时DB验收：缺根默认拒；同人两别名/换key拒第二独立标签，跨主体/孩/协议/media越权拒；一次 nonce 并发和丢响应只一份原回执；保存后App/后台重开与重新认证恢复；签发返回前撤回/换源、事务回滚、restore quarantine不复活；DTO/音频权限不泄露预测/他人标签/原路径。实际签名/两个主体只能来自明确隔离的测试根，不能安装为生产根或证明现实独立性。
- UI 验收先证明保存/恢复再请真人标注；验证原音绑定/帧段、无预测盲DTO、超时/取消和同主体草稿恢复。两名真实独立评审、可信签发凭据、真实材料许可、实际音频质量及资格仍属外部/目标验收，保持 blocked，不用测试自签发PASS。

Root 待决仅三点：采用哪一真实注册/签发信任根及离线凭据验证方式；review grant/session 与独立评审视图的授权边界；上述持久 intent/claim/draft 的具体事务存储布局及隐私/恢复归属。确认后按“身份/范围拒绝 → 单任务保存恢复 → 一次签名提交兼容”三个纵向片段实施。本轮仅补文档，没有 Main、DB/schema、注册表、模型或 Windows 变更。
