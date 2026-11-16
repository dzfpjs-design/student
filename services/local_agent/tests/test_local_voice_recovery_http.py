"""Isolated SQLite/HTTP and synthetic PCM; no real household or paid ASR."""
import base64
from dataclasses import replace
import json

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.local_media_ai import LocalMediaAI
from home_study_agent.voice_capture_binding import VoiceCaptureBindingService, VoiceCaptureBindingError
from test_diagnostic_prompt_http import environment, start, CHILD, OTHER
from test_learning_runtime_http import NoCloud
from test_voice_runtime_binding import wav_file
from test_voice_activity_capture_lifecycle import parent
from home_study_agent.agent_runtime import AgentRuntimeStore
from home_study_agent.cloud_asr import CloudAsrGateway


WORDS = '我现在想伤害自己，请帮我找大人。'


def audio(settings):
    path = settings.data_dir / 'synthetic-local-rescue.wav'
    wav_file(path)
    return path.read_bytes()


def payload(settings, key='recovery-capture', **reference):
    return {'wav_base64': base64.b64encode(audio(settings)).decode(), 'purpose': 'agent_command',
            'client_action_id': key, 'capture_reference': {
                'child_id': CHILD, 'local_recovery_only': True, **reference}}


def test_local_recognition_forces_no_cloud_and_no_raw_even_if_requested(environment, monkeypatch):
    settings, db = environment
    with TestClient(create_app(settings, model_gateway=NoCloud())) as client:
        sid = start(client)
        media = LocalMediaAI(db, settings.data_dir, settings.data_dir / 'missing-models')
        calls = []
        def recognize(wav, **kwargs):
            calls.append(kwargs)
            return WORDS, 1000, 'synthetic-local', False
        monkeypatch.setattr(media, '_recognize_wav', recognize)
        result = media.transcribe(sid, audio(settings), 'pet_speaking', 'direct-local-rescue', True,
                                 local_recovery_only=True)
        assert calls[0]['prefer_cloud'] is False
        assert calls[0]['purpose'] == 'agent_command'
        assert result['purpose'] == 'agent_command' and result['local_path'] is None


@pytest.mark.parametrize('field,value', [
    ('pending_evaluation_turn_id', 'unknown-eval'),
    ('question_reference', {'prompt_id': 'p', 'item_version': 'v'}),
    ('activity_reference', {'activity_id': 'a', 'version': 'v', 'last_turn_id': 't'}),
])
def test_recovery_capture_rejects_all_learning_references(environment, field, value):
    settings, db = environment
    with TestClient(create_app(settings, model_gateway=NoCloud())) as client:
        sid = start(client)
        with pytest.raises(VoiceCaptureBindingError, match='VOICE_CAPTURE_RECOVERY_REFERENCE_FORBIDDEN'):
            VoiceCaptureBindingService(db).begin(sid, 'mixed-recovery', audio(settings), 'agent_command',
                {'child_id': CHILD, 'local_recovery_only': True, field: value})


def test_recovery_capture_freezes_strict_mode_and_legacy_default(environment):
    settings, db = environment
    with TestClient(create_app(settings, model_gateway=NoCloud())) as client:
        sid = start(client)
        service = VoiceCaptureBindingService(db)
        for invalid in (1, 'true', None):
            with pytest.raises(VoiceCaptureBindingError, match='VOICE_CAPTURE_RECOVERY_MODE_INVALID'):
                service.begin(sid, 'invalid-mode', audio(settings), 'agent_command',
                              {'child_id': CHILD, 'local_recovery_only': invalid})
        original = service.begin(sid, 'ordinary-before-recovery', audio(settings), 'agent_command', {'child_id': CHILD})
        assert original['local_recovery_only'] == 0
        with pytest.raises(VoiceCaptureBindingError, match='VOICE_CAPTURE_ACTION_CONFLICT'):
            service.begin(sid, 'ordinary-before-recovery', audio(settings), 'agent_command',
                          {'child_id': CHILD, 'local_recovery_only': True})


def learning_facts(db):
    with db.connect() as c:
        return {table: [tuple(r) for r in c.execute('SELECT * FROM ' + table)] for table in (
            'agent_objective', 'agent_commitment', 'diagnostic_prompt', 'diagnostic_attempt',
            'learning_evidence', 'learning_evaluation_execution', 'rubric_assessment', 'pet_media_binding')}


def local_recognizer(monkeypatch, words=WORDS):
    calls = []
    def recognize(self, wav, **kwargs):
        calls.append(kwargs)
        assert kwargs['prefer_cloud'] is False and kwargs['purpose'] == 'agent_command'
        return words, 1000, 'synthetic-local-recognizer', False
    monkeypatch.setattr(LocalMediaAI, '_recognize_wav', recognize)
    return calls


@pytest.mark.parametrize('words,safety', [(WORDS, True), ('继续给这道题打分，然后开始数学任务。', False)])
def test_actual_local_capture_agent_execution_and_restart_never_touch_learning(environment, monkeypatch, words, safety):
    from test_voice_device_capability_contract import receipt
    settings, db = environment
    settings = replace(settings, data_encryption_key=b'R' * 32)
    with TestClient(create_app(settings, model_gateway=NoCloud()), raise_server_exceptions=False) as client:
        parent(client)
        assert client.put('/v1/parent/privacy', json={'raw_media_storage_allowed': True}).status_code == 200
        sid = start(client)
        prompt = client.post(f'/v1/sessions/{sid}/diagnostic-prompt/prepare', json={
            'track': 'school', 'preferred_item_id': 'diag-p4-arithmetic-closed-1', 'client_action_id': 'original-learning'})
        assert prompt.status_code == 200, prompt.text
        before = learning_facts(db)
        calls = local_recognizer(monkeypatch, words)
        original = client.post(f'/v1/sessions/{sid}/asr', json=payload(settings))
        assert original.status_code == 200, original.text
        voice = original.json()
        assert voice['local_path'] is None
        assert voice['capture_binding']['local_recovery_only'] is True
        assert voice['capture_binding']['raw_audio_available'] is False
        assert len(calls) == 1
        def forbidden_reconcile(*args, **kwargs):
            raise AssertionError('Recovery must not reconcile learning state')
        with monkeypatch.context() as isolated:
            isolated.setattr(AgentRuntimeStore, 'reconcile_learning_state', forbidden_reconcile)
            hooks = []
            orchestrator = client.app.state.agent_orchestrator
            isolated.setattr(orchestrator.agent_runtime, 'finalize_diagnostic_turn', lambda *a, **k: hooks.append('diagnostic'))
            isolated.setattr(orchestrator.companion_memory, 'observe_turn', lambda *a, **k: hooks.append('memory'))
            body = {'transcript': words, 'client_action_id': 'local-recovery-agent',
                    'local_recovery_only': True, 'voice_input_id': voice['voice_input_id']}
            heard = client.post(f'/v1/sessions/{sid}/agent-turn', json=body)
            assert heard.status_code == 200, heard.text
            turn = heard.json()
            assert turn['action']['local_recovery_control'] == {'version': 'local_voice_recovery_v1', 'mode': 'safety_only'}
            assert turn['after_execution'] == 'stop'
            assert 'pending_evaluation_control' not in turn['action']
            assert turn['action']['type'] == ('find_parent_urgent' if safety else 'none')
            if safety:
                assert turn['requires_control_admission'] is True
                admitted = client.post(f"/v1/sessions/{sid}/agent-turns/{turn['agent_turn_id']}/control-admission")
                assert admitted.status_code == 200 and admitted.json()['status'] == 'admitted', admitted.text
                ack = receipt('safety.find_parent', {'parent_contact_prompted': True})
                ack['execution_receipt']['action_type'] = turn['action']['type']
                done = client.put(f"/v1/agent-turns/{turn['agent_turn_id']}/outcome", json={
                    'final_response': turn['response_text'], 'tool_status': 'completed', 'result': ack})
            else:
                assert turn['requires_control_admission'] is False
                done = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
            assert done.status_code == 200, done.text
            assert hooks == []
        assert learning_facts(db) == before
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM child_safety_event').fetchone()[0] == int(safety)
            assert c.execute('SELECT objective_id FROM agent_plan WHERE agent_turn_id=?',
                             (turn['agent_turn_id'],)).fetchone()[0] is None
            assert c.execute('SELECT count(*) FROM model_call').fetchone()[0] == 0
        for changed in ({**body, 'local_recovery_only': False}, {**body, 'pending_evaluation_turn_id': 'foreign'}):
            result = client.post(f'/v1/sessions/{sid}/agent-turn', json=changed)
            assert result.status_code == 409, result.text
    with TestClient(create_app(settings, model_gateway=NoCloud()), raise_server_exceptions=False) as client:
        calls = local_recognizer(monkeypatch, words)
        replay = client.post(f'/v1/sessions/{sid}/agent-turn', json=body)
        assert replay.status_code == 200, replay.text
        assert replay.json()['agent_turn_id'] == turn['agent_turn_id']
        assert not calls and learning_facts(db) == before
        repeated = client.post(f'/v1/sessions/{sid}/asr', json=payload(settings))
        assert repeated.status_code == 200, repeated.text
        assert repeated.json()['voice_input_id'] == voice['voice_input_id'] and not calls
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM child_safety_event').fetchone()[0] == int(safety)


def test_missing_local_model_is_real_failure_and_standby_cannot_send_recovery_audio(environment, monkeypatch):
    settings, db = environment
    cloud_calls = []
    def forbidden_cloud(*args, **kwargs):
        cloud_calls.append(True)
        raise AssertionError('Recovery must never send audio to cloud')
    monkeypatch.setattr(CloudAsrGateway, 'transcribe', forbidden_cloud)
    with TestClient(create_app(settings, model_gateway=NoCloud()), raise_server_exceptions=False) as client:
        sid = start(client)
        media = client.app.state.media_ai
        media.model_root = settings.data_dir / 'definitely-no-local-model'
        class ConfiguredCloud:
            configured = True
            transcribe = staticmethod(forbidden_cloud)
        media._cloud_asr = ConfiguredCloud()
        result = client.post(f'/v1/sessions/{sid}/asr', json=payload(settings))
        assert result.status_code == 503, result.text
        assert 'ASR_MODEL_NOT_READY' in result.text
        standby = client.post('/v1/standby/asr', json=payload(settings, key='standby-recovery'))
        assert standby.status_code == 409, standby.text
        assert not cloud_calls
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM voice_input').fetchone()[0] == 0
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 0


def rescue_turn(client, settings, monkeypatch):
    sid = start(client)
    local_recognizer(monkeypatch)
    captured = client.post(f'/v1/sessions/{sid}/asr', json=payload(settings))
    assert captured.status_code == 200, captured.text
    heard = client.post(f'/v1/sessions/{sid}/agent-turn', json={
        'transcript': WORDS, 'client_action_id': 'new-rescue-turn', 'local_recovery_only': True,
        'voice_input_id': captured.json()['voice_input_id']})
    assert heard.status_code == 200, heard.text
    turn = heard.json()
    assert turn['requires_control_admission'] is True
    admitted = client.post(f"/v1/sessions/{sid}/agent-turns/{turn['agent_turn_id']}/control-admission")
    assert admitted.status_code == 200 and admitted.json()['status'] == 'admitted', admitted.text
    return sid, turn


def complete_rescue(client, turn, response=None):
    from test_voice_device_capability_contract import receipt
    ack = receipt('safety.find_parent', {'parent_contact_prompted': True})
    ack['execution_receipt']['action_type'] = turn['action']['type']
    return client.put(f"/v1/agent-turns/{turn['agent_turn_id']}/outcome", json={
        'final_response': response if response is not None else turn['response_text'],
        'tool_status': 'completed', 'result': ack})


def test_rescue_bridge_cannot_persist_false_parent_contact_claim(environment, monkeypatch):
    settings, db = environment
    with TestClient(create_app(settings, model_gateway=NoCloud()), raise_server_exceptions=False) as client:
        _, turn = rescue_turn(client, settings, monkeypatch)
        rejected = complete_rescue(client, turn, '已经联系到家长，家长马上就来。')
        assert rejected.status_code == 409, rejected.text
        with db.connect() as c:
            row = c.execute('SELECT response_text,tool_status FROM agent_turn WHERE agent_turn_id=?',
                            (turn['agent_turn_id'],)).fetchone()
            assert row['response_text'] == turn['response_text'] and row['tool_status'] == 'pending'
        accepted = complete_rescue(client, turn)
        assert accepted.status_code == 200, accepted.text
        with db.connect() as c:
            assert c.execute('SELECT response_text FROM agent_turn WHERE agent_turn_id=?',
                             (turn['agent_turn_id'],)).fetchone()[0] == turn['response_text']


@pytest.mark.parametrize('fault', ['capture_cancel', 'turn_cancel', 'switch_child', 'changed_quote'])
def test_rescue_actual_admission_does_not_authorize_late_invalid_outcome(environment, monkeypatch, fault):
    settings, db = environment
    with TestClient(create_app(settings, model_gateway=NoCloud()), raise_server_exceptions=False) as client:
        sid, turn = rescue_turn(client, settings, monkeypatch)
        if fault == 'capture_cancel':
            assert client.post(f'/v1/sessions/{sid}/asr/actions/recovery-capture/cancel').status_code == 200
        elif fault == 'switch_child':
            switched = client.post(f'/v1/sessions/{sid}/switch-child', json={
                'target_child_id': OTHER, 'client_action_id': 'switch-after-rescue-admission'})
            assert switched.status_code == 200, switched.text
        else:
            # Fault injection changes/revokes already-admitted authority. Never
            # sets running: admission above is the actual production HTTP path.
            with db.connect() as c:
                if fault == 'turn_cancel':
                    c.execute("UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id=?", (turn['agent_turn_id'],))
                    c.execute("UPDATE agent_step SET status='cancelled' WHERE plan_id=(SELECT plan_id FROM agent_plan WHERE agent_turn_id=?)", (turn['agent_turn_id'],))
                else:
                    c.execute("UPDATE agent_turn SET transcript='请帮我做数学题' WHERE agent_turn_id=?", (turn['agent_turn_id'],))
                c.commit()
        before = learning_facts(db)
        rejected = complete_rescue(client, turn)
        assert rejected.status_code == 409, rejected.text
        assert learning_facts(db) == before
        with db.connect() as c:
            row = c.execute('SELECT response_text,tool_status FROM agent_turn WHERE agent_turn_id=?',
                            (turn['agent_turn_id'],)).fetchone()
            assert row['response_text'] == turn['response_text'] and row['tool_status'] != 'completed'


def test_inflight_normal_semantic_journal_cannot_be_reused_as_local_recovery(tmp_path):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    from home_study_agent.local_voice_recovery import LocalVoiceRecovery
    from home_study_agent.agent_runtime import InvalidAgentPlanError
    from test_pending_evaluation_control import host, ask
    nested = []
    holder = {}
    def during_model(db):
        # The transport already runs in TestClient's portal. Run the actual
        # local service on a separate loop, not a reentrant fake HTTP call.
        def recover():
            try:
                return asyncio.run(LocalVoiceRecovery(holder['client'].app.state.agent_orchestrator).interpret(
                    holder['sid'], '帮我看看刚才评价怎么样了？', 'restricted-control'))
            except InvalidAgentPlanError as error:
                return str(error)
        with ThreadPoolExecutor(max_workers=1) as pool:
            nested.append(pool.submit(recover).result(timeout=3))
    with host(tmp_path, hook=during_model) as (db, sid, client, requests):
        holder.update(client=client, sid=sid)
        result = ask(client, sid)
        assert len(nested) == 1
        assert nested[0] == 'LOCAL_VOICE_RECOVERY_REQUEST_CONFLICT', nested[0]
        assert result.status_code == 200, result.text
        assert len(requests) == 1
        with db.connect() as c:
            assert c.execute("SELECT count(*) FROM agent_turn WHERE json_extract(action_json,'$.local_recovery_control') IS NOT NULL").fetchone()[0] == 0


@pytest.mark.parametrize('source', ['cancelled', 'deleted'])
def test_rescue_does_not_depend_on_invalid_original_evaluation(tmp_path, monkeypatch, source):
    from test_pending_evaluation_control import host
    with host(tmp_path) as (db, sid, client, requests):
        settings = client.app.state.media_ai
        # Actual original evaluation exists before this explicit cancellation /
        # deletion fault. It is never supplied to the independent rescue request.
        with db.connect() as c:
            if source == 'cancelled':
                c.execute("UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id='eval'")
                c.execute("UPDATE agent_step SET status='cancelled' WHERE plan_id=(SELECT plan_id FROM agent_plan WHERE agent_turn_id='eval')")
            else:
                c.execute('DELETE FROM learning_evaluation_execution')
                c.execute("UPDATE agent_turn SET tool_error_code='SOURCE_DELETED' WHERE agent_turn_id='eval'")
            c.commit()
        before = learning_facts(db)
        local_recognizer(monkeypatch)
        captured = client.post(f'/v1/sessions/{sid}/asr', json=payload(settings, child_id='child-runtime'))
        assert captured.status_code == 200, captured.text
        def forbidden(*args, **kwargs):
            raise AssertionError('Rescue cannot reconcile original learning')
        with monkeypatch.context() as isolated:
            isolated.setattr(AgentRuntimeStore, 'reconcile_learning_state', forbidden)
            heard = client.post(f'/v1/sessions/{sid}/agent-turn', json={
                'transcript': WORDS, 'client_action_id': 'independent-rescue', 'local_recovery_only': True,
                'voice_input_id': captured.json()['voice_input_id']})
            assert heard.status_code == 200, heard.text
            turn = heard.json()
            assert turn['action']['type'] == 'find_parent_urgent' and turn['requires_control_admission']
            assert client.post(f"/v1/sessions/{sid}/agent-turns/{turn['agent_turn_id']}/control-admission").json()['status'] == 'admitted'
            assert complete_rescue(client, turn).status_code == 200
        assert learning_facts(db) == before and requests == []


def test_legacy_capture_migration_cannot_gain_recovery_mode_and_ordinary_asr_is_unchanged(environment, monkeypatch):
    settings, db = environment
    calls = []
    def recognize(self, wav, **kwargs):
        calls.append(kwargs)
        return '我刚才说的话先保留。', 1000, 'synthetic-ordinary', False
    monkeypatch.setattr(LocalMediaAI, '_recognize_wav', recognize)
    body = payload(settings, 'legacy-ordinary', local_recovery_only=False)
    with TestClient(create_app(settings, model_gateway=NoCloud()), raise_server_exceptions=False) as client:
        sid = start(client)
        original = client.post(f'/v1/sessions/{sid}/asr', json=body)
        assert original.status_code == 200, original.text
        assert len(calls) == 1 and calls[0]['prefer_cloud'] is True
    with db.connect() as c:
        c.execute('ALTER TABLE voice_capture_binding DROP COLUMN local_recovery_only')
        c.commit()
    with TestClient(create_app(settings, model_gateway=NoCloud()), raise_server_exceptions=False) as client:
        replay = client.post(f'/v1/sessions/{sid}/asr', json=body)
        assert replay.status_code == 200 and replay.json()['voice_input_id'] == original.json()['voice_input_id'], replay.text
        assert len(calls) == 1
        rejected = client.post(f'/v1/sessions/{sid}/asr', json=payload(settings, 'legacy-ordinary'))
        assert rejected.status_code == 409, rejected.text
        with db.connect() as c:
            row = c.execute('SELECT local_recovery_only FROM voice_capture_binding WHERE voice_input_id=?',
                            (original.json()['voice_input_id'],)).fetchone()
            assert row[0] == 0


def test_background_memory_catchup_cannot_ingest_local_recovery(environment, monkeypatch):
    settings, db = environment
    words = '我喜欢画画。'
    with TestClient(create_app(settings, model_gateway=NoCloud()), raise_server_exceptions=False) as client:
        sid = start(client)
        local_recognizer(monkeypatch, words)
        captured = client.post(f'/v1/sessions/{sid}/asr', json=payload(settings))
        assert captured.status_code == 200, captured.text
        heard = client.post(f'/v1/sessions/{sid}/agent-turn', json={
            'transcript': words, 'client_action_id': 'not-memory-source', 'local_recovery_only': True,
            'voice_input_id': captured.json()['voice_input_id']})
        assert heard.status_code == 200, heard.text
        tid = heard.json()['agent_turn_id']
        executed = client.post(f'/v1/agent-turns/{tid}/execute')
        assert executed.status_code == 200, executed.text
        memory = client.app.state.agent_orchestrator.companion_memory
        with db.connect() as c:
            assert not c.execute('SELECT 1 FROM companion_memory_source WHERE turn_id=?', (tid,)).fetchone()
            assert not c.execute('SELECT 1 FROM companion_memory_checkpoint WHERE turn_id=?', (tid,)).fetchone()
        # Direct observation must return before pruning; independent background
        # maintenance may still prune unrelated memories.
        with monkeypatch.context() as isolated:
            pruned = []
            isolated.setattr(memory, '_prune', lambda *a, **k: pruned.append(True))
            memory.observe_turn(tid)
            assert pruned == []
        observed = []
        observe = memory.observe_turn
        def traced(turn_id):
            observed.append(turn_id)
            return observe(turn_id)
        monkeypatch.setattr(memory, 'observe_turn', traced)
        memory.process_pending()
        assert tid not in observed
        with db.connect() as c:
            turn = c.execute('SELECT * FROM agent_turn WHERE agent_turn_id=?', (tid,)).fetchone()
            assert memory._eligible(turn) is False
            assert not c.execute('SELECT 1 FROM companion_memory_source WHERE turn_id=?', (tid,)).fetchone()
            assert not c.execute('SELECT 1 FROM companion_memory_checkpoint WHERE turn_id=?', (tid,)).fetchone()


def test_rescue_event_failure_rolls_back_whole_turn_and_original_key_can_retry(environment, monkeypatch):
    import sqlite3
    import home_study_agent.local_voice_recovery as recovery
    settings, db = environment
    with TestClient(create_app(settings, model_gateway=NoCloud()), raise_server_exceptions=False) as client:
        sid = start(client)
        local_recognizer(monkeypatch)
        captured = client.post(f'/v1/sessions/{sid}/asr', json=payload(settings))
        assert captured.status_code == 200, captured.text
        body = {'transcript': WORDS, 'client_action_id': 'retry-atomic-rescue', 'local_recovery_only': True,
                'voice_input_id': captured.json()['voice_input_id']}
        before = learning_facts(db)
        persist = recovery.persist_safety_event
        def fail_after_event(*args, **kwargs):
            persist(*args, **kwargs)
            raise sqlite3.OperationalError('synthetic local write fault')
        with monkeypatch.context() as isolated:
            isolated.setattr(recovery, 'persist_safety_event', fail_after_event)
            failed = client.post(f'/v1/sessions/{sid}/agent-turn', json=body)
            assert failed.status_code == 500, failed.text
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM agent_turn').fetchone()[0] == 0
            assert c.execute('SELECT count(*) FROM agent_plan').fetchone()[0] == 0
            assert c.execute('SELECT count(*) FROM agent_step').fetchone()[0] == 0
            assert c.execute('SELECT count(*) FROM child_safety_event').fetchone()[0] == 0
        retried = client.post(f'/v1/sessions/{sid}/agent-turn', json=body)
        assert retried.status_code == 200, retried.text
        assert client.post(f'/v1/sessions/{sid}/agent-turn', json=body).json()['agent_turn_id'] == retried.json()['agent_turn_id']
        assert learning_facts(db) == before
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM agent_turn').fetchone()[0] == 1
            assert c.execute('SELECT count(*) FROM child_safety_event').fetchone()[0] == 1
