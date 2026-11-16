"""Public synthetic frames; the HTTP admission must precede every evidence write."""
import base64
import json
from datetime import timedelta

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from test_behavior_observation_coverage import managed, ZERO, at

EPOCH = 'public-epoch-001'

def frame():
    pixels = np.tile(np.array([80, 180], dtype=np.uint8), (240, 160))
    return base64.b64encode(cv2.imencode('.png', pixels)[1]).decode()

def body(epoch=EPOCH, sequence=1):
    return {'frame_base64': frame(), 'capture_sample': {
        'stream_epoch': epoch, 'sequence': sequence, 'captured_at': at(sequence * 2),
        'capture_monotonic_ms': sequence * 2000,
    }}

def evidence(db):
    with db.connect() as c:
        return {table: [tuple(row) for row in c.execute('SELECT * FROM ' + table + ' ORDER BY rowid')]
                for table in ('child_state_event', 'observation_qualification', 'observable_event',
                              'probabilistic_state', 'intervention', 'daily_feature')}

@pytest.fixture
def actual(managed, tmp_path, monkeypatch):
    db, _, _, _ = managed
    app = create_app(Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False))
    engine = app.state.observation_engine
    engine._now = lambda: ZERO + timedelta(seconds=2)
    def classify(gray, brightness, sharpness, motion, tracker, now):
        tracker.face_seen_at = now
        return 'present', .9, 'public synthetic classification'
    monkeypatch.setattr(engine, '_classify', classify)
    with TestClient(app) as client:
        yield db, app, client, engine, classify

def start(client, epoch=EPOCH, action='public-start'):
    result = client.post('/v1/sessions/session/observation/stream/start', json={
        'stream_epoch': epoch, 'client_action_id': action})
    assert result.status_code == 200, result.text
    return result

def test_unknown_epoch_rejects_before_analysis_or_evidence(actual):
    db, app, client, engine, _ = actual
    before = evidence(db)
    result = client.post('/v1/sessions/session/observation-frame', json=body())
    assert result.status_code == 409, result.text
    assert result.json()['code'] == 'OBSERVATION_CAPTURE_STALE'
    assert evidence(db) == before
    assert not engine._sessions and not engine._coverage_analysis

def test_unbound_frame_is_explicit_framing_only_without_tracker_or_claim(actual):
    db, app, client, engine, _ = actual
    before = evidence(db)
    result = client.post('/v1/sessions/session/observation-frame', json={'frame_base64': frame()})
    assert result.status_code == 200, result.text
    data = result.json()
    assert data['coverage_record'] == {'status': 'unavailable', 'reason': 'CAPTURE_CLOCK_MISSING'}
    assert data['analysis_scope'] == 'framing_only'
    assert '缺采样绑定仅检查取景' in data['reason']
    assert data['observation_ready'] is False and data['longitudinal_recorded'] is False
    assert data['qualification']['longitudinal_inference_enabled'] is False
    assert evidence(db) == before
    assert not engine._sessions and not engine._coverage_analysis

def test_real_second_connection_stop_during_analysis_prevents_all_frame_writes(actual, monkeypatch):
    db, app, client, engine, classify = actual
    start(client)
    stopped = []
    def stopping(*args):
        result = app.state.behavior_integration.stop_observation_stream('session', stream_epoch=EPOCH,
            sequence=0, reason='disconnected', client_action_id='public-cross-actor-stop')
        stopped.append(result)
        return classify(*args)
    monkeypatch.setattr(engine, '_classify', stopping)
    before = evidence(db)
    result = client.post('/v1/sessions/session/observation-frame', json=body())
    assert stopped and result.status_code == 409, result.text
    assert result.json()['code'] == 'OBSERVATION_CAPTURE_STALE'
    after = evidence(db)
    assert {k:v for k,v in after.items() if k != 'observable_event'} == {k:v for k,v in before.items() if k != 'observable_event'}
    assert len(after['observable_event']) == len(before['observable_event']) + 1
    assert not engine._sessions and not engine._coverage_analysis

@pytest.mark.parametrize('change', ['revision', 'generation', 'mode'])
def test_second_connection_server_binding_change_during_analysis_rejects(actual, monkeypatch, change):
    db, app, client, engine, classify = actual
    start(client)
    changed = []
    def update(*args):
        # A real second original Database.connect commits before the frame writer starts.
        with db.connect() as other:
            if change == 'revision':
                other.execute("UPDATE daily_session SET observation_revision=observation_revision+1 WHERE session_id='session'")
            elif change == 'generation':
                other.execute("UPDATE privacy_storage_generation SET generation='public-generation-replaced' WHERE singleton=1")
            else:
                other.execute("UPDATE family_rule SET observation_mode='shadow' WHERE child_id='child-primary-4'")
            other.commit()
        changed.append(True)
        return classify(*args)
    monkeypatch.setattr(engine, '_classify', update)
    before = evidence(db)
    result = client.post('/v1/sessions/session/observation-frame', json=body())
    assert changed and result.status_code == 409, result.text
    assert result.json()['code'] == 'OBSERVATION_CAPTURE_STALE'
    assert evidence(db) == before
    assert not engine._sessions and not engine._coverage_analysis


def test_valid_frame_durable_and_replay_rejected_without_more_effects(actual):
    db, app, client, engine, _ = actual
    start(client)
    result = client.post('/v1/sessions/session/observation-frame', json=body())
    assert result.status_code == 200, result.text
    data = result.json()
    assert data['state_event_id'] and data['coverage_record']['status'] == 'recorded'
    assert data['qualification']['status'] == 'qualified' and data['longitudinal_recorded'] is True
    before = evidence(db)
    assert len(before['child_state_event']) == 1
    replay = client.post('/v1/sessions/session/observation-frame', json=body())
    assert replay.status_code == 409 and replay.json()['code'] == 'OBSERVATION_CAPTURE_STALE'
    assert evidence(db) == before
    with db.connect() as c:
        assert c.execute('PRAGMA foreign_key_check').fetchall() == []


def test_new_epoch_is_not_poisoned_by_old_stop_and_does_not_inherit_person_baseline(actual, monkeypatch):
    db, app, client, engine, _ = actual
    start(client)
    assert client.post('/v1/sessions/session/observation-frame', json=body()).status_code == 200
    start(client, 'public-epoch-002', 'public-second-start')
    stopped = client.post('/v1/sessions/session/observation/stream/stop', json={
        'stream_epoch': EPOCH, 'sequence': 1, 'reason': 'restarted', 'client_action_id': 'old-stop-after-new'})
    assert stopped.status_code == 200
    before = evidence(db)
    old = client.post('/v1/sessions/session/observation-frame', json=body(sequence=2))
    assert old.status_code == 409 and evidence(db) == before
    monkeypatch.setattr(engine, '_classify', lambda *args: ('unknown', .2, 'no visible person'))
    new = client.post('/v1/sessions/session/observation-frame', json=body('public-epoch-002'))
    assert new.status_code == 200, new.text
    assert new.json()['person_baseline']['status'] == 'awaiting_person'
    assert new.json()['observation_ready'] is False
    assert new.json()['longitudinal_recorded'] is False
    assert evidence(db)['child_state_event'] == before['child_state_event']


def test_unbound_preview_cannot_seed_next_bound_frame_person_baseline(actual, monkeypatch):
    db, app, client, engine, _ = actual
    assert client.post('/v1/sessions/session/observation-frame', json={'frame_base64': frame()}).status_code == 200
    start(client)
    monkeypatch.setattr(engine, '_classify', lambda *args: ('unknown', .2, 'no visible person'))
    result = client.post('/v1/sessions/session/observation-frame', json=body())
    assert result.status_code == 200, result.text
    assert result.json()['person_baseline']['status'] == 'awaiting_person'
    assert result.json()['longitudinal_recorded'] is False
    assert evidence(db)['child_state_event'] == []


@pytest.mark.parametrize('stage', ['coverage', 'qualification', 'longitudinal'])
def test_bound_frame_failure_rolls_back_all_effects_and_does_not_accept_tracker(actual, stage):
    db, app, client, engine, _ = actual
    start(client)
    with db.connect() as c:
        target, condition = {
            'coverage': ('observable_event', "WHEN NEW.event_type='observation.sample'"),
            'qualification': ('observation_qualification', ''),
            'longitudinal': ('observable_event', "WHEN NEW.source='local_vision'"),
        }[stage]
        c.execute(f"CREATE TRIGGER public_fail_coverage BEFORE INSERT ON {target} {condition} BEGIN SELECT RAISE(ABORT,'public capture failure'); END")
        c.commit()
    before = evidence(db)
    with pytest.raises(Exception, match='public capture failure'):
        client.post('/v1/sessions/session/observation-frame', json=body())
    assert evidence(db) == before
    assert not engine._sessions and not engine._coverage_analysis
    with db.connect() as c:
        c.execute('DROP TRIGGER public_fail_coverage')
        c.commit()
    result = client.post('/v1/sessions/session/observation-frame', json=body())
    assert result.status_code == 200, result.text
    assert len(evidence(db)['child_state_event']) == 1


def test_invalid_capture_clock_has_finite_rejection_without_evidence(actual):
    db, app, client, engine, _ = actual
    start(client)
    value = body()
    value['capture_sample']['captured_at'] = 'public-not-a-timestamp'
    before = evidence(db)
    result = client.post('/v1/sessions/session/observation-frame', json=value)
    assert result.status_code == 409 and result.json()['code'] == 'OBSERVATION_CAPTURE_STALE'
    assert evidence(db) == before and not engine._sessions


def test_borrowed_frame_service_preserves_outer_transaction_and_rejects_foreign_db(managed, tmp_path):
    from home_study_agent.database import Database
    from home_study_agent.restore_publication import RestorePublicationError
    db, service, _, _ = managed
    foreign = Database(tmp_path / 'foreign.db')
    foreign.initialize()
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute("UPDATE family_rule SET observation_mode='shadow' WHERE child_id='child-primary-4'")
        service.replay('child-primary-4', 'session', connection=c)
        assert c.in_transaction
        assert c.execute("SELECT observation_mode FROM family_rule WHERE child_id='child-primary-4'").fetchone()[0] == 'shadow'
        c.rollback()
    with db.connect() as c:
        assert c.execute("SELECT observation_mode FROM family_rule WHERE child_id='child-primary-4'").fetchone()[0] == 'gentle'
    with foreign.connect() as other:
        other.execute('BEGIN IMMEDIATE')
        with pytest.raises(RestorePublicationError, match='ORIGINAL_CONNECTION_READ_REQUIRED'):
            service.replay('child-primary-4', connection=other)
        assert other.in_transaction
        other.rollback()


@pytest.mark.parametrize('kind', ['unknown', 'deleted'])
def test_missing_original_session_is_finite_stale_without_effects(actual, kind):
    db, app, client, engine, _ = actual
    sid = 'public-missing-session'
    if kind == 'deleted':
        with db.connect() as c:
            c.execute("UPDATE daily_session SET status='completed' WHERE status='active'")
            c.execute("INSERT INTO daily_session(session_id,child_id,status,state,started_at,last_checkpoint_at,client_action_id) VALUES(?,'child-junior-3','active','study',?,?,'public-to-delete')", (sid, at(0), at(0)))
            c.commit()
        opened = client.post(f'/v1/sessions/{sid}/observation/stream/start', json={
            'stream_epoch': EPOCH, 'client_action_id': 'public-deleted-opening'})
        assert opened.status_code == 200, opened.text
        with db.connect() as c:
            c.execute('DELETE FROM daily_session WHERE session_id=?', (sid,))
            c.commit()
    before = evidence(db)
    result = client.post(f'/v1/sessions/{sid}/observation-frame', json=body())
    assert result.status_code == 409, result.text
    assert result.json()['code'] == 'OBSERVATION_CAPTURE_STALE'
    assert evidence(db) == before
    assert sid not in engine._sessions and sid not in engine._coverage_analysis


@pytest.mark.parametrize('change', ['paused', 'completed'])
def test_real_second_connection_session_retirement_during_analysis_is_stale(actual, monkeypatch, change):
    db, app, client, engine, classify = actual
    start(client)
    def retire(*args):
        with db.connect() as other:
            if change == 'paused':
                other.execute("UPDATE daily_session SET observation_status='paused',observation_revision=observation_revision+1 WHERE session_id='session'")
            else:
                other.execute("UPDATE daily_session SET status='completed' WHERE session_id='session'")
            other.commit()
        return classify(*args)
    monkeypatch.setattr(engine, '_classify', retire)
    before = evidence(db)
    result = client.post('/v1/sessions/session/observation-frame', json=body())
    assert result.status_code == 409 and result.json()['code'] == 'OBSERVATION_CAPTURE_STALE'
    assert evidence(db) == before and not engine._sessions and not engine._coverage_analysis


def test_bound_invalid_png_preserves_original_422(actual):
    db, app, client, engine, _ = actual
    start(client)
    value = body()
    value['frame_base64'] = base64.b64encode(b'public not a PNG frame' * 3).decode()
    before = evidence(db)
    result = client.post('/v1/sessions/session/observation-frame', json=value)
    assert result.status_code == 422 and result.json()['code'] == 'INVALID_OBSERVATION_FRAME'
    assert evidence(db) == before and not engine._sessions


def test_small_original_api_acceptance_script_keeps_all_checks(tmp_path):
    import importlib.util
    from pathlib import Path
    path = Path(__file__).parents[1] / 'scripts/run_observation_quality_api_acceptance.py'
    spec = importlib.util.spec_from_file_location('public_observation_api_script', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    report = module.run(tmp_path / 'public-api-acceptance')
    assert all(report['checks'].values()), report['checks']
    assert report['aggregate']['active_leases'] == 0
    assert report['aggregate']['raw_media_files_stored'] == 0
