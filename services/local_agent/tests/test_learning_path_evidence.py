from datetime import timedelta
from unittest.mock import patch

import pytest

from home_study_agent import learning_obligations, learning_state
from home_study_agent.learning_state import LearningStateService
from home_study_agent.mistake_book import MistakeBookService
from test_mastery_evidence_replay import CHILD, ITEM, NODE, OTHER, SECOND, flow, history


TARGET = "node-p4-math-fractions-decimals"


@pytest.fixture
def path_flow(flow):
    db, _, _, _ = flow
    with db.connect() as c:
        c.execute("""INSERT INTO knowledge_edge(edge_id,source_node_id,target_node_id,relation_type,rationale,
            source_ref,knowledge_version,status) VALUES ('test-path',?,?,'prerequisite_of','Synthetic prerequisite',
            'synthetic','test','active')""", (NODE, TARGET))
        c.commit()
    return flow


def path(db):
    with db.connect() as c:
        c.execute("PRAGMA query_only=ON")
        return LearningStateService._path_context(c, CHILD, TARGET)["prerequisites"][0]


def retained(path_flow):
    db, service, sid, clock = path_flow
    first = service.submit_diagnostic(sid, ITEM, "90", "baseline")
    clock[0] += timedelta(days=1)
    second = service.submit_diagnostic(sid, SECOND, "42", "different-question")
    clock[0] += timedelta(days=2)
    with db.connect() as c:
        review = c.execute("SELECT * FROM review_task WHERE source_evidence_id=? AND interval_label='48h'", (first["evidence_id"],)).fetchone()
    last = service.submit_diagnostic(sid, SECOND, "42", "delayed", review["review_task_id"])
    assert last["score_status"] == "verified_local"
    return first, second, last


def test_historical_high_cache_cannot_create_readiness_or_change_question_priority(path_flow):
    db, service, _, clock = path_flow
    before = service.next_diagnostic(CHILD, "school", subject="数学")
    with db.connect() as c:
        for node, probability in ((NODE, .99), (TARGET, .01)):
            c.execute("""INSERT INTO mastery_state(child_id,knowledge_node_id,mastery_probability,evidence_count,
                hint_dependency,status_confidence,algorithm_version,updated_at) VALUES (?,?,?,999,0,1,'legacy',?)""",
                (CHILD, node, probability, clock[0].isoformat()))
        c.commit()
    current = path(db)
    assert current["mastery_probability"] == .99
    assert current["mastery_fields_source"] == "historical_cache"
    assert current["mastery_claim_allowed"] is False
    assert current["readiness"] == "needs_evidence"
    assert current["current_evidence"]["evidence_refs"] == []
    assert service.next_diagnostic(CHILD, "school", subject="数学")["diagnostic_item_id"] == before["diagnostic_item_id"]


def test_ready_requires_current_independent_distinct_and_valid_delayed_receipts(path_flow):
    db, service, _, clock = path_flow
    first, second, last = retained(path_flow)
    with db.connect() as c:
        c.execute("UPDATE mastery_state SET mastery_probability=.01,status_confidence=0 WHERE child_id=?", (CHILD,))
        c.commit()
    ready = path(db)
    assert ready["readiness"] == "ready" and ready["mastery_probability"] == .01
    assert ready["mastery_claim_allowed"] is False
    assert ready["current_evidence"]["independent_distinct_items"] == 2
    assert set(ready["current_evidence"]["evidence_refs"]) == {row["evidence_id"] for row in (first, second, last)}
    assert ready["current_evidence"]["retention_basis_refs"]
    with db.connect() as c:
        assert LearningStateService._path_context(c, OTHER, TARGET)["prerequisites"][0]["readiness"] == "needs_evidence"
    clock[0] += timedelta(days=91)
    assert path(db)["readiness"] == "needs_evidence"
    assert len(history(db, "diagnostic_attempt")) == 3


@pytest.mark.parametrize("invalidation", ["hint", "confidence", "withdrawn_source", "retired_item"])
def test_path_revalidates_next_read_without_rewriting_high_cache(path_flow, invalidation):
    db, _, _, _ = path_flow
    first, _, last = retained(path_flow)
    assert path(db)["readiness"] == "ready"
    if invalidation == "withdrawn_source":
        book = MistakeBookService(db)
        case = book.capture(child_id=CHILD, knowledge_node_id=NODE, subject="Math", title="Review source",
            prompt_summary="Synthetic review source", submitted_answer="90", source_type="manual", source_ref=None,
            confidence=1, client_action_id="capture-withdrawal", source_evidence_id=first["evidence_id"], diagnostic_item_id=ITEM)
        book.confirm(case["mistake_id"], CHILD, "exclude", "withdrawal")
    with db.connect() as c:
        if invalidation == "hint":
            c.execute("UPDATE learning_evidence SET hint_level=1,independent=0 WHERE evidence_id=?", (last["evidence_id"],))
        elif invalidation == "confidence":
            c.execute("UPDATE learning_evidence SET confidence=.35 WHERE evidence_id=?", (last["evidence_id"],))
        elif invalidation == "retired_item":
            c.execute("UPDATE diagnostic_item SET status='inactive' WHERE diagnostic_item_id=?", (SECOND,))
        c.execute("UPDATE mastery_state SET mastery_probability=.99,status_confidence=1 WHERE child_id=?", (CHILD,))
        c.commit()
    assert path(db)["readiness"] == "needs_evidence"


def test_same_question_delayed_success_is_not_multiple_independent_questions(path_flow):
    db, service, sid, clock = path_flow
    first = service.submit_diagnostic(sid, ITEM, "90", "baseline")
    clock[0] += timedelta(days=2)
    with db.connect() as c:
        review = c.execute("SELECT * FROM review_task WHERE source_evidence_id=? AND interval_label='48h'", (first["evidence_id"],)).fetchone()
        c.execute("UPDATE review_task SET diagnostic_item_id=? WHERE review_task_id=?", (ITEM, review["review_task_id"]))
        c.commit()
    service.submit_diagnostic(sid, ITEM, "90", "same-question-delayed", review["review_task_id"])
    current = path(db)
    assert current["current_evidence"]["status"] == "retention_observed"
    assert current["current_evidence"]["independent_distinct_items"] == 1
    assert current["readiness"] == "needs_evidence"


def test_dense_repeat_and_hint_do_not_unlock_prerequisite(path_flow):
    db, service, sid, clock = path_flow
    service.submit_diagnostic(sid, ITEM, "90", "first")
    clock[0] += timedelta(seconds=1)
    service.submit_diagnostic(sid, ITEM, "90", "repeat")
    service.submit_diagnostic(sid, SECOND, "42", "hinted", hint_level=1)
    current = path(db)
    assert current["current_evidence"]["attempt_count"] == 2
    assert current["current_evidence"]["independent_distinct_items"] == 1
    assert current["readiness"] == "needs_evidence"


def test_all_read_consumers_share_one_batch_and_ignore_invalidated_old_aggregates(path_flow):
    db, service, sid, _ = path_flow
    source = service.submit_diagnostic(sid, ITEM, "wrong", "source")
    book = MistakeBookService(db)
    book.capture_from_attempt(source)
    with db.connect() as c:
        c.execute("UPDATE learning_evidence SET confidence=.35 WHERE evidence_id=?", (source["evidence_id"],))
        c.execute("UPDATE mastery_state SET mastery_probability=.99,evidence_count=999,status_confidence=1 WHERE child_id=?", (CHILD,))
        c.commit()
    original = {table: history(db, table) for table in ("learning_evidence", "mastery_state", "diagnostic_attempt", "review_task")}
    consumers = [(lambda: service.dashboard(CHILD), "items"), (lambda: service.graph(CHILD), "nodes"),
                 (lambda: book.weakness_clusters(CHILD), "items")]
    for read, key in consumers:
        with patch.object(learning_obligations, "_question_catalog", wraps=learning_obligations._question_catalog) as catalog:
            result = read()
            assert catalog.call_count == 1
        row = next(row for row in result[key] if row["knowledge_node_id"] == NODE)
        assert row["mastery_probability"] == .99 and row["mastery_fields_source"] == "historical_cache"
        assert row["current_evidence"]["attempt_count"] == 0 and row["current_evidence"]["evidence_refs"] == []
        assert row["readiness"] == "needs_evidence" and row["mastery_claim_allowed"] is False
    with patch.object(learning_obligations, "_question_catalog", wraps=learning_obligations._question_catalog) as catalog:
        service.next_diagnostic(CHILD, "school")
        assert catalog.call_count == 1
    assert {table: history(db, table) for table in original} == original
