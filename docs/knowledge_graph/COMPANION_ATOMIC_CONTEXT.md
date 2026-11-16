# 思维导图原子知识接入陪伴智能体

## 真实闭环

陪伴智能体通过 `GET /v1/children/{child_id}/knowledge-graph/atomic-context` 读取当前本地
原子构建库。接口从应用知识库中的有效孩子档案确定年级，按年级、学科和问题关键词检索，
最多返回 8 个命中节点及每个查询最多 16 个一跳邻域节点，因而不会把 139,115 个节点一次送进模型或浏览器。

返回内容包括定义、能力边界、识别/独立/迁移三层诊断、掌握标准、误区假设、课标对齐、
来源证据、置信度和带方向的关系。构建库始终用 SQLite `mode=ro` 打开。

## 事实边界

- `approved` 节点可以作为已审核讲解上下文和诊断问题依据。
- 未审核、待合并或待纠正节点只能用于导航、追问和薄弱点假设，不能作为确定事实讲解。
- 调用该接口绝不更新孩子掌握度。掌握度仍只接受“人工批准知识节点 + 客观作答结果”。
- 被驳回的节点和关系不会返回。
- 不上传教材、视频或家庭数据；应用库只保存加盐后的孩子/问题指纹、返回节点键和计数，
  不保存孩子标识或原始问题。
- 返回值含本地来源证据，`cloud_transmission_allowed=false`。调用方不得把这段上下文转发给云模型；
  应由本地模型消费，或直接使用服务生成的确定性追问。现有云端陪伴闲聊链路不能接收这段内容。

## 智能体调用约定

陪伴智能体在解释错误或规划追问前，以孩子 id、当前问题和已知学科调用该接口：

```text
GET /v1/children/child-primary-4/knowledge-graph/atomic-context
  ?query=小数的意义和计算&subject=数学&limit=6
```

智能体必须逐项遵守返回的 `usage_policy`。`weakness_hypotheses` 是下一轮问题候选，不能直接
写入 `kg_mastery_state`。只有孩子完成经主程序评分的诊断题后，才走现有
`diagnostic-results` / 主机诊断桥写入掌握证据。

家长/研发者可通过 `GET /v1/parent/knowledge-graph/atomic-context-usage` 查看调用次数、返回节点
数量和最后调用时间；该接口不返回孩子或问题指纹。

## 主运行时接线边界

合入本提交后，现有 `install_knowledge_graph_routes(...)` 会在真实主应用中注册上述接口，
`application.state.knowledge_graph.atomic_companion` 也可供主机直接调用。主机的学习规划/辅导
路径应在收到具体学科问题后调用它，并优先直接采用 `weakness_hypotheses` 生成本地追问。

主运行时已完成以下接线：

- 只有本地意图已明确判定为 `tutor_question` / `correct_previous` 且动作为 `ask_tutor` 时才读取原子上下文。
- 明确学习问题的学科与问题重建由本地策略锁定；通用云规划器不能将物理问题重标为数学或回答评估。
- 原子查询发生在该回合所有云规划之后；返回的节点、关系、来源引文和候选键均不进入任何 `ModelRequest`。
- `agent_turn.action_json.local_learning_context` 只持久化允许列表字段：`audit_event_id`、命中/审核/假设计数、本地追问和两个禁止位。
- 本地确定性生成器只把 `question_required` 假设改写成疑问句；长步骤、分号串和不可说的标题会被拒绝或裁成简短概念，不当作事实讲解。
- 本地追问不作为 `/tutor` 参数；云讲解完成后，渲染器才在本地将它追加到语音和黑板时间线。非学习页阶段也有全局黑板兜底。
- `approved_node_count=0` 时仍只能用于追问假设，`mastery_update_allowed=false`；该路径没有掌握度写入接口。

图谱不可用、无命中或没有可说的假设时，讲题主流程继续，但不伪造图谱追问。

## 本机验证

```bash
PYTHONPATH=services/local_agent/src python services/local_agent/scripts/verify_atomic_companion_context.py
```

脚本对真实四年级和初三档案分别检索数学、物理知识，校验构建库前后 SHA-256 不变，并
重新打开服务确认使用审计记录已经持久化。输出只包含范围、计数、知识标题和审计事件 id。
