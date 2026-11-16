"""Read saved original answers without completing or replaying their turn."""
import json

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.diagnostic_prompt import DiagnosticPromptService
from test_agent_continuation import Gateway
from test_diagnostic_prompt_http import environment, start, prepare, OTHER
from test_pending_answer_arbitration_http import turn


def snapshot(db):
    with db.connect() as c:
        return {name: [tuple(row) for row in c.execute(f'SELECT * FROM {name} ORDER BY rowid')]
                for name in ('agent_turn', 'agent_plan', 'agent_step', 'diagnostic_prompt', 'diagnostic_attempt')}


def save_original(client, sid, db, monkeypatch, *, scoring_interrupted=False):
    prompt = prepare(client, sid)['prompt']
    speech = turn(client, sid, '12', 'saved-original-answer')
    assert speech['action']['type'] == 'submit_diagnostic_answer'
    if scoring_interrupted:
        def interrupted(*args, **kwargs):
            raise RuntimeError('synthetic interruption after durable answer reservation')
        monkeypatch.setattr(DiagnosticPromptService, 'resume', interrupted)
    response = client.post(f'/v1/sessions/{sid}/diagnostic-prompt/answer', json={
        **{key: prompt[key] for key in ('prompt_id', 'item_version')},
        'answer': '12', 'client_action_id': 'saved-answer-write',
        'authorized_turn_id': speech['agent_turn_id'],
    })
    assert response.status_code == (500 if scoring_interrupted else 200), response.text
    with db.connect() as c:
        assert c.execute('SELECT tool_status FROM agent_turn WHERE agent_turn_id=?',
                         (speech['agent_turn_id'],)).fetchone()[0] not in ('completed', 'failed')
    return prompt, speech


@pytest.mark.parametrize('scoring_interrupted', [False, True])
def test_saved_answer_is_clear_but_unfinished_turn_is_not_completed_or_replayed(
        environment, monkeypatch, scoring_interrupted):
    settings, db = environment
    gateway = Gateway({})
    gateway.configured = False
    with TestClient(create_app(settings, model_gateway=gateway), raise_server_exceptions=False) as client:
        sid = start(client)
        save_original(client, sid, db, monkeypatch, scoring_interrupted=scoring_interrupted)
        route = f'/v1/sessions/{sid}/agent-request-receipts/saved-original-answer'
        before = snapshot(db)
        response = client.get(route)
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload['status'] == 'interpreted'
        assert '原答案已经保存' in payload['response_text']
        assert '后续操作还没有完成回执' in payload['response_text']
        assert '不用重复提交' in payload['response_text']
        assert '继续任务' in payload['response_text']
        assert payload['replay_allowed'] is False
        assert payload['mastery_update_allowed'] is False
        assert payload['local_only'] is True
        assert payload['cloud_transmission_allowed'] is False
        assert response.headers['cache-control'] == 'no-store'
        assert snapshot(db) == before
    monkeypatch.undo()
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        assert client.get(route).json() == payload
        assert snapshot(db) == before
    assert not gateway.requests


@pytest.mark.parametrize('damage', [
    'authorization', 'child', 'version', 'answer', 'malformed_answer', 'attempt_child', 'missing_attempt',
])
def test_unrelated_or_inconsistent_saved_answer_cannot_qualify_original_request(
        environment, monkeypatch, damage):
    settings, db = environment
    gateway = Gateway({})
    gateway.configured = False
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid = start(client)
        prompt, _ = save_original(client, sid, db, monkeypatch)
        with db.connect() as c:
            if damage == 'attempt_child':
                c.execute('UPDATE diagnostic_attempt SET child_id=? WHERE attempt_id='
                          '(SELECT attempt_id FROM diagnostic_prompt WHERE prompt_id=?)', (OTHER, prompt['prompt_id']))
            else:
                field, value = {
                    'authorization': ('authorized_turn_id', 'another-turn'),
                    'child': ('child_id', OTHER),
                    'version': ('item_version', 'different-version'),
                    'answer': ('answer_json', json.dumps({'answer': '13', 'hint_level': 0})),
                    'malformed_answer': ('answer_json', '{invalid'),
                    'missing_attempt': ('attempt_id', None),
                }[damage]
                c.execute(f'UPDATE diagnostic_prompt SET {field}=? WHERE prompt_id=?', (value, prompt['prompt_id']))
            c.commit()
        before = snapshot(db)
        response = client.get(f'/v1/sessions/{sid}/agent-request-receipts/saved-original-answer')
        assert response.status_code == 200, response.text
        assert response.json()['status'] == 'interpreted'
        assert '原答案已经保存' not in response.json()['response_text']
        assert snapshot(db) == before
    assert not gateway.requests
