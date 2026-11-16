"""Legacy persisted runs must not absorb answers after plan rescheduling."""

import pytest

from home_study_agent.database import Database
from home_study_agent.learning_state import LearningStateService
from home_study_agent.repository import StudyRepository
from home_study_agent.specialized_program import SpecializedProgramService


@pytest.fixture
def legacy_run(tmp_path):
    db = Database(tmp_path / "legacy-run.db")
    db.initialize()
    repo = StudyRepository(db)
    repo.seed_family()
    learning = LearningStateService(db)
    learning.seed_catalog()
    sid = repo.start_session("child-primary-4", "session")["session_id"]
    # Represents a run persisted before the new typed-stage start contract.
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
        c.execute("""INSERT INTO daily_plan(plan_id,child_id,session_id,plan_date,status,
            total_minutes,generator,version,created_at,client_action_id)
            VALUES('plan','child-primary-4',?,date('now'),'active',15,'fixture',1,datetime('now'),'plan')""", (sid,))
        c.execute("""INSERT INTO plan_item(plan_item_id,plan_id,subject,title,expected_minutes,
            priority,status,position,source_type,source_ref,task_type)
            VALUES('item','plan','数学','Synthetic legacy assessment',15,1,'active',1,
            'competition_math','competition_math.logic','oral')""")
        c.commit()
    service = SpecializedProgramService(db)
    run = service.start_for_plan_item("item", "run")
    attempt = learning.submit_diagnostic(sid, run["next_item_id"], "不会", "answer")
    return db, repo, service, sid, run, attempt


def reschedule(repo):
    repo.pause_plan_item("item", "pause")
    repo.reschedule_plan_item("item", "Synthetic reschedule", "reschedule")


@pytest.mark.parametrize("mutation", ["reschedule", "session_closed", "child_archived", "plan_cancelled"])
def test_inactive_original_owner_is_not_a_waiting_run_or_new_capture(legacy_run, mutation):
    db, repo, service, sid, run, attempt = legacy_run
    if mutation == "reschedule":
        reschedule(repo)
    else:
        with db.connect() as c:
            c.execute({"session_closed": "UPDATE daily_session SET status='completed'",
                       "child_archived": "UPDATE child_profile SET status='archived'",
                       "plan_cancelled": "UPDATE daily_plan SET status='cancelled'"}[mutation])
            c.commit()
    service = SpecializedProgramService(Database(db.path))
    assert service.active_guided_for_session(sid) is None
    assert service.capture_guided_attempt(sid, attempt) is None
    status = service.guided_status(run["run_id"])
    assert status["next_item_id"] is None
    assert status["execution_status"] == "blocked_owner_inactive"
    with db.connect() as c:
        assert c.execute("SELECT count(*) FROM specialized_assessment_run_attempt").fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM diagnostic_attempt").fetchone()[0] == 1


def test_plan_rescheduled_between_lookup_and_capture_is_rechecked_in_write_transaction(legacy_run, monkeypatch):
    db, repo, service, sid, run, attempt = legacy_run
    lookup = service.active_guided_for_session

    def interrupted(session_id):
        found = lookup(session_id)
        reschedule(repo)
        return found

    monkeypatch.setattr(service, "active_guided_for_session", interrupted)
    result = service.capture_guided_attempt(sid, attempt)
    assert result is None or result["next_item_id"] is None
    with db.connect() as c:
        assert c.execute("SELECT count(*) FROM specialized_assessment_run_attempt").fetchone()[0] == 0


def test_cancel_does_not_erase_a_real_preexisting_receipt(legacy_run):
    db, repo, service, sid, run, attempt = legacy_run
    original = service.capture_guided_attempt(sid, attempt)
    assert original["completed_items"] == 1
    reschedule(repo)
    service.cancel_for_plan_item("item")
    replay = SpecializedProgramService(Database(db.path)).capture_guided_attempt(sid, attempt)
    assert replay["status"] == "cancelled"
    assert replay["completed_items"] == 1 and replay["next_item_id"] is None
