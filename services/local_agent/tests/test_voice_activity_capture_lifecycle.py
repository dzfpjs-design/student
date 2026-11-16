"""Synthetic PCM, real ASR/parent HTTP and managed backup lifecycles.

Legacy fixtures only remove newly introduced columns. They never attach an
activity to an old recording or manufacture a voice/source receipt.
"""
from synthetic_native_restore_fixture import KEY, signed_local_restore_post, observe_original_publication
from dataclasses import replace
import json
from pathlib import Path

import pytest
from privacy_restore_operation import restore_operation
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.local_media_ai import LocalMediaAI
from home_study_agent.voice_capture_binding import resolve_voice_capture, VoiceCaptureBindingError
from test_diagnostic_prompt_http import environment, start, CHILD, OTHER
from test_companion_request_provenance_http import planner
from test_voice_activity_capture import started, payload


WORDS = '我已经准备好了，想把桌面这一块整理一下。'


def parent(client):
    response = client.post('/v1/parent/setup', json={'pin': '5837'})
    assert response.status_code == 200, response.text
    client.headers['X-HSC-Parent'] = response.json()['parent_token']


def recognize(monkeypatch, text=WORDS):
    calls = []
    def recognized(*args, **kwargs):
        calls.append(kwargs['purpose'])
        return text, 1000, 'synthetic-recognition', False
    monkeypatch.setattr(LocalMediaAI, '_recognize_wav', recognized)
    return calls


def captured(client, settings, db, monkeypatch):
    sid, focus = started(client, settings, db, monkeypatch)
    calls = recognize(monkeypatch)
    body = payload(settings, focus)
    response = client.post(f'/v1/sessions/{sid}/asr', json=body)
    assert response.status_code == 200, response.text
    assert calls == ['agent_command']
    return sid, focus, body, response.json()['voice_input_id']


@pytest.mark.parametrize('scope', ['learning', 'media', 'all_history'])
def test_activity_capture_export_delete_and_restart_preserve_other_child(environment, monkeypatch, scope):
    settings, db = environment
    with TestClient(create_app(settings, model_gateway=planner())) as client:
        parent(client)
        sid, focus, body, vid = captured(client, settings, db, monkeypatch)
        response = client.post(f'/v1/sessions/{sid}/switch-child', json={
            'target_child_id': OTHER, 'client_action_id': 'switch-for-isolation'})
        assert response.status_code == 200, response.text
        other_sid = response.json()['session_id']
        other_body = payload(settings, None, 'other-profile-voice')
        other_body['capture_reference']['child_id'] = OTHER
        recognize(monkeypatch, '另一位孩子的合成话语。')
        other = client.post(f'/v1/sessions/{other_sid}/asr', json=other_body)
        assert other.status_code == 200, other.text
        other_vid = other.json()['voice_input_id']
        with db.connect() as c:
            other_before = tuple(c.execute('SELECT * FROM voice_capture_binding WHERE voice_input_id=?', (other_vid,)).fetchone())
        exported = client.post(f'/v1/parent/children/{CHILD}/export')
        assert exported.status_code == 200, exported.text
        data = json.loads(Path(exported.json()['path']).read_text())
        rows = data['voice_capture_bindings']
        original = next(row for row in rows if row['voice_input_id'] == vid)
        assert json.loads(original['activity_reference_json']) == focus
        assert original['activity_validation'] == 'valid'
        assert all(row['child_id'] == CHILD for row in rows)
        assert all(row['voice_input_id'] != other_vid for row in rows)
        deleted = client.post(f'/v1/parent/children/{CHILD}/delete', json={
            'scope': scope, 'confirmation': '确认删除', 'client_action_id': 'erase-activity-capture'})
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()['status'] == 'completed'
        with db.connect() as c:
            assert not c.execute("SELECT 1 FROM voice_capture_binding WHERE child_id=? AND (status!='cancelled' OR activity_reference_json IS NOT NULL OR question_reference_json IS NOT NULL)", (CHILD,)).fetchone()
            assert tuple(c.execute('SELECT * FROM voice_capture_binding WHERE voice_input_id=?', (other_vid,)).fetchone()) == other_before
            with pytest.raises(VoiceCaptureBindingError):
                resolve_voice_capture(c, session_id=sid, child_id=CHILD, voice_input_id=vid, transcript=WORDS)
            assert resolve_voice_capture(c, session_id=other_sid, child_id=OTHER,
                voice_input_id=other_vid, transcript='另一位孩子的合成话语。')['status'] == 'completed'
            assert not c.execute('PRAGMA foreign_key_check').fetchall()
    with TestClient(create_app(settings, model_gateway=planner())) as client:
        calls = recognize(monkeypatch)
        replay = client.post(f'/v1/sessions/{sid}/asr', json=body)
        assert replay.status_code == 409, replay.text
        assert not calls
        with db.connect() as c:
            assert tuple(c.execute('SELECT * FROM voice_capture_binding WHERE voice_input_id=?', (other_vid,)).fetchone()) == other_before


@pytest.mark.parametrize('portable', [False, True])
def test_restored_activity_recording_stays_cancelled_and_never_restores_focus(environment, monkeypatch, portable):
    settings, db = environment
    settings = replace(settings, data_encryption_key=b'L' * 32, native_launch_attestation_key=KEY if not portable else None)
    password = 'synthetic-8eca26e7cf9d3d18ba5940d3'
    with TestClient(create_app(settings, model_gateway=planner())) as client:
        parent(client)
        sid, focus, body, vid = captured(client, settings, db, monkeypatch)
        saved = (client.post('/v1/parent/backups/portable', json={'passphrase': password}) if portable
                 else client.post('/v1/parent/backups'))
        assert saved.status_code == 200, saved.text
        cancelled = client.post(f'/v1/sessions/{sid}/asr/actions/{body["client_action_id"]}/cancel')
        assert cancelled.status_code == 200, cancelled.text
        # Only the restore precondition is staged; no learning/session completion
        # is claimed. The backup still contains the formerly active session.
        with db.connect() as c:
            c.execute("UPDATE daily_session SET status='completed' WHERE session_id=?", (sid,))
            c.commit()
        result = (client.post('/v1/parent/backups/portable/restore', json={
            'path': saved.json()['path'], 'passphrase': password, 'confirmation': '确认迁移恢复',
            'operation': restore_operation(client)}) if portable
            else signed_local_restore_post(client, json={
                'backup_id': saved.json()['backup_id'], 'confirmation': '确认恢复',
                'operation': restore_operation(client)}))
        assert result.status_code == 200, result.text
        assert result.json()['status'] == 'restored'
        with db.connect() as c:
            row = c.execute('SELECT * FROM voice_capture_binding WHERE voice_input_id=?', (vid,)).fetchone()
            assert row['status'] == 'cancelled'
            assert json.loads(row['activity_reference_json']) == focus
            with pytest.raises(VoiceCaptureBindingError, match='VOICE_CAPTURE_CANCELLED'):
                resolve_voice_capture(c, session_id=sid, child_id=CHILD, voice_input_id=vid, transcript=WORDS)
        assert client.get(f'/v1/sessions/{sid}/voice/activity-focus').json()['activity_focus'] is None
        calls = recognize(monkeypatch)
        assert client.post(f'/v1/sessions/{sid}/asr', json=body).status_code == 409
        assert not calls
    with TestClient(create_app(settings, model_gateway=planner())) as client:
        assert client.get(f'/v1/sessions/{sid}/voice/activity-focus').json()['activity_focus'] is None
        assert client.post(f'/v1/sessions/{sid}/asr', json=body).status_code == 409


def remove_activity_columns(db):
    with db.connect() as c:
        assert not c.execute('SELECT 1 FROM voice_capture_binding WHERE activity_reference_json IS NOT NULL').fetchone()
        c.execute('ALTER TABLE voice_capture_binding DROP COLUMN activity_reference_json')
        c.execute('ALTER TABLE voice_capture_binding DROP COLUMN activity_validation')
        c.commit()


def test_old_schema_restart_does_not_backfill_activity_on_an_existing_recording(environment, monkeypatch):
    settings, db = environment
    with TestClient(create_app(settings, model_gateway=planner())) as client:
        sid, focus = started(client, settings, db, monkeypatch)
        calls = recognize(monkeypatch)
        body = payload(settings, None, 'legacy-unfocused-voice')
        old = client.post(f'/v1/sessions/{sid}/asr', json=body)
        assert old.status_code == 200, old.text
        vid = old.json()['voice_input_id']
    remove_activity_columns(db)
    with TestClient(create_app(settings, model_gateway=planner())) as client:
        calls = recognize(monkeypatch)
        with db.connect() as c:
            row = c.execute('SELECT * FROM voice_capture_binding WHERE voice_input_id=?', (vid,)).fetchone()
            assert row['activity_reference_json'] is None
            assert 'activity_reference' not in resolve_voice_capture(c,
                session_id=sid, child_id=CHILD, voice_input_id=vid, transcript=WORDS)
        replay = client.post(f'/v1/sessions/{sid}/asr', json=body)
        assert replay.status_code == 200, replay.text
        assert not calls
        changed = payload(settings, focus, 'legacy-unfocused-voice')
        assert client.post(f'/v1/sessions/{sid}/asr', json=changed).status_code == 409
        with db.connect() as c:
            assert c.execute('SELECT activity_reference_json FROM voice_capture_binding WHERE voice_input_id=?', (vid,)).fetchone()[0] is None


def test_online_restore_of_old_capture_schema_allows_fresh_asr_without_restart(environment, monkeypatch):
    settings, db = environment
    settings = replace(settings,native_launch_attestation_key=KEY)
    published = []
    with TestClient(create_app(settings, model_gateway=planner()), raise_server_exceptions=False) as client:
        parent(client)
        sid = start(client)
        recognize(monkeypatch)
        body = payload(settings, None, 'pre-upgrade-voice')
        old = client.post(f'/v1/sessions/{sid}/asr', json=body)
        assert old.status_code == 200, old.text
        remove_activity_columns(db)
        # Produce a real managed backup in the immediately preceding schema.
        saved = client.post('/v1/parent/backups')
        assert saved.status_code == 200, saved.text
        from home_study_agent.voice_capture_binding import VoiceCaptureBindingService
        VoiceCaptureBindingService(db)
        with db.connect() as c:
            c.execute("UPDATE daily_session SET status='completed' WHERE session_id=?", (sid,))
            c.commit()
        from home_study_agent.database import Database
        def read_snapshot(c):
            columns={r[1]for r in c.execute('PRAGMA table_info(voice_capture_binding)')}
            status=c.execute('SELECT status FROM voice_capture_binding WHERE voice_input_id=?',(old.json()['voice_input_id'],)).fetchone()[0]
            return ({'activity_reference_json','activity_validation'}<=columns,status)
        with observe_original_publication(monkeypatch,db,read_snapshot,published):
            restored = signed_local_restore_post(client, json={
                'backup_id': saved.json()['backup_id'], 'confirmation': '确认恢复',
                'operation': restore_operation(client)})
            assert restored.status_code == 200, restored.text
        calls = recognize(monkeypatch)
        assert client.post(f'/v1/sessions/{sid}/asr', json=body).status_code == 409
        assert not calls
        fresh_body = payload(settings, None, 'fresh-post-restore-voice')
        fresh = client.post(f'/v1/sessions/{sid}/asr', json=fresh_body)
        assert fresh.status_code == 200, fresh.text
        assert calls == ['agent_command']
        assert published and all(observation == (True, 'cancelled') for observation in published)
        vid = fresh.json()['voice_input_id']
        with db.connect() as c:
            columns = {row[1] for row in c.execute('PRAGMA table_info(voice_capture_binding)')}
            assert {'activity_reference_json', 'activity_validation'} <= columns
            original = c.execute('SELECT * FROM voice_capture_binding WHERE voice_input_id=?', (old.json()['voice_input_id'],)).fetchone()
            assert original['status'] == 'cancelled' and original['activity_reference_json'] is None
            assert resolve_voice_capture(c, session_id=sid, child_id=CHILD,
                voice_input_id=vid, transcript=WORDS)['status'] == 'completed'
    with TestClient(create_app(settings, model_gateway=planner())) as client:
        calls = recognize(monkeypatch)
        replay = client.post(f'/v1/sessions/{sid}/asr', json=fresh_body)
        assert replay.status_code == 200, replay.text
        assert replay.json()['voice_input_id'] == vid
        assert not calls
        with db.connect() as c:
            original = c.execute('SELECT * FROM voice_capture_binding WHERE voice_input_id=?', (old.json()['voice_input_id'],)).fetchone()
            assert original['status'] == 'cancelled' and original['activity_reference_json'] is None
            assert c.execute("SELECT COUNT(*) FROM voice_input WHERE client_action_id='fresh-post-restore-voice'").fetchone()[0] == 1
