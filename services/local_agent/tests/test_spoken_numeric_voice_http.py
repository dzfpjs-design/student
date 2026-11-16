"""Synthetic ASR text through actual agent and original-answer HTTP handlers."""
import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from test_diagnostic_prompt_http import environment, start, endpoint, binding
from test_specialized_closed_http import generated_http
from test_specialized_closed_contract import ClosedGateway
from test_spoken_numeric import numeric_generated


@pytest.mark.parametrize('cancel', [False, True])
def test_agent_dispatches_numeric_speech_to_only_original_reviewed_question(environment, monkeypatch, cancel):
    import test_specialized_closed_contract as closed
    monkeypatch.setattr(closed, 'generated', numeric_generated)
    settings, db = environment
    gateway = ClosedGateway()
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        headers, candidates = generated_http(client, db)
        candidate = next(r for r in candidates if r['stage'] == 'parallel')
        approved = client.post('/v1/parent/specialized-supply/candidates/' + candidate['candidate_id'] + '/review',
            headers=headers, json={'decision': 'approve', 'note': 'Synthetic parent checked integer value contract',
                                   'client_action_id': 'numeric-http-review'})
        assert approved.status_code == 200, approved.text
        sid = start(client)
        prepared = client.post(endpoint(sid, 'prepare'), json={'track': 'competition_math',
            'preferred_item_id': approved.json()['diagnostic_item_id'], 'client_action_id': 'numeric-http-prepare'})
        assert prepared.status_code == 200, prepared.text
        prompt = prepared.json()['prompt']
        transcript = '我的答案是六。'
        turn = client.post(f'/v1/sessions/{sid}/agent-turn', json={'transcript': transcript,
            'client_action_id': 'numeric-original-turn', 'cloud_text_allowed': True})
        assert turn.status_code == 200, turn.text
        action = turn.json()['action']
        assert action['type'] == 'submit_diagnostic_answer', turn.json()
        assert action['diagnostic_prompt_id'] == prompt['prompt_id']
        assert action['diagnostic_item_version'] == prompt['item_version']
        assert action['answer'] == '六'
        assert action['cloud_transmission_allowed'] is False
        if cancel:
            from home_study_agent.diagnostic_prompt import DiagnosticPromptService
            service = DiagnosticPromptService(db)
            service.cancel(sid, **binding(prompt))
            replacement = service.prepare(sid, preferred_item_id='diag-p4-arithmetic-closed-1',
                                          client_action_id='numeric-replacement')['prompt']
        payload = {**binding(prompt), 'answer': action['answer'], 'client_action_id': 'numeric-original-answer',
                   'authorized_turn_id': turn.json()['agent_turn_id']}
        answer = client.post(endpoint(sid, 'answer'), json=payload)
        assert answer.status_code == 200, answer.text
        if cancel:
            assert answer.json()['status'] == 'cancelled'
            assert service.read(sid)['prompt']['prompt_id'] == replacement['prompt_id']
        else:
            attempt = answer.json()['attempt']
            assert attempt['correctness'] == 1 and attempt['score_status'] == 'verified_local'
            assert attempt['submitted_answer'] == '六'
            again = client.post(endpoint(sid, 'answer'), json=payload)
            assert again.status_code == 200
            assert again.json()['attempt']['attempt_id'] == attempt['attempt_id']
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_attempt WHERE session_id=?', (sid,)).fetchone()[0] == (0 if cancel else 1)
            original = dict(c.execute('SELECT * FROM agent_turn WHERE agent_turn_id=?',
                                     (turn.json()['agent_turn_id'],)).fetchone())
            assert transcript in original.values()
    assert len(gateway.requests) == 1  # Only synthetic source generation, never this answer.
