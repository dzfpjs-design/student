"""Synthetic SQLite journals, including old derived caches and backup recovery."""
import sqlite3
import json
from datetime import datetime, timedelta, timezone

import pytest

from home_study_agent.behavior_model import BehaviorModelService
from home_study_agent.child_model import ChildModelService
from home_study_agent.database import Database
from test_calendar_audit_counterexamples import observe
from test_period_report_reliable_evidence import flow, learning_clock, CHILD

OTHER = 'child-junior-3'


def features(service):
    with service._connect() as c:
        return [tuple(row) for row in c.execute(
            'SELECT child_id,day,stall_count FROM daily_feature ORDER BY child_id,day')]


def originals(service):
    with service._connect() as c:
        return [tuple(row) for row in c.execute('SELECT * FROM observable_event ORDER BY event_id')]


def old_cache(service):
    with service._connect() as c:
        c.execute('DELETE FROM daily_feature')
        c.executemany('INSERT INTO daily_feature(child_id,day,stall_count) VALUES(?,?,?)',
            [(CHILD, '2026-09-06', 17), (OTHER, '2026-09-06', 99), ('no-original-child', '2026-09-01', 500)])
        c.commit()


def test_startup_and_backup_rebuild_only_raw_events_and_respect_retraction(tmp_path):
    service = BehaviorModelService(tmp_path / 'old.db')
    first = observe(service, '2026-09-06T16:00:00Z')
    observe(service, '2026-09-07T01:00:00+08:00')
    observe(service, '2026-09-06T17:00:00Z', child=OTHER)
    service.retract_event(first, CHILD, 'old-retraction', 'Synthetic correction')
    journal = originals(service)
    old_cache(service)
    restored_path = tmp_path / 'restored.db'
    with service._connect() as source:
        target = sqlite3.connect(restored_path)
        try:
            source.backup(target)
        finally:
            target.close()
    restored = BehaviorModelService(restored_path)
    expected = [(OTHER, '2026-09-07', 1), (CHILD, '2026-09-07', 1)]
    assert features(restored) == expected
    assert originals(restored) == journal
    assert features(BehaviorModelService(restored_path)) == expected
    assert restored.baseline('no-original-child')['days_observed'] == 0
    assert restored.baseline('no-original-child')['coverage']['observed_seconds'] is None
    assert restored.retract_event(first, OTHER, 'cross-child', 'Invalid owner')['status'] == 'not_found'
    assert features(restored) == expected


def test_managed_sidecar_migration_ignores_aggregate_only_rows(flow, tmp_path):
    source = BehaviorModelService(tmp_path / 'legacy.db')
    observe(source, '2026-09-06T16:00:00Z')
    journal = originals(source)
    old_cache(source)
    managed = BehaviorModelService(flow.db, legacy_path=source.path)
    managed.initialize()
    assert originals(managed) == journal
    assert features(managed) == [(CHILD, '2026-09-07', 1)]
    reopened = BehaviorModelService(Database(flow.db.path))
    reopened.initialize()
    assert features(reopened) == features(managed)


def test_rebuild_failure_rolls_back_derived_cache_and_leaves_journal(tmp_path):
    service = BehaviorModelService(tmp_path / 'rollback.db')
    observe(service, '2026-09-06T16:00:00Z')
    journal = originals(service)
    old_cache(service)
    before = features(service)
    with service._connect() as c:
        c.execute("CREATE TRIGGER reject_rebuild BEFORE INSERT ON daily_feature BEGIN SELECT RAISE(ABORT,'synthetic write fault'); END")
        c.commit()
    with pytest.raises(sqlite3.IntegrityError, match='synthetic write fault'):
        service.initialize()
    assert features(service) == before
    assert originals(service) == journal


def test_last_retraction_removes_empty_day_without_affecting_other_child(tmp_path):
    service = BehaviorModelService(tmp_path / 'withdraw.db')
    event = observe(service, '2026-09-06T16:00:00Z')
    observe(service, '2026-09-06T16:00:00Z', child=OTHER)
    service.retract_event(event, CHILD, 'withdraw-last', 'Synthetic correction')
    assert features(service) == [(OTHER, '2026-09-07', 1)]
    assert service.retract_event(event, CHILD, 'withdraw-last', 'Synthetic correction')['idempotent']
    assert service.baseline(CHILD)['days_observed'] == 0


def test_one_local_day_cannot_propose_two_day_habit(tmp_path):
    service = BehaviorModelService(tmp_path / 'habit.db')
    for at in ['2026-09-06T16:00:00Z', '2026-09-07T00:00:00Z', '2026-09-07T01:00:00Z']:
        observe(service, at)
    assert service.pending_habit_experiment(CHILD)['status'] == 'none'
    assert service.baseline(CHILD)['coverage']['observed_days'] == 1


def test_behavior_consumer_window_uses_local_day_and_excludes_future(flow):
    # These are deliberately prebuilt *local* day buckets, to isolate consumption.
    with flow.db.connect() as c:
        c.executemany('INSERT INTO daily_feature(child_id,day,stall_count) VALUES(?,?,?)',
            [(CHILD, '2026-08-10', 10), (CHILD, '2026-08-11', 1),
             (CHILD, '2026-09-07', 2), (CHILD, '2026-09-08', 100), (OTHER, '2026-09-07', 500)])
        c.commit()
        result = ChildModelService(flow.db)._behavior(c, CHILD, datetime(2026, 9, 6, 16, tzinfo=timezone.utc))
    assert (result['observed_days'], result['signals']['stall_candidates']) == (2, 3)


def test_live_privacy_restore_rebuilds_before_exposing_the_database(flow, tmp_path):
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    from privacy_restore_operation import restore_operation
    from synthetic_native_restore_request import signed_restore_post, KEY

    settings = Settings(data_dir=tmp_path.resolve(), database_path=flow.db.path,
                        data_encryption_key=b'0' * 32,
                        native_launch_attestation_key=KEY, maintenance_enabled=False)
    with TestClient(create_app(settings)) as client:
        authenticated = client.post('/v1/parent/setup', json={'pin': '4286'})
        assert authenticated.status_code == 200, authenticated.text
        headers = {'X-HSC-Parent': authenticated.json()['parent_token']}
        service = BehaviorModelService(flow.db)
        service.initialize()
        observe(service, '2026-09-06T16:00:00Z', session=flow.sid)
        journal = originals(service)
        old_cache(service)
        with flow.db.connect() as c:
            c.execute("UPDATE daily_session SET status='completed',state='completed'")
            c.commit()
        privacy = client.app.state.privacy_lifecycle
        backup = privacy.create_backup(prune=False)
        # Startup would repair this, but a live restore must not require a restart.
        service.initialize()
        restored = signed_restore_post(client, headers=headers, json={
            'backup_id': backup['backup_id'], 'confirmation': '\u786e\u8ba4\u6062\u590d',
            'operation': restore_operation(client, headers=headers)})
        assert restored.status_code == 200, restored.text
        assert restored.json()['status'] == 'restored'
        assert features(service) == [(CHILD, '2026-09-07', 1)]
        assert originals(service) == journal


def test_coverage_days_match_local_opportunity_days(tmp_path):
    from test_behavior_observation_coverage import begin, sample, put, at
    service = BehaviorModelService(tmp_path / 'coverage.db')
    # Fixture origin is Sep 1 10:00 UTC. Both intervals fall on Sep 2 in Nanjing.
    for epoch, seconds in [('before-utc-midnight', 7 * 3600), ('after-utc-midnight', 15 * 3600)]:
        begin(service, epoch=epoch, seconds=seconds)
        put(service, sample(0, seconds, epoch=epoch))
        put(service, sample(1, seconds + 2, epoch=epoch))
        service.stop_observation_stream('child-1', 'session-child-1', stream_epoch=epoch,
            sequence=2, reason='ended', at=at(seconds + 2), client_action_id='stop-' + epoch)
    result = service.observation_coverage('child-1', start_at=at(0), end_at=at(24 * 3600))
    assert result['observed_seconds'] == 4
    assert (result['observed_days'], result['strata'][0]['behavior_observed_days']) == (1, 1)
    assert features(service) == []  # Exposure is not an observed behavioral event.


def test_clean_startup_does_not_rebuild_but_same_count_timestamp_edit_does(tmp_path, monkeypatch):
    service = BehaviorModelService(tmp_path / 'dirty.db')
    event = observe(service, '2026-09-06T16:00:00Z')
    original_rebuild = service._rebuild_daily_features
    calls = []
    def tracked(c):
        calls.append(True)
        return original_rebuild(c)
    monkeypatch.setattr(service, '_rebuild_daily_features', tracked)
    service.initialize()
    assert calls == []
    with service._connect() as c:
        identity = tuple(c.execute('SELECT COUNT(*),MAX(occurred_at) FROM observable_event').fetchone())
        c.execute("UPDATE observable_event SET event_type='work.writing' WHERE event_id=?", (event,))
        assert identity == tuple(c.execute('SELECT COUNT(*),MAX(occurred_at) FROM observable_event').fetchone())
        c.commit()
    service.initialize()
    assert len(calls) == 1
    with service._connect() as c:
        assert tuple(c.execute('SELECT day,stall_count,writing_count FROM daily_feature').fetchone()) == ('2026-09-07', 0, 1)
    service.initialize()
    assert len(calls) == 1


def test_legacy_naive_calendar_bucket_is_not_qualified_observation(tmp_path):
    service = BehaviorModelService(tmp_path / 'naive.db')
    event = observe(service, '2026-09-06T16:00:00Z')
    with service._connect() as c:
        c.execute("UPDATE observable_event SET occurred_at='2026-09-06T16:00:00' WHERE event_id=?", (event,))
        c.commit()
    journal = originals(service)
    restarted = BehaviorModelService(service.path)
    assert features(restarted) == [(CHILD, '2026-09-06', 1)]
    coverage = restarted.baseline(CHILD)['coverage']
    assert coverage['event_count'] == coverage['observed_days'] == 0
    assert coverage['observed_seconds'] is None
    assert restarted.behavior_model_summary(CHILD, None, '2026-09-06')['event_count'] == 0
    assert restarted.report_safe_context(CHILD)['behavior_model_summary']['active_hypotheses'] == []
    assert originals(restarted) == journal


def test_projection_version_change_rebuilds_and_invalid_time_has_no_bucket(tmp_path, monkeypatch):
    from home_study_agent.behavior_model import service as module
    service = BehaviorModelService(tmp_path / 'version.db')
    event = observe(service, '2026-09-06T16:00:00Z')
    with service._connect() as c:
        c.execute("UPDATE observable_event SET occurred_at='not-a-date' WHERE event_id=?", (event,))
        # A version change must rebuild even if a historical marker claims clean.
        c.execute('UPDATE behavior_daily_projection SET dirty=0')
        c.commit()
    monkeypatch.setattr(module, 'DAILY_PROJECTION_VERSION', 'synthetic-next-policy')
    service.initialize()
    assert features(service) == []
    assert originals(service)[0][3] == 'not-a-date'


def test_legacy_effect_samples_on_one_local_day_do_not_satisfy_two_day_gate():
    from home_study_agent.behavior_model.outcome_evidence import effect_summary
    rows = []
    for index, stamp in enumerate(['2026-09-06T16:00:00Z', '2026-09-06T17:00:00Z',
                                 '2026-09-07T00:00:00Z', '2026-09-07T01:00:00Z']):
        instant = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
        rows.append({'source_event_id': str(index), 'intervention_id': str(index),
            'intervention_type': 'default_ask_first', 'feedback': 'accepted',
            'source': 'local_vision', 'confidence': .9, 'payload_json': '{}', 'missing_sources_json': '[]',
            'decided_at': stamp, 'feedback_at': (instant + timedelta(minutes=1)).isoformat(),
            'outcome_json': json.dumps({'state': 'writing', 'probability': .9,
                'observed_at': (instant + timedelta(minutes=2)).isoformat()})})
    result = effect_summary(rows, datetime(2026, 9, 8, tzinfo=timezone.utc))
    assert result['effect_samples'] == 4
    assert (result['effect_days'], result['effect_status']) == (1, 'insufficient_evidence')
