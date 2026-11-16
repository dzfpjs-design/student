# C03：教材补题到原任务恢复交接

## 当前实施续记（2026-09-08，覆盖下文只读历史）

- V7已经结束。严格PET写作/听力/客观原题导入已修复，完整JSON与合法NDJSON不再按普通索引截断，非法批次整体拒绝，真实导入至发布/重启/撤回已验证。新5项HTTP/事务增量与既有85项受影响组分别通过，原失败日志保留；不表示内容覆盖完整。
- roadmap库存、原路线执行与初三复测供应改用只读题目资格，不虚拟答对来查库存；稳定相关135项通过。证据见`../evidence/2026-09-09-practice-inventory/READOUT.md`。
- 一般PET日计划启动原先只有计时、没有自动原题待答绑定，正在修复。后端冻结原计划题目版本并在prepare/计时/读回答各处重验；前端用返回原prompt走既有听力播放链，普通TTS不朗读听力原文。词卡/写作stage保持原路径，尚未标完成。
- 旧截断资料尚不能安全原地修复：知识源有`UNIQUE(child_id,content_hash)`且同hash直接返回旧源；不能改空格、伪hash或修改旧excerpt来绕过。需显式prepare/replace合同、已知旧格式的完整hash及存储指纹匹配、新源新excerpt和追加lineage、旧资格退役但原文不变、新源重审且不复制权限或掌握证据。中央迁移尚未实现。
- 替代源不能靠同alias“取最新”生效。teaching_context及goal_roadmap对同alias多个适用源当前保守聚合资格，显式修订还需精确revision消费者；现有candidate supersedes只替题，不替知识源。
- 首题缺seed、家长友好的原题准备、明确缺货绑定与补题后原任务恢复仍为未完成范围；以下条目保留为根因和后续验收条件，不删除范围。

日期：2026-09-08。V7 运行期间限范围只读审计；唯一写入本文件。
未运行测试、访问正式家庭库/设备、下载材料、调用模型或恢复批任务。以下是当前源码与测试定义的证据，不是本轮运行通过记录。完整产品仍 partial。

## 1. 提示不等于已定位真实缺货

- [GoalRoadmapPanel.tsx:109](../../src/renderer/src/GoalRoadmapPanel.tsx#L109) 将所有非 `dispatchable && status=suggested` 动作显示为“缺合格题源，暂不安排诊断”；它没有提供从该动作直达对应教材/节点缺口的入口。
- [goal_roadmap.py:213](../../services/local_agent/src/home_study_agent/goal_roadmap.py#L213) `_has_qualified_question` 要求 `exact_answers`、用其中一个答案调用 `qualified_exact_score` 并排除近期曝光；[同文件:337](../../services/local_agent/src/home_study_agent/goal_roadmap.py#L337) 用该结果形成 `qualified_source_needed`。它把“存在可执行题”与“当前能得到 exact 资格”混用：没有 exact 答案的真实写作题不会让该布尔值变真。听力评分还要求原次媒体资格，不能以假播放使这次库存查询通过。
- 实际 PET 写作排程已有不同消费者：[pet_training_plan.py:30](../../services/local_agent/src/home_study_agent/pet_training_plan.py#L30) 调 [pet_writing_stage_plan.py:11](../../services/local_agent/src/home_study_agent/pet_writing_stage_plan.py#L11)，读取当前 stage 的 exact item/source/review/date，ready 才排整项12分钟，waiting_due 与 source_unavailable 分开；[repository.py:2158](../../services/local_agent/src/home_study_agent/repository.py#L2158) 实际消费并在写事务重验。
- 因而当前画面不能独自证明“家庭没有可用 PET 题”。下一次宿主复现需同时记录本孩只读 roadmap.next_daily_action、stage next_action、allocator unplaced 的原因。不能只改面板颜色，也不能调用评分函数虚构作答资格来修库存。

## 2. 已有教材入口与具体缺口

### 已有能力，不重复造库

[ParentCenter.tsx:1676](../../src/renderer/src/ParentCenter.tsx#L1676) 可导入本孩材料；[CurriculumSupplyReviewPanel.tsx:291](../../src/renderer/src/CurriculumSupplyReviewPanel.tsx#L291) 已有 `selection:<child_id>` 精确教材区域、教材/学期/课标选择、草案及审核发布。不是“没有审核入口”。

[curriculum_supply.py:94](../../services/local_agent/src/home_study_agent/curriculum_supply.py#L94) 要求当前同孩 seed、准确教材 selection、approved 且许可为 public/family_owned_index 的本地来源、有效日期、原 excerpt、已核课标页及原文引用。[同文件:323](../../services/local_agent/src/home_study_agent/curriculum_supply.py#L323) `generate_draft` 只是从原 excerpt 解析现成题，不生成缺失答案/材料。

[curriculum_supply.py:531](../../services/local_agent/src/home_study_agent/curriculum_supply.py#L531) 在同事务验证并调用 `specialized_supply.promote_candidate`，写入既有 diagnostic_item，保留 package/candidate 来源；[同文件:543](../../services/local_agent/src/home_study_agent/curriculum_supply.py#L543) 撤回退役题目。来源发布不授云许可、不产生孩子掌握证据。

### P1-A：原始教材到合法结构化题的准备路径仍有断点

- 材料管理正文入口 [ParentCenter.tsx:1702](../../src/renderer/src/ParentCenter.tsx#L1702) 提示“每行一个章节、知识点或页码索引”，未提供本范围内的 PET 原题结构化录入/从原页确认题干、题型、原答案、听音要求的完整作者工作流。家长只有目录或普通课文时，供给服务不会自动得到合格题，这是未接原题准备能力，不是孩子验证问题。
- [knowledge_base.py:19](../../services/local_agent/src/home_study_agent/knowledge_base.py#L19) `_typed_document` 只识别 `reviewed_open_response_source_v1/v2`；[同文件:69](../../services/local_agent/src/home_study_agent/knowledge_base.py#L69) 其余按行拆分；[同文件:115](../../services/local_agent/src/home_study_agent/knowledge_base.py#L115) 非识别类型每行只存1000字符，同时 hash 仍取完整行。
- PET 原题却要求完整 JSON：`pet_listening_source_v1` 由 [pet_reviewed_source.py:40](../../services/local_agent/src/home_study_agent/pet_reviewed_source.py#L40) 精确解析（含 instruction/required_play_count/exact 原答案）；`pet_writing_source_v1` 由 [pet_writing_source.py:26](../../services/local_agent/src/home_study_agent/pet_writing_source.py#L26) 解析（原 task_points/字数范围，无 exact）；`curriculum_objective_source_v1` 由 [curriculum_objective_source.py:36](../../services/local_agent/src/home_study_agent/curriculum_objective_source.py#L36) 解析。
- 可复现输入：合法 PET 题 pretty JSON，或压成单行但超过1000字符、仍小于解析器16k限制；真实 import 后再走 source-options/generate，可能已无完整可解析 excerpt。此处源码路径确定，本轮尚未实际 HTTP 复现，不声称已证实运行失败。短单行 JSON 可避开，不代表日常教材入口完整。
- 下一修改最窄落点：上述严格 parser 的完整保存识别及原题作者入口。仍需每条精确 schema/重复键/字段/长度检查、原文 hash、同孩课标/来源审核；不得 SQL 补正文、替换 source_ref、把 private 改 public 或为兼容关掉来源校验。

### P1-B：缺首题不能由“补缺口”自行解决

[curriculum_supply_http.py:528](../../services/local_agent/src/home_study_agent/curriculum_supply_http.py#L528) source-options 从已有 active diagnostic_item 起步，逐题 validate，再按节点/answer_type/typed schema 筛 seed；[curriculum_supply.py:95](../../services/local_agent/src/home_study_agent/curriculum_supply.py#L95) 起草也必须先 validate seed。因此未有合格 seed 的节点，即使有已审核教材，当前入口不能直接建立首个可信原题。

[specialized_supply.py:310](../../services/local_agent/src/home_study_agent/specialized_supply.py#L310) `enqueue_gaps` 同样先找已有 qualified seed，没有则 `skipped.reason=seed_missing`。 [ParentCenter.tsx:480](../../src/renderer/src/ParentCenter.tsx#L480) 的准备按钮对零新job显示“可能已经达标或正在生成”，虽[同文件:539](../../src/renderer/src/ParentCenter.tsx#L539) 另有缺 seed 提示，但没有从该 domain/阶段绑定去准备首题的操作合同。

下一合同应是“已审核现成题建立首题”与“从现有题生成变式”分开；前者需可信当前 node/课标/原答案与模态约束，不以错误 seed 或任意 family label 绕过。不能删除 `seed_missing` 让无根题入库。

## 3. 模板、缺口与阶段不是同一个计数

- [local_supply_templates.py:7](../../services/local_agent/src/home_study_agent/local_supply_templates.py#L7) 列五个闭合模板族；PET 仅窄词义和一般现在时，其中后者不自动资格化。[local_supply.py:220](../../services/local_agent/src/home_study_agent/local_supply.py#L220) 还有少数竞赛/初三数学模板，不覆盖全 PET 听说读写；不同模板或有审核 schema 不等于所有题型已供给。
- [curriculum_objective_source.py:74](../../services/local_agent/src/home_study_agent/curriculum_objective_source.py#L74) 客观原题 validator 固定 parallel，不接受长材料/stimulus 或把开放过程塞进 exact。PET speaking 没有这里独立的 typed 原题供给分支：[curriculum_supply_http.py:536](../../services/local_agent/src/home_study_agent/curriculum_supply_http.py#L536) 可选 validator 分支不含 speaking；objective 明确排除 speaking/listening/writing 节点。已有原音审核不补足该来源入口。
- [specialized_supply.py:271](../../services/local_agent/src/home_study_agent/specialized_supply.py#L271) `gaps` 按 candidate 原 `stage` 汇总四类 supply stage；[specialized_stages.py:17](../../services/local_agent/src/home_study_agent/specialized_stages.py#L17) 是 baseline/near/far/delayed 的孩子证据协议，且[同文件:73](../../services/local_agent/src/home_study_agent/specialized_stages.py#L73) 另读审核 stage。两者不能互换，“还有缺口”未必是 ready 原题不存在。
- 不误报 near/far 审核未实现：[CurriculumSupplyReviewPanel.tsx:236](../../src/renderer/src/CurriculumSupplyReviewPanel.tsx#L236) 已能选审核阶段；[stage_source_review.py:43](../../services/local_agent/src/home_study_agent/stage_source_review.py#L43) 校验 source/candidate 原引用、binding 和改变情境的依据。generate 默认 `seed.stages[0]` 并不证明只能 parallel，因为已发布消费者还读该独立审核回执。阶段审核仅证明题目设计依据，不证明孩子迁移。

## 4. 发布后原任务恢复仍缺确切消费者

- [CurriculumSupplyReviewPanel.tsx:250](../../src/renderer/src/CurriculumSupplyReviewPanel.tsx#L250) 发布提交的是 child/candidate/version/action；[curriculum_supply.py:531](../../services/local_agent/src/home_study_agent/curriculum_supply.py#L531) 发布事务没有原 blocked plan_item/step/缺货义务绑定或恢复回执；返回的 next_action 是 `plan_selection`（同文件:637）。
- 当前补充会进入下一次有效题库查询/新计划；但“原来阻塞的是哪项、发布哪题满足哪种缺口、孩子说继续如何回原义务”没有在这条发布合同中串起来。[repository.py:1785](../../services/local_agent/src/home_study_agent/repository.py#L1785) 已有 dailyplan 原 key 返回既有 workspace，不据新发布重建原计划。
- 对已经执行过的 task.start，[agent_capability_executor.py:53](../../services/local_agent/src/home_study_agent/agent_capability_executor.py#L53) completed step 直接返回原 result；缺题返回 blocked/no evidence（同文件:827），之后仍写完成的执行回执（同文件:128）。重复旧请求不是补货恢复 API，不能改写旧结果去偷换原题。
- [local_supply.py:438](../../services/local_agent/src/home_study_agent/local_supply.py#L438) job retry 只是把原 local blocked/failed job 再 pending；curriculum_review 明确拒绝这种重试。它不是恢复孩子原学习任务，模板不支持也不会因反复 retry 自动变支持。
- 最小待接合同：原缺货义务持久引用 child/session/goal 或 plan_item、所需 node/family/stage、原失败 reason/version；审核发布后重新解析原义务并冻结新 exact item/source/stage 绑定，生成有 lineage 的新执行尝试，保留旧 step/job 历史。不让家长/孩子输入 ID/new key；重启与“继续”从原对象读回。原题已经绑定或已有作答则不得自动换题；原题撤回应明确重新选择，不自动复活。此合同尚未实现，不在冻结期新增表或 Runtime 接点。

## 5. 已有测试边界与唯一下一纵向验收

本轮只读到的现有测试，不重复执行或援引为现版绿：

- [test_curriculum_supply_options_http.py:6](../../services/local_agent/tests/test_curriculum_supply_options_http.py#L6)：已有算术材料/seed 的 options→generate→review→publish→withdraw，不能证明普通 PET 教材转换。
- [test_curriculum_stage_review_http.py:26](../../services/local_agent/tests/test_curriculum_stage_review_http.py#L26)：精确阶段审核、重启/撤回；不等于孩子原阻塞任务恢复。
- [test_curriculum_supply.py:328](../../services/local_agent/tests/test_curriculum_supply.py#L328)：发布一题不清除无关 template/stage blocked job，原反例必须保留。
- [test_pet_writing_stage_execution.py:14](../../services/local_agent/tests/test_pet_writing_stage_execution.py#L14)：先 prepared/publish，再造当天计划、实际 executor 精确 prompt、重启与撤源；未覆盖“先缺货任务，再家长补材料”顺序。

**下一条纵向验收：真实 ParentCenter PET 写作缺货→原材料补题→审核→原义务继续。**

1. 独立临时家庭，保留真实 seed/课标守卫，先形成一个缺 distinct writing stage 题的原义务，保存原 goal/plan/step 与 blocker；不是发布后才生成测试起点。
2. 家长在主壳从该缺口进入准确教材；用本地合成、明确非官方的合法现成原题（pretty JSON 或 >1000字符的合法题源），经真实 import/readback 确认原文、hash、node/版本不被拆断，再走 selection/options/generate/validate/review/publish。无云请求；家长不需输入内部ID。
3. 原动作恢复读取最新供给但保留原义务：不足预算仍 unplaced；有合法题时产生新 exact stage binding。孩子自然“继续”经真实 app/orchestrator/executor 只开启该题；未保存真实原稿/评价前不能完成学习任务或称掌握。
4. 中途关闭/重启、发布丢回执原 key 重试，验证只一题、一新执行尝试；另一孩题不补本孩缺口。仅撤材料源后旧授权/题/晚回执失效，原孩子作答归档保留；不以删全库替代撤源。
5. 同屏核对 roadmap 的可执行库存语义、stage/allocator、实际 prompt 一致；准备未就绪必须指出具体来源/首题/阶段/原音/预算原因，不再全部归为泛化“缺题”。

建议下一阶段所有权按主线程分配：严格 typed import+作者录入；缺口对象/发布后重解析的服务与 repository/Runtime 消费；路线可执行库存投影/UI。此文仅交接，不解冻这些文件，不继续校准理论。
