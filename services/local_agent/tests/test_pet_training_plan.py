from datetime import date, timedelta

import pytest

from home_study_agent.database import Database
from home_study_agent.learning_state import LearningStateService
from home_study_agent.pet_training_plan import allocate_pet_practice
from home_study_agent.repository import StudyRepository


CHILD = 'child-primary-4'


@pytest.fixture
def setup(tmp_path):
    db = Database(tmp_path / 'pet-planning.db')
    db.initialize()
    repo = StudyRepository(db)
    repo.seed_family()
    learning = LearningStateService(db)
    learning.seed_catalog()
    sid = repo.start_session(CHILD, 'pet-planning-session')['session_id']
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
        c.commit()
    return db, learning, sid


def test_four_skills_receive_opportunities_even_with_due_vocabulary_every_day(setup, monkeypatch):
    db, learning, sid = setup
    observed = set()
    first = date(2026, 9, 7)
    for index in range(14):
        day = first + timedelta(days=index)
        monkeypatch.setattr('home_study_agent.learning_state.utc_now', lambda day=day: day.isoformat() + 'T12:00:00+00:00')
        with db.connect() as c:
            result = allocate_pet_practice(c, CHILD, 15, 30, today=day)
        assert sum(item['minutes'] for item in result['items']) <= 15
        vocabulary = next(item for item in result['items'] if item['task_type'] == 'memorization')
        skill = next(item for item in result['items'] if item['task_type'] != 'memorization')
        assert vocabulary['minutes'] <= 5 and skill['minutes'] >= 5
        observed.add(skill['title'].split(' · ')[0])
        learning.submit_diagnostic(sid, skill['source_ref'], 'I am not sure', f'pet-opportunity-{index}')
        assert not result['mastery_claim_allowed']
    assert observed == {'PET听力', 'PET口语', 'PET阅读', 'PET写作'}
    with db.connect() as c:
        repeated = allocate_pet_practice(c, CHILD, 15, 30, today=day)
    with Database(db.path).connect() as c:
        assert allocate_pet_practice(c, CHILD, 15, 30, today=day) == repeated


def test_missing_listening_stimulus_is_explicit_gap_not_vocabulary_substitution(setup):
    db, _, _ = setup
    with db.connect() as c:
        c.execute("UPDATE diagnostic_item SET stimulus_text=NULL WHERE knowledge_node_id='node-pet-listening-detail'")
        c.commit()
        result = allocate_pet_practice(c, CHILD, 10, 100)
        assert 'listening' in result['missing_skills']
        assert all(not item['title'].startswith('PET听力') for item in result['items'])
        assert any(item['task_type'] != 'memorization' for item in result['items'])


def test_insufficient_budget_and_other_child_do_not_create_extra_time_or_borrow_content(setup):
    db, _, _ = setup
    with db.connect() as c:
        tiny = allocate_pet_practice(c, CHILD, 3, 20)
        assert sum(item['minutes'] for item in tiny['items']) == 3
        other = allocate_pet_practice(c, 'child-junior-3', 15, 0)
        assert other['items'] == []
        assert set(other['missing_skills']) == {'listening', 'speaking', 'reading', 'writing'}
