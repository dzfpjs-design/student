# 家长成长报告与送达：集成说明

## 工作树隔离记录

本任务早期实现曾误写入 `<本机用户目录>/Documents/学生教育` 主目录；该目录中的已有变更**未被回退或修改**。本说明所列实现已于本次修正迁入当前工作树 `<本机用户目录>/.codex/worktrees/8b62/学生教育`，后续所有开发与验证均只在当前工作树执行。

主任务在集成前应审查主目录中同名文件是否与本工作树版本一致，尤其是：`INTEGRATION_PARENT_REPORTS.md`、`services/local_agent/src/home_study_agent/{app.py,privacy_lifecycle.py,notification_channels.py,parent_reports.py,report_delivery.py}`、`services/local_agent/tests/test_report_delivery.py`、`src/renderer/src/{ParentCenter.tsx,ParentReportDeliveryPanel.tsx,ParentReportDeliveryPanel.test.tsx,api.ts,types.ts,styles.css}`。不要用覆盖或回退方式处理主目录的其他未相关修改。

## 本任务文件清单

| 文件 | 责任 |
| --- | --- |
| `services/local_agent/src/home_study_agent/parent_reports.py` | 基于日报/阶段报告冻结事实、推断、证据版本、审核与家长反馈。 |
| `services/local_agent/src/home_study_agent/report_delivery.py` | 已授权收件人、加密通道凭证、排程、outbox、重试、撤销与保留期。 |
| `services/local_agent/src/home_study_agent/notification_channels.py` | SMTP 及企业微信群消息推送适配器；没有个人微信适配器。 |
| `services/local_agent/src/home_study_agent/repository.py` | 将日报绑定到同一会话内的知识掌握证据和关联错题，而非跨会话猜测。 |
| `services/local_agent/src/home_study_agent/reports.py` | 将计划、知识证据、错题、空白声明和个性化沟通建议写成事实/推断分离的日报。 |
| `services/local_agent/src/home_study_agent/app.py` | 最小服务装配、日报/阶段快照生成恢复和家长端 API。 |
| `services/local_agent/src/home_study_agent/privacy_lifecycle.py` | 删除学习报告时一并删除快照与未保留的送达数据。 |
| `src/renderer/src/ParentReportDeliveryPanel.tsx` | 已挂载的预览、编辑、批准、撤销、定时发送、收件人、通道与送达历史 UI。 |
| `src/renderer/src/ParentCenter.tsx` | 在家长中心挂载送达面板，并呈现周报/月报的逐日信号、趋势门槛、下一步和话术。 |
| `src/renderer/src/{api.ts,types.ts,styles.css}` | 家长报告接口契约和响应式样式的最小改动。 |
| `services/local_agent/tests/test_report_delivery.py` | 快照、会话级证据、真实本地 SMTP、阶段报告渲染、低配并发与重启恢复回归。 |
| `src/renderer/src/{ParentReportDeliveryPanel.test.tsx,ParentCenter.test.tsx}` | 家长界面的证据优先、沟通建议、授权门槛和阶段报告趋势边界回归。 |

## 已交付的纵向切片

孩子完成学习复盘后，现有日报生成器仍负责根据确认的学习证据生成日报；随后 `ParentReportService` 会创建一份独立、不可变的 `parent_report_snapshot` 草稿。草稿保存：

- 输入证据、事实、推断和不确定性的分离副本；
- SHA-256 证据版本、规则/模板版本和来源版本；
- 家长允许添加的发送开场白/备注（事实和推断本身不能改写）；
- 批准、撤销、反馈、独立的“事实待修正”说明和送达审计记录。

日报事实现在汇总同一学习会话中的计划完成、课堂回想、知识掌握证据和关联错题。界面在“查看证据明细”中把独立/有提示的作答、正确率、掌握估计和错题状态放在事实旁；没有可归属证据时明确显示“系统不补造掌握结论”。趋势仍只在阶段报告达到记录天数门槛后说明，不能从单次学习推断稳定能力。

只有家长批准后的快照能进入 `report_delivery_outbox`。每次入队都有唯一幂等键，重复调度不会重复发信；发送前再次检查批准状态、收件人白名单和同意状态。队列支持定时、安静时段延后、取消、凭证/网络失败原因、指数退避重试与后台重启后继续处理。`sent` 的含义是服务商已接受提交，不表示家长已经阅读。

本次以新增 `parent_reports.py`、`report_delivery.py`、`notification_channels.py` 为核心；`app.py` 仅增加服务装配、生命周期处理和 API 路由，`ParentCenter.tsx` 只增加一个面板挂载点，未重写共享主流程或数据库迁移主链。

## 共享入口的适配器边界（供主任务逐点集成）

当前工作树的 `services/` 与 `src/` 基线均为未跟踪内容，不能把整份 `app.py`、`ParentCenter.tsx` 或其相邻的语音/OCR 功能作为本任务提交。集成时只取下列明确的适配点；其余同名或相邻代码保持主任务版本。

| 共享文件 | 仅需接入的内容 | 不属于本任务、不得覆盖 |
| --- | --- | --- |
| `services/local_agent/src/home_study_agent/app.py` | 装配 `ParentReportService`、`ReportDeliveryService`、`enrich_parent_coaching`；在日报成功后创建/恢复快照；下表列出的 `/v1/parent/report-snapshots*` 与 `/v1/parent/report-deliver*` 路由。 | 摄像头、语音、OCR、会话采集、设备绑定和非家长 API 的既有处理器。 |
| `services/local_agent/src/home_study_agent/repository.py` | `save_daily_report(..., source=...)` 与同一 `session_id` 的学习事实收集。 | 其他报告、知识库、OCR 或媒体持久化查询。 |
| `services/local_agent/src/home_study_agent/reports.py` | 日报中传入的会话级事实/不确定性，以及安全的话术、行动和反例替代表达。 | 学习流程的状态迁移和采集逻辑。 |
| `services/local_agent/src/home_study_agent/privacy_lifecycle.py` | 删除报告范围时级联 `parent_report_snapshot_correction`。 | 其他隐私范围与媒体删除策略。 |
| `src/renderer/src/ParentCenter.tsx` | 单一 import 和 `<ParentReportDeliveryPanel childId={selectedChildId} />`，放在阶段报告之后。 | 语音、OCR、设备、模型设置、其他家长中心面板的结构或状态。 |
| `src/renderer/src/api.ts`、`types.ts`、`styles.css` | `reportSnapshots`/`reportDeliveries` 的 API 契约、对应类型、仅 `.parent-report-delivery` 选择器样式。 | 其他接口、共享类型和通用样式重排。 |

后端新增/适配的路由清单（均沿用现有 `X-HSC-Parent` 家长鉴权）：

- 快照审核：`GET /v1/parent/children/{child_id}/report-snapshots`、`GET|PUT /v1/parent/report-snapshots/{snapshot_id}`、`POST …/approve`、`…/revoke`、`…/feedback`、`…/corrections`。
- 送达设置：`GET|PUT /v1/parent/report-delivery/channels`、`GET /v1/parent/children/{child_id}/report-delivery/recipients`、`POST /v1/parent/report-delivery/recipients`、`DELETE /v1/parent/report-delivery/recipients/{recipient_id}`、`PUT /v1/parent/children/{child_id}/report-delivery/preferences`。
- outbox：`GET|POST /v1/parent/report-snapshots/{snapshot_id}/deliveries`、`POST /v1/parent/report-deliveries/{delivery_id}/cancel`、`…/retry`、`POST /v1/parent/report-deliveries/process`。

核心模块只通过 `ParentReportService`、`ReportDeliveryService` 和 `NotificationChannel` 协议交互；没有对 OCR/语音模块的 import、路由前缀、数据库表或组件 props 的隐式依赖。外部合入应先应用独立核心提交，再逐项嫁接上述共享适配点，并跑本文件末尾的回归命令。

## 最低配置与模型边界

目标家庭电脑可以是 **Windows x64、4 核/8 线程 AVX2 CPU、16GB RAM、无独立显卡**。本交付中的快照构建、邮件 HTML/纯文本渲染、SQLite outbox、重试、脱敏、定时扫描和 SMTP/企业微信网络等待均不加载本地大模型、CUDA、ONNX 推理或 GPU 权重。送达工作器把并发固定上限为 2：足以同时处理父母两个收件人，又不会在低配设备上挤占学习流程。

报告生成现在是**云优先、证据受控**：本地先构造并冻结计划、知识证据、错题和状态自评等事实；在家长已开启文本云处理且模型已配置时，云模型只可补充家长沟通建议，不能改写事实、证据版本或不确定性。每次云调用使用最小化脱敏事实摘要、记录模型调用审计、提示词版本和实际提供方/模型；返回 JSON 必须通过字段、时长、禁用表达校验才会采用。当前来源会如实标为 `cloud_parent_coaching_v1`。

`local_rules_v2` 仍是无模型的确定性降级路径：未配置模型、家长关闭云文本、敏感文本、本地/云网络错误、超时或不合规 JSON 都会保留规则建议并标为 `local_rules_v2`，不会假称云模型结果，也不需要 4090。云模型账户、网络可用性、成本和提示词质量仍是生产报告质量的独立验证项。

后台启动会把被中断的 `report_job.processing` 复位为待处理，并以最多两个任务一批恢复待生成日报；outbox 在重启后从持久化 `scheduled` 状态继续。所有这些恢复任务只调用现有报告管线与轻量 I/O，不新增本地模型依赖。

## 集成到家长中心

该面板现已挂载在 `ParentCenter.tsx` 的阶段报告之后。如需在另一个家长入口复用，可导入并渲染：

```tsx
import { ParentReportDeliveryPanel } from './ParentReportDeliveryPanel'

<ParentReportDeliveryPanel childId={selectedChildId} />
```

组件采用“先证据、后结论、再审批和送达”的顺序：家长可看到事实与不确定性、紧邻事实核对本次“今晚做什么/做多久/何时停止”“可直接这样说”及“不要这样说/可替代表达”，仅编辑自己的备注、批准或撤销、管理逐一授权的收件人、即时或定时排队、重试/取消并查看送达历史；还可以评价建议帮助程度和沟通压力，用于后续建议调整。它刻意不在主界面展示 JSON 或内部 ID。

审核预览还提供“查看学习证据明细”：可从同一快照直接下钻到已确认课堂回想、计划完成、独立/有提示的知识作答、关联错题和孩子复盘；阶段报告只展示本期覆盖率与逐日信号。缺少关联证据时会明确提示，不将其他会话的记录或空白补入。

若家长发现事实不准确，可填写修正说明并“标记待修正并停止外发”。这会撤销快照、取消未发送任务并写入独立审计记录；不会悄悄覆盖原有事实或已发送副本。之后仍需在学习记录中核对原始证据，当前模块不把家长说明伪装成孩子学习事实。

同一页面的周报/月报已将每日记录、是否达到趋势判断门槛、下一步所做内容/时长/步骤以及可直接使用的话术放进可展开区。记录天数不足时，界面明确写出“暂不判断趋势”，但仍保留已发生的日期信号与可执行的下一步，避免把单日波动包装成稳定能力结论。

面板已包含桌面与窄屏布局样式，并支持本地加密 SMTP/Webhook 配置、安静时段、保留期及手动检查到期发送。

## 邮件配置和真实验证边界

`email` adapter 是标准 SMTP：HTML + 纯文本 multipart 邮件，并附带同内容的离线 HTML 报告附件（不使用对外收件人无法访问的本地链接）。它支持 SMTP SSL（通常 465）或 STARTTLS（通常 587）、用户名/应用专用密码与发件人地址。凭证只会在 `HSC_DATA_ENCRYPTION_KEY` 为有效 32-byte 密钥时以 AES-GCM 加密写入本地 SQLite；API 从不回传密码。

标准披露副本会发送冻结的确认事实、下一步和时长、可直接使用的话术，以及“不要这样说/可以改说”的对应表达；周报/月报则把已记录天数、会话数、证据数和逐日信号转换为相同的人话事实，而不会误把空的日报事实列表发出去。最小披露副本仍不含这些学习细节。

调用 `PUT /v1/parent/report-delivery/channels/email` 时需保存例如：

```json
{
  "config": {
    "host": "smtp.example.net",
    "port": 587,
    "username": "report@example.net",
    "password": "synthetic-fb6544585cd07b24cf2ea3ad",
    "from_address": "report@example.net",
    "starttls": true,
    "allow_external_delivery": true
  }
}
```

`allow_external_delivery` 默认关闭；没有它，任务会明确失败为 `EXTERNAL_DELIVERY_NOT_AUTHORIZED`，不会发送或假装发送。当前已通过本地 SMTP mail sink 验证实际 SMTP 协议提交、多段 HTML/纯文本内容和本地回收；没有向外网邮箱发送任何孩子数据，也尚未验证具体云邮箱提供商、域名 SPF/DKIM/DMARC、或真实收件箱呈现。这些是部署前的凭证/基础设施验证项。

## 微信路径：实现与阻塞的如实说明

目前实现的微信适配器是 `wechat_work_bot`（企业微信“消息推送/原群机器人”）：保存 HTTPS webhook（同样 AES-GCM 加密）后，发送标准文本 POST。它只在“该家庭明确允许外发 + 群内所有可见成员均是获得同意的成年人”时可使用；收件人字段只作为本地同意与审计标签，不能把群机器人误当成对某位家长的私信。

可行路径的比较：

| 路径 | 适合程度 | 已实现 | 账号/认证/模板限制 |
| --- | --- | --- | --- |
| 企业微信群消息推送（Webhook） | 小范围、已有家庭/学校合规群 | 是 | 群创建者生成保密 HTTPS webhook；没有成员级送达/阅读回执，群成员本身是隐私边界。官方说明支持 text 等消息，text 上限 2048 UTF-8 bytes。[官方文档](https://developer.work.weixin.qq.com/document/path/91770) |
| 企业微信自建应用消息 | 需要按家长成员精确发送、需要撤回消息 | 未实现，适配器预留 | 需要企业/组织、应用 `agentid`、访问令牌、家长的企业成员 `userid` 与应用可见范围；可获得 `msgid` 并受频率/可见范围约束。[官方文档](https://developer.work.weixin.qq.com/document/path/90236) |
| 服务号/公众号模板消息 | 家长已关注且产品满足模板场景 | 未实现 | 需要服务号能力、服务端 access token、家长 OpenID、审核/可用模板及字段长度约束；模板消息接口支持 `client_msg_id` 防重。[官方文档](https://developers.weixin.qq.com/doc/service/api/notify/template/api_sendtemplatemessage.html) |
| 个人微信 | 不作为产品通道 | 明确不实现 | 没有稳定公开的个人号消息 API；不使用 UI 自动化、机器人扫码、逆向协议或伪接口。替代方式是邮件、企业微信合规群，或家长在应用内查看报告。 |

企业微信 webhook 是真实的可发送路径，但仍需产品运营者提供组织账号与明确的家庭同意流程；企业应用和公众号还被账号、认证、模板审核与家长身份映射阻塞，不能声称已开通。

适配器会在网络请求前检查企业微信机器人文本的 2048 UTF-8 bytes 上限，超限任务显示 `WECOM_MESSAGE_TOO_LARGE`，提示改用最小披露、邮件或本机查看；不会静默截断学习事实。

## 运维、隐私与验收

- 收件人必须在界面勾选“已同意接收”后才能加入白名单；企业微信群还要求操作者确认群内所有可见成员均是已同意的成年人。收件人可撤销：所有已排队任务会立即取消，工作器在真正调用通道前还会再核验授权；已被外部通道接受的消息无法在此应用中撤回。
- 安静时段、保留天数和送达内容级别按孩子保存。`standard` 包含确认的事实、行动和沟通建议；`minimal` 只发送标题、不确定性及“在本机家长中心查看完整报告”的提示，不包含学习细节、孩子原话或沟通脚本。定期维护会清理超出保留期的已发送/失败/取消 outbox 记录，保留独立审计事件；不会删除仍排队的任务。报告范围删除会级联删除快照、更正说明和送达数据，且在删除结果中计数更正说明。
- 当上游报告构建/模型步骤超时或日报为空时，原有 `report_job` 留在待重试状态，快照和发送任务不会凭空生成；恢复后只从已经写入的真实学习证据创建日报及对应阶段草稿。
- 后端启动时会补建历史日报/阶段报告的草稿快照，重启后会继续处理 outbox；但 SMTP/微信凭证失效只能如实显示失败码并等待家长修复后重试。

已覆盖：快照不可变与跨源表修改、批准门槛、重复入队幂等、家长标记待修正、收件人撤销、临时 SMTP 网络中断的持久化退避/恢复、失败凭证的人工重试、实际本地 SMTP mail sink、HTML/纯文本封装、企业微信长度拒绝、低配工作器的双收件人并发上限、定时 outbox 重启恢复、以及待生成日报的启动恢复（不加载本地模型），并通过 TypeScript 检查。待目标环境验收：Windows x64 最低配置上的长期运行、云模型账户/网络/成本/超时表现、邮件服务商账号、企业微信组织/群成员同意、真实网络中断恢复、定时进程在打包桌面端的长期运行，以及家长中心嵌入后的浏览器人工可用性检查。

“空报告”并非伪造结果：没有校内作业或孩子跳过复盘时，现有报告生成器保留明确声明和不确定性，提示家长不追加练习、不追问、不做态度/能力推断；该路径由既有日报回归覆盖。送达面板只冻结已生成报告，绝不从空白补造学习事实。

## 验收证据矩阵（截至当前工作树）

| 目标 | 当前证据 | 结论 |
| --- | --- | --- |
| 学习证据到冻结快照 | 会话级计划、回想、知识作答、错题和状态自评写入日报；快照保存 SHA-256 版本、事实/推断/不确定性，并有跨源修改不回写的回归。 | 自动化已验证。 |
| 云优先沟通建议 | 家长已授权且模型已配置时，脱敏事实摘要进入现有 `ModelGateway`；云只可更丰富沟通建议，严格 JSON/安全校验后采用，模型调用可审计。超时/敏感/无配置/不合规则显式 `local_rules_v2` 降级。 | Mock 网关纵向成功与超时降级已验证；真实云账号、延迟和成本待验收。 |
| 家长审核、下钻和修正 | 家长中心已挂载审核面板；事实先于解读；可下钻证据；待修正操作在一个本地事务中撤销、取消待发、保存说明和审计。 | 前端/API/SQLite 回归已验证。 |
| 定时、重启和多人送达 | SQLite outbox 有幂等键、安静时段、双工作线程上限、重试、取消与重启恢复；收件人撤销在提交前再核验。 | 自动化已验证；实际 Windows 长期运行未验证。 |
| 邮件纵向切片 | 本地 SMTP sink 已收到 multipart 纯文本/HTML 和离线 HTML 附件；密码加密落库且不从状态 API 返回。 | **仅本地 sink 已验证**；真实外网 SMTP/收件箱、SPF/DKIM/DMARC 待家长提供测试账号/收件人。 |
| 企业微信 | 官方企业微信群机器人 Webhook 适配器、HTTPS/授权检查和长度限制已实现。 | 无组织账号、Webhook 和成人群同意，未做真实发送；企业应用/公众号仍是账号和模板能力阻塞。 |
| 最低配置 | 报告快照、渲染、outbox 和最多 2 个发送工作线程不加载本地大模型或 GPU；启动恢复回归覆盖。 | 开发环境代码路径已验证；Windows x64 4 核/8 线程、16GB、无独显实机仍待验收。 |

## 本机真实证据的仅本地预览复核

2026-08-27 在本机家庭数据文件的 SQLite 一致性**副本**上完成一次端到端复核，原始生产数据库未写入、未发送任何网络请求、也未输出孩子的文本或身份数据。源库当时有 12 个已完成会话/日报、4 条知识证据和 2 条课堂回想；从已有日报产生了 5 份日度快照，并选择证据数最高的一份预览。

- 快照中有 7 条冻结事实、5 条已确认学习证据，`contains_raw_audio_or_video=false`，序列化快照未包含媒体本地路径；事实、置信度/不确定性、行动（10 分钟）、4 句可直接照说的话术和 4 条“不要这样说/可以改说”均存在。
- 在副本上以家长 API 保存“仅本地预览”备注、批准报告、建立 `local-preview@invalid` 的授权测试收件人并排队；没有保存通道凭证或调用 SMTP/企业微信。随后提交事实修正，状态变为 `revoked`，待发 outbox 变为 `cancelled`，故不会发生外送。
- 重启本地服务后，冻结的家长备注、批准/更正审计和已取消 outbox 均仍可读取。该验证证明的是数据副本上的本地预览/审核/恢复链路；不是外网邮箱或企业微信送达验证。
- 当快照证据计数为 `limited` 或没有可呈现事实时，面板在事实区明确显示“**证据不足**”，并说明不会据此推断掌握程度、能力、态度或趋势；没有让云模型补造空白事实。
