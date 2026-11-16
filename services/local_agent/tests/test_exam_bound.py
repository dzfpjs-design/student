"""Arrival-bound exam answers on disposable synthetic databases."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
import subprocess
import sys

import pytest

from home_study_agent.database import Database
from home_study_agent.exam_simulation import ExamSimulationError, ExamSimulationService
from home_study_agent.learning_state import LearningStateService
from home_study_agent.repository import StaleSessionStateError
from test_exam_simulation import _database


@pytest.fixture
def flow(tmp_path):
    db, sid = _database(tmp_path / "bound-exam.db")
    service = ExamSimulationService(db)
    service.specialized.ensure_schema()
    run = service.start(sid, "competition_math", "数学", "practice", 2, 30, "bound-exam-start")
    return db, service, sid, run


def change(db, sql, args=()):
    with db.connect() as c:
        c.execute(sql, args)
        c.commit()


def rows(db, table):
    with db.connect() as c:
        return [dict(r) for r in c.execute(f"SELECT * FROM {table}")]


def submit(flow, binding, answer="I am not sure", action="bound-exam-answer"):
    return flow[1].submit_bound(flow[2], **binding, answer=answer, client_action_id=action)


def test_arrival_is_read_only_concrete_bound_slot_without_answer_key(flow):
    db, service, sid, run = flow
    before = rows(db, "specialized_exam_item"), rows(db, "specialized_exam_run"), rows(db, "audit_event")
    binding = service.arrival_binding(sid)
    assert set(binding) == {"run_id", "position", "item_id", "source_version"}
    assert binding["run_id"] == run["run_id"] and binding["position"] == 1
    assert binding["item_id"] == run["current_item"]["diagnostic_item_id"]
    assert binding["source_version"] == rows(db, "specialized_exam_item")[0]["source_version"]
    assert service.arrival_binding(sid, run_id=run["run_id"]) == binding
    assert before == (rows(db, "specialized_exam_item"), rows(db, "specialized_exam_run"), rows(db, "audit_event"))


def test_late_answer_after_slot_advance_never_scores_next_question(flow):
    original = flow[1].arrival_binding(flow[2])
    first = submit(flow, original)
    later = flow[1].arrival_binding(flow[2])
    assert later["position"] == 2 and later["item_id"] != original["item_id"]
    with pytest.raises(ExamSimulationError):
        submit(flow, original, action="model-late-answer")
    assert len(rows(flow[0], "diagnostic_attempt")) == 1
    replay = submit(flow, original)
    assert replay["attempt"] == first["attempt"]
    assert flow[1].arrival_binding(flow[2]) == later
    with pytest.raises(ExamSimulationError):
        submit(flow, later)
    with pytest.raises(ExamSimulationError):
        submit(flow, original, answer="changed words")


def test_model_waiting_cancel_then_new_run_does_not_rebind_old_answer(flow):
    db, service, sid, run = flow
    arrival = service.arrival_binding(sid)
    service.cancel(run["run_id"])
    newer = service.start(sid, "competition_math", "数学", "practice", 2, 30, "replacement-exam")
    with pytest.raises(ExamSimulationError):
        submit(flow, arrival)
    assert rows(db, "diagnostic_attempt") == []
    assert service.arrival_binding(sid)["run_id"] == newer["run_id"]


@pytest.mark.parametrize("state", ["check_in", "classroom_recall", "homework_capture", "planning", "study", "break", "reflection"])
def test_start_and_trusted_bound_score_in_active_nonterminal_phases(flow, state):
    db, service, sid, run = flow
    service.cancel(run["run_id"])
    change(db, "UPDATE daily_session SET state=? WHERE session_id=?", (state, sid))
    service.start(sid, "competition_math", "数学", "practice", 2, 30, "phase-exam")
    binding = service.arrival_binding(sid)
    result = submit(flow, binding)
    assert result["attempt"]["diagnostic_item_id"] == binding["item_id"]
    assert rows(db, "daily_session")[0]["state"] == state
    if state != "study":
        with pytest.raises(StaleSessionStateError):
            service.learning.submit_diagnostic(sid, binding["item_id"], "another answer", "ordinary-diagnostic")


@pytest.mark.parametrize("sql,args", [
    ("UPDATE daily_session SET state='evolving'", ()),
    ("UPDATE daily_session SET state='completed'", ()),
    ("UPDATE daily_session SET status='completed'", ()),
    ("UPDATE child_profile SET status='archived' WHERE child_id='child-primary-4'", ()),
    ("UPDATE daily_session SET child_id='child-junior-3'", ()),
])
def test_inactive_terminal_or_changed_child_cannot_replay_or_score(flow, sql, args):
    binding = flow[1].arrival_binding(flow[2])
    submit(flow, binding)
    change(flow[0], sql, args)
    assert flow[1].arrival_binding(flow[2]) is None
    with pytest.raises(ExamSimulationError):
        submit(flow, binding)
    with pytest.raises(ExamSimulationError):
        flow[1].start(flow[2], "competition_math", "数学", "practice", 2, 30, "terminal-start")


@pytest.mark.parametrize("sql", [
    "UPDATE diagnostic_item SET prompt=prompt || ' revised' WHERE diagnostic_item_id=?",
    "UPDATE diagnostic_item SET expected_answer_json='[\"changed\"]' WHERE diagnostic_item_id=?",
    "UPDATE diagnostic_item SET status='withdrawn' WHERE diagnostic_item_id=?",
    "UPDATE knowledge_node SET knowledge_version='revised' WHERE knowledge_node_id=(SELECT knowledge_node_id FROM diagnostic_item WHERE diagnostic_item_id=?)",
])
def test_source_version_changes_reject_arrival_and_saved_binding(flow, sql):
    binding = flow[1].arrival_binding(flow[2])
    change(flow[0], sql, (binding["item_id"],))
    assert flow[1].arrival_binding(flow[2]) is None
    with pytest.raises(ExamSimulationError):
        submit(flow, binding)
    assert rows(flow[0], "diagnostic_attempt") == []


def test_missing_historical_version_is_not_backfilled_or_claimed_waiting(flow):
    binding = flow[1].arrival_binding(flow[2])
    change(flow[0], "UPDATE specialized_exam_item SET source_version=NULL")
    flow[1].ensure_schema()
    assert flow[1].arrival_binding(flow[2]) is None
    assert all(r["source_version"] is None for r in rows(flow[0], "specialized_exam_item"))
    with pytest.raises(ExamSimulationError):
        submit(flow, binding)


def test_reserved_answer_survives_pre_score_crash_and_resumes_only_original_payload(flow, monkeypatch):
    binding = flow[1].arrival_binding(flow[2])
    with monkeypatch.context() as patch:
        patch.setattr(flow[1], "_submit_reserved", lambda *a, **kw: (_ for _ in ()).throw(OSError("before score")))
        with pytest.raises(OSError, match="before score"):
            submit(flow, binding)
    slot = rows(flow[0], "specialized_exam_item")[0]
    assert slot["answer_text"] == "I am not sure" and slot["answer_action_id"] == "bound-exam-answer"
    assert slot["attempt_id"] is None and rows(flow[0], "diagnostic_attempt") == []
    restarted = ExamSimulationService(Database(flow[0].path))
    restarted.recover_after_restart()
    with pytest.raises(ExamSimulationError):
        restarted.submit_bound(flow[2], **binding, answer="I am not sure", client_action_id="bound-exam-answer")
    restarted.resume(binding["run_id"])
    with pytest.raises(ExamSimulationError):
        restarted.submit_bound(flow[2], **binding, answer="Changed", client_action_id="bound-exam-answer")
    with pytest.raises(ExamSimulationError):
        restarted.submit_bound(flow[2], **binding, answer="I am not sure", client_action_id="another-action")
    result = restarted.submit_bound(flow[2], **binding, answer="I am not sure", client_action_id="bound-exam-answer")
    assert result["exam"]["current_position"] == 2


@pytest.mark.parametrize("mutation", ["cancel", "source", "session", "timeout"])
def test_reservation_to_scoring_race_rechecks_all_guards(flow, monkeypatch, mutation):
    db, service, sid, _ = flow
    binding = service.arrival_binding(sid)
    original = service._submit_reserved
    def change_before_scoring(*args, **kwargs):
        if mutation == "cancel":
            service.cancel(binding["run_id"])
        elif mutation == "source":
            change(db, "UPDATE diagnostic_item SET prompt='Revised' WHERE diagnostic_item_id=?", (binding["item_id"],))
        elif mutation == "session":
            change(db, "UPDATE daily_session SET status='completed'")
        else:
            change(db, "UPDATE specialized_exam_section_state SET elapsed_seconds=time_limit_seconds,active_since=NULL")
        return original(*args, **kwargs)
    monkeypatch.setattr(service, "_submit_reserved", change_before_scoring)
    with pytest.raises(ExamSimulationError):
        submit(flow, binding)
    assert rows(db, "diagnostic_attempt") == []
    assert rows(db, "specialized_exam_item")[0]["attempt_id"] is None


def test_scoring_then_slot_write_failure_rolls_back_attempt_and_evidence(flow):
    import sqlite3
    binding = flow[1].arrival_binding(flow[2])
    change(flow[0], """CREATE TRIGGER reject_slot_receipt BEFORE UPDATE OF attempt_id ON specialized_exam_item
        WHEN NEW.attempt_id IS NOT NULL BEGIN SELECT RAISE(ABORT, 'slot write failed'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="slot write failed"):
        submit(flow, binding)
    assert rows(flow[0], "diagnostic_attempt") == []
    assert rows(flow[0], "learning_evidence") == []
    assert rows(flow[0], "specialized_exam_item")[0]["answer_action_id"] == "bound-exam-answer"
    change(flow[0], "DROP TRIGGER reject_slot_receipt")
    assert submit(flow, binding)["exam"]["answered_items"] == 1


@pytest.mark.parametrize("same_key", [False, True])
def test_concurrent_bound_answers_reserve_one_slot_without_double_scoring(flow, same_key):
    binding = flow[1].arrival_binding(flow[2])
    def answer(key):
        try:
            return submit(flow, binding, action=key)
        except ExamSimulationError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(answer, ["race-a", "race-a" if same_key else "race-b"]))
    assert sum(r is not None for r in results) == (2 if same_key else 1)
    assert len(rows(flow[0], "diagnostic_attempt")) == 1
    assert flow[1].arrival_binding(flow[2])["position"] == 2


def test_trusted_exam_permission_requires_persisted_reservation_and_cannot_bypass_normal_api(flow, monkeypatch):
    db, service, sid, _ = flow
    binding = service.arrival_binding(sid)
    key = f"exam-bound-answer:{binding['run_id']}:{binding['position']}"
    with pytest.raises(ValueError):
        service.learning.submit_diagnostic(sid, binding["item_id"], "I am not sure", key)
    with pytest.raises(ValueError):
        service.learning.submit_diagnostic(sid, binding["item_id"], "I am not sure", key,
            _trusted_exam_slot=(binding["run_id"], binding["position"]))
    with monkeypatch.context() as patch:
        patch.setattr(service, "_submit_reserved", lambda *a, **kw: (_ for _ in ()).throw(OSError("saved")))
        with pytest.raises(OSError):
            submit(flow, binding)
    for kwargs in ({"answer": "different"}, {"review_task_id": "forged"}, {"hint_level": 1}):
        payload = {"session_id": sid, "item_id": binding["item_id"], "answer": "I am not sure",
                   "client_action_id": key, "_trusted_exam_slot": (binding["run_id"], binding["position"]), **kwargs}
        with pytest.raises(ValueError):
            service.learning.submit_diagnostic(**payload)
    assert rows(db, "diagnostic_attempt") == []


def test_unreliable_answers_are_saved_not_exported_as_confirmed_exam_scores(flow):
    for index in range(2):
        result = submit(flow, flow[1].arrival_binding(flow[2]), action=f"unreliable-{index}")
        assert result["attempt"]["score_status"] == "needs_review"
    assert result["exam"]["status"] == "completed"
    assert result["exam"]["result"]["score"] is None
    assert result["exam"]["result"]["answered_items"] == 2
    assert result["exam"]["result"]["domain_results"] == {}
    assert rows(flow[0], "specialized_assessment_event") == []


def test_read_expired_slot_does_not_apply_timeout_or_invent_next_binding(flow):
    change(flow[0], "UPDATE specialized_exam_section_state SET elapsed_seconds=time_limit_seconds,active_since=NULL")
    before = rows(flow[0], "specialized_exam_run"), rows(flow[0], "specialized_exam_section_state")
    assert flow[1].arrival_binding(flow[2]) is None
    assert before == (rows(flow[0], "specialized_exam_run"), rows(flow[0], "specialized_exam_section_state"))


def test_completed_receipt_replays_original_run_after_new_process_and_new_run(flow):
    binding = flow[1].arrival_binding(flow[2])
    first = submit(flow, binding)
    flow[1].cancel(binding["run_id"])
    later = flow[1].start(flow[2], "competition_math", "数学", "practice", 2, 30, "later-exam-run")
    code = """import json, sys
from home_study_agent.database import Database
from home_study_agent.exam_simulation import ExamSimulationService
result = ExamSimulationService(Database(sys.argv[1])).submit_bound(sys.argv[2], **json.loads(sys.argv[3]),
    answer='I am not sure', client_action_id='bound-exam-answer')
print(json.dumps(result))
"""
    result = subprocess.run([sys.executable, "-c", code, str(flow[0].path), flow[2], json.dumps(binding)],
        env=dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path)), capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr[-1800:]
    replay = json.loads(result.stdout)
    assert replay["attempt"] == first["attempt"] and replay["exam"]["run_id"] == binding["run_id"]
    assert flow[1].arrival_binding(flow[2])["run_id"] == later["run_id"]


@pytest.mark.parametrize("command", ["pause", "resume", "cancel"])
def test_terminal_session_cannot_control_exam(flow, command):
    change(flow[0], "UPDATE daily_session SET state='evolving'")
    with pytest.raises(ExamSimulationError):
        getattr(flow[1], command)(flow[3]["run_id"])


def test_legacy_submit_cannot_consume_strong_reservation(flow, monkeypatch):
    binding = flow[1].arrival_binding(flow[2])
    monkeypatch.setattr(flow[1], "_submit_reserved", lambda *a, **kw: (_ for _ in ()).throw(OSError("saved")))
    with pytest.raises(OSError):
        submit(flow, binding)
    with pytest.raises(ExamSimulationError, match="STRONG_BINDING_REQUIRED"):
        flow[1].submit_answer(binding["run_id"], "I am not sure", "bound-exam-answer")
    assert rows(flow[0], "diagnostic_attempt") == []


@pytest.mark.parametrize("crash_finalize", [False, True])
def test_closed_local_template_answers_use_existing_scorer_and_persist_only_verified_domains(flow, monkeypatch, crash_finalize):
    from home_study_agent.specialized_supply import SpecializedDiagnosticSupplyService

    db, service, sid, run = flow
    service.cancel(run["run_id"])
    supply = SpecializedDiagnosticSupplyService(db)
    with db.connect() as c:
        seed = c.execute("SELECT diagnostic_item_id FROM diagnostic_item WHERE knowledge_node_id='node-oly-arithmetic-structure'").fetchone()[0]
    supply.enqueue_local(seed, "child-primary-4", "exam-local-proof", count=2)
    supply.process_local_pending()
    candidates = supply.list("child-primary-4")["items"]
    assert candidates
    for candidate in candidates:
        supply.qualify_local(candidate["candidate_id"], "child-primary-4", "qualify-" + candidate["candidate_id"])
    run = service.start(sid, "competition_math", "数学", "practice", 100, 30, "exam-with-qualified-items")
    verified = []
    for index in range(run["total_items"]):
        binding = service.arrival_binding(sid)
        with db.connect() as c:
            rubric = json.loads(c.execute("SELECT expected_answer_json FROM diagnostic_item WHERE diagnostic_item_id=?",
                                         (binding["item_id"],)).fetchone()[0])
        answer = rubric["exact_answers"][0] if "exact_answers" in rubric else "I am not sure"
        if crash_finalize and index == run["total_items"] - 1:
            with monkeypatch.context() as patch:
                patch.setattr(service.specialized, "record_assessment",
                              lambda **kwargs: (_ for _ in ()).throw(OSError("after result before assessment")))
                with pytest.raises(OSError, match="after result before assessment"):
                    submit(flow, binding, answer=str(answer), action=f"proved-slot-{index}")
            assert rows(db, "specialized_exam_run")[-1]["result_json"]
        result = submit(flow, binding, answer=str(answer), action=f"proved-slot-{index}")
        if result["attempt"]["score_status"] == "verified_local":
            verified.append(result["attempt"])
    assert len(verified) == len(candidates)
    assert all(a["correctness"] == 1 for a in verified)
    assert result["exam"]["result"]["score"] is None
    assert result["exam"]["result"]["verified_items"] == len(verified)
    events = rows(db, "specialized_assessment_event")
    assert len(events) == 1 and set(json.loads(events[0]["domain_results_json"]).values()) == {1.0}
    assert events[0]["score"] is None and events[0]["max_score"] is None
    assert submit(flow, binding, answer=str(answer), action=f"proved-slot-{index}")["attempt"] == result["attempt"]
    assert rows(db, "specialized_assessment_event") == events
