from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from importlib import import_module
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest

from home_study_agent import repository as repository_module
from home_study_agent.database import Database
from home_study_agent.learning_state import LearningStateService
from home_study_agent.mistake_book import MistakeBookService
from home_study_agent.repository import PlanInputError, PlanItemStateError, StudyRepository


CHILD = "child-primary-4"
OTHER = "child-junior-3"
ITEM = "diag-p4-arithmetic-closed-1"
REVIEW_ITEM = "diag-p4-arithmetic-closed-2"
WRONG_ANSWER = "12"


def freeze_clock(monkeypatch, clock):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            instant = datetime.fromisoformat(clock[0])
            return instant.astimezone(tz) if tz else instant.astimezone().replace(tzinfo=None)

    # Reuse this clock in the restarted interpreter, including implicit projection cutoffs.
    for name in (
        "repository", "learning_state", "learning_obligations", "learning_feedback",
        "child_model", "mistake_book", "goal_program", "strategy_evolution",
        "learning_outcome_projection",
    ):
        monkeypatch.setattr(import_module(f"home_study_agent.{name}"), "datetime", FrozenDateTime)


@pytest.fixture
def flow(tmp_path, monkeypatch):
    first_day = datetime.now(timezone.utc) - timedelta(days=2)
    clock = [first_day.isoformat()]
    freeze_clock(monkeypatch, clock)
    db = Database(tmp_path / "test.db")
    db.initialize()
    repo = StudyRepository(db)
    repo.seed_family()
    learning = LearningStateService(db)
    learning.seed_catalog()
    first = repo.start_session(CHILD, str(uuid4()))["session_id"]
    with db.connect() as connection:
        connection.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (first,))
        connection.commit()
    original = learning.submit_diagnostic(first, ITEM, WRONG_ANSWER, str(uuid4()))
    assert original["correctness"] == 0
    with db.connect() as connection:
        from home_study_agent.learning_obligations import project_mastery_evidence

        projection = project_mastery_evidence(connection, CHILD, "node-p4-math-arithmetic")
        assert [row["evidence_id"] for row in projection["effective_evidence"]] == [original["evidence_id"]]
    with db.connect() as connection:
        connection.execute("UPDATE daily_session SET state='reflection' WHERE session_id=?", (first,))
        connection.commit()
    repo.complete_with_reflection(first, "", "", "", "", False, str(uuid4()))
    closure = learning.close_session_learning(first)
    review = next(row for row in closure["remediation"] if row["source_type"] == "due_review")
    assert review["diagnostic_item_id"] == REVIEW_ITEM != ITEM
    clock[0] = (first_day + timedelta(days=1)).isoformat()
    sid = repo.start_session(CHILD, str(uuid4()))["session_id"]
    with db.connect() as connection:
        connection.execute("""UPDATE daily_session SET state='homework_capture',
            homework_declared_empty=1,recall_declared_empty=1 WHERE session_id=?""", (sid,))
        connection.execute("UPDATE family_rule SET latest_end_time=NULL,max_learning_minutes=90 WHERE child_id=?", (CHILD,))
        connection.commit()
    plan = repo.generate_plan(sid, str(uuid4()))
    item = next(row for row in plan["plan_items"] if row["source_type"] == "due_review")
    assert item["source_ref"] == review["source_ref"]
    with db.connect() as connection:
        connection.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
        connection.commit()
    return {"db": db, "repo": repo, "learning": learning, "clock": clock,
            "session": sid, "original_session": first, "original": original,
            "item": item, "review": review}


def start(flow):
    return flow["repo"].start_plan_item(flow["item"]["plan_item_id"], str(uuid4()))


def answer(flow, text=WRONG_ANSWER):
    return flow["learning"].submit_diagnostic(
        flow["session"], flow["review"]["diagnostic_item_id"], text,
        str(uuid4()), flow["review"]["source_ref"],
    )


def complete(flow, evidence="voice_confirmed", action=None):
    return flow["repo"].complete_plan_item(
        flow["item"]["plan_item_id"], 2, action or str(uuid4()), evidence,
        completion_checklist=["review_completed", "self_tested"] if evidence == "child_checked" else None,
    )


def sql(flow, query, args=()):
    with flow["db"].connect() as connection:
        connection.execute(query, args)
        connection.commit()


@pytest.mark.parametrize("evidence", ["child_confirmed", "voice_confirmed", "child_checked"])
def test_no_answer_cannot_complete_even_with_all_checks(flow, evidence):
    start(flow)
    with pytest.raises(PlanInputError):
        complete(flow, evidence)
    with flow["db"].connect() as connection:
        assert connection.execute("SELECT status FROM plan_item WHERE plan_item_id=?", (flow["item"]["plan_item_id"],)).fetchone()[0] == "active"
        assert connection.execute("SELECT count(*) FROM diagnostic_attempt WHERE session_id=?", (flow["session"],)).fetchone()[0] == 0


def test_exact_question_read_only_and_wrong_answer_restart_recovery(flow):
    start(flow)
    with flow["db"].connect() as connection:
        before = list(connection.iterdump())
    question = flow["repo"].plan_review_question(flow["item"]["plan_item_id"])
    assert question["review_task_id"] == flow["item"]["source_ref"]
    assert question["diagnostic_item_id"] == flow["review"]["diagnostic_item_id"]
    assert question["child_id"] == CHILD and question["session_id"] == flow["session"]
    assert question["review_status"] == "pending" and question["attempt"] is None
    assert "expected_answer_json" not in question and "explanation" not in question
    with flow["db"].connect() as connection:
        assert list(connection.iterdump()) == before
    attempt = answer(flow)
    assert attempt["correctness"] < 1
    restarted = StudyRepository(Database(flow["db"].path))
    restored = restarted.plan_review_question(flow["item"]["plan_item_id"])
    assert restored["review_status"] == "completed"
    assert restored["attempt"]["attempt_id"] == attempt["attempt_id"]
    done = restarted.complete_plan_item(flow["item"]["plan_item_id"], 2, "finish-after-restart", "child_checked")
    assert done["status"] == "completed"
    assert restarted.complete_plan_item(flow["item"]["plan_item_id"], 2, "finish-after-restart") == done
    assert restarted.complete_plan_item(flow["item"]["plan_item_id"], 2, "retry-new-action") == done
    assert restarted.plan_review_question(flow["item"]["plan_item_id"])["attempt"] == restored["attempt"]
    with flow["db"].connect() as connection:
        assert connection.execute("SELECT count(*) FROM review_task WHERE child_id=? AND status='pending' AND interval_label='next_day'", (CHILD,)).fetchone()[0] >= 1
        assert connection.execute("SELECT count(*) FROM diagnostic_attempt WHERE session_id=?", (flow["session"],)).fetchone()[0] == 1
        events = connection.execute("SELECT payload_json FROM plan_item_event WHERE plan_item_id=? AND event_type='completed'", (flow["item"]["plan_item_id"],)).fetchall()
        assert len(events) == 1
        assert json.loads(events[0][0])["review_receipt"]["evidence_id"] == attempt["evidence_id"]


@pytest.mark.parametrize("mutation", ["other_child", "future", "inactive_item", "inactive_node", "missing_source"])
def test_start_rejects_invalid_review_source(flow, mutation):
    review_id = flow["review"]["source_ref"]
    if mutation == "other_child":
        sql(flow, "UPDATE review_task SET child_id=? WHERE review_task_id=?", (OTHER, review_id))
    elif mutation == "future":
        future = (datetime.fromisoformat(flow["clock"][0]) + timedelta(days=1)).isoformat()
        sql(flow, "UPDATE review_task SET due_at=? WHERE review_task_id=?", (future, review_id))
    elif mutation == "inactive_item":
        sql(flow, "UPDATE diagnostic_item SET status='inactive' WHERE diagnostic_item_id=?", (flow["review"]["diagnostic_item_id"],))
    elif mutation == "inactive_node":
        sql(flow, "UPDATE knowledge_node SET status='inactive' WHERE knowledge_node_id=(SELECT knowledge_node_id FROM review_task WHERE review_task_id=?)", (review_id,))
    else:
        with flow["db"].connect() as connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("DELETE FROM learning_evidence WHERE evidence_id=?", (flow["original"]["evidence_id"],))
            connection.commit()
    with pytest.raises(PlanInputError):
        start(flow)


@pytest.mark.parametrize("mutation", ["session", "child", "review", "item", "result", "empty", "old", "evidence_session", "evidence_node"])
def test_completion_rejects_unrelated_or_invalid_receipt(flow, mutation):
    start(flow)
    attempt = answer(flow)
    if mutation == "session":
        sql(flow, "UPDATE diagnostic_attempt SET session_id=? WHERE attempt_id=?", (flow["original_session"], attempt["attempt_id"]))
    elif mutation == "child":
        sql(flow, "UPDATE diagnostic_attempt SET child_id=? WHERE attempt_id=?", (OTHER, attempt["attempt_id"]))
    elif mutation == "review":
        sql(flow, "UPDATE diagnostic_attempt SET review_task_id=NULL WHERE attempt_id=?", (attempt["attempt_id"],))
    elif mutation == "item":
        sql(flow, "UPDATE diagnostic_attempt SET diagnostic_item_id=? WHERE attempt_id=?", (ITEM, attempt["attempt_id"]))
    elif mutation == "result":
        sql(flow, "UPDATE review_task SET result_evidence_id=? WHERE review_task_id=?", (flow["original"]["evidence_id"], flow["review"]["source_ref"]))
    elif mutation == "empty":
        sql(flow, "UPDATE diagnostic_attempt SET submitted_answer='   ' WHERE attempt_id=?", (attempt["attempt_id"],))
    elif mutation == "old":
        old = (datetime.fromisoformat(flow["clock"][0]) - timedelta(days=1)).isoformat()
        sql(flow, "UPDATE diagnostic_attempt SET created_at=? WHERE attempt_id=?", (old, attempt["attempt_id"]))
    elif mutation == "evidence_session":
        sql(flow, "UPDATE learning_evidence SET session_id=? WHERE evidence_id=?", (flow["original_session"], attempt["evidence_id"]))
    else:
        sql(flow, "UPDATE learning_evidence SET knowledge_node_id='node-p4-math-fractions-decimals' WHERE evidence_id=?", (attempt["evidence_id"],))
    with pytest.raises(PlanInputError):
        complete(flow)
    with pytest.raises(PlanInputError):
        flow["repo"].plan_review_question(flow["item"]["plan_item_id"])


def exclude_source(flow):
    with flow["db"].connect() as connection:
        node = connection.execute("SELECT knowledge_node_id FROM learning_evidence WHERE evidence_id=?", (flow["original"]["evidence_id"],)).fetchone()[0]
    mistakes = MistakeBookService(flow["db"])
    captured = mistakes.capture(child_id=CHILD, knowledge_node_id=node, subject="math", title="整数四则运算",
        prompt_summary="计算 (24+6)×3", submitted_answer=WRONG_ANSWER, source_type="diagnostic",
        source_ref=flow["original"]["attempt_id"], confidence=0.8, client_action_id=str(uuid4()),
        diagnostic_item_id=ITEM, source_evidence_id=flow["original"]["evidence_id"])
    mistakes.confirm(captured["mistake_id"], CHILD, "exclude", str(uuid4()))


@pytest.mark.parametrize("stage", ["start", "question", "completion"])
def test_excluded_original_source_invalidates_bound_review(flow, stage):
    if stage != "start":
        start(flow)
    if stage == "completion":
        answer(flow)
    exclude_source(flow)
    with pytest.raises((PlanInputError, PlanItemStateError)):
        if stage == "start":
            start(flow)
        elif stage == "question":
            flow["repo"].plan_review_question(flow["item"]["plan_item_id"])
        else:
            complete(flow)


def test_unbound_answer_same_question_is_not_review_completion(flow):
    start(flow)
    flow["learning"].submit_diagnostic(flow["session"], flow["review"]["diagnostic_item_id"], WRONG_ANSWER, str(uuid4()))
    with pytest.raises(PlanInputError):
        complete(flow)


def test_question_requires_started_item_and_active_owned_session(flow):
    with pytest.raises(PlanItemStateError):
        flow["repo"].plan_review_question(flow["item"]["plan_item_id"])
    start(flow)
    sql(flow, "UPDATE daily_session SET child_id=? WHERE session_id=?", (OTHER, flow["session"]))
    with pytest.raises(PlanInputError):
        flow["repo"].plan_review_question(flow["item"]["plan_item_id"])


def test_concurrent_start_and_completion_have_single_event_and_conflicting_action_rejected(flow):
    item_id = flow["item"]["plan_item_id"]
    with ThreadPoolExecutor(2) as pool:
        started = list(pool.map(lambda _: StudyRepository(flow["db"]).start_plan_item(item_id, "start-once"), range(2)))
    assert started[0] == started[1]
    with pytest.raises(PlanInputError):
        complete(flow, action="start-once")
    with pytest.raises(PlanInputError):
        flow["repo"].start_plan_item("unrelated-item", "start-once")
    answer(flow)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: complete(flow, action="complete-once"), range(2)))
    assert results[0] == results[1]
    with pytest.raises(PlanInputError):
        flow["repo"].complete_plan_item("unrelated-item", 2, "complete-once")
    with flow["db"].connect() as connection:
        events = connection.execute("SELECT event_type,count(*) FROM plan_item_event WHERE plan_item_id=? GROUP BY event_type", (item_id,)).fetchall()
        assert dict(events) == {"started": 1, "completed": 1}


def test_new_python_process_recovers_receipt_and_finishes_without_resubmission(flow):
    start(flow)
    attempt = answer(flow)
    script = """
import json, sys
from pathlib import Path
import pytest
from test_due_review_plan_completion import freeze_clock
from home_study_agent import repository
from home_study_agent.database import Database
patch = pytest.MonkeyPatch()
freeze_clock(patch, [sys.argv[3]])
repo = repository.StudyRepository(Database(Path(sys.argv[1])))
question = repo.plan_review_question(sys.argv[2])
completed = repo.complete_plan_item(sys.argv[2], 2, 'new-process-complete')
print(json.dumps({'question': question, 'status': completed['status']}))
"""
    env = dict(os.environ, PYTHONPATH=os.pathsep.join((
        str(Path(repository_module.__file__).resolve().parent.parent), str(Path(__file__).resolve().parent),
    )))
    output = subprocess.check_output(
        [sys.executable, "-B", "-c", script, str(flow["db"].path),
         flow["item"]["plan_item_id"], flow["clock"][0]],
        env=env, text=True, timeout=20,
    )
    recovered = json.loads(output)
    assert recovered["status"] == "completed"
    assert recovered["question"]["attempt"]["attempt_id"] == attempt["attempt_id"]
    with flow["db"].connect() as connection:
        assert connection.execute("SELECT count(*) FROM diagnostic_attempt WHERE session_id=?", (flow["session"],)).fetchone()[0] == 1


def test_http_bound_question_answer_and_recovery_complete(flow):
    from test_app import client_for

    item_id = flow["item"]["plan_item_id"]
    sid = flow["session"]
    with client_for(flow["db"].path.parent) as client:
        started = client.post(f"/v1/plan-items/{item_id}/start", json={"client_action_id": "http-review-start"})
        assert started.status_code == 200, started.text
        denied = client.post(f"/v1/plan-items/{item_id}/complete", json={
            "actual_minutes": 1, "completion_evidence_type": "child_checked",
            "completion_checklist": ["review_completed", "self_tested"], "client_action_id": "http-no-answer",
        })
        assert denied.status_code == 422, denied.text
        question = client.get(f"/v1/plan-items/{item_id}/review-question")
        assert question.status_code == 200, question.text
        data = question.json()
        assert data["review_task_id"] == flow["item"]["source_ref"]
        submitted = client.post(f"/v1/sessions/{sid}/diagnostics/answer", json={
            "diagnostic_item_id": data["diagnostic_item_id"], "review_task_id": data["review_task_id"],
            "answer": WRONG_ANSWER, "client_action_id": "http-real-answer",
        })
        assert submitted.status_code == 200, submitted.text
        recovered = client.get(f"/v1/plan-items/{item_id}/review-question")
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["review_status"] == "completed"
        assert recovered.json()["attempt"]["correctness"] < 1
        completed = client.post(f"/v1/plan-items/{item_id}/complete", json={
            "actual_minutes": 2, "client_action_id": "http-complete-answer",
        })
        assert completed.status_code == 200, completed.text
        assert completed.json()["status"] == "completed"


def test_paused_plan_still_exposes_same_question_after_repository_restart(flow):
    start(flow)
    flow["repo"].pause_plan_item(flow["item"]["plan_item_id"], "pause-review")
    repo = StudyRepository(Database(flow["db"].path))
    assert repo.plan_review_question(flow["item"]["plan_item_id"])["review_task_id"] == flow["item"]["source_ref"]


def test_start_rejects_session_child_mismatch(flow):
    sql(flow, "UPDATE daily_session SET child_id=? WHERE session_id=?", (OTHER, flow["session"]))
    with pytest.raises(PlanInputError):
        start(flow)
