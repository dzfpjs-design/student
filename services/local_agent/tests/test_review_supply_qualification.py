from datetime import timedelta
from uuid import uuid4

import pytest

from home_study_agent.database import Database
from home_study_agent.learning_state import LearningStateService
from home_study_agent.repository import PlanInputError, StudyRepository
from test_mastery_evidence_replay import CHILD, ITEM, NODE, OTHER, SECOND, flow, history


@pytest.fixture
def keyword_item(flow, monkeypatch):
    from home_study_agent import learning_state
    db, service, _, _ = flow
    item_id = "diag-000-review-reference"
    monkeypatch.setattr(learning_state, "ITEMS", (*learning_state.ITEMS,
        (item_id, NODE, "Explain the order of operations.", "text", {"keywords": ["multiply"]}, "Synthetic reference.", 1)))
    service.seed_catalog()
    return item_id


def legacy_review(db, source_id, now, *, target=SECOND, label="immediate", child=CHILD):
    review_id = str(uuid4())
    with db.connect() as c:
        c.execute("""INSERT INTO review_task(review_task_id,child_id,knowledge_node_id,diagnostic_item_id,
            source_evidence_id,interval_label,due_at,status,created_at) VALUES (?,?,?,?,?,?,?,'pending',?)""",
            (review_id, child, NODE, target, source_id, label, now, now))
        c.commit()
    return review_id


@pytest.mark.parametrize("item", ["diag-pet-daily-dialogue-1", "diag-oly-number-sense-1", "diag-p4-english-phonics-1"])
def test_reference_scores_never_create_mandatory_reviews(flow, item):
    db, service, sid, _ = flow
    answer = service.submit_diagnostic(sid, item, "12", "reference")
    assert answer["score_status"] == "needs_review"
    assert history(db, "review_task") == []
    assert len(history(db, "diagnostic_attempt")) == 1


def test_both_schedulers_choose_exact_alternative_and_fallback_to_exact_original(flow, keyword_item):
    db, service, sid, clock = flow
    answer = service.submit_diagnostic(sid, ITEM, "wrong", "source")
    with db.connect() as c:
        service._schedule_next_day_review(c, CHILD, NODE, ITEM, answer["evidence_id"], clock[0].isoformat())
        c.commit()
        assert {r[0] for r in c.execute("SELECT diagnostic_item_id FROM review_task")} == {SECOND}
        c.execute("UPDATE diagnostic_item SET status='inactive' WHERE knowledge_node_id=? AND diagnostic_item_id!=?", (NODE, ITEM))
        assert service._select_review_item(c, CHILD, NODE, ITEM) == ITEM
        c.execute("UPDATE diagnostic_item SET stimulus_asset='forged' WHERE diagnostic_item_id=?", (ITEM,))
        assert service._select_review_item(c, CHILD, NODE, ITEM) is None
        for r in c.execute("SELECT review_task_id FROM review_task"):
            with pytest.raises(PlanInputError):
                StudyRepository._validate_review_source(c, r[0], CHILD)


@pytest.mark.parametrize("corruption", ["target", "source_score", "source_item", "source_repeat", "source_child"])
def test_legacy_pending_review_fails_closed_without_rebinding_or_history_edits(flow, keyword_item, corruption):
    db, service, sid, clock = flow
    answer = service.submit_diagnostic(sid, ITEM, "wrong", "source")
    with db.connect() as c:
        review = dict(c.execute("SELECT * FROM review_task WHERE interval_label='immediate'").fetchone())
    if corruption == "source_repeat":
        clock[0] += timedelta(seconds=1)
        repeated = service.submit_diagnostic(sid, ITEM, "90", "repeat")
    with db.connect() as c:
        if corruption == "target":
            c.execute("UPDATE review_task SET diagnostic_item_id=? WHERE review_task_id=?", (keyword_item, review["review_task_id"]))
        elif corruption == "source_score":
            c.execute("UPDATE learning_evidence SET confidence=.35 WHERE evidence_id=?", (answer["evidence_id"],))
        elif corruption == "source_item":
            c.execute("UPDATE diagnostic_item SET stimulus_text='forged' WHERE diagnostic_item_id=?", (ITEM,))
        elif corruption == "source_repeat":
            c.execute("UPDATE review_task SET source_evidence_id=? WHERE review_task_id=?", (repeated["evidence_id"], review["review_task_id"]))
        else:
            c.execute("UPDATE learning_evidence SET child_id=? WHERE evidence_id=?", (OTHER, answer["evidence_id"]))
        c.commit()
    before = {name: history(db, name) for name in ("review_task", "learning_evidence", "diagnostic_attempt")}
    with Database(db.path).connect() as c:
        c.execute("PRAGMA query_only=ON")
        with pytest.raises(PlanInputError):
            StudyRepository._validate_review_source(c, review["review_task_id"], CHILD)
    selected = service.next_diagnostic(CHILD, "school")
    assert selected is None or selected.get("review_task_id") != review["review_task_id"]
    for name, rows in before.items():
        assert history(db, name) == rows


def test_bad_legacy_target_is_deferred_visibly_and_cannot_hide_valid_same_node_review(flow, keyword_item):
    db, service, sid, clock = flow
    answer = service.submit_diagnostic(sid, ITEM, "wrong", "source")
    bad = legacy_review(db, answer["evidence_id"], clock[0].isoformat(), target=keyword_item, label="next_day")
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='homework_capture',homework_declared_empty=1,recall_declared_empty=1 WHERE session_id=?", (sid,))
        c.execute("UPDATE family_rule SET latest_end_time='23:59',max_learning_minutes=90 WHERE child_id=?", (CHILD,))
        c.commit()
    workspace = StudyRepository(db).generate_plan(sid, "plan")
    due = [row for row in workspace["plan_items"] if row["source_type"] == "due_review"]
    assert len(due) == 1 and due[0]["source_ref"] != bad
    with db.connect() as c:
        decision = c.execute("SELECT * FROM plan_constraint_decision WHERE source_ref=?", (bad,)).fetchone()
        assert decision["decision"] == "deferred" and "REVIEW_ITEM_UNQUALIFIED" in decision["reason"]
        assert c.execute("SELECT status FROM review_task WHERE review_task_id=?", (bad,)).fetchone()[0] == "pending"


def test_retired_pending_source_does_not_block_new_next_day_supply(flow):
    db, service, sid, clock = flow
    first = service.submit_diagnostic(sid, ITEM, "wrong", "first")
    stale = legacy_review(db, first["evidence_id"], clock[0].isoformat(), label="next_day")
    with db.connect() as c:
        c.execute("UPDATE learning_evidence SET confidence=.35 WHERE evidence_id=?", (first["evidence_id"],))
        c.commit()
    clock[0] += timedelta(days=2)
    second = service.submit_diagnostic(sid, ITEM, "wrong", "second")
    with db.connect() as c:
        service._schedule_next_day_review(c, CHILD, NODE, ITEM, second["evidence_id"], clock[0].isoformat())
        rows = c.execute("SELECT review_task_id,source_evidence_id FROM review_task WHERE interval_label='next_day'").fetchall()
        assert len(rows) == 2 and any(row[0] == stale for row in rows)
        assert any(row[1] == second["evidence_id"] for row in rows)
