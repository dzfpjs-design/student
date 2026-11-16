"""PET completion binds real item identity, never a seed-name convention."""
from datetime import datetime, timedelta, timezone

import pytest

from home_study_agent.repository import StudyRepository, PlanInputError
from home_study_agent.learning_state import LearningStateService
from test_writing_source_publication import prepared, publish, CHILD


@pytest.fixture
def planned(tmp_path):
    db, service, spec, _ = prepared(tmp_path)
    published = publish(service, spec)
    repository = StudyRepository(db)
    sid = repository.start_session(CHILD, 'published-plan')['session_id']
    now = datetime.now(timezone.utc)
    started = (now - timedelta(minutes=2)).isoformat()
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
        c.execute('INSERT INTO daily_plan VALUES(?,?,?,?,?,?,?,?,?,?)',
            ('plan', CHILD, sid, now.date().isoformat(), 'active', 10, 'isolated', 1, started, 'plan'))
        c.execute('''INSERT INTO plan_item(plan_item_id,plan_id,subject,title,expected_minutes,priority,
            status,position,started_at,source_type,source_ref,task_type) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
            ('slot', 'plan', '英语', 'PET writing', 10, 1, 'active', 1, started,
             'pet_daily', published['diagnostic_item_id'], 'writing'))
        c.commit()
    return db, repository, sid, published


@pytest.mark.parametrize('source', ['published', 'unknown', 'empty'])
def test_nonseed_pet_cannot_complete_without_the_bound_response(planned, source):
    db, repository, _, _ = planned
    if source != 'published':
        with db.connect() as c:
            c.execute('UPDATE plan_item SET source_ref=? WHERE plan_item_id=?',
                ('unknown-item' if source == 'unknown' else None, 'slot'))
            c.commit()
    with pytest.raises(PlanInputError):
        repository.complete_plan_item('slot', 2, 'complete')
    with db.connect() as c:
        assert c.execute("SELECT status FROM plan_item WHERE plan_item_id='slot'").fetchone()[0] == 'active'
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='completed'").fetchone()[0] == 0


def test_reference_answer_alone_does_not_complete_a_writing_stage(planned):
    db, repository, sid, published = planned
    result = LearningStateService(db).submit_diagnostic(sid, published['diagnostic_item_id'],
        'I attended a concert. I liked the music. Please come with me next time.', 'written-answer')
    assert result['diagnostic_item_id'] == published['diagnostic_item_id']
    with pytest.raises(PlanInputError):
        repository.complete_plan_item('slot', 2, 'complete')
    with db.connect() as c:
        assert c.execute("SELECT independent FROM learning_evidence WHERE evidence_id=?", (result['evidence_id'],)).fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='completed'").fetchone()[0] == 0
