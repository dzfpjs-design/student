"""Current Node source reader -> original Runtime execution -> durable output.

Supplier events, audio and model results are synthetic, never user recordings.
"""
import json
import multiprocessing
from pathlib import Path
import socket
import subprocess
import time
from contextlib import contextmanager

import httpx
import pytest
import uvicorn
from test_realtime_output_authority import case, KEY, DOMAIN
from test_realtime_input_source import input_case, input_proof
from test_realtime_output_authority_tcp import call


def worker(listener, directory, database_path, lose_bind):
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    from test_cloud_first_agent import Planner
    gateway = Planner('conversation.respond', {'response': '我们按顺序继续聊。'}, '陪我聊聊花园里的花，第一句还有第二句。',
                      role='question', objective_type='companion')
    gateway.payload['spoken_transition'] = '我们按顺序继续聊。'
    settings = Settings(data_dir=Path(directory), database_path=Path(database_path), maintenance_enabled=False,
        session_token='synthetic-output-session-token', native_launch_attestation_key=KEY, agent_runtime_enabled=True)
    application = create_app(settings, model_gateway=gateway)
    async def wrapper(scope, receive, send):
        if scope['type'] == 'http' and scope['path'].endswith('/voice-output-authority'):
            messages = []
            async def hold(message): messages.append(message)
            await application(scope, receive, hold)
            body = b''.join(message.get('body', b'') for message in messages)
            if messages[0]['status'] != 200:
                print('QUALIFICATION_DENIED ' + str(json.loads(body).get('detail', {}).get('code')), flush=True)
            if lose_bind and messages[0]['status'] == 200 and json.loads(body).get('dispatch_allowed') is True:
                await send({'type': 'http.response.start', 'status': 503, 'headers': [(b'content-length', b'0')]})
                await send({'type': 'http.response.body', 'body': b''})
                return
            for message in messages: await send(message)
        else: await application(scope, receive, send)
    uvicorn.Server(uvicorn.Config(wrapper, log_level='error', access_log=False)).run(sockets=[listener])


@contextmanager
def runtime_server(db, lose_bind=False):
    listener = socket.socket(); listener.bind(('127.0.0.1', 0))
    origin = 'http://127.0.0.1:' + str(listener.getsockname()[1])
    process = multiprocessing.get_context('spawn').Process(target=worker, args=(listener, str(db.path.parent), str(db.path), lose_bind))
    process.start()
    try:
        with httpx.Client(base_url=origin, trust_env=False) as client:
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                assert process.is_alive()
                try:
                    if client.get('/v1/health', timeout=.5, headers={'X-HSC-Session': 'synthetic-output-session-token'}).status_code == 200: break
                except httpx.HTTPError: pass
                time.sleep(.05)
            else: raise AssertionError('Owned Runtime startup deadline')
        yield origin, process
    finally:
        if process.is_alive(): process.terminate()
        process.join(10)
        if process.is_alive(): process.kill(); process.join(5)
        listener.close()
        assert not process.is_alive()


def test_original_capture_not_model_rewrite_executes_once_but_protected_reply_stays_local(input_case, case):
    db, scope, capture = input_case
    output = case[1]
    root = Path(__file__).resolve().parents[3]
    script = root / 'docs/evidence/2026-10-05-realtime-original-input/connection-client.cjs'
    with runtime_server(db) as (origin, process):
        result = subprocess.run(['node', str(script)], input=json.dumps({'scope': scope, 'output': output,
            'runtime': {'status': 'ready', 'localApiUrl': origin, 'sessionToken': 'synthetic-output-session-token'}}),
            text=True, capture_output=True, timeout=30)
        assert 'signature' not in result.stdout and capture['original_transcript'] not in result.stdout
        observed = json.loads(result.stdout)
        assert result.returncode == 0, observed
        assert observed['original_passed_to_runtime'] and observed['source_reads'] == 2
        with db.connect() as c:
            row = c.execute('SELECT intent,action_json FROM agent_turn WHERE agent_turn_id=?', (observed['identity']['agent_turn_id'],)).fetchone()
            action = json.loads(row['action_json'])
            policy = {'intent': row['intent'], **{k: action.get(k) for k in ('type', 'source', 'local_only', 'cloud_transmission_allowed')}}
        # This legacy Planner supplies no actual Gateway transport provenance.
        # D-030 still refuses its synthetic self-assertion; original flags stay protected.
        assert policy['local_only'] is True and policy['cloud_transmission_allowed'] is False
        assert observed['sent'].count('response.create') == 0 and observed['owned_socket_closed']
        assert not observed['supplier_real'] and not observed['paid_response_requests'] and not observed['rendered_completed']
        assert not observed['generated_completed'] and observed['frames'] == []
        assert observed['failures'] == ['QWEN_CONNECTION_OUTPUT_UNCONFIRMED']
        process.kill(); process.join(10)
        assert process.exitcode == -9
    with runtime_server(db) as (origin, _), httpx.Client(base_url=origin, trust_env=False) as client:
        recovered = call(origin, observed['identity'], 'inspect')
        assert recovered == {'error': 'NATIVE_REALTIME_RECEIPT_UNCONFIRMED'}
        result = client.post(f"/v1/sessions/{scope['session_id']}/voice-input-source", json=input_proof(scope),
            headers={'X-HSC-Session': 'synthetic-output-session-token'})
        assert result.status_code == 200 and result.json()['transcript'] == capture['original_transcript']
        assert result.json()['arrival_status'] == 'current' and not result.json()['replay_allowed']
        receipt = client.get(f"/v1/sessions/{scope['session_id']}/agent-request-receipts/realtime-{scope['voice_input_id']}",
            headers={'X-HSC-Session': 'synthetic-output-session-token'})
        assert receipt.status_code == 200 and receipt.json()['status'] == 'recorded_completed'
        repeated = client.post(f"/v1/sessions/{scope['session_id']}/voice/realtime/actions/{scope['client_action_id']}/resume",
            json={'child_id': scope['child_id'], 'voice_input_id': scope['voice_input_id']},
            headers={'X-HSC-Session': 'synthetic-output-session-token'})
        assert repeated.status_code == 409
    with db.connect() as c:
        turns = c.execute('SELECT * FROM agent_turn WHERE client_action_id=?', ('realtime-' + scope['voice_input_id'],)).fetchall()
        assert len(turns) == 1 and turns[0]['transcript'] == capture['original_transcript']
        assert turns[0]['tool_status'] == 'completed'
        assert c.execute("SELECT count(*) FROM model_call WHERE purpose='agent_plan' AND provider='test'").fetchone()[0] == 1
        assert c.execute('SELECT count(*) FROM audit_event WHERE actor=?', (DOMAIN,)).fetchone()[0] == 0
        assert c.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
