"""W19 context controls using disposable SQLite and real scoring/correction APIs."""

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import subprocess
import sys

import pytest

from home_study_agent import learning_state, repository
from home_study_agent.child_model import ChildModelService
from home_study_agent.database import Database
from home_study_agent.learning_state import LearningStateService
from home_study_agent.repository import StudyRepository

CHILD = "child-primary-4"
OTHER = "child-junior-3"
NODE = "node-p4-math-arithmetic"
ITEM = "diag-p4-arithmetic-closed-1"
SECOND = "diag-p4-arithmetic-closed-2"


@pytest.fixture
def flow(tmp_path, monkeypatch):
    db = Database(tmp_path / "teaching.db")
    db.initialize()
    repo = StudyRepository(db)
    repo.seed_family()
    learning = LearningStateService(db)
    learning.seed_catalog()
    clock = [datetime.now(timezone.utc) - timedelta(minutes=5)]
    monkeypatch.setattr(repository, "utc_now", lambda: clock[0].isoformat())
    monkeypatch.setattr(learning_state, "utc_now", lambda: clock[0].isoformat())
    sid = repo.start_session(CHILD, "teaching-session")["session_id"]
    change(db, "UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
    ChildModelService(db).ensure_schema()
    return db, repo, learning, clock, sid


def change(db, sql, args=()):
    with db.connect() as c:
        c.execute(sql, args)
        c.commit()


def controls(flow, **kwargs):
    return ChildModelService(flow[0]).tutor_controls(CHILD, 1, None, {}, **{
        "session_id": flow[4], "subject": "数学", "knowledge_node_id": NODE,
        "task_type": "diagnostic", **kwargs})


def context(flow, **kwargs):
    return controls(flow, **kwargs)["audit"]["teaching_context"]


def pacing(result):
    return result["audit"]["controls"]["explanation_pacing"]


def score(flow, item=ITEM, answer="wrong", key="first", hint_level=0):
    return flow[2].submit_diagnostic(flow[4], item, answer, key, hint_level=hint_level)


def test_latest_counterevidence_changes_support_not_mastery(flow):
    first = score(flow)
    weak = context(flow)
    assert weak["learning"]["status"] == "recheck_needed"
    assert weak["learning"]["evidence_refs"] == [first["evidence_id"]]
    assert pacing(controls(flow)) == "short_step_then_restate"
    flow[3][0] += timedelta(minutes=1)
    second = score(flow, SECOND, "42", "second")
    improved = context(flow)
    assert improved["learning"]["status"] == "independent_observed"
    assert improved["learning"]["evidence_refs"] == [second["evidence_id"]]
    assert pacing(controls(flow)) == "standard_layered"
    assert improved["source_version"] != weak["source_version"]
    assert not improved["mastery_claim_allowed"]


@pytest.mark.parametrize("scope", [
    {"subject": "英语", "knowledge_node_id": "node-p4-english-phonics-discrimination"},
    {"task_type": "oral_recall"}, {"knowledge_node_id": None}, {"task_type": None},
])
def test_unfamiliar_subject_node_or_task_never_borrows_child_totals(flow, scope):
    score(flow, answer="90", hint_level=2)
    result = controls(flow, **scope)
    assert pacing(result) == "standard_layered"
    assert result["audit"]["teaching_context"]["learning"]["status"] == "unknown"
    assert result["audit"]["teaching_context"]["learning"]["evidence_refs"] == []


def test_old_caller_missing_context_stays_unknown(flow):
    score(flow, answer="90", hint_level=2)
    result = ChildModelService(flow[0]).tutor_controls(CHILD, 1, None, {})
    assert pacing(result) == "standard_layered"
    assert result["audit"]["teaching_context"]["learning"]["status"] == "unknown"


def test_withdrawn_item_and_restored_source_reproject_after_restart(flow):
    score(flow)
    before = context(flow)
    change(flow[0], "UPDATE diagnostic_item SET status='withdrawn' WHERE diagnostic_item_id=?", (ITEM,))
    withdrawn = context(flow)
    assert withdrawn["learning"]["status"] == "unknown"
    assert withdrawn["source_version"] != before["source_version"]
    assert pacing(controls(flow)) == "standard_layered"
    change(flow[0], "UPDATE diagnostic_item SET status='active' WHERE diagnostic_item_id=?", (ITEM,))
    assert context(flow)["learning"]["status"] == "recheck_needed"


def test_expired_receipt_is_not_current_ability_loss(flow):
    flow[3][0] -= timedelta(days=31)
    score(flow)
    result = context(flow)
    assert result["learning"]["status"] == "unknown"
    assert result["learning"]["reason"] == "evidence_expired"
    assert not result["mastery_claim_allowed"]


def test_same_question_repetition_does_not_overwrite_independent_check(flow):
    score(flow, answer="90")
    flow[3][0] += timedelta(minutes=1)
    score(flow, key="repeat", hint_level=3)
    assert context(flow)["learning"]["status"] == "independent_observed"
    assert pacing(controls(flow)) == "standard_layered"


@pytest.mark.parametrize("mismatch", ["child", "subject", "terminal"])
def test_context_scope_mismatch_rejected_without_foreign_evidence(flow, mismatch):
    score(flow)
    kwargs = {}
    if mismatch == "child":
        change(flow[0], "UPDATE daily_session SET status='completed',state='completed' WHERE session_id=?", (flow[4],))
        kwargs["session_id"] = flow[1].start_session(OTHER, "other-session")["session_id"]
    elif mismatch == "subject":
        kwargs["subject"] = "英语"
    else:
        change(flow[0], "UPDATE daily_session SET state='completed',status='completed' WHERE session_id=?", (flow[4],))
    result = context(flow, **kwargs)
    assert result["status"] == "invalid_context"
    assert result["learning"]["evidence_refs"] == []


def save_energy(flow, energy, key):
    change(flow[0], "UPDATE daily_session SET state='check_in' WHERE session_id=?", (flow[4],))
    flow[1].save_planning_context(flow[4], energy, 0, "PRIVATE ENERGY NOTE", None, None, 0, key)


def test_recent_energy_revision_and_expiry_are_temporary_not_learning(flow):
    save_energy(flow, "low", "low")
    low = controls(flow)
    assert pacing(low) == "short_step_then_restate"
    assert low["audit"]["sources"]["explanation_pacing"] == "recent_context"
    assert low["audit"]["teaching_context"]["learning"]["status"] == "unknown"
    assert "PRIVATE" not in low["prompt_extension"]
    flow[3][0] += timedelta(minutes=1)
    save_energy(flow, "high", "high")
    assert pacing(controls(flow)) == "standard_layered"
    flow[3][0] -= timedelta(hours=3)
    save_energy(flow, "low", "expired-low")
    assert pacing(controls(flow)) == "standard_layered"


def test_context_row_without_matching_persisted_event_is_not_authority(flow):
    save_energy(flow, "high", "high")
    change(flow[0], "UPDATE session_planning_context SET energy_level='low' WHERE session_id=?", (flow[4],))
    assert pacing(controls(flow)) == "standard_layered"
    assert context(flow)["recent"]["status"] == "unknown"


def test_parent_correction_supersession_revoke_and_private_boundary(flow):
    score(flow)
    service = ChildModelService(flow[0])
    first = service.correct(CHILD, "explanation_pacing", "standard_layered", "PRIVATE REASON", None, "correct")
    corrected = controls(flow)
    assert pacing(corrected) == "standard_layered"
    assert corrected["audit"]["teaching_context"]["learning"]["status"] == "recheck_needed"
    assert corrected["audit"]["decisions"]["explanation_pacing"]["evidence_refs"] == [first["correction_id"]]
    assert "PRIVATE" not in corrected["prompt_extension"]
    second = service.correct(CHILD, "explanation_pacing", "short_step_then_restate", "PRIVATE REVISION", None, "correct2")
    assert controls(flow)["audit"]["correction_ids"] == [second["correction_id"]]
    service.revoke(second["correction_id"], "PRIVATE REVOKE", "revoke")
    assert controls(flow)["audit"]["correction_ids"] == []
    assert pacing(controls(flow)) == "short_step_then_restate"
    assert context(flow)["source_version"] != corrected["audit"]["teaching_context"]["source_version"]


def test_foreign_correction_does_not_change_current_context(flow):
    before = context(flow)
    ChildModelService(flow[0]).correct(OTHER, "explanation_pacing", "short_step_then_restate", "other private", None, "other")
    assert context(flow) == before


def test_helper_readonly_and_process_restart_reproduce_context(flow):
    from home_study_agent.teaching_context import build_teaching_context
    score(flow)
    now = datetime.now(timezone.utc).isoformat()
    kwargs = dict(session_id=flow[4], subject="数学", knowledge_node_id=NODE, task_type="diagnostic")
    with flow[0].connect() as c:
        before = hashlib.sha256("\n".join(c.iterdump()).encode()).hexdigest()
        result = build_teaching_context(c, CHILD, now=datetime.fromisoformat(now), **kwargs)
        assert hashlib.sha256("\n".join(c.iterdump()).encode()).hexdigest() == before
    script = """import json,sys
from datetime import datetime
from home_study_agent.database import Database
from home_study_agent.teaching_context import build_teaching_context
with Database(sys.argv[1]).connect() as c:
 print(json.dumps(build_teaching_context(c,sys.argv[2],now=datetime.fromisoformat(sys.argv[3]),**json.loads(sys.argv[4]))))
"""
    rebuilt = subprocess.run([sys.executable, "-c", script, str(flow[0].path), CHILD, now, json.dumps(kwargs)],
                             capture_output=True, text=True, check=True, env=os.environ.copy())
    assert json.loads(rebuilt.stdout) == result


def test_real_evidence_withdrawal_not_just_item_status(flow):
    from home_study_agent.mistake_book import MistakeBookService
    attempt = score(flow)
    book = MistakeBookService(flow[0])
    mistake = book.capture_from_attempt(attempt)
    before = context(flow)
    book.confirm(mistake["mistake_id"], CHILD, "exclude", "withdraw-receipt")
    after = context(flow)
    assert after["learning"]["status"] == "unknown"
    assert after["source_version"] != before["source_version"]
    assert any(row["reason"] == "withdrawn" for row in after["excluded"])


def test_expired_parent_correction_does_not_survive_restart(flow):
    service = ChildModelService(flow[0])
    saved = service.correct(CHILD, "explanation_pacing", "short_step_then_restate", "private", None, "expiry")
    assert pacing(controls(flow)) == "short_step_then_restate"
    change(flow[0], "UPDATE child_model_correction SET expires_at=? WHERE correction_id=?",
           ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), saved["correction_id"]))
    assert pacing(controls(flow)) == "standard_layered"
    assert context(flow)["corrections"] == []


@pytest.mark.parametrize("mutation", ["withdrawn", "expired", "future", "revised_item", "node_inactive", "missing_source"])
def test_current_source_must_remain_valid(flow, mutation):
    score(flow)
    before = context(flow)
    db = flow[0]
    if mutation == "revised_item":
        change(db, "UPDATE diagnostic_item SET prompt='PRIVATE changed question' WHERE diagnostic_item_id=?", (ITEM,))
    elif mutation == "node_inactive":
        change(db, "UPDATE knowledge_node SET status='inactive' WHERE knowledge_node_id=?", (NODE,))
    elif mutation == "missing_source":
        change(db, "UPDATE knowledge_node SET source_ref='unverified-source' WHERE knowledge_node_id=?", (NODE,))
    else:
        change(db, "UPDATE knowledge_node SET source_ref='context-source' WHERE knowledge_node_id=?", (NODE,))
        change(db, """INSERT INTO knowledge_source(source_id,child_id,source_type,title,subject,grade_band,
            source_ref,license_status,review_status,content_hash,imported_at,effective_from,effective_to)
            VALUES('context-source',?,'test','PRIVATE SOURCE','数学','四年级','context-source','local',
            ?,'v1',?, ?, ?)""", (CHILD, "withdrawn" if mutation == "withdrawn" else "approved",
            flow[3][0].isoformat(), "2999-01-01" if mutation == "future" else None,
            "2000-01-01" if mutation == "expired" else None))
    after = context(flow)
    assert after["learning"]["status"] == "unknown"
    assert pacing(controls(flow)) == "standard_layered"
    assert before["source_version"] != after["source_version"]
    assert "PRIVATE" not in controls(flow)["prompt_extension"]


def bind_plan(flow, *, child=CHILD, task_type="written", subject="数学", node=NODE):
    db, _, _, clock, sid = flow
    change(db, """INSERT INTO daily_plan(plan_id,child_id,session_id,plan_date,status,total_minutes,
        generator,version,created_at,client_action_id) VALUES('context-plan',?,?,?,'active',15,'test',1,?,'plan')""",
        (child, sid, clock[0].date().isoformat(), clock[0].isoformat()))
    change(db, """INSERT INTO plan_item(plan_item_id,plan_id,subject,title,expected_minutes,priority,status,
        position,source_type,source_ref,task_type) VALUES('context-item','context-plan',?,'PRIVATE task',15,
        1,'active',0,'junior_exam_goal','test-cycle',?)""", (subject, task_type))
    change(db, """INSERT INTO plan_constraint_decision(decision_id,plan_id,obligation_type,source_ref,
        decision,reason,metadata_json,created_at) VALUES('context-binding','context-plan','junior_exam_goal',
        'test-cycle','scheduled','test',?,?)""", (json.dumps({"planning_evidence": {"knowledge_node_id": node,
        "action": "clarify_then_recheck", "roadmap_source_version": "synthetic-plan-version"}}), clock[0].isoformat()))
    return "context-item"


def test_persisted_plan_node_and_task_determine_receipt_family(flow):
    score(flow)
    pid = bind_plan(flow)
    result = context(flow, plan_item_id=pid, task_type=None, subject=None, knowledge_node_id=None)
    assert result["scope"]["knowledge_node_id"] == NODE
    assert result["scope"]["task_type"] == "written"
    assert result["learning"]["status"] == "recheck_needed"
    assert result["scope"]["evidence_task_family"] == "diagnostic"


@pytest.mark.parametrize("scope", [
    {"subject": "英语", "task_type": "written"},
    {"task_type": "oral"},
    {"knowledge_node_id": "node-p4-math-fractions-decimals", "task_type": "written"},
])
def test_plan_scope_cannot_be_relabelled_by_caller(flow, scope):
    score(flow)
    result = context(flow, plan_item_id=bind_plan(flow), **scope)
    assert result["status"] == "invalid_context"
    assert result["learning"]["evidence_refs"] == []


def test_plan_child_fk_must_match_session_owner(flow):
    score(flow)
    result = context(flow, plan_item_id=bind_plan(flow, child=OTHER), task_type=None)
    assert result["status"] == "invalid_context"


def test_model_summary_and_self_report_never_supply_mastery_or_support_counts(flow):
    db, repo, _, clock, sid = flow
    # Legacy summaries have no scoring receipt, even when their count is high.
    for index in range(5):
        change(db, """INSERT INTO learning_evidence(evidence_id,child_id,session_id,knowledge_node_id,
            evidence_type,correctness,hint_level,independent,confidence,evidence_summary,occurred_at,client_action_id)
            VALUES(?,?,?,?,'model_summary',1,3,0,1,'PRIVATE MODEL: mastered',?,?)""",
            (f"summary-{index}", CHILD, sid, NODE, clock[0].isoformat(), f"summary-{index}"))
    result = controls(flow)
    assert result["audit"]["teaching_context"]["learning"]["status"] == "unknown"
    assert pacing(result) == "standard_layered"
    assert "PRIVATE" not in result["prompt_extension"]


def test_real_math_success_and_english_failure_select_different_support(flow):
    from home_study_agent.specialized_supply import SpecializedDiagnosticSupplyService
    score(flow, answer="90")
    supply = SpecializedDiagnosticSupplyService(flow[0])
    supply.enqueue_local("diag-pet-reading-vocabulary-baseline", CHILD, "english-local")
    supply.process_local_pending()
    candidate = supply.list(CHILD)["items"][0]
    item = supply.qualify_local(candidate["candidate_id"], CHILD, "english-qualify")
    flow[3][0] += timedelta(minutes=1)
    attempt = score(flow, item["diagnostic_item_id"], "wrong", "english-answer")
    english = controls(flow, subject="英语", knowledge_node_id="node-pet-reading-vocabulary")
    math = controls(flow)
    assert pacing(english) == "short_step_then_restate"
    assert pacing(math) == "standard_layered"
    assert english["audit"]["teaching_context"]["learning"]["evidence_refs"] == [attempt["evidence_id"]]
    assert not english["audit"]["teaching_context"]["mastery_claim_allowed"]
    assert math["audit"]["decisions"]["explanation_pacing"]["confidence"] == "qualified_single_response"


def test_fixed_prompt_never_contains_local_context_or_correction_payload(flow):
    score(flow)
    saved = ChildModelService(flow[0]).correct(CHILD, "interaction_tone", "brief", "PRIVATE RAW MATERIAL", None, "private")
    output = controls(flow)
    for forbidden in (CHILD, NODE, ITEM, saved["correction_id"], flow[4], "PRIVATE",
                      output["audit"]["teaching_context"]["source_version"], "recheck_needed"):
        assert forbidden not in output["prompt_extension"]
    assert "不增加提示级别" in output["prompt_extension"]
    assert output["audit"]["cloud_transmission_allowed"] is False


def test_child_preference_decision_has_distinct_version_without_becoming_evidence(flow):
    service = ChildModelService(flow[0])
    first = service.tutor_controls(CHILD, 1, None, {})
    second = service.tutor_controls(CHILD, 1, None, {"pace": "slow"})
    assert first["audit"]["teaching_context"] == second["audit"]["teaching_context"]
    assert first["audit"]["decisions"]["explanation_pacing"]["version"] != second["audit"]["decisions"]["explanation_pacing"]["version"]
    assert not second["audit"]["decisions"]["explanation_pacing"]["mastery_claim_allowed"]


def test_real_pet_plan_source_resolves_its_node_and_rejects_withdrawal(flow):
    pid = bind_plan(flow, subject="英语", node="node-pet-reading-vocabulary")
    change(flow[0], "DELETE FROM plan_constraint_decision WHERE plan_id='context-plan'")
    change(flow[0], "UPDATE plan_item SET source_type='pet_daily',source_ref='diag-pet-reading-vocabulary-baseline' WHERE plan_item_id=?", (pid,))
    result = context(flow, plan_item_id=pid, subject=None, knowledge_node_id=None, task_type=None)
    assert result["scope"]["knowledge_node_id"] == "node-pet-reading-vocabulary"
    assert result["scope"]["evidence_task_family"] == "diagnostic"
    assert result["learning"]["status"] == "unknown"
    change(flow[0], "UPDATE diagnostic_item SET status='withdrawn' WHERE diagnostic_item_id='diag-pet-reading-vocabulary-baseline'")
    assert context(flow, plan_item_id=pid, subject=None, knowledge_node_id=None, task_type=None)["status"] == "invalid_context"


def test_unbound_pet_vocabulary_cards_do_not_inherit_diagnostic_context(flow):
    score(flow)
    pid = bind_plan(flow)
    change(flow[0], "DELETE FROM plan_constraint_decision WHERE plan_id='context-plan'")
    change(flow[0], "UPDATE plan_item SET source_type='pet_daily',source_ref='pet-vocabulary:5' WHERE plan_item_id=?", (pid,))
    result = context(flow, plan_item_id=pid, task_type=None)
    assert result["learning"]["status"] == "unknown"
    assert result["scope"]["evidence_task_family"] is None


@pytest.mark.parametrize("phase", ["check_in", "classroom_recall", "homework_capture", "planning", "study", "break", "summary"])
def test_teaching_support_is_not_study_page_gated(flow, phase):
    score(flow)
    change(flow[0], "UPDATE daily_session SET state=? WHERE session_id=?", (phase, flow[4]))
    assert context(flow)["learning"]["status"] == "recheck_needed"


def test_real_tutor_self_reports_never_override_qualified_independent_answer(flow):
    score(flow, answer="90")
    db, repo, _, clock, sid = flow
    for index in range(3):
        turn = repo.save_tutor_turn(sid, None, "英语", "PRIVATE QUESTION", "PRIVATE EXPLANATION", "test", "test", f"turn-{index}")
        repo.save_tutor_feedback(turn["turn_id"], "PRIVATE self report", "still_stuck", f"feedback-{index}")
    assert pacing(controls(flow)) == "standard_layered"
    result = controls(flow, subject="英语", knowledge_node_id="node-p4-english-phonics-discrimination")
    assert result["audit"]["teaching_context"]["learning"]["status"] == "unknown"
    assert pacing(result) == "standard_layered"


def test_real_review_and_diagnostic_receipts_are_not_the_same_task_context(flow):
    flow[3][0] -= timedelta(days=3)
    first = score(flow)
    with flow[0].connect() as c:
        review = dict(c.execute("SELECT * FROM review_task WHERE source_evidence_id=?", (first["evidence_id"],)).fetchone())
    flow[3][0] += timedelta(days=2)
    reviewed = flow[2].submit_diagnostic(flow[4], review["diagnostic_item_id"], "42", "review-answer", review["review_task_id"])
    assert pacing(controls(flow, task_type="diagnostic")) == "short_step_then_restate"
    assert pacing(controls(flow, task_type="review")) == "standard_layered"
    assert context(flow, task_type="review")["learning"]["evidence_refs"] == [reviewed["evidence_id"]]


def test_source_expiry_caps_support_validity_and_revision_changes_version(flow):
    score(flow)
    now = datetime.now(timezone.utc)
    change(flow[0], """INSERT INTO knowledge_source(source_id,child_id,source_type,title,subject,grade_band,
        source_ref,license_status,review_status,content_hash,imported_at,effective_to)
        VALUES('bounded-source',?,'test','PRIVATE','数学','四年级','hsc_original_v1','local','approved',
        'bounded-v1',?,?)""", (CHILD, now.isoformat(), now.date().isoformat()))
    first = context(flow)
    assert datetime.fromisoformat(first["learning"]["expires_at"]).date() == now.date() + timedelta(days=1)
    change(flow[0], "UPDATE knowledge_source SET content_hash='bounded-v2' WHERE source_id='bounded-source'")
    revised = context(flow)
    assert revised["source_version"] != first["source_version"]
    assert revised["learning"]["status"] == "recheck_needed"
