"""Real HTTP writes keep business replay separate from command authorization."""
import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.diagnostic_prompt import DiagnosticPromptService
from test_diagnostic_prompt_http import environment, start, prepare
from test_pending_answer_arbitration_http import turn
from test_agent_continuation import Gateway
from home_study_agent.exam_simulation import ExamSimulationService
from test_pet_vocabulary_prompt_integration import environment as vocabulary_environment, flow, next_prompt


@pytest.mark.parametrize('fault,cancel', [(False, False), (False, True), (True, False), (True, True)])
def test_diagnostic_http_reservation_keeps_original_authority_across_restart(environment, monkeypatch, fault, cancel):
    settings, db = environment
    gateway = Gateway({})
    gateway.configured = False
    original_resume = DiagnosticPromptService.resume
    with TestClient(create_app(settings, model_gateway=gateway), raise_server_exceptions=False) as client:
        sid = start(client)
        prompt = prepare(client, sid)['prompt']
        speech = turn(client, sid, '12', 'authorized-original-answer')
        origin = speech['agent_turn_id']
        payload = {key: prompt[key] for key in ('prompt_id', 'item_version')}
        payload.update(answer='12', client_action_id='stable-answer-key', authorized_turn_id=origin)
        if fault:
            def unavailable(*args, **kwargs):
                raise RuntimeError('synthetic reservation-to-score failure')
            monkeypatch.setattr(DiagnosticPromptService, 'resume', unavailable)
        response = client.post(f'/v1/sessions/{sid}/diagnostic-prompt/answer', json=payload)
        assert response.status_code == (500 if fault else 200), response.text
        monkeypatch.setattr(DiagnosticPromptService, 'resume', original_resume)
        if cancel:
            with db.connect() as c:
                c.execute("UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id=?", (origin,))
                c.commit()
    with TestClient(create_app(settings, model_gateway=gateway), raise_server_exceptions=False) as client:
        retry_turn = turn(client, sid, '12', 'retry-with-new-current-turn')
        payload['authorized_turn_id'] = retry_turn['agent_turn_id']
        replay = client.post(f'/v1/sessions/{sid}/diagnostic-prompt/answer', json=payload)
        assert replay.status_code == (409 if fault and cancel else 200), replay.text
        with db.connect() as c:
            stored = c.execute('SELECT authorized_turn_id,status FROM diagnostic_prompt WHERE prompt_id=?', (prompt['prompt_id'],)).fetchone()
            assert stored['authorized_turn_id'] == origin
            assert stored['status'] == ('answer_saved' if fault and cancel else 'answered')
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == (0 if fault and cancel else 1)
        if not (fault and cancel):
            conflict = client.post(f'/v1/sessions/{sid}/diagnostic-prompt/answer', json={**payload, 'answer': '13'})
            assert conflict.status_code == 409
    assert not gateway.requests


@pytest.mark.parametrize('cancel', [False, True])
def test_exam_confirmed_reservation_rechecks_original_turn_after_restart(environment, monkeypatch, cancel):
    settings, db = environment
    gateway = Gateway({})
    gateway.configured = False
    submit_reserved = ExamSimulationService._submit_reserved
    with TestClient(create_app(settings, model_gateway=gateway), raise_server_exceptions=False) as client:
        sid = start(client)
        with db.connect() as c:
            c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
            c.commit()
        service = ExamSimulationService(db)
        run = service.start(sid, 'competition_math', '数学', 'practice', 2, 30, 'authorized-exam')
        binding = service.arrival_binding(sid)
        original = '我先合并两个部分，再去掉多出的那一段'
        candidate = turn(client, sid, original, 'exam-original-words')
        assert candidate['intent'] == 'pending_answer_clarification'
        assert client.post(f"/v1/agent-turns/{candidate['agent_turn_id']}/execute").status_code == 200
        confirmation = turn(client, sid, '刚才那句就是我的答案', 'exam-role-confirmed')
        origin = confirmation['agent_turn_id']
        payload = {**binding, 'answer': original, 'client_action_id': 'exam-stable-answer', 'authorized_turn_id': origin}
        def unavailable(*args, **kwargs):
            raise RuntimeError('synthetic exam reservation failure')
        monkeypatch.setattr(ExamSimulationService, '_submit_reserved', unavailable)
        assert client.post(f'/v1/sessions/{sid}/exam-simulations/bound-answer', json=payload).status_code == 500
        monkeypatch.setattr(ExamSimulationService, '_submit_reserved', submit_reserved)
        if cancel:
            with db.connect() as c:
                c.execute("UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id=?", (origin,))
                c.commit()
    with TestClient(create_app(settings, model_gateway=gateway), raise_server_exceptions=False) as client:
        resumed = client.post(f"/v1/exam-simulations/{run['run_id']}/control", json={'command': 'resume'})
        assert resumed.status_code == 200, resumed.text
        later = turn(client, sid, '今天计划做到哪了', 'exam-retry-context')
        payload['authorized_turn_id'] = later['agent_turn_id']
        replay = client.post(f'/v1/sessions/{sid}/exam-simulations/bound-answer', json=payload)
        assert replay.status_code == (409 if cancel else 200), replay.text
        with db.connect() as c:
            saved = c.execute('SELECT authorized_turn_id,answer_text FROM specialized_exam_item WHERE run_id=? AND position=?',
                              (binding['run_id'], binding['position'])).fetchone()
            assert saved['authorized_turn_id'] == origin and saved['answer_text'] == original
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == (0 if cancel else 1)
        if not cancel:
            with db.connect() as c:
                c.execute("UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id=?", (origin,))
                c.commit()
            again = client.post(f'/v1/sessions/{sid}/exam-simulations/bound-answer', json=payload)
            assert again.status_code == 200 and again.json()['attempt'] == replay.json()['attempt']
    assert not gateway.requests


@pytest.mark.parametrize('cancel_before_write', [False, True])
def test_vocabulary_http_gate_and_scored_receipt_priority(vocabulary_environment, cancel_before_write):
    settings, db, sid = vocabulary_environment
    gateway = Gateway({})
    gateway.configured = False
    with TestClient(create_app(settings, model_gateway=gateway), raise_server_exceptions=False) as client:
        card = next_prompt(client, sid)
        speech = turn(client, sid, 'library', 'authorized-vocabulary-answer')
        origin = speech['agent_turn_id']
        payload = {key: card[key] for key in ('prompt_session_id', 'card_version')}
        payload.update(answer='library', client_action_id='vocabulary-stable-answer', authorized_turn_id=origin)
        if cancel_before_write:
            with db.connect() as c:
                c.execute("UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id=?", (origin,))
                c.commit()
        result = client.post(f'/v1/sessions/{sid}/pet-vocabulary/prompt/answer', json=payload)
        assert result.status_code == (409 if cancel_before_write else 200), result.text
        if not cancel_before_write:
            with db.connect() as c:
                c.execute("UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id=?", (origin,))
                c.commit()
            later = turn(client, sid, '今天计划做到哪了', 'vocabulary-retry-context')
            again = client.post(f'/v1/sessions/{sid}/pet-vocabulary/prompt/answer',
                                json={**payload, 'authorized_turn_id': later['agent_turn_id']})
            assert again.status_code == 200 and again.json()['attempt'] == result.json()['attempt']
            with db.connect() as c:
                assert c.execute('SELECT authorized_turn_id FROM pet_vocabulary_prompt_session WHERE prompt_session_id=?',
                                 (card['prompt_session_id'],)).fetchone()[0] == origin
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM pet_vocabulary_attempt').fetchone()[0] == (0 if cancel_before_write else 1)
    assert not gateway.requests
