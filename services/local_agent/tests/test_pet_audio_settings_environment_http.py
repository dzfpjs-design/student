"""Synthetic renderer IPC -> protected settings -> real environment -> HTTP/SSE/SQLite.

OS keychain/IPC/PIN and physical ASR/planning/provider are synthetic; no cloud calls.
"""
import base64
from contextlib import contextmanager
from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time

import httpx
import pytest
from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.pet_audio_catalog import MODEL
from home_study_agent.pet_audio_gateway import PetAudioGateway, from_settings
from home_study_agent.pet_audio_work import PetAudioTeachingService
from home_study_agent.pet_audio_source import PetAudioAuthorityError
from test_pet_audio_gateway import feedback, event
from test_pet_audio_vertical_http import tcp_client
from test_pet_oral_supply_http import setup, post, CHILD
from test_oral_runtime_feedback_http import OralGateway, begin_oral, ANSWER
from test_oral_followup_execution_http import answer
from test_pet_audio_catalog import catalog_clock


@contextmanager
def provider(monkeypatch):
    posts, errors = [], []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            try:
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                audio = base64.b64decode(payload['messages'][1]['content'][1]['input_audio']['data'].split(',')[1])
                posts.append({'model': payload['model'], 'sha256': hashlib.sha256(audio).hexdigest(),
                    'request_keys': tuple(sorted(payload)), 'modalities': payload['modalities'], 'max_tokens': payload['max_tokens']})
                body = event({'model': MODEL, 'choices': [{'index': 0, 'delta': {'content': json.dumps(feedback())}, 'finish_reason': None}]})
                body += event({'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]}) + b'data: [DONE]\n\n'
                self.send_response(200); self.send_header('Content-Type', 'text/event-stream'); self.send_header('Content-Length', str(len(body))); self.end_headers()
                for offset in range(0, len(body), 13): self.wfile.write(body[offset:offset+13]); self.wfile.flush()
            except BaseException as error: errors.append(repr(error))
    server = HTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, name='synthetic-pet-config-provider'); thread.start()
    class Forward(httpx.AsyncBaseTransport):
        def __init__(self): self.inner = httpx.AsyncHTTPTransport()
        async def handle_async_request(self, request):
            assert request.url.host == 'dashscope.aliyuncs.com'
            forwarded = httpx.Request(request.method, f'http://127.0.0.1:{server.server_port}/chat/completions',
                headers=request.headers, stream=request.stream)
            return await self.inner.handle_async_request(forwarded)
        async def aclose(self): await self.inner.aclose()
    original = PetAudioGateway.__init__
    def initialize(self, endpoint, **kwargs):
        original(self, endpoint, **kwargs)
        self.transport = Forward()
    monkeypatch.setattr(PetAudioGateway, '__init__', initialize)
    try: yield posts
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)
        assert not thread.is_alive() and not errors, errors


def test_saved_reviewed_settings_environment_requires_separate_child_budget_and_survives_http_restart(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[3]
    output = tmp_path/'synthetic-env.json'
    env = {key: value for key, value in os.environ.items() if not key.startswith('HSC_')}
    env['HSC_PET_SETTINGS_TEST_ENV_PATH'] = str(output)
    log = tmp_path/'renderer-ipc.log'
    with log.open('w') as handle:
        completed = subprocess.run([shutil.which('node'), str(root/'node_modules/vitest/vitest.mjs'), 'run',
            'src/renderer/src/ParentPetAudioEnablement.ipc.test.tsx'], cwd=root, env=env, stdout=handle, stderr=subprocess.STDOUT, timeout=45)
    assert completed.returncode == 0, log.read_text()[-3000:]
    packet = json.loads(output.read_text())
    assert packet['synthetic'] is True and packet['applies'] == 1 and packet['revision'] == 1
    control=Path(packet['environment']['HSC_PET_AUDIO_CONTROL_FILE'])
    assert control.name=='pet-audio-catalog-control.bin' and control.parent.parent==tmp_path.resolve()
    assert control.parent.is_dir() and not control.exists() and packet['environment']['HSC_PET_AUDIO_CONTROL_STATE']=='ABSENT'
    for key, value in packet['environment'].items(): monkeypatch.setenv(key, value)
    parsed = Settings.from_environment()
    initial, settings = setup(tmp_path)
    settings = replace(settings, data_encryption_key=b'A'*32,
        **{field.name: getattr(parsed, field.name) for field in fields(parsed) if field.name.startswith('pet_audio_')})
    db = Database(initial.path, media_root=settings.data_dir, data_encryption_key=settings.data_encryption_key)
    planner = OralGateway(); headers = {'X-HSC-Session': settings.session_token}
    path = f'/v1/parent/children/{CHILD}/pet-audio/authorization'
    with provider(monkeypatch) as posts:
        app = create_app(settings, model_gateway=planner)
        with tcp_client(app, headers) as client:
            token = post(client, '/v1/parent/setup', {'pin': '7391'})['parent_token']; client.headers['X-HSC-Parent'] = token
            before = client.get(path).json()
            assert before['configured'] is True and before['current'] is False and before['grant'] is None
            assert len(posts) == 0
            assert client.put('/v1/parent/privacy', json={'raw_media_storage_allowed': True,
                'cloud_model_text_allowed': True, 'cloud_audio_allowed': True}).status_code == 200
            grant = dict(allowed=True, expires_at=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat(),
                day_limit_micros=1000000, request_limit_micros=100000, expected_revision=0, action_id='purpose-after-settings')
            assert client.post(path, json={**grant, 'request_limit_micros': 0}).status_code == 422
            assert post(client, path, grant)['grant']['revision'] == 1
            assert post(client, path, grant)['grant']['revision'] == 1
            assert client.get(path.replace(CHILD, 'child-junior-3')).json()['current'] is False
            sid, prompt, _, _ = begin_oral(client, db, planner)
            saved, voice = answer(client, settings, monkeypatch, sid, prompt, planner, ANSWER, 'settings-original')
            assert saved['original_audio_teaching']['state'] == 'queued'
            tid = saved['local_learning_evaluation']['feedback_turn_id']
            deadline = time.monotonic()+15
            while time.monotonic() < deadline:
                status = client.get(f'/v1/agent-turns/{tid}/execution-status').json()
                if status['status'] == 'completed': break
                time.sleep(.05)
            result = status['execution']['execution']['result']
            assert result['oral_feedback']['status'] == 'formative_audio_feedback'
            assert result['objective_update_applied'] is False and saved['attempt']['score_status'] == 'needs_review'
            assert len(posts) == 1 and posts[0]['model'] == MODEL
            assert 'enable_thinking' not in posts[0]['request_keys']
            assert posts[0]['modalities'] == ['text'] and posts[0]['max_tokens'] == 1024
            with db.connect() as connection:
                row = dict(connection.execute('SELECT * FROM pet_audio_dispatch').fetchone())
                assert row['state'] == 'settled'
                assert connection.execute('SELECT count(*) FROM rubric_review_event').fetchone()[0] == 0
                original_dispatch = row
        with tcp_client(create_app(settings, model_gateway=planner), headers) as client:
            client.headers['X-HSC-Parent'] = post(client, '/v1/parent/unlock', {'pin': '7391'})['parent_token']
            assert client.get(path).json()['current'] is True
            assert client.get(path+'/actions/purpose-after-settings').json()['grant']['revision'] == 1
            assert post(client, f'/v1/agent-turns/{tid}/execute', {})['execution']['result']['oral_feedback']['status'] == 'formative_audio_feedback'
            assert len(posts) == 1
            with db.connect() as connection:
                assert dict(connection.execute('SELECT * FROM pet_audio_dispatch').fetchone()) == original_dispatch
            assert post(client, path, {**grant, 'allowed': False, 'expected_revision': 1, 'action_id': 'revoke-after-restart'})['grant']['revision'] == 2
            assert client.get(path).json()['current'] is False and len(posts) == 1
        changed = replace(settings, pet_audio_configuration_id='same-provider-new-config')
        service = PetAudioTeachingService(db, media_root=settings.data_dir, data_encryption_key=settings.data_encryption_key, gateway=from_settings(changed))
        with db.connect() as connection:
            with pytest.raises(PetAudioAuthorityError): service._grant(connection, CHILD)
        print('PET_SETTINGS_E2E '+json.dumps({'ipc_applies': packet['applies'], 'provider_posts': len(posts),
            'model': posts[0]['model'], 'audio_sha256': posts[0]['sha256'], 'settings_action_id': parsed.pet_audio_configuration_id,
            'grant_key': 'purpose-after-settings', 'dispatch_state': original_dispatch['state'],
            'restart_duplicate_posts': 0, 'review_events': 0, 'source': 'synthetic-WAV-ASR-planner-IPC-keychain-provider'}))
