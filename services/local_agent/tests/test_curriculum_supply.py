"""Isolated fixtures are not approval evidence for the production corpus."""
import json

import pytest

from home_study_agent.curriculum_supply import CurriculumSupplyService, CurriculumSupplyError
from home_study_agent.database import Database
from home_study_agent.knowledge_base import KnowledgeBaseService
from home_study_agent.learning_state import LearningStateService
from home_study_agent.local_supply import LocalSupplyError, build_candidate, validate_item
from home_study_agent.repository import StudyRepository
from home_study_agent.specialized_supply import SpecializedDiagnosticSupplyService


CHILD = "child-primary-4"
PROMPT = "计算：125*(32+8)。"
SCOPE = {"grade": "四年级", "subject": "数学", "edition": "测试教材2026",
         "semester": "上学期", "standard_scope": "第二学段"}


def prepared(tmp_path):
    db = Database(tmp_path / "curriculum-supply.db")
    db.initialize()
    StudyRepository(db).seed_family()
    LearningStateService(db).seed_catalog()
    source = KnowledgeBaseService(db).import_source(CHILD, {
        "source_type": "school_material", "title": "隔离验收教材页（非正式资产）",
        "subject": "数学", "grade_band": "四年级", "version_label": SCOPE["edition"],
        "source_ref": "isolated-fixture:page12", "license_status": "family_owned_index",
        "parent_reviewed": True, "content_text": PROMPT,
    })
    standard_root = tmp_path.resolve(strict=True) / "standards"
    standard_root.mkdir()
    (standard_root / "manifest.json").write_text(json.dumps({
        "format": "hsc_official_curriculum_sources_v1",
        "sources": [{"source_key": "fixture", "pdf_filename": None, "pages_filename": None}],
    }), encoding="utf-8")
    with db.connect() as c:
        excerpt = c.execute("SELECT excerpt_id FROM knowledge_excerpt WHERE source_id=?",
                            (source["source_id"],)).fetchone()[0]
        c.execute("""INSERT INTO curriculum_standard_source(
            source_id,source_key,jurisdiction,source_type,subject_title,title,version_label,
            official_url,local_root_path,status,imported_at,updated_at)
            VALUES('std','fixture','CN','national_standard','数学','隔离课标片段','2022',
            'isolated-fixture:standard',?,'structured','2026-09-01','2026-09-01')""",
                  (str(standard_root),))
        c.execute("INSERT INTO curriculum_standard_page VALUES('std',12,'理解运算律并合理选择运算方法。','hash',16)")
        c.execute("""INSERT INTO curriculum_standard_requirement(
            requirement_id,source_id,subject_title,stage_scope,requirement_type,title,
            requirement_text,observable_outcome,page_start,page_end,confidence,maturity,created_at,updated_at)
            VALUES('req','std','数学','第二学段','content_requirement','运算律',
            '理解运算律并合理选择运算方法。','解释方法',12,12,.9,'verified','2026-09-01','2026-09-01')""")
        seed = c.execute("SELECT diagnostic_item_id FROM diagnostic_item WHERE knowledge_node_id='node-p4-math-arithmetic' LIMIT 1").fetchone()[0]
        c.commit()
    service = CurriculumSupplyService(db)
    service.select_curriculum(CHILD, {**SCOPE, "textbook_source_id": source["source_id"]}, actor="parent", action_id="scope", expected_version=0)
    payload = {"seed_item_id": seed, "stage": "parallel", "prompt": PROMPT,
               "expected_answer": "5000", "explanation": "先算括号内40，再计算125乘40得5000。",
               "difficulty": 1, "excerpt_id": excerpt, "requirement_id": "req",
               "standard_page": 12, "standard_quote": "理解运算律并合理选择运算方法。",
               "validator": "arithmetic_expression_v1"}
    return db, service, payload


def draft(service, payload, action="draft", **kwargs):
    return service.draft(CHILD, payload, author="author", action_id=action, **kwargs)


def publish(service, payload, action="draft"):
    result = draft(service, payload, action)
    cid = result["candidate_id"]
    result = service.validate(CHILD, cid, actor="independent-checker", action_id=action+":validate", expected_version=1)
    assert result["state"] == "validated"
    result = service.review(CHILD, cid, decision="approve", actor="parent", note="已核对题源、课标范围、节点及平行题属性",
                            action_id=action+":review", expected_version=2)
    assert result["state"] == "reviewed"
    return service.publish(CHILD, cid, actor="parent", action_id=action+":publish", expected_version=3)


def test_draft_check_review_publish_restart_withdraw(tmp_path):
    db, service, payload = prepared(tmp_path)
    result = publish(service, payload)
    item = result["diagnostic_item_id"]
    assert result["state"] == "published"
    with db.connect() as c:
        assert validate_item(c, CHILD, item)
        assert c.execute("SELECT COUNT(*) FROM learning_evidence").fetchone()[0] == 0
    restarted = CurriculumSupplyService(Database(db.path))
    assert restarted.get(CHILD, result["candidate_id"])["eligible"] is True
    withdrawn = restarted.withdraw(CHILD, result["candidate_id"], actor="parent", note="题源授权撤回",
                                   action_id="withdraw", expected_version=4)
    assert withdrawn["state"] == "withdrawn"
    with db.connect() as c, pytest.raises(LocalSupplyError):
        validate_item(c, CHILD, item)
    assert len(restarted.get(CHILD, result["candidate_id"])["events"]) == 5


@pytest.mark.parametrize("change,reason", [
    ("unreviewed", "CURRICULUM_TEXTBOOK_UNREVIEWED"),
    ("standard", "CURRICULUM_STANDARD_UNREVIEWED"),
    ("edition", "CURRICULUM_SCOPE_MISMATCH"),
    ("missing", "CURRICULUM_TEXTBOOK_UNAVAILABLE"),
])
def test_missing_or_unreviewed_sources_stay_blocked(tmp_path, change, reason):
    db, service, payload = prepared(tmp_path)
    with db.connect() as c:
        if change == "unreviewed":
            c.execute("UPDATE knowledge_source SET review_status='quarantined'")
        elif change == "standard":
            c.execute("UPDATE curriculum_standard_requirement SET maturity='model_drafted'")
        elif change == "edition":
            c.execute("UPDATE knowledge_source SET version_label='另一版'")
        else:
            payload["excerpt_id"] = "missing"
        c.commit()
    result = draft(service, payload)
    assert result["state"] == "blocked"
    assert reason in result["blockers"]
    with pytest.raises(CurriculumSupplyError):
        service.publish(CHILD, result["candidate_id"], actor="parent", action_id="bad", expected_version=1)


@pytest.mark.parametrize("stage", ["representation_transfer", "far_transfer", "transfer", "discrimination"])
def test_no_stage_relabeling(tmp_path, stage):
    _, service, payload = prepared(tmp_path)
    result = draft(service, {**payload, "stage": stage})
    checked = service.validate(CHILD, result["candidate_id"], actor="checker", action_id="check", expected_version=1)
    assert checked["state"] == "blocked"
    assert "CURRICULUM_STAGE_VALIDATOR_UNAVAILABLE" in checked["blockers"]


def test_independent_check_wrong_answer_and_same_author(tmp_path):
    _, service, payload = prepared(tmp_path)
    result = draft(service, {**payload, "expected_answer": "4999"})
    with pytest.raises(CurriculumSupplyError, match="INDEPENDENT"):
        service.validate(CHILD, result["candidate_id"], actor="author", action_id="bad", expected_version=1)
    result = service.validate(CHILD, result["candidate_id"], actor="checker", action_id="check", expected_version=1)
    assert "CURRICULUM_ANSWER_MISMATCH" in result["blockers"]


@pytest.mark.parametrize("change", ["excerpt", "standard_page", "review", "scope"])
def test_published_projection_rechecks_live_evidence(tmp_path, change):
    db, service, payload = prepared(tmp_path)
    result = publish(service, payload)
    if change == "scope":
        with db.connect() as c:
            scope = json.loads(c.execute("SELECT scope_json FROM curriculum_supply_selection").fetchone()[0])
        service.select_curriculum(CHILD, {**scope, "semester": "下学期"}, actor="parent", action_id="next-term", expected_version=1)
    else:
        with db.connect() as c:
            if change == "excerpt":
                c.execute("UPDATE knowledge_excerpt SET content_text=content_text||'修订'")
            elif change == "standard_page":
                c.execute("UPDATE curriculum_standard_page SET content_text=content_text||'修订'")
            else:
                c.execute("UPDATE knowledge_source SET review_status='quarantined'")
            c.commit()
    assert service.get(CHILD, result["candidate_id"])["eligible"] is False
    with db.connect() as c, pytest.raises(LocalSupplyError):
        validate_item(c, CHILD, result["diagnostic_item_id"])


def test_old_review_endpoint_cannot_bypass_independent_validation(tmp_path):
    db, service, payload = prepared(tmp_path)
    result = draft(service, payload)
    pending = SpecializedDiagnosticSupplyService(db).list(CHILD)['items']
    current = next(item for item in pending if item['candidate_id'] == result['candidate_id'])
    assert current['review_workflow'] == 'curriculum_review'
    with pytest.raises(ValueError):
        SpecializedDiagnosticSupplyService(db).review(result["candidate_id"], "approve", "绕过", "bypass")


def test_other_child_and_idempotency_conflicts(tmp_path):
    _, service, payload = prepared(tmp_path)
    result = draft(service, payload)
    assert draft(service, payload)["candidate_id"] == result["candidate_id"]
    with pytest.raises(CurriculumSupplyError, match="CONFLICT"):
        draft(service, {**payload, "expected_answer": "4999"})
    with pytest.raises(KeyError):
        service.get("child-junior-3", result["candidate_id"])
    with pytest.raises(CurriculumSupplyError, match="VERSION"):
        service.validate(CHILD, result["candidate_id"], actor="checker", action_id="check", expected_version=0)


def test_revision_is_new_candidate_and_old_release_is_invalidated(tmp_path):
    db, service, payload = prepared(tmp_path)
    old = publish(service, payload)
    new = draft(service, {**payload, "explanation": "40乘125等于5000。"}, "revise", supersedes=old["candidate_id"], supersedes_version=4)
    assert new["candidate_id"] != old["candidate_id"]
    assert service.get(CHILD, old["candidate_id"])["state"] == "withdrawn"
    with db.connect() as c, pytest.raises(LocalSupplyError):
        validate_item(c, CHILD, old["diagnostic_item_id"])


def test_original_stage_guards_still_hold(tmp_path):
    db, _, payload = prepared(tmp_path)
    with db.connect() as c:
        seed = validate_item(c, CHILD, payload["seed_item_id"])
    for stage in ("representation_transfer", "far_transfer"):
        with pytest.raises(LocalSupplyError):
            build_candidate(seed, stage, 0)


def test_real_plan_selection_scoring_restart_and_withdrawal(tmp_path):
    db, service, payload = prepared(tmp_path)
    published = publish(service, payload)
    repo, learning = StudyRepository(db), LearningStateService(db)
    sid = repo.start_session(CHILD, "start")["session_id"]
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
        c.commit()
    initial = learning.submit_diagnostic(sid, payload["seed_item_id"], "不会", "initial")
    assert initial["score_status"] == "verified_local"
    SpecializedDiagnosticSupplyService(db).bind_due_reviews(CHILD)
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='homework_capture',recall_declared_empty=1,homework_declared_empty=1 WHERE session_id=?", (sid,))
        c.execute("UPDATE family_rule SET latest_end_time=NULL,max_learning_minutes=90 WHERE child_id=?", (CHILD,))
        c.commit()
    plan = repo.generate_plan(sid, "plan")
    due = next(item for item in plan["plan_items"] if item["source_type"] == "due_review")
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
        c.commit()
    repo.start_plan_item(due["plan_item_id"], "start-item")
    question = repo.plan_review_question(due["plan_item_id"])
    assert question["diagnostic_item_id"] == published["diagnostic_item_id"]
    attempt = learning.submit_diagnostic(sid, question["diagnostic_item_id"], "5000", "answer", question["review_task_id"])
    assert attempt["score_status"] == "verified_local"
    restarted = StudyRepository(Database(db.path))
    assert restarted.plan_review_question(due["plan_item_id"])["attempt"]["attempt_id"] == attempt["attempt_id"]
    from home_study_agent.learning_obligations import project_child_mastery_evidence
    with db.connect() as c:
        before = project_child_mastery_evidence(c, CHILD)
        assert attempt["evidence_id"] in {row["evidence_id"] for row in before["node-p4-math-arithmetic"]["effective_evidence"]}
    service.withdraw(CHILD, published["candidate_id"], actor="parent", note="来源撤回", action_id="withdraw", expected_version=4)
    with db.connect() as c:
        after = project_child_mastery_evidence(c, CHILD)
        assert attempt["evidence_id"] not in {row["evidence_id"] for row in after["node-p4-math-arithmetic"]["effective_evidence"]}
        assert c.execute("SELECT COUNT(*) FROM diagnostic_attempt WHERE attempt_id=?", (attempt["attempt_id"],)).fetchone()[0] == 1


def test_publish_failure_rolls_back_and_retries_once(tmp_path, monkeypatch):
    db, service, payload = prepared(tmp_path)
    result = draft(service, payload)
    cid = result["candidate_id"]
    service.validate(CHILD, cid, actor="checker", action_id="v", expected_version=1)
    service.review(CHILD, cid, decision="approve", actor="parent", note="范围和题目已核对", action_id="r", expected_version=2)
    from home_study_agent import specialized_supply
    real = specialized_supply.promote_candidate

    def fail(c, row):
        real(c, row)
        raise OSError("isolated injected failure")

    monkeypatch.setattr(specialized_supply, "promote_candidate", fail)
    with pytest.raises(OSError):
        service.publish(CHILD, cid, actor="parent", action_id="p", expected_version=3)
    assert service.get(CHILD, cid)["state"] == "reviewed"
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM diagnostic_item WHERE source_ref=?", ("parent_approved_specialized:"+cid,)).fetchone()[0] == 0
    monkeypatch.setattr(specialized_supply, "promote_candidate", real)
    first = service.publish(CHILD, cid, actor="parent", action_id="p", expected_version=3)
    second = service.publish(CHILD, cid, actor="parent", action_id="p", expected_version=3)
    assert first["diagnostic_item_id"] == second["diagnostic_item_id"]
    assert len(second["events"]) == 4


def test_package_privacy_deletion_order_is_allowed(tmp_path):
    db, service, payload = prepared(tmp_path)
    result = publish(service, payload)
    with db.connect() as c:
        c.execute("DELETE FROM curriculum_supply_package WHERE child_id=?", (CHILD,))
        c.execute("DELETE FROM curriculum_supply_selection WHERE child_id=?", (CHILD,))
        assert c.execute("SELECT COUNT(*) FROM curriculum_supply_event").fetchone()[0] == 0
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []
        with pytest.raises(LocalSupplyError):
            validate_item(c, CHILD, result["diagnostic_item_id"])
        c.commit()


@pytest.mark.parametrize("expression", ["__import__('os')", "2**1000000", "1/0", "True", "sum([1,2])"])
def test_arithmetic_validator_never_executes_code(tmp_path, expression):
    db, service, payload = prepared(tmp_path)
    prompt = "计算："+expression+"。"
    with db.connect() as c:
        c.execute("UPDATE knowledge_excerpt SET content_text=?", (prompt,))
        c.commit()
    result = draft(service, {**payload, "prompt": prompt})
    checked = service.validate(CHILD, result["candidate_id"], actor="checker", action_id="check", expected_version=1)
    assert checked["state"] == "blocked"


def test_extract_draft_does_not_guess_answer_or_mark_it_verified(tmp_path):
    _, service, payload = prepared(tmp_path)
    source_spec = {key: value for key, value in payload.items() if key not in {"prompt", "expected_answer"}}
    result = service.generate_draft(CHILD, source_spec, author="source-extractor", action_id="extract")
    assert result["state"] == "draft" and result["eligible"] is False
    assert result["payload"]["prompt"] == PROMPT
    assert result["payload"]["expected_answer"] is None
    checked = service.validate(CHILD, result["candidate_id"], actor="checker", action_id="check", expected_version=1)
    assert checked["state"] == "validated"
    assert checked["events"][-1]["result"]["check"]["computed_answer"] == "5000"


def test_revision_expected_version_and_question_alias_receipt(tmp_path):
    db, service, payload = prepared(tmp_path)
    old = publish(service, payload)
    repo = StudyRepository(db)
    sid = repo.start_session(CHILD, "session")["session_id"]
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
        c.commit()
    LearningStateService(db).submit_diagnostic(sid, old["diagnostic_item_id"], "5000", "old-answer")
    with pytest.raises(CurriculumSupplyError, match="VERSION"):
        draft(service, payload, "stale-revision", supersedes=old["candidate_id"], supersedes_version=3)
    new = draft(service, payload, "revision", supersedes=old["candidate_id"], supersedes_version=4)
    cid = new["candidate_id"]
    service.validate(CHILD, cid, actor="checker", action_id="v2", expected_version=1)
    service.review(CHILD, cid, actor="parent", decision="approve", note="核对修订版", action_id="r2", expected_version=2)
    new = service.publish(CHILD, cid, actor="parent", action_id="p2", expected_version=3)
    second = LearningStateService(db).submit_diagnostic(sid, new["diagnostic_item_id"], "5000", "new-answer")
    from home_study_agent.learning_obligations import project_child_mastery_evidence
    with db.connect() as c:
        projection = project_child_mastery_evidence(c, CHILD)["node-p4-math-arithmetic"]
        assert second["evidence_id"] not in {e["evidence_id"] for e in projection["effective_evidence"]}


def test_published_item_cannot_be_moved_to_a_reasoning_node(tmp_path):
    db, service, payload = prepared(tmp_path)
    result = publish(service, payload)
    with db.connect() as c:
        c.execute("UPDATE diagnostic_item SET knowledge_node_id='node-oly-arithmetic-structure' WHERE diagnostic_item_id=?", (result["diagnostic_item_id"],))
        c.commit()
        with pytest.raises(LocalSupplyError):
            validate_item(c, CHILD, result["diagnostic_item_id"])


def test_isolated_blocked_root_causes_are_not_cleared_by_curriculum_publication(tmp_path):
    db, service, payload = prepared(tmp_path)
    supply = SpecializedDiagnosticSupplyService(db)
    supply.enqueue_local("diag-oly-number-theory-1", CHILD, "no-template")
    supply.enqueue_local("diag-oly-arithmetic-1", CHILD, "no-stage")
    supply.process_local_pending()
    before = service.audit(CHILD)
    assert before["blocked_job_counts"] == {"LOCAL_TEMPLATE_UNAVAILABLE": 1, "LOCAL_STAGE_UNSUPPORTED": 1}
    publish(service, payload)
    after = service.audit(CHILD)
    assert after["blocked_job_counts"] == before["blocked_job_counts"]
    assert after["eligible_curriculum_items"] == 1
    assert after["qualified_stages"] == ["parallel"]
    assert after["thinking_or_transfer_coverage_claim_allowed"] is False


def test_blocked_curriculum_job_cannot_be_retried_as_model_work(tmp_path):
    db, service, payload = prepared(tmp_path)
    result = draft(service, {**payload, "stage": "far_transfer"})
    service.validate(CHILD, result["candidate_id"], actor="checker", action_id="v", expected_version=1)
    with db.connect() as c:
        job = c.execute("SELECT j.* FROM specialized_supply_job j JOIN specialized_supply_candidate c USING(job_id) WHERE c.candidate_id=?",
                        (result["candidate_id"],)).fetchone()
        assert job["status"] == "blocked"
    with pytest.raises(LocalSupplyError):
        SpecializedDiagnosticSupplyService(db).retry_job(CHILD, job["job_id"], allow_model_retry=True)


def test_expression_alias_does_not_get_a_new_question_identity(tmp_path):
    db, service, payload = prepared(tmp_path)
    old = publish(service, payload)
    alias = "计算：125 * (32+8)。"
    with db.connect() as c:
        c.execute("UPDATE knowledge_excerpt SET content_text=?", (alias,))
        c.commit()
    with pytest.raises(CurriculumSupplyError, match="DUPLICATE"):
        draft(service, {**payload, "prompt": alias}, "alias", supersedes=old["candidate_id"], supersedes_version=4)
    assert service.get(CHILD, old["candidate_id"])["state"] == "published"


def test_same_grade_other_child_still_cannot_use_or_publish_this_supply(tmp_path):
    db, service, payload = prepared(tmp_path)
    result = publish(service, payload)
    other = "child-junior-3"
    with db.connect() as c:
        c.execute("UPDATE child_profile SET grade='四年级' WHERE child_id=?", (other,))
        c.commit()
        with pytest.raises(LocalSupplyError):
            validate_item(c, other, result["diagnostic_item_id"])
    with pytest.raises(KeyError):
        service.withdraw(other, result["candidate_id"], actor="parent", note="wrong child", action_id="w", expected_version=4)
    assert LearningStateService(db).next_diagnostic(other, "school", preferred_item_id=result["diagnostic_item_id"]) is None


def test_withdrawal_invalidates_template_descendants_without_rewriting_them(tmp_path):
    db, service, payload = prepared(tmp_path)
    result = publish(service, payload)
    from home_study_agent.local_supply import LocalSupplyQueue
    supply = SpecializedDiagnosticSupplyService(db)
    LocalSupplyQueue(supply).enqueue(CHILD, result["diagnostic_item_id"], "descendant", track="school",
                                    domain_id="node-p4-math-arithmetic", count=1)
    supply.process_local_pending()
    candidate = next(row for row in supply.list(CHILD)["items"] if row["seed_item_id"] == result["diagnostic_item_id"])
    descendant = supply.qualify_local(candidate["candidate_id"], CHILD, "qualify")
    with db.connect() as c:
        assert validate_item(c, CHILD, descendant["diagnostic_item_id"])
    service.withdraw(CHILD, result["candidate_id"], actor="parent", note="source withdrawn", action_id="w", expected_version=4)
    with db.connect() as c:
        with pytest.raises(LocalSupplyError):
            validate_item(c, CHILD, descendant["diagnostic_item_id"])
        assert c.execute("SELECT status FROM diagnostic_item WHERE diagnostic_item_id=?", (descendant["diagnostic_item_id"],)).fetchone()[0] == "active"


def test_simultaneous_publish_is_idempotent(tmp_path):
    _, service, payload = prepared(tmp_path)
    result = draft(service, payload)
    cid = result["candidate_id"]
    service.validate(CHILD, cid, actor="checker", action_id="v", expected_version=1)
    service.review(CHILD, cid, decision="approve", actor="parent", note="核对范围和答案", action_id="r", expected_version=2)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(service.publish, CHILD, cid, actor="parent", action_id="p", expected_version=3) for _ in range(2)]
        results = [future.result() for future in futures]
    assert len({r["diagnostic_item_id"] for r in results}) == 1
    assert len(service.get(CHILD, cid)["events"]) == 4


def test_review_packet_binds_actual_source_quote_and_hides_storage_paths(tmp_path):
    _, service, payload = prepared(tmp_path)
    result = draft(service, payload)
    packet = result["review_packet"]
    assert packet["textbook"]["content_text"] == PROMPT
    assert packet["standard"]["quote"] == payload["standard_quote"]
    assert packet["standard"]["page_number"] == 12
    assert "local_root_path" not in json.dumps(packet)
    assert service.get(CHILD, result["candidate_id"])["review_packet"]["fingerprint"] == packet["fingerprint"]
