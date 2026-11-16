from __future__ import annotations

import copy
import asyncio
import json
from pathlib import Path

import pytest

from home_study_agent.knowledge_graph.atomic_model_pipeline import (
    AtomicKnowledgeExtractionService,
    AtomicMergeCandidateService,
    AtomicModelProtocol,
    InvalidAtomicModelOutput,
)
from home_study_agent.database import Database
from home_study_agent.knowledge_ingestion.local_corpora import CorpusSpec, LocalCorpusIngestor
from home_study_agent.model_gateway import ModelResult


def _packet() -> dict:
    return {
        "resources": [{
            "resource_id": "resource-1",
            "title": "二次函数的图像和性质",
            "scope": {"grade": "初三", "subject": "数学", "semester": "上学期", "edition": "苏科版"},
            "unit_path": "上册/二次函数",
            "segments": [{
                "segment_id": "segment-1", "ordinal": 0,
                "text": "学习目标：根据二次函数解析式确定抛物线开口方向，并解释它与系数a符号的对应关系。",
            }],
        }],
        "official_standard_evidence": [{
            "source_ref": "math-standard:91", "source_title": "数学课标", "page_number": 91,
            "exact_page_excerpt": "能根据函数表达式画出函数图象，并结合图象分析函数性质。",
        }],
    }


def _valid_output() -> dict:
    return {
        "definitions": [{
            "local_id": "a1", "atom_kind": "concept_discrimination", "action_verb": "判断",
            "knowledge_component": "二次函数解析式中系数a符号对应的抛物线开口方向",
            "boundary": {"context": "给定二次函数解析式", "representation": "symbolic", "exclusions": ["不要求绘制完整图象"]},
            "observable_outcome": "能根据解析式判断抛物线的开口方向",
            "mastery_criteria": {
                "recognition": "能从两个选项中识别系数符号对应的开口方向",
                "independent": "连续独立判断三个不同解析式的开口方向",
                "transfer": "延迟一天后在含参数的解析式中仍能正确判断",
            },
            "diagnostic_blueprint": [
                {"level": "recognition", "task_family": "符号与方向配对", "success_rule": "四项至少答对三项"},
                {"level": "independent", "task_family": "由解析式判开口方向", "success_rule": "三题全部正确且无提示"},
                {"level": "transfer", "task_family": "含参数解析式变式", "success_rule": "说明参数符号条件并判断正确"},
            ],
            "misconceptions": ["把a的绝对值大小误当成开口方向的决定条件"],
            "concept_tags": ["二次函数", "抛物线", "系数a"],
            "evidence": [{
                "resource_id": "resource-1", "segment_id": "segment-1",
                "exact_quote": "根据二次函数解析式确定抛物线开口方向", "evidence_role": "objective",
            }],
            "standard_alignments": [{
                "source_ref": "math-standard:91", "exact_quote": "结合图象分析函数性质",
            }],
            "confidence": 0.91,
        }],
        "relations": [],
    }


def test_valid_model_atom_is_evidence_bound_and_independently_assessable() -> None:
    result = AtomicModelProtocol.parse(json.dumps(_valid_output(), ensure_ascii=False), _packet())
    atom = result.definitions[0]
    assert atom["action_verb"] == "判断"
    assert atom["knowledge_component"].startswith("二次函数解析式")
    assert {item["level"] for item in atom["diagnostic_blueprint"]} == {"recognition", "independent", "transfer"}


def test_atoms_top_level_alias_is_normalized_without_weakening_item_validation() -> None:
    output = _valid_output()
    output["atoms"] = output.pop("definitions")
    result = AtomicModelProtocol.parse(json.dumps(output, ensure_ascii=False), _packet())
    assert result.definitions[0]["local_id"] == "a1"


def test_controlled_action_and_atom_kind_aliases_are_canonicalized() -> None:
    output = _valid_output()
    atom = output["definitions"][0]
    atom["atom_kind"] = "reasoning"
    atom["action_verb"] = "归纳"
    atom["observable_outcome"] = "学生能归纳二次函数图像的开口方向"

    result = AtomicModelProtocol.parse(json.dumps(output, ensure_ascii=False), _packet())

    assert result.definitions[0]["atom_kind"] == "evidence_reasoning"
    assert result.definitions[0]["action_verb"] == "概括"
    assert result.definitions[0]["observable_outcome"] == "学生能概括二次函数图像的开口方向"


def test_topic_label_cannot_be_promoted_as_atomic_component() -> None:
    output = _valid_output()
    output["definitions"][0]["knowledge_component"] = "二次函数"
    with pytest.raises(InvalidAtomicModelOutput, match="不是可测原子项"):
        AtomicModelProtocol.parse(json.dumps(output, ensure_ascii=False), _packet())


def test_one_atom_cannot_hide_multiple_observable_actions() -> None:
    output = _valid_output()
    output["definitions"][0]["observable_outcome"] = "能判断抛物线开口方向并解释判断理由"
    with pytest.raises(InvalidAtomicModelOutput, match="只能使用所声明的一个动作"):
        AtomicModelProtocol.parse(json.dumps(output, ensure_ascii=False), _packet())


@pytest.mark.parametrize("component", [
    "操作系统管理软硬件资源的基本功能",
    "在WPS文字中插入页眉页脚并设置页码的操作流程",
])
def test_action_word_inside_noun_phrase_is_not_misread_as_a_second_action(component: str) -> None:
    output = _valid_output()
    atom = output["definitions"][0]
    atom["atom_kind"] = "concept_identification"
    atom["action_verb"] = "识别"
    atom["knowledge_component"] = component
    atom["observable_outcome"] = f"学生能识别{component}"

    result = AtomicModelProtocol.parse(json.dumps(output, ensure_ascii=False), _packet())

    assert result.definitions[0]["knowledge_component"] == component


def test_transcript_and_standard_quotes_must_be_exact() -> None:
    transcript_forgery = _valid_output()
    transcript_forgery["definitions"][0]["evidence"][0]["exact_quote"] = "转录中不存在的学习目标"
    with pytest.raises(InvalidAtomicModelOutput, match="转录证据无法逐字核对"):
        AtomicModelProtocol.parse(json.dumps(transcript_forgery, ensure_ascii=False), _packet())

    standard_forgery = copy.deepcopy(_valid_output())
    standard_forgery["definitions"][0]["standard_alignments"][0]["exact_quote"] = "课标没有写过这句话"
    result = AtomicModelProtocol.parse(json.dumps(standard_forgery, ensure_ascii=False), _packet())
    assert result.definitions[0]["standard_alignments"] == []


def test_quote_punctuation_is_anchored_back_to_exact_local_source() -> None:
    output = _valid_output()
    output["definitions"][0]["evidence"][0]["exact_quote"] = (
        "根据二次函数解析式，确定抛物线开口方向"
    )
    result = AtomicModelProtocol.parse(json.dumps(output, ensure_ascii=False), _packet())
    quote = result.definitions[0]["evidence"][0]["exact_quote"]
    assert quote == "根据二次函数解析式确定抛物线开口方向"
    assert quote in _packet()["resources"][0]["segments"][0]["text"]


def test_quote_with_one_inserted_phrase_anchors_only_the_long_exact_source_span() -> None:
    source = "右击线条在快捷菜单中选择设置为镜像线命令此时绿色实线变成虚线"
    proposed = "右击线条在快捷菜单中选择设置为镜像线命令，此时绿色轮廓实线变成虚线"

    anchored = AtomicModelProtocol._anchor_quote(proposed, source)

    assert anchored is not None
    assert anchored in source
    assert "轮廓" not in anchored
    assert len(anchored) >= 12


def test_invalid_extra_evidence_is_dropped_when_exact_evidence_remains() -> None:
    output = _valid_output()
    output["definitions"][0]["evidence"].append({
        "resource_id": "resource-1", "segment_id": "segment-1",
        "exact_quote": "模型改写而不是源文本中的连续原句", "evidence_role": "explanation",
    })
    result = AtomicModelProtocol.parse(json.dumps(output, ensure_ascii=False), _packet())
    assert len(result.definitions[0]["evidence"]) == 1


def test_invalid_atom_is_isolated_when_another_evidence_bound_atom_survives() -> None:
    output = _valid_output()
    invalid = copy.deepcopy(output["definitions"][0])
    invalid["local_id"] = "bad"
    invalid["action_verb"] = "观察"
    output["definitions"].insert(0, invalid)

    result = AtomicModelProtocol.parse(json.dumps(output, ensure_ascii=False), _packet())

    assert [item["local_id"] for item in result.definitions] == ["a1"]


def test_invalid_nested_atom_fields_are_isolated_from_valid_siblings() -> None:
    output = _valid_output()
    invalid = copy.deepcopy(output["definitions"][0])
    invalid["local_id"] = "bad-nested"
    invalid["mastery_criteria"]["recognition"] = ""
    output["definitions"].insert(0, invalid)

    result = AtomicModelProtocol.parse(json.dumps(output, ensure_ascii=False), _packet())

    assert [item["local_id"] for item in result.definitions] == ["a1"]


def test_every_resource_in_batch_requires_evidence_mapping() -> None:
    packet = _packet()
    duplicate = copy.deepcopy(packet["resources"][0])
    duplicate["resource_id"] = "resource-2"
    duplicate["segments"][0]["segment_id"] = "segment-2"
    packet["resources"].append(duplicate)
    with pytest.raises(InvalidAtomicModelOutput, match="每个资源"):
        AtomicModelProtocol.parse(json.dumps(_valid_output(), ensure_ascii=False), packet)


def test_prerequisite_relations_must_be_evidence_backed_and_acyclic() -> None:
    output = _valid_output()
    second = copy.deepcopy(output["definitions"][0])
    second.update({
        "local_id": "a2", "atom_kind": "procedure_execution", "action_verb": "确定",
        "knowledge_component": "二次函数解析式对应的抛物线对称轴",
        "observable_outcome": "能根据二次函数解析式确定抛物线对称轴",
    })
    output["definitions"].append(second)
    output["relations"] = [
        {"source_local_id": "a1", "target_local_id": "a2", "relation_type": "prerequisite_of",
         "rationale": "先辨认解析式系数含义，才能独立推导对称轴表达式", "evidence_ref": "segment-1", "confidence": .8},
        {"source_local_id": "a2", "target_local_id": "a1", "relation_type": "prerequisite_of",
         "rationale": "反向关系会制造不成立的循环依赖，必须在入库前拒绝", "evidence_ref": "segment-1", "confidence": .6},
    ]
    with pytest.raises(InvalidAtomicModelOutput, match="形成循环"):
        AtomicModelProtocol.parse(json.dumps(output, ensure_ascii=False), _packet())


def test_paraphrase_similarity_finds_merge_candidates_without_equating_different_skills() -> None:
    same = AtomicMergeCandidateService.similarity(
        "二次函数解析式中系数a符号对应的抛物线开口方向",
        "系数a的符号与二次函数图像开口方向的对应关系",
        {"二次函数", "系数a", "开口方向"}, {"二次函数", "系数a", "开口方向"},
    )
    different = AtomicMergeCandidateService.similarity(
        "二次函数解析式中系数a符号对应的抛物线开口方向",
        "二次函数解析式对应的抛物线对称轴",
        {"二次函数", "系数a", "开口方向"}, {"二次函数", "对称轴"},
    )
    assert same >= .72 and different < same


class _Gateway:
    async def complete(self, request):
        packet = json.loads(request.messages[1]["content"])["input"]
        resource = packet["resources"][0]
        segment = resource["segments"][0]
        output = _valid_output()
        atom = output["definitions"][0]
        atom["evidence"] = [{
            "resource_id": resource["resource_id"], "segment_id": segment["segment_id"],
            "exact_quote": "根据二次函数解析式确定抛物线开口方向", "evidence_role": "objective",
        }]
        atom["standard_alignments"] = []
        return ModelResult(json.dumps(output, ensure_ascii=False), "test", "atomic-test", 3, 100, 200)


class _FailingGateway:
    async def complete(self, request):
        raise InvalidAtomicModelOutput("可核对的失败原因")


class _UnexpectedGateway:
    async def complete(self, request):
        raise AssertionError("非教学转录不应调用模型")


class _SplitGateway:
    async def complete(self, request):
        packet = json.loads(request.messages[1]["content"])["input"]
        resource = packet["resources"][0]
        segment = resource["segments"][0]
        output = _valid_output()
        atom = output["definitions"][0]
        atom["evidence"] = [{
            "resource_id": resource["resource_id"], "segment_id": segment["segment_id"],
            "exact_quote": "根据二次函数解析式确定抛物线开口方向", "evidence_role": "objective",
        }]
        atom["standard_alignments"] = []
        return ModelResult(json.dumps(output, ensure_ascii=False), "test", "split-test", 2, 10, 20)


class _RepairGateway:
    fallback_configured = True

    async def complete_on_fallback(self, request):
        assert request.context_manifest["repair"] is True
        assert "最多输出4个" in request.messages[-1]["content"]
        return ModelResult(
            json.dumps(_valid_output(), ensure_ascii=False),
            "deepseek", "deepseek-v4-flash", 4, 30, 40, True,
        )


def test_durable_job_stores_unreviewed_draft_and_exact_evidence_once(tmp_path: Path) -> None:
    database = Database(tmp_path / "study.db"); database.initialize()
    transcripts = tmp_path / "transcripts"; transcripts.mkdir()
    (transcripts / "lesson.txt").write_text(
        "学习目标：根据二次函数解析式确定抛物线开口方向，并解释它与系数a符号的对应关系。",
        encoding="utf-8",
    )
    spec = CorpusSpec("sample", "样本", "seyk", transcripts, None, "local", "local_only")
    ingestor = LocalCorpusIngestor(database)
    import_job = ingestor.start(spec, idempotency_key="sample-import")
    assert ingestor.run(spec, import_job)["status"] == "completed"
    service = AtomicKnowledgeExtractionService(database, _Gateway())
    job = service.create_job(idempotency_key="atomic-job", grades=[""], subjects=[""])
    completed = asyncio.run(service.run(job["job_id"]))
    assert completed["status"] == "completed" and completed["draft_count"] == 1
    assert asyncio.run(service.run(job["job_id"]))["draft_count"] == 1
    with Database(tmp_path / "study.db").connect() as connection:
        draft = connection.execute("SELECT review_status,source_method FROM kg_atomic_definition_draft").fetchone()
        evidence = connection.execute("SELECT exact_quote FROM kg_atomic_draft_evidence").fetchall()
        provenance = connection.execute("SELECT job_id,batch_id FROM kg_atomic_draft_batch").fetchall()
    assert tuple(draft) == ("unreviewed", "model")
    assert [row["exact_quote"] for row in evidence] == ["根据二次函数解析式确定抛物线开口方向"]
    assert len(provenance) == 1 and provenance[0]["job_id"] == job["job_id"]


def test_failed_batch_persists_bounded_error_detail(tmp_path: Path) -> None:
    database = Database(tmp_path / "study.db"); database.initialize()
    transcripts = tmp_path / "transcripts"; transcripts.mkdir()
    (transcripts / "lesson.txt").write_text(
        "学习目标：根据二次函数解析式确定抛物线开口方向。", encoding="utf-8",
    )
    spec = CorpusSpec("sample", "样本", "seyk", transcripts, None, "local", "local_only")
    ingestor = LocalCorpusIngestor(database)
    import_job = ingestor.start(spec, idempotency_key="sample-import")
    assert ingestor.run(spec, import_job)["status"] == "completed"
    service = AtomicKnowledgeExtractionService(database, _FailingGateway())
    job = service.create_job(idempotency_key="failing-atomic-job", grades=[""], subjects=[""])
    result = asyncio.run(service.run(job["job_id"], stop_after=1))

    assert result["failed_batches"] == 1
    with database.connect() as connection:
        batch = connection.execute(
            "SELECT error_code,error_detail FROM kg_atomic_extraction_batch WHERE job_id=?",
            (job["job_id"],),
        ).fetchone()
    assert tuple(batch) == ("InvalidAtomicModelOutput", "可核对的失败原因")


def test_terminal_batch_failure_creates_durable_resource_gap(tmp_path: Path) -> None:
    database = Database(tmp_path / "study.db"); database.initialize()
    transcripts = tmp_path / "transcripts"; transcripts.mkdir()
    (transcripts / "lesson.txt").write_text(
        "学习目标：根据二次函数解析式确定抛物线开口方向。", encoding="utf-8",
    )
    spec = CorpusSpec("terminal", "终态失败样本", "seyk", transcripts, None, "local", "local_only")
    ingestor = LocalCorpusIngestor(database)
    import_job = ingestor.start(spec, idempotency_key="terminal-import")
    assert ingestor.run(spec, import_job)["status"] == "completed"
    service = AtomicKnowledgeExtractionService(database, _FailingGateway())
    job = service.create_job(idempotency_key="terminal-atomic-job", grades=[""], subjects=[""])

    result = asyncio.run(service.run(job["job_id"]))

    assert result["status"] == "failed" and result["failed_batches"] == 1
    with database.connect() as connection:
        gap = connection.execute(
            """SELECT status,reason_code,attempt_count,error_code
               FROM kg_atomic_resource_gap WHERE job_id=?""",
            (job["job_id"],),
        ).fetchone()
    assert tuple(gap) == (
        "open", "terminal_model_validation_failure", 3, "InvalidAtomicModelOutput",
    )

    with database.connect() as connection:
        connection.execute(
            """UPDATE kg_atomic_resource_gap SET status='resolved',
               reason_code='specialized_extraction_required',resolution_ref='lexeme-job:test',
               resolved_at='2026-08-29T00:00:00+00:00' WHERE job_id=?""",
            (job["job_id"],),
        )
        connection.commit()
    service._sync_terminal_gaps(job["job_id"])
    with database.connect() as connection:
        preserved = connection.execute(
            "SELECT status,reason_code,resolution_ref,resolved_at FROM kg_atomic_resource_gap WHERE job_id=?",
            (job["job_id"],),
        ).fetchone()
    assert tuple(preserved) == (
        "resolved", "specialized_extraction_required", "lexeme-job:test", "2026-08-29T00:00:00+00:00",
    )


def test_repeated_subtitle_watermark_is_completed_as_auditable_noninstructional_resource(tmp_path: Path) -> None:
    database = Database(tmp_path / "study.db"); database.initialize()
    transcripts = tmp_path / "transcripts"; transcripts.mkdir()
    (transcripts / "watermark.txt").write_text("字幕志愿者 李宗盛 " * 40, encoding="utf-8")
    spec = CorpusSpec("noise", "噪声样本", "seyk", transcripts, None, "local", "local_only")
    ingestor = LocalCorpusIngestor(database)
    import_job = ingestor.start(spec, idempotency_key="noise-import")
    assert ingestor.run(spec, import_job)["status"] == "completed"
    service = AtomicKnowledgeExtractionService(database, _UnexpectedGateway())
    job = service.create_job(idempotency_key="noise-atomic-job", grades=[""], subjects=[""])

    completed = asyncio.run(service.run(job["job_id"]))

    assert completed["status"] == "completed" and completed["draft_count"] == 0
    with database.connect() as connection:
        disposition = connection.execute(
            "SELECT disposition,reason_code,detector_version FROM kg_atomic_resource_disposition WHERE job_id=?",
            (job["job_id"],),
        ).fetchone()
    assert tuple(disposition) == ("noninstructional", "repeated_transcript_noise", "local-content-v1")


def test_repeated_instructional_examples_are_not_mislabeled_as_transcript_noise() -> None:
    resource = copy.deepcopy(_packet()["resources"][0])
    resource["segments"][0]["text"] = (
        "学习目标：理解二次函数开口方向。"
        "例题一：系数a大于零时开口向上。"
        "例题二：系数a小于零时开口向下。"
        "请独立判断三个不同解析式的开口方向。" * 4
    )

    assert AtomicKnowledgeExtractionService._noninstructional_reason(resource) is None


def test_repeated_channel_watermark_wins_over_one_trailing_class_marker() -> None:
    resource = copy.deepcopy(_packet()["resources"][0])
    resource["segments"][0]["text"] = "明镜与点点栏目" * 35 + "这节课就上到这里同学们再见"

    assert (
        AtomicKnowledgeExtractionService._noninstructional_reason(resource)
        == "repeated_transcript_noise"
    )


def test_short_teaching_excerpt_before_dominant_watermark_is_not_discarded() -> None:
    resource = copy.deepcopy(_packet()["resources"][0])
    resource["segments"][0]["text"] = (
        "一 热身：运球过程中踢球穿单练习，踢球时注意控制力量。"
        + "字幕志愿者 李宗盛" * 45
    )

    assert AtomicKnowledgeExtractionService._noninstructional_reason(resource) is None


def test_partition_drops_pure_repetition_segment_but_keeps_useful_sibling() -> None:
    packet = _packet()
    packet["resources"][0]["segments"] = [
        {"segment_id": "noise", "ordinal": 0, "text": "mieux " * 180},
        {
            "segment_id": "teaching", "ordinal": 1,
            "text": "Learning goal: use will to introduce the weather and cities in Australia.",
        },
    ]

    filtered, dispositions = AtomicKnowledgeExtractionService._partition_packet(packet)

    assert dispositions == []
    assert [item["segment_id"] for item in filtered["resources"][0]["segments"]] == ["teaching"]


def test_two_resource_repair_extracts_each_resource_with_exact_evidence(tmp_path: Path) -> None:
    database = Database(tmp_path / "study.db"); database.initialize()
    service = AtomicKnowledgeExtractionService(database, _SplitGateway())
    packet = _packet()
    second = copy.deepcopy(packet["resources"][0])
    second["resource_id"] = "resource-2"
    second["segments"][0]["segment_id"] = "segment-2"
    packet["resources"].append(second)
    original = ModelResult("rejected-combined-output", "test", "split-test", 1, 5, 5)

    validated, aggregate = asyncio.run(service._extract_split_batch("job", "batch", packet, original))

    assert {item["evidence"][0]["resource_id"] for item in validated.definitions} == {
        "resource-1", "resource-2",
    }
    assert len({item["local_id"] for item in validated.definitions}) == 2
    assert aggregate.prompt_tokens == 25
    assert aggregate.completion_tokens == 45


def test_single_resource_semantic_failure_is_repaired_by_configured_fallback(tmp_path: Path) -> None:
    database = Database(tmp_path / "study.db"); database.initialize()
    service = AtomicKnowledgeExtractionService(database, _RepairGateway())
    original = ModelResult("rejected", "deepseek", "deepseek-v4-pro", 3, 11, 12)

    validated, aggregate = asyncio.run(service._repair_single_resource(
        "job", "batch", _packet(), original,
        InvalidAtomicModelOutput("转录证据无法逐字核对"),
    ))

    assert validated.definitions[0]["local_id"] == "a1"
    assert aggregate.model == "deepseek-v4-flash"
    assert aggregate.fallback_used is True
    assert aggregate.prompt_tokens == 41 and aggregate.completion_tokens == 52
