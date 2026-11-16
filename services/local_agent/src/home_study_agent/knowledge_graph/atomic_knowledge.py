"""Evidence-first atomic learning objectives.

Topics organise a curriculum.  Atoms are smaller: one observable student action,
one finite object, an assessment condition, and an explicit success criterion.
Candidates stay outside the mastery graph until a reviewer confirms them.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Iterable
from uuid import uuid4

from ..database import Database
from ..repository import utc_now
from .schema import ensure_schema


GRANULARITY_VERSION = "atomic-objective-v2"

ACTION_RULES: tuple[tuple[str, str, str], ...] = (
    ("概括", "概括", "evidence_reasoning"), ("归纳", "归纳", "evidence_reasoning"),
    ("推断", "推断", "evidence_reasoning"), ("评价", "评价", "evidence_reasoning"),
    ("证明", "论证", "evidence_reasoning"), ("解释", "解释", "evidence_reasoning"),
    ("说明", "解释", "evidence_reasoning"), ("理解", "解释", "evidence_reasoning"),
    ("辨别", "判断", "concept_discrimination"), ("区分", "判断", "concept_discrimination"),
    ("判断", "判断", "concept_discrimination"), ("确定", "确定", "procedure_execution"),
    ("识别", "识别", "concept_identification"),
    ("指出", "识别", "concept_identification"), ("认识", "识别", "concept_identification"),
    ("了解", "识别", "fact_recall"), ("知道", "识别", "fact_recall"),
    ("绘制", "表示", "representation_translation"), ("画出", "表示", "representation_translation"),
    ("标注", "表示", "representation_translation"), ("表示", "表示", "representation_translation"),
    ("转换", "转换", "representation_translation"), ("翻译", "翻译", "language_meaning"),
    ("朗读", "朗读", "language_form"), ("复述", "复述", "language_use"),
    ("阅读", "阅读", "language_meaning"), ("写作", "写作", "language_use"),
    ("写出", "写出", "language_use"), ("说出", "说出", "language_use"),
    ("运用", "运用", "strategy_selection"), ("应用", "运用", "strategy_selection"),
    ("解决", "运用", "strategy_selection"), ("选择", "选择", "strategy_selection"),
    ("结合", "运用", "strategy_selection"), ("计算", "计算", "procedure_execution"),
    ("测量", "测量", "procedure_execution"), ("配平", "配平", "procedure_execution"),
    ("书写", "书写", "procedure_execution"), ("操作", "操作", "procedure_execution"),
    ("完成", "执行", "procedure_execution"), ("掌握", "执行", "procedure_execution"),
)

ACTION_PATTERN = re.compile("|".join(re.escape(item[0]) for item in ACTION_RULES))
ACTION_LOOKUP = {surface:(canonical,kind) for surface,canonical,kind in ACTION_RULES}
OBJECTIVE_MARKER = re.compile(r"(?:本课|本节课)?(?:学习|教学)目标|目标要求")
OBJECTIVE_BOUNDARY = re.compile(r"教学重难点|学习重难点|教学重点|学习重点|重点难点|本节课的?重点|课前准备|下面|接下来|首先让我们|今天我们")
TASK_MARKER = re.compile(r"学习任务|自学要求|课堂任务|请(?:同学们|大家|你)?")
SUMMARY_MARKER = re.compile(r"(?:本节课|这节课|通过(?:本节课|这节课|以上)的?学习).{0,36}?(?:学会|掌握|理解|知道|认识|能够|能)")
ENUMERATION = re.compile(r"(?:^|[。；;])\s*(?:[一二三四五六七八九十]|[1-9])[、.．)]\s*(?:认知目标|知识目标|技能目标|能力目标)?[：:]?")
SPACE = re.compile(r"\s+")

NON_MASTERY_OBJECTIVES = ("情感目标", "情感态度", "培养学生", "增强自信", "合作意识", "竞争意识", "学习兴趣")
UMBRELLA_OBJECTS = {
    "数学", "语文", "英语", "物理", "化学", "阅读理解", "写作", "二次函数", "一次函数",
    "圆", "概率", "统计", "有理数", "分式", "三角形", "新航路", "体育锻炼",
}
TITLE_OPERATIONS = (
    "解决问题", "计算", "估算", "化简", "求值", "配平", "测量", "作图", "画图", "判断",
    "判定", "比较", "辨析", "概括", "分析", "证明", "翻译", "朗读", "复述", "书写", "改写",
)
DISCOURSE_BOUNDARY = re.compile(r"同学们|我们(?:来|一起|发现|学习|知道)|老师|接下来|首先|然后|那么|好的|今天|本节课|这节课|下面")
GENERIC_OBJECT = re.compile(r"^(?:一下|下列|以下|这些|这个|本节课所学|课本|材料|方案|练习|问题|任务|内容|知识|图如右图所示|表格|评价标准|自己的作品)$")
ASSESSABLE_MARKERS = (
    "原因","条件","关系","规律","性质","方法","步骤","依据","作用","意义","主旨","结构","形象",
    "观点","论据","论证","表达效果","方程","函数","概率","反应","图像","数据","角","数","式",
    "词","句","段","文章","实验","现象","结论","路线","时间","位置","语法","发音","含义","方向",
    "节奏","动作","操作","计算","写作","阅读","翻译","朗读","复述","比较","分类","特征","要点",
)


@dataclass(frozen=True)
class AtomicCandidate:
    title: str
    action_verb: str
    object_text: str
    atom_kind: str
    condition_text: str
    representation: str
    mastery_criterion: str
    evidence_text: str
    evidence_start: int | None
    evidence_end: int | None
    scope: dict
    unit_path: str
    source_kind: str
    confidence: float
    canonical_signature: str
    atomicity: dict


class AtomicObjectiveExtractor:
    """Extract high-recall candidates without pretending they are approved atoms."""

    def extract(self, *, title: str, transcript: str, scope: dict, unit_path: str = "") -> list[AtomicCandidate]:
        candidates: list[AtomicCandidate] = []
        for text,start in self._objective_clauses(transcript):
            candidates.extend(self._from_clause(text,start,scope,unit_path,"explicit_objective",.80))
        for text,start in self._assessment_clauses(transcript):
            candidates.extend(self._from_clause(text,start,scope,unit_path,"assessment_prompt",.67))
        for text,start in self._summary_clauses(transcript):
            candidates.extend(self._from_clause(text,start,scope,unit_path,"lesson_summary",.72))
        if not candidates:
            candidates.extend(self._from_title(title,scope,unit_path))
        unique: dict[str,AtomicCandidate] = {}
        for candidate in candidates:
            unique.setdefault(candidate.canonical_signature,candidate)
        return list(unique.values())

    def _objective_clauses(self, transcript: str) -> list[tuple[str,int]]:
        clauses: list[tuple[str,int]]=[]
        for marker in OBJECTIVE_MARKER.finditer(transcript[:16000]):
            start=marker.end();window=transcript[start:start+900]
            boundary=OBJECTIVE_BOUNDARY.search(window)
            if boundary: window=window[:boundary.start()]
            normalized=ENUMERATION.sub("\n",window.replace("：","。").replace(":","。"))
            cursor=start
            for raw in re.split(r"[\n。；;]+",normalized):
                clause=self._clean_clause(raw)
                if not clause: continue
                if len(clause)>180:continue
                found=transcript.find(clause,cursor,min(len(transcript),start+900))
                if found < 0: found=start
                clauses.append((clause,found));cursor=max(cursor,found+len(clause))
            break
        return clauses

    def _assessment_clauses(self,transcript:str)->list[tuple[str,int]]:
        clauses=[]
        for marker in TASK_MARKER.finditer(transcript):
            raw=transcript[marker.end():marker.end()+140]
            raw=re.split(r"[。！？!?；;]",raw,1)[0]
            raw=DISCOURSE_BOUNDARY.split(raw,1)[0]
            clause=self._clean_clause(raw)
            action=ACTION_PATTERN.search(clause) if clause else None
            if action and action.start()<=55 and len(clause)<=120:clauses.append((clause,marker.start()))
            if len(clauses)>=24:break
        return clauses

    def _summary_clauses(self,transcript:str)->list[tuple[str,int]]:
        clauses=[]
        # Summaries usually occur late, but ASR files are not guaranteed to have
        # reliable timestamps or paragraph breaks.  Search the whole transcript
        # and keep only short, action-bearing claims.
        for marker in SUMMARY_MARKER.finditer(transcript):
            start=marker.start();raw=transcript[start:start+180]
            sentence_end=re.search(r"[。！？!?；;]",raw)
            if not sentence_end or sentence_end.start()>140:continue
            raw=raw[:sentence_end.start()]
            clause=self._clean_clause(raw)
            if clause and ACTION_PATTERN.search(clause):clauses.append((clause,start))
            if len(clauses)>=8:break
        return clauses

    @staticmethod
    def _clean_clause(text: str) -> str:
        text=SPACE.sub("",text).strip("，,、：:。；;0123456789一二三四五六七八九十")
        text=re.sub(r"^(?:认知|知识|技能|能力)?目标", "", text)
        text=re.sub(r"^(?:通过(?:本节课|本课|学习|练习|活动)[^，,。；;]{0,24}?)(?:学生|同学们)?", "", text)
        text=re.sub(r"^(?:学生|同学们)?(?:能够|能|可以|会|要)", "", text)
        if len(text)<4 or any(item in text for item in NON_MASTERY_OBJECTIVES): return ""
        return text[:260]

    def _from_clause(self,text:str,start:int,scope:dict,unit_path:str,source_kind:str,confidence:float)->list[AtomicCandidate]:
        matches=list(ACTION_PATTERN.finditer(text))
        if not matches:return []
        candidates=[]
        for index,match in enumerate(matches[:4]):
            end=matches[index+1].start() if index+1<len(matches) else len(text)
            raw_object=text[match.end():end]
            if not raw_object.strip() and text[:match.start()].startswith("把"):
                raw_object=text[1:match.start()]
            raw_object=re.sub(r"^(?:并|且|能|能够|会|正确地?|准确地?|独立地?)", "", raw_object)
            condition="" if text[:match.start()].startswith("把") else self._condition(text[:match.start()])
            for object_text in self._split_objects(raw_object):
                if source_kind=="assessment_prompt" and not any(marker in object_text for marker in ASSESSABLE_MARKERS):
                    continue
                candidate=self._candidate(match.group(),object_text,condition,text,start,scope,unit_path,source_kind,confidence)
                if candidate:candidates.append(candidate)
        return candidates

    def _from_title(self,title:str,scope:dict,unit_path:str)->list[AtomicCandidate]:
        clean=re.sub(r"^(?:第?\d+(?:\.\d+)?\s*)", "", title)
        clean=re.sub(r"(?:第[一二三四五六七八九十\d]+课时|[（(]\d+[）)]|小结与思考|单元复习)$", "", clean).strip(" -—·")
        if clean in UMBRELLA_OBJECTS:return []
        action_match=ACTION_PATTERN.search(clean)
        if action_match:
            return self._from_clause(clean,0,scope,unit_path,"lesson_title",.58)
        operation_match=re.search(r"(?:的)?(计算|估算|化简|求值|配平|测量|作图|画图|判断|判定|比较|辨析|概括|分析|证明|翻译|朗读|复述|书写|改写)$",clean)
        operation="解决问题" if re.match(r"^用.+解决问题",clean) else operation_match.group(1) if operation_match else None
        if not operation:return []
        surface="计算" if operation in {"计算","估算","化简","求值"} else "判断" if operation in {"判断","判定","比较","辨析"} else "运用" if operation=="解决问题" else operation
        object_text=clean
        candidate=self._candidate(surface,object_text,"",title,0,scope,unit_path,"lesson_title",.55)
        return [candidate] if candidate else []

    @staticmethod
    def _split_objects(text:str)->list[str]:
        text=text.strip("，,。；;：:")
        parts=re.split(r"以及|并且|；|;",text)
        cleaned=[]
        for part in parts:
            value=re.sub(r"^(?:能|能够|会)", "", part).strip("，,。；;：:")
            if 2<=len(value)<=100:cleaned.append(value)
        return cleaned[:5]

    @staticmethod
    def _condition(prefix:str)->str:
        prefix=re.sub(r"^(?:通过(?:学习|练习|活动)|学生|同学们|能|能够|会)", "", prefix)
        match=re.search(r"((?:根据|借助|结合|在).{2,40})$",prefix)
        return match.group(1) if match else ""

    def _candidate(self,surface_action:str,object_text:str,condition:str,evidence:str,start:int,scope:dict,
                   unit_path:str,source_kind:str,confidence:float)->AtomicCandidate|None:
        object_text=self._normalize_object(object_text)
        if not object_text or object_text in UMBRELLA_OBJECTS:return None
        canonical,kind=ACTION_LOOKUP.get(surface_action,(surface_action,"procedure_execution"))
        subject=str(scope.get("subject") or "")
        if subject in {"体育与健康","体育"} and canonical in {"执行","运用","操作"}:kind="motor_skill"
        if canonical in {"朗读","复述","写作","写出","说出"} and kind not in {"language_form","language_use"}:kind="language_use"
        representation=self._representation(object_text+condition)
        criterion=self._criterion(kind,canonical,object_text)
        signature=self._signature(scope,kind,canonical,object_text,condition,representation)
        atomicity={
            "is_observable":canonical not in {"了解","理解","掌握"},
            "single_action":True,
            "finite_object":2<=len(object_text)<=100,
            "has_mastery_criterion":bool(criterion),
            "topic_only":object_text in UMBRELLA_OBJECTS,
            "version":GRANULARITY_VERSION,
        }
        if not all((atomicity["is_observable"],atomicity["single_action"],atomicity["finite_object"],atomicity["has_mastery_criterion"])):return None
        title=f"{canonical}{object_text}"
        return AtomicCandidate(title,canonical,object_text,kind,condition,representation,criterion,evidence[:260],start,
                               start+len(evidence) if start is not None else None,dict(scope),unit_path,source_kind,
                               confidence,signature,atomicity)

    @staticmethod
    def _normalize_object(text:str)->str:
        text=SPACE.sub("",text)
        text=DISCOURSE_BOUNDARY.split(text,1)[0]
        text=re.split(r"(?:根据|为了|为了加深|请大家|请你|可求出|答案是)",text,1)[0]
        text=re.sub(r"^(?:对|关于)", "", text)
        text=re.sub(r"^(?:请|尝试|试着|快速地?|再|来|把|对|出|一下|下列|以下|这些|这个)+", "", text)
        text=re.sub(r"(?:呢|吗|吧|一下|下列|以下)$", "", text)
        text=re.sub(r"(?:的知识|的基本知识|的概念)$", "", text)
        text=text.strip("，,。；;：:")
        if GENERIC_OBJECT.fullmatch(text):return ""
        return text[:100]

    @staticmethod
    def _representation(text:str)->str:
        for token,label in (("图像","graph"),("表格","table"),("地图","map"),("思维导图","concept_map"),
                            ("实验","experiment"),("解析式","symbolic"),("文本","text"),("口语","oral"),
                            ("情境","context"),("材料","source_material")):
            if token in text:return label
        return "unspecified"

    @staticmethod
    def _criterion(kind:str,action:str,obj:str)->str:
        if kind=="fact_recall":return f"在即时与延迟提取中，独立识别或说出“{obj}”的关键要点。"
        if kind=="concept_discrimination":return f"面对至少3个变式，独立判断“{obj}”并说明判据，至少2个正确。"
        if kind=="procedure_execution":return f"在两道条件不同的任务中独立完成“{obj}”，关键步骤完整且结果可检验。"
        if kind=="representation_translation":return f"能在两种表征之间独立完成“{obj}”，信息无关键遗漏。"
        if kind=="evidence_reasoning":return f"能基于给定证据独立{action}“{obj}”，结论、依据和对象一致。"
        if kind=="strategy_selection":return f"在新情境中独立选择并运用“{obj}”，并能说明选择理由。"
        if kind.startswith("language_"):return f"在不同语境中独立完成“{obj}”，核心意义或交际任务保持正确。"
        if kind=="motor_skill":return f"在安全条件下连续完成“{obj}”，关键动作达到教师审核标准。"
        return f"通过至少两次独立表现验证“{obj}”，其中一次为变式或延迟任务。"

    @staticmethod
    def _signature(scope:dict,kind:str,action:str,obj:str,condition:str,representation:str)->str:
        normalize=lambda value:re.sub(r"[\s的，,。；;：:()（）]", "", str(value or "")).lower()
        parts=(normalize(scope.get("grade")),normalize(scope.get("subject")),kind,normalize(action),
               normalize(obj),normalize(condition),representation)
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


class AtomicCandidateStore:
    version=GRANULARITY_VERSION

    def __init__(self,database:Database,extractor:AtomicObjectiveExtractor|None=None):
        self.database=database;self.extractor=extractor or AtomicObjectiveExtractor()

    def extract_pending(self,*,limit:int|None=None)->dict:
        now=utc_now();processed=inserted=0;signatures:set[str]=set()
        with self.database.connect() as connection:
            ensure_schema(connection,now);connection.commit()
            resources=connection.execute("SELECT resource_id,title,grade_scope,subject_scope,semester_scope,metadata_json,source_ref FROM kg_resource ORDER BY resource_id"+(" LIMIT ?" if limit else ""),((limit,) if limit else ())).fetchall()
        for resource in resources:
            metadata=json.loads(resource["metadata_json"] or "{}")
            scope={"grade":resource["grade_scope"],"subject":resource["subject_scope"],"semester":resource["semester_scope"],
                   "edition":metadata.get("edition")}
            unit_path=str(metadata.get("unit_path") or metadata.get("chapter_title") or "")
            with self.database.connect() as connection:
                occurrences=connection.execute("""SELECT s.segment_id,s.text,o.start_char,o.end_char
                    FROM kg_segment_occurrence o JOIN kg_segment s ON s.segment_id=o.segment_id
                    WHERE o.resource_id=? ORDER BY o.ordinal""",(resource["resource_id"],)).fetchall()
            transcript="。".join(row["text"] for row in occurrences)
            candidates=self.extractor.extract(title=resource["title"],transcript=transcript,scope=scope,unit_path=unit_path)
            with self.database.connect() as connection:
                connection.execute("DELETE FROM kg_atomic_candidate WHERE resource_id=? AND status='pending' AND version_label=?",
                                   (resource["resource_id"],self.version))
                for candidate in candidates:
                    segment_id=self._segment_for_evidence(occurrences,candidate.evidence_start)
                    connection.execute("""INSERT INTO kg_atomic_candidate(candidate_id,resource_id,segment_id,canonical_signature,
                        title,atom_kind,action_verb,object_text,condition_text,representation,mastery_criterion,evidence_text,
                        evidence_start,evidence_end,scope_json,unit_path,source_kind,source_ref,confidence,atomicity_json,status,
                        version_label,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (str(uuid4()),resource["resource_id"],segment_id,candidate.canonical_signature,candidate.title,
                         candidate.atom_kind,candidate.action_verb,candidate.object_text,candidate.condition_text,
                         candidate.representation,candidate.mastery_criterion,candidate.evidence_text,candidate.evidence_start,
                         candidate.evidence_end,json.dumps(candidate.scope,ensure_ascii=False),candidate.unit_path,
                         candidate.source_kind,resource["source_ref"],candidate.confidence,
                         json.dumps(candidate.atomicity,ensure_ascii=False),"pending",self.version,now,now))
                    inserted+=1;signatures.add(candidate.canonical_signature)
                connection.commit()
            processed+=1
        return {"version":self.version,"resources_processed":processed,"candidate_evidence_rows":inserted,
                "unique_candidate_signatures":len(signatures)}

    @staticmethod
    def _segment_for_evidence(rows:Iterable,evidence_start:int|None)->str|None:
        rows=list(rows)
        if not rows:return None
        if evidence_start is None:return rows[0]["segment_id"]
        for row in rows:
            if row["start_char"]<=evidence_start<row["end_char"]:return row["segment_id"]
        return rows[0]["segment_id"]


def validate_prerequisite_acyclic(edges:Iterable[tuple[str,str]])->bool:
    adjacency:dict[str,set[str]]={}
    for source,target in edges:
        if source==target:raise ValueError("prerequisite self edge")
        adjacency.setdefault(source,set()).add(target);adjacency.setdefault(target,set())
    visiting:set[str]=set();visited:set[str]=set()
    def visit(node:str)->None:
        if node in visiting:raise ValueError("prerequisite cycle")
        if node in visited:return
        visiting.add(node)
        for target in adjacency[node]:visit(target)
        visiting.remove(node);visited.add(node)
    for node in adjacency:visit(node)
    return True
