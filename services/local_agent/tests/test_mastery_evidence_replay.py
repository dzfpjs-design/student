from datetime import datetime, timedelta, timezone
import json
import sqlite3
from time import perf_counter
from unittest.mock import patch

import pytest

from home_study_agent import learning_state, mistake_book, repository
from home_study_agent.database import Database
from home_study_agent.learning_state import LearningStateService
from home_study_agent.learning_obligations import (
    QUESTION_IDENTITY_VERSION, had_recent_item_exposure, project_item_exposure,
    project_child_mastery_evidence, project_mastery_evidence, qualified_exact_score, question_identity, withdrawn_evidence_ids,
)
from home_study_agent.mistake_book import MistakeBookService
from home_study_agent.repository import StudyRepository


CHILD = "child-primary-4"
OTHER = "child-junior-3"
NODE = "node-p4-math-arithmetic"
ITEM = "diag-p4-arithmetic-closed-1"
SECOND = "diag-p4-arithmetic-closed-2"


@pytest.fixture
def flow(tmp_path, monkeypatch):
    db = Database(tmp_path / "mastery-replay.db")
    db.initialize()
    repo = StudyRepository(db)
    repo.seed_family()
    learning = LearningStateService(db)
    learning.seed_catalog()
    clock = [datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=10)]
    for module in (learning_state, mistake_book, repository):
        monkeypatch.setattr(module, "utc_now", lambda: clock[0].isoformat())
    sid = repo.start_session(CHILD, "replay-session")["session_id"]
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
        c.commit()
    return db, learning, sid, clock


def state(db, child=CHILD, node=NODE):
    with db.connect() as c:
        row = c.execute("SELECT * FROM mastery_state WHERE child_id=? AND knowledge_node_id=?", (child, node)).fetchone()
        return dict(row) if row else None


def history(db, table):
    with db.connect() as c:
        return [dict(row) for row in c.execute(f"SELECT * FROM {table} ORDER BY rowid")]


def test_same_question_same_day_cannot_farm_mastery_or_claim_hints(flow):
    db, learning, sid, clock = flow
    first = learning.submit_diagnostic(sid, ITEM, "90", "first")
    initial = state(db)
    for n in range(3):
        clock[0] += timedelta(seconds=1)
        learning.submit_diagnostic(sid, ITEM, "90", f"repeat-{n}")
    for key in ("mastery_probability", "evidence_count", "status_confidence", "forgetting_rate", "last_independent_correct_at"):
        assert state(db)[key] == initial[key]
    events = history(db, "learning_evidence")
    assert events[0]["independent"] == 1
    assert all(e["hint_level"] == 0 and e["independent"] == 0 for e in events[1:])
    assert all("independence_reason=same_item_within_24h" in e["evidence_summary"] for e in events[1:])
    assert learning.submit_diagnostic(sid, ITEM, "90", "first") == first
    assert len(history(db, "diagnostic_attempt")) == 4
    assert len(history(db, "review_task")) == 3
    assert {row["source_evidence_id"] for row in history(db, "review_task")} == {first["evidence_id"]}


def test_keyword_feedback_remains_but_is_not_mastery_evidence(flow):
    db, learning, sid, _ = flow
    result = learning.submit_diagnostic(sid, "diag-p4-english-phonics-1", "bag 不同 cake name", "keyword")
    assert result["correctness"] == 1
    assert result["score_status"] == "needs_review" and result["mastery_update_allowed"] is False
    assert result["feedback"] == "作答已保存，当前评分方式不能可靠确认对错，需要进一步核实。"
    assert history(db, "diagnostic_attempt")[0]["feedback"] == result["feedback"]
    evidence = history(db, "learning_evidence")[0]
    assert evidence["confidence"] < .8
    assert evidence["independent"] == 0
    assert state(db, node="node-p4-english-phonics-discrimination") is None
    assert history(db, "review_task") == []
    assert history(db, "mastery_update_event") == []


def test_replay_is_idempotent_and_rebuilds_from_receipts_not_mutable_mastery(flow):
    db, learning, sid, clock = flow
    learning.submit_diagnostic(sid, ITEM, "90", "first")
    clock[0] += timedelta(days=1)
    learning.submit_diagnostic(sid, SECOND, "42", "second")
    correct = state(db)
    events = history(db, "mastery_update_event")
    with db.connect() as c:
        c.execute("UPDATE mastery_state SET mastery_probability=.98,evidence_count=999 WHERE child_id=?", (CHILD,))
        c.commit()
    learning.replay_mastery(CHILD, NODE, as_of=clock[0].isoformat())
    rebuilt = state(db)
    assert rebuilt == correct
    audit = history(db, "audit_event")
    LearningStateService(Database(db.path)).replay_mastery(CHILD, NODE, as_of=clock[0].isoformat())
    assert state(db) == rebuilt
    assert history(db, "audit_event") == audit
    assert history(db, "mastery_update_event") == events


def test_exclusion_replays_only_affected_child_node_preserving_unrelated_evidence(flow):
    db, learning, sid, clock = flow
    bad = learning.submit_diagnostic(sid, ITEM, "wrong", "wrong")
    service = MistakeBookService(db)
    clock[0] += timedelta(days=2)
    with db.connect() as c:
        review = c.execute("SELECT * FROM review_task WHERE source_evidence_id=? AND interval_label='48h'", (bad["evidence_id"],)).fetchone()
    descendant = learning.submit_diagnostic(sid, SECOND, "42", "descendant", review["review_task_id"])
    case = service.capture_from_attempt(bad)["mistake_id"]
    clock[0] += timedelta(days=1)
    unrelated = learning.submit_diagnostic(sid, SECOND, "42", "unrelated")
    learning.submit_diagnostic(sid, "diag-p4-fraction-decimal-closed-1", "0.3", "other-node")
    with db.connect() as c:
        c.execute("""INSERT INTO mastery_state(child_id,knowledge_node_id,mastery_probability,evidence_count,
                   hint_dependency,status_confidence,forgetting_rate,algorithm_version,updated_at)
                   VALUES (?, ?, .61, 2, 0, .4, .18, 'untouched', ?)""", (OTHER, NODE, clock[0].isoformat()))
        c.commit()
    other_child = state(db, OTHER)
    other_node = state(db, node="node-p4-math-fractions-decimals")
    evidence_history = history(db, "learning_evidence")
    attempt_history = history(db, "diagnostic_attempt")
    update_history = history(db, "mastery_update_event")
    service.confirm(case, CHILD, "exclude", "exclude")
    assert state(db)["evidence_count"] == 1
    assert state(db)["mastery_probability"] > .35
    assert state(db, OTHER) == other_child
    assert state(db, node="node-p4-math-fractions-decimals") == other_node
    assert history(db, "learning_evidence") == evidence_history
    assert history(db, "diagnostic_attempt") == attempt_history
    assert history(db, "mastery_update_event") == update_history
    with db.connect() as c:
        withdrawn = withdrawn_evidence_ids(c, CHILD)
        assert {bad["evidence_id"], descendant["evidence_id"]} <= withdrawn
        assert unrelated["evidence_id"] not in withdrawn
        assert c.execute("SELECT status FROM review_task WHERE review_task_id=?", (review["review_task_id"],)).fetchone()[0] == "completed"
    snapshot = state(db)
    audit = history(db, "audit_event")
    service.confirm(case, CHILD, "exclude", "exclude")
    assert state(db) == snapshot
    assert history(db, "audit_event") == audit


def test_excluding_last_evidence_resets_to_unknown_without_deleting_history(flow):
    db, learning, sid, _ = flow
    bad = learning.submit_diagnostic(sid, ITEM, "wrong", "wrong")
    service = MistakeBookService(db)
    case = service.capture_from_attempt(bad)["mistake_id"]
    service.confirm(case, CHILD, "exclude", "exclude")
    current = state(db)
    assert current["mastery_probability"] == .35
    assert current["evidence_count"] == 0
    assert current["status_confidence"] == 0
    assert current["last_practiced_at"] is None
    assert current["next_review_at"] is None
    assert len(history(db, "learning_evidence")) == len(history(db, "mastery_update_event")) == 1


@pytest.mark.parametrize("sql", [
    "UPDATE diagnostic_attempt SET correctness=0",
    "UPDATE diagnostic_attempt SET submitted_answer='wrong'",
    "UPDATE learning_evidence SET confidence=.2",
    "UPDATE diagnostic_item SET prompt='tampered' WHERE diagnostic_item_id='diag-p4-arithmetic-closed-1'",
    "UPDATE diagnostic_attempt SET child_id='child-junior-3'",
])
def test_replay_rejects_unqualified_or_mismatched_receipts_without_erasing_them(flow, sql):
    db, learning, sid, clock = flow
    learning.submit_diagnostic(sid, ITEM, "90", "first")
    with db.connect() as c:
        c.execute(sql)
        c.commit()
    records = history(db, "learning_evidence")
    learning.replay_mastery(CHILD, NODE, as_of=clock[0].isoformat())
    assert state(db)["evidence_count"] == 0
    assert history(db, "learning_evidence") == records


def test_explicit_hint_is_not_independent_and_retry_payload_is_checked(flow):
    db, learning, sid, _ = flow
    receipt = learning.submit_diagnostic(sid, ITEM, "90", "hinted", hint_level=2)
    assert learning.submit_diagnostic(sid, ITEM, "90", "hinted", hint_level=2) == receipt
    with pytest.raises(ValueError):
        learning.submit_diagnostic(sid, ITEM, "90", "hinted", hint_level=0)
    evidence = history(db, "learning_evidence")[0]
    assert evidence["independent"] == 0 and evidence["hint_level"] == 2
    assert "independence_reason=reported_hint" in evidence["evidence_summary"]
    assert state(db)["last_independent_correct_at"] is None


def test_exclusion_and_replay_audit_commit_atomically(flow):
    db, learning, sid, _ = flow
    bad = learning.submit_diagnostic(sid, ITEM, "wrong", "wrong")
    service = MistakeBookService(db)
    case = service.capture_from_attempt(bad)["mistake_id"]
    before = state(db)
    reviews_before = history(db, "review_task")
    with db.connect() as c:
        c.execute("""CREATE TRIGGER reject_replay BEFORE INSERT ON audit_event
                   WHEN NEW.action='mastery_evidence_replayed'
                   BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END""")
        c.commit()
    with pytest.raises(sqlite3.IntegrityError, match="audit unavailable"):
        service.confirm(case, CHILD, "exclude", "exclude")
    assert state(db) == before
    assert service.get(case, CHILD)["status"] != "excluded"
    assert history(db, "review_task") == reviews_before


def project(db, clock):
    with db.connect() as c:
        c.execute("PRAGMA query_only=ON")
        return project_mastery_evidence(c, CHILD, NODE, as_of=clock[0].isoformat())


def test_rolling_exposure_crosses_beijing_midnight_and_resets_after_full_24h(flow):
    db, learning, sid, clock = flow
    clock[0] = clock[0].replace(hour=15, minute=59, second=0)
    first = learning.submit_diagnostic(sid, ITEM, "90", "first")
    clock[0] += timedelta(minutes=2)
    midnight = learning.submit_diagnostic(sid, ITEM, "90", "midnight")
    clock[0] += timedelta(hours=23, minutes=59)
    dense = learning.submit_diagnostic(sid, ITEM, "90", "dense")
    assert state(db)["evidence_count"] == 1
    clock[0] += timedelta(hours=24)
    rested = learning.submit_diagnostic(sid, ITEM, "90", "rested")
    projection = project(db, clock)
    assert [r["evidence_id"] for r in projection["effective_evidence"]] == [first["evidence_id"], rested["evidence_id"]]
    assert {r["evidence_id"] for r in projection["excluded_evidence"]} == {midnight["evidence_id"], dense["evidence_id"]}
    assert all(r["reason"] == "same_item_within_24h" for r in projection["excluded_evidence"])
    assert state(db)["evidence_count"] == 2


def test_legacy_hinted_then_independent_flag_cannot_upgrade_and_history_is_immutable(flow):
    db, learning, sid, clock = flow
    first = learning.submit_diagnostic(sid, ITEM, "90", "hinted", hint_level=2)
    clock[0] += timedelta(seconds=1)
    learning.submit_diagnostic(sid, ITEM, "90", "old-repeat")
    with db.connect() as c:
        c.execute("UPDATE learning_evidence SET independent=1,evidence_summary='legacy' WHERE hint_level=0")
        c.commit()
    before = history(db, "learning_evidence")
    learning.replay_mastery(CHILD, NODE, as_of=clock[0].isoformat())
    assert state(db)["evidence_count"] == 1
    assert state(db)["last_independent_correct_at"] is None
    assert project(db, clock)["effective_evidence"][0]["evidence_id"] == first["evidence_id"]
    assert history(db, "learning_evidence") == before


def test_withdrawn_exposure_does_not_promote_legacy_repeat(flow):
    db, learning, sid, clock = flow
    bad = learning.submit_diagnostic(sid, ITEM, "wrong", "wrong")
    service = MistakeBookService(db)
    case = service.capture_from_attempt(bad)["mistake_id"]
    clock[0] += timedelta(seconds=1)
    repeated = learning.submit_diagnostic(sid, ITEM, "90", "repeat")
    with db.connect() as c:
        c.execute("UPDATE learning_evidence SET independent=1 WHERE evidence_id=?", (repeated["evidence_id"],))
        c.commit()
    service.confirm(case, CHILD, "exclude", "exclude")
    assert state(db)["evidence_count"] == 0
    assert {r["reason"] for r in project(db, clock)["excluded_evidence"]} == {"withdrawn", "same_item_within_24h"}


def test_all_history_readonly_projection_retains_review_provenance_and_stable_replay(flow):
    db, learning, sid, clock = flow
    clock[0] -= timedelta(days=100)
    first = learning.submit_diagnostic(sid, ITEM, "90", "old-first")
    clock[0] += timedelta(days=100)
    with db.connect() as c:
        review = c.execute("SELECT * FROM review_task WHERE source_evidence_id=? AND interval_label='48h'", (first["evidence_id"],)).fetchone()
    result = learning.submit_diagnostic(sid, SECOND, "42", "review", review["review_task_id"])
    before = state(db)
    audit = history(db, "audit_event")
    projection = project(db, clock)
    assert projection["all_effective_receipts"] == projection["effective_evidence"]
    rows = projection["effective_evidence"]
    assert len(rows) == 2
    assert rows[1]["evidence_id"] == result["evidence_id"]
    assert rows[1]["receipt_kind"] == "diagnostic"
    assert rows[1]["review_source_evidence_id"] == first["evidence_id"]
    assert rows[1]["review_task_id"] == review["review_task_id"]
    assert rows[1]["stage"] is None and rows[1]["mistake_id"] is None
    clock[0] += timedelta(days=200)
    assert project(db, clock)["evidence_fingerprint"] == projection["evidence_fingerprint"]
    learning.replay_mastery(CHILD, NODE, as_of=clock[0].isoformat())
    assert state(db) == before
    assert history(db, "audit_event") == audit


def test_real_mistake_receipt_is_replayed_after_persistence_and_withdrawn_with_case(flow):
    db, learning, sid, clock = flow
    bad = learning.submit_diagnostic(sid, ITEM, "wrong", "wrong")
    service = MistakeBookService(db)
    case = service.capture_from_attempt(bad)["mistake_id"]
    service.confirm(case, CHILD, "confirm", "confirm")
    service.diagnose(case, CHILD, "unknown", "Possible cause", .9, "diagnose")
    service.add_remediation(case, CHILD, "active_recall", "completed", None, "remediate")
    service.submit_attempt(case, CHILD, "near_transfer", SECOND, "42", 0, "mistake-response")
    rows = project(db, clock)["effective_evidence"]
    assert state(db)["evidence_count"] == 2
    assert rows[-1]["receipt_kind"] == "mistake"
    assert rows[-1]["stage"] == "near_transfer" and rows[-1]["mistake_id"] == case
    assert rows[-1]["question_identity"].startswith(QUESTION_IDENTITY_VERSION + ":")
    assert rows[-1]["identity_version"] == QUESTION_IDENTITY_VERSION
    assert rows[-1]["review_task_id"] is None
    with db.connect() as c:
        assert project_child_mastery_evidence(c, CHILD, as_of=clock[0].isoformat())[NODE] == project(db, clock)
    evidence = history(db, "learning_evidence")
    attempts = history(db, "mistake_attempt")
    updates = history(db, "mastery_update_event")
    service.confirm(case, CHILD, "exclude", "exclude")
    assert state(db)["evidence_count"] == 0
    assert history(db, "learning_evidence") == evidence
    assert history(db, "mistake_attempt") == attempts
    assert history(db, "mastery_update_event") == updates


@pytest.mark.parametrize("sql,reason", [
    ("UPDATE learning_evidence SET confidence='invalid'", "unreliable_score"),
    ("UPDATE learning_evidence SET hint_level=9", "ownership_or_conditions_mismatch"),
    ("UPDATE diagnostic_attempt SET created_at='2000-01-01T00:00:00+00:00'", "receipt_timestamp_mismatch"),
])
def test_corrupt_conditions_are_visible_and_fail_closed(flow, sql, reason):
    db, learning, sid, clock = flow
    learning.submit_diagnostic(sid, ITEM, "90", "first")
    with db.connect() as c:
        c.execute(sql)
        c.commit()
    result = project(db, clock)
    assert not result["effective_evidence"]
    assert result["excluded_evidence"][0]["reason"] == reason


def test_recent_exposure_is_shared_across_sessions_but_not_children(flow):
    db, learning, sid, clock = flow
    first = learning.submit_diagnostic(sid, ITEM, "90", "first")
    with db.connect() as c:
        c.execute("UPDATE daily_session SET status='completed' WHERE session_id=?", (sid,))
        c.execute("UPDATE child_profile SET grade=(SELECT grade FROM child_profile WHERE child_id=?) WHERE child_id=?", (CHILD, OTHER))
        c.commit()
    repo = StudyRepository(db)
    other_sid = repo.start_session(OTHER, "other-session")["session_id"]
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (other_sid,))
        c.commit()
    other = learning.submit_diagnostic(other_sid, ITEM, "90", "other-first")
    assert state(db, OTHER)["evidence_count"] == 1
    other_state = state(db, OTHER)
    with db.connect() as c:
        c.execute("UPDATE daily_session SET status='completed' WHERE session_id=?", (other_sid,))
        c.commit()
    next_sid = repo.start_session(CHILD, "next-session")["session_id"]
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (next_sid,))
        c.commit()
    learning.submit_diagnostic(next_sid, ITEM, "90", "repeat-in-new-session")
    assert state(db)["evidence_count"] == 1
    assert [r["evidence_id"] for r in project(db, clock)["effective_evidence"]] == [first["evidence_id"]]
    learning.replay_mastery(CHILD, NODE, as_of=clock[0].isoformat())
    assert state(db, OTHER) == other_state
    with db.connect() as c:
        assert project_mastery_evidence(c, OTHER, NODE, as_of=clock[0].isoformat())["effective_evidence"][0]["evidence_id"] == other["evidence_id"]


def test_exact_wrong_answer_still_schedules_durable_closure_review(flow):
    db, learning, sid, clock = flow
    answer = learning.submit_diagnostic(sid, ITEM, "wrong", "wrong")
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='reflection' WHERE session_id=?", (sid,))
        c.commit()
    StudyRepository(db).complete_with_reflection(sid, "", "", "", "", False, "reflection")
    closure = learning.close_session_learning(sid)
    assert closure["weak_points"] and closure["remediation"]
    assert any(r["source_evidence_id"] == answer["evidence_id"] for r in closure["remediation"])
    reopened = LearningStateService(Database(db.path))
    assert reopened.close_session_learning(sid) == closure


def test_keyword_only_legacy_mastery_is_reset_without_rewriting_events(flow):
    db, learning, sid, clock = flow
    learning.submit_diagnostic(sid, "diag-p4-english-phonics-1", "bag 不同 cake name", "keyword")
    node = "node-p4-english-phonics-discrimination"
    with db.connect() as c:
        c.execute("UPDATE learning_evidence SET confidence=1,independent=1")
        c.execute("""INSERT INTO mastery_state(child_id,knowledge_node_id,mastery_probability,evidence_count,
            hint_dependency,status_confidence,algorithm_version,updated_at) VALUES (?,?,.98,50,0,1,'mastery_rules_v2',?)""",
            (CHILD, node, clock[0].isoformat()))
        c.commit()
    before = history(db, "learning_evidence")
    learning.replay_mastery(CHILD, node, as_of=clock[0].isoformat())
    assert state(db, node=node)["evidence_count"] == 0
    assert state(db, node=node)["status_confidence"] == 0
    assert history(db, "learning_evidence") == before


@pytest.mark.parametrize("cross_session", [False, True])
@pytest.mark.parametrize("corruption", ["retired", "modified", "low_confidence"])
def test_closure_rejected_later_correct_cannot_hide_reliable_error(flow, corruption, cross_session):
    db, learning, sid, clock = flow
    wrong = learning.submit_diagnostic(sid, ITEM, "wrong", "wrong")
    later_sid = sid
    if cross_session:
        with db.connect() as c:
            c.execute("UPDATE daily_session SET status='completed',ended_at=? WHERE session_id=?", (clock[0].isoformat(), sid))
            c.commit()
        later_sid = StudyRepository(db).start_session(CHILD, "later-session")["session_id"]
        with db.connect() as c:
            c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (later_sid,))
            c.commit()
    clock[0] += timedelta(seconds=1)
    later = learning.submit_diagnostic(later_sid, SECOND, "42", "later")
    with db.connect() as c:
        if corruption == "retired":
            c.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id=?", (SECOND,))
        elif corruption == "modified":
            c.execute("UPDATE diagnostic_item SET prompt='changed' WHERE diagnostic_item_id=?", (SECOND,))
        else:
            c.execute("UPDATE learning_evidence SET confidence=.2 WHERE evidence_id=?", (later["evidence_id"],))
        c.commit()
    original = history(db, "learning_evidence")
    result = learning.preview_session_learning(sid)
    assert [r["source"]["source_id"] for r in result["weak_points"]] == [wrong["evidence_id"]]
    result = learning.preview_session_learning(later_sid)
    pending = next(r for r in result["needs_verification"] if r["source"]["source_id"] == later["evidence_id"])
    assert pending["score_status"] == "needs_review" and pending["correctness"] is None
    assert pending["reference_correctness"] == 1 and pending["exclusion_reason"]
    assert LearningStateService(Database(db.path)).preview_session_learning(later_sid) == result
    assert history(db, "learning_evidence") == original


@pytest.mark.parametrize("kind", ["same_item_hinted", "legacy_repeat", "other_item_hinted"])
def test_closure_supported_or_repeated_success_cannot_clear_error(flow, kind):
    db, learning, sid, clock = flow
    wrong = learning.submit_diagnostic(sid, ITEM, "wrong", "wrong")
    clock[0] += timedelta(seconds=1)
    item, answer = (SECOND, "42") if kind == "other_item_hinted" else (ITEM, "90")
    later = learning.submit_diagnostic(sid, item, answer, "later", hint_level=0 if kind == "legacy_repeat" else 2)
    if kind == "legacy_repeat":
        with db.connect() as c:
            c.execute("UPDATE learning_evidence SET independent=1 WHERE evidence_id=?", (later["evidence_id"],))
            c.commit()
    result = learning.preview_session_learning(sid)
    assert [r["source"]["source_id"] for r in result["weak_points"]] == [wrong["evidence_id"]]


def test_closure_independent_success_clears_current_error_not_previous_day(flow):
    db, learning, sid, clock = flow
    clock[0] = clock[0].replace(hour=15, minute=59, second=0)
    day = learning._learning_date(clock[0].isoformat())
    wrong = learning.submit_diagnostic(sid, ITEM, "wrong", "wrong")
    clock[0] += timedelta(minutes=1)
    learning.submit_diagnostic(sid, SECOND, "42", "independent")
    assert learning.preview_session_learning(sid)["weak_points"] == []
    historical = LearningStateService(Database(db.path)).daily_learning_summary(CHILD, day)
    assert [r["source"]["source_id"] for r in historical["weak_points"]] == [wrong["evidence_id"]]
    assert historical["weak_points"][0]["current_status_source_id"] == wrong["evidence_id"]


def test_unqualified_legacy_review_is_blocked_without_destroying_saved_source_answer(flow):
    db, learning, sid, clock = flow
    item = "diag-p4-english-phonics-1"
    first = learning.submit_diagnostic(sid, item, "bag 不同 cake name", "keyword-source")
    clock[0] += timedelta(days=2)
    with db.connect() as c:
        c.execute("""INSERT INTO review_task(review_task_id,child_id,knowledge_node_id,diagnostic_item_id,
            source_evidence_id,interval_label,due_at,status,created_at) VALUES ('legacy-unqualified',?,?,?,?,'48h',?,'pending',?)""",
            (CHILD, "node-p4-english-phonics-discrimination", item, first["evidence_id"], clock[0].isoformat(), clock[0].isoformat()))
        c.commit()
    attempts, evidence = history(db, "diagnostic_attempt"), history(db, "learning_evidence")
    for _ in range(2):
        with pytest.raises(learning_state.UnknownReviewTaskError):
            learning.submit_diagnostic(sid, item, "bag 不同 cake name", "unqualified-review", "legacy-unqualified")
    assert history(db, "diagnostic_attempt") == attempts
    assert history(db, "learning_evidence") == evidence
    assert learning.submit_diagnostic(sid, item, "bag 不同 cake name", "keyword-source") == first
    with db.connect() as c:
        pending = c.execute("SELECT * FROM review_task WHERE review_task_id='legacy-unqualified'").fetchone()
        assert pending["status"] == "pending" and pending["result_evidence_id"] is None and pending["completed_at"] is None
        with pytest.raises(repository.PlanInputError):
            StudyRepository._validate_review_source(c, "legacy-unqualified", CHILD, allowed_statuses=("completed",))


def test_old_unqualified_positive_feedback_is_sanitized_on_read_without_history_edits(flow):
    db, learning, sid, _ = flow
    receipt = learning.submit_diagnostic(sid, "diag-p4-english-phonics-1", "bag 不同 cake name", "legacy")
    with db.connect() as c:
        c.execute("UPDATE diagnostic_attempt SET feedback='正确，已经掌握。' WHERE attempt_id=?", (receipt["attempt_id"],))
        c.execute("UPDATE learning_evidence SET confidence=1,independent=1 WHERE evidence_id=?", (receipt["evidence_id"],))
        c.commit()
    before = history(db, "diagnostic_attempt")
    result = learning.submit_diagnostic(sid, "diag-p4-english-phonics-1", "bag 不同 cake name", "legacy")
    assert result["correctness"] == 1 and result["score_status"] == "needs_review"
    assert result["mastery_update_allowed"] is False and "需要进一步核实" in result["feedback"]
    assert "已经掌握" not in result["feedback"]
    assert history(db, "diagnostic_attempt") == before


def test_diagnostic_http_response_never_speaks_unqualified_keyword_success(tmp_path):
    from test_app import client_for

    class NoCloud:
        configured = False

        async def complete(self, *args, **kwargs):
            raise AssertionError("No model call is allowed in this synthetic test")

    with client_for(tmp_path, model_gateway=NoCloud()) as client:
        response = client.post("/v1/sessions", json={"child_id": OTHER, "client_action_id": "api-session"})
        assert response.status_code == 201
        sid = response.json()["session_id"]
        with Database(tmp_path / "test.db").connect() as c:
            c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
            c.commit()
        response = client.post(f"/v1/sessions/{sid}/diagnostics/answer", json={
            "diagnostic_item_id": "diag-j3-discriminant-1", "answer": "0，一个不同的实数根", "client_action_id": "api-answer"})
        assert response.status_code == 200
        result = response.json()
        assert result["correctness"] == 1 and result["score_status"] == "needs_review"
        assert result["mastery_update_allowed"] is False
        assert result["feedback"] == "作答已保存，当前评分方式不能可靠确认对错，需要进一步核实。"


def save_hint(db, sid, item=ITEM, *, plan_item_id=None, question=None, hint_level=1):
    with db.connect() as c:
        prompt = c.execute("SELECT prompt FROM diagnostic_item WHERE diagnostic_item_id=?", (item,)).fetchone()[0]
    return StudyRepository(db).save_tutor_turn(sid, plan_item_id, "数学", question or prompt,
        "先检查运算顺序。", "local", "synthetic", "hint:" + str(len(history(db, "tutor_turn"))), hint_level=hint_level)


@pytest.mark.parametrize("hint_level", [1, 6])
def test_exact_tutor_assistance_keeps_correct_receipt_but_not_independence(flow, hint_level):
    db, learning, sid, clock = flow
    turn = save_hint(db, sid, hint_level=hint_level)
    clock[0] += timedelta(seconds=1)
    with db.connect() as c:
        assert had_recent_item_exposure(c, CHILD, ITEM, clock[0].isoformat())
        exposure = project_item_exposure(c, CHILD, ITEM, as_of=clock[0].isoformat())
        assert exposure["tutor_assisted"] and not exposure["answer_repeated"]
    result = learning.submit_diagnostic(sid, ITEM, "90", "after-hint")
    assert result["correctness"] == 1 and result["score_status"] == "verified_local"
    row = project(db, clock)["effective_evidence"][0]
    assert row["independent"] == 0 and row["hint_level"] == 0 and row["correctness"] == 1
    assert row["independence_reason"] == "tutor_assistance_within_24h"
    assert row["tutor_refs"][0]["turn_id"] == turn["turn_id"]
    assert state(db)["evidence_count"] == 1 and state(db)["last_independent_correct_at"] is None
    assert "independence_reason=tutor_assistance_within_24h" in history(db, "learning_evidence")[0]["evidence_summary"]
    clock[0] += timedelta(seconds=1)
    learning.submit_diagnostic(sid, SECOND, "42", "new-question")
    assert project(db, clock)["effective_evidence"][-1]["independent"] == 1


@pytest.mark.parametrize("kind", ["incomplete", "safety_blocked", "empty", "non_hint", "other_child", "unrelated_question", "expired", "future"])
def test_unmatched_tutor_turn_does_not_reduce_independence(flow, kind):
    db, learning, sid, clock = flow
    save_hint(db, sid)
    with db.connect() as c:
        if kind == "incomplete":
            c.execute("UPDATE tutor_turn SET status='failed'")
        elif kind == "safety_blocked":
            c.execute("UPDATE tutor_turn SET safety_status='blocked',answer='此请求已拦截。'")
        elif kind == "empty":
            c.execute("UPDATE tutor_turn SET answer=''")
        elif kind == "non_hint":
            c.execute("UPDATE tutor_turn SET response_kind='child_answer'")
        elif kind == "other_child":
            c.execute("UPDATE tutor_turn SET child_id=?", (OTHER,))
        elif kind == "unrelated_question":
            c.execute("UPDATE tutor_turn SET question='这个知识点怎么学？'")
        elif kind == "expired":
            clock[0] += timedelta(hours=24)
        else:
            c.execute("UPDATE tutor_turn SET created_at=?", ((clock[0] + timedelta(seconds=1)).isoformat(),))
        c.commit()
    learning.submit_diagnostic(sid, ITEM, "90", "answer")
    row = project(db, clock)["effective_evidence"][0]
    assert row["independent"] == 1 and row["tutor_refs"] == []


def test_replay_tutor_assistance_downgrades_legacy_flag_without_editing_receipt(flow):
    db, learning, sid, clock = flow
    save_hint(db, sid)
    clock[0] += timedelta(seconds=1)
    learning.submit_diagnostic(sid, ITEM, "90", "answer")
    with db.connect() as c:
        c.execute("UPDATE learning_evidence SET independent=1,evidence_summary='legacy'")
        c.execute("UPDATE mastery_state SET last_independent_correct_at=last_practiced_at")
        c.commit()
    evidence, events = history(db, "learning_evidence"), history(db, "mastery_update_event")
    learning.replay_mastery(CHILD, NODE, as_of=clock[0].isoformat())
    assert state(db)["evidence_count"] == 1 and state(db)["last_independent_correct_at"] is None
    assert history(db, "learning_evidence") == evidence and history(db, "mastery_update_event") == events
    from home_study_agent.learning_feedback import build_learning_feedback
    with db.connect() as c:
        feedback = build_learning_feedback(c, CHILD, now=clock[0])
    assert feedback["nodes"][0]["independent_correct"] == 0
    assert feedback["nodes"][0]["retention_success_count"] == 0


@pytest.mark.parametrize("foreign_binding", [False, True])
@pytest.mark.parametrize("use_alias", [False, True])
def test_tutor_bound_review_identifies_only_exact_item_with_owned_plan(flow, monkeypatch, foreign_binding, use_alias):
    db, learning, sid, clock = flow
    target = seed_alias(db, monkeypatch, "bound-alias", seed=SECOND) if use_alias else SECOND
    first = learning.submit_diagnostic(sid, ITEM, "wrong", "wrong")
    with db.connect() as c:
        review = c.execute("SELECT * FROM review_task WHERE source_evidence_id=? AND interval_label='48h'", (first["evidence_id"],)).fetchone()
        c.execute("""INSERT INTO daily_plan(plan_id,child_id,session_id,plan_date,status,total_minutes,generator,version,created_at,client_action_id)
            VALUES ('bound-plan',?,?,'2026-09-01','active',10,'test',1,?,'bound-plan')""", (OTHER if foreign_binding else CHILD, sid, clock[0].isoformat()))
        c.execute("""INSERT INTO plan_item(plan_item_id,plan_id,subject,title,expected_minutes,priority,status,position,source_type,source_ref)
            VALUES ('bound-item','bound-plan','Math','Review',10,1,'active',1,'due_review',?)""", (review["review_task_id"],))
        c.commit()
    clock[0] += timedelta(days=2)
    save_hint(db, sid, plan_item_id="bound-item", question="请讲解当前这道题。")
    clock[0] += timedelta(seconds=1)
    with db.connect() as c:
        exposure = project_item_exposure(c, CHILD, target, as_of=clock[0].isoformat())
        assert exposure["tutor_assisted"] is (not foreign_binding)
        assert project_item_exposure(c, CHILD, ITEM, as_of=clock[0].isoformat())["tutor_refs"] == []
    learning.submit_diagnostic(sid, target, "42", "answer")
    row = project(db, clock)["effective_evidence"][-1]
    assert row["independent"] == int(foreign_binding)
    if not foreign_binding:
        assert row["tutor_refs"][0]["matched_by"] == "bound_review_item"


def test_tutor_assisted_mistake_answer_does_not_pass_independent_gate(flow):
    db, learning, sid, clock = flow
    bad = learning.submit_diagnostic(sid, ITEM, "wrong", "wrong")
    service = MistakeBookService(db)
    case = service.capture_from_attempt(bad)["mistake_id"]
    service.confirm(case, CHILD, "confirm", "confirm")
    service.diagnose(case, CHILD, "unknown", "Possible cause", .9, "diagnose")
    service.add_remediation(case, CHILD, "active_recall", "completed", None, "remediate")
    save_hint(db, sid, SECOND)
    clock[0] += timedelta(seconds=1)
    result = service.submit_attempt(case, CHILD, "near_transfer", SECOND, "42", 0, "assisted-mistake")
    assert result["attempt_result"]["correctness"] == 1 and result["attempt_result"]["independent"] is False
    assert result["mastery_gate"]["satisfied"] == {}
    row = project(db, clock)["effective_evidence"][-1]
    assert row["correctness"] == 1 and row["independent"] == 0 and row["tutor_refs"]


def test_capture_rereads_low_quality_receipt_instead_of_trusting_status(flow):
    db, learning, sid, _ = flow
    low = learning.submit_diagnostic(sid, "diag-p4-english-phonics-1", "wrong", "low")
    forged = {**low, "score_status": "verified_local", "mastery_update_allowed": True}
    assert MistakeBookService(db).capture_from_attempt(forged) is None
    assert history(db, "mistake_case") == [] and history(db, "mistake_occurrence") == []
    with db.connect() as c:
        c.execute("UPDATE learning_evidence SET confidence=1,independent=1")
        c.commit()
    assert MistakeBookService(db).capture_from_attempt(forged) is None


@pytest.mark.parametrize("key,value", [("child_id", OTHER), ("session_id", "foreign"), ("evidence_id", "foreign"), ("diagnostic_item_id", SECOND)])
def test_capture_rejects_forged_receipt_identity(flow, key, value):
    db, learning, sid, _ = flow
    from home_study_agent.mistake_book import UnknownMistakeReferenceError
    receipt = learning.submit_diagnostic(sid, ITEM, "wrong", "wrong")
    with pytest.raises(UnknownMistakeReferenceError):
        MistakeBookService(db).capture_from_attempt({**receipt, key: value})
    assert history(db, "mistake_case") == []


def test_capture_practice_error_uses_real_score_confidence_and_preserves_mastery(flow):
    db, learning, sid, clock = flow
    learning.submit_diagnostic(sid, ITEM, "90", "first")
    before = state(db)
    clock[0] += timedelta(seconds=1)
    practice = learning.submit_diagnostic(sid, ITEM, "wrong", "practice")
    with db.connect() as c:
        c.execute("UPDATE learning_evidence SET confidence=.9 WHERE evidence_id=?", (practice["evidence_id"],))
        c.commit()
    service = MistakeBookService(db)
    case = service.capture_from_attempt({**practice, "score_status": "needs_review", "correctness": 1, "submitted_answer": "fake"})
    assert case["submitted_answer"] == "wrong" and case["confidence"] == .9
    assert case["status"] == "diagnosing" and case["confirmed_at"] is not None
    assert service.capture_from_attempt(practice)["mistake_id"] == case["mistake_id"]
    assert len(history(db, "mistake_occurrence")) == 1
    assert state(db)["mastery_probability"] == before["mastery_probability"]
    assert state(db)["evidence_count"] == 1


def test_tutor_assisted_delayed_review_cannot_claim_retention_after_independent_baseline(flow):
    db, learning, sid, clock = flow
    baseline = learning.submit_diagnostic(sid, ITEM, "90", "baseline")
    clock[0] += timedelta(days=2)
    with db.connect() as c:
        review = c.execute("SELECT * FROM review_task WHERE source_evidence_id=? AND interval_label='48h'", (baseline["evidence_id"],)).fetchone()
    save_hint(db, sid, SECOND)
    clock[0] += timedelta(seconds=1)
    response = learning.submit_diagnostic(sid, SECOND, "42", "delayed-assisted", review["review_task_id"])
    assert response["correctness"] == 1
    from home_study_agent.learning_feedback import build_learning_feedback
    with db.connect() as c:
        feedback = build_learning_feedback(c, CHILD, now=clock[0])
    node = next(n for n in feedback["nodes"] if n["knowledge_node_id"] == NODE)
    assert node["attempt_count"] == 2 and node["independent_correct"] == 1
    assert node["retention_success_count"] == 0 and node["status"] == "supported_only"


def test_other_child_owned_tutor_turn_never_reduces_current_child_independence(flow):
    db, learning, sid, clock = flow
    with db.connect() as c:
        c.execute("UPDATE daily_session SET status='completed' WHERE session_id=?", (sid,))
        c.commit()
    repo = StudyRepository(db)
    other_sid = repo.start_session(OTHER, "other-child")["session_id"]
    save_hint(db, other_sid, ITEM)
    with db.connect() as c:
        c.execute("UPDATE daily_session SET status='completed' WHERE session_id=?", (other_sid,))
        c.commit()
    current_sid = repo.start_session(CHILD, "current-child")["session_id"]
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (current_sid,))
        c.commit()
    clock[0] += timedelta(seconds=1)
    learning.submit_diagnostic(current_sid, ITEM, "90", "answer")
    assert project(db, clock)["effective_evidence"][0]["independent"] == 1


def test_auto_capture_receipt_validation_and_case_insert_rollback_together(flow):
    db, learning, sid, _ = flow
    receipt = learning.submit_diagnostic(sid, ITEM, "wrong", "wrong")
    evidence = history(db, "learning_evidence")
    with db.connect() as c:
        c.execute("""CREATE TRIGGER reject_occurrence BEFORE INSERT ON mistake_occurrence
            BEGIN SELECT RAISE(ABORT, 'occurrence unavailable'); END""")
        c.commit()
    with pytest.raises(sqlite3.IntegrityError, match="occurrence unavailable"):
        MistakeBookService(db).capture_from_attempt(receipt)
    assert history(db, "mistake_case") == [] and history(db, "mistake_cluster") == []
    assert history(db, "learning_evidence") == evidence


def seed_alias(db, monkeypatch, item_id, *, seed=ITEM, node=NODE, prompt=None, answer=None, stimulus_text=None, stimulus_asset=None):
    """Versioned synthetic catalog entry; the real source validator remains enabled."""
    original = next(row for row in learning_state.ITEMS if row[0] == seed)
    rubric = {**original[4]}
    if answer is not None:
        rubric.update(exact_answers=[answer], keywords=[answer])
    alias = (item_id, node, original[2] if prompt is None else prompt, original[3], rubric, original[5], original[6])
    if stimulus_text is not None or stimulus_asset is not None:
        monkeypatch.setattr(learning_state, "EXTENDED_ITEMS", (*learning_state.EXTENDED_ITEMS,
            (*alias, stimulus_text, stimulus_asset, "objective")))
    else:
        monkeypatch.setattr(learning_state, "ITEMS", (*learning_state.ITEMS, alias))
    LearningStateService(db).seed_catalog()
    with db.connect() as c:
        from home_study_agent.local_supply import validate_item
        validate_item(c, CHILD, item_id)
    return item_id


def test_question_identity_includes_all_visible_content_not_ids_answers_or_nodes():
    item = {"prompt": "  Compute  1 + 2\n", "stimulus_text": " Read\n this ", "stimulus_asset": "asset:one"}
    identity = question_identity(item)
    assert identity.startswith(QUESTION_IDENTITY_VERSION + ":")
    assert question_identity({**item, "prompt": "Compute 1 + 2", "stimulus_text": "Read this",
        "diagnostic_item_id": "new", "knowledge_node_id": "other", "expected_answer_json": '{"exact_answers":["999"]}'}) == identity
    assert question_identity({"prompt": "x"}) == question_identity({"prompt": "x", "stimulus_text": None, "stimulus_asset": ""})
    for field, value in (("prompt", "Compute 1 - 2"), ("stimulus_text", "Other material"), ("stimulus_asset", "asset:two")):
        assert question_identity({**item, field: value}) != identity
    assert question_identity({**item, "prompt": "compute 1 + 2"}) != identity
    with pytest.raises(ValueError):
        question_identity({"prompt": "   "})


def test_batch_matches_wrapper_including_conflicts_tutor_and_rejected_only_nodes(flow, monkeypatch):
    db, learning, sid, clock = flow
    other_node = "node-p4-math-fractions-decimals"
    alias = seed_alias(db, monkeypatch, "batch-conflict", node=other_node)
    learning.submit_diagnostic(sid, ITEM, "90", "original")
    clock[0] += timedelta(days=2)
    learning.submit_diagnostic(sid, alias, "90", "alias")
    save_hint(db, sid, SECOND)
    clock[0] += timedelta(seconds=1)
    learning.submit_diagnostic(sid, SECOND, "42", "assisted")
    learning.submit_diagnostic(sid, "diag-p4-english-phonics-1", "bag", "unqualified")
    with db.connect() as c:
        c.execute("PRAGMA query_only=ON")
        batch = project_child_mastery_evidence(c, CHILD, as_of=clock[0].isoformat())
        assert len(batch) == 3
        for node, projection in batch.items():
            assert projection == project_mastery_evidence(c, CHILD, node, as_of=clock[0].isoformat())
        assert batch[NODE]["identity_conflicts"] == batch[other_node]["identity_conflicts"]
        assert batch[other_node]["effective_evidence"] == []
        assert batch[NODE]["effective_evidence"][0]["independence_reason"] == "tutor_assistance_within_24h"
        assert batch[NODE]["effective_evidence"][0]["tutor_refs"]
        assert project_child_mastery_evidence(c, OTHER, as_of=clock[0].isoformat()) == {}
        empty = project_mastery_evidence(c, OTHER, NODE, as_of=clock[0].isoformat())
        assert set(empty) == set(batch[NODE])
        assert all(empty[key] == [] for key in ("effective_evidence", "all_effective_receipts",
            "excluded_evidence", "related_knowledge_node_ids", "identity_conflicts"))
        assert empty["next_review_at"] is None and empty["mastery_claim_allowed"] is False
        assert empty == project_mastery_evidence(c, OTHER, NODE, as_of=(clock[0] + timedelta(days=1)).isoformat())
    from home_study_agent import learning_obligations as obligations
    with db.connect() as c, patch.object(obligations, "_question_catalog", wraps=obligations._question_catalog) as catalog:
        replayed = learning._replay_mastery_scope(c, CHILD, NODE, clock[0].isoformat(), "test_scope")
        assert catalog.call_count == 1
        assert replayed["related_knowledge_node_ids"] == sorted([NODE, other_node])


def test_batch_is_call_local_withdrawal_and_source_changes_are_immediately_visible(flow):
    db, learning, sid, clock = flow
    wrong = learning.submit_diagnostic(sid, ITEM, "wrong", "wrong")
    good = learning.submit_diagnostic(sid, SECOND, "42", "good")
    book = MistakeBookService(db)
    case = book.capture_from_attempt(wrong)["mistake_id"]
    with db.connect() as c:
        before = project_child_mastery_evidence(c, CHILD, as_of=clock[0].isoformat())
        snapshot = json.dumps(before, sort_keys=True)
        book.confirm(case, CHILD, "exclude", "exclude")
        after = project_child_mastery_evidence(c, CHILD, as_of=clock[0].isoformat())
        assert [row["evidence_id"] for row in after[NODE]["effective_evidence"]] == [good["evidence_id"]]
        assert after[NODE]["excluded_evidence"][0]["reason"] == "withdrawn"
        assert after[NODE]["evidence_fingerprint"] != before[NODE]["evidence_fingerprint"]
        c.execute("UPDATE diagnostic_item SET stimulus_text='forged' WHERE diagnostic_item_id=?", (SECOND,))
        changed = project_child_mastery_evidence(c, CHILD, as_of=clock[0].isoformat())
        assert changed[NODE]["effective_evidence"] == []
        assert any(row["reason"] == "source_unqualified_or_reference_only" for row in changed[NODE]["excluded_evidence"])
        assert json.dumps(before, sort_keys=True) == snapshot


def test_batch_tutor_index_refreshes_on_next_call_without_rewriting_correctness(flow):
    db, learning, sid, clock = flow
    learning.submit_diagnostic(sid, ITEM, "90", "independent")
    with db.connect() as c:
        before = project_child_mastery_evidence(c, CHILD, as_of=clock[0].isoformat())
        save_hint(db, sid)
        after = project_child_mastery_evidence(c, CHILD, as_of=clock[0].isoformat())
        assert before[NODE]["effective_evidence"][0]["independent"] == 1
        assert after[NODE]["effective_evidence"][0]["independent"] == 0
        assert after[NODE]["effective_evidence"][0]["correctness"] == 1
        assert after[NODE]["evidence_fingerprint"] != before[NODE]["evidence_fingerprint"]
        c.execute("UPDATE tutor_turn SET safety_status='blocked' WHERE child_id=?", (CHILD,))
        assert project_child_mastery_evidence(c, CHILD, as_of=clock[0].isoformat()) == before
        assert c.execute("SELECT independent FROM learning_evidence").fetchone()[0] == 1


def test_closure_and_replay_share_one_batch_including_empty_state_nodes(flow):
    from home_study_agent import learning_obligations as obligations
    db, learning, sid, clock = flow
    learning.submit_diagnostic(sid, ITEM, "wrong", "wrong")
    learning.submit_diagnostic(sid, "diag-p4-fraction-decimal-closed-1", "0.3", "other-node")
    with db.connect() as c:
        c.execute("""INSERT INTO mastery_state(child_id,knowledge_node_id,mastery_probability,evidence_count,
            hint_dependency,status_confidence,algorithm_version,updated_at) VALUES (?,?,.9,99,0,1,'legacy',?)""",
            (CHILD, "node-p4-english-phonics-discrimination", clock[0].isoformat()))
        c.commit()
    with patch.object(obligations, "_question_catalog", wraps=obligations._question_catalog) as catalog:
        learning.replay_mastery(CHILD, as_of=clock[0].isoformat())
        assert catalog.call_count == 1
    assert state(db, node="node-p4-english-phonics-discrimination")["evidence_count"] == 0
    with db.connect() as c, patch.object(obligations, "_question_catalog", wraps=obligations._question_catalog) as catalog:
        learning._closure_material(c, sid)
        assert catalog.call_count == 1


def test_batch_and_complete_feedback_operation_counts_and_isolated_benchmark(flow, monkeypatch):
    from home_study_agent import learning_obligations as obligations, local_supply
    from home_study_agent.learning_feedback import build_learning_feedback
    db, learning, sid, clock = flow
    nodes, items = [], []
    for n in range(10):
        node = f"node-batch-benchmark-{n}"
        nodes.append((node, *learning_state.NODES[0][1:4], f"BATCH.BENCHMARK.{n}", *learning_state.NODES[0][5:]))
        for i in range(20):
            value = 1000 + 20 * n + i
            items.append((f"diag-batch-{n}-{i}", node, f"Compute {value} + 1. Answer one integer.", "short_answer",
                {"local_rule": "local_exact_v1", "exact_answers": [str(value + 1)]}, "Synthetic exact addition.", 1))
    monkeypatch.setattr(learning_state, "NODES", (*learning_state.NODES, *nodes))
    monkeypatch.setattr(learning_state, "ITEMS", (*learning_state.ITEMS, *items))
    learning.seed_catalog()
    with db.connect() as c:
        for i, item in enumerate(items):
            eid, occurred = f"benchmark-evidence-{i}", (clock[0] - timedelta(seconds=200-i)).isoformat()
            c.execute("""INSERT INTO learning_evidence(evidence_id,child_id,session_id,knowledge_node_id,evidence_type,
                correctness,hint_level,independent,confidence,evidence_summary,occurred_at,client_action_id)
                VALUES (?,?,?,?,'diagnostic_answer',1,0,1,1,'synthetic',?,?)""", (eid, CHILD, sid, item[1], occurred, eid))
            c.execute("""INSERT INTO diagnostic_attempt(attempt_id,diagnostic_item_id,evidence_id,child_id,session_id,
                submitted_answer,correctness,feedback,created_at,client_action_id) VALUES (?,?,?,?,?,?,1,'synthetic',?,?)""",
                (eid, item[0], eid, CHILD, sid, item[4]["exact_answers"][0], occurred, eid))
        c.commit()
        c.execute("PRAGMA query_only=ON")
        changes = c.total_changes
        measurements = {}
        def measure(name, operation):
            statements = []
            c.set_trace_callback(statements.append)
            with patch.object(local_supply, "score_local_item", wraps=local_supply.score_local_item) as score, \
                    patch.object(obligations, "_question_catalog", wraps=obligations._question_catalog) as catalog, \
                    patch.object(obligations, "_tutor_exposure_index", wraps=obligations._tutor_exposure_index) as tutor:
                started = perf_counter()
                result = operation()
                measurements[name] = {"seconds": round(perf_counter()-started, 4), "scores": score.call_count,
                    "catalog_loads": catalog.call_count, "tutor_loads": tutor.call_count, "sql": len(statements),
                    "child_evidence_scans": sum("SELECT * FROM learning_evidence WHERE child_id=" in sql for sql in statements)}
            c.set_trace_callback(None)
            return result
        batch = measure("batch", lambda: project_child_mastery_evidence(c, CHILD, as_of=clock[0].isoformat()))
        feedback = measure("complete_feedback", lambda: build_learning_feedback(c, CHILD, now=clock[0]))
        singles = measure("ten_compatibility_calls", lambda: {node[0]: project_mastery_evidence(c, CHILD, node[0],
            as_of=clock[0].isoformat()) for node in nodes})
        assert batch == singles
        assert len(batch) == len(feedback["nodes"]) == 10
        assert sum(len(row["effective_evidence"]) for row in batch.values()) == 200
        assert sum(row["attempt_count"] for row in feedback["nodes"]) == 200
        for name in ("batch", "complete_feedback"):
            assert measurements[name]["scores"] == 200
            assert measurements[name]["catalog_loads"] == measurements[name]["tutor_loads"] == measurements[name]["child_evidence_scans"] == 1
        assert measurements["ten_compatibility_calls"]["scores"] == 2000
        assert measurements["batch"]["sql"] < measurements["ten_compatibility_calls"]["sql"]
        assert c.total_changes == changes
        print("ISOLATED_MASTERY_BENCHMARK " + json.dumps(measurements, sort_keys=True))


@pytest.mark.parametrize("answer", [None, "999"])
def test_cross_id_exact_question_suppresses_dense_repeat_not_delayed_retest(flow, monkeypatch, answer):
    db, learning, sid, clock = flow
    original = next(row for row in learning_state.ITEMS if row[0] == ITEM)
    alias = seed_alias(db, monkeypatch, "same-question-alias", prompt="  " + original[2] + "\n", answer=answer)
    first = learning.submit_diagnostic(sid, ITEM, "90", "first")
    clock[0] += timedelta(seconds=1)
    with db.connect() as c:
        assert project_item_exposure(c, CHILD, alias, as_of=clock[0].isoformat())["answer_repeated"]
    repeat = learning.submit_diagnostic(sid, alias, answer or "90", "alias-repeat")
    assert repeat["score_status"] == "practice_only" and repeat["mastery_update_allowed"] is False
    assert repeat["question_identity"] == first["question_identity"]
    assert state(db)["evidence_count"] == 1
    clock[0] += timedelta(hours=24)
    later = learning.submit_diagnostic(sid, alias, answer or "90", "delayed-alias")
    rows = project(db, clock)["effective_evidence"]
    assert [r["evidence_id"] for r in rows] == [first["evidence_id"], later["evidence_id"]]
    assert len({r["diagnostic_item_id"] for r in rows}) == 2
    assert len({r["question_identity"] for r in rows}) == 1
    assert all(r["identity_version"] == QUESTION_IDENTITY_VERSION for r in rows)


@pytest.mark.parametrize("field", ["stimulus_text", "stimulus_asset"])
def test_legitimate_distinct_stimuli_are_not_collapsed(flow, monkeypatch, field):
    db, learning, sid, clock = flow
    one = seed_alias(db, monkeypatch, "material-one", **{field: "material-one"})
    two = seed_alias(db, monkeypatch, "material-two", **{field: "material-two"})
    learning.submit_diagnostic(sid, one, "90", "one")
    clock[0] += timedelta(seconds=1)
    learning.submit_diagnostic(sid, two, "90", "two")
    rows = project(db, clock)["effective_evidence"]
    assert len(rows) == 2 and all(r["independent"] == 1 for r in rows)
    assert len({r["question_identity"] for r in rows}) == 2


@pytest.mark.parametrize("field", ["stimulus_text", "stimulus_asset"])
def test_forged_stimulus_cannot_create_new_question_or_reliable_score(flow, monkeypatch, field):
    db, learning, sid, clock = flow
    alias = seed_alias(db, monkeypatch, "clean-alias")
    learning.submit_diagnostic(sid, ITEM, "90", "original")
    with db.connect() as c:
        c.execute(f"UPDATE diagnostic_item SET {field}='forged material' WHERE diagnostic_item_id=?", (ITEM,))
        c.commit()
        assert qualified_exact_score(c, CHILD, ITEM, "90") is None
        assert project_item_exposure(c, CHILD, alias, as_of=clock[0].isoformat())["answer_repeated"]
    clock[0] += timedelta(seconds=1)
    clean_repeat = learning.submit_diagnostic(sid, alias, "90", "clean-repeat")
    forged = learning.submit_diagnostic(sid, ITEM, "90", "forged-answer")
    assert clean_repeat["score_status"] == "practice_only"
    assert forged["score_status"] == "needs_review" and forged["mastery_update_allowed"] is False
    assert project(db, clock)["effective_evidence"] == []


def test_cross_node_identity_conflict_never_transfers_support_or_selects_first_node(flow, monkeypatch):
    db, learning, sid, clock = flow
    other_node = "node-p4-math-fractions-decimals"
    alias = seed_alias(db, monkeypatch, "cross-node-alias", node=other_node)
    original = learning.submit_diagnostic(sid, ITEM, "90", "original")
    unrelated = learning.submit_diagnostic(sid, "diag-p4-fraction-decimal-closed-1", "0.3", "unrelated")
    clock[0] += timedelta(days=2)
    conflicting = learning.submit_diagnostic(sid, alias, "90", "conflicting")
    assert conflicting["score_status"] == "needs_review"
    assert conflicting["score_reason"] == "question_identity_node_conflict"
    assert state(db)["evidence_count"] == 0
    assert state(db, node=other_node)["evidence_count"] == 1
    evidence, updates = history(db, "learning_evidence"), history(db, "mastery_update_event")
    with db.connect() as c:
        a = project_mastery_evidence(c, CHILD, NODE, as_of=clock[0].isoformat())
        b = project_mastery_evidence(c, CHILD, other_node, as_of=clock[0].isoformat())
    assert a["effective_evidence"] == []
    assert [r["evidence_id"] for r in b["effective_evidence"]] == [unrelated["evidence_id"]]
    assert any(r["evidence_id"] == original["evidence_id"] and r["reason"] == "question_identity_node_conflict" for r in a["excluded_evidence"])
    assert a["identity_conflicts"] == b["identity_conflicts"]
    learning.replay_mastery(CHILD, NODE, as_of=clock[0].isoformat())
    assert history(db, "learning_evidence") == evidence and history(db, "mastery_update_event") == updates


def test_same_identity_in_other_child_cannot_conflict_or_suppress(flow, monkeypatch):
    db, learning, sid, clock = flow
    alias = seed_alias(db, monkeypatch, "other-node-alias", node="node-p4-math-fractions-decimals")
    learning.submit_diagnostic(sid, ITEM, "90", "first-child")
    with db.connect() as c:
        c.execute("UPDATE daily_session SET status='completed' WHERE session_id=?", (sid,))
        c.execute("UPDATE child_profile SET grade=(SELECT grade FROM child_profile WHERE child_id=?) WHERE child_id=?", (CHILD, OTHER))
        c.commit()
    other_sid = StudyRepository(db).start_session(OTHER, "other-child")["session_id"]
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (other_sid,))
        c.commit()
    result = learning.submit_diagnostic(other_sid, alias, "90", "other-answer")
    assert result["score_status"] == "verified_local" and result["mastery_update_allowed"] is True
    assert state(db)["evidence_count"] == 1
    assert project(db, clock)["identity_conflicts"] == []


def test_real_qualified_variant_supply_aliases_share_identity_without_mocking_validation(flow):
    from home_study_agent.local_supply import validate_item
    from home_study_agent.variant_supply import VariantSupplyService

    class NoCloud:
        configured = False

        async def complete(self, *args, **kwargs):
            raise AssertionError("Only deterministic local supply is allowed")

    db, learning, sid, clock = flow
    mistakes, supply = MistakeBookService(db), VariantSupplyService(db, NoCloud())
    aliases = []
    for seed in (ITEM, SECOND):
        case = mistakes.capture(child_id=CHILD, knowledge_node_id=NODE, subject="Math", title="Synthetic",
            prompt_summary=seed, submitted_answer="wrong", source_type="manual", source_ref=seed,
            confidence=1, client_action_id="case:" + seed, diagnostic_item_id=seed)["mistake_id"]
        mistakes.confirm(case, CHILD, "confirm", "confirm:" + seed)
        mistakes.diagnose(case, CHILD, "unknown", "Synthetic review", .9, "diagnose:" + seed)
        supply.enqueue_local(case, CHILD, "supply:" + seed)
        assert supply.process_local_pending()["jobs"][0]["status"] == "completed"
        with db.connect() as c:
            candidate = c.execute("SELECT candidate_id FROM variant_candidate WHERE mistake_id=? AND stage='near_transfer'", (case,)).fetchone()[0]
        approved = supply.qualify_local(candidate, CHILD, "qualify:" + seed)
        with db.connect() as c:
            aliases.append(validate_item(c, CHILD, approved["diagnostic_item_id"]))
    assert aliases[0]["diagnostic_item_id"] != aliases[1]["diagnostic_item_id"]
    assert question_identity(aliases[0]) == question_identity(aliases[1])
    clock[0] = datetime.now(timezone.utc) + timedelta(seconds=1)
    first = learning.submit_diagnostic(sid, aliases[0]["diagnostic_item_id"], json.loads(aliases[0]["expected_answer_json"])["exact_answers"][0], "qualified-first")
    clock[0] += timedelta(seconds=1)
    repeat = learning.submit_diagnostic(sid, aliases[1]["diagnostic_item_id"], json.loads(aliases[1]["expected_answer_json"])["exact_answers"][0], "qualified-alias")
    assert first["score_status"] == "verified_local" and repeat["score_status"] == "practice_only"
    assert state(db)["evidence_count"] == 1
    with db.connect() as c:
        c.execute("UPDATE diagnostic_item SET stimulus_asset='forged' WHERE diagnostic_item_id=?", (aliases[1]["diagnostic_item_id"],))
        c.commit()
        assert qualified_exact_score(c, CHILD, aliases[1]["diagnostic_item_id"], "wrong") is None
