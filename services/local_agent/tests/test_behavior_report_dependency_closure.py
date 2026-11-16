"""Temporary real SQLite: shared habit sources, no models or private media."""
import json
import sqlite3

import pytest

from home_study_agent.behavior_model import BehaviorModelService, ObservableEvent
from home_study_agent.daily_behavior_source import daily_behavior_fingerprint
from home_study_agent.period_behavior_source import period_behavior_fingerprint
from home_study_agent.database import Database
from home_study_agent.parent_reports import ParentReportService
from home_study_agent.repository import StudyRepository
from home_study_agent.reports import build_daily_report
from test_behavior_goal_confirmation import history, automatic_records, confirm, CHILD, NOW
from test_behavior_observation_coverage import managed


def fingerprints(c):
    return (daily_behavior_fingerprint(c, CHILD, 'session'),
            period_behavior_fingerprint(c, CHILD, '2026-08-01', '2026-08-31'))


def mutate(db, service, change):
    if change == 'goal_correction':
        opportunity = service.goal_behavior_opportunities(CHILD, 'goal', now=NOW)['opportunities'][-1]
        confirm(service, opportunity, success=False, key='closure-correction')
        return
    with db.connect() as c:
        c.execute("UPDATE homework_item SET confirmation_status='unconfirmed' WHERE homework_id='homework-11'"
                  if change == 'task_authority' else "DELETE FROM plan_item_event WHERE event_id='started-11'")
        c.commit()


@pytest.mark.parametrize('change', ['goal_correction', 'task_authority', 'task_start'])
def test_daily_approved_habit_result_loses_authority_and_preserves_parent_work(managed, change):
    db, service = history(managed, 'distraction_candidate')
    automatic_records(db, service)
    repo = StudyRepository(db)
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='reflection' WHERE session_id='session'")
        c.commit()
    repo.complete_with_reflection('session', '', '', 'okay', '', True, 'closure-finish')
    summary, coaching = build_daily_report(repo.report_evidence('session'))
    daily = repo.save_daily_report('session', summary, coaching)
    parents = ParentReportService(db)
    snapshot = parents.create_from_daily(daily)
    parents.edit_draft(snapshot['snapshot_id'], parent_note='Keep parent correction')
    parents.approve(snapshot['snapshot_id'])
    before = daily['evidence']['habit_experiments'][0]['outcome']
    with db.connect() as c:
        frozen = dict(c.execute('SELECT * FROM parent_report_snapshot WHERE snapshot_id=?', (snapshot['snapshot_id'],)).fetchone())
        assert parents.snapshot_source_current(c, frozen)
    mutate(db, service, change)
    after = service.report_safe_context(CHILD, 'session')['habit_experiments'][0]['outcome']
    assert before != after
    with db.connect() as c:
        c.execute('PRAGMA query_only=ON')
        assert not parents.snapshot_source_current(c, frozen)
        assert c.total_changes == 0
    reopened = StudyRepository(Database(db.path)).get_daily_report(daily['report_id'])
    assert reopened['evidence']['habit_experiments'][0]['outcome'] == after
    assert parents.get(snapshot['snapshot_id'])['parent_edit']['parent_note'] == 'Keep parent correction'


def test_unrelated_sessions_children_and_hidden_habits_do_not_invalidate(managed):
    db, service = history(managed, 'distraction_candidate')
    automatic_records(db, service)
    service.report_safe_context(CHILD, 'session')
    with db.connect() as c:
        before = fingerprints(c)
    service.ingest(ObservableEvent(child_id=CHILD, session_id='out-of-window', event_id='old-unrelated',
        occurred_at='2026-05-01T10:00:00Z', event_type='work.reading', source='local_vision', confidence=.9))
    service.ingest(ObservableEvent(child_id='child-junior-3', session_id='other-child', event_id='unrelated-child',
        occurred_at='2026-08-25T10:00:00Z', event_type='work.stall', source='local_vision', confidence=.9))
    with db.connect() as c:
        assert fingerprints(c) == before
        original = dict(c.execute("SELECT * FROM habit_experiment WHERE experiment_id='goal'").fetchone())
        for i in range(4):
            item = {**original, 'experiment_id': 'hidden-' + str(i), 'proposed_at': '2026-01-0' + str(i + 1),
                    'status': 'declined', 'started_at': None, 'ends_at': None, 'closed_at': None}
            c.execute('INSERT INTO habit_experiment(' + ','.join(item) + ') VALUES(' + ','.join('?' for _ in item) + ')', tuple(item.values()))
        c.commit()
        before = fingerprints(c)
        c.execute("UPDATE habit_experiment SET title='Hidden correction' WHERE experiment_id='hidden-0'")
        c.commit()
        assert fingerprints(c) == before


def test_uncommitted_change_rollback_readonly_and_backup_restore_have_exact_sources(managed, tmp_path):
    db, service = history(managed, 'distraction_candidate')
    automatic_records(db, service)
    service.report_safe_context(CHILD, 'session')
    with db.connect() as c:
        before = fingerprints(c)
        c.execute('BEGIN IMMEDIATE')
        c.execute("UPDATE homework_item SET confirmation_status='unconfirmed' WHERE homework_id='homework-11'")
        assert fingerprints(c) != before
        c.rollback()
        assert fingerprints(c) == before
        backup = sqlite3.connect(str(tmp_path / 'restored.db'))
        try:
            c.backup(backup)
            backup.execute('PRAGMA journal_mode=DELETE')
        finally:
            backup.close()
    restored = Database(tmp_path / 'restored.db')
    BehaviorModelService(restored).initialize(force_daily_rebuild=True)
    with restored.connect() as c:
        c.execute('PRAGMA query_only=ON')
        c.execute('BEGIN')
        assert fingerprints(c) == before
        assert c.total_changes == 0


def test_confirmation_payload_correction_not_just_id_invalidates(managed):
    db, service = history(managed, 'distraction_candidate')
    automatic_records(db, service)
    op = service.goal_behavior_opportunities(CHILD, 'goal', now=NOW)['opportunities'][-1]
    confirm(service, op, success=True, key='same-journal-id')
    service.report_safe_context(CHILD, 'session')
    with db.connect() as c:
        before = fingerprints(c)
        row = c.execute("SELECT event_id,payload_json FROM habit_experiment_event WHERE client_action_id='same-journal-id'").fetchone()
        payload = json.loads(row['payload_json'])
        payload['assistance_level'] = 'assisted'
        c.execute('UPDATE habit_experiment_event SET payload_json=? WHERE event_id=?', (json.dumps(payload), row['event_id']))
        c.commit()
        assert all(a != b for a, b in zip(before, fingerprints(c)))


def test_active_habit_cache_recomputed_at_later_clock_does_not_churn_source(managed):
    db, service = history(managed, 'distraction_candidate')
    automatic_records(db, service)
    with db.connect() as c:
        c.execute("UPDATE habit_experiment SET ends_at='2099-09-30T00:00:00Z',status='active',closed_at=NULL WHERE experiment_id='goal'")
        c.commit()
    service.report_safe_context(CHILD, 'session')
    with db.connect() as c:
        before = fingerprints(c)
        old_cache = c.execute("SELECT outcome_json FROM habit_experiment WHERE experiment_id='goal'").fetchone()[0]
    service.report_safe_context(CHILD, 'session')
    with db.connect() as c:
        assert old_cache != c.execute("SELECT outcome_json FROM habit_experiment WHERE experiment_id='goal'").fetchone()[0]
        assert fingerprints(c) == before
