"""Opt-in official-model check using one synthetic replacement request."""
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.model_gateway import ModelGateway
from test_mistake_pending_answer_http import execute
from test_mistake_pending_recovery_control_http import checkpoint
from test_mistake_voice_journey import focus, ok


@pytest.mark.skipif(os.getenv('HSC_RUN_PENDING_REPLACEMENT_LIVE') != '1',
                    reason='explicit single paid synthetic probe')
def test_official_model_replaces_only_unprocessed_saved_answer(tmp_path, monkeypatch):
    configured, sid, db, pending = checkpoint(tmp_path, monkeypatch)
    credential = subprocess.run(
        ['security', 'find-generic-password', '-s', 'hsc-deepseek-api-key', '-w'],
        capture_output=True, text=True, timeout=15)
    assert credential.returncode == 0, 'authorized model credential unavailable'
    live = replace(
        configured,
        model_provider='deepseek',
        model_api_key=credential.stdout.strip(),
        model_base_url='https://api.deepseek.com/v1',
        model_name='deepseek-v4-pro',
        voice_intent_model_name='deepseek-v4-pro',
        model_timeout_seconds=180,
        fallback_model_provider=None,
        fallback_model_api_key=None,
    )
    metrics = []

    async def transport(request):
        assert not metrics and request.url.host == 'api.deepseek.com'
        body = json.loads(request.content)
        sent = json.dumps(body['messages'], ensure_ascii=False)
        for private in (pending['utterance'], pending['action_id'], pending['authorized_turn_id'], sid):
            assert private not in sent
        entry = {'requested_model': body['model']}
        metrics.append(entry)
        started = time.monotonic()
        async with httpx.AsyncClient(timeout=180, trust_env=False, follow_redirects=False) as client:
            response = await client.send(request)
        output = response.json()
        entry.update(
            status=response.status_code,
            actual_model=output.get('model'),
            usage=output.get('usage'),
            elapsed_seconds=round(time.monotonic() - started, 3),
        )
        return response

    try:
        text = '刚才没处理完那份不是我要交的，把答案换成A吧'
        with TestClient(create_app(
                live, model_gateway=ModelGateway(live, transport=httpx.MockTransport(transport)))) as client:
            request = ok(client.post(f'/v1/sessions/{sid}/agent-turn', json={
                'transcript': text,
                'client_action_id': 'live-replace-pending',
                'cloud_text_allowed': True,
            }))
            assert request['action']['type'] == 'continue_mistake_dialogue'
            assert request['action']['pending_answer_control'] == 'replace'
            assert request['action']['replacement_answer'] == 'A'
        with TestClient(create_app(configured)) as client:
            result = execute(client, request)
            assert execute(client, request) == result
            assert 'pending_operation' not in focus(client, sid)['state']
        with db.connect() as c:
            assert [row[0] for row in c.execute('SELECT submitted_answer FROM mistake_attempt')] == ['A']
            audit = json.loads(c.execute(
                'SELECT result_json FROM mistake_dialogue_event WHERE client_action_id=?',
                (pending['action_id'],)).fetchone()[0])
            assert audit['pending_answer_replacement']['replacement_answer'] == 'A'
        assert len(metrics) == 1
    finally:
        Path('/tmp/hsc-pending-replacement-live-metrics.json').write_text(
            json.dumps(metrics, ensure_ascii=False, indent=2))
