"""Evidence-bound model pipeline for atomic curriculum knowledge.

The transcript is evidence, not the ontology.  This module builds small,
replayable evidence packets and accepts only model drafts that can be checked
against exact local transcript and official-standard excerpts.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from typing import Any, Iterable
from uuid import uuid4

from ..database import Database
from ..model_gateway import ModelGateway, ModelRequest, ModelResponseError, ModelResult
from ..repository import utc_now
from .schema import ensure_schema


PROMPT_VERSION = "atomic_knowledge_v4"
MAX_RESOURCES_PER_BATCH = 2
ATOM_KINDS = {
    "fact_recall", "concept_identification", "concept_discrimination", "procedure_execution",
    "representation_translation", "evidence_reasoning", "strategy_selection", "language_form",
    "language_meaning", "language_use", "experiment_skill", "motor_skill",
}
OBSERVABLE_ACTIONS = {
    "识别", "说出", "判断", "区分", "解释", "概括", "推断", "论证", "评价", "确定",
    "计算", "测量", "操作", "绘制", "表示", "转换", "选择", "运用", "朗读", "复述",
    "翻译", "写出", "写作", "实验", "执行",
}
RELATION_TYPES = {
    "prerequisite_of", "enables", "representation_of", "contrasts_with", "transfers_to", "co_taught_with",
}
EVIDENCE_ROLES = {"objective", "explanation", "worked_example", "assessment", "summary"}
ATOM_KIND_ALIASES = {"reasoning": "evidence_reasoning"}
ACTION_ALIASES = {"解读": "解释", "归纳": "概括", "比较": "区分"}
UMBRELLA_COMPONENTS = {
    "数学", "语文", "英语", "物理", "化学", "阅读理解", "写作", "一次函数", "二次函数",
    "圆", "统计", "概率", "三角形", "四边形", "分式", "有理数", "实数",
}
GENERIC_COMPONENT = re.compile(r"^(?:知识|内容|问题|任务|练习|方法|概念|课文|材料|这个|这些|下列|以下|本节课)$")
DISCOURSE_NOISE = re.compile(r"同学们|老师|大家|让我们|接下来|按下暂停|做得很好|今天我们|本节课我们")
QUOTE_IGNORABLE = re.compile(r"[\s，。；：！？、,.!?;:\"“”'‘’（）()【】\[\]·—-]")
OBJECTIVE_MARKERS = ("学习目标", "教学目标", "目标要求", "教学重点", "学习重点")
ASSESSMENT_MARKERS = ("请同学", "请你", "判断", "说明理由", "完成", "练习", "思考")
SUMMARY_MARKERS = ("本节课", "这节课", "课堂小结", "通过学习", "我们学会")
CONTENT_DETECTOR_VERSION = "local-content-v1"
ACTION_NOMINAL_SUFFIXES = (
    "系统", "流程", "步骤", "方法", "策略", "规则", "公式", "结果", "工具", "单位",
    "对象", "技巧", "能力", "要求", "规范", "界面", "软件", "平台", "活动", "任务", "题型", "材料", "内容",
)


class InvalidAtomicModelOutput(ValueError):
    pass


@dataclass(frozen=True)
class ValidatedAtomicBatch:
    definitions: list[dict]
    relations: list[dict]
    dispositions: tuple[dict, ...] = ()


class AtomicEvidencePacketBuilder:
    """Build bounded packets whose evidence can be verified locally."""

    def __init__(self, database: Database):
        self.database = database

    def build(self, resource_ids: Iterable[str]) -> dict:
        resources = []
        with self.database.connect() as connection:
            for resource_id in resource_ids:
                resource = connection.execute(
                    "SELECT * FROM kg_resource WHERE resource_id = ?", (resource_id,),
                ).fetchone()
                if not resource:
                    raise ValueError(f"unknown resource: {resource_id}")
                metadata = json.loads(resource["metadata_json"] or "{}")
                rows = connection.execute(
                    """SELECT s.segment_id,s.text,o.ordinal FROM kg_segment_occurrence o
                       JOIN kg_segment s ON s.segment_id=o.segment_id
                       WHERE o.resource_id=? ORDER BY o.ordinal""", (resource_id,),
                ).fetchall()
                selected = self._select_segments(rows, resource["title"], metadata)
                resources.append({
                    "resource_id": resource_id,
                    "title": resource["title"],
                    "scope": {
                        "grade": resource["grade_scope"] or "",
                        "subject": resource["subject_scope"] or "",
                        "semester": resource["semester_scope"] or "",
                        "edition": metadata.get("edition") or "",
                    },
                    "unit_path": metadata.get("unit_path") or metadata.get("chapter_title") or "",
                    "segments": selected,
                })
            standards = self._standard_evidence(connection, resources)
        return {"resources": resources, "official_standard_evidence": standards}

    @staticmethod
    def _select_segments(rows: Iterable[Any], title: str, metadata: dict) -> list[dict]:
        rows = list(rows)
        if not rows:
            return []
        terms = [term for term in re.split(r"[/：:、，,（）()\s]+", f"{title} {metadata.get('chapter_title','')}") if len(term) >= 2]

        def score(row: Any) -> tuple[int, int]:
            text = row["text"]
            value = 0
            value += 12 * sum(marker in text for marker in OBJECTIVE_MARKERS)
            value += 7 * sum(marker in text for marker in ASSESSMENT_MARKERS)
            value += 6 * sum(marker in text for marker in SUMMARY_MARKERS)
            value += min(6, sum(term in text for term in terms))
            if row["ordinal"] == 0:
                value += 3
            return value, -row["ordinal"]

        selected = sorted(rows, key=score, reverse=True)[:5]
        result = []
        markers = OBJECTIVE_MARKERS + ASSESSMENT_MARKERS + SUMMARY_MARKERS
        for row in sorted(selected, key=lambda value: value["ordinal"]):
            text = row["text"]
            positions = [text.find(marker) for marker in markers if marker in text]
            center = min(positions) if positions else 0
            start = max(0, center - 180)
            excerpt = text[start:start + 900]
            result.append({"segment_id": row["segment_id"], "ordinal": row["ordinal"], "excerpt_start": start, "text": excerpt})
        return result

    @staticmethod
    def _standard_evidence(connection: Any, resources: list[dict]) -> list[dict]:
        if not resources:
            return []
        subject = resources[0]["scope"]["subject"].replace("小学", "").replace("初中", "")
        if subject == "生物":
            subject = "生物学"
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='curriculum_standard_page'"
        ).fetchone()
        if not table:
            return []
        terms: list[str] = []
        for resource in resources:
            for value in (resource["title"], resource["unit_path"].split("/")[-1]):
                compact = re.sub(r"第[一二三四五六七八九十\d]+课时|\d+(?:\.\d+)?", "", value)
                for term in re.split(r"[/：:、，,（）()\s——]+", compact):
                    if 2 <= len(term) <= 16 and term not in terms:
                        terms.append(term)
        joined = " ".join(f"{resource['title']} {resource['unit_path']}" for resource in resources)
        anchors = AtomicEvidencePacketBuilder._subject_anchors(subject, joined)
        grade = resources[0]["scope"]["grade"]
        stage_terms = {
            "四年级": ("第二学段", "3~4年级", "三至四年级"),
            "五年级": ("第三学段", "5~6年级", "五至六年级"),
            "六年级": ("第三学段", "5~6年级", "五至六年级"),
            "初一": ("第四学段", "7~9年级", "七至九年级"),
            "初二": ("第四学段", "7~9年级", "七至九年级"),
            "初三": ("第四学段", "7~9年级", "七至九年级"),
        }.get(grade, ())
        rows = connection.execute(
            """SELECT s.source_id,s.title,p.page_number,p.content_text
               FROM curriculum_standard_source s JOIN curriculum_standard_page p ON p.source_id=s.source_id
               WHERE s.subject_title=? ORDER BY p.page_number""", (subject,),
        ).fetchall()
        ranked = []
        useful_terms = list(dict.fromkeys(terms[:12] + anchors))
        for row in rows:
            text = row["content_text"]
            matched = [term for term in useful_terms if term in text]
            stage_match = [term for term in stage_terms if term in text]
            heading = sum(marker in text for marker in ("内容要求", "学业要求", "学业质量", "课程内容"))
            score = 8 * len(stage_match) + 4 * len(matched) + 2 * heading
            if score:
                focus = (matched + stage_match)[0] if matched or stage_match else next(
                    marker for marker in ("内容要求", "学业要求", "学业质量", "课程内容") if marker in text
                )
                index = text.find(focus)
                ranked.append((score, -row["page_number"], row, focus, index))
        found = []
        for _, _, row, focus, index in sorted(ranked, reverse=True)[:6]:
            text = row["content_text"]
            excerpt = text[max(0, index - 260):index + len(focus) + 640]
            found.append({
                "source_ref": f"{row['source_id']}:{row['page_number']}", "source_title": row["title"],
                "page_number": row["page_number"], "exact_page_excerpt": excerpt,
            })
        return found

    @staticmethod
    def _subject_anchors(subject: str, text: str) -> list[str]:
        rules = {
            "语文": (
                (("习作", "作文", "写信"), ("表达与交流", "写作")),
                (("口语交际", "演讲", "辩论"), ("口语交际", "表达与交流")),
                (("古诗", "文言文"), ("古诗文", "诵读")),
                (("阅读", "课文", "单元"), ("阅读与鉴赏", "文学阅读", "梳理与探究")),
            ),
            "数学": (
                (("数", "式", "方程", "函数", "运算"), ("数与代数", "数与运算", "数量关系")),
                (("图形", "角", "圆", "三角", "四边形", "几何"), ("图形与几何", "图形的认识", "图形的测量")),
                (("统计", "概率", "平均数", "数据"), ("统计与概率", "数据意识")),
            ),
            "英语": (
                (("听力", "听说"), ("听", "理解性技能")),
                (("口语", "对话"), ("说", "表达性技能")),
                (("阅读", "故事"), ("读", "理解性技能")),
                (("写作", "书面"), ("写", "表达性技能")),
                (("词", "语法", "句型", "发音"), ("语言知识", "语音", "词汇", "语法")),
            ),
            "物理": (
                (("力", "运动", "能", "机械"), ("运动和相互作用", "能量")),
                (("电", "磁"), ("电磁能", "电和磁")),
                (("光", "声", "热"), ("声和光", "内能")),
                (("实验", "测量"), ("科学探究", "实验探究")),
            ),
            "化学": (
                (("物质", "元素", "分子", "原子"), ("物质的组成与结构", "物质的性质与应用")),
                (("反应", "方程式"), ("物质的化学变化", "化学反应")),
                (("实验", "制取"), ("科学探究与化学实验", "实验探究")),
            ),
        }
        result = []
        for triggers, anchors in rules.get(subject, ()):
            if any(trigger in text for trigger in triggers):
                result.extend(anchors)
        if not result:
            result.extend({
                "语文": ("阅读与鉴赏", "表达与交流"), "数学": ("课程内容", "学业质量"),
                "英语": ("语言能力", "学习能力"), "物理": ("科学思维", "科学探究"),
                "化学": ("化学观念", "科学探究"),
            }.get(subject, ("内容要求", "学业质量")))
        return list(dict.fromkeys(result))


class AtomicModelProtocol:
    @staticmethod
    def _action_mentions(text: str) -> set[str]:
        mentions = set()
        for action in OBSERVABLE_ACTIONS:
            for match in re.finditer(re.escape(action), text):
                suffix = text[match.end():]
                if suffix.startswith(ACTION_NOMINAL_SUFFIXES):
                    continue
                mentions.add(action)
                break
        return mentions

    @staticmethod
    def request(job_id: str, batch_id: str, packet: dict) -> ModelRequest:
        system = (
            "你是义务教育知识工程师。课程标题只是内容容器，转录只是证据。"
            "请提取可独立诊断、独立训练、独立更新掌握度的最小知识或能力。"
            "每项必须只有一个可观察动作和一个边界清楚的知识成分；不得把教师指令、课堂话语、一道题答案、"
            "章节名或含有多个动作的综合目标当作原子知识。observable_outcome必须严格写成"
            "‘学生能{action_verb}{knowledge_component}’，且只能出现所声明的一个动作词。只输出JSON对象。"
        )
        schema = {
            "definitions": [{
                "local_id": "批内唯一ID",
                "atom_kind": "只能填一个允许值: " + "|".join(sorted(ATOM_KINDS)),
                "action_verb": "只能填一个允许值: " + "|".join(sorted(OBSERVABLE_ACTIONS)),
                "knowledge_component": "2-80字，不含动作词",
                "boundary": {"context": "适用情境", "representation": "表征形式或unspecified", "exclusions": ["不包含什么"]},
                "observable_outcome": "只含一个可观察动作的学生表现",
                "mastery_criteria": {"recognition": "基础识别", "independent": "独立应用", "transfer": "变式或延迟迁移"},
                "diagnostic_blueprint": [
                    {"level": "recognition", "task_family": "题型族", "success_rule": "判定规则"},
                    {"level": "independent", "task_family": "题型族", "success_rule": "判定规则"},
                    {"level": "transfer", "task_family": "题型族", "success_rule": "判定规则"},
                ],
                "misconceptions": ["可观察的具体错误"], "concept_tags": ["概念标签"],
                "evidence": [{
                    "resource_id": "输入ID", "segment_id": "输入片段ID",
                    "exact_quote": "输入片段中连续且逐字相同的6-180字短引，不得用省略号拼接",
                    "evidence_role": "只能填一个允许值: " + "|".join(sorted(EVIDENCE_ROLES)),
                }],
                "standard_alignments": [{"source_ref": "输入课标引用", "exact_quote": "课标片段逐字短引"}],
                "confidence": "0到1",
            }],
            "relations": [{
                "source_local_id": "前项", "target_local_id": "后项",
                "relation_type": "只能填一个允许值: " + "|".join(sorted(RELATION_TYPES)),
                "rationale": "为何成立，不能用课时先后代替认知依赖", "evidence_ref": "segment_id或source_ref", "confidence": "0到1",
            }],
        }
        hard_rules = [
            "每个resource_id至少出现在一项evidence中；重复课时可共同支撑同一原子项",
            "每项evidence.exact_quote必须逐字存在于对应segment_id，课标引文也必须逐字存在",
            "每条exact_quote只能引用一段连续原文，禁止使用...或省略号连接多个位置；每条evidence_role只能填一个字符串值",
            "知识成分必须脱离本题仍可复测；具体题目、例题数字和答案放在证据中，不进入知识成分",
            "同一目标若含识别、计算、解释等多个动作必须拆开；同义重复项合并",
            "prerequisite_of方向为前置项到后继项，且必须说明认知依赖，禁止根据课程顺序臆造",
            "每项必须给出识别、独立应用、迁移或延迟复测三层标准",
            "observable_outcome使用‘学生能{action_verb}{knowledge_component}’模板，不得加入第二个动作词",
            "每个原子项只保留1到2条最有力的连续逐字证据，不要重复引用相同结论",
            "同一课程组输出2到12个稳定原子项；不要为每个例题数字或课堂活动重复造点",
            "若证据不足以形成原子项，宁可不生成，但仍需把该资源映射为其他已有意义项的证据",
        ]
        return ModelRequest(
            request_id=str(uuid4()), child_id="curriculum-system", session_id=f"atomic:{job_id}",
            request_type="atomic_knowledge_extraction", risk_level="medium",
            context_manifest={"purpose": "atomic_knowledge_extraction", "raw_media": False, "batch_id": batch_id},
            provider_preference="configured_primary", policy_version="local_text_evidence_v1",
            prompt_version=PROMPT_VERSION, knowledge_version=None, output_type="knowledge_structuring",
            evidence_refs=tuple(resource["resource_id"] for resource in packet["resources"]),
            messages=(
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps({"input": packet, "output_schema": schema, "hard_rules": hard_rules}, ensure_ascii=False)},
            ),
        )

    @staticmethod
    def repair_request(job_id: str, batch_id: str, packet: dict, error: Exception) -> ModelRequest:
        request = AtomicModelProtocol.request(job_id, batch_id, packet)
        correction = (
            "上一次输出未通过本地严格校验。失败原因："
            f"{str(error)[:180]}。请重新生成完整JSON；单个资源最多输出4个最关键原子项，"
            "所有evidence.exact_quote必须直接从给定segment原文连续复制，不得改写，"
            "atom_kind和action_verb只能使用schema列出的值。"
        )
        return replace(
            request,
            request_id=str(uuid4()),
            context_manifest={**request.context_manifest, "repair": True},
            messages=(*request.messages, {"role": "user", "content": correction}),
        )

    @staticmethod
    def parse(content: str, packet: dict) -> ValidatedAtomicBatch:
        value = content.strip()
        if value.startswith("```"):
            value = value.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        try:
            payload = json.loads(value)
        except json.JSONDecodeError as error:
            raise InvalidAtomicModelOutput("模型没有返回有效JSON") from error
        if isinstance(payload, dict) and set(payload) == {"atoms", "relations"}:
            payload = {"definitions": payload["atoms"], "relations": payload["relations"]}
        if not isinstance(payload, dict) or set(payload) != {"definitions", "relations"}:
            actual = sorted(payload) if isinstance(payload, dict) else [type(payload).__name__]
            raise InvalidAtomicModelOutput(
                f"顶层字段必须严格为definitions和relations，实际为{actual}"
            )
        definitions = payload["definitions"]
        if not isinstance(definitions, list) or not definitions or len(definitions) > max(12, len(packet["resources"]) * 8):
            raise InvalidAtomicModelOutput("原子项数量异常")
        segments = {
            (resource["resource_id"], segment["segment_id"]): segment["text"]
            for resource in packet["resources"] for segment in resource["segments"]
        }
        standards = {item["source_ref"]: item["exact_page_excerpt"] for item in packet["official_standard_evidence"]}
        resource_ids = {resource["resource_id"] for resource in packet["resources"]}
        seen_local: set[str] = set()
        covered_resources: set[str] = set()
        normalized = []
        required = {
            "local_id", "atom_kind", "action_verb", "knowledge_component", "boundary", "observable_outcome",
            "mastery_criteria", "diagnostic_blueprint", "misconceptions", "concept_tags", "evidence",
            "standard_alignments", "confidence",
        }
        definition_errors: list[str] = []

        def reject(local_id: str | None, message: str) -> None:
            if local_id is not None:
                seen_local.discard(local_id)
            definition_errors.append(message)

        for item in definitions:
            if not isinstance(item, dict) or set(item) != required:
                reject(None, "原子项字段不完整或包含额外字段")
                continue
            try:
                local_id = AtomicModelProtocol._text(item["local_id"], 1, 40)
            except InvalidAtomicModelOutput as error:
                reject(None, str(error))
                continue
            if local_id in seen_local:
                reject(None, "local_id重复")
                continue
            seen_local.add(local_id)
            atom_kind = ATOM_KIND_ALIASES.get(item["atom_kind"], item["atom_kind"])
            raw_action = item["action_verb"]
            action = ACTION_ALIASES.get(raw_action, raw_action)
            if atom_kind not in ATOM_KINDS or action not in OBSERVABLE_ACTIONS:
                reject(local_id, "原子类型或动作不受支持")
                continue
            try:
                component = AtomicModelProtocol._text(item["knowledge_component"], 2, 80)
            except InvalidAtomicModelOutput as error:
                reject(local_id, str(error))
                continue
            if component in UMBRELLA_COMPONENTS or GENERIC_COMPONENT.fullmatch(component) or DISCOURSE_NOISE.search(component):
                reject(local_id, f"知识成分不是可测原子项: {component}")
                continue
            if AtomicModelProtocol._action_mentions(component):
                reject(local_id, f"知识成分混入动作或多个目标: {component}")
                continue
            try:
                outcome = AtomicModelProtocol._text(item["observable_outcome"], 6, 180)
            except InvalidAtomicModelOutput as error:
                reject(local_id, str(error))
                continue
            if action != raw_action:
                outcome = outcome.replace(raw_action, action, 1)
            actions_in_outcome = AtomicModelProtocol._action_mentions(outcome)
            if actions_in_outcome != {action}:
                reject(local_id,
                    "可观察结果必须且只能使用所声明的一个动作："
                    f"local_id={local_id}, declared={action}, found={sorted(actions_in_outcome)}"
                )
                continue
            try:
                fields = AtomicModelProtocol._normalize_definition_fields(item, segments, standards)
            except InvalidAtomicModelOutput as error:
                reject(local_id, str(error))
                continue
            evidence = fields["evidence"]
            if not evidence:
                reject(local_id, "转录证据无法逐字核对：原子项没有可保留的连续证据")
                continue
            covered_resources.update(value["resource_id"] for value in evidence)
            normalized.append({
                "local_id": local_id, "atom_kind": atom_kind, "action_verb": action,
                "knowledge_component": component, "observable_outcome": outcome, **fields,
            })
        if not normalized:
            raise InvalidAtomicModelOutput(
                definition_errors[0] if definition_errors
                else "转录证据无法逐字核对：批次内没有可保留的原子项"
            )
        if covered_resources != resource_ids:
            raise InvalidAtomicModelOutput("模型没有为批次内每个资源提供逐字证据")
        relations = AtomicModelProtocol._validate_relations(payload["relations"], seen_local, segments, standards)
        AtomicModelProtocol._validate_prerequisite_graph(relations)
        return ValidatedAtomicBatch(normalized, relations)

    @staticmethod
    def _normalize_definition_fields(item: dict, segments: dict, standards: dict) -> dict:
        boundary = item["boundary"]
        if not isinstance(boundary, dict) or set(boundary) != {"context", "representation", "exclusions"}:
            raise InvalidAtomicModelOutput("能力边界字段无效")
        normalized_boundary = {
            "context": AtomicModelProtocol._text(boundary["context"], 1, 100),
            "representation": AtomicModelProtocol._text(boundary["representation"], 1, 60),
            "exclusions": AtomicModelProtocol._list(boundary["exclusions"], 0, 6, 100),
        }
        criteria = item["mastery_criteria"]
        if not isinstance(criteria, dict) or set(criteria) != {"recognition", "independent", "transfer"}:
            raise InvalidAtomicModelOutput("掌握标准必须包含三个层级")
        normalized_criteria = {
            key: AtomicModelProtocol._text(criteria[key], 6, 180) for key in criteria
        }
        blueprint = item["diagnostic_blueprint"]
        if not isinstance(blueprint, list) or len(blueprint) != 3:
            raise InvalidAtomicModelOutput("诊断蓝图必须正好三层")
        levels = []
        for task in blueprint:
            if not isinstance(task, dict) or set(task) != {"level", "task_family", "success_rule"}:
                raise InvalidAtomicModelOutput("诊断蓝图字段无效")
            if task["level"] not in {"recognition", "independent", "transfer"}:
                raise InvalidAtomicModelOutput("诊断层级无效")
            levels.append({
                "level": task["level"],
                "task_family": AtomicModelProtocol._text(task["task_family"], 2, 100),
                "success_rule": AtomicModelProtocol._text(task["success_rule"], 4, 160),
            })
        if {value["level"] for value in levels} != {"recognition", "independent", "transfer"}:
            raise InvalidAtomicModelOutput("诊断蓝图缺少层级")
        return {
            "boundary": normalized_boundary,
            "mastery_criteria": normalized_criteria,
            "diagnostic_blueprint": levels,
            "misconceptions": AtomicModelProtocol._list(item["misconceptions"], 1, 10, 180),
            "concept_tags": AtomicModelProtocol._list(item["concept_tags"], 1, 10, 40),
            "evidence": AtomicModelProtocol._validate_evidence(item["evidence"], segments),
            "standard_alignments": AtomicModelProtocol._validate_alignments(item["standard_alignments"], standards),
            "confidence": AtomicModelProtocol._confidence(item["confidence"]),
        }

    @staticmethod
    def _validate_evidence(value: Any, segments: dict[tuple[str, str], str]) -> list[dict]:
        if not isinstance(value, list) or not 1 <= len(value) <= 8:
            raise InvalidAtomicModelOutput("证据数量无效")
        result = []
        for item in value:
            if not isinstance(item, dict) or set(item) != {"resource_id", "segment_id", "exact_quote", "evidence_role"}:
                continue
            key = (str(item["resource_id"]), str(item["segment_id"]))
            try:
                quote = AtomicModelProtocol._text(item["exact_quote"], 6, 180)
            except InvalidAtomicModelOutput:
                continue
            if key not in segments:
                continue
            quote = AtomicModelProtocol._anchor_quote(quote, segments[key])
            if quote is None:
                continue
            if item["evidence_role"] not in EVIDENCE_ROLES:
                continue
            result.append({"resource_id": key[0], "segment_id": key[1], "exact_quote": quote, "evidence_role": item["evidence_role"]})
        return result

    @staticmethod
    def _validate_alignments(value: Any, standards: dict[str, str]) -> list[dict]:
        if not isinstance(value, list) or len(value) > 6:
            raise InvalidAtomicModelOutput("课标对齐数量无效")
        result = []
        for item in value:
            if not isinstance(item, dict) or set(item) != {"source_ref", "exact_quote"}:
                continue
            ref = str(item["source_ref"])
            try:
                quote = AtomicModelProtocol._text(item["exact_quote"], 8, 180)
            except InvalidAtomicModelOutput:
                continue
            if ref not in standards:
                continue
            quote = AtomicModelProtocol._anchor_quote(quote, standards[ref])
            if quote is None:
                continue
            result.append({"source_ref": ref, "exact_quote": quote})
        return result

    @staticmethod
    def _anchor_quote(quote: str, source: str) -> str | None:
        if quote in source:
            return quote
        source_chars: list[str] = []
        source_indexes: list[int] = []
        for index, char in enumerate(source):
            if not QUOTE_IGNORABLE.fullmatch(char):
                source_chars.append(char)
                source_indexes.append(index)
        normalized_quote = QUOTE_IGNORABLE.sub("", quote)
        if len(normalized_quote) < 6:
            return None
        normalized_source = "".join(source_chars)
        start = normalized_source.find(normalized_quote)
        length = len(normalized_quote)
        if start < 0:
            match = SequenceMatcher(None, normalized_quote, normalized_source, autojunk=False).find_longest_match(
                0, len(normalized_quote), 0, len(normalized_source)
            )
            minimum = max(12, round(len(normalized_quote) * 0.45))
            if match.size < minimum:
                return None
            start = match.b
            length = match.size
        exact = source[source_indexes[start]:source_indexes[start + length - 1] + 1]
        return exact if exact in source else None

    @staticmethod
    def _validate_relations(value: Any, local_ids: set[str], segments: dict, standards: dict) -> list[dict]:
        if not isinstance(value, list) or len(value) > len(local_ids) * 4:
            raise InvalidAtomicModelOutput("关系数量无效")
        result = []
        for item in value:
            required = {"source_local_id", "target_local_id", "relation_type", "rationale", "evidence_ref", "confidence"}
            if not isinstance(item, dict) or set(item) != required:
                continue
            source, target = str(item["source_local_id"]), str(item["target_local_id"])
            if source not in local_ids or target not in local_ids or source == target:
                continue
            if item["relation_type"] not in RELATION_TYPES:
                continue
            evidence_ref = str(item["evidence_ref"])
            valid_refs = {segment_id for _, segment_id in segments} | set(standards)
            if evidence_ref not in valid_refs:
                continue
            try:
                rationale = AtomicModelProtocol._text(item["rationale"], 8, 240)
                confidence = AtomicModelProtocol._confidence(item["confidence"])
            except InvalidAtomicModelOutput:
                continue
            result.append({
                "source_local_id": source, "target_local_id": target, "relation_type": item["relation_type"],
                "rationale": rationale, "evidence_ref": evidence_ref, "confidence": confidence,
            })
        return result

    @staticmethod
    def _validate_prerequisite_graph(relations: list[dict]) -> None:
        adjacency: dict[str, set[str]] = {}
        for item in relations:
            if item["relation_type"] != "prerequisite_of":
                continue
            adjacency.setdefault(item["source_local_id"], set()).add(item["target_local_id"])
            adjacency.setdefault(item["target_local_id"], set())
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node: str) -> None:
            if node in visiting:
                raise InvalidAtomicModelOutput("前置关系形成循环")
            if node in visited:
                return
            visiting.add(node)
            for target in adjacency[node]:
                visit(target)
            visiting.remove(node); visited.add(node)

        for node in adjacency:
            visit(node)

    @staticmethod
    def _text(value: Any, minimum: int, maximum: int) -> str:
        if not isinstance(value, str) or not minimum <= len(value.strip()) <= maximum:
            raise InvalidAtomicModelOutput("文本字段长度无效")
        return value.strip()

    @staticmethod
    def _list(value: Any, minimum: int, maximum: int, item_maximum: int) -> list[str]:
        if not isinstance(value, list) or not minimum <= len(value) <= maximum:
            raise InvalidAtomicModelOutput("列表字段长度无效")
        result = []
        for item in value:
            text = AtomicModelProtocol._text(item, 1, item_maximum)
            if text not in result:
                result.append(text)
        if len(result) < minimum:
            raise InvalidAtomicModelOutput("列表去重后数量不足")
        return result

    @staticmethod
    def _confidence(value: Any) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= float(value) <= 1:
            raise InvalidAtomicModelOutput("置信度无效")
        return round(float(value), 3)


class AtomicKnowledgeExtractionService:
    """Durable batch runner. Model drafts never become mastery-active automatically."""

    def __init__(self, database: Database, gateway: ModelGateway):
        self.database = database
        self.gateway = gateway
        self.packets = AtomicEvidencePacketBuilder(database)

    def create_job(self, *, idempotency_key: str, grades: list[str], subjects: list[str], max_resources: int | None = None) -> dict:
        now = utc_now()
        with self.database.connect() as connection:
            ensure_schema(connection, now)
            existing = connection.execute(
                "SELECT job_id FROM kg_atomic_extraction_job WHERE idempotency_key=?", (idempotency_key,),
            ).fetchone()
            if existing:
                return self.get_job(existing["job_id"])
            clauses, parameters = ["COALESCE(grade_scope,'') IN (%s)" % ",".join("?" for _ in grades)], list(grades)
            clauses.append("COALESCE(subject_scope,'') IN (%s)" % ",".join("?" for _ in subjects)); parameters.extend(subjects)
            sql = f"SELECT resource_id,grade_scope,subject_scope,metadata_json,title FROM kg_resource WHERE {' AND '.join(clauses)} ORDER BY grade_scope,subject_scope,resource_id"
            if max_resources:
                sql += " LIMIT ?"; parameters.append(max_resources)
            rows = connection.execute(sql, parameters).fetchall()
            if not rows:
                raise ValueError("scope has no resources")
            groups: dict[tuple[str, str, str], list[str]] = {}
            for row in rows:
                metadata = json.loads(row["metadata_json"] or "{}")
                lesson = str(metadata.get("chapter_title") or row["title"])
                lesson = re.sub(r"第[一二三四五六七八九十\d]+课时", "", lesson).strip()
                groups.setdefault((row["grade_scope"] or "", row["subject_scope"] or "", lesson), []).append(row["resource_id"])
            batches: list[list[str]] = []
            for resource_ids in groups.values():
                for offset in range(0, len(resource_ids), MAX_RESOURCES_PER_BATCH):
                    batches.append(resource_ids[offset:offset + MAX_RESOURCES_PER_BATCH])
            job_id = str(uuid4())
            scope = {"grades": grades, "subjects": subjects, "max_resources": max_resources}
            connection.execute(
                """INSERT INTO kg_atomic_extraction_job(job_id,idempotency_key,status,scope_json,prompt_version,total_batches,created_at,updated_at)
                   VALUES(?,?,'pending',?,?,?,?,?)""",
                (job_id, idempotency_key, json.dumps(scope, ensure_ascii=False), PROMPT_VERSION, len(batches), now, now),
            )
            for index, resource_ids in enumerate(batches):
                packet = self.packets.build(resource_ids)
                payload = json.dumps(packet, ensure_ascii=False, sort_keys=True)
                connection.execute(
                    """INSERT INTO kg_atomic_extraction_batch(batch_id,job_id,batch_index,resource_ids_json,input_json,input_sha256,status,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,'pending',?,?)""",
                    (str(uuid4()), job_id, index, json.dumps(resource_ids), payload, hashlib.sha256(payload.encode()).hexdigest(), now, now),
                )
            connection.commit()
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> dict:
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM kg_atomic_extraction_job WHERE job_id=?", (job_id,)).fetchone()
        if not row:
            raise ValueError("unknown atomic extraction job")
        return dict(row)

    async def run(self, job_id: str, *, stop_after: int | None = None, concurrency: int = 1) -> dict:
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute("UPDATE kg_atomic_extraction_job SET status='processing',updated_at=? WHERE job_id=?", (now, job_id))
            connection.execute("UPDATE kg_atomic_extraction_batch SET status='interrupted',updated_at=? WHERE job_id=? AND status='processing'", (now, job_id))
            connection.execute("UPDATE kg_atomic_extraction_batch SET status='pending',updated_at=? WHERE job_id=? AND status='interrupted'", (now, job_id))
            connection.execute(
                """UPDATE kg_atomic_extraction_batch SET status='pending',error_code=NULL,error_detail=NULL,
                   rejected_output_json=NULL,completed_at=NULL,updated_at=?
                   WHERE job_id=? AND status='failed' AND attempt_count<3""",
                (now, job_id),
            )
            connection.commit()
        concurrency = max(1, min(8, int(concurrency)))
        processed = 0
        async def worker() -> None:
            nonlocal processed
            while stop_after is None or processed < stop_after:
                batch = self._claim_batch(job_id)
                if not batch:
                    return
                processed += 1
                await self._process_batch(job_id, batch)
                self._checkpoint_progress(job_id)
                self._sync_terminal_gaps(job_id)

        while True:
            await asyncio.gather(*(worker() for _ in range(concurrency)))
            if stop_after is not None or not self._reset_retryable_failures(job_id):
                break
        return self._finish(job_id)

    def _claim_batch(self, job_id: str) -> Any | None:
        with self.database.connect() as connection:
            batch = connection.execute(
                "SELECT * FROM kg_atomic_extraction_batch WHERE job_id=? AND status='pending' ORDER BY batch_index LIMIT 1",
                (job_id,),
            ).fetchone()
            if not batch:
                return None
            connection.execute(
                "UPDATE kg_atomic_extraction_batch SET status='processing',attempt_count=attempt_count+1,updated_at=? WHERE batch_id=?",
                (utc_now(), batch["batch_id"]),
            )
            connection.commit()
        return batch

    async def _process_batch(self, job_id: str, batch: Any) -> None:
        original_packet = json.loads(batch["input_json"])
        packet, dispositions = self._partition_packet(original_packet)
        result = None
        try:
            if not packet["resources"]:
                result = ModelResult("local-noninstructional", "local", CONTENT_DETECTOR_VERSION, 0, 0, 0)
                validated = ValidatedAtomicBatch([], [], tuple(dispositions))
            else:
                try:
                    result = await self.gateway.complete(
                        AtomicModelProtocol.request(job_id, batch["batch_id"], packet)
                    )
                    validated = AtomicModelProtocol.parse(result.content, packet)
                except ModelResponseError as error:
                    if error.code != "MODEL_INVALID_JSON" or len(packet["resources"]) == 1:
                        raise
                    validated, result = await self._extract_split_batch(
                        job_id, batch["batch_id"], packet, None,
                    )
                except InvalidAtomicModelOutput as error:
                    if len(packet["resources"]) == 1:
                        validated, result = await self._repair_single_resource(
                            job_id, batch["batch_id"], packet, result, error,
                        )
                    else:
                        validated, result = await self._extract_split_batch(
                            job_id, batch["batch_id"], packet, result,
                        )
                validated = ValidatedAtomicBatch(
                    validated.definitions, validated.relations, tuple(dispositions),
                )
            self._store(batch, validated, result)
        except Exception as error:
            with self.database.connect() as connection:
                connection.execute(
                    """UPDATE kg_atomic_extraction_batch SET status='failed',error_code=?,error_detail=?,
                       rejected_output_json=?,updated_at=?,completed_at=? WHERE batch_id=?""",
                    (type(error).__name__, str(error)[:500], (result.content[:100000] if result else None),
                     utc_now(), utc_now(), batch["batch_id"]),
                )
                if result:
                    connection.execute(
                        """UPDATE kg_atomic_extraction_job SET
                           prompt_tokens=prompt_tokens+?,completion_tokens=completion_tokens+?,updated_at=?
                           WHERE job_id=?""",
                        (result.prompt_tokens or 0, result.completion_tokens or 0, utc_now(), job_id),
                    )
                connection.commit()

    @staticmethod
    def _noninstructional_reason(resource: dict) -> str | None:
        text = "".join(str(segment.get("text") or "") for segment in resource.get("segments", []))
        normalized = QUOTE_IGNORABLE.sub("", text)
        if len(normalized) < 6:
            return "no_usable_transcript"
        if len(normalized) < 80:
            return None
        watermarks = (
            "字幕志愿者李宗盛", "字幕志愿者", "明镜与点点栏目",
            "请不吝点赞订阅转发打赏支持明镜与点点栏目",
            "优优独播剧场YoYoTelevisionSeriesExclusive", "与点点栏目",
        )
        residual = normalized
        known_noise_chars = 0
        for watermark in watermarks:
            occurrences = residual.count(watermark)
            known_noise_chars += occurrences * len(watermark)
            residual = residual.replace(watermark, "")
        if known_noise_chars >= len(normalized) * 0.35:
            if any(marker in residual for marker in OBJECTIVE_MARKERS + ASSESSMENT_MARKERS):
                return None
            return "repeated_transcript_noise" if len(residual) < 20 else None
        instructional_markers = OBJECTIVE_MARKERS + ASSESSMENT_MARKERS + SUMMARY_MARKERS
        if sum(text.count(marker) for marker in instructional_markers) >= 2:
            return None
        maximum_period = min(64, len(normalized) // 3)
        for period in range(4, maximum_period + 1):
            matching = sum(
                normalized[index] == normalized[index % period]
                for index in range(period, len(normalized))
            )
            if matching / (len(normalized) - period) >= 0.94:
                return "repeated_transcript_noise"
        return None

    @classmethod
    def _partition_packet(cls, packet: dict) -> tuple[dict, list[dict]]:
        resources = []
        dispositions = []
        for resource in packet["resources"]:
            original_segments = resource.get("segments", [])
            usable_segments = [
                segment for segment in original_segments
                if cls._noninstructional_reason({"segments": [segment]}) is None
            ]
            filtered_resource = {
                **resource,
                "segments": usable_segments or original_segments,
            }
            reason = cls._noninstructional_reason(filtered_resource)
            if reason is None:
                resources.append(filtered_resource)
                continue
            dispositions.append({
                "resource_id": resource["resource_id"],
                "disposition": "noninstructional",
                "reason_code": reason,
                "detector_version": CONTENT_DETECTOR_VERSION,
            })
        return {**packet, "resources": resources}, dispositions

    async def _extract_split_batch(
        self, job_id: str, batch_id: str, packet: dict, original: ModelResult | None,
    ) -> tuple[ValidatedAtomicBatch, ModelResult]:
        definitions: list[dict] = []
        relations: list[dict] = []
        results = [original] if original else []
        for index, resource in enumerate(packet["resources"]):
            subpacket = {
                "resources": [resource],
                "official_standard_evidence": packet["official_standard_evidence"],
            }
            subresult = None
            try:
                subresult = await self.gateway.complete(
                    AtomicModelProtocol.request(job_id, f"{batch_id}:resource:{index}", subpacket)
                )
                validated = AtomicModelProtocol.parse(subresult.content, subpacket)
            except ModelResponseError as error:
                if error.code != "MODEL_INVALID_JSON":
                    raise
                validated, subresult = await self._repair_single_resource(
                    job_id, f"{batch_id}:resource:{index}", subpacket, None, error,
                )
            except InvalidAtomicModelOutput as error:
                validated, subresult = await self._repair_single_resource(
                    job_id, f"{batch_id}:resource:{index}", subpacket, subresult, error,
                )
            id_map = {
                item["local_id"]: f"r{index}_{item['local_id']}" for item in validated.definitions
            }
            definitions.extend([
                {**item, "local_id": id_map[item["local_id"]]} for item in validated.definitions
            ])
            relations.extend([
                {
                    **item,
                    "source_local_id": id_map[item["source_local_id"]],
                    "target_local_id": id_map[item["target_local_id"]],
                }
                for item in validated.relations
            ])
            results.append(subresult)
        aggregate = ModelResult(
            content="split-resource-repair",
            provider=results[-1].provider,
            model=results[-1].model,
            latency_ms=sum(item.latency_ms for item in results),
            prompt_tokens=sum(item.prompt_tokens or 0 for item in results),
            completion_tokens=sum(item.completion_tokens or 0 for item in results),
            fallback_used=any(item.fallback_used for item in results),
        )
        return ValidatedAtomicBatch(definitions, relations), aggregate

    async def _repair_single_resource(
        self,
        job_id: str,
        batch_id: str,
        packet: dict,
        original: ModelResult | None,
        error: Exception,
    ) -> tuple[ValidatedAtomicBatch, ModelResult]:
        if not getattr(self.gateway, "fallback_configured", False):
            raise error
        repaired = await self.gateway.complete_on_fallback(
            AtomicModelProtocol.repair_request(job_id, f"{batch_id}:repair", packet, error)
        )
        validated = AtomicModelProtocol.parse(repaired.content, packet)
        results = [item for item in (original, repaired) if item is not None]
        aggregate = ModelResult(
            content=repaired.content,
            provider=repaired.provider,
            model=repaired.model,
            latency_ms=sum(item.latency_ms for item in results),
            prompt_tokens=sum(item.prompt_tokens or 0 for item in results),
            completion_tokens=sum(item.completion_tokens or 0 for item in results),
            fallback_used=True,
        )
        return validated, aggregate

    def _reset_retryable_failures(self, job_id: str) -> int:
        with self.database.connect() as connection:
            retried = connection.execute(
                """UPDATE kg_atomic_extraction_batch SET status='pending',error_code=NULL,error_detail=NULL,
                   rejected_output_json=NULL,completed_at=NULL,updated_at=?
                   WHERE job_id=? AND status='failed' AND attempt_count<3""",
                (utc_now(), job_id),
            ).rowcount
            connection.commit()
        return retried

    def _checkpoint_progress(self, job_id: str) -> None:
        with self.database.connect() as connection:
            counts = connection.execute(
                """SELECT SUM(status='completed') completed,SUM(status='failed') failed
                   FROM kg_atomic_extraction_batch WHERE job_id=?""",
                (job_id,),
            ).fetchone()
            drafts = connection.execute(
                "SELECT COUNT(DISTINCT draft_id) FROM kg_atomic_draft_batch WHERE job_id=?", (job_id,),
            ).fetchone()[0]
            connection.execute(
                """UPDATE kg_atomic_extraction_job SET completed_batches=?,failed_batches=?,draft_count=?,updated_at=?
                   WHERE job_id=?""",
                (counts["completed"] or 0, counts["failed"] or 0, drafts, utc_now(), job_id),
            )
            connection.commit()

    def _sync_terminal_gaps(self, job_id: str) -> int:
        now = utc_now()
        created = 0
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT batch_id,resource_ids_json,attempt_count,error_code,error_detail
                   FROM kg_atomic_extraction_batch
                   WHERE job_id=? AND status='failed' AND attempt_count>=3""",
                (job_id,),
            ).fetchall()
            for row in rows:
                for resource_id in json.loads(row["resource_ids_json"]):
                    gap_id = "atom-gap-" + hashlib.sha256(
                        f"{job_id}|{resource_id}".encode()
                    ).hexdigest()[:24]
                    created += connection.execute(
                        """INSERT INTO kg_atomic_resource_gap(gap_id,job_id,batch_id,resource_id,status,
                           reason_code,attempt_count,error_code,error_detail,created_at,updated_at)
                           VALUES(?,?,?,?,'open','terminal_model_validation_failure',?,?,?,?,?)
                           ON CONFLICT(job_id,resource_id) DO UPDATE SET batch_id=excluded.batch_id,
                             attempt_count=excluded.attempt_count,error_code=excluded.error_code,
                             error_detail=excluded.error_detail,updated_at=excluded.updated_at""",
                        (gap_id, job_id, row["batch_id"], resource_id, row["attempt_count"],
                         row["error_code"], row["error_detail"], now, now),
                    ).rowcount
            connection.commit()
        return created

    def _store(self, batch: Any, validated: ValidatedAtomicBatch, result: Any) -> None:
        packet = json.loads(batch["input_json"])
        scope = packet["resources"][0]["scope"]
        grade_band = self._grade_band(scope["grade"])
        local_to_signature: dict[str, str] = {}
        now = utc_now()
        expected_resources = {resource["resource_id"] for resource in packet["resources"]}
        covered_resources = {
            evidence["resource_id"]
            for item in validated.definitions
            for evidence in item["evidence"]
        }
        disposed_resources = {item["resource_id"] for item in validated.dispositions}
        if expected_resources != covered_resources | disposed_resources:
            raise InvalidAtomicModelOutput("批次资源必须有逐字证据或可审计的非教学处置")
        if covered_resources & disposed_resources:
            raise InvalidAtomicModelOutput("资源不能同时标记为证据覆盖和非教学内容")
        with self.database.connect() as connection:
            if expected_resources:
                placeholders = ",".join("?" for _ in expected_resources)
                connection.execute(
                    f"""UPDATE kg_atomic_resource_gap SET status='resolved',resolution_ref=?,
                        updated_at=?,resolved_at=? WHERE job_id=? AND resource_id IN ({placeholders})""",
                    (f"batch:{batch['batch_id']}", now, now, batch["job_id"], *sorted(expected_resources)),
                )
            for disposition in validated.dispositions:
                disposition_id = "atom-disposition-" + hashlib.sha256(
                    f"{batch['job_id']}|{disposition['resource_id']}".encode()
                ).hexdigest()[:24]
                connection.execute(
                    """INSERT INTO kg_atomic_resource_disposition(disposition_id,job_id,batch_id,resource_id,
                       disposition,reason_code,detector_version,details_json,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,'{}',?,?)
                       ON CONFLICT(job_id,resource_id) DO UPDATE SET batch_id=excluded.batch_id,
                         disposition=excluded.disposition,reason_code=excluded.reason_code,
                         detector_version=excluded.detector_version,updated_at=excluded.updated_at""",
                    (disposition_id, batch["job_id"], batch["batch_id"], disposition["resource_id"],
                     disposition["disposition"], disposition["reason_code"], disposition["detector_version"], now, now),
                )
            for item in validated.definitions:
                signature = self._signature(grade_band, scope["subject"], item)
                local_to_signature[item["local_id"]] = signature
                draft_id = f"atom-draft-{signature[:24]}"
                connection.execute(
                    """INSERT INTO kg_atomic_definition_draft(draft_id,canonical_signature,title,grade_band,subject,atom_kind,
                       action_verb,knowledge_component,boundary_json,observable_outcome,mastery_criteria_json,
                       diagnostic_blueprint_json,misconception_hypotheses_json,concept_tags_json,standard_alignments_json,
                       confidence,review_status,source_method,model_provider,model_name,prompt_version,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'unreviewed','model',?,?,?,?,?)
                       ON CONFLICT(canonical_signature,prompt_version) DO UPDATE SET
                         confidence=MAX(confidence,excluded.confidence),updated_at=excluded.updated_at""",
                    (draft_id, signature, f"{item['action_verb']}{item['knowledge_component']}", grade_band, scope["subject"],
                     item["atom_kind"], item["action_verb"], item["knowledge_component"],
                     json.dumps(item["boundary"], ensure_ascii=False), item["observable_outcome"],
                     json.dumps(item["mastery_criteria"], ensure_ascii=False), json.dumps(item["diagnostic_blueprint"], ensure_ascii=False),
                     json.dumps(item["misconceptions"], ensure_ascii=False), json.dumps(item["concept_tags"], ensure_ascii=False),
                     json.dumps(item["standard_alignments"], ensure_ascii=False), item["confidence"], result.provider, result.model,
                     PROMPT_VERSION, now, now),
                )
                stored = connection.execute(
                    "SELECT draft_id FROM kg_atomic_definition_draft WHERE canonical_signature=? AND prompt_version=?",
                    (signature, PROMPT_VERSION),
                ).fetchone()["draft_id"]
                connection.execute(
                    """INSERT OR IGNORE INTO kg_atomic_draft_batch(draft_id,batch_id,job_id,created_at)
                       VALUES(?,?,?,?)""",
                    (stored, batch["batch_id"], batch["job_id"], now),
                )
                for evidence in item["evidence"]:
                    quote_hash = hashlib.sha256(evidence["exact_quote"].encode()).hexdigest()
                    evidence_key = "|".join((stored, evidence["resource_id"], evidence["segment_id"], quote_hash))
                    evidence_id = f"atom-evidence-{hashlib.sha256(evidence_key.encode()).hexdigest()[:24]}"
                    connection.execute(
                        """INSERT OR IGNORE INTO kg_atomic_draft_evidence(evidence_id,draft_id,resource_id,segment_id,exact_quote,
                           quote_sha256,evidence_role,confidence,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                        (evidence_id, stored, evidence["resource_id"], evidence["segment_id"], evidence["exact_quote"], quote_hash,
                         evidence["evidence_role"], item["confidence"], now),
                    )
                for alignment in item["standard_alignments"]:
                    quote_hash = hashlib.sha256(alignment["exact_quote"].encode()).hexdigest()
                    alignment_key = "|".join((stored, alignment["source_ref"], quote_hash))
                    alignment_id = f"atom-standard-{hashlib.sha256(alignment_key.encode()).hexdigest()[:24]}"
                    connection.execute(
                        """INSERT OR IGNORE INTO kg_atomic_draft_standard_alignment(alignment_id,draft_id,source_ref,
                           exact_quote,quote_sha256,confidence,created_at) VALUES(?,?,?,?,?,?,?)""",
                        (alignment_id, stored, alignment["source_ref"], alignment["exact_quote"], quote_hash,
                         item["confidence"], now),
                    )
            for relation in validated.relations:
                source = local_to_signature[relation["source_local_id"]]
                target = local_to_signature[relation["target_local_id"]]
                relation_key = "|".join((source, target, relation["relation_type"]))
                relation_id = f"atom-relation-{hashlib.sha256(relation_key.encode()).hexdigest()[:24]}"
                connection.execute(
                    """INSERT OR IGNORE INTO kg_atomic_relation_candidate(relation_id,source_key,target_key,relation_type,rationale,
                       evidence_ref,confidence,review_status,version_label,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,'unreviewed',?,?,?)""",
                    (relation_id, source, target, relation["relation_type"], relation["rationale"], relation["evidence_ref"],
                     relation["confidence"], PROMPT_VERSION, now, now),
                )
            output_hash = hashlib.sha256(json.dumps({
                "definitions": validated.definitions,
                "dispositions": validated.dispositions,
            }, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            connection.execute(
                """UPDATE kg_atomic_extraction_batch SET status='completed',output_sha256=?,model_provider=?,model_name=?,error_code=NULL,error_detail=NULL,rejected_output_json=NULL,
                   updated_at=?,completed_at=? WHERE batch_id=?""",
                (output_hash, result.provider, result.model, now, now, batch["batch_id"]),
            )
            connection.execute(
                """UPDATE kg_atomic_extraction_job SET
                   prompt_tokens=prompt_tokens+?,completion_tokens=completion_tokens+?,updated_at=? WHERE job_id=?""",
                (result.prompt_tokens or 0, result.completion_tokens or 0, now, batch["job_id"]),
            ); connection.commit()

    def _finish(self, job_id: str) -> dict:
        now = utc_now()
        with self.database.connect() as connection:
            counts = connection.execute(
                """SELECT COUNT(*) total,SUM(status='completed') completed,SUM(status='failed') failed,
                   SUM(status='pending') pending,SUM(status='processing') processing
                   FROM kg_atomic_extraction_batch WHERE job_id=?""", (job_id,),
            ).fetchone()
            draft_count = connection.execute(
                "SELECT COUNT(DISTINCT draft_id) FROM kg_atomic_draft_batch WHERE job_id=?", (job_id,),
            ).fetchone()[0]
            status = (
                "completed" if counts["completed"] == counts["total"]
                else "failed" if not (counts["pending"] or counts["processing"]) and counts["failed"]
                else "processing"
            )
            connection.execute(
                """UPDATE kg_atomic_extraction_job SET status=?,completed_batches=?,failed_batches=?,draft_count=?,updated_at=?,
                   completed_at=CASE WHEN ? IN ('completed','failed') THEN ? ELSE NULL END WHERE job_id=?""",
                (status, counts["completed"] or 0, counts["failed"] or 0, draft_count, now, status, now, job_id),
            ); connection.commit()
        return self.get_job(job_id)

    @staticmethod
    def _grade_band(grade: str) -> str:
        if grade in {"一年级", "二年级"}:
            return "小学1-2"
        if grade in {"三年级", "四年级"}:
            return "小学3-4"
        if grade in {"五年级", "六年级"}:
            return "小学5-6"
        return "初中7-9" if grade in {"初一", "初二", "初三"} else grade

    @staticmethod
    def _signature(grade_band: str, subject: str, item: dict) -> str:
        normalize = lambda value: re.sub(r"[\s，,。；;：:（）()]", "", str(value)).lower()
        identity = "|".join((normalize(grade_band), normalize(subject), item["atom_kind"],
                             normalize(item["action_verb"]), normalize(item["knowledge_component"])))
        return hashlib.sha256(identity.encode()).hexdigest()


class AtomicMergeCandidateService:
    """Find likely paraphrases without silently merging mastery targets."""

    algorithm_version = "atomic-merge-v1"

    def __init__(self, database: Database):
        self.database = database

    def generate(self, *, minimum_similarity: float = .72) -> dict:
        now = utc_now()
        with self.database.connect() as connection:
            ensure_schema(connection, now)
            rows = connection.execute(
                """SELECT draft_id,grade_band,subject,atom_kind,action_verb,knowledge_component,concept_tags_json
                   FROM kg_atomic_definition_draft WHERE review_status IN ('unreviewed','approved')
                   ORDER BY draft_id"""
            ).fetchall()
            by_block: dict[tuple[str, str, str, str], list[dict]] = {}
            for row in rows:
                item = dict(row); item["concept_tags"] = set(json.loads(row["concept_tags_json"] or "[]"))
                key = (row["grade_band"], row["subject"], row["atom_kind"], row["action_verb"])
                by_block.setdefault(key, []).append(item)
            inserted = compared = 0
            for items in by_block.values():
                tag_index: dict[str, set[int]] = {}
                for index, item in enumerate(items):
                    for tag in item["concept_tags"]:
                        tag_index.setdefault(tag, set()).add(index)
                seen: set[tuple[int, int]] = set()
                for indexes in tag_index.values():
                    ordered = sorted(indexes)
                    for offset, left_index in enumerate(ordered):
                        for right_index in ordered[offset + 1:]:
                            if (left_index, right_index) in seen:
                                continue
                            seen.add((left_index, right_index)); compared += 1
                            left, right = items[left_index], items[right_index]
                            similarity = self.similarity(
                                left["knowledge_component"], right["knowledge_component"],
                                left["concept_tags"], right["concept_tags"],
                            )
                            if similarity < minimum_similarity:
                                continue
                            shared = sorted(left["concept_tags"] & right["concept_tags"])
                            left_id, right_id = sorted((left["draft_id"], right["draft_id"]))
                            key = f"{left_id}|{right_id}|{self.algorithm_version}"
                            merge_id = f"atom-merge-{hashlib.sha256(key.encode()).hexdigest()[:24]}"
                            result = connection.execute(
                                """INSERT OR IGNORE INTO kg_atomic_merge_candidate(merge_id,left_draft_id,right_draft_id,
                                   lexical_similarity,shared_concepts_json,rationale,review_status,algorithm_version,created_at,updated_at)
                                   VALUES(?,?,?,?,?,?,'unreviewed',?,?,?)""",
                                (merge_id, left_id, right_id, similarity, json.dumps(shared, ensure_ascii=False),
                                 "同学段、学科、类型和动作一致，且共享概念标签；需审核是否为同一掌握目标。",
                                 self.algorithm_version, now, now),
                            )
                            inserted += result.rowcount
            connection.commit()
        return {"drafts": len(rows), "pairs_compared": compared, "merge_candidates_inserted": inserted,
                "algorithm_version": self.algorithm_version}

    @staticmethod
    def similarity(left: str, right: str, left_tags: Iterable[str] = (), right_tags: Iterable[str] = ()) -> float:
        normalize = lambda value: re.sub(r"[\s，,。；;：:（）()的]", "", value).lower()
        a, b = normalize(left), normalize(right)
        if not a or not b:
            return 0.0
        sequence = SequenceMatcher(None, a, b).ratio()
        trigrams = lambda value: {value[index:index + 3] for index in range(max(1, len(value) - 2))}
        left_grams, right_grams = trigrams(a), trigrams(b)
        jaccard = len(left_grams & right_grams) / max(1, len(left_grams | right_grams))
        containment = min(len(a), len(b)) / max(len(a), len(b)) if a in b or b in a else 0.0
        base = max(sequence * .7 + jaccard * .3, containment)
        left_chars, right_chars = set(a), set(b)
        character_dice = 2 * len(left_chars & right_chars) / max(1, len(left_chars) + len(right_chars))
        left_tag_set, right_tag_set = set(left_tags), set(right_tags)
        if left_tag_set and right_tag_set:
            concept_overlap = len(left_tag_set & right_tag_set) / min(len(left_tag_set), len(right_tag_set))
            base = max(base, concept_overlap * .55 + character_dice * .45)
        return round(base, 3)
