"""Isolated actual parent publication and original oral task consumers, no network."""
import json

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.learning_state import LearningStateService
from home_study_agent.local_supply import validate_item, item_snapshot, digest, score_local_item, LocalSupplyError
from home_study_agent.repository import StudyRepository
from test_pet_typed_source_import import add_standard, values, CHILD, OTHER
from test_pet_reviewed_source_actual_http import NoCloud

SCHEMA = 'pet_speaking_source_v1'
VALIDATOR = 'pet_speaking_rubric_source_v1'
BASE = f'/api/parent/children/{CHILD}/curriculum-supply'
FAMILIES = {
    'topic_response': 'diag-pet-speaking-2',
    'picture_description': 'diag-pet-speaking-1',
    'read_aloud': 'diag-pet-read-aloud-1',
    'interaction': 'diag-pet-speaking-interaction-baseline',
    'extended_turn': 'diag-pet-speaking-extended-baseline',
    'daily_dialogue': 'diag-pet-daily-dialogue-1',
    'retell': 'diag-pet-daily-retell-1',
}


def setup(tmp_path):
    db = Database(tmp_path / 'oral.db')
    db.initialize()
    StudyRepository(db).seed_family()
    LearningStateService(db).seed_catalog()
    add_standard(db)
    settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False,
                        session_token='isolated-oral-source', data_encryption_key=b'T' * 32)
    return db, settings


def packet(db, family):
    with db.connect() as c:
        seed = validate_item(c, CHILD, FAMILIES[family])
    prompts = {
        'topic_response': 'Talk about an outdoor place you want to visit. Say where it is, what you would do, and why.',
        'picture_description': 'Look at the study-room picture. Describe two actions and compare what the people are doing.',
        'read_aloud': 'Read the entire passage aloud in your own natural voice. Keep the beginning, action and ending.',
        'interaction': 'Plan a class visit with me. Suggest a destination and transport, respond to my suggestion and ask a question.',
        'extended_turn': 'Talk for about half a minute about a hobby you would like to try. Explain a reason and a practical first step.',
        'daily_dialogue': 'I ask: Shall we visit the school library tomorrow? Reply, suggest a time, explain a reason and ask a question.',
        'retell': 'Read the whole story below and retell it. Explain who was involved, the problem, and how it was resolved.',
    }
    original = dict(schema=SCHEMA, subject='英语', grade='四年级', knowledge_node_id=seed['knowledge_node_id'],
        answer_type=seed['answer_type'], assessment_mode='pet_reference_rubric', task_family=family,
        prompt=prompts[family], task_points=['Address the complete original task', 'Include supporting details'],
        stimulus_text=None, stimulus_binding=None)
    if family in {'read_aloud', 'retell'}:
        original['stimulus_text'] = 'Ada visited the school garden on Tuesday. She found a dry plant and asked her teacher for help. Together they watered it and moved it into the shade.'
    if family == 'picture_description':
        original['stimulus_binding'] = dict(diagnostic_item_id=seed['diagnostic_item_id'],
            source_version=digest(item_snapshot(seed)), stimulus_asset=seed['stimulus_asset'])
    return original


def post(client, path, body, status=200):
    response = client.post(path, json=body)
    assert response.status_code == status, response.text[:1000]
    return response.json()


def authored(client, db, family):
    original = packet(db, family)
    source = post(client, f'/v1/parent/children/{CHILD}/knowledge-sources',
        {**values(json.dumps(original, ensure_ascii=False)), 'source_ref': 'isolated:oral-' + family})
    detail = client.get(BASE + f"/sources/{source['source_id']}/options")
    assert detail.status_code == 200, detail.text
    options = detail.json()
    assert options['local_only'] and not options['cloud_transmission_allowed']
    assert json.loads(options['excerpts'][0]['text']) == original
    seed = next((s for s in options['seeds'] if s['seed_item_id'] == FAMILIES[family]), None)
    assert seed and seed['validator'] == VALIDATOR, options['seeds']
    standard = next(s for s in options['standards'] if s['requirement_id'] == 'c03-req')
    assert standard['available']
    selected = post(client, BASE + '/select', dict(action_id='select', expected_version=0, scope=dict(
        grade='四年级', subject='英语', edition='isolated-c03-v1', semester='上学期',
        standard_scope=standard['stage_scope'], textbook_source_id=source['source_id'])))
    spec = dict(seed_item_id=FAMILIES[family], stage='parallel', excerpt_id=options['excerpts'][0]['excerpt_id'],
        requirement_id='c03-req', standard_page=standard['page_number'], standard_quote=standard['quote'],
        validator=VALIDATOR, difficulty=1, explanation='仅核对原题和参考任务点，不认证口语能力或发音。')
    body = dict(action_id='generate', expected_version=0, expected_scope_version=selected['version'], source=spec)
    draft = post(client, BASE + '/generate', body)
    assert draft['state'] == 'draft', draft['blockers']
    assert draft['payload']['pet_oral'] == original
    assert post(client, BASE + '/generate', body)['candidate_id'] == draft['candidate_id']
    return original, source, draft


@pytest.mark.parametrize('family', FAMILIES)
def test_real_parent_oral_publication_exact_prompt_restart_withdraw(tmp_path, family):
    db, settings = setup(tmp_path)
    headers = {'X-HSC-Session': settings.session_token}
    with TestClient(create_app(settings, model_gateway=NoCloud()), headers=headers, client=('127.0.0.1', 41900)) as client:
        assert client.get(BASE + '/options').status_code == 401
        client.headers['X-HSC-Parent'] = post(client, '/v1/parent/setup', {'pin': '7391'})['parent_token']
        original, source, draft = authored(client, db, family)
        cid = draft['candidate_id']
        assert client.get(BASE.replace(CHILD, OTHER) + '/' + cid).status_code == 404
        for version, operation in enumerate(('validate', 'review', 'publish'), 1):
            body = dict(action_id=operation, expected_version=version)
            if operation == 'review':
                body.update(decision='approve', note='Synthetic original task and complete material reviewed, no learner score')
            result = post(client, BASE + f'/{cid}/{operation}', body)
        assert result['eligible'], result['blockers']
        assert result['review_packet']['pet_oral'] == original
        item_id = result['diagnostic_item_id']
        with db.connect() as c:
            item = validate_item(c, CHILD, item_id)
            assert item['answer_type'] == original['answer_type']
            assert item['assessment_mode'] == 'pet_reference_rubric'
            assert item['prompt'] == original['prompt']
            assert (item['stimulus_text'] or None) == original['stimulus_text']
            assert (item['stimulus_asset'] or None) == (original['stimulus_binding'] or {}).get('stimulus_asset')
            from home_study_agent.learning_obligations import _source_question, question_identity
            frozen_question = _source_question(c, item)
            assert question_identity(frozen_question) == question_identity(item)
            mutated_item = {**item, 'stimulus_text': 'Not the original passage',
                            'stimulus_asset': 'not-the-original-picture'}
            assert _source_question(c, mutated_item) == frozen_question
            assert json.loads(item['expected_answer_json'])['task_points'] == original['task_points']
            assert score_local_item(item, ' '.join(original['task_points'])) is None
            assert 'local_rule' not in json.loads(item['expected_answer_json'])
            old = validate_item(c, CHILD, FAMILIES[family])
            assert old['prompt'] != item['prompt'] and old['answer_type'] == item['answer_type']
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 0
            assert c.execute('SELECT count(*) FROM model_call').fetchone()[0] == 0
        exported = client.get(BASE + f'/{cid}/export')
        assert exported.status_code == 200 and exported.json()['payload']['pet_oral'] == original
    with TestClient(create_app(settings, model_gateway=NoCloud()), headers=headers, client=('127.0.0.1', 41901)) as client:
        client.headers['X-HSC-Parent'] = post(client, '/v1/parent/unlock', {'pin': '7391'})['parent_token']
        assert client.get(BASE + '/' + cid).json()['eligible']
        withdrawn = post(client, BASE + f'/{cid}/withdraw', dict(action_id='withdraw', expected_version=4, note='Source withdrawn'))
        assert not withdrawn['eligible']
        replay = post(client, BASE + f'/{cid}/publish', dict(action_id='publish', expected_version=3))
        assert replay['state'] == 'withdrawn' and not replay['eligible']
        with db.connect() as c:
            with pytest.raises(LocalSupplyError):
                validate_item(c, CHILD, item_id)
            assert validate_item(c, CHILD, FAMILIES[family])['status'] == 'active'
