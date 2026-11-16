"""A stale displayed question blocks grading, not a queued safety command."""
import json

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.agent_runtime import AgentRuntimeStore
from home_study_agent.diagnostic_prompt import DiagnosticPromptService
from test_agent_continuation import Gateway
from test_diagnostic_prompt_http import environment
from test_voice_runtime_binding import start, prepare, capture, interpret


@pytest.mark.parametrize('text,action_type', [
    ('关闭摄像头', 'pause_observation'), ('停止监听', 'privacy_pause'),
    ('别说了', 'stop_speech'), ('90', 'none'),
])
def test_old_question_queued_asr_preserves_control_but_not_answer(environment, monkeypatch, text, action_type):
    settings, db = environment
    gateway = Gateway({})
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid = start(client)
        original = prepare(client, sid)
        DiagnosticPromptService(db).cancel(sid, prompt_id=original['prompt_id'], item_version=original['item_version'])
        current = prepare(client, sid, 'new-waiting-question', 'diag-p4-arithmetic-closed-2')
        voice = capture(client, settings, monkeypatch, sid, text, original)
        heard = interpret(client, sid, text, 'stale-source-command', voice)
        assert heard['action']['type'] == action_type
        snapshot = heard['action']['voice_input_binding']
        assert snapshot['question_validation'] == 'stale'
        assert snapshot['question_reference'] == {'kind': 'diagnostic', 'binding': {
            key: original[key] for key in ('prompt_id', 'item_version')}}
        assert not AgentRuntimeStore.capture_matches_answer(snapshot, 'submit_diagnostic_answer',
            snapshot['question_reference']['binding'])
        if action_type == 'none':
            assert heard['intent'] == 'pending_answer_clarification'
            assert 'pending_answer_candidate' not in heard['action']
            assert client.post(f"/v1/agent-turns/{heard['agent_turn_id']}/execute").status_code == 200
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 0
            assert c.execute('SELECT status FROM diagnostic_prompt WHERE prompt_id=?', (current['prompt_id'],)).fetchone()[0] == 'waiting_answer'
            row = c.execute('SELECT question_validation,question_reference_json FROM voice_capture_binding WHERE voice_input_id=?', (voice,)).fetchone()
            assert row['question_validation'] == 'stale'
            assert json.loads(row['question_reference_json']) == snapshot['question_reference']
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        replay = interpret(client, sid, text, 'stale-source-command', voice)
        assert replay['agent_turn_id'] == heard['agent_turn_id']
        assert replay['action']['voice_input_binding'] == snapshot
    assert not gateway.requests
