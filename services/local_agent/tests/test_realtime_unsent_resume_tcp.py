"""Mounted renderer -> TCP -> Runtime -> SQLite -> SIGKILL -> original receipt."""
from contextlib import closing
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.voice_capture_binding import VoiceCaptureBindingService
from test_diagnostic_prompt_http import environment, start, CHILD


@pytest.mark.parametrize('delivery', ['unsent', 'planned'])
def test_original_unsent_speech_renderer_execution_and_crash_recovery(environment, tmp_path, delivery):
    settings, db = environment
    source = Path(__file__).parents[1] / 'src'
    fixture = Path(__file__).parent / 'fixtures/document_dialogue_process.py'
    root = Path(__file__).parents[3]
    with TestClient(create_app(settings)) as setup:
        sid = start(setup)
        service = VoiceCaptureBindingService(db)
        key = 'tcp-unsent-original'
        quote = '我们先不看这张纸，你有PET学习资料可以给我介绍吗？'
        service.begin_realtime(sid, key, 'agent_command', {'child_id': CHILD}, model_version='synthetic-asr')
        voice = service.complete_realtime(sid, key, quote)
    processes = []
    runtime_snapshot = None
    def launch():
        nonlocal runtime_snapshot
        log = (tmp_path / f'resume-backend-{len(processes)}.log').open('w')
        p = subprocess.Popen([sys.executable, str(fixture), '--data-root', str(tmp_path),
            '--session-token', 'public-realtime-resume-tcp-session'],
            stdout=subprocess.PIPE, stderr=log, text=True,
            env={**os.environ, 'PYTHONPATH': str(source), 'PYTHONDONTWRITEBYTECODE': '1'})
        processes.append((p, log))
        assert select.select([p.stdout], [], [], 15)[0], 'startup deadline'
        info = json.loads(p.stdout.readline())
        assert info['synthetic_models']
        assert info['pid']==p.pid and p.poll() is None
        assert info['session_token']=='public-realtime-resume-tcp-session'
        client = httpx.Client(base_url=f"http://127.0.0.1:{info['port']}", timeout=10, trust_env=False,
            headers={'X-HSC-Session':info['session_token']})
        for _ in range(200):
            try:
                if client.get('/openapi.json').status_code == 200:
                    assert p.poll() is None
                    runtime_snapshot=dict(localApiUrl=str(client.base_url).rstrip('/'),
                        sessionToken=info['session_token'],runtimeEpoch=len(processes))
                    return p, client
            except httpx.TransportError: pass
            time.sleep(.05)
        client.close()
        raise AssertionError('HTTP readiness deadline')
    try:
        process, client = launch()
        planned_turn_id = None
        if delivery == 'planned':
            with closing(client):
                original = client.post(f'/v1/sessions/{sid}/agent-turn', json={'transcript': quote,
                    'voice_input_id': voice['voice_input_id'], 'client_action_id': 'realtime-' + voice['voice_input_id']})
                assert original.status_code == 200, original.text
                planned_turn_id = original.json()['agent_turn_id']
                assert original.json()['tool_status'] == 'pending'
            process.kill(); process.wait(timeout=5)
            process, client = launch()
        with closing(client):
            config = tmp_path / 'renderer-resume-input.json'
            result = tmp_path / 'renderer-resume-result.json'
            config.write_text(json.dumps(dict(base_url=str(client.base_url).rstrip('/'), runtime_snapshot=runtime_snapshot, session_id=sid,
                child_id=CHILD, input_id=key, voice_input_id=voice['voice_input_id'],
                planned_turn_id=planned_turn_id, result=str(result))))
            log = tmp_path / 'renderer-resume.log'
            with log.open('w') as output:
                rendered = subprocess.run([str(root / 'node_modules/.bin/vitest'), 'run',
                    'src/renderer/src/RealtimeResumeRealTcp.test.tsx'], cwd=root,
                    env={**os.environ, 'HSC_REALTIME_RESUME_TCP_CASE': str(config)},
                    stdout=output, stderr=subprocess.STDOUT, timeout=90)
            assert rendered.returncode == 0, log.read_text()[-4500:]
            assert result.exists()
        process.kill(); process.wait(timeout=5)
        assert process.returncode == -signal.SIGKILL
        process, client = launch()
        with closing(client):
            receipt = client.get(f"/v1/sessions/{sid}/agent-request-receipts/realtime-{voice['voice_input_id']}")
            assert receipt.status_code == 200 and receipt.json()['status'] == 'recorded_completed', receipt.text
            rejected = client.post(f'/v1/sessions/{sid}/voice/realtime/actions/{key}/resume',
                json={'child_id': CHILD, 'voice_input_id': voice['voice_input_id']})
            assert rejected.status_code == 409, rejected.text
        with db.connect() as c:
            rows = c.execute('SELECT agent_turn_id,transcript,client_action_id FROM agent_turn').fetchall()
            assert len(rows) == 1 and rows[0]['transcript'] == quote
            assert rows[0]['client_action_id'] == 'realtime-' + voice['voice_input_id']
            if planned_turn_id: assert rows[0]['agent_turn_id'] == planned_turn_id
            assert c.execute("SELECT COUNT(*) FROM model_call WHERE purpose='agent_plan'").fetchone()[0] == 1
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
    finally:
        for p, log in processes:
            if p.poll() is None: p.terminate(); p.wait(timeout=5)
            if p.stdout: p.stdout.close()
            log.close()
