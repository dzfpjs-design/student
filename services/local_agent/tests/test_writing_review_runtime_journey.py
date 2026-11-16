"""Daily writing review -> Runtime start -> original -> rubric -> saved task."""
from datetime import datetime, timedelta, timezone

import pytest

from home_study_agent.agent_capability_executor import AgentCapabilityExecutor
from home_study_agent.database import Database
from home_study_agent.diagnostic_prompt import DiagnosticPromptService
from home_study_agent.learning_rubric import RubricEvidenceService
from home_study_agent.repository import StudyRepository, PlanInputError
from home_study_agent.writing_response_origin import WritingOriginalService
from test_goal_task_prompt import command
from test_writing_stage_reviews import writing_chain, complete_transfer_chain, CHILD
from test_writing_stage_review_consumers import advance_clock, review_due


def start_due_review(writing_chain, monkeypatch):
    db, previous_sid = writing_chain[:2]
    _, far, _ = complete_transfer_chain(writing_chain)
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='completed',status='completed',ended_at=? WHERE session_id=?",
                  (far['attempt']['created_at'], previous_sid))
        c.execute("UPDATE agent_objective SET status='completed',completed_at=? WHERE child_id=? AND status='active'",
            (far['attempt']['created_at'], CHILD))
        c.commit()
    clock = advance_clock(monkeypatch, datetime.fromisoformat(far['attempt']['created_at']) + timedelta(days=8))
    from home_study_agent import learning_input_replan
    monkeypatch.setattr(learning_input_replan, 'utc_now', lambda: clock.now(timezone.utc))
    repo = StudyRepository(db)
    sid = repo.start_session(CHILD, 'writing-review-next-day')['session_id']
    assert sid != previous_sid
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='homework_capture',homework_declared_empty=1 WHERE session_id=?", (sid,))
        c.execute('''UPDATE family_rule SET latest_end_time=NULL,max_learning_minutes=120,
            task_segment_minutes=30,pet_min_minutes=15,competition_minutes=0 WHERE child_id=?''', (CHILD,))
        c.commit()
    workspace = repo.generate_plan(sid, 'review-runtime-plan')
    slot = next(item for item in workspace['plan_items'] if item['source_type'] == 'due_review')
    turn_id = command((db, sid, slot['plan_item_id'], None, None), child_id=CHILD)
    executor = AgentCapabilityExecutor(db, repo)
    started = executor.execute(turn_id)
    assert started['result']['status'] == 'active', started
    prompt = started['result']['diagnostic_prompt']['prompt']
    assert prompt['review_task_id'] == slot['source_ref']
    assert AgentCapabilityExecutor(Database(db.path), StudyRepository(Database(db.path))).execute(turn_id) == started
    return db, sid, slot, turn_id, prompt


@pytest.mark.parametrize('score', [0, 3])
def test_writing_review_daily_task_needs_original_and_review_then_recovers(writing_chain, monkeypatch, score):
    db, sid, slot, _, prompt = start_due_review(writing_chain, monkeypatch)
    repo = StudyRepository(db)
    with pytest.raises(PlanInputError):
        repo.complete_plan_item(slot['plan_item_id'], 1, 'empty-review-completion')
    text = 'Dear Ben, our science club meets on Tuesdays in the library. I want to build a tall bridge with my friends because we enjoy designing useful things. Please join us next Tuesday and bring your ideas. Best wishes, Mia.'
    original = WritingOriginalService(db).capture_native_response(sid, prompt_id=prompt['prompt_id'],
        item_version=prompt['item_version'], text=text, client_action_id='runtime-review-original')
    answer = DiagnosticPromptService(db).answer(sid, prompt_id=prompt['prompt_id'], item_version=prompt['item_version'],
        answer=text, writing_original_artifact_id=original['artifact_id'], client_action_id='runtime-review-answer')
    with pytest.raises(PlanInputError):
        repo.complete_plan_item(slot['plan_item_id'], 1, 'reference-review-completion')
    assessment, body = review_due(db, sid, prompt, text, answer, score=score)
    RubricEvidenceService(db).review(CHILD, assessment['assessment_id'], **body)
    result = repo.complete_plan_item(slot['plan_item_id'], 1, 'verified-review-completion')
    assert result['status'] == 'completed'
    reopened = StudyRepository(Database(db.path))
    assert reopened.complete_plan_item(slot['plan_item_id'], 1, 'verified-review-completion')['status'] == 'completed'
    with db.connect() as c:
        task = c.execute('SELECT * FROM review_task WHERE review_task_id=?', (slot['source_ref'],)).fetchone()
        assert task['status'] == 'completed' and task['result_evidence_id'] == answer['attempt']['evidence_id']
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE review_task_id=?', (slot['source_ref'],)).fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id=? AND event_type='started'", (slot['plan_item_id'],)).fetchone()[0] == 1


@pytest.mark.parametrize('change', ['cancel', 'source'])
def test_prepared_writing_review_cannot_accept_answer_after_authority_changes(writing_chain, monkeypatch, change):
    db, sid, slot, turn_id, prompt = start_due_review(writing_chain, monkeypatch)
    with db.connect() as c:
        if change == 'cancel':
            c.execute("UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id=?", (turn_id,))
        else:
            c.execute("UPDATE diagnostic_item SET status='withdrawn' WHERE diagnostic_item_id=?", (prompt['diagnostic_item_id'],))
        c.commit()
    service = DiagnosticPromptService(Database(db.path))
    assert service.read(sid, prompt_id=prompt['prompt_id'])['status'] == 'invalidated'
    refused = service.answer(sid, prompt_id=prompt['prompt_id'], item_version=prompt['item_version'],
                             answer='An answer after cancellation', client_action_id='late-cancelled-answer')
    assert refused['status'] == 'invalidated' and refused['attempt'] is None
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE review_task_id=?', (slot['source_ref'],)).fetchone()[0] == 0
