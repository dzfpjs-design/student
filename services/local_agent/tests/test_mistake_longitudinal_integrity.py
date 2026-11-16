from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4

import pytest

from home_study_agent import child_model, mistake_book
from home_study_agent.child_model import ChildModelService
from home_study_agent.database import Database
from home_study_agent.learning_state import LearningStateService
from home_study_agent.mistake_book import (
    InvalidMistakeInputError,
    InvalidMistakeStateError,
    MistakeBookService,
    MistakeReviewNotDueError,
    UnknownMistakeError,
    UnknownMistakeReferenceError,
)
from home_study_agent.repository import StudyRepository
from home_study_agent.variant_supply import VariantSupplyService


CHILD = "child-primary-4"
OTHER = "child-junior-3"
NODE = "node-p4-english-phonics-discrimination"
ITEM = "diag-p4-english-phonics-1"


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "integrity.db")
    database.initialize()
    StudyRepository(database).seed_family()
    LearningStateService(database).seed_catalog()
    return database


def capture(db, child=CHILD, action=None, **kwargs):
    return MistakeBookService(db).capture(
        child_id=child, knowledge_node_id=NODE, subject="English", title="Phonics",
        prompt_summary=str(uuid4()), submitted_answer="cake", source_type="manual",
        source_ref=None, confidence=1, client_action_id=action or str(uuid4()), **kwargs,
    )["mistake_id"]


def prepare(db, error="unknown", child=CHILD, remediate=True):
    service = MistakeBookService(db)
    case = capture(db, child, diagnostic_item_id="diag-p4-english-phonics-3")
    service.confirm(case, child, "confirm", str(uuid4()))
    service.diagnose(case, child, error, "Possible cause", .99, str(uuid4()))
    if remediate:
        service.add_remediation(case, child, "active_recall", "completed", None, str(uuid4()))
    return service, case


def submit(service, case, stage="near_transfer", item=ITEM, answer="bag", hint=0, action=None, child=CHILD):
    return service.submit_attempt(case, child, stage, item, answer, hint, action or str(uuid4()))


def prepare_exact(db, error="unknown"):
    service = MistakeBookService(db)
    case = service.capture(child_id=CHILD, knowledge_node_id="node-p4-math-arithmetic", subject="Math",
        title="Synthetic arithmetic", prompt_summary=str(uuid4()), submitted_answer="wrong", source_type="manual",
        source_ref=None, confidence=1, client_action_id=str(uuid4()))["mistake_id"]
    service.confirm(case, CHILD, "confirm", str(uuid4()))
    service.diagnose(case, CHILD, error, "Candidate only", .99, str(uuid4()))
    service.add_remediation(case, CHILD, "active_recall", "completed", None, str(uuid4()))
    return service, case


def submit_exact(service, case, stage="near_transfer", *, hint=0, wrong=False):
    item, answer = ("diag-p4-arithmetic-closed-1", "90") if stage == "repair_check" else ("diag-p4-arithmetic-closed-2", "42")
    return submit(service, case, stage=stage, item=item, answer="wrong" if wrong else answer, hint=hint)


def freeze(monkeypatch, now):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now.replace(tzinfo=None)

    monkeypatch.setattr(mistake_book, "datetime", Clock)
    monkeypatch.setattr(child_model, "datetime", Clock)


def exact_diagnostic(db, child=CHILD, *, answer="90"):
    repo = StudyRepository(db)
    sid = repo.start_session(child, str(uuid4()))["session_id"]
    with db.connect() as conn:
        conn.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
        conn.commit()
    result = LearningStateService(db).submit_diagnostic(sid, "diag-p4-arithmetic-closed-1", answer, str(uuid4()))
    assert result["score_status"] == "verified_local"
    return result


@pytest.mark.parametrize("answer", [
    "cake and name are different", "cake name", "bag cake name",
    "bag is not different", "not bag", "I choose cake, not bag",
    "bag is different, cake and name are different", "bag or cake",
    "bagel is different", "bag?", "bag 和 cake 一样", "不是 bag，是 cake",
    "bag /eɪ/ 不同，cake name /æ/", "bag /æ/ 不同，cake name /æ/",
])
def test_phonics_distractors_and_ambiguous_answers_cannot_advance_gate(db, answer):
    service, case = prepare(db)
    result = submit(service, case, answer=answer)
    assert result["attempt_result"]["correctness"] == 0
    assert result["mastery_gate"]["satisfied"] == {}
    assert result["mastery_gate"]["missing"][0] == "near_transfer"
    assert service.next_activity(case, CHILD)["item"]["diagnostic_item_id"] != ITEM


@pytest.mark.parametrize("answer", [
    "bag", "Bag!", "I choose bag", "bag is different",
    "bag 不同，cake 和 name 读音相同", "cake and name have the same sound, so bag is different",
    "bag /æ/ 不同，cake name /eɪ/",
])
def test_phonics_explicit_answer_keeps_reference_score_without_certification(db, answer):
    service, case = prepare(db)
    result = submit(service, case, answer=answer)
    assert result["attempt_result"]["correctness"] == 1
    assert result["attempt_result"]["score_status"] == "needs_review"
    assert result["mastery_gate"]["satisfied"] == {}


@pytest.mark.parametrize("item, answer", [
    ("diag-p4-english-phonics-2", "sit"), ("diag-p4-english-phonics-3", "hot"),
    ("diag-p4-english-phonics-2", "sit /ɪ/ 不同，bike five /aɪ/"),
    ("diag-p4-english-phonics-3", "hot /ɒ/ 不同，home nose /əʊ/"),
])
def test_other_seed_phonics_items_use_their_actual_answer(db, item, answer):
    service, case = prepare(db, "execution_error")
    assert submit(service, case, stage="repair_check", item=item, answer=answer)["attempt_result"]["correctness"] == 1


def test_hints_exhaust_transfer_supply_without_counting_independence(db):
    service, case = prepare(db)
    for item, answer in ((ITEM, "bag"), ("diag-p4-english-phonics-2", "sit")):
        result = submit(service, case, item=item, answer=answer, hint=1)
        assert result["attempt_result"]["correctness"] == 1
        assert result["attempt_result"]["independent"] is False
        assert result["attempt_result"]["score_status"] == "needs_review"
        assert result["mastery_gate"]["satisfied"] == {}
    assert service.next_activity(case, CHILD)["kind"] == "blocked"
    with pytest.raises(InvalidMistakeStateError):
        submit(service, case)


@pytest.mark.parametrize("state", ["awaiting_confirmation", "diagnosing", "remediating", "excluded", "relapsed"])
def test_attempt_requires_ready_state_even_when_gate_exists(db, state):
    service, case = prepare(db)
    with db.connect() as conn:
        conn.execute("UPDATE mistake_case SET status = ? WHERE mistake_id = ?", (state, case))
        conn.commit()
    with pytest.raises(InvalidMistakeStateError):
        submit(service, case)
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM mistake_attempt").fetchone()[0] == 0


def test_attempt_cannot_skip_stage_or_reuse_original_or_action(db):
    service, case = prepare(db)
    with pytest.raises(InvalidMistakeStateError):
        submit(service, case, stage="representation_transfer")
    with pytest.raises(InvalidMistakeStateError):
        submit(service, case, item="diag-p4-english-phonics-3", answer="hot")
    first = submit(service, case, action="same-answer")
    assert submit(service, case, action="same-answer")["mastery_gate"] == first["mastery_gate"]
    with pytest.raises(InvalidMistakeInputError):
        submit(service, case, action="same-answer", answer="cake")


def test_delayed_review_waits_then_reuses_exhausted_item_and_survives_restart(db, monkeypatch):
    start = datetime.now(timezone.utc)
    freeze(monkeypatch, start)
    service, case = prepare_exact(db, "retrieval_failure")
    submit_exact(service, case, stage="repair_check")
    submit_exact(service, case)
    with pytest.raises(MistakeReviewNotDueError):
        submit_exact(service, case, stage="delayed_48h")
    review_item = "diag-p4-arithmetic-closed-2"
    with db.connect() as conn:
        conn.execute("UPDATE diagnostic_item SET status = 'inactive' WHERE knowledge_node_id = ? AND diagnostic_item_id != ?", ("node-p4-math-arithmetic", review_item))
        conn.commit()
    model = ChildModelService(db)
    snapshot = model.snapshot(CHILD)
    restarted = Database(db.path)
    restarted.initialize()
    service = MistakeBookService(restarted)
    assert ChildModelService(restarted).snapshot(CHILD)["snapshot_id"] == snapshot["snapshot_id"]
    # Retiring the repair source invalidates its pass; restore it before
    # verifying the independent delayed-reuse boundary.
    assert "repair_check" in service.get(case, CHILD)["mastery_gate"]["missing"]
    with restarted.connect() as conn:
        conn.execute("UPDATE diagnostic_item SET status='active' WHERE diagnostic_item_id='diag-p4-arithmetic-closed-1'")
        conn.commit()
    freeze(monkeypatch, start + timedelta(hours=49))
    activity = service.next_activity(case, CHILD)
    assert activity["kind"] == "attempt"
    assert activity["variant_freshness"] == "spaced_reuse"
    assert activity["item"]["diagnostic_item_id"] in {review_item, "diag-p4-arithmetic-closed-1"}
    result = submit_exact(service, case, stage="delayed_48h")
    assert result["mastery_gate"]["missing"] == ["delayed_7d"]
    with pytest.raises(MistakeReviewNotDueError):
        submit_exact(service, case, stage="delayed_7d")
    freeze(monkeypatch, start + timedelta(days=8))
    assert submit_exact(service, case, stage="delayed_7d")["status"] == "mastered"


def test_rediagnosis_does_not_reuse_prior_cycle_passes(db, monkeypatch):
    start = datetime.now(timezone.utc)
    freeze(monkeypatch, start)
    service, case = prepare_exact(db, "incidental")
    assert submit_exact(service, case, stage="repair_check")["status"] != "mastered"
    submit_exact(service, case)
    freeze(monkeypatch, start + timedelta(hours=49))
    assert submit_exact(service, case, stage="delayed_48h")["status"] == "mastered"
    with db.connect() as conn:
        conn.execute("UPDATE mistake_case SET status = 'relapsed' WHERE mistake_id = ?", (case,))
        conn.commit()
    freeze(monkeypatch, start + timedelta(days=3))
    result = service.diagnose(case, CHILD, "incidental", "New hypothesis", .99, str(uuid4()))
    assert result["mastery_gate"]["satisfied"] == {}


def test_repeating_hinted_repair_does_not_become_independent(db):
    service, case = prepare_exact(db, "execution_error")
    submit_exact(service, case, stage="repair_check", hint=1)
    repeated = submit_exact(service, case, stage="repair_check")
    assert repeated["attempt_result"]["independent"] is False
    assert repeated["mastery_gate"]["satisfied"] == {}


def test_delayed_clock_starts_after_repair_and_failed_review_reschedules(db, monkeypatch):
    start = datetime.now(timezone.utc)
    freeze(monkeypatch, start)
    service, case = prepare_exact(db, "execution_error")
    freeze(monkeypatch, start + timedelta(days=3))
    submit_exact(service, case, stage="repair_check")
    submit_exact(service, case)
    with pytest.raises(MistakeReviewNotDueError):
        submit_exact(service, case, stage="delayed_48h")
    freeze(monkeypatch, start + timedelta(days=6))
    failed = submit_exact(service, case, stage="delayed_48h", wrong=True)
    assert failed["mastery_gate"]["missing"] == ["delayed_48h"]
    assert service.next_activity(case, CHILD)["kind"] == "scheduled"
    with pytest.raises(MistakeReviewNotDueError):
        submit_exact(service, case, stage="delayed_48h")


def test_capture_rejects_foreign_action_and_foreign_source_evidence(db):
    service, case = prepare(db)
    submit(service, case)
    capture(db, action="owned-action")
    with pytest.raises(UnknownMistakeError):
        capture(db, OTHER, action="owned-action")
    with db.connect() as conn:
        evidence = conn.execute("SELECT evidence_id FROM mistake_attempt WHERE mistake_id = ?", (case,)).fetchone()[0]
    with pytest.raises(UnknownMistakeReferenceError):
        capture(db, OTHER, source_evidence_id=evidence)


def test_personal_variant_cannot_be_selected_or_submitted_by_other_child(db):
    # Use a same-grade peer so ownership, not an invalid seed grade, is tested.
    with db.connect() as conn:
        conn.execute("UPDATE child_profile SET grade='四年级' WHERE child_id=?", (OTHER,))
        conn.commit()
    service, cases = MistakeBookService(db), {}
    for child in (CHILD, OTHER):
        source = exact_diagnostic(db, child, answer="wrong")
        cases[child] = service.capture_from_attempt(source)["mistake_id"]
        service.confirm(cases[child], child, "confirm", str(uuid4()))
        service.diagnose(cases[child], child, "unknown", "Possible cause", .9, str(uuid4()))
        service.add_remediation(cases[child], child, "active_recall", "completed", None, str(uuid4()))
        with db.connect() as conn:
            conn.execute("UPDATE daily_session SET status='completed',ended_at=? WHERE session_id=?",
                (datetime.now(timezone.utc).isoformat(), source["session_id"]))
            conn.commit()
    case, foreign = cases[CHILD], cases[OTHER]
    from test_local_supply import ForbiddenGateway
    supply = VariantSupplyService(db, ForbiddenGateway())
    job = supply.enqueue_local(foreign, OTHER, "foreign-job")
    assert supply.process_local_pending()["jobs"][0]["status"] == "completed"
    candidate = next(row for row in supply.list(OTHER)["items"] if row["stage"] == "near_transfer")
    promoted = supply.review(candidate["candidate_id"], "approve", "Checked", "approve-private")
    with db.connect() as conn:
        from home_study_agent.local_supply import LocalSupplyError, validate_item
        private = validate_item(conn, OTHER, promoted["diagnostic_item_id"])
        answer = json.loads(private["expected_answer_json"])["exact_answers"][0]
        snapshot = conn.execute("SELECT source_snapshot_json FROM variant_generation_job WHERE job_id=?", (job["job_id"],)).fetchone()[0]
        assert snapshot and "stimulus_asset" in json.loads(snapshot)
        with pytest.raises(LocalSupplyError, match="SUPPLY_REVIEW_UNAVAILABLE"):
            validate_item(conn, CHILD, promoted["diagnostic_item_id"])
        conn.execute("""UPDATE diagnostic_item SET status='inactive' WHERE knowledge_node_id=?
            AND diagnostic_item_id NOT IN (?, 'diag-p4-arithmetic-closed-1')""", (private["knowledge_node_id"], promoted["diagnostic_item_id"]))
        assert validate_item(conn, OTHER, promoted["diagnostic_item_id"])
        conn.commit()
    assert service.next_activity(case, CHILD)["kind"] == "blocked"
    with pytest.raises(UnknownMistakeReferenceError):
        submit(service, case, item=promoted["diagnostic_item_id"], answer=answer)
    with pytest.raises(UnknownMistakeReferenceError):
        service.capture(child_id=CHILD, knowledge_node_id=private["knowledge_node_id"], subject="Math", title="Private variant",
            prompt_summary=private["prompt"], submitted_answer="wrong", source_type="manual", source_ref=None,
            confidence=1, client_action_id=str(uuid4()), diagnostic_item_id=promoted["diagnostic_item_id"])
    with Database(db.path).connect() as conn:
        assert validate_item(conn, OTHER, promoted["diagnostic_item_id"])
        with pytest.raises(LocalSupplyError, match="SUPPLY_REVIEW_UNAVAILABLE"):
            validate_item(conn, CHILD, promoted["diagnostic_item_id"])


def test_snapshot_tracks_mutated_projection_without_timestamp_change(db):
    source = exact_diagnostic(db)
    model = ChildModelService(db)
    before = model.snapshot(CHILD)
    other = model.snapshot(OTHER)
    with db.connect() as conn:
        original_time = conn.execute("SELECT occurred_at FROM learning_evidence WHERE evidence_id=?", (source["evidence_id"],)).fetchone()[0]
        conn.execute("UPDATE learning_evidence SET hint_level=1,independent=0 WHERE evidence_id=?", (source["evidence_id"],))
        assert conn.execute("SELECT occurred_at FROM learning_evidence WHERE evidence_id=?", (source["evidence_id"],)).fetchone()[0] == original_time
        conn.commit()
    changed = model.snapshot(CHILD)
    assert changed["snapshot_id"] != before["snapshot_id"]
    assert changed["payload"]["support_strategy"]["explanation_pacing"] == "short_step_then_restate"
    assert model.compact_for_agent(CHILD)["snapshot_id"] == changed["snapshot_id"]
    assert model.snapshot(OTHER)["snapshot_id"] == other["snapshot_id"]
    assert ChildModelService(Database(db.path)).snapshot(CHILD)["snapshot_id"] == changed["snapshot_id"]


def test_snapshot_expires_time_based_projection_without_new_rows(db, monkeypatch):
    start = datetime.now(timezone.utc)
    freeze(monkeypatch, start)
    from home_study_agent import learning_state, repository
    monkeypatch.setattr(learning_state, "utc_now", lambda: start.isoformat())
    monkeypatch.setattr(repository, "utc_now", lambda: start.isoformat())
    source = exact_diagnostic(db)
    with db.connect() as conn:
        conn.execute("UPDATE review_task SET due_at=? WHERE source_evidence_id=? AND interval_label='immediate'",
            ((start + timedelta(hours=1)).isoformat(), source["evidence_id"]))
        conn.commit()
    LearningStateService(db).replay_mastery(CHILD, as_of=start.isoformat())
    model = ChildModelService(db)
    first = model.snapshot(CHILD)
    assert first["payload"]["learning"]["subjects"][0]["due_reviews"] == 0
    freeze(monkeypatch, start + timedelta(hours=2))
    assert model.compact_for_agent(CHILD)["snapshot_persisted"] is False
    persisted = model.persisted_compact_for_agent(CHILD)
    assert persisted["snapshot_persisted"] is True
    assert persisted["snapshot_id"]
    changed = model.snapshot(CHILD)
    assert changed["snapshot_id"] == persisted["snapshot_id"]
    assert changed["snapshot_id"] != first["snapshot_id"]
    assert changed["payload"]["learning"]["subjects"][0]["due_reviews"] == 1


def test_confident_hypothesis_is_not_confirmed_memory(db):
    service, case = prepare(db)
    detail = service.get(case, CHILD)
    assert detail["diagnosis"]["status"] == "candidate"
    assert detail["diagnosis_status"] == "suspected"
    with db.connect() as conn:
        conn.execute("UPDATE mistake_cluster SET diagnosis_status = 'confirmed_gap' WHERE child_id = ?", (CHILD,))
        conn.commit()
    snapshot = ChildModelService(db).snapshot(CHILD)
    assert snapshot["payload"]["mistakes"]["top_clusters"][0]["diagnosis_status"] == "suspected"


@pytest.mark.parametrize("phase, excluded, foreign, focused", [
    ("attempt", False, False, True), ("scheduled", False, False, False),
    ("paused", False, False, False), ("closed", False, False, False),
    ("attempt", True, False, False), ("attempt", False, True, False),
])
def test_session_focus_precedes_queue_and_is_owned(db, phase, excluded, foreign, focused):
    fallback = capture(db)
    focus = capture(db, OTHER if foreign else CHILD)
    session = StudyRepository(db).start_session(CHILD, "session-focus")
    with db.connect() as conn:
        # Windows can timestamp consecutive captures identically; keep FIFO ties stable.
        conn.execute("UPDATE mistake_case SET updated_at = ?, created_at = ? WHERE mistake_id IN (?, ?)",
                     ("2026-09-05T10:00:00+00:00", "2026-09-05T10:00:00+00:00", fallback, focus))
        # Mirrors the parent's session-focus contract until that schema lands.
        conn.execute("""CREATE TABLE IF NOT EXISTS mistake_dialogue (
            session_id TEXT PRIMARY KEY REFERENCES daily_session(session_id) ON DELETE CASCADE,
            child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
            candidate_id TEXT, mistake_id TEXT REFERENCES mistake_case(mistake_id) ON DELETE SET NULL,
            phase TEXT NOT NULL, state_json TEXT NOT NULL, updated_at TEXT NOT NULL
        )""")
        conn.execute("INSERT INTO mistake_dialogue(session_id, child_id, mistake_id, phase, state_json, updated_at) VALUES (?, ?, ?, ?, '{}', ?)",
                     (session["session_id"], CHILD, focus, phase, datetime.now(timezone.utc).isoformat()))
        if excluded:
            conn.execute("UPDATE mistake_case SET status = 'excluded' WHERE mistake_id = ?", (focus,))
        conn.commit()
    assert MistakeBookService(Database(db.path)).current_for_session(session["session_id"])["mistake_id"] == (focus if focused else fallback)


def test_excluded_mistake_retracts_weakness_memory_without_erasing_audit(db):
    service, case = prepare(db)
    before = ChildModelService(db).snapshot(CHILD)
    assert before["payload"]["mistakes"]["top_clusters"]
    service.confirm(case, CHILD, "exclude", "exclude-memory-case")
    after = ChildModelService(Database(db.path)).snapshot(CHILD)
    assert after["snapshot_id"] != before["snapshot_id"]
    assert after["payload"]["mistakes"]["top_clusters"] == []
    assert service.weakness_clusters(CHILD)["items"] == []
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM mistake_occurrence WHERE mistake_id=?", (case,)).fetchone()[0] == 1
        assert conn.execute("SELECT occurrence_count FROM mistake_cluster WHERE child_id=?", (CHILD,)).fetchone()[0] == 0


def test_future_focused_review_does_not_shadow_other_due_case(db, monkeypatch):
    start = datetime.now(timezone.utc)
    freeze(monkeypatch, start)
    service, future_case = prepare_exact(db)
    submit_exact(service, future_case)
    freeze(monkeypatch, start + timedelta(minutes=1))
    _, due_case = prepare_exact(db)
    submit(service, due_case, item="diag-p4-arithmetic-closed-1", answer="90")
    session = StudyRepository(db).start_session(CHILD, "scheduled-focus")
    with db.connect() as conn:
        conn.execute("INSERT INTO mistake_dialogue(session_id, child_id, mistake_id, phase, state_json, updated_at) VALUES (?, ?, ?, 'scheduled', '{}', ?)",
                     (session["session_id"], CHILD, future_case, start.isoformat()))
        conn.execute("UPDATE mistake_mastery_gate SET delayed_48h_due_at = ? WHERE mistake_id = ?",
                     ((start + timedelta(days=9)).isoformat(), future_case))
        conn.commit()
    freeze(monkeypatch, start + timedelta(days=3))
    assert service.current_for_session(session["session_id"])["mistake_id"] == due_case
    freeze(monkeypatch, start + timedelta(days=10))
    assert service.current_for_session(session["session_id"])["mistake_id"] == future_case
