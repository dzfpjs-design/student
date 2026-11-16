"""Actual PCM recorder/API/TCP/SQLite/SIGKILL; synthetic acoustics/recognition."""
from contextlib import closing
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time

from fastapi.testclient import TestClient
import httpx
import pytest

from home_study_agent.app import create_app
from home_study_agent.database import Database
from home_study_agent.config import Settings
from home_study_agent.pet_media_evidence import PetMediaEvidenceService
from test_diagnostic_prompt_http import start, CHILD
from test_learning_runtime_http import NoCloud


@pytest.mark.parametrize('retain', [False, True])
@pytest.mark.parametrize('capture_mode', ['prefix', 'short', 'short_48k', 'preroll_short', 'real_short_word', 'real_dialogue_marker'])
def test_original_pcm_answer_tcp_and_restart(tmp_path, monkeypatch, retain, capture_mode):
    real_vad = capture_mode in ('real_short_word', 'real_dialogue_marker')
    monkeypatch.setenv('HSC_DATA_DIR', str(tmp_path / 'import-only'))
    monkeypatch.setenv('HSC_DATABASE_PATH', str(tmp_path / 'import-only' / 'home-study.db'))
    settings = Settings(data_dir=tmp_path, database_path=tmp_path / 'http-prompts.db',
                        maintenance_enabled=False, data_encryption_key=b'D' * 32)
    db = Database(settings.database_path)
    with TestClient(create_app(settings, model_gateway=NoCloud())) as client:
        sid = start(client)
        token = client.post('/v1/parent/setup', json={'pin': '7391'}).json()['parent_token']
        assert client.put('/v1/parent/privacy', headers={'X-HSC-Parent': token},
                          json={'raw_media_storage_allowed': retain}).status_code == 200
        prepared = client.post(f'/v1/sessions/{sid}/diagnostic-prompt/prepare', json={
            'track': 'pet', 'preferred_item_id': 'diag-pet-speaking-2', 'client_action_id': 'original-tcp-prompt'})
        assert prepared.status_code == 200, prepared.text
        prompt = prepared.json()['prompt']
    source = Path(__file__).parents[1] / 'src'
    root = Path(__file__).parents[3]
    processes = []

    def launch():
        log = (tmp_path / f'backend-{len(processes)}.log').open('w')
        command = [sys.executable, str(Path(__file__).parent / 'fixtures/original_answer_process.py'),
                   '--data-root', str(tmp_path)]
        if real_vad:
            command.append('--real-vad')
        if capture_mode == 'real_dialogue_marker':
            command.append('--dialogue-marker')
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=log, text=True,
                                   env={**os.environ, 'PYTHONPATH': str(source), 'PYTHONDONTWRITEBYTECODE': '1'})
        processes.append((process, log))
        assert select.select([process.stdout], [], [], 20)[0], 'startup deadline'
        info = json.loads(process.stdout.readline())
        assert info['synthetic_models']
        client = httpx.Client(base_url=f"http://127.0.0.1:{info['port']}", timeout=10, trust_env=False)
        for _ in range(200):
            try:
                if client.get('/openapi.json').status_code == 200:
                    return process, client
            except httpx.TransportError:
                pass
            time.sleep(.05)
        client.close()
        raise AssertionError('readiness deadline')

    try:
        process, client = launch()
        with closing(client):
            config = tmp_path / 'renderer-input.json'
            result = tmp_path / 'renderer-result.json'
            config.write_text(json.dumps({'base_url': str(client.base_url).rstrip('/'), 'session_id': sid,
                'child_id': CHILD, 'prompt_id': prompt['prompt_id'], 'item_version': prompt['item_version'],
                'result': str(result), 'capture_mode': capture_mode,
                'wav_fixture': str(Path(__file__).parent / 'fixtures/short-mandarin-dui-trimmed.wav')}))
            log = tmp_path / 'renderer.log'
            with log.open('w') as output:
                rendered = subprocess.run([str(root / 'node_modules/.bin/vitest'), 'run',
                    'src/renderer/src/OriginalAnswerRealTcp.test.tsx'], cwd=root, timeout=90,
                    env={**os.environ, 'HSC_ORIGINAL_ANSWER_TCP_CASE': str(config)},
                    stdout=output, stderr=subprocess.STDOUT)
            assert rendered.returncode == 0, log.read_text()[-4000:]
            record = json.loads(result.read_text())
        process.kill(); process.wait(timeout=5)
        assert process.returncode == -signal.SIGKILL
        process, client = launch()
        with closing(client):
            recovered = client.get(f'/v1/sessions/{sid}/asr/actions/{record["client_action_id"]}')
            assert recovered.status_code == 200, recovered.text
            assert recovered.json()['voice_input_id'] == record['voice_input_id']
            assert bool(recovered.json()['local_path']) is retain
            assert recovered.json()['duration_ms'] == record['duration_ms']
            outcome = client.get(f'/v1/sessions/{sid}/agent-request-receipts/original-tcp-turn')
            assert outcome.status_code == 200, outcome.text
            assert outcome.json()['status'] == ('interpreted' if real_vad else 'recorded_completed'), outcome.text
            answer = client.get(f'/v1/sessions/{sid}/diagnostic-prompt', params={'prompt_id': prompt['prompt_id']})
            assert answer.status_code == 200, answer.text
            assert answer.json()['status'] == ('waiting_answer' if real_vad else 'answered'), answer.text
            assert answer.json()['prompt']['prompt_id'] == prompt['prompt_id']
        with db.connect() as c:
            capture = c.execute('SELECT * FROM voice_capture_binding WHERE voice_input_id=?', (record['voice_input_id'],)).fetchone()
            assert capture['wav_sha256'] == record['sha256']
            assert capture['prompt_id'] == prompt['prompt_id'] and capture['item_version'] == prompt['item_version']
            assert record['raw_audio_available'] is retain
            assert c.execute('SELECT tool_status FROM agent_turn WHERE agent_turn_id=?',
                             (record['agent_turn_id'],)).fetchone()[0] == ('pending' if real_vad else 'completed')
            assert c.execute('SELECT COUNT(*) FROM voice_input').fetchone()[0] == 1
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == int(not real_vad)
            assert c.execute('SELECT COUNT(*) FROM pet_media_binding').fetchone()[0] == int(retain)
            calls = c.execute('SELECT * FROM model_call').fetchall()
            assert len(calls) == int(real_vad)
            if calls:
                manifest = json.loads(calls[0]['context_manifest_json'])
                assert calls[0]['status'] == 'completed'
                assert manifest['speech_admission']['admission_policy'] == 'webrtc-sustained-short-v2'
                assert manifest['speech_admission']['speech_frames'] >= 4
            media_id = c.execute('SELECT media_id FROM pet_media_binding WHERE voice_input_id=?',
                                 (record['voice_input_id'],)).fetchone()
        if retain:
            media = PetMediaEvidenceService(db, media_root=tmp_path, data_encryption_key=b'D' * 32).read(sid, media_id=media_id[0])
            assert media['audio']['sha256'] == record['sha256']
            assert media['qualification']['skill_qualified'] is False
        calls = [json.loads(line) for line in (tmp_path / 'recognizer-calls.jsonl').read_text().splitlines()]
        assert calls == [{'sha256': record['sha256'], 'byte_length': record['byte_length'], 'purpose': 'pet_speaking'}]
    finally:
        for process, log in processes:
            if process.poll() is None:
                process.terminate(); process.wait(timeout=5)
            if process.stdout:
                process.stdout.close()
            log.close()
