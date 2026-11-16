"""Actual host author/review/publish, typed answer and withdrawal; isolated sources."""
import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.learning_obligations import qualified_exact_score
from test_curriculum_objective_source import setup_source
from test_pet_reviewed_source_actual_http import NoCloud


@pytest.mark.parametrize('subject', ['数学', '英语', '语文'])
def test_actual_host_options_publish_typed_score_restart_and_withdraw(tmp_path, subject):
    db, _, spec, original, child = setup_source(tmp_path, subject)
    settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False,
                        session_token='isolated-objective-source')
    headers = {'X-HSC-Session': settings.session_token}
    base = f'/api/parent/children/{child}/curriculum-supply'
    with TestClient(create_app(settings, model_gateway=NoCloud()), client=('127.0.0.1', 41200), headers=headers) as client:
        token = client.post('/v1/parent/setup', json={'pin': '7391'}).json()['parent_token']
        body = {'action_id': 'draft', 'expected_version': 0, 'expected_scope_version': 1, 'source': spec}
        assert client.post(base + '/generate', json=body).status_code == 401
        client.headers['X-HSC-Parent'] = token
        source = client.get(base + '/options').json()['sources'][0]
        options = client.get(base + f"/sources/{source['source_id']}/options").json()
        assert any(v['seed_item_id'] == spec['seed_item_id'] and v['validator'] == spec['validator'] for v in options['seeds'])
        response = client.post(base + '/generate', json=body)
        assert response.status_code == 200, response.text
        draft = response.json()
        assert draft['payload']['objective_source'] == original
        cid = draft['candidate_id']
        assert client.post(base + '/generate', json=body).json()['candidate_id'] == cid
        for version, operation in enumerate(('validate', 'review', 'publish'), 1):
            payload = {'expected_version': version, 'action_id': operation}
            if operation == 'review':
                detail = client.get(base + f'/{cid}').json()
                context = detail['stage_review_context']
                payload.update(decision='approve', note='核对原题、完整答案和独立来源', stage_review={
                    'schema': context['schema'], 'stage': 'near_transfer', 'binding_version': context['binding_version'],
                    'source_quote': context['source_prompt'], 'candidate_quote': context['candidate_prompt'],
                    'same_skill_rationale': '隔离人工审核：两题检验同一明确知识节点。',
                    'changed_context_rationale': '隔离人工审核：题目使用不同的内容与语境。'})
            response = client.post(base + f'/{cid}/{operation}', json=payload)
            assert response.status_code == 200, response.text
            result = response.json()
        assert result['eligible']
        item_id = result['diagnostic_item_id']
        session = client.post('/v1/sessions', json={'child_id': child, 'client_action_id': 'objective-session'})
        assert session.status_code == 201, session.text
        sid = session.json()['session_id']
        path = f'/v1/sessions/{sid}/diagnostic-prompt'
        response = client.post(path + '/prepare', json={'track': 'school', 'preferred_item_id': item_id, 'client_action_id': 'objective-prompt'})
        assert response.status_code == 200, response.text
        prompt = response.json()['prompt']
        answer = {'prompt_id': prompt['prompt_id'], 'item_version': prompt['item_version'],
                  'answer': original['expected_answer']['exact_answers'][0], 'client_action_id': 'objective-answer'}
        response = client.post(path + '/answer', json=answer)
        assert response.status_code == 200, response.text
        attempt = response.json()['attempt']
        assert attempt['score_status'] == 'verified_local'
        assert attempt['correctness'] == 1
        assert client.post(path + '/answer', json=answer).json()['attempt']['attempt_id'] == attempt['attempt_id']
    with TestClient(create_app(settings, model_gateway=NoCloud()), client=('127.0.0.1', 41201), headers=headers) as client:
        client.headers['X-HSC-Parent'] = client.post('/v1/parent/unlock', json={'pin': '7391'}).json()['parent_token']
        assert client.get(base + f'/{cid}').json()['eligible']
        response = client.post(base + f'/{cid}/withdraw', json={'action_id': 'withdraw', 'expected_version': 4, 'note': '原始材料撤回'})
        assert response.status_code == 200, response.text
        with db.connect() as c:
            assert qualified_exact_score(c, child, item_id, answer['answer']) is None
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 1
