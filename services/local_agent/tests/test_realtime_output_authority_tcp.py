"""Actual Node signer/client -> FastAPI TCP -> committed SQLite -> SIGKILL/restart.

Only synthetic stored replies are used. No microphone, supplier or household IO.
"""
import json
import multiprocessing
from pathlib import Path
import socket
import subprocess
import time
from contextlib import contextmanager

import httpx
import uvicorn
import pytest
from home_study_agent.app import create_app
from home_study_agent.config import Settings
from test_realtime_output_authority import case, KEY, DOMAIN


def worker(listener, directory, database_path, lose_bind):
    settings = Settings(data_dir=Path(directory), database_path=Path(database_path), maintenance_enabled=False,
                        session_token='synthetic-output-session-token', native_launch_attestation_key=KEY)
    application = create_app(settings)
    async def wrapper(scope, receive, send):
        if lose_bind and scope['type'] == 'http' and scope['path'].endswith('/voice-output-authority'):
            messages = []
            async def hold(message): messages.append(message)
            await application(scope, receive, hold)
            if messages[0]['status'] == 200:
                body = b''.join(m.get('body', b'') for m in messages)
                if json.loads(body).get('dispatch_allowed') is True:
                    # The application has already committed bind. Only the
                    # actual HTTP receipt is lost; no product guard is bypassed.
                    await send({'type': 'http.response.start', 'status': 503, 'headers': [(b'content-length', b'0')]})
                    await send({'type': 'http.response.body', 'body': b''})
                    return
            for message in messages: await send(message)
        else: await application(scope, receive, send)
    uvicorn.Server(uvicorn.Config(wrapper, log_level='error', access_log=False)).run(sockets=[listener])


@contextmanager
def server(db, lose_bind=False):
    context = multiprocessing.get_context('spawn')
    listener = socket.socket(); listener.bind(('127.0.0.1', 0))
    origin = 'http://127.0.0.1:' + str(listener.getsockname()[1])
    process = context.Process(target=worker, args=(listener, str(db.path.parent), str(db.path), lose_bind))
    process.start()
    try:
        with httpx.Client(base_url=origin, trust_env=False) as client:
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                assert process.is_alive()
                try:
                    if client.get('/v1/health', timeout=.5,
                                  headers={'X-HSC-Session': 'synthetic-output-session-token'}).status_code == 200: break
                except httpx.HTTPError: pass
                time.sleep(.05)
            else: raise AssertionError('Owned Runtime did not start')
        yield origin, process
    finally:
        if process.is_alive(): process.terminate()
        process.join(10)
        if process.is_alive(): process.kill(); process.join(5)
        listener.close()
        assert not process.is_alive()


def call(origin, scope, operation, response_id=''):
    root = Path(__file__).resolve().parents[3]
    script = root / 'docs/evidence/2026-10-05-realtime-tool-output-bridge/authority-client.cjs'
    command = dict(scope, operation=operation, response_id=response_id)
    result = subprocess.run(['node', str(script)], input=json.dumps({'command': command,
        'runtime': {'status': 'ready', 'localApiUrl': origin, 'sessionToken': 'synthetic-output-session-token'}}),
        text=True, capture_output=True, timeout=10)
    assert result.returncode in (0, 2), result.stderr[:200]
    assert '我们接着' not in result.stdout and 'signature' not in result.stdout
    return json.loads(result.stdout)


def test_native_main_post_commit_loss_and_killed_runtime_do_not_resend_or_rebind(case):
    db, scope, _ = case
    with server(db, lose_bind=True) as (origin, process):
        assert call(origin, scope, 'reserve')['has_reply'] is True
        assert call(origin, scope, 'bind', 'resp_synthetic_output') == {'error': 'NATIVE_REALTIME_RECEIPT_UNCONFIRMED'}
        process.kill(); process.join(10)
        assert process.exitcode == -9
    with db.connect() as c:
        assert c.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert c.execute('SELECT count(*) FROM audit_event WHERE actor=?', (DOMAIN,)).fetchone()[0] == 2
    with server(db) as (origin, _):
        result = call(origin, scope, 'inspect')
        assert result['state'] == 'bound' and result['response_id'] == 'resp_synthetic_output'
        assert not result['has_reply'] and not result['dispatch_allowed'] and not result['rendered_completed']
        assert call(origin, scope, 'reserve') == {'error': 'NATIVE_REALTIME_RECEIPT_UNCONFIRMED'}
        assert call(origin, scope, 'bind', 'resp_synthetic_output') == {'error': 'NATIVE_REALTIME_RECEIPT_UNCONFIRMED'}
        assert call(origin, scope, 'retire')['state'] == 'retired'
    with db.connect() as c:
        assert c.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert c.execute('SELECT count(*) FROM audit_event WHERE actor=?', (DOMAIN,)).fetchone()[0] == 3


@pytest.mark.parametrize('lose_bind', [False, True])
def test_original_tool_output_bridge_uses_real_runtime_grant_and_no_dispatch_retry(case, lose_bind):
    db, scope, _ = case
    script = Path(__file__).resolve().parents[3] / 'docs/evidence/2026-10-05-realtime-tool-output-bridge/runtime-turn-client.cjs'
    with server(db, lose_bind=lose_bind) as (origin, _):
        result = subprocess.run(['node', str(script)], input=json.dumps({'identity': scope, 'runtime': {
            'status': 'ready', 'localApiUrl': origin, 'sessionToken': 'synthetic-output-session-token'}}),
            text=True, capture_output=True, timeout=20)
        assert result.returncode == 0, result.stderr[:200]
        assert '我们接着' not in result.stdout and 'signature' not in result.stdout
        observed = json.loads(result.stdout)
        assert observed['started'] is True and observed['sent'].count('response.create') == 1
        assert observed['sent'].count('conversation.item.create') == 1
        assert observed['supplier_real'] is False and observed['rendered_verified'] is False
        if lose_bind:
            assert observed['frames'] == [] and observed['sent'].count('response.cancel') == 1
            assert observed['failures'] == ['QWEN_RUNTIME_OUTPUT_RECEIPT_UNCONFIRMED']
        else:
            assert observed['frames'] == [{'sequence': n, 'sampleCount': 2400} for n in [1, 2, 3]]
            assert observed['failures'] == [] and 'response.cancel' not in observed['sent']
    with server(db) as (origin, _):
        recovered = call(origin, scope, 'inspect')
        assert recovered['state'] == 'retired' and recovered['dispatch_allowed'] is False and not recovered['has_reply']
    with db.connect() as c:
        assert c.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        rows = c.execute('SELECT summary_json FROM audit_event WHERE actor=?', (DOMAIN,)).fetchall()
        assert len(rows) == 3 and all('我们接着' not in row[0] for row in rows)
