from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4

import pytest

from home_study_agent import learning_state as module
from home_study_agent import repository as repository_module
from home_study_agent.database import Database
from home_study_agent.learning_state import LearningStateService
from home_study_agent.repository import StudyRepository


CHILD = "child-junior-3"
OTHER = "child-primary-4"
ITEM = "diag-j3-discriminant-1"
EXACT_ITEM = "diag-p4-arithmetic-closed-1"


@pytest.fixture
def db(tmp_path, monkeypatch):
    now = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(module, "utc_now", lambda: now.isoformat())
    monkeypatch.setattr(repository_module, "utc_now", lambda: module.utc_now())
    database = Database(tmp_path / "closure.db")
    database.initialize()
    StudyRepository(database).seed_family()
    LearningStateService(database).seed_catalog()
    return database


def session(db, child=CHILD):
    sid = StudyRepository(db).start_session(child, str(uuid4()))["session_id"]
    with db.connect() as connection:
        connection.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
        connection.commit()
    return sid


def finish(db, sid, learned="", difficult=""):
    with db.connect() as connection:
        connection.execute("UPDATE daily_session SET state='reflection' WHERE session_id=?", (sid,))
        connection.commit()
    StudyRepository(db).complete_with_reflection(sid, learned, difficult, "", "", False, str(uuid4()))
    with db.connect() as connection:
        connection.execute("UPDATE daily_session SET ended_at=? WHERE session_id=?", (module.utc_now(), sid))
        connection.commit()


def wrong(db, sid, action=None, *, item_id=ITEM):
    return LearningStateService(db).submit_diagnostic(sid, item_id, "我暂时不知道", action or str(uuid4()))


def test_closure_schedules_existing_review_mechanism_and_survives_restart(db):
    sid = session(db, OTHER)
    attempt = wrong(db, sid, item_id=EXACT_ITEM)
    finish(db, sid, "听了混合运算讲解", "还不会确定计算顺序")
    state = LearningStateService(db)
    result = state.close_session_learning(sid)
    assert result["cloud_transmission_allowed"] is False
    assert result["learning_date"] == "2026-09-05"
    assert result["weak_points"] and result["remediation"]
    planned = next(item for item in result["remediation"] if item["source_type"] == "due_review")
    assert planned["source_evidence_id"] == attempt["evidence_id"]
    assert planned["due_at"] == "2026-09-05T16:00:00+00:00"
    assert planned["status"] == "pending" and planned["mastery_update_allowed"] is False
    reopened = LearningStateService(Database(db.path))
    assert reopened.close_session_learning(sid) == result
    assert reopened.daily_learning_summary(OTHER, "2026-09-05")["sessions"][0] == result
    with db.connect() as connection:
        assert connection.execute("SELECT count(*) FROM review_task WHERE interval_label='next_day'").fetchone()[0] == 1


def test_self_report_and_explanation_never_raise_mastery_or_make_verified_weakness(db):
    sid = session(db)
    finish(db, sid, "我学会了判别式", "可能还不懂判别式")
    service = LearningStateService(db)
    service.derive_reflection_evidence(sid)
    result = service.close_session_learning(sid)
    assert result["weak_points"] == [] and result["remediation"] == []
    assert result["needs_verification"]
    with db.connect() as connection:
        assert connection.execute("SELECT count(*) FROM mastery_update_event").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM mastery_state").fetchone()[0] == 0


def test_closure_child_isolation_idempotent_concurrency_and_failure_recovery(db, monkeypatch):
    sid = session(db)
    wrong(db, sid)
    finish(db, sid)
    service = LearningStateService(db)
    service.ensure_closure_schema()
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: LearningStateService(db).close_session_learning(sid), range(2)))
    assert results[0] == results[1]
    assert service.daily_learning_summary(OTHER, "2026-09-05")["sessions"] == []
    other = session(db, OTHER)
    finish(db, other, learned="今天练了英语朗读")
    original = service._closure_material

    def fail(*args, **kwargs):
        raise RuntimeError("不要记录孩子原文")

    monkeypatch.setattr(service, "_closure_material", fail)
    result = service.process_pending_closures()
    assert result["failed"] and "孩子原文" not in str(result)
    monkeypatch.setattr(service, "_closure_material", original)
    service.close_session_learning(other)
    assert service.daily_learning_summary(OTHER, "2026-09-05")["sessions"]


def test_retest_requires_due_date_and_real_answer_and_failed_retest_reschedules(db, monkeypatch):
    sid = session(db, OTHER)
    wrong(db, sid, item_id=EXACT_ITEM)
    finish(db, sid)
    service = LearningStateService(db)
    closure = service.close_session_learning(sid)
    review = next(item for item in closure["remediation"] if item["source_type"] == "due_review")
    next_sid = session(db, OTHER)
    with pytest.raises(ValueError, match="due|尚未"):
        service.submit_diagnostic(next_sid, review["diagnostic_item_id"], "不知道", "too-early", review["source_ref"])
    monkeypatch.setattr(module, "utc_now", lambda: "2026-09-06T12:00:00+00:00")
    response = service.submit_diagnostic(next_sid, review["diagnostic_item_id"], "不知道", "real-retest", review["source_ref"])
    assert response["correctness"] < 1
    with db.connect() as connection:
        assert connection.execute("SELECT count(*) FROM review_task WHERE interval_label='next_day' AND status='pending'").fetchone()[0] == 1


def test_reflection_lost_response_retry_same_payload_and_conflicts(db):
    sid = session(db)
    repository = StudyRepository(db)
    with db.connect() as connection:
        connection.execute("UPDATE daily_session SET state='reflection' WHERE session_id=?", (sid,))
        connection.commit()
    args = (sid, "判别式", "符号", "还好", "明天再试", False, "closure-action")
    first = repository.complete_with_reflection(*args)
    assert StudyRepository(Database(db.path)).complete_with_reflection(*args) == first
    with pytest.raises(ValueError, match="conflict"):
        repository.complete_with_reflection(sid, "不同内容", *args[2:])
    other = session(db, OTHER)
    with pytest.raises(ValueError, match="conflict"):
        repository.complete_with_reflection(other, *args[1:])
    with db.connect() as connection:
        assert connection.execute("SELECT count(*) FROM daily_reflection WHERE session_id=?", (sid,)).fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM report_job WHERE session_id=?", (sid,)).fetchone()[0] == 1


def test_reflection_pauses_owned_objective_and_next_session_repairs_legacy_active(db):
    sid = session(db)
    now = module.utc_now()
    with db.connect() as connection:
        connection.execute(
            """INSERT INTO agent_objective(
                 objective_id,child_id,session_id,objective_type,summary,
                 success_criteria_json,phase,status,created_at,updated_at
               ) VALUES('closing-objective',?,?,?,?,?,'check','active',?,?)""",
            (CHILD, sid, 'learning', 'finish today', '[]', now, now),
        )
        connection.execute("UPDATE daily_session SET state='reflection' WHERE session_id=?", (sid,))
        connection.commit()

    repo = StudyRepository(db)
    args = (sid, '', '', '', '', True, 'objective-close-reflection')
    repo.complete_with_reflection(*args)
    with db.connect() as connection:
        row = connection.execute(
            "SELECT status,completed_at FROM agent_objective WHERE objective_id='closing-objective'"
        ).fetchone()
        assert tuple(row) == ('paused', None)
        # Simulate a database produced by a pre-fix Windows build.
        connection.execute(
            "UPDATE agent_objective SET status='active' WHERE objective_id='closing-objective'"
        )
        connection.commit()

    next_sid = repo.start_session(CHILD, 'after-legacy-objective')['session_id']
    with db.connect() as connection:
        assert connection.execute(
            "SELECT status FROM agent_objective WHERE objective_id='closing-objective'"
        ).fetchone()[0] == 'paused'
        assert connection.execute(
            "SELECT status FROM daily_session WHERE session_id=?", (next_sid,)
        ).fetchone()[0] == 'active'

    # A replay of the original close request remains idempotent and repairs
    # the same stale state without creating another report or reflection.
    with db.connect() as connection:
        connection.execute(
            "UPDATE agent_objective SET status='active' WHERE objective_id='closing-objective'"
        )
        connection.commit()
    repo.complete_with_reflection(*args)
    with db.connect() as connection:
        assert connection.execute(
            "SELECT status FROM agent_objective WHERE objective_id='closing-objective'"
        ).fetchone()[0] == 'paused'
        assert connection.execute(
            "SELECT count(*) FROM daily_reflection WHERE session_id=?", (sid,)
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT count(*) FROM report_job WHERE session_id=?", (sid,)
        ).fetchone()[0] == 1


def test_preview_is_read_only_and_today_combines_multiple_sessions(db):
    sid = session(db, OTHER)
    wrong(db, sid, item_id=EXACT_ITEM)
    service = LearningStateService(db)
    with db.connect() as connection:
        before = connection.total_changes
        assert not connection.execute("SELECT 1 FROM sqlite_master WHERE name='learning_session_closure'").fetchone()
    preview = service.preview_session_learning(sid)
    assert preview["session_status"] == "active" and preview["local_only"]
    assert preview["remediation"][0]["status"] == "needs_scheduling"
    assert "已留下一项" not in preview["child_response"] and preview["child_response"].endswith("？")
    finish(db, sid)
    service.close_session_learning(sid)
    another = session(db, OTHER)
    service.submit_diagnostic(another, "diag-p4-fraction-decimal-closed-1", "还不会", "second-topic")
    today = service.daily_learning_summary(OTHER, "2026-09-05")
    assert len(today["sessions"]) == 2 and today["session_status"] == "active"
    assert {item["source"]["session_id"] for item in today["learning_content"]} == {sid, another}
    assert len(today["child_response"]) < 350
    assert "独立作答和延迟复测继续核对" not in today["child_response"]


def test_same_session_cross_midnight_uses_each_evidence_date_after_restart(db, monkeypatch):
    service = LearningStateService(db)
    monkeypatch.setattr(module, "utc_now", lambda: "2026-09-05T15:55:00+00:00")
    sid = session(db)
    first = wrong(db, sid)
    monkeypatch.setattr(module, "utc_now", lambda: "2026-09-05T16:05:00+00:00")
    restarted = LearningStateService(Database(db.path))
    assert restarted.daily_learning_summary(CHILD, "2026-09-06")["learning_content"] == []
    assert len(restarted.daily_learning_summary(CHILD, "2026-09-05")["learning_content"]) == 1
    second = service.submit_diagnostic(sid, "diag-j3-chinese-reading-1", "不知道", "after-midnight")
    finish(db, sid)
    result = service.close_session_learning(sid)
    assert result["learning_date"] == "2026-09-06"  # Scheduling date only.
    assert not result["child_response"].endswith("？")
    old = restarted.daily_learning_summary(CHILD, "2026-09-05")
    new = restarted.daily_learning_summary(CHILD, "2026-09-06")
    assert {item["source"]["source_id"] for item in old["learning_content"]} == {first["evidence_id"]}
    assert {item["source"]["source_id"] for item in new["learning_content"]} == {second["evidence_id"]}


def test_next_day_plan_consumes_real_review_and_does_not_complete_from_explanation(db, monkeypatch):
    sid = session(db, OTHER)
    wrong(db, sid, item_id=EXACT_ITEM)
    finish(db, sid)
    service = LearningStateService(db)
    review = service.close_session_learning(sid)["remediation"][0]
    monkeypatch.setattr(module, "utc_now", lambda: "2026-09-06T12:00:00+00:00")
    repository = StudyRepository(db)
    next_sid = session(db, OTHER)
    with db.connect() as connection:
        connection.execute("UPDATE daily_session SET state='homework_capture',homework_declared_empty=1,recall_declared_empty=1 WHERE session_id=?", (next_sid,))
        connection.execute("UPDATE family_rule SET latest_end_time='23:59',max_learning_minutes=90 WHERE child_id=?", (OTHER,))
        connection.commit()
    workspace = repository.generate_plan(next_sid, "next-day-plan")
    due = [item for item in workspace["plan_items"] if item["source_type"] == "due_review"]
    assert len(due) == 1 and due[0]["source_ref"] == review["source_ref"]
    with db.connect() as connection:
        connection.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (next_sid,))
        connection.commit()
    repository.start_plan_item(due[0]["plan_item_id"], "start-next-day")
    with pytest.raises(repository_module.PlanInputError):
        repository.complete_plan_item(due[0]["plan_item_id"], 10, "explanation-is-not-answer", "voice_confirmed", "我听懂了")
    upcoming = service.next_diagnostic(OTHER, "school")
    assert upcoming["review_task_id"] == review["source_ref"]
    service.submit_diagnostic(next_sid, review["diagnostic_item_id"], "不知道", "answer-next-day", review["source_ref"])
    completed = repository.complete_plan_item(due[0]["plan_item_id"], 10, "completed-answer", "child_checked", "仍需核对")
    assert completed["status"] == "completed"


@pytest.mark.parametrize("answer", ["我暂时不知道", "0，一个不同的实数根"])
def test_unqualified_legacy_discriminant_answer_explicitly_requires_review(db, answer):
    from home_study_agent.learning_obligations import project_mastery_evidence

    sid = session(db)
    service = LearningStateService(db)
    receipt = service.submit_diagnostic(sid, ITEM, answer, "legacy-answer")
    with db.connect() as connection:
        evidence = connection.execute("SELECT * FROM learning_evidence WHERE evidence_id=?", (receipt["evidence_id"],)).fetchone()
        assert evidence["confidence"] < .8
        projection = project_mastery_evidence(connection, CHILD, "node-j3-math-discriminant", as_of=module.utc_now())
        assert projection["effective_evidence"] == []
        assert projection["excluded_evidence"][0]["evidence_id"] == receipt["evidence_id"]
        assert connection.execute("SELECT count(*) FROM mastery_state WHERE child_id=?", (CHILD,)).fetchone()[0] == 0
    preview = service.preview_session_learning(sid)
    finish(db, sid)
    closure = service.close_session_learning(sid)
    reopened = LearningStateService(Database(db.path)).close_session_learning(sid)
    assert reopened == closure
    for result in (preview, closure, reopened):
        assert result["weak_points"] == [] and result["remediation"] == []
        pending = [r for r in result["needs_verification"] if r["source"]["source_id"] == receipt["evidence_id"]]
        assert pending, "Unqualified diagnostic answer must be visibly pending verification, not silently discarded"
        assert all(r["next_action"] and r["mastery_update_allowed"] is False for r in pending)


def test_diagnostic_action_isolation_and_atomic_closure_failure_after_scheduling(db, monkeypatch):
    sid = session(db)
    wrong(db, sid, "shared-answer-action")
    finish(db, sid)
    other = session(db, OTHER)
    with pytest.raises(ValueError, match="conflict"):
        wrong(db, other, "shared-answer-action")
    service = LearningStateService(db)
    original = service._closure_actions

    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("private closure detail")

    monkeypatch.setattr(service, "_closure_actions", fail)
    with pytest.raises(RuntimeError):
        service.close_session_learning(sid)
    with db.connect() as connection:
        assert connection.execute("SELECT count(*) FROM review_task WHERE interval_label='next_day'").fetchone()[0] == 0
        assert connection.execute("SELECT status FROM daily_session WHERE session_id=?", (sid,)).fetchone()[0] == "completed"
        assert "private closure detail" not in str([tuple(row) for row in connection.execute("SELECT * FROM learning_session_closure")])
