"""Synthetic PCM/recognizer, real ASR HTTP, source binding and privacy writes."""
from synthetic_native_restore_fixture import KEY, signed_local_restore_post, observe_original_publication
import base64
from copy import deepcopy
from contextlib import contextmanager
from dataclasses import replace
import json
from pathlib import Path

import pytest
from privacy_restore_operation import restore_operation

from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.local_media_ai import LocalMediaAI
from test_diagnostic_prompt_http import environment, start, CHILD
from test_learning_runtime_http import NoCloud
from test_voice_runtime_binding import wav_file
from test_voice_runtime_binding import capture, interpret, submit
from test_agent_continuation import Gateway
from test_pet_media_evidence import local_review
from home_study_agent.voice_capture_binding import resolve_voice_capture
from home_study_agent.voice_capture_binding import VoiceCaptureBindingError, VoiceCaptureBindingService
from home_study_agent.config import Settings
from test_learning_outcome_evaluator import evaluation_fixture
from test_voice_activity_capture_lifecycle import parent
from test_diagnostic_prompt_http import OTHER


WORDS = '先暂停一下，我想看看刚才的评价。'


def recording(settings, prompt, reference, key='pending-control-audio', child=CHILD):
    path = settings.data_dir / 'synthetic-pending-control.wav'
    wav_file(path)
    binding = {'child_id': child}
    if prompt:
        binding['question_reference'] = {'kind': 'diagnostic', 'binding': {
            k: prompt[k] for k in ('prompt_id', 'item_version')}}
    if reference is not None:
        binding['pending_evaluation_turn_id'] = reference
    return {'wav_base64': base64.b64encode(path.read_bytes()).decode(), 'purpose': 'agent_command',
        'client_action_id': key, 'capture_reference': binding}


def recognizer(monkeypatch):
    calls = []
    def recognize(*args, **kwargs):
        calls.append(kwargs['purpose'])
        return WORDS, 1000, 'synthetic-recognizer', False
    monkeypatch.setattr(LocalMediaAI, '_recognize_wav', recognize)
    return calls


def speaking_receipt(client, settings, db, monkeypatch):
    """Real ASR/answer; completed local receipt awaiting UI, not running cloud work."""
    sid = start(client)
    prepared = client.post(f'/v1/sessions/{sid}/diagnostic-prompt/prepare', json={
        'track': 'pet', 'preferred_item_id': 'diag-pet-speaking-2', 'client_action_id': 'speaking-original'})
    assert prepared.status_code == 200, prepared.text
    prompt = prepared.json()['prompt']
    answer = 'I like reading in the library because it is quiet. I read story books there after school with my friends.'
    vid = capture(client, settings, monkeypatch, sid, answer, prompt, 'actual-original-audio')
    turn = interpret(client, sid, answer, 'original-answer-turn', vid)
    if turn['intent'] == 'pending_answer_clarification':
        result = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
        assert result.status_code == 200, result.text
        confirmed = client.post(f'/v1/sessions/{sid}/agent-turn', json={
            'transcript': '刚才那句就是我的答案', 'client_action_id': 'confirm-original-answer'})
        assert confirmed.status_code == 200, confirmed.text
        turn = confirmed.json()
    assert turn['action']['source_voice_input_id'] == vid
    result = submit(client, sid, prompt, turn, answer, 'real-speaking-answer')
    assert result.status_code == 200, result.text
    assert result.json()['status'] == 'answered'
    # Existing evaluator fixture constructs only a local evaluation request; it
    # consumes this actual answer and does not approve its rubric or audio skill.
    local_review((db, None, sid, None, None, None), prompt, answer,
                 approve=False, scored=result.json())
    with db.connect() as c:
        rows = c.execute('SELECT p.agent_turn_id,e.record_json FROM learning_evaluation_execution e JOIN agent_step s USING(step_id) JOIN agent_plan p USING(plan_id)').fetchall()
        exact = [row for row in rows if json.loads(row['record_json']).get('source_evidence_id') ==
                 result.json()['attempt']['evidence_id']]
        assert len(exact) == 1
        tid = exact[0]['agent_turn_id']
    return sid, prompt, tid, answer


def immutable_learning(db):
    with db.connect() as c:
        return {table: [tuple(r) for r in c.execute('SELECT * FROM ' + table)] for table in
                ('diagnostic_attempt', 'pet_media_binding', 'learning_evidence', 'rubric_assessment')}


@contextmanager
def inline_host(tmp_path):
    db, sid, evaluator, gateway, _ = evaluation_fixture(tmp_path)
    settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False)
    with TestClient(create_app(settings, model_gateway=NoCloud()), raise_server_exceptions=False) as client:
        evaluator._claim(evaluator._load('eval'))
        assert not gateway.requests
        yield settings, db, sid, client


def test_inline_running_original_survives_control_capture_without_question_inference(tmp_path, monkeypatch):
    with inline_host(tmp_path) as (settings, db, sid, client):
        calls = recognizer(monkeypatch)
        before = immutable_learning(db)
        with db.connect() as c:
            original = tuple(c.execute('SELECT * FROM learning_evaluation_execution').fetchone())
        body = recording(settings, None, 'eval', child='child-runtime')
        captured = client.post(f'/v1/sessions/{sid}/asr', json=body)
        assert captured.status_code == 200, captured.text
        assert captured.json()['capture_binding']['pending_evaluation_turn_id'] == 'eval'
        assert calls == ['agent_command']
        with db.connect() as c:
            resolved = resolve_voice_capture(c, session_id=sid, child_id='child-runtime',
                voice_input_id=captured.json()['voice_input_id'], transcript=WORDS)
            assert not resolved.get('question_reference') and not resolved.get('prompt_id')
            assert tuple(c.execute('SELECT * FROM learning_evaluation_execution').fetchone()) == original
        assert immutable_learning(db) == before


def test_actual_asr_to_restricted_agent_http_and_dispatch_keeps_original_inline_response(tmp_path, monkeypatch):
    from test_pending_evaluation_control import host, facts
    # Real ModelGateway protocol with MockTransport, not a paid model or ASR
    # quality claim. Host asserts only this utterance and state booleans leave.
    with host(tmp_path) as (db, sid, client, requests):
        settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False)
        calls = recognizer(monkeypatch)
        before = facts(db)
        captured = client.post(f'/v1/sessions/{sid}/asr', json=recording(
            settings, None, 'eval', child='child-runtime'))
        assert captured.status_code == 200, captured.text
        body = {'transcript': WORDS, 'client_action_id': 'captured-status-query',
                'voice_input_id': captured.json()['voice_input_id'], 'pending_evaluation_turn_id': 'eval'}
        with db.connect() as c:
            voice_before = tuple(c.execute('SELECT * FROM voice_capture_binding WHERE voice_input_id=?',
                (captured.json()['voice_input_id'],)).fetchone())
            turns_before = c.execute('SELECT count(*) FROM agent_turn').fetchone()[0]
        for index, replacement in enumerate((None, 'another-original')):
            changed = dict(body, client_action_id=f'fresh-wrong-mode-{index}')
            if replacement is None:
                changed.pop('pending_evaluation_turn_id')
            else:
                changed['pending_evaluation_turn_id'] = replacement
            rejected = client.post(f'/v1/sessions/{sid}/agent-turn', json=changed)
            assert rejected.status_code == 409, rejected.text
            assert requests == [] and facts(db) == before
            with db.connect() as c:
                assert c.execute('SELECT count(*) FROM agent_turn').fetchone()[0] == turns_before
                assert tuple(c.execute('SELECT * FROM voice_capture_binding WHERE voice_input_id=?',
                    (captured.json()['voice_input_id'],)).fetchone()) == voice_before
        heard = client.post(f'/v1/sessions/{sid}/agent-turn', json=body)
        assert heard.status_code == 200, heard.text
        turn = heard.json()
        assert turn['action']['pending_evaluation_control']['evaluation_turn_id'] == 'eval'
        assert turn['after_execution'] == 'stop'
        assert turn['runtime_capability'] == 'conversation.respond'
        executed = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
        assert executed.status_code == 200, executed.text
        assert client.post(f'/v1/sessions/{sid}/agent-turn', json=body).json()['agent_turn_id'] == turn['agent_turn_id']
        for replacement in (None, 'another-original'):
            changed = dict(body)
            if replacement is None:
                changed.pop('pending_evaluation_turn_id')
            else:
                changed['pending_evaluation_turn_id'] = replacement
            rejected = client.post(f'/v1/sessions/{sid}/agent-turn', json=changed)
            assert rejected.status_code == 409, rejected.text
        assert len(requests) == 1 and requests[0]['transcript'] == WORDS
        assert calls == ['agent_command'] and facts(db) == before
        with db.connect() as c:
            assert tuple(c.execute('SELECT * FROM voice_capture_binding WHERE voice_input_id=?',
                (captured.json()['voice_input_id'],)).fetchone()) == voice_before


def test_missing_reference_is_ordinary_capture_but_old_key_cannot_gain_pending_authority(tmp_path, monkeypatch):
    with inline_host(tmp_path) as (settings, db, sid, client):
        calls = recognizer(monkeypatch)
        body = recording(settings, None, None, child='child-runtime')
        original = client.post(f'/v1/sessions/{sid}/asr', json=body)
        assert original.status_code == 200, original.text
        body['capture_reference']['pending_evaluation_turn_id'] = 'eval'
        assert client.post(f'/v1/sessions/{sid}/asr', json=body).status_code == 409
        with db.connect() as c:
            assert c.execute('SELECT pending_evaluation_turn_id FROM voice_capture_binding WHERE voice_input_id=?',
                (original.json()['voice_input_id'],)).fetchone()[0] is None
        assert calls == ['agent_command']


@pytest.mark.parametrize('fault', ['other_child', 'cancelled', 'source_deleted', 'changed_answer'])
def test_pending_original_owner_or_source_change_rejects_before_asr(tmp_path, monkeypatch, fault):
    with inline_host(tmp_path) as (settings, db, sid, client):
        child = 'child-runtime'
        if fault == 'other_child':
            switched = client.post(f'/v1/sessions/{sid}/switch-child', json={
                'target_child_id': OTHER, 'client_action_id': 'other-child-control'})
            assert switched.status_code == 200, switched.text
            sid, child = switched.json()['session_id'], OTHER
        else:
            # Explicit source-revocation/corruption fault, never a fabricated
            # positive authority or replacement latest question.
            with db.connect() as c:
                if fault == 'cancelled':
                    c.execute("UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id='eval'")
                elif fault == 'source_deleted':
                    c.execute("UPDATE agent_turn SET tool_error_code='SOURCE_DELETED' WHERE agent_turn_id='eval'")
                else:
                    c.execute("UPDATE agent_step SET arguments_json=json_set(arguments_json,'$.response','replacement') WHERE plan_id=(SELECT plan_id FROM agent_plan WHERE agent_turn_id='eval')")
                c.commit()
        calls = recognizer(monkeypatch)
        before = immutable_learning(db)
        response = client.post(f'/v1/sessions/{sid}/asr', json=recording(settings, None, 'eval', child=child))
        assert response.status_code == 409, response.text
        assert not calls and immutable_learning(db) == before
        with db.connect() as c:
            assert not c.execute('SELECT 1 FROM voice_input').fetchone()


@pytest.mark.parametrize('scope', ['learning', 'media', 'all_history'])
def test_pending_capture_export_delete_restart_does_not_erase_other_child(environment, monkeypatch, scope):
    settings, db = environment
    with TestClient(create_app(settings, model_gateway=Gateway({})), raise_server_exceptions=False) as client:
        parent(client)
        sid, prompt, tid, _ = speaking_receipt(client, settings, db, monkeypatch)
        recognizer(monkeypatch)
        body = recording(settings, prompt, tid)
        first = client.post(f'/v1/sessions/{sid}/asr', json=body)
        assert first.status_code == 200, first.text
        vid = first.json()['voice_input_id']
        switched = client.post(f'/v1/sessions/{sid}/switch-child', json={
            'target_child_id': OTHER, 'client_action_id': 'other-child-for-lifecycle'})
        assert switched.status_code == 200, switched.text
        other_sid = switched.json()['session_id']
        other = client.post(f'/v1/sessions/{other_sid}/asr', json=recording(
            settings, None, None, key='other-child-capture', child=OTHER))
        assert other.status_code == 200, other.text
        other_vid = other.json()['voice_input_id']
        with db.connect() as c:
            other_before = tuple(c.execute('SELECT * FROM voice_capture_binding WHERE voice_input_id=?', (other_vid,)).fetchone())
        exported = client.post(f'/v1/parent/children/{CHILD}/export')
        assert exported.status_code == 200, exported.text
        data = json.loads(Path(exported.json()['path']).read_text())
        rows = data['voice_capture_bindings']
        exact = next(r for r in rows if r['voice_input_id'] == vid)
        assert exact['pending_evaluation_turn_id'] == tid
        assert all(r['child_id'] == CHILD for r in rows)
        assert all(r['voice_input_id'] != other_vid for r in rows)
        deleted = client.post(f'/v1/parent/children/{CHILD}/delete', json={
            'scope': scope, 'confirmation': '确认删除', 'client_action_id': 'erase-pending-capture'})
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()['status'] == 'completed'
        with db.connect() as c:
            assert not c.execute("SELECT 1 FROM voice_capture_binding WHERE child_id=? AND (status!='cancelled' OR pending_evaluation_turn_id IS NOT NULL OR question_reference_json IS NOT NULL)", (CHILD,)).fetchone()
            assert tuple(c.execute('SELECT * FROM voice_capture_binding WHERE voice_input_id=?', (other_vid,)).fetchone()) == other_before
            with pytest.raises(VoiceCaptureBindingError):
                resolve_voice_capture(c, session_id=sid, child_id=CHILD, voice_input_id=vid, transcript=WORDS)
            assert resolve_voice_capture(c, session_id=other_sid, child_id=OTHER,
                voice_input_id=other_vid, transcript=WORDS)['status'] == 'completed'
            assert not c.execute('PRAGMA foreign_key_check').fetchall()
    with TestClient(create_app(settings, model_gateway=Gateway({})), raise_server_exceptions=False) as client:
        calls = recognizer(monkeypatch)
        assert client.post(f'/v1/sessions/{sid}/asr', json=body).status_code == 409
        assert not calls
        with db.connect() as c:
            assert tuple(c.execute('SELECT * FROM voice_capture_binding WHERE voice_input_id=?', (other_vid,)).fetchone()) == other_before


def test_old_schema_online_restore_cancels_capture_without_backfilling_pending_reference(environment, monkeypatch):
    settings, db = environment
    settings = replace(settings, native_launch_attestation_key=KEY)
    published = []
    with TestClient(create_app(settings, model_gateway=Gateway({})), raise_server_exceptions=False) as client:
        parent(client)
        sid, prompt, tid, _ = speaking_receipt(client, settings, db, monkeypatch)
        # Genuine pre-feature recording has no evaluation link, even though a
        # completed evaluation exists. The old backup must not infer one.
        recognizer(monkeypatch)
        body = recording(settings, None, None, 'old-schema-recording')
        old = client.post(f'/v1/sessions/{sid}/asr', json=body)
        assert old.status_code == 200, old.text
        vid = old.json()['voice_input_id']
        with db.connect() as c:
            assert not c.execute('SELECT 1 FROM voice_capture_binding WHERE pending_evaluation_turn_id IS NOT NULL').fetchone()
            c.execute('ALTER TABLE voice_capture_binding DROP COLUMN pending_evaluation_turn_id')
            c.commit()
        saved = client.post('/v1/parent/backups')
        assert saved.status_code == 200, saved.text
        VoiceCaptureBindingService(db)
        # Restore requires an inactive live session; this stages that precondition
        # only, and is not evidence that the child completed any learning goal.
        with db.connect() as c:
            c.execute("UPDATE daily_session SET status='completed' WHERE session_id=?", (sid,))
            c.commit()
        from home_study_agent.database import Database
        def read_snapshot(c):
            columns={r[1]for r in c.execute('PRAGMA table_info(voice_capture_binding)')}
            row=c.execute('SELECT * FROM voice_capture_binding WHERE voice_input_id=?',(vid,)).fetchone()
            return ('pending_evaluation_turn_id'in columns,row['status'],row['pending_evaluation_turn_id']if'pending_evaluation_turn_id'in columns else'missing')
        with observe_original_publication(monkeypatch,db,read_snapshot,published):
            restored = signed_local_restore_post(client, json={
                'backup_id': saved.json()['backup_id'], 'confirmation': '确认恢复',
                'operation': restore_operation(client)})
            assert restored.status_code == 200, restored.text
        assert published and all(row == (True, 'cancelled', None) for row in published)
        calls = recognizer(monkeypatch)
        assert client.post(f'/v1/sessions/{sid}/asr', json=body).status_code == 409
        changed = deepcopy(body)
        changed['capture_reference']['pending_evaluation_turn_id'] = tid
        assert client.post(f'/v1/sessions/{sid}/asr', json=changed).status_code == 409
        assert not calls
        with db.connect() as c:
            row = c.execute('SELECT * FROM voice_capture_binding WHERE voice_input_id=?', (vid,)).fetchone()
            assert row['pending_evaluation_turn_id'] is None and row['status'] == 'cancelled'
        fresh_body = recording(settings, None, None, 'fresh-online-after-restore')
        fresh = client.post(f'/v1/sessions/{sid}/asr', json=fresh_body)
        assert fresh.status_code == 200, fresh.text
        assert calls == ['agent_command']
    with TestClient(create_app(settings, model_gateway=Gateway({})), raise_server_exceptions=False) as client:
        calls = recognizer(monkeypatch)
        replay = client.post(f'/v1/sessions/{sid}/asr', json=fresh_body)
        assert replay.status_code == 200, replay.text
        assert replay.json()['voice_input_id'] == fresh.json()['voice_input_id']
        assert not calls
        assert client.post(f'/v1/sessions/{sid}/asr', json=body).status_code == 409


@pytest.mark.parametrize('retain', [False, True])
def test_speaking_original_pending_control_asr_cannot_become_pet_media_or_another_answer(environment, monkeypatch, retain):
    settings, db = environment
    settings = replace(settings, data_encryption_key=b'P' * 32)
    with TestClient(create_app(settings, model_gateway=Gateway({})), raise_server_exceptions=False) as client:
        parent(client)
        privacy = client.put('/v1/parent/privacy', json={'raw_media_storage_allowed': retain})
        assert privacy.status_code == 200, privacy.text
        sid, prompt, tid, answer = speaking_receipt(client, settings, db, monkeypatch)
        before = immutable_learning(db)
        assert len(before['pet_media_binding']) == int(retain)
        assert len(before['diagnostic_attempt']) == 1
        calls = recognizer(monkeypatch)
        body = recording(settings, prompt, tid)
        response = client.post(f'/v1/sessions/{sid}/asr', json=body)
        assert response.status_code == 200, response.text
        value = response.json()
        assert calls == ['agent_command']
        assert value['capture_binding']['pending_evaluation_turn_id'] == tid
        assert value['capture_binding']['raw_audio_available'] is retain
        assert 'speaking_media_status' not in value['capture_binding']
        assert immutable_learning(db) == before
        with db.connect() as c:
            resolved = resolve_voice_capture(c, session_id=sid, child_id=CHILD,
                voice_input_id=value['voice_input_id'], transcript=WORDS)
            assert resolved['pending_evaluation_turn_id'] == tid
            from home_study_agent.agent_runtime import AgentRuntimeStore
            assert not AgentRuntimeStore.capture_matches_answer(resolved, 'submit_diagnostic_answer',
                {'prompt_id': prompt['prompt_id'], 'item_version': prompt['item_version']})
            record = json.loads(c.execute('SELECT e.record_json FROM learning_evaluation_execution e JOIN agent_step s USING(step_id) JOIN agent_plan p USING(plan_id) WHERE p.agent_turn_id=?', (tid,)).fetchone()[0])
            assert record['child_response'] == answer and record['child_response'] != WORDS
        for replacement in (None, 'another-evaluation'):
            changed = deepcopy(body)
            if replacement is None:
                changed['capture_reference'].pop('pending_evaluation_turn_id')
            else:
                changed['capture_reference']['pending_evaluation_turn_id'] = replacement
            assert client.post(f'/v1/sessions/{sid}/asr', json=changed).status_code == 409
        assert calls == ['agent_command']
    with TestClient(create_app(settings, model_gateway=Gateway({})), raise_server_exceptions=False) as client:
        calls = recognizer(monkeypatch)
        replay = client.post(f'/v1/sessions/{sid}/asr', json=body)
        assert replay.status_code == 200, replay.text
        assert replay.json()['voice_input_id'] == value['voice_input_id']
        assert replay.json()['capture_binding']['pending_evaluation_turn_id'] == tid
        assert not calls
        assert immutable_learning(db) == before


def test_unknown_pending_evaluation_is_rejected_before_recognition_or_pet_media(environment, monkeypatch):
    settings, db = environment
    with TestClient(create_app(settings, model_gateway=NoCloud()), raise_server_exceptions=False) as client:
        sid = start(client)
        prepared = client.post(f'/v1/sessions/{sid}/diagnostic-prompt/prepare', json={
            'track': 'pet', 'preferred_item_id': 'diag-pet-speaking-2', 'client_action_id': 'actual-speaking-original'})
        assert prepared.status_code == 200, prepared.text
        prompt = prepared.json()['prompt']
        calls = recognizer(monkeypatch)
        result = client.post(f'/v1/sessions/{sid}/asr', json=recording(settings, prompt, 'missing-evaluation'))
        assert result.status_code == 409, result.text
        assert calls == []
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM voice_input').fetchone()[0] == 0
            assert c.execute('SELECT COUNT(*) FROM pet_media_binding').fetchone()[0] == 0
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
