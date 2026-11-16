"""Synthetic realtime text admitted into the canonical voice records, not fake WAVs."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

import pytest

from home_study_agent.voice_capture_binding import VoiceCaptureBindingError, VoiceCaptureBindingService, resolve_voice_capture
from home_study_agent.database import LATEST_SCHEMA_VERSION
from home_study_agent.app import create_app
from test_diagnostic_prompt_http import http, environment, start, CHILD


def admit(db, sid, key='realtime-one'):
    service = VoiceCaptureBindingService(db)
    source = service.begin_realtime(sid, key, 'agent_command', {'child_id': CHILD}, model_version='synthetic-cloud-asr')
    return service, source


@pytest.mark.parametrize('fault', [False, True])
def test_same_version_capture_repair_is_backed_up_and_rolls_back_on_failure(http, monkeypatch, fault):
    from home_study_agent import voice_capture_schema
    _client, db, sid = http
    service, _source = admit(db, sid)
    original = service.complete_realtime(sid, 'realtime-one', '合成原话')
    with db.connect() as c:
        c.execute('ALTER TABLE voice_capture_binding DROP COLUMN activity_reference_json')
        c.commit()
    real = voice_capture_schema.ensure_schema
    if fault:
        def fail_after_repair(c):
            real(c)
            c.execute('DELETE FROM voice_capture_binding')
            c.commit()
            raise RuntimeError('synthetic same-version repair failure')
        monkeypatch.setattr(voice_capture_schema, 'ensure_schema', fail_after_repair)
        with pytest.raises(RuntimeError, match='same-version repair failure'):
            db.initialize()
    else:
        db.initialize()
    status = db.latest_upgrade_status()
    assert status and status['snapshot_available']
    assert status['source_schema_version'] == status['target_schema_version'] == LATEST_SCHEMA_VERSION
    assert status['status'] == ('restored_after_failure' if fault else 'completed')
    with db.connect() as c:
        row = c.execute('SELECT * FROM voice_capture_binding WHERE voice_input_id=?', (original['voice_input_id'],)).fetchone()
        assert row['status'] == 'completed'
        assert ('activity_reference_json' in row.keys()) is not fault
        if not fault:
            assert row['activity_reference_json'] is None
    if not fault:
        before = len(list((db.path.parent / 'upgrade-backups').glob('pre-upgrade-*.json')))
        db.initialize()
        assert len(list((db.path.parent / 'upgrade-backups').glob('pre-upgrade-*.json'))) == before


def test_readonly_realtime_admission_receipt_survives_restart_without_replay(environment):
    settings, db = environment
    settings = replace(settings, session_token='synthetic-local-session-token')
    headers = {'X-HSC-Session': settings.session_token}
    with TestClient(create_app(settings), headers=headers) as client:
        sid = start(client)
        root = f'/v1/sessions/{sid}/voice/realtime'
        path = root + '/actions/receipt-original'
        assert client.get(path, headers={'X-HSC-Session': 'wrong-local-token'}).status_code == 401
        missing = client.get(path, headers=headers)
        assert missing.status_code == 200, missing.text
        assert missing.json()['status'] == 'not_found'
        begin = client.post(root + '/begin', headers=headers, json={'client_action_id': 'receipt-original',
            'model_label': 'synthetic-asr', 'captured_at_ms': int(datetime.now(timezone.utc).timestamp()*1000),
            'capture_reference': {'child_id': CHILD}})
        assert begin.status_code == 200, begin.text
        prepared = client.get(path, headers=headers).json()
        assert prepared['status'] == 'prepared' and prepared['voice_input_id'] is None
        complete = client.post(root + '/complete', headers=headers, json={
            'client_action_id': 'receipt-original', 'transcript': '合成孩子完整原话，不应出现在恢复状态响应'})
        assert complete.status_code == 200, complete.text
        # Synthetic closed-session fixture permits the other child's session;
        # historical delivery checks must remain read-only after closure.
        with db.connect() as c:
            c.execute("UPDATE daily_session SET status='completed' WHERE session_id=?", (sid,))
            c.commit()
        other_sid = start(client, child='child-junior-3', action='other-receipt-session')
        assert client.get(f'/v1/sessions/{other_sid}/voice/realtime/actions/receipt-original', headers=headers).json()['status'] == 'not_found'
    with TestClient(create_app(settings)) as restarted:
        recovered = restarted.get(path, headers=headers)
        assert recovered.status_code == 200, recovered.text
        assert recovered.headers['cache-control'] == 'no-store'
        assert recovered.json() == {'session_id': sid, 'child_id': CHILD, 'client_action_id': 'receipt-original',
            'status': 'completed', 'voice_input_id': complete.json()['voice_input_id'],
            'local_only': True, 'cloud_transmission_allowed': False, 'replay_allowed': False, 'mastery_update_allowed': False}
        assert '原话' not in recovered.text
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM voice_input').fetchone()[0] == 1
            assert not c.execute('SELECT 1 FROM agent_turn').fetchone()
        stopped = restarted.post(f'/v1/sessions/{sid}/asr/actions/receipt-original/cancel', headers=headers)
        assert stopped.status_code == 200, stopped.text
        assert restarted.get(path, headers=headers).json()['status'] == 'cancelled'


def test_cancellation_without_admission_has_a_readonly_recovery_receipt(http):
    client, _db, sid = http
    assert client.post(f'/v1/sessions/{sid}/asr/actions/cancelled-onset/cancel').status_code == 200
    result = client.get(f'/v1/sessions/{sid}/voice/realtime/actions/cancelled-onset')
    assert result.status_code == 200, result.text
    assert result.json()['status'] == 'cancelled'
    assert result.json()['voice_input_id'] is None


def test_readonly_receipt_cannot_accept_an_action_rebound_to_another_child(http):
    client, db, sid = http
    service, _source = admit(db, sid)
    original = service.complete_realtime(sid, 'realtime-one', '合成原话')
    with db.connect() as c:
        c.execute("UPDATE voice_transcription_action SET child_id='child-junior-3' WHERE client_action_id='realtime-one'")
        c.commit()
    result = client.get(f'/v1/sessions/{sid}/voice/realtime/actions/realtime-one')
    assert result.status_code == 409, result.text
    with db.connect() as c:
        with pytest.raises(VoiceCaptureBindingError, match='OWNER_CHANGED'):
            resolve_voice_capture(c, session_id=sid, child_id=CHILD,
                                  voice_input_id=original['voice_input_id'], transcript='合成原话')


def test_realtime_onset_is_not_late_admission_time_and_is_immutable(http):
    from home_study_agent.voice_capture_binding import capture_started_at
    _client, db, sid = http
    service = VoiceCaptureBindingService(db)
    onset = datetime.now(timezone.utc) - timedelta(seconds=45)
    stamp = int(onset.timestamp() * 1000)
    first = service.begin_realtime(sid, 'delayed-onset', 'agent_command', {'child_id': CHILD},
        model_version='synthetic-asr', captured_at_ms=stamp)
    assert datetime.fromisoformat(first['speech_started_at']) < datetime.fromisoformat(first['created_at'])
    result = service.complete_realtime(sid, 'delayed-onset', '小知，原题我选B。')
    with db.connect() as c:
        assert capture_started_at(c, result['voice_input_id']) == first['speech_started_at']
    restarted = VoiceCaptureBindingService(db)
    assert restarted.begin_realtime(sid, 'delayed-onset', 'agent_command', {'child_id': CHILD},
        model_version='synthetic-asr', captured_at_ms=stamp)['speech_started_at'] == first['speech_started_at']
    with pytest.raises(VoiceCaptureBindingError, match='ACTION_CONFLICT'):
        restarted.begin_realtime(sid, 'delayed-onset', 'agent_command', {'child_id': CHILD},
            model_version='synthetic-asr', captured_at_ms=stamp + 1)


def test_realtime_unknown_onset_never_borrows_server_admission_time(http):
    from home_study_agent.voice_capture_binding import capture_started_at
    _client, db, sid = http
    service, _source = admit(db, sid)
    result = service.complete_realtime(sid, 'realtime-one', '原话')
    with db.connect() as c:
        assert capture_started_at(c, result['voice_input_id']) is None


@pytest.mark.parametrize('stamp', [True, -1, float('nan'), 10**30])
def test_realtime_onset_rejects_invalid_or_future_client_clock(http, stamp):
    _client, db, sid = http
    with pytest.raises(VoiceCaptureBindingError, match='ONSET_INVALID'):
        VoiceCaptureBindingService(db).begin_realtime(sid, 'invalid-onset', 'agent_command',
            {'child_id': CHILD}, model_version='synthetic-asr', captured_at_ms=stamp)
    with db.connect() as c:
        assert not c.execute('SELECT 1 FROM voice_capture_binding').fetchone()


def test_realtime_source_and_complete_text_survive_restart_without_raw_audio(http):
    _client, db, sid = http
    service, source = admit(db, sid)
    assert source['source_kind'] == 'realtime_transcript'
    assert source['wav_sha256'] is None
    result = service.complete_realtime(sid, 'realtime-one', '我想核对今天的物理作业。')
    restarted = VoiceCaptureBindingService(db)
    assert restarted.complete_realtime(sid, 'realtime-one', '我想核对今天的物理作业。') == result
    with db.connect() as c:
        voice = c.execute('SELECT * FROM voice_input').fetchone()
        assert voice['local_path'] is None and voice['duration_ms'] is None
        assert voice['model_version'] == 'synthetic-cloud-asr'
        assert c.execute('SELECT COUNT(*) FROM voice_input').fetchone()[0] == 1
        assert c.execute('SELECT COUNT(*) FROM voice_transcription_action').fetchone()[0] == 1
        captured = resolve_voice_capture(c, session_id=sid, child_id=CHILD,
            voice_input_id=voice['voice_input_id'], transcript=voice['transcript'])
        assert captured['raw_audio_available'] is False
        assert captured['source_kind'] == 'realtime_transcript'


def test_realtime_input_key_cannot_change_its_source_model_or_body(http):
    _client, db, sid = http
    service, _source = admit(db, sid)
    with pytest.raises(VoiceCaptureBindingError, match='ACTION_CONFLICT'):
        service.begin_realtime(sid, 'realtime-one', 'agent_command', {'child_id': CHILD}, model_version='different')
    with pytest.raises(VoiceCaptureBindingError, match='ACTION_CONFLICT'):
        service.begin(sid, 'realtime-one', b'not-the-original-audio', 'agent_command', {'child_id': CHILD})
    service.complete_realtime(sid, 'realtime-one', '第一句原话')
    with pytest.raises(VoiceCaptureBindingError, match='TRANSCRIPT_CHANGED'):
        service.complete_realtime(sid, 'realtime-one', '另一句原话')
    with db.connect() as c:
        assert c.execute('SELECT transcript FROM voice_input').fetchone()[0] == '第一句原话'


@pytest.mark.parametrize('complete_first', [False, True])
def test_cancelled_realtime_capture_cannot_recreate_a_late_body(http, complete_first):
    _client, db, sid = http
    service, _source = admit(db, sid)
    if complete_first:
        service.complete_realtime(sid, 'realtime-one', '原话')
    service.cancel(sid, 'realtime-one')
    with pytest.raises(VoiceCaptureBindingError, match='CANCELLED'):
        VoiceCaptureBindingService(db).complete_realtime(sid, 'realtime-one', '原话')
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM voice_input').fetchone()[0] == int(complete_first)


def test_concurrent_realtime_completion_is_one_canonical_voice_input(http):
    _client, db, sid = http
    service, _source = admit(db, sid)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: service.complete_realtime(sid, 'realtime-one', '相同输入的传输重试'), range(4)))
    assert len({result['voice_input_id'] for result in results}) == 1
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM voice_input').fetchone()[0] == 1
        assert c.execute('PRAGMA foreign_key_check').fetchall() == []


def test_realtime_completion_failure_rolls_back_body_action_and_binding(http, monkeypatch):
    from home_study_agent import voice_capture_binding
    _client, db, sid = http
    service, _source = admit(db, sid)
    real = voice_capture_binding.resolve_voice_capture
    def fail(*args, **kwargs):
        raise RuntimeError('synthetic interrupted admission')
    monkeypatch.setattr(voice_capture_binding, 'resolve_voice_capture', fail)
    with pytest.raises(RuntimeError, match='interrupted admission'):
        service.complete_realtime(sid, 'realtime-one', '完整原话')
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM voice_input').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM voice_transcription_action').fetchone()[0] == 0
        assert dict(c.execute('SELECT status,voice_input_id FROM voice_capture_binding').fetchone()) == {
            'status': 'prepared', 'voice_input_id': None}
    monkeypatch.setattr(voice_capture_binding, 'resolve_voice_capture', real)
    assert VoiceCaptureBindingService(db).complete_realtime(sid, 'realtime-one', '完整原话')['raw_audio_available'] is False


def test_actual_http_realtime_admission_has_no_audio_or_verified_model_claim(http):
    client, db, sid = http
    root = f'/v1/sessions/{sid}/voice/realtime'
    begin = client.post(root + '/begin', json={'client_action_id': 'http-realtime-input',
        'model_label': 'synthetic-asr', 'capture_reference': {'child_id': CHILD}, 'captured_at_ms': int(datetime.now(timezone.utc).timestamp()*1000)})
    assert begin.status_code == 200, begin.text
    assert begin.json()['model_provenance'] == 'client_reported'
    body = {'client_action_id': 'http-realtime-input', 'transcript': '小知，请核对我的作业。'}
    completed = client.post(root + '/complete', json=body)
    assert completed.status_code == 200, completed.text
    assert completed.json()['raw_audio_available'] is False
    assert client.post(root + '/complete', json=body).json() == completed.json()
    with db.connect() as c:
        voice = c.execute('SELECT * FROM voice_input').fetchone()
        assert voice['transcript'] == body['transcript']
        assert voice['local_path'] is None
        assert voice['model_version'] == 'client_reported_realtime:synthetic-asr'
    assert client.post(root + '/complete', json={**body, 'transcript': '改成另一句'}).status_code == 409
    assert client.post(root + '/begin', json={'client_action_id': 'cross-child-input',
        'model_label': 'synthetic-asr', 'capture_reference': {'child_id': 'child-junior-3'}, 'captured_at_ms': int(datetime.now(timezone.utc).timestamp()*1000)}).status_code == 409
    assert client.post(root + '/complete', json={**body, 'wav_base64': 'forbidden-fake-audio'}).status_code == 422


def test_actual_http_realtime_scope_restart_and_cancel(environment):
    settings, db = environment
    settings = replace(settings, session_token='synthetic-local-session-token')
    headers = {'X-HSC-Session': settings.session_token}
    with TestClient(create_app(settings), headers=headers) as client:
        sid = start(client)
        client.headers.pop('X-HSC-Session')
        root = f'/v1/sessions/{sid}/voice/realtime'
        source = {'client_action_id': 'http-restart-input', 'model_label': 'synthetic-asr', 'capture_reference': {'child_id': CHILD}, 'captured_at_ms': int(datetime.now(timezone.utc).timestamp()*1000)}
        body = {'client_action_id': source['client_action_id'], 'transcript': '完整原话'}
        assert client.post(root + '/begin', json=source).status_code == 401
        assert client.post(root + '/complete', json=body).status_code == 401
        assert client.post(root + '/begin', json=source, headers=headers).status_code == 200
        first = client.post(root + '/complete', json=body, headers=headers)
        assert first.status_code == 200, first.text
        original = first.json()
    with TestClient(create_app(settings)) as restarted:
        assert restarted.post(root + '/complete', json=body, headers=headers).json() == original
        cancellation = restarted.post(f'/v1/sessions/{sid}/asr/actions/{source["client_action_id"]}/cancel', headers=headers)
        assert cancellation.status_code == 200, cancellation.text
        for _ in range(3):
            assert restarted.post(f'/v1/sessions/{sid}/asr/actions/{source["client_action_id"]}/cancel', headers=headers).status_code == 200
            receipt = restarted.get(root + '/actions/' + source['client_action_id'], headers=headers)
            assert receipt.status_code == 200, receipt.text
            assert receipt.json()['status'] == 'cancelled'
            assert receipt.json()['replay_allowed'] is False
            assert receipt.json()['mastery_update_allowed'] is False
        assert restarted.post(root + '/complete', json=body, headers=headers).status_code == 409
        assert restarted.post(root + '/begin', json=source, headers=headers).status_code == 409
    with TestClient(create_app(settings), headers=headers) as recovered:
        assert recovered.get(root + '/actions/' + source['client_action_id']).json()['status'] == 'cancelled'
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM voice_input').fetchone()[0] == 1
        assert c.execute('SELECT transcript FROM voice_input').fetchone()[0] == '完整原话'
        assert c.execute('SELECT status FROM voice_input').fetchone()[0] == 'ignored'
        assert c.execute('SELECT status FROM voice_capture_binding').fetchone()[0] == 'cancelled'


@pytest.mark.parametrize('fault', [None, 'custom_index', 'after_rebuild'])
def test_v79_capture_upgrade_preserves_original_and_restores_any_failed_rebuild(tmp_path, monkeypatch, fault):
    from home_study_agent import voice_capture_schema
    from historical_component_fixture import family
    db, _repo, sid = family(tmp_path / 'legacy-capture.db', CHILD, 'legacy-capture-session')
    original = VoiceCaptureBindingService(db).begin(sid, 'original-wav', b'synthetic-legacy-bytes', 'agent_command', {'child_id': CHILD})
    # Materialize the historical NOT NULL layout, not a v80 table mislabeled v79.
    historical = voice_capture_schema.TABLE_SQL.split('    source_kind ')[0].rstrip().rstrip(',') + ')'
    historical = historical.replace('wav_sha256 TEXT,', 'wav_sha256 TEXT NOT NULL,')
    with db.connect() as c:
        c.execute(historical.format(table='historical_capture'))
        names = [row[1] for row in c.execute('PRAGMA table_info(historical_capture)')]
        columns = ','.join(names)
        c.execute('INSERT INTO historical_capture (' + columns + ') SELECT ' + columns + ' FROM voice_capture_binding')
        c.execute('DROP TABLE voice_capture_binding')
        c.execute('ALTER TABLE historical_capture RENAME TO voice_capture_binding')
        c.execute('DELETE FROM schema_migration WHERE version>=80')
        if fault == 'custom_index':
            c.execute('CREATE INDEX operator_capture_index ON voice_capture_binding(purpose)')
        c.commit()
    if fault == 'after_rebuild':
        real = voice_capture_schema.ensure_schema
        def failed_upgrade(c):
            real(c)
            c.execute("DELETE FROM voice_capture_binding")
            c.commit()
            raise RuntimeError('synthetic post-rebuild failure')
        monkeypatch.setattr(voice_capture_schema, 'ensure_schema', failed_upgrade)
    if fault:
        with pytest.raises((ValueError, RuntimeError)):
            db.initialize()
    else:
        db.initialize()
    with db.connect() as c:
        preserved = dict(c.execute('SELECT * FROM voice_capture_binding').fetchone())
        assert all(preserved[key] == original[key] for key in names)
        info = {row[1]: row for row in c.execute('PRAGMA table_info(voice_capture_binding)')}
        assert bool(info['wav_sha256'][3]) == bool(fault)
        assert c.execute('SELECT MAX(version) FROM schema_migration').fetchone()[0] == (79 if fault else LATEST_SCHEMA_VERSION)
        assert c.execute('PRAGMA foreign_key_check').fetchall() == []
        assert c.execute("SELECT 1 FROM sqlite_master WHERE name='voice_capture_binding_v80'").fetchone() is None
        if not fault:
            assert preserved['source_kind'] == 'local_wav' and preserved['source_model'] is None
        if fault == 'custom_index':
            assert c.execute("SELECT 1 FROM sqlite_master WHERE name='operator_capture_index'").fetchone()
    status = db.latest_upgrade_status()
    assert status['source_schema_version'] == 79 and status['target_schema_version'] == LATEST_SCHEMA_VERSION
    assert status['status'] == ('restored_after_failure' if fault else 'completed')
