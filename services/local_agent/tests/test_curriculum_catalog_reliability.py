import asyncio
import json

import pytest

from home_study_agent.curriculum_internalization import CurriculumInternalizationService
from home_study_agent.curriculum_standards import CurriculumStandardService
from home_study_agent.curriculum_supply import CurriculumSupplyService
from home_study_agent.database import Database
from home_study_agent.knowledge_base import KnowledgeBaseService
from home_study_agent.repository import StudyRepository
from test_curriculum_corpus import InternalizationGateway, create_official_math_standard, create_seyk_source, import_payload


CHILD = "child-primary-4"


class RevisedGateway(InternalizationGateway):
    change = None

    async def complete(self, request):
        result = await super().complete(request)
        value = json.loads(result.content)
        item = value["capabilities"][0]
        if self.change == "criteria":
            item["mastery_criteria"] = ["新增独立解释及延迟复测条件"]
        elif self.change == "requirement":
            item["standard_alignments"][0]["requirement_text"] = "模型改写了课标概括，尚未经审核。"
        elif self.change == "confidence":
            item["confidence"] = .7
        return type(result)(**{**result.__dict__, "content": json.dumps(value, ensure_ascii=False)})


@pytest.fixture
def catalog(tmp_path):
    db = Database(tmp_path / "catalog.db")
    db.initialize()
    StudyRepository(db).seed_family()
    kb = KnowledgeBaseService(db)
    corpus = kb.import_corpus(import_payload(*create_seyk_source(tmp_path / "source")))
    standards = create_official_math_standard(tmp_path / "standards", "小数加法学习中，学生理解小数的意义，能进行简单的小数加减运算，并形成运算能力。")
    CurriculumStandardService(db).import_root(str(standards))
    gateway = RevisedGateway()
    service = CurriculumInternalizationService(db, gateway)
    return db, kb, corpus, gateway, service


def run(catalog, action):
    db, _, corpus, _, service = catalog
    from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
    privacy = PrivacyLifecycleService(db, db.path.parent)
    privacy.initialize()
    privacy.update_preferences({'cloud_model_text_allowed': True})
    job = service.create_job(corpus["corpus_id"], {"child_id": CHILD, "subject_title": "小学数学",
        "version_title": "苏教版", "semester": "下册", "max_units": 1, "client_action_id": action})
    asyncio.run(service.run_job(job["job_id"]))
    assert service.get_job(job["job_id"])["status"] == "completed"
    return service.list_capabilities(CHILD)


@pytest.mark.parametrize("change", ["criteria", "requirement"])
def test_generated_material_revision_never_inherits_old_verified_version(catalog, change):
    db, _, _, gateway, service = catalog
    old = run(catalog, "first")[0]
    service.review_capability(old["capability_id"], "verify", "隔离人工审核")
    with db.connect() as c:
        old_requirement = dict(c.execute("SELECT * FROM curriculum_standard_requirement").fetchone())
    gateway.change = change
    current = run(catalog, "second")
    assert len(current) == 2
    assert {r["maturity"] for r in current} == {"model_drafted", "verified"}
    preserved = next(r for r in current if r["capability_id"] == old["capability_id"])
    assert preserved["mastery_criteria"] == old["mastery_criteria"]
    with db.connect() as c:
        assert dict(c.execute("SELECT * FROM curriculum_standard_requirement WHERE requirement_id=?", (old_requirement["requirement_id"],)).fetchone()) == old_requirement
        if change == "requirement":
            assert c.execute("SELECT COUNT(*) FROM curriculum_standard_requirement WHERE maturity='model_drafted'").fetchone()[0] == 1


def test_regeneration_cannot_resurrect_rejected_material(catalog):
    _, _, _, gateway, service = catalog
    old = run(catalog, "first")[0]
    service.review_capability(old["capability_id"], "reject", "不通过")
    gateway.change = "confidence"
    current = run(catalog, "second")
    assert len(current) == 1 and current[0]["maturity"] == "retired"


def test_changed_source_under_same_topic_id_requires_new_unreviewed_version(catalog):
    db, _, _, _, service = catalog
    old = run(catalog, "first")[0]
    service.review_capability(old["capability_id"], "verify", "审核旧题源")
    with db.connect() as c:
        c.execute("UPDATE curriculum_resource SET transcript_text=transcript_text||' 修订后的教学证据。'")
        c.commit()
    current = run(catalog, "second")
    assert len(current) == 2
    assert next(r for r in current if r["capability_id"] != old["capability_id"])["maturity"] == "model_drafted"


def test_source_change_during_model_request_cannot_be_bound_to_new_source(catalog, monkeypatch):
    db, _, corpus, gateway, service = catalog
    from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
    privacy = PrivacyLifecycleService(db, db.path.parent)
    privacy.initialize()
    privacy.update_preferences({'cloud_model_text_allowed': True})
    real = gateway.complete

    async def changed(request):
        result = await real(request)
        with db.connect() as c:
            c.execute("UPDATE curriculum_resource SET transcript_text=transcript_text||' changed during request'")
            c.commit()
        return result

    monkeypatch.setattr(gateway, "complete", changed)
    job = service.create_job(corpus["corpus_id"], {"child_id": CHILD, "subject_title": "小学数学", "version_title": "苏教版",
        "semester": "下册", "max_units": 1, "client_action_id": "race"})
    asyncio.run(service.run_job(job["job_id"]))
    assert service.get_job(job["job_id"])["status"] == "failed"
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM curriculum_capability").fetchone()[0] == 0


def test_current_curriculum_filters_topics_capabilities_search_and_grounding(catalog):
    db, kb, _, _, service = catalog
    run(catalog, "first")
    assert kb.list_topics(CHILD) and service.list_capabilities(CHILD)
    source = kb.import_source(CHILD, {"source_type": "school_material", "title": "隔离审核教材",
        "subject": "数学", "grade_band": "四年级", "version_label": "苏教版", "source_ref": "fixture:math",
        "license_status": "family_owned_index", "parent_reviewed": True, "content_text": "小数加法"})
    supply = CurriculumSupplyService(db)
    scope = {"grade": "四年级", "subject": "数学", "edition": "苏教版", "semester": "下学期",
             "standard_scope": "第二学段", "textbook_source_id": source["source_id"]}
    supply.select_curriculum(CHILD, scope, actor="parent", action_id="scope", expected_version=0)
    assert kb.list_topics(CHILD) and service.list_capabilities(CHILD)
    assert kb.search_corpus(CHILD, "小数加法")
    assert kb.grounding_context(CHILD, "数学", "小数加法怎样把数位对齐")
    supply.select_curriculum(CHILD, {**scope, "semester": "上学期"}, actor="parent", action_id="term", expected_version=1)
    assert kb.list_topics(CHILD) == []
    assert service.list_capabilities(CHILD) == []
    assert kb.search_corpus(CHILD, "小数加法") == []
    assert kb.grounding_context(CHILD, "数学", "小数加法怎样把数位对齐") == []
    # The immutable approved version remains available to historical inspection.
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM curriculum_capability").fetchone()[0] == 1


@pytest.mark.parametrize("change", ["source", "legacy_review"])
def test_verified_reads_recompute_review_provenance_without_erasing_history(catalog, change):
    db, _, _, _, service = catalog
    old = run(catalog, "first")[0]
    service.review_capability(old["capability_id"], "verify", "核对当时题源")
    with db.connect() as c:
        if change == "source":
            c.execute("UPDATE curriculum_resource SET transcript_text=transcript_text||' withdrawn source version'")
        else:
            c.execute("UPDATE audit_event SET summary_json=json_remove(summary_json,'$.review_fingerprint') WHERE action='curriculum_capability_reviewed'")
        c.commit()
    assert service.list_capabilities(CHILD, maturity="verified") == []
    projected = service.list_capabilities_for_ids([old["capability_id"]])[0]
    assert projected["maturity"] == "conflicted"
    with db.connect() as c:
        assert c.execute("SELECT maturity FROM curriculum_capability WHERE capability_id=?", (old["capability_id"],)).fetchone()[0] == "verified"
    reviewed = service.review_capability(old["capability_id"], "verify", "重新核对当前题源")
    assert reviewed["maturity"] == "verified"
