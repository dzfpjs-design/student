"""Isolated prompt workflow tests using the existing catalog and scoring service."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from home_study_agent.database import Database
from home_study_agent.diagnostic_prompt import DiagnosticPromptError, DiagnosticPromptService
from home_study_agent.learning_state import LearningStateService
from home_study_agent.repository import StaleSessionStateError, StudyRepository


CHILD = "child-primary-4"
OTHER = "child-junior-3"
ITEM = "diag-p4-arithmetic-closed-1"
SECOND = "diag-p4-arithmetic-closed-2"


@pytest.fixture
def flow(tmp_path):
    db = Database(tmp_path / "prompts.db")
    db.initialize()
    repo = StudyRepository(db)
    repo.seed_family()
    LearningStateService(db).seed_catalog()
    sid = repo.start_session(CHILD, "start")["session_id"]
    change(db, "UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
    return db, DiagnosticPromptService(db), sid


def change(db, sql, args=()):
    with db.connect() as c:
        c.execute(sql, args)
        c.commit()


def rows(db, table):
    with db.connect() as c:
        return [dict(row) for row in c.execute(f"SELECT * FROM {table}")]


def prepare(service, sid, **kwargs):
    return service.prepare(sid, preferred_item_id=ITEM, client_action_id="ask", **kwargs)


def answer(service, sid, prompt, text="90", action="answer", **kwargs):
    return service.answer(sid, prompt_id=prompt["prompt_id"], item_version=prompt["item_version"],
                          answer=text, client_action_id=action, **kwargs)


def save_before_crash(service, sid, prompt, monkeypatch, **kwargs):
    def crash(*args, **kwargs):
        raise RuntimeError("crash after answer saved")

    with monkeypatch.context() as patch:
        patch.setattr(service, "resume", crash)
        with pytest.raises(RuntimeError, match="after answer saved"):
            answer(service, sid, prompt, **kwargs)


def test_concrete_prompt_survives_restart_and_exactly_one_attempt(flow):
    db, service, sid = flow
    result = prepare(service, sid)
    prompt = result["prompt"]
    assert result["status"] == "waiting_answer"
    assert prompt["origin_session_id"] == sid and prompt["child_id"] == CHILD
    assert prompt["item"]["prompt"]
    assert "expected_answer" not in json.dumps(result)
    restarted = DiagnosticPromptService(Database(db.path))
    assert restarted.read(sid) == result
    assert prepare(restarted, sid) == result
    scored = answer(restarted, sid, prompt)
    assert scored["status"] == "answered"
    assert scored["attempt"]["score_status"] == "verified_local"
    assert scored["attempt"]["session_id"] == sid
    assert answer(service, sid, prompt) == scored
    assert restarted.resume(sid, prompt_id=prompt["prompt_id"]) == scored
    assert len(rows(db, "diagnostic_attempt")) == 1
    assert len(rows(db, "learning_evidence")) == 1
    assert len(rows(db, "mastery_update_event")) == 1
    assert rows(db, "diagnostic_prompt")[0]["answer_json"] is None
    with pytest.raises(DiagnosticPromptError):
        answer(service, sid, prompt, text="91")
    with pytest.raises(DiagnosticPromptError):
        answer(service, sid, prompt, action="another-action")
    assert len(rows(db, "diagnostic_attempt")) == 1


@pytest.mark.parametrize("stage", ["check_in", "classroom_recall", "homework_capture", "planning", "study", "break", "reflection"])
def test_cross_stage_answer_scores_without_changing_original_session(flow, stage):
    db, service, sid = flow
    change(db, "UPDATE daily_session SET state=? WHERE session_id=?", (stage, sid))
    prompt = prepare(service, sid)["prompt"]
    scored = answer(service, sid, prompt)
    assert scored["status"] == "answered" and scored["attempt"]["score_status"] == "verified_local"
    assert scored["reason"] is None
    restarted = DiagnosticPromptService(Database(db.path))
    assert restarted.read(sid)["status"] == "answered"
    assert restarted.resume(sid, prompt_id=prompt["prompt_id"])["status"] == "answered"
    assert rows(db, "daily_session")[0]["state"] == stage
    assert len(rows(db, "diagnostic_attempt")) == 1
    assert rows(db, "diagnostic_attempt")[0]["session_id"] == sid


@pytest.mark.parametrize("mutation", [
    "UPDATE diagnostic_item SET status='inactive' WHERE diagnostic_item_id=?",
    "UPDATE diagnostic_item SET prompt='A changed question' WHERE diagnostic_item_id=?",
    "UPDATE diagnostic_item SET expected_answer_json='{}' WHERE diagnostic_item_id=?",
    "UPDATE diagnostic_item SET stimulus_asset='changed' WHERE diagnostic_item_id=?",
])
def test_changed_or_withdrawn_question_rejected_before_answer(flow, mutation):
    db, service, sid = flow
    prompt = prepare(service, sid)["prompt"]
    change(db, mutation, (ITEM,))
    assert answer(service, sid, prompt)["status"] == "invalidated"
    assert DiagnosticPromptService(Database(db.path)).read(sid)["status"] == "invalidated"
    assert rows(db, "diagnostic_attempt") == []


def test_missing_or_blank_item_is_not_waiting_and_request_is_idempotent(flow):
    db, service, sid = flow
    missing = service.prepare(sid, preferred_item_id="missing", client_action_id="none")
    assert missing["status"] == "no_item" and missing["prompt"] is None
    assert rows(db, "diagnostic_prompt") == []
    change(db, "UPDATE diagnostic_item SET prompt='  ' WHERE diagnostic_item_id=?", (ITEM,))
    assert prepare(service, sid)["status"] == "no_item"
    assert service.read(sid)["status"] == "no_item"


def test_version_hint_and_empty_answer_validation_and_pending_conflict(flow):
    db, service, sid = flow
    prompt = prepare(service, sid)["prompt"]
    with pytest.raises(DiagnosticPromptError):
        answer(service, sid, {**prompt, "item_version": "old"})
    for invalid in (True, -1, 6):
        with pytest.raises(DiagnosticPromptError):
            answer(service, sid, prompt, hint_level=invalid)
    with pytest.raises(DiagnosticPromptError):
        answer(service, sid, prompt, text=" ")
    with pytest.raises(DiagnosticPromptError):
        service.prepare(sid, preferred_item_id=SECOND, client_action_id="ask")
    with pytest.raises(DiagnosticPromptError):
        service.prepare(sid, preferred_item_id=SECOND, client_action_id="new")
    assert rows(db, "diagnostic_attempt") == []


def test_original_session_and_child_are_not_rebound(flow):
    db, service, sid = flow
    prompt = prepare(service, sid)["prompt"]
    change(db, "UPDATE daily_session SET status='completed' WHERE session_id=?", (sid,))
    other = StudyRepository(db).start_session(OTHER, "other")["session_id"]
    for invoke in (
        lambda: service.read(other, prompt_id=prompt["prompt_id"]),
        lambda: answer(service, other, prompt),
        lambda: service.resume(other, prompt_id=prompt["prompt_id"]),
        lambda: service.prepare(other, client_action_id="ask"),
    ):
        with pytest.raises(DiagnosticPromptError):
            invoke()
    assert answer(service, sid, prompt)["status"] == "invalidated"
    assert rows(db, "diagnostic_attempt") == []


def test_cancelled_question_never_scores_and_allows_new_prompt(flow):
    db, service, sid = flow
    prompt = prepare(service, sid)["prompt"]
    cancelled = service.cancel(sid, prompt_id=prompt["prompt_id"], item_version=prompt["item_version"])
    assert cancelled["status"] == "cancelled"
    assert answer(service, sid, prompt)["status"] == "cancelled"
    fresh = service.prepare(sid, preferred_item_id=SECOND, client_action_id="new")
    assert fresh["status"] == "waiting_answer" and fresh["prompt"]["prompt_id"] != prompt["prompt_id"]
    assert rows(db, "diagnostic_attempt") == []


def test_concurrent_retries_cannot_score_twice(flow):
    db, service, sid = flow
    prompt = prepare(service, sid)["prompt"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: answer(DiagnosticPromptService(db), sid, prompt), range(2)))
    assert results[0]["attempt"]["attempt_id"] == results[1]["attempt"]["attempt_id"]
    assert len(rows(db, "diagnostic_attempt")) == 1


def test_scoring_failure_rolls_back_attempt_but_retains_answer_for_restart(flow, monkeypatch):
    db, service, sid = flow
    change(db, "UPDATE daily_session SET state='reflection' WHERE session_id=?", (sid,))
    prompt = prepare(service, sid)["prompt"]
    with monkeypatch.context() as patch:
        patch.setattr(LearningStateService, "_attempt", staticmethod(lambda *args: (_ for _ in ()).throw(RuntimeError("response lost"))))
        with pytest.raises(RuntimeError, match="response lost"):
            answer(service, sid, prompt)
    assert rows(db, "diagnostic_attempt") == []
    assert rows(db, "learning_evidence") == []
    assert rows(db, "mastery_update_event") == []
    assert service.read(sid)["status"] == "answer_saved"
    result = DiagnosticPromptService(Database(db.path)).resume(sid, prompt_id=prompt["prompt_id"])
    assert result["status"] == "answered" and len(rows(db, "diagnostic_attempt")) == 1


@pytest.mark.parametrize("invalidate", [False, True])
def test_review_binding_persists_and_withdrawn_source_refuses_resume(flow, invalidate, monkeypatch):
    db, service, sid = flow
    learning = LearningStateService(db)
    source = learning.submit_diagnostic(sid, ITEM, "wrong", "source")
    review = next(row for row in rows(db, "review_task") if row["interval_label"] == "immediate")
    change(db, "UPDATE daily_session SET state='reflection' WHERE session_id=?", (sid,))
    result = service.prepare(sid, review_task_id=review["review_task_id"], client_action_id="review")
    prompt = result["prompt"]
    assert prompt["diagnostic_item_id"] == review["diagnostic_item_id"]
    assert prompt["review_task_id"] == review["review_task_id"]
    save_before_crash(service, sid, prompt, monkeypatch, text="42")
    if invalidate:
        change(db, "UPDATE learning_evidence SET confidence=.35 WHERE evidence_id=?", (source["evidence_id"],))
    resumed = DiagnosticPromptService(Database(db.path)).resume(sid, prompt_id=prompt["prompt_id"])
    assert resumed["status"] == ("invalidated" if invalidate else "answered")
    assert len(rows(db, "diagnostic_attempt")) == (1 if invalidate else 2)
    if not invalidate:
        assert resumed["attempt"]["review_task_id"] == review["review_task_id"]


def test_saved_answer_is_rechecked_and_cascade_does_not_orphan_child_data(flow, monkeypatch):
    db, service, sid = flow
    prompt = prepare(service, sid)["prompt"]
    change(db, "UPDATE daily_session SET state='reflection' WHERE session_id=?", (sid,))
    save_before_crash(service, sid, prompt, monkeypatch)
    change(db, "UPDATE diagnostic_item SET status='inactive' WHERE diagnostic_item_id=?", (ITEM,))
    change(db, "UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
    assert service.resume(sid, prompt_id=prompt["prompt_id"])["status"] == "invalidated"
    change(db, "DELETE FROM session_transition WHERE session_id=?", (sid,))
    change(db, "DELETE FROM daily_session WHERE session_id=?", (sid,))
    assert rows(db, "diagnostic_prompt") == []


def test_knowledge_version_change_invalidates_snapshot_and_never_exposes_rubric(flow):
    db, service, sid = flow
    prompt = prepare(service, sid)["prompt"]
    assert "expected_answer_json" not in prompt["item"]
    assert "explanation" not in prompt["item"]
    change(db, "UPDATE knowledge_node SET knowledge_version='revised' WHERE knowledge_node_id=?",
           (prompt["item"]["knowledge_node_id"],))
    assert answer(service, sid, prompt)["reason"] == "item_version_changed"
    assert rows(db, "diagnostic_attempt") == []


@pytest.mark.parametrize("mutation", ["replaced", "completed", "not_due", "wrong_child"])
def test_review_task_is_revalidated_not_silently_rebound(flow, mutation):
    db, service, sid = flow
    LearningStateService(db).submit_diagnostic(sid, ITEM, "wrong", "source")
    review = next(row for row in rows(db, "review_task") if row["interval_label"] == "immediate")
    prompt = service.prepare(sid, review_task_id=review["review_task_id"], client_action_id="review")["prompt"]
    sql, value = {
        "replaced": ("diagnostic_item_id", ITEM), "completed": ("status", "completed"),
        "not_due": ("due_at", "2999-01-01T00:00:00+00:00"), "wrong_child": ("child_id", OTHER),
    }[mutation]
    change(db, f"UPDATE review_task SET {sql}=? WHERE review_task_id=?", (value, review["review_task_id"]))
    assert answer(service, sid, prompt, text="42")["status"] == "invalidated"
    assert len(rows(db, "diagnostic_attempt")) == 1


def test_answer_action_id_cannot_be_rebound_to_new_prompt(flow):
    db, service, sid = flow
    prompt = prepare(service, sid)["prompt"]
    answer(service, sid, prompt)
    next_prompt = service.prepare(sid, preferred_item_id=SECOND, client_action_id="second")["prompt"]
    with pytest.raises(DiagnosticPromptError, match="answer_action_conflict"):
        answer(service, sid, next_prompt, text="42")
    assert len(rows(db, "diagnostic_attempt")) == 1


def test_saved_answer_hint_survives_restart_without_upgrading_independence(flow, monkeypatch):
    db, service, sid = flow
    change(db, "UPDATE daily_session SET state='reflection' WHERE session_id=?", (sid,))
    prompt = prepare(service, sid)["prompt"]
    save_before_crash(service, sid, prompt, monkeypatch, hint_level=2)
    DiagnosticPromptService(Database(db.path)).resume(sid, prompt_id=prompt["prompt_id"])
    evidence = rows(db, "learning_evidence")[0]
    assert evidence["hint_level"] == 2 and evidence["independent"] == 0


def test_attempt_reference_cannot_return_another_question_receipt(flow):
    db, service, sid = flow
    prompt = prepare(service, sid)["prompt"]
    answer(service, sid, prompt)
    wrong = LearningStateService(db).submit_diagnostic(sid, SECOND, "42", "unrelated")
    change(db, "UPDATE diagnostic_prompt SET attempt_id=? WHERE prompt_id=?", (wrong["attempt_id"], prompt["prompt_id"]))
    result = service.read(sid)
    assert result["status"] == "invalidated" and result["attempt"] is None
    assert answer(service, sid, prompt)["reason"] == "attempt_unavailable"
    assert len(rows(db, "diagnostic_attempt")) == 2


def test_guided_preferred_item_cannot_bypass_subject_filter(flow):
    _, service, sid = flow
    result = prepare(service, sid, subject="English")
    assert result["status"] == "no_item" and result["prompt"] is None


def test_independent_process_recovers_saved_answer_and_original_question(flow, monkeypatch):
    db, service, sid = flow
    change(db, "UPDATE daily_session SET state='reflection' WHERE session_id=?", (sid,))
    prompt = prepare(service, sid)["prompt"]
    save_before_crash(service, sid, prompt, monkeypatch)
    program = """
import json, sys
from pathlib import Path
from home_study_agent.database import Database
from home_study_agent.diagnostic_prompt import DiagnosticPromptService
service = DiagnosticPromptService(Database(Path(sys.argv[1])))
print(json.dumps(service.read(sys.argv[2])))
print(json.dumps(service.resume(sys.argv[2], prompt_id=sys.argv[3])))
"""
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    process = subprocess.run([sys.executable, "-c", program, str(db.path), sid, prompt["prompt_id"]],
                             env=env, capture_output=True, text=True, check=True, timeout=20)
    recovered, scored = [json.loads(line) for line in process.stdout.splitlines()]
    assert recovered["status"] == "answer_saved" and recovered["prompt"] == prompt
    assert recovered["attempt"] is None
    assert scored["status"] == "answered" and scored["attempt"]["session_id"] == sid
    assert len(rows(db, "diagnostic_attempt")) == 1
    assert rows(db, "daily_session")[0]["state"] == "reflection"


@pytest.mark.parametrize("stage", ["check_in", "reflection", "evolving", "completed"])
def test_legacy_submit_still_requires_study(flow, stage):
    db, _, sid = flow
    change(db, "UPDATE daily_session SET state=? WHERE session_id=?", (stage, sid))
    with pytest.raises(StaleSessionStateError):
        LearningStateService(db).submit_diagnostic(sid, ITEM, "90", "legacy")
    assert rows(db, "diagnostic_attempt") == []


@pytest.mark.parametrize("stage,status", [("evolving", "active"), ("completed", "active"),
                                          ("reflection", "completed"), ("reflection", "cancelled")])
def test_terminal_session_rejects_saved_prompt_scoring(flow, monkeypatch, stage, status):
    db, service, sid = flow
    prompt = prepare(service, sid)["prompt"]
    save_before_crash(service, sid, prompt, monkeypatch)
    change(db, "UPDATE daily_session SET state=?,status=? WHERE session_id=?", (stage, status, sid))
    assert service.resume(sid, prompt_id=prompt["prompt_id"])["status"] == "invalidated"
    assert rows(db, "diagnostic_attempt") == []


@pytest.mark.parametrize("defect", ["flag", "missing", "unsaved", "cancelled", "invalidated", "item", "version",
    "answer", "hint", "action", "review", "child", "session_child", "session", "retired", "evolving", "inactive_child"])
def test_trusted_entry_revalidates_persisted_binding_even_in_study(flow, monkeypatch, defect):
    db, service, sid = flow
    prompt = prepare(service, sid)["prompt"]
    if defect != "unsaved":
        save_before_crash(service, sid, prompt, monkeypatch)
    kwargs = dict(session_id=sid, item_id=ITEM, answer="90", hint_level=0,
        client_action_id="diagnostic-prompt-answer:" + prompt["prompt_id"], _trusted_prompt_id=prompt["prompt_id"])
    if defect in {"flag", "missing"}:
        kwargs["_trusted_prompt_id"] = True if defect == "flag" else "nonexistent"
    elif defect == "item":
        kwargs["item_id"] = SECOND
    elif defect == "answer":
        kwargs["answer"] = "91"
    elif defect == "hint":
        kwargs["hint_level"] = 1
    elif defect == "action":
        kwargs["client_action_id"] = "unbound-retry"
    elif defect == "review":
        kwargs["review_task_id"] = "unbound-review"
    elif defect == "session":
        kwargs["session_id"] = "not-the-origin"
    elif defect in {"cancelled", "invalidated"}:
        change(db, "UPDATE diagnostic_prompt SET status=? WHERE prompt_id=?", (defect, prompt["prompt_id"]))
    elif defect == "version":
        change(db, "UPDATE diagnostic_item SET prompt='Changed' WHERE diagnostic_item_id=?", (ITEM,))
    elif defect == "retired":
        change(db, "UPDATE diagnostic_item SET status='inactive' WHERE diagnostic_item_id=?", (ITEM,))
    elif defect == "child":
        change(db, "UPDATE diagnostic_prompt SET child_id=? WHERE prompt_id=?", (OTHER, prompt["prompt_id"]))
    elif defect == "session_child":
        change(db, "UPDATE daily_session SET child_id=? WHERE session_id=?", (OTHER, sid))
    elif defect == "evolving":
        change(db, "UPDATE daily_session SET state='evolving' WHERE session_id=?", (sid,))
    elif defect == "inactive_child":
        change(db, "UPDATE child_profile SET status='archived' WHERE child_id=?", (CHILD,))
    with pytest.raises(DiagnosticPromptError):
        LearningStateService(db).submit_diagnostic(**kwargs)
    assert rows(db, "diagnostic_attempt") == []


@pytest.mark.parametrize("defect", ["cancelled", "item_version", "evolving", "changed_action", "changed_answer"])
def test_existing_attempt_does_not_bypass_trusted_permission(flow, defect):
    db, service, sid = flow
    change(db, "UPDATE daily_session SET state='reflection' WHERE session_id=?", (sid,))
    prompt = prepare(service, sid)["prompt"]
    scored = answer(service, sid, prompt)
    kwargs = dict(session_id=sid, item_id=ITEM, answer="90", hint_level=0,
        client_action_id="diagnostic-prompt-answer:" + prompt["prompt_id"], _trusted_prompt_id=prompt["prompt_id"])
    learning = LearningStateService(Database(db.path))
    assert learning.submit_diagnostic(**kwargs)["attempt_id"] == scored["attempt"]["attempt_id"]
    if defect == "cancelled":
        change(db, "UPDATE diagnostic_prompt SET status='cancelled' WHERE prompt_id=?", (prompt["prompt_id"],))
    elif defect == "item_version":
        change(db, "UPDATE diagnostic_item SET prompt='Revised' WHERE diagnostic_item_id=?", (ITEM,))
    elif defect == "evolving":
        change(db, "UPDATE daily_session SET state='evolving' WHERE session_id=?", (sid,))
    elif defect == "changed_action":
        kwargs["client_action_id"] = "new-action"
    elif defect == "changed_answer":
        kwargs["answer"] = "wrong"
    with pytest.raises(DiagnosticPromptError):
        learning.submit_diagnostic(**kwargs)
    assert len(rows(db, "diagnostic_attempt")) == 1


def test_permission_check_is_inside_scoring_write_transaction(flow, monkeypatch):
    db, service, sid = flow
    change(db, "UPDATE daily_session SET state='reflection' WHERE session_id=?", (sid,))
    prompt = prepare(service, sid)["prompt"]
    original = DiagnosticPromptService._validate_scoring_permission
    checks = []

    def check_locked(connection, **kwargs):
        original(connection, **kwargs)
        competing = sqlite3.connect(db.path, timeout=0)
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                competing.execute("UPDATE diagnostic_item SET status='inactive' WHERE diagnostic_item_id=?", (ITEM,))
        finally:
            competing.close()
        checks.append(kwargs["prompt_id"])

    monkeypatch.setattr(DiagnosticPromptService, "_validate_scoring_permission", staticmethod(check_locked))
    assert answer(service, sid, prompt)["status"] == "answered"
    assert checks == [prompt["prompt_id"]]
    assert rows(db, "daily_session")[0]["state"] == "reflection"


def test_missing_prompt_store_and_bare_allow_flag_cannot_enable_scoring(flow):
    db, _, sid = flow
    change(db, "DROP TABLE diagnostic_prompt")
    learning = LearningStateService(db)
    with pytest.raises(DiagnosticPromptError, match="unknown_trusted_prompt"):
        learning.submit_diagnostic(sid, ITEM, "90", "untrusted", _trusted_prompt_id="missing")
    with pytest.raises(TypeError):
        learning.submit_diagnostic(sid, ITEM, "90", "untrusted", allow=True)
    assert rows(db, "diagnostic_attempt") == []
    assert learning.submit_diagnostic(sid, ITEM, "90", "legacy")["score_status"] == "verified_local"


@pytest.mark.parametrize("already_scored", [False, True])
def test_legacy_cannot_preempt_or_replay_reserved_prompt_action(flow, already_scored):
    db, service, sid = flow
    prompt = prepare(service, sid)["prompt"]
    if already_scored:
        answer(service, sid, prompt)
    with pytest.raises(ValueError, match="reserved action"):
        LearningStateService(db).submit_diagnostic(sid, ITEM, "90",
            "diagnostic-prompt-answer:" + prompt["prompt_id"])
    assert len(rows(db, "diagnostic_attempt")) == int(already_scored)


def test_real_cancel_after_saved_crash_rejects_scoring(flow, monkeypatch):
    db, service, sid = flow
    change(db, "UPDATE daily_session SET state='reflection' WHERE session_id=?", (sid,))
    prompt = prepare(service, sid)["prompt"]
    save_before_crash(service, sid, prompt, monkeypatch)
    service.cancel(sid, prompt_id=prompt["prompt_id"], item_version=prompt["item_version"])
    assert DiagnosticPromptService(Database(db.path)).resume(sid, prompt_id=prompt["prompt_id"])["status"] == "cancelled"
    assert rows(db, "diagnostic_attempt") == []


def test_committed_score_with_lost_response_replays_same_attempt(flow, monkeypatch):
    db, service, sid = flow
    prompt = prepare(service, sid)["prompt"]
    original = service.resume

    def lose_response(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("HTTP disconnected after commit")

    monkeypatch.setattr(service, "resume", lose_response)
    with pytest.raises(RuntimeError, match="after commit"):
        answer(service, sid, prompt)
    recorded = rows(db, "diagnostic_attempt")[0]["attempt_id"]
    replay = answer(DiagnosticPromptService(Database(db.path)), sid, prompt)
    assert replay["attempt"]["attempt_id"] == recorded
    assert len(rows(db, "diagnostic_attempt")) == 1
    assert len(rows(db, "mastery_update_event")) == 1
