"""Non-seed original listening sources use the real author/review/publication chain."""

import json

import pytest

from home_study_agent.curriculum_supply import CurriculumSupplyService, CurriculumSupplyError
from home_study_agent.database import Database
from home_study_agent.knowledge_base import KnowledgeBaseService
from home_study_agent.learning_state import LearningStateService
from home_study_agent.local_supply import LocalSupplyError, validate_item
from home_study_agent.repository import StudyRepository
from home_study_agent.pet_reviewed_source import VALIDATOR, listening_exact_rubric
from test_pet_reviewed_source_contract import packet


CHILD = "child-primary-4"


def prepared(tmp_path, source=None):
    source = source or packet()
    db = Database(tmp_path / "reviewed-listening.db")
    db.initialize()
    StudyRepository(db).seed_family()
    LearningStateService(db).seed_catalog()
    imported = KnowledgeBaseService(db).import_source(CHILD, {
        "source_type": "school_material", "title": "Isolated authored listening practice, not official PET audio",
        "subject": "英语", "grade_band": "四年级", "version_label": "isolated-2026",
        "source_ref": "isolated-fixture:authored-original", "license_status": "public",
        "parent_reviewed": True, "content_text": json.dumps(source, ensure_ascii=False),
    })
    standard_root = tmp_path.resolve(strict=True) / "listening-standards"
    standard_root.mkdir()
    (standard_root / "manifest.json").write_text(json.dumps({
        "format": "hsc_official_curriculum_sources_v1",
        "sources": [{"source_key": "isolated-listening", "pdf_filename": None, "pages_filename": None}],
    }), encoding="utf-8")
    with db.connect() as c:
        excerpt = c.execute("SELECT excerpt_id FROM knowledge_excerpt WHERE source_id=?", (imported["source_id"],)).fetchone()[0]
        c.execute("""INSERT INTO curriculum_standard_source(
            source_id,source_key,jurisdiction,source_type,subject_title,title,version_label,
            official_url,local_root_path,status,imported_at,updated_at)
            VALUES('pet-std','isolated-listening','CN','national_standard','英语','Isolated scope','2022',
            'isolated-fixture:standard',?,'structured','2026-09-01','2026-09-01')""", (str(standard_root),))
        quote = "听懂简短通知并提取明确的地点信息。"
        c.execute("INSERT INTO curriculum_standard_page VALUES('pet-std',12,?,'fixture-hash',20)", (quote,))
        c.execute("""INSERT INTO curriculum_standard_requirement(
            requirement_id,source_id,subject_title,stage_scope,requirement_type,title,
            requirement_text,observable_outcome,page_start,page_end,confidence,maturity,created_at,updated_at)
            VALUES('pet-req','pet-std','英语','第二学段','content_requirement','Listening detail',
            ?,'Exact place',12,12,.9,'verified','2026-09-01','2026-09-01')""", (quote,))
        c.commit()
    service = CurriculumSupplyService(db)
    service.select_curriculum(CHILD, {"grade": "四年级", "subject": "英语", "edition": "isolated-2026",
        "semester": "上学期", "standard_scope": "第二学段", "textbook_source_id": imported["source_id"]},
        actor="parent", action_id="pet-scope", expected_version=0)
    spec = {"seed_item_id": "diag-pet-listening-1", "stage": "parallel", "excerpt_id": excerpt,
        "requirement_id": "pet-req", "standard_page": 12, "standard_quote": quote, "validator": VALIDATOR,
        "explanation": "The original message specifies the complete meeting place.", "difficulty": 1}
    return db, service, spec, source


def publish(service, spec):
    result = service.generate_draft(CHILD, spec, author="local:reviewed_source_extractor_v1",
        action_id="pet-source-draft", expected_scope_version=1)
    assert result["state"] == "draft", result
    cid = result["candidate_id"]
    result = service.validate(CHILD, cid, actor="parent", action_id="pet-check", expected_version=1)
    assert result["state"] == "validated", result
    result = service.review(CHILD, cid, actor="parent", decision="approve", note="Checked original instruction, complete answer and audio text",
        action_id="pet-review", expected_version=2)
    assert result["state"] == "reviewed", result
    return service.publish(CHILD, cid, actor="parent", action_id="pet-publish", expected_version=3)


@pytest.mark.parametrize("count", [1, 3])
def test_nonseed_source_survives_real_review_publish_restart_and_withdraw(tmp_path, count):
    db, service, spec, source = prepared(tmp_path, packet(required_play_count=count,
        instruction="Listen once." if count == 1 else "Listen three times."))
    result = publish(service, spec)
    item_id = result["diagnostic_item_id"]
    assert item_id not in {"diag-pet-listening-1", "diag-pet-listening-2"}
    assert result["eligible"] is True
    with db.connect() as c:
        item = validate_item(c, CHILD, item_id)
        assert item["answer_type"] == "listening_text"
        assert item["prompt"] == source["prompt"] and item["stimulus_text"] == source["stimulus_text"]
        assert json.loads(item["expected_answer_json"]) == listening_exact_rubric(source)
        assert c.execute("SELECT COUNT(*) FROM diagnostic_attempt").fetchone()[0] == 0
    restarted = CurriculumSupplyService(Database(db.path))
    assert restarted.get(CHILD, result["candidate_id"])["eligible"] is True
    before = len(restarted.get(CHILD, result["candidate_id"])["events"])
    replay = restarted.publish(CHILD, result["candidate_id"], actor="parent", action_id="pet-publish", expected_version=3)
    assert replay["diagnostic_item_id"] == item_id and len(replay["events"]) == before
    restarted.withdraw(CHILD, result["candidate_id"], actor="parent", note="Original source withdrawn",
                       action_id="pet-withdraw", expected_version=4)
    with db.connect() as c, pytest.raises(LocalSupplyError):
        validate_item(c, CHILD, item_id)


def test_same_author_cannot_validate_or_skip_review(tmp_path):
    _, service, spec, _ = prepared(tmp_path)
    draft = service.generate_draft(CHILD, spec, author="author", action_id="draft", expected_scope_version=1)
    with pytest.raises(CurriculumSupplyError, match="INDEPENDENT"):
        service.validate(CHILD, draft["candidate_id"], actor="author", action_id="self", expected_version=1)
    with pytest.raises(CurriculumSupplyError, match="STATE"):
        service.publish(CHILD, draft["candidate_id"], actor="parent", action_id="skip", expected_version=1)


@pytest.mark.parametrize("fault", ["source_text", "source_review", "source_expired", "source_child", "node", "item_audio", "item_answer", "standard"])
def test_current_original_source_and_review_guards_are_not_label_permissions(tmp_path, fault):
    db, service, spec, _ = prepared(tmp_path)
    result = publish(service, spec)
    with db.connect() as c:
        source_id = c.execute("SELECT source_id FROM knowledge_excerpt WHERE excerpt_id=?", (spec["excerpt_id"],)).fetchone()[0]
        if fault == "source_text":
            c.execute("UPDATE knowledge_excerpt SET content_text=replace(content_text,'red bridge','blue bridge') WHERE excerpt_id=?", (spec["excerpt_id"],))
        elif fault == "source_review":
            c.execute("UPDATE knowledge_source SET review_status='needs_review' WHERE source_id=?", (source_id,))
        elif fault == "source_expired":
            c.execute("UPDATE knowledge_source SET effective_to='2000-01-01' WHERE source_id=?", (source_id,))
        elif fault == "source_child":
            c.execute("UPDATE knowledge_source SET child_id='child-junior-3' WHERE source_id=?", (source_id,))
        elif fault == "node":
            c.execute("UPDATE knowledge_node SET code='PET.SPEAKING.changed' WHERE knowledge_node_id=(SELECT knowledge_node_id FROM diagnostic_item WHERE diagnostic_item_id=?)", (result["diagnostic_item_id"],))
        elif fault == "item_audio":
            c.execute("UPDATE diagnostic_item SET stimulus_text='different recording' WHERE diagnostic_item_id=?", (result["diagnostic_item_id"],))
        elif fault == "item_answer":
            c.execute("UPDATE diagnostic_item SET expected_answer_json='{}' WHERE diagnostic_item_id=?", (result["diagnostic_item_id"],))
        else:
            c.execute("UPDATE curriculum_standard_requirement SET maturity='retired' WHERE requirement_id=?", (spec["requirement_id"],))
        c.commit()
    assert not CurriculumSupplyService(Database(db.path)).get(CHILD, result["candidate_id"])["eligible"]
    with db.connect() as c:
        with pytest.raises(LocalSupplyError):
            validate_item(c, CHILD, result["diagnostic_item_id"])
        assert c.execute("SELECT COUNT(*) FROM diagnostic_attempt").fetchone()[0] == 0


def test_original_packet_override_and_other_child_are_rejected(tmp_path):
    db, service, spec, source = prepared(tmp_path)
    with pytest.raises(LocalSupplyError):
        service.generate_draft(CHILD, {**spec, "pet_listening": source}, author="author", action_id="override")
    bad = service.draft(CHILD, {**spec, "prompt": source["prompt"], "expected_answer": None,
        "pet_listening": {**source, "required_play_count": 2}}, author="author", action_id="mismatch")
    assert bad["state"] == "blocked" and "CURRICULUM_QUESTION_SOURCE_MISMATCH" in bad["blockers"]
    with pytest.raises(KeyError):
        service.get("child-junior-3", bad["candidate_id"])
    with pytest.raises(LocalSupplyError):
        service.generate_draft("child-junior-3", spec, author="author", action_id="other-child")
