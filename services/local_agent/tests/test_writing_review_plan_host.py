"""Real writing-stage receipts enter the daily planner without duplicate work."""
from datetime import datetime, timedelta

import pytest

from home_study_agent.repository import StudyRepository, PlanInputError
from home_study_agent.database import Database
from home_study_agent.writing_stage_reviews import reconcile_writing_stage_reviews
from test_writing_stage_reviews import writing_chain, complete_transfer_chain, CHILD


def due_case(case, monkeypatch):
    db = case[0]
    _, scored, _ = complete_transfer_chain(case)
    future = (datetime.fromisoformat(scored['attempt']['created_at']) + timedelta(days=8)).isoformat()
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        reconcile_writing_stage_reviews(c, CHILD, as_of=future)
        review = dict(c.execute("SELECT * FROM review_task WHERE child_id=? AND interval_label='writing_stage_7d'", (CHILD,)).fetchone())
        c.commit()
    monkeypatch.setattr('home_study_agent.repository.utc_now', lambda: future)
    return db, review, future


def test_bound_writing_review_passes_source_gate_without_exact_answer_fabrication(writing_chain, monkeypatch):
    db, review, _ = due_case(writing_chain, monkeypatch)
    with db.connect() as c:
        validated = StudyRepository._validate_review_source(c, review['review_task_id'], CHILD)
        assert validated == review
        with pytest.raises(PlanInputError):
            StudyRepository._validate_review_source(c, review['review_task_id'], 'wrong-child')


@pytest.mark.parametrize('budget', [120, 12])
def test_daily_plan_retains_one_writing_review_obligation(writing_chain, monkeypatch, budget):
    db, review, future = due_case(writing_chain, monkeypatch)
    repository = StudyRepository(db)
    sid = writing_chain[1]
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='homework_capture',homework_declared_empty=1 WHERE session_id=?", (sid,))
        c.execute('''UPDATE family_rule SET latest_end_time=NULL,max_learning_minutes=?,
            task_segment_minutes=30,pet_min_minutes=15,competition_minutes=0 WHERE child_id=?''', (budget, CHILD))
        # Keep the source dependency chain while making writing the available PET activity.
        c.execute("UPDATE diagnostic_item SET status='retired' WHERE knowledge_node_id IN (SELECT knowledge_node_id FROM knowledge_node WHERE track='pet' AND knowledge_node_id!=?)", (review['knowledge_node_id'],))
        c.commit()
    result = repository.generate_plan(sid, 'writing-retention-day')
    scheduled = [row for row in result['plan_items'] if row['source_ref'] in
        {review['review_task_id'], review['diagnostic_item_id']}]
    assert len(scheduled) == (1 if budget == 120 else 0), [(r['source_type'], r['source_ref']) for r in scheduled]
    if scheduled:
        assert scheduled[0]['source_type'] == 'due_review'
        assert scheduled[0]['expected_minutes'] == 12
    decisions = [row for row in result['constraint_decisions'] if row['source_ref'] in
        {review['review_task_id'], review['diagnostic_item_id']}]
    assert len(decisions) == 1, decisions
    assert decisions[0]['decision'] == ('scheduled' if budget == 120 else 'deferred')
    assert decisions[0]['requested_minutes'] == 12
    reopened = StudyRepository(Database(db.path)).get_workspace(sid)
    assert reopened['plan_items'] == result['plan_items']
    assert reopened['constraint_decisions'] == result['constraint_decisions']
    if scheduled:
        from home_study_agent.stage_execution import resolve_stage_execution
        with db.connect() as c:
            resolved = resolve_stage_execution(c, sid, scheduled[0]['plan_item_id'], as_of=future)
            assert resolved['status'] == 'ready', resolved
            assert resolved['review_task_id'] == review['review_task_id']
            assert resolved['diagnostic_item_id'] == review['diagnostic_item_id']
