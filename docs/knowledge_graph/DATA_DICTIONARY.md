# 本地知识图谱数据字典

所有 `kg_*` 表位于现有应用 SQLite 中，由 `KnowledgeGraphService.ensure_ready()` 建立；它们不替换原有 `knowledge_node`、`mastery_state`，因此可以独立接入和回滚。

| 表/实体 | 作用 | 关键可追溯字段 |
|---|---|---|
| `kg_corpus` | 本地课程语料库登记 | 本地根目录、来源说明、权利边界、版本、审核状态 |
| `kg_resource` | 一个视频/转录资源 | 原始 ID、标题、学段范围、转录 SHA-256、视频 SHA-256 或文件指纹、来源 |
| `kg_document_page` | 高拍仪/本地 OCR 文档的一页，与同页 OCR 资源一对一关联 | 文档类型、页码、相对图片定位、可选图片 SHA-256 或指纹、OCR 引擎/置信度、来源、版本与本地使用边界；绝不存绝对路径或复制原图 |
| `kg_segment` | 约 900 字符的可检索转录片段 | 字符范围、文本 SHA-256、置信度、审核状态 |
| `kg_segment_occurrence` | 相同片段在不同资源中的出现 | 资源、片段、序号、字符范围；用于跨库去重而不丢来源 |
| `kg_segment_fts` / `kg_segment_embedding` | SQLite FTS5 和本地 n-gram 向量投影 | 无云模型；向量算法和维度版本化 |
| `kg_node` | 课标能力、学段/年级/学期/学科/单元、知识点、易错概念、讲解、例题、错题、证据、掌握状态 | `scope_json`、来源/时间/版本、权利边界、置信度、审核状态 |
| `kg_edge` | `contains`、`prerequisite_of`、`related_to`、`misconception_of` 等关系 | 关系理由、来源、版本、置信度、审核状态 |
| `kg_resource_node` | 资源或片段与知识点的覆盖/讲解/例题/测评映射 | 映射来源 `source_ref`、置信度与审核状态；学习路径同时返回资源来源和此映射来源 |
| `kg_ingest_job` / `kg_ingest_failure` | 可恢复导入、检查点和失败队列 | 幂等键、游标、处理统计、错误、重试次数 |
| `kg_mastery_evidence` / `kg_mastery_state` | 学生诊断/错题/复习证据与掌握度的耐久状态 | 来源类型、答对率、独立性、算法版本、时间 |
| `kg_review_plan` | 本机生成并可重启恢复的复习计划 | 计划算法、计划项、证据数量、幂等 action、状态与生成时间 |
| `kg_mapping_review` / `kg_mapping_review_event` | 父母/教师审核映射的当前决定与不可变历史 | 审核人、接受/拒绝/需拆分、撤销、理由、客户端幂等 action、时间；`rejected` 绝不进入学生召回 |

每一条新写入的 `kg_mastery_evidence` 还会同步生成一个 `kg_node`：错题使用 `mistake`，其余诊断/复习证据使用 `diagnostic_evidence`；每个受影响知识点的派生状态使用 `mastery_state`。节点只保存 SHA-256 截断得到的 `child_fingerprint`，不把孩子 ID、题图或视频路径放入图节点。`evidence evidenced_by knowledge_point` 表示该本地证据指向知识点，`mastery_state generated_from evidence` 表示算法状态由证据更新而来。

边的方向：`A prerequisite_of B` 表示 A 是 B 的前置知识；一次 B 的错误会以折减权重回传为 A 的薄弱证据，绝不自动把候选映射或转录中的模型/ASR内容当作已审核事实。

相似题检索只沿 `assessed_by` 和一跳 `related_to` 关系：结果中的 `basis` 为 `same_knowledge_point` 或 `explicit_related_knowledge_point`，因此能解释“为什么推荐这题”，且不会把 LLM 生成文本冒充题库题目。

## 权利与审核约定

`license_boundary` 必填。推荐值包括 `public_standard`、`user_owned`、`teacher_authored`、`school_official_local`、`purchased_local`、`platform_content_local_index_only`、`unknown`。Bilibili/公开课素材以 `restricted` 或 `unreviewed` 起步：只在设备上定位和展示必要摘录，不宣称教材或题库授权，也不将其改写为可再分发题库。
