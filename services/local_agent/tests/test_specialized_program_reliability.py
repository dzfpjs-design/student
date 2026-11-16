from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4

import pytest

from home_study_agent.database import Database
from home_study_agent.learning_state import LearningStateService
from home_study_agent.mistake_book import MistakeBookService
from home_study_agent.repository import StudyRepository, utc_now
from home_study_agent.specialized_program import SpecializedProgramError, SpecializedProgramService
from home_study_agent.specialized_supply import SpecializedDiagnosticSupplyService


CHILD = "child-junior-3"
OTHER = "child-primary-4"
DOMAIN = "junior_exam.数学.方程与不等式"


@pytest.fixture
def flow(tmp_path):
    db = Database(tmp_path / "specialized-reliability.db")
    db.initialize()
    repo = StudyRepository(db)
    repo.seed_family()
    learning = LearningStateService(db)
    learning.seed_catalog()
    supply = SpecializedDiagnosticSupplyService(db)
    supply.enqueue_local("diag-j3-discriminant-1", CHILD, "exact-queue", count=3)
    supply.process_local_pending()
    queue = []
    for item in supply.list(CHILD)["items"]:
        promoted = supply.qualify_local(item["candidate_id"], CHILD, str(uuid4()))
        queue.append({"diagnostic_item_id": promoted["diagnostic_item_id"], "domain_id": DOMAIN})
    sid = repo.start_session(CHILD, "specialized-session")["session_id"]
    service = SpecializedProgramService(db)
    service.ensure_schema()
    run_id = str(uuid4())
    with db.connect() as connection:
        connection.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
        # Persist a preselected teaching queue; scores still come only from submit_diagnostic.
        connection.execute(
            """INSERT INTO specialized_assessment_run(run_id,child_id,session_id,track,catalog_track,
            subject,phase,item_queue_json,status,started_at,client_action_id)
            VALUES (?,?,?,'junior_exam','school','数学','baseline',?,'active',?,?)""",
            (run_id, CHILD, sid, json.dumps(queue), utc_now(), run_id),
        )
        connection.commit()
    return db, service, learning, sid, run_id, queue


def answer(flow, index, *, correct=True, hint_level=0, capture=True):
    db, service, learning, sid, _, queue = flow
    item_id = queue[index]["diagnostic_item_id"]
    with db.connect() as connection:
        rubric = json.loads(connection.execute("SELECT expected_answer_json FROM diagnostic_item WHERE diagnostic_item_id=?", (item_id,)).fetchone()[0])
    assert rubric["local_rule"] == "local_exact_v1"
    attempt = learning.submit_diagnostic(sid, item_id, rubric["exact_answers"][0] if correct else "不知道",
                                         str(uuid4()), hint_level=hint_level)
    if capture:
        service.capture_guided_attempt(sid, attempt)
    return attempt


def test_known_qualification_denominator_excludes_hinted_unknowns_and_one_correct_is_not_ability(flow):
    db, service, _, _, run_id, _ = flow
    answer(flow, 0)
    answer(flow, 1, hint_level=2)
    answer(flow, 2, correct=False, hint_level=2)
    status = service.guided_status(run_id)
    assert status["completed_items"] == 3
    assert status["qualified_items"] == status["qualification_denominator"] == 1
    assert status["unverified_items"] == 2
    assert status["domain_results"] == {DOMAIN: 1.0}
    assert status["reference_domain_results"][DOMAIN] == pytest.approx(2 / 3, abs=.001)
    roadmap = service.roadmap(CHILD, "junior_exam")
    domain = next(row for row in roadmap["domains"] if row["domain_id"] == DOMAIN)
    assert domain["reference_score"] == 100
    assert domain["status"] != "strong"
    assert domain["learning_evidence_count"] == 1
    assert domain["goal_attainment"] == "not_established"
    assert roadmap["reference_denominator"] == 1
    assert roadmap["unknown_domain_count"] == roadmap["total_domain_count"] - 1
    assert roadmap["reference_readiness"] == 100
    assert roadmap["mastery_claim_allowed"] is False


@pytest.mark.parametrize("mutation", ["retired", "withdrawn", "forged_score", "other_child", "other_domain"])
def test_invalidated_sources_cannot_resurrect_guided_or_reference_results(flow, mutation):
    db, service, _, _, run_id, queue = flow
    attempts = [answer(flow, i, correct=False) for i in range(3)]
    assert service.guided_status(run_id)["domain_results"] == {DOMAIN: 0.0}
    assert service.roadmap(CHILD, "junior_exam")["evidence_domain_count"] == 1
    if mutation == "withdrawn":
        mistakes = MistakeBookService(db)
        for attempt in attempts:
            case = mistakes.capture_from_attempt(attempt)
            mistakes.confirm(case["mistake_id"], CHILD, "exclude", str(uuid4()))
    else:
        with db.connect() as connection:
            if mutation == "retired":
                for item in queue:
                    connection.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id=?", (item["diagnostic_item_id"],))
            elif mutation == "forged_score":
                connection.execute("UPDATE diagnostic_attempt SET correctness=1 WHERE session_id=(SELECT session_id FROM specialized_assessment_run WHERE run_id=?)", (run_id,))
                connection.execute("UPDATE learning_evidence SET correctness=1 WHERE child_id=?", (CHILD,))
            elif mutation == "other_child":
                connection.execute("UPDATE diagnostic_attempt SET child_id=?", (OTHER,))
            else:
                connection.execute("UPDATE specialized_assessment_run_attempt SET domain_id='junior_exam.数学.函数' WHERE run_id=?", (run_id,))
            connection.commit()
    for _ in range(2):
        service = SpecializedProgramService(Database(db.path))
        status = service.guided_status(run_id)
        assert status["status"] == "completed"
        assert status["completed_items"] == 3
        assert status["next_item_id"] is None
        assert status["domain_results"] == status["reference_domain_results"] == {}
        assert status["qualified_items"] == 0
        assert status["assessment_status"] == "invalidated"
        roadmap = service.roadmap(CHILD, "junior_exam")
        if mutation != "other_domain":
            assert roadmap["evidence_domain_count"] == 0
        assert roadmap["assessment_history"][-1]["current_evidence_status"] == "invalidated"
    with db.connect() as connection:
        assert connection.execute("SELECT count(*) FROM specialized_assessment_run_attempt WHERE run_id=?", (run_id,)).fetchone()[0] == 3
        assert connection.execute("SELECT count(*) FROM specialized_assessment_event WHERE source_ref=?", (run_id,)).fetchone()[0] == 1


def test_callback_and_cached_correctness_never_override_rescored_receipt(flow):
    db, service, _, sid, run_id, _ = flow
    attempt = answer(flow, 0, correct=False, capture=False)
    status = service.capture_guided_attempt(sid, {**attempt, "correctness": 1.0, "child_id": OTHER})
    assert status["domain_results"] == {DOMAIN: 0.0}
    with db.connect() as connection:
        connection.execute("UPDATE specialized_assessment_run_attempt SET correctness=1 WHERE run_id=?", (run_id,))
        connection.commit()
    status = service.guided_status(run_id)
    assert status["domain_results"] == status["reference_domain_results"] == {DOMAIN: 0.0}


def test_foreign_session_attempt_cannot_advance_run(flow):
    db, service, learning, sid, run_id, _ = flow
    with db.connect() as connection:
        connection.execute("UPDATE daily_session SET status='completed' WHERE session_id=?", (sid,))
        connection.commit()
    other_sid = StudyRepository(db).start_session(OTHER, "foreign-session")["session_id"]
    with db.connect() as connection:
        connection.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (other_sid,))
        connection.commit()
    foreign = learning.submit_diagnostic(other_sid, "diag-p4-arithmetic-closed-1", "90", "foreign-answer")
    with pytest.raises(SpecializedProgramError):
        service.capture_guided_attempt(sid, foreign)
    assert service.guided_status(run_id)["completed_items"] == 0


def test_completed_run_recovers_missing_assessment_and_duplicate_final_feedback(flow, monkeypatch):
    db, service, _, sid, run_id, _ = flow
    answer(flow, 0)
    answer(flow, 1)
    with monkeypatch.context() as fault:
        def unavailable(*args, **kwargs):
            raise RuntimeError("injected projection write failure")
        fault.setattr(service, "record_assessment", unavailable)
        final = answer(flow, 2, capture=False)
        status = service.capture_guided_attempt(sid, final)
        assert status["status"] == "completed"
        assert status["assessment_status"] == "pending_retry"
        assert status["completed_items"] == 3
    restarted = SpecializedProgramService(Database(db.path))
    recovered = restarted.capture_guided_attempt(sid, final)
    assert recovered["run_id"] == run_id
    assert recovered["status"] == "completed"
    assert recovered["assessment_status"] == "recorded"
    assert restarted.guided_status(run_id)["assessment_id"] == recovered["assessment_id"]
    with db.connect() as connection:
        assert connection.execute("SELECT count(*) FROM specialized_assessment_event WHERE source_ref=?", (run_id,)).fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM specialized_assessment_run_attempt WHERE run_id=?", (run_id,)).fetchone()[0] == 3


def test_historical_receipt_cannot_be_attached_to_new_run(flow):
    db, service, _, sid, run_id, _ = flow
    attempt = answer(flow, 0, capture=False)
    future_start = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
    with db.connect() as connection:
        connection.execute("UPDATE specialized_assessment_run SET started_at=? WHERE run_id=?", (future_start, run_id))
        connection.commit()
    with pytest.raises(SpecializedProgramError):
        service.capture_guided_attempt(sid, attempt)
    assert service.guided_status(run_id)["completed_items"] == 0


def test_parent_action_id_cannot_return_another_child_assessment(flow):
    _, service, _, _, _, _ = flow
    service.record_assessment(CHILD, "junior_exam", "数学", "parent_exam", {DOMAIN: .8}, "shared-key")
    with pytest.raises(SpecializedProgramError):
        service.record_assessment(OTHER, "competition_math", "数学", "parent_exam", {"competition_math.logic": .8}, "shared-key")


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1.0, 1.1])
def test_nonfinite_or_out_of_range_reference_score_is_rejected(flow, value):
    _, service, _, _, _, _ = flow
    with pytest.raises(SpecializedProgramError):
        service.record_assessment(CHILD, "junior_exam", "数学", "parent_exam", {DOMAIN: value}, str(uuid4()))


def test_guided_assessment_cannot_be_forged_without_completed_owned_run(flow):
    _, service, _, _, run_id, _ = flow
    with pytest.raises(SpecializedProgramError):
        service.record_assessment(CHILD, "junior_exam", "数学", "guided_baseline", {DOMAIN: 1}, "forged-guided", source_ref=run_id)


def test_legacy_cached_guided_total_is_never_presented_as_a_current_exam_score(flow):
    db, service, _, _, run_id, _ = flow
    for index in range(3):
        answer(flow, index)
    with db.connect() as connection:
        connection.execute("UPDATE specialized_assessment_event SET score=100,max_score=100 WHERE source_ref=?", (run_id,))
        connection.commit()
    roadmap = service.roadmap(CHILD, "junior_exam")
    history = roadmap["assessment_history"][-1]
    assert history["percentage"] is None
    assert history["score"] is None
    assert history["historical_reference"] == {"score": 100, "max_score": 100}
    assert roadmap["whole_exam_analysis"]["latest_exam"] is None


def test_saved_answer_can_resume_run_after_capture_was_interrupted(flow):
    db, service, _, sid, run_id, _ = flow
    attempt = answer(flow, 0, capture=False)
    assert service.guided_status(run_id)["completed_items"] == 0
    restarted = SpecializedProgramService(Database(db.path))
    resumed = restarted.capture_guided_attempt(sid, attempt)
    assert resumed["completed_items"] == 1
    assert resumed["status"] == "active"
    assert restarted.capture_guided_attempt(sid, attempt)["completed_items"] == 1


def test_owned_tutor_assistance_is_unverified_not_failed_learning(flow):
    db, service, _, sid, run_id, queue = flow
    with db.connect() as connection:
        prompt = connection.execute("SELECT prompt FROM diagnostic_item WHERE diagnostic_item_id=?",
                                    (queue[0]["diagnostic_item_id"],)).fetchone()[0]
    StudyRepository(db).save_tutor_turn(sid, None, "数学", prompt, "fixture hint", "fixture", "fixture", "same-question-hint")
    answer(flow, 0)
    status = service.guided_status(run_id)
    assert status["completed_items"] == 1
    assert status["qualified_items"] == 0
    assert status["unverified_items"] == 1
    assert status["domain_results"] == {}
    assert status["reference_domain_results"] == {DOMAIN: 1.0}
    roadmap = service.roadmap(CHILD, "junior_exam")
    assert roadmap["reference_readiness"] is None
    assert roadmap["reference_denominator"] == 0


def test_retired_pending_question_blocks_without_erasing_progress(flow):
    db, service, _, _, run_id, queue = flow
    answer(flow, 0)
    pending_id = queue[1]["diagnostic_item_id"]
    with db.connect() as connection:
        connection.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id=?", (pending_id,))
        connection.commit()
    status = SpecializedProgramService(Database(db.path)).guided_status(run_id)
    assert status["status"] == "active"
    assert status["execution_status"] == "blocked_source_unavailable"
    assert status["completed_items"] == 1
    assert status["total_items"] == 3
    assert status["next_item_id"] is None
    assert status["blocked_item_ids"] == [pending_id]


def test_child_model_cannot_revive_retired_guided_domain_results(flow):
    from home_study_agent.child_model import ChildModelService

    db, service, _, _, run_id, queue = flow
    for index in range(3):
        answer(flow, index, correct=False)
    before = ChildModelService(db).snapshot(CHILD)["payload"]["specialized_progress"]
    assert sum(row["domain_count"] for row in before) == 1
    with db.connect() as connection:
        for item in queue:
            connection.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id=?", (item["diagnostic_item_id"],))
        connection.commit()
    assert service.guided_status(run_id)["domain_results"] == {}
    assert service.roadmap(CHILD, "junior_exam")["evidence_domain_count"] == 0
    after = ChildModelService(Database(db.path)).snapshot(CHILD)["payload"]["specialized_progress"]
    assert sum(row["domain_count"] for row in after) == 0, "ChildModel must revalidate guided sources, not reuse cached assessment JSON"


@pytest.mark.parametrize("mutation", ["retired", "withdrawn", "parent_score", "run_binding", "attachment", "child_grade"])
def test_report_reference_fingerprint_invalidates_other_session_dependencies(flow, mutation):
    from home_study_agent.learning_feedback import session_report_feedback
    from home_study_agent.specialized_program import report_reference_fingerprint

    db, service, _, source_session, run_id, queue = flow
    attempts = [answer(flow, index, correct=False) for index in range(3)]
    repo = StudyRepository(db)
    with db.connect() as connection:
        connection.execute("UPDATE daily_session SET state='reflection' WHERE session_id=?", (source_session,))
        connection.commit()
    repo.complete_with_reflection(source_session, "", "", "", "", True, "finish-source")
    current_session = repo.start_session(CHILD, "current-report-session")["session_id"]
    parent = service.record_assessment(CHILD, "junior_exam", "数学", "parent_reference",
                                       {DOMAIN: .25}, "parent-reference-for-report")
    with db.connect() as connection:
        before = report_reference_fingerprint(connection, CHILD)
        other_before = report_reference_fingerprint(connection, OTHER)
        own_report_fp = session_report_feedback(connection, current_session)["evidence_fingerprint"]
        assert before.startswith("specialized_report_reference_v1:")
    if mutation == "withdrawn":
        mistakes = MistakeBookService(db)
        for attempt in attempts:
            case = mistakes.capture_from_attempt(attempt)
            mistakes.confirm(case["mistake_id"], CHILD, "exclude", str(uuid4()))
    else:
        with db.connect() as connection:
            if mutation == "retired":
                connection.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id=?",
                                   (queue[0]["diagnostic_item_id"],))
            elif mutation == "parent_score":
                connection.execute("UPDATE specialized_assessment_event SET domain_results_json=? WHERE assessment_id=?",
                                   (json.dumps({DOMAIN: .75}), parent["assessment_id"]))
            elif mutation == "run_binding":
                connection.execute("UPDATE specialized_assessment_run SET status='cancelled' WHERE run_id=?", (run_id,))
            elif mutation == "attachment":
                connection.execute("UPDATE specialized_assessment_run_attempt SET domain_id='junior_exam.数学.函数' WHERE run_id=?", (run_id,))
            else:
                connection.execute("UPDATE child_profile SET grade='四年级' WHERE child_id=?", (CHILD,))
            connection.commit()
    with Database(db.path).connect() as connection:
        assert report_reference_fingerprint(connection, CHILD) != before
        assert report_reference_fingerprint(connection, OTHER) == other_before
        assert session_report_feedback(connection, current_session)["evidence_fingerprint"] == own_report_fp


def test_report_reference_fingerprint_is_read_only_and_has_no_rebuild_cache(flow, monkeypatch):
    import sqlite3
    from home_study_agent import specialized_program

    db, service, _, _, run_id, queue = flow
    for index in range(3):
        answer(flow, index, correct=False)
    with db.connect() as connection:
        before = specialized_program.report_reference_fingerprint(connection, CHILD)
    service.roadmap(CHILD, "junior_exam")
    with db.connect() as connection:
        assert specialized_program.report_reference_fingerprint(connection, CHILD) == before

    def forbidden(*args, **kwargs):
        raise AssertionError("fingerprint must only read the borrowed connection")

    with monkeypatch.context() as guard:
        guard.setattr(Database, "connect", forbidden)
        for name in ("ensure_schema", "roadmap", "roadmaps", "guided_status", "record_assessment"):
            guard.setattr(SpecializedProgramService, name, forbidden)
        connection = sqlite3.connect(db.path.as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("BEGIN")
            assert specialized_program.report_reference_fingerprint(connection, CHILD) == before
            assert connection.in_transaction
            assert connection.total_changes == 0
            assert connection.execute("SELECT 1").fetchone()[0] == 1
        finally:
            connection.close()
    with db.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id=?", (queue[0]["diagnostic_item_id"],))
        changed = connection.total_changes
        assert specialized_program.report_reference_fingerprint(connection, CHILD) != before
        assert connection.in_transaction and connection.total_changes == changed
        connection.rollback()
        assert specialized_program.report_reference_fingerprint(connection, CHILD) == before


def test_report_reference_fingerprint_needs_no_specialized_schema(tmp_path):
    from home_study_agent.learning_state import LearningStateService
    from home_study_agent.specialized_program import report_reference_fingerprint

    db = Database(tmp_path / "no-specialized-schema.db")
    db.initialize()
    StudyRepository(db).seed_family()
    LearningStateService(db).seed_catalog()
    with db.connect() as connection:
        tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        before = report_reference_fingerprint(connection, CHILD)
        assert report_reference_fingerprint(connection, CHILD) == before
        assert connection.total_changes == 0
        assert connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall() == tables


def test_daily_report_rebuilds_after_other_session_specialized_sources_retire(flow):
    from home_study_agent.reports import build_daily_report

    db, service, _, source_session, run_id, queue = flow
    for index in range(3):
        answer(flow, index, correct=False)
    repo = StudyRepository(db)

    def finish(session_id, action):
        with db.connect() as connection:
            connection.execute("UPDATE daily_session SET state='reflection' WHERE session_id=?", (session_id,))
            connection.commit()
        repo.complete_with_reflection(session_id, "", "", "", "", True, action)

    finish(source_session, "finish-specialized-source")
    report_session = repo.start_session(CHILD, "separate-report-session")["session_id"]
    finish(report_session, "finish-separate-report")
    summary, coaching = build_daily_report(repo.report_evidence(report_session))
    saved = repo.save_daily_report(report_session, summary, coaching)
    old_learning_fp = saved["evidence"]["reliable_learning_fingerprint"]
    old_reference_fp = saved["evidence"]["specialized_reference_fingerprint"]
    assert any("中考七科准备" in fact and "当前参考准备度0分" in fact for fact in saved["summary"]["facts"])
    assert saved["evidence_detail"]["knowledge_evidence"] == []

    with db.connect() as connection:
        for item in queue:
            connection.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id=?", (item["diagnostic_item_id"],))
        connection.commit()
    assert service.guided_status(run_id)["domain_results"] == {}
    refreshed = StudyRepository(Database(db.path)).get_daily_report(saved["report_id"])
    assert refreshed["evidence"]["reliable_learning_fingerprint"] == old_learning_fp
    assert refreshed["evidence"]["specialized_reference_fingerprint"] != old_reference_fp
    assert refreshed["summary"]["specialized_reference_fingerprint"] == refreshed["evidence"]["specialized_reference_fingerprint"]
    assert not any("当前参考准备度0分" in fact for fact in refreshed["summary"]["facts"])
    assert any("中考七科准备" in fact and "证据覆盖0%" in fact and "证据不足暂不计算准备度" in fact
               for fact in refreshed["summary"]["facts"])
    with db.connect() as connection:
        audit_count = connection.execute("SELECT count(*) FROM audit_event WHERE action='daily_report_evidence_revalidated'").fetchone()[0]
    restarted = StudyRepository(Database(db.path)).get_daily_report(saved["report_id"])
    assert restarted["summary"] == refreshed["summary"]
    assert restarted["evidence"] == refreshed["evidence"]
    with db.connect() as connection:
        assert connection.execute("SELECT count(*) FROM audit_event WHERE action='daily_report_evidence_revalidated'").fetchone()[0] == audit_count
