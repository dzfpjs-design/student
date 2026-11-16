"""Actual create_app/HTTP/SQLite/Runtime with synthetic Main proofs only.

No family data, live microphone, Windows, provider connection or paid model.
The executor spy delegates to the original implementation except in the one
explicit pre-effect TimeoutError fault case.
"""
import asyncio
import base64
from datetime import datetime, timezone
import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.realtime_capability_http import DOMAIN, FIELDS
from test_ordinary_learning_real_source_execution import (
    OriginalGatewayWithControlledTransport, env,
)

KEY = 'cd' * 32
TOKEN = 'public-synthetic-native-session-token'
CHILD = 'child-primary-4'
PATHS = {'input': 'native-input', 'proposal': 'capability-proposals',
         'receipt': 'capability-receipt', 'dialogue': 'native-dialogue',
         'context': 'native-context', 'catalog': 'capabilities', 'client': 'native-client'}


def sign(raw, operation, sid, voice='', *, connection='synthetic-owned-host', generation=1):
    proof = {'version': 1, 'operation': operation, 'session_id': sid,
             'voice_input_id': voice, 'connection_id': connection,
             'connection_generation': generation,
             'body_sha256': hashlib.sha256(raw).hexdigest(),
             'issued_at': datetime.now(timezone.utc).isoformat()}
    proof = {field: proof[field] for field in FIELDS}
    encoded = json.dumps([DOMAIN] + [proof[field] for field in FIELDS],
                         ensure_ascii=False, separators=(',', ':')).encode()
    proof['signature'] = hmac.new(bytes.fromhex(KEY), encoded, hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(json.dumps(proof).encode()).decode().rstrip('=')


@pytest.fixture
def native(env, tmp_path, monkeypatch):
    db, _ = env
    monkeypatch.setenv('HSC_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('HSC_DATABASE_PATH', str(db.path))
    settings = Settings(data_dir=tmp_path, database_path=db.path,
                        session_token=TOKEN, native_launch_attestation_key=KEY,
                        maintenance_enabled=False, model_api_key='SYNTHETIC_NO_REAL_ACCOUNT',
                        model_base_url='https://synthetic.invalid', fallback_model_api_key=None)
    gateway = OriginalGatewayWithControlledTransport(settings)
    app = create_app(settings, model_gateway=gateway)
    original = app.state.agent_capability_executor.execute
    calls = []
    faults = set()

    def execute(turn_id):
        calls.append(turn_id)
        if turn_id in faults:
            raise TimeoutError('PUBLIC_SYNTHETIC_BEFORE_EFFECT_TIMEOUT')
        return original(turn_id)

    monkeypatch.setattr(app.state.agent_capability_executor, 'execute', execute)
    with TestClient(app, client=('127.0.0.1', 1234), headers={'X-HSC-Session': TOKEN}) as client:
        setup = client.post('/v1/parent/setup', json={'pin': '8264'})
        assert setup.status_code == 200, setup.text
        parent = {'X-HSC-Parent': setup.json()['parent_token']}
        allowed = client.put('/v1/parent/privacy', headers=parent,
                             json={'cloud_model_text_allowed': True, 'cloud_audio_allowed': True})
        assert allowed.status_code == 200, allowed.text
        started = client.post('/v1/sessions', json={'child_id': CHILD,
                              'client_action_id': 'public-native-hmac-session'})
        assert started.status_code == 201, started.text
        sid = started.json()['session_id']

        def request(operation, body=None, **proof_options):
            raw = b'' if body is None else json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode()
            voice = '' if operation in {'input', 'catalog', 'context'} else body['voice_input_id']
            headers = {'X-HSC-Native-Voice-Proof': sign(raw, operation, sid, voice, **proof_options)}
            path = f'/v1/sessions/{sid}/voice/realtime/{PATHS[operation]}'
            return client.get(path, headers=headers) if body is None else client.post(
                path, content=raw, headers={**headers, 'Content-Type': 'application/json'})

        yield client, db, gateway, sid, parent, request, calls, faults
    asyncio.run(gateway.close())


def admit(native, key='fresh-input', words='帮我复习初三地理第一课，再查询本机学习资料。', **changes):
    body = {'version': 1, 'local_input_id': key, 'transcript': words,
            'captured_at_ms': int(time.time() * 1000), 'capture_reference': {'child_id': CHILD}}
    body.update(changes)
    response = native[5]('input', body)
    assert response.status_code == 200, response.text
    return response.json(), body


def proposal(voice, call, capability='learning.prepare_request', args=None, previous=None):
    body = {'version': 1, 'voice_input_id': voice['voice_input_id'],
            'supplier_session_id': 'public-supplier', 'input_item_id': 'public-input',
            'response_id': 'public-response', 'call_id': call,
            'capability_name': capability,
            'args': {'subject': '地理', 'requested_grade': '初三', 'lesson': '第一课'} if args is None else args}
    if previous is not None:
        body['previous_action_id'] = previous
    return body


def counts(db):
    with Database(db.path).connect() as c:
        return {table: c.execute('SELECT count(*) FROM '+table).fetchone()[0] for table in
                ('voice_input', 'voice_capture_binding', 'agent_turn', 'agent_plan',
                 'agent_objective', 'agent_outcome', 'model_call')}


def success(response):
    assert response.status_code == 200, response.text
    return response.json()


def execute_signed_server_proposal(request, body):
    admitted = success(request('proposal', body))
    assert admitted['execution'] == {'status': 'pending', 'location': 'server'}
    dispatch = admitted['client_dispatch']
    identity = {key: body[key] for key in ('version', 'voice_input_id', 'supplier_session_id',
                'input_item_id', 'response_id', 'call_id')}
    signed = {**identity, 'dispatch_id': dispatch['dispatch_id']}
    without_claim = request('client', {**signed, 'phase': 'execute'})
    assert without_claim.status_code == 409 and without_claim.json()['detail']['code'] == 'REALTIME_CLIENT_CLAIM_REQUIRED'
    assert success(request('client', {**signed, 'phase': 'claim'}))['status'] == 'claimed'
    try:
        execution_response = request('client', {**signed, 'phase': 'execute'})
    except TimeoutError:
        return success(request('receipt', identity))
    if execution_response.status_code == 500:
        return success(request('receipt', identity))
    executed = success(execution_response)
    turn = executed.get('host_execution', {}).get('turn') or {}
    if turn.get('tool_status') != 'completed':
        return executed
    assert executed['execution']['status'] == 'unknown'
    receipt = {'capability': body['capability_name'], 'execution_location': 'client_device_bridge',
               'action_type': dispatch['turn']['action']['type'], 'status': 'completed',
               'completed_at': datetime.now(timezone.utc).isoformat(), 'output': {}}
    return success(request('client', {**signed, 'phase': 'complete', 'tool_status': 'completed',
        'final_response': turn['response_text'], 'result': {'client_bridge_receipt': receipt}}))


def test_native_rename_claim_is_durable_and_does_not_claim_name_was_saved(native):
    _, db, _, sid, _, request, _, _ = native
    voice, _ = admit(native, 'rename-input', '以后叫你小枝小枝')
    body = proposal(voice, 'rename-call', 'voice.configure_wake_word', {'candidate': '小枝小枝'})
    result = success(request('proposal', body))
    dispatch = result['client_dispatch']
    assert dispatch['turn']['action']['type'] == 'configure_wake_word'
    assert dispatch['turn']['action']['candidate'] == '小枝小枝'
    identity = {key: body[key] for key in ('version', 'voice_input_id', 'supplier_session_id',
                'input_item_id', 'response_id', 'call_id')}
    claim_body = {**identity, 'dispatch_id': dispatch['dispatch_id'], 'phase': 'claim'}
    claim = success(request('client', claim_body))
    assert claim['status'] == 'claimed'
    assert claim['completion_descriptor']['agent_turn_id'] == dispatch['turn']['agent_turn_id']
    assert request('client', claim_body).status_code != 200
    reopened = Database(db.path)
    with reopened.connect() as connection:
        row = connection.execute('SELECT result_json FROM agent_step JOIN agent_plan USING(plan_id) WHERE agent_turn_id=?',
                                 (dispatch['turn']['agent_turn_id'],)).fetchone()
    assert row is not None and row['result_json'] is None


def test_actual_native_input_witness_runtime_multitool_replay_and_bad_argument_repair(native):
    client, db, gateway, sid, parent, request, calls, faults = native
    voice, body = admit(native)
    replay = success(request('input', body))
    assert replay['voice_input_id'] == voice['voice_input_id']
    assert voice['purpose'] == 'native_cloud_dialogue' and voice['model_provenance'] == 'client_reported'
    with Database(db.path).connect() as c:
        row = c.execute('SELECT * FROM voice_input WHERE voice_input_id=?', (voice['voice_input_id'],)).fetchone()
        assert row['purpose'] == 'native_cloud_dialogue' and row['transcript'] == body['transcript']
        assert row['local_path'] is None and row['duration_ms'] is None
        witness = json.loads(c.execute('SELECT summary_json FROM audit_event WHERE audit_id=?',
                                      ('native-input-'+voice['voice_input_id'],)).fetchone()[0])
        assert witness['voice_input_id'] == voice['voice_input_id'] and witness['child_id'] == CHILD
        assert witness['session_id'] == sid and witness['transcript_sha256'] == hashlib.sha256(
            json.dumps(body['transcript'], ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        assert c.execute('SELECT count(*) FROM voice_input').fetchone()[0] == 1

    prepared = success(request('proposal', proposal(voice, 'prepare')))
    assert prepared['execution']['status'] == 'completed'
    assert prepared['public_result']['request_saved'] is True
    assert prepared['public_result']['requested']['requested_grade'] == '初三'
    assert prepared['public_result']['source_verified'] is False and calls == []
    inspect_body = proposal(voice, 'inspect', 'system.inspect_learning',
                            {'scope': 'resources', 'track': 'all'}, prepared['action_id'])
    inspected = execute_signed_server_proposal(request, inspect_body)
    assert inspected['execution']['status'] == 'completed', inspected
    assert inspected['public_result']['source_kind'] == 'runtime_execution'
    assert inspected['public_result']['inventory_scope'] == 'local_index_metadata'
    assert len(calls) == 1
    before = counts(db)
    assert success(request('proposal', inspect_body))['action_id'] == inspected['action_id']
    semantic = {**inspect_body, 'call_id': 'inspect-semantic-replay'}
    assert success(request('proposal', semantic))['action_id'] == inspected['action_id']
    assert counts(db) == before and len(calls) == 1

    invalid = proposal(voice, 'bad-args', 'system.inspect_learning',
                       {'scope': 'not_a_registered_scope', 'track': 'all'}, inspected['action_id'])
    rejected = success(request('proposal', invalid))
    assert rejected['not_admitted'] is True and rejected['proposal_status'] == 'rejected'
    assert rejected['execution']['status'] == 'failed'
    assert counts(db) == before and len(calls) == 1
    fixed = {**invalid, 'call_id': 'repaired-args', 'args': {'scope': 'plan', 'track': 'all'}}
    repaired = execute_signed_server_proposal(request, fixed)
    assert repaired['execution']['status'] == 'completed' and len(calls) == 2
    assert counts(db)['agent_turn'] == before['agent_turn'] + 1
    assert counts(db)['agent_outcome'] == 0 and gateway.requests == []


def test_native_unverified_dialogue_revocation_and_legacy_routes_cannot_forge_outcomes(native):
    client, db, gateway, sid, parent, request, calls, faults = native
    voice, _ = admit(native)
    prepare_body = proposal(voice, 'prepare-guard')
    prepared = success(request('proposal', prepare_body))
    inspect_body = proposal(voice, 'inspect-guard', 'system.inspect_learning',
                            {'scope': 'resources', 'track': 'all'}, prepared['action_id'])
    inspected = execute_signed_server_proposal(request, inspect_body)
    dialogue = {key: prepare_body[key] for key in
                ('version', 'voice_input_id', 'supplier_session_id', 'input_item_id', 'response_id')}
    dialogue.update(text='我们可以先核对课本，再按你的问题继续交流。', status='unverified', terminal='completed')
    saved = success(request('dialogue', dialogue))
    before = counts(db)
    with db.connect() as c:
        stored = dict(c.execute('SELECT * FROM agent_turn WHERE agent_turn_id=?',
                                (prepared['agent_turn_id'],)).fetchone())
        action = json.loads(c.execute('SELECT action_json FROM agent_turn WHERE agent_turn_id=?',
                                     (saved['agent_turn_id'],)).fetchone()[0])
        assert action['mastery_update_allowed'] is False and action['independent_evidence'] is False
    denied = client.post('/v1/agent-turns/'+inspected['agent_turn_id']+'/execute')
    assert denied.status_code == 409, denied.text
    forged = {'final_response': '伪造的完全掌握', 'tool_status': 'completed',
              'result': {'mastery': True, 'execution_receipt': {'status': 'completed'}}}
    outcome = client.put('/v1/agent-turns/'+prepared['agent_turn_id']+'/outcome', json=forged)
    assert outcome.status_code == 409 and outcome.json()['detail']['code'] == 'NATIVE_CLIENT_COMPLETION_NOT_AUTHORIZED', outcome.text
    for suffix, method in (('/execute', client.post), ('/outcome', client.put)):
        result = method('/v1/agent-turns/'+saved['agent_turn_id']+suffix,
                        **({'json': forged} if suffix == '/outcome' else {}))
        assert result.status_code == 409, result.text
    assert counts(db) == before and len(calls) == 1
    with db.connect() as c:
        assert dict(c.execute('SELECT * FROM agent_turn WHERE agent_turn_id=?',
                              (prepared['agent_turn_id'],)).fetchone()) == stored
    revoked = client.put('/v1/parent/privacy', headers=parent, json={'cloud_audio_allowed': False})
    assert revoked.status_code == 200, revoked.text
    receipt = success(request('receipt', {key: inspect_body[key] for key in
                                         ('version', 'voice_input_id', 'supplier_session_id', 'input_item_id', 'response_id', 'call_id')}))
    assert receipt['execution']['status'] == 'completed'
    assert receipt['public_projection']['eligible'] is False and receipt['public_result']['withheld'] is True
    assert len(calls) == 1 and gateway.requests == []


def test_unknown_native_execution_is_read_only_and_does_not_block_a_new_independent_voice(native, monkeypatch):
    client, db, gateway, sid, parent, request, calls, faults = native
    voice, _ = admit(native, key='uncertain-original')
    service = client.app.state.native_realtime_capabilities
    original_execute = service.execute_turn
    uncertain_turns = []

    def fault_before_original(turn_id):
        uncertain_turns.append(turn_id)
        faults.add(turn_id)
        return original_execute(turn_id)

    monkeypatch.setattr(service, 'execute_turn', fault_before_original)
    body = proposal(voice, 'uncertain-call', 'system.inspect_learning', {'scope': 'resources', 'track': 'all'})
    first = execute_signed_server_proposal(request, body)
    assert first['execution']['status'] == 'unknown', first
    assert len(calls) == len(uncertain_turns) == 1
    before = counts(db)
    for _ in range(2):
        reread = success(request('proposal', body))
        assert reread['execution']['status'] == 'unknown' and reread['action_id'] == first['action_id']
    legacy = client.post('/v1/agent-turns/'+first['agent_turn_id']+'/execute')
    assert legacy.status_code == 409, legacy.text
    forged = client.put('/v1/agent-turns/'+first['agent_turn_id']+'/outcome',
                        json={'final_response': '伪造完成', 'tool_status': 'completed'})
    assert forged.status_code == 409, forged.text
    assert counts(db) == before and len(calls) == 1
    next_voice, _ = admit(native, key='new-independent-input', words='换一个新请求，帮我复习七年级历史。')
    next_goal = success(request('proposal', proposal(next_voice, 'new-independent-call',
                        args={'subject': '历史', 'requested_grade': '七年级'})))
    assert next_goal['execution']['status'] == 'completed'
    assert next_goal['public_result']['request_saved'] is True
    assert len(calls) == 1 and counts(db)['agent_outcome'] == 0 and gateway.requests == []


def test_official_consent_revocation_between_begin_and_complete_blocks_voice_and_witness_then_recovers(native, monkeypatch):
    from home_study_agent.voice_capture_binding import VoiceCaptureBindingService
    client, db, gateway, sid, parent, request, calls, faults = native
    original = VoiceCaptureBindingService.complete_realtime
    intervened = []

    def revoke_before_complete(captures, session_id, action, transcript, **hooks):
        if not intervened and action.startswith('qwen-native-input-'):
            # The real begin transaction has committed; the original complete
            # transaction and its pre_persist check have not started.
            assert hooks.get('pre_persist') is not None and hooks.get('post_persist') is not None
            revoked = client.put('/v1/parent/privacy', headers=parent, json={'cloud_audio_allowed': False})
            assert revoked.status_code == 200, revoked.text
            intervened.append(action)
        return original(captures, session_id, action, transcript, **hooks)

    monkeypatch.setattr(VoiceCaptureBindingService, 'complete_realtime', revoke_before_complete)
    body = {'version': 1, 'local_input_id': 'native-commit-revoke', 'transcript': '合成的新语音请求',
            'captured_at_ms': int(time.time()*1000), 'capture_reference': {'child_id': CHILD}}
    blocked = request('input', body)
    assert blocked.status_code == 409, blocked.text
    assert len(intervened) == 1 and counts(db)['voice_input'] == 0
    with db.connect() as c:
        assert c.execute("SELECT count(*) FROM audit_event WHERE actor='native_cloud_input_v1'").fetchone()[0] == 0
        prepared = c.execute('SELECT status,voice_input_id,purpose FROM voice_capture_binding').fetchone()
        assert prepared['status'] == 'prepared' and prepared['voice_input_id'] is None
        assert prepared['purpose'] == 'native_cloud_dialogue'
    restored = client.put('/v1/parent/privacy', headers=parent,
                          json={'cloud_audio_allowed': True, 'cloud_model_text_allowed': True})
    assert restored.status_code == 200, restored.text
    recovered = success(request('input', body))
    assert recovered['purpose'] == 'native_cloud_dialogue' and counts(db)['voice_input'] == 1
    with Database(db.path).connect() as c:
        assert c.execute("SELECT count(*) FROM audit_event WHERE actor='native_cloud_input_v1'").fetchone()[0] == 1
    assert counts(db)['agent_turn'] == 0 and calls == [] and gateway.requests == []


def test_new_host_generation_between_begin_and_complete_fences_the_queued_old_input(native, monkeypatch):
    from home_study_agent.voice_capture_binding import VoiceCaptureBindingService
    client, db, gateway, sid, parent, request, calls, faults = native
    original = VoiceCaptureBindingService.complete_realtime
    intervened = []

    def replace_host_before_complete(captures, session_id, action, transcript, **hooks):
        if not intervened and action.startswith('qwen-native-input-'):
            replacement = request('catalog', connection='synthetic-new-owned-host', generation=2)
            assert replacement.status_code == 200, replacement.text
            intervened.append(action)
        return original(captures, session_id, action, transcript, **hooks)

    monkeypatch.setattr(VoiceCaptureBindingService, 'complete_realtime', replace_host_before_complete)
    body = {'version': 1, 'local_input_id': 'native-commit-new-host', 'transcript': '合成的新连接语音请求',
            'captured_at_ms': int(time.time()*1000), 'capture_reference': {'child_id': CHILD}}
    blocked = request('input', body)
    assert blocked.status_code == 409, blocked.text
    assert len(intervened) == 1 and counts(db)['voice_input'] == 0
    with db.connect() as c:
        assert c.execute("SELECT count(*) FROM audit_event WHERE actor='native_cloud_input_v1'").fetchone()[0] == 0
    old = request('catalog')
    assert old.status_code == 409, old.text
    fresh = success(request('input', body, connection='synthetic-new-owned-host', generation=2))
    assert fresh['purpose'] == 'native_cloud_dialogue' and counts(db)['voice_input'] == 1
    assert counts(db)['agent_turn'] == 0 and calls == [] and gateway.requests == []
