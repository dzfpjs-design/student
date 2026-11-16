"""Calendar-only change: original reference attempts are never mastery proofs."""
from datetime import date
import json

import pytest

from home_study_agent import pet_training_plan, repository
from home_study_agent.database import Database
from home_study_agent.learning_state import LearningStateService
from home_study_agent.pet_training_plan import allocate_pet_practice
from test_pet_training_plan import setup, CHILD


def record(setup, monkeypatch, at, skill='reading', key='original'):
    db, learning, sid = setup
    with db.connect() as c:
        item = c.execute("""SELECT di.diagnostic_item_id FROM diagnostic_item di JOIN knowledge_node kn USING(knowledge_node_id)
            WHERE kn.track='pet' AND kn.code LIKE ? AND di.status='active' ORDER BY di.diagnostic_item_id LIMIT 1""",
                         ('PET.' + skill.upper() + '.%',)).fetchone()[0]
    monkeypatch.setattr('home_study_agent.learning_state.utc_now', lambda: at.replace('Z', '+00:00'))
    return learning.submit_diagnostic(sid, item, 'I am not sure yet.', key)


def observe(db, at, day=None):
    with db.connect() as c:
        before = c.total_changes
        result = allocate_pet_practice(c, CHILD, 15, 0, today=day, as_of=at)
        assert c.total_changes == before
        assert not result['mastery_claim_allowed']
        return result


@pytest.mark.parametrize('answer,as_of,day,age', [
    ('2026-09-09T15:00:00+00:00', '2026-09-09T17:00:00+00:00', None, 1),
    ('2026-09-09T16:10:00+00:00', '2026-09-09T17:00:00+00:00', date(2026,9,10), 0),
    ('2026-09-09T23:59:59+00:00', '2026-09-10T00:00:00+00:00', None, 0),
    ('2026-09-09T15:59:59+00:00', '2026-09-09T16:00:00+00:00', None, 1),
    ('2026-09-09T16:00:00Z', '2026-09-09T16:00:00Z', None, 0),
    ('2026-09-09T16:00:01+00:00', '2026-09-09T16:00:00+00:00', None, None),
    ('2026-09-09T16:00:00+00:00', '2026-09-10T03:00:00+00:00', date(2026,9,9), None),
    ('2026-09-09T15:59:59+00:00', '2026-09-10T03:00:00+00:00', date(2026,9,9), 0),
])
def test_actual_attempts_use_family_day_and_exact_historical_cutoff(setup, monkeypatch, answer, as_of, day, age):
    record(setup, monkeypatch, answer)
    db = setup[0]
    with db.connect() as c:
        original = [tuple(row) for row in c.execute('SELECT * FROM diagnostic_attempt')]
    class WrongHost(date):
        @classmethod
        def today(cls):
            return date(2026,9,8)
    monkeypatch.setattr(pet_training_plan, 'date', WrongHost)
    result = observe(db, as_of, day)
    reading = next(row for row in result['opportunities'] if row['skill']=='reading')
    assert reading['days_since_practice'] == age
    assert reading['practice_days_14'] == int(age is not None)
    assert observe(Database(db.path), as_of, day) == result
    with db.connect() as c:
        assert [tuple(row) for row in c.execute('SELECT * FROM diagnostic_attempt')] == original


@pytest.mark.parametrize('bad', ['2026-09-09', '2026-09-09T10:00:00', 'not-a-time'])
def test_unknown_legacy_timestamp_is_not_current_opportunity_and_is_not_rewritten(setup, monkeypatch, bad):
    receipt = record(setup, monkeypatch, '2026-09-09T10:00:00+00:00')
    db = setup[0]
    with db.connect() as c:
        c.execute('UPDATE diagnostic_attempt SET created_at=? WHERE evidence_id=?', (bad, receipt['evidence_id']))
        c.commit()
    result = observe(db, '2026-09-09T12:00:00+00:00')
    assert next(v for v in result['opportunities'] if v['skill']=='reading')['days_since_practice'] is None
    with db.connect() as c:
        assert c.execute('SELECT created_at FROM diagnostic_attempt WHERE evidence_id=?', (receipt['evidence_id'],)).fetchone()[0] == bad


def test_future_attempt_does_not_affect_real_candidate_order(setup, monkeypatch):
    before = observe(setup[0], '2026-09-09T12:00:00+00:00')
    selected = next(item for item in before['items'] if item['task_type'] != 'memorization')
    monkeypatch.setattr('home_study_agent.learning_state.utc_now', lambda: '2026-09-09T12:00:01+00:00')
    setup[1].submit_diagnostic(setup[2], selected['source_ref'], 'I am not sure.', 'future-response')
    after = observe(setup[0], '2026-09-09T12:00:00+00:00')
    assert after['opportunities'] == before['opportunities']
    assert [v['source_ref'] for v in after['items']] == [v['source_ref'] for v in before['items']]


def test_family_day_changes_actual_protected_skill_selection(setup, monkeypatch):
    db = setup[0]
    record(setup, monkeypatch, '2026-09-03T10:00:00+00:00', skill='reading', key='reading-day3')
    record(setup, monkeypatch, '2026-09-03T17:00:00+00:00', skill='speaking', key='speaking-day4')
    with db.connect() as c:
        # Isolate two available skills; no media or scoring qualification added.
        c.execute("UPDATE diagnostic_item SET status='retired' WHERE knowledge_node_id IN (SELECT knowledge_node_id FROM knowledge_node WHERE code LIKE 'PET.LISTENING.%' OR code LIKE 'PET.WRITING.%')")
        c.commit()
    result = observe(db, '2026-09-10T06:00:00+00:00', date(2026,9,10))
    skills = {v['skill']: v for v in result['opportunities']}
    assert skills['reading']['available_items'] > 0 and skills['speaking']['available_items'] > 0
    assert skills['reading']['protection_due'] and not skills['speaking']['protection_due']
    selected = next(v for v in result['items'] if v['task_type']!='memorization')
    assert selected['title'].startswith('PET阅读'), result


def test_generated_plan_persists_family_day_opportunities_and_same_key_restart(setup, monkeypatch):
    db, _, sid = setup
    record(setup, monkeypatch, '2026-09-09T23:59:59+00:00')
    instant = '2026-09-10T00:00:00+00:00'
    monkeypatch.setattr(repository, 'utc_now', lambda: instant)
    class WrongHost(date):
        @classmethod
        def today(cls):
            return date(2026,9,9)
    monkeypatch.setattr(pet_training_plan, 'date', WrongHost)
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='homework_capture',homework_declared_empty=1,recall_declared_empty=1 WHERE session_id=?", (sid,))
        c.execute("UPDATE family_rule SET max_learning_minutes=120,pet_min_minutes=15,competition_minutes=0 WHERE child_id=?", (CHILD,))
        original = [tuple(row) for row in c.execute('SELECT * FROM diagnostic_attempt')]
        c.commit()
    repo = repository.StudyRepository(db)
    first = repo.generate_plan(sid, 'calendar-plan')
    assert first['plan']['plan_date'] == '2026-09-10'
    assert first['plan']['total_minutes'] <= 120
    with db.connect() as c:
        metadata = [json.loads(row[0]) for row in c.execute('SELECT metadata_json FROM plan_constraint_decision WHERE plan_id=?', (first['plan']['plan_id'],))]
    bases = [m['planning_evidence'] for m in metadata if 'opportunities' in m.get('planning_evidence', {})]
    assert bases, metadata
    assert all(next(v for v in b['opportunities'] if v['skill']=='reading')['days_since_practice']==0 for b in bases)
    restored = repository.StudyRepository(Database(db.path)).generate_plan(sid, 'calendar-plan')
    assert restored['plan']['plan_id'] == first['plan']['plan_id']
    with db.connect() as c:
        assert [tuple(row) for row in c.execute('SELECT * FROM diagnostic_attempt')] == original
        assert c.execute('SELECT COUNT(*) FROM daily_plan WHERE session_id=?', (sid,)).fetchone()[0] == 1
