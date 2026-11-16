"""Legacy registry aliases must obey the same original-answer boundary."""
from test_app import client_for, advance_to_study
from test_cloud_first_agent import Planner
import pytest
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.agent_orchestrator import AgentOrchestrator
from test_mistake_pending_answer_http import waiting, hear, execute
from test_mistake_voice_journey import settings, focus


@pytest.mark.parametrize('quote,scored', [
    ('错题答案是A', True),
    ('这题答案是A', True),
    ('错题答案是A，请不要提交。', False),
])
def test_new_alias_uses_frozen_dialogue_and_original_utterance(tmp_path, quote, scored):
    with TestClient(create_app(settings(tmp_path))) as client:
        sid, db = waiting(client)
        original = focus(client, sid)['answer_binding']
        turn = hear(client, sid, quote, 'canonical-mistake-answer')
        assert turn['action']['type'] == ('continue_mistake_dialogue' if scored else 'none')
        if scored:
            assert turn['action']['utterance'] == quote
            assert turn['action']['mistake_answer_binding'] == original
        assert turn['action']['local_only'] and not turn['action']['cloud_transmission_allowed']
        execute(client, turn)
    with TestClient(create_app(settings(tmp_path))) as client:
        replay = hear(client, sid, quote, 'canonical-mistake-answer')
        execute(client, replay)
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM mistake_attempt').fetchone()[0] == int(scored)


def test_new_alias_without_displayed_focus_opens_review_without_grading(tmp_path):
    with client_for(tmp_path, agent_runtime_enabled=True) as client:
        sid, _ = advance_to_study(client, child_id='child-primary-4', action_prefix='no-mistake-focus')
        turn = hear(client, sid, '错题答案是42', 'no-focus-answer')
        assert turn['action']['type'] == 'open_mistake_book'
        assert '先' in turn['response_text'] and '答案' in turn['response_text']
        execute(client, turn)
        with client.app.state.repository.database.connect() as c:
            assert c.execute('SELECT count(*) FROM mistake_attempt').fetchone()[0] == 0


def test_persisted_legacy_step_real_dispatch_only_restores_focus(tmp_path, monkeypatch):
    quote = '错题答案是A，请不要提交。'
    with TestClient(create_app(settings(tmp_path))) as client:
        sid, db = waiting(client)
        # Simulate the previous binary's adapter, not fabricated step rows.
        # Parsing, persistence, admission and execution remain real.
        with monkeypatch.context() as old_binary:
            old_binary.setattr(AgentOrchestrator, '_canonical_mistake_submission', staticmethod(
                lambda *a, **kw: ('submit_mistake_answer', '旧版本待执行回复',
                                  {'type': 'submit_mistake_answer', 'answer': 'A'})))
            turn = hear(client, sid, quote, 'persisted-legacy-step')
        assert turn['runtime_capability'] == 'mistake.submit_response'
    with TestClient(create_app(settings(tmp_path))) as client:
        result = execute(client, turn)
        receipt = result['execution']
        assert receipt['result']['legacy_answer_scored'] is False
        assert receipt['result']['status'] == 'requires_new_answer'
        assert receipt['requires_child_reply'] is True
        assert receipt['evidence'] == ['mistake_focus_restored']
        execute(client, turn)
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM mistake_attempt').fetchone()[0] == 0
        fresh = hear(client, sid, '错题答案是A', 'fresh-after-legacy-restore')
        assert fresh['action']['type'] == 'continue_mistake_dialogue'
        execute(client, fresh)
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM mistake_attempt').fetchone()[0] == 1


def test_cloud_legacy_arguments_cannot_replace_original_answer(tmp_path):
    quote = '帮我记录刚才说的错题回答'
    gateway = Planner('mistake.submit_response', {'answer': 'A'}, quote, role='answer', objective_type='learning')
    with client_for(tmp_path, model_gateway=gateway, agent_runtime_enabled=True) as client:
        sid, _ = advance_to_study(client, child_id='child-primary-4', action_prefix='cloud-legacy')
        turn = client.post(f'/v1/sessions/{sid}/agent-turn', json={
            'transcript': quote, 'cloud_text_allowed': True, 'client_action_id': 'cloud-legacy-answer'})
        assert turn.status_code == 200, turn.text
        assert turn.json()['action']['type'] == 'open_mistake_book', turn.json()
        assert len(gateway.requests) == 1
        execute(client, turn.json())
        with client.app.state.repository.database.connect() as c:
            assert c.execute('SELECT count(*) FROM mistake_attempt').fetchone()[0] == 0


def test_legacy_mistake_capability_cannot_score_same_turn_withdrawal(tmp_path):
    quote = '错题答案是42，请不要提交。'
    gateway = Planner('mistake.submit_response', {'answer': '42'}, quote, role='answer', objective_type='learning')
    with client_for(tmp_path, model_gateway=gateway, agent_runtime_enabled=True) as client:
        sid, _ = advance_to_study(client, child_id='child-primary-4', action_prefix='legacy-answer-gate')
        wrong = client.post(f'/v1/sessions/{sid}/diagnostics/answer', json={
            'diagnostic_item_id': 'diag-p4-arithmetic-closed-1', 'answer': '12', 'client_action_id': 'legacy-wrong-answer'})
        assert wrong.status_code == 200, wrong.text
        mistake = wrong.json()['mistake']['mistake_id']
        common = {'child_id': 'child-primary-4'}
        assert client.post(f'/v1/mistakes/{mistake}/confirm', json={**common, 'decision': 'confirm',
            'client_action_id': 'legacy-confirm-case'}).status_code == 200
        assert client.post(f'/v1/mistakes/{mistake}/diagnose', json={**common, 'error_type': 'execution_error',
            'explanation': 'Synthetic explicit diagnosis fixture', 'confidence': .8,
            'client_action_id': 'legacy-diagnosis-fixture'}).status_code == 200
        remediation = client.post(f'/v1/mistakes/{mistake}/remediation-steps', json={**common,
            'step_type': 'problem_restatement', 'outcome': 'completed', 'notes': 'Synthetic completed work',
            'client_action_id': 'legacy-remediation-fixture'})
        assert remediation.status_code == 200, remediation.text
        turn = client.post(f'/v1/sessions/{sid}/agent-turn', json={
            'transcript': quote, 'cloud_text_allowed': True, 'client_action_id': 'legacy-withdraw-answer'})
        assert turn.status_code == 200, turn.text
        if turn.json().get('runtime_capability'):
            result = client.post(f"/v1/agent-turns/{turn.json()['agent_turn_id']}/execute")
            assert result.status_code in (200, 409), result.text
        with client.app.state.repository.database.connect() as c:
            assert c.execute('SELECT count(*) FROM mistake_attempt').fetchone()[0] == 0
