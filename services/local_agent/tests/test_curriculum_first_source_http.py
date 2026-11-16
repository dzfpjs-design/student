"""Synthetic source-first journey. No production content or remote model calls."""
import json

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.knowledge_base import KnowledgeBaseService
from home_study_agent.curriculum_supply import CurriculumSupplyService
from test_curriculum_supply import prepared
from test_pet_reviewed_source_actual_http import NoCloud


CHILD = 'child-junior-3'
NODE = 'node-j3-math-quadratic-formula'
BASE = f'/api/parent/children/{CHILD}/curriculum-supply'


def first_source_fixture(tmp_path, *, with_parallel_original=False,with_spoken_tuple=False):
    db, _, _ = prepared(tmp_path)
    from test_school_oral_catalog import without_authored_tasks
    without_authored_tasks(db)
    original = dict(schema='curriculum_objective_source_v1', subject='数学', grade='初三',
                    knowledge_node_id=NODE, assessment_mode='objective', answer_type='text',
                    prompt='用求根公式解 x²-5x+6=0，按从小到大用逗号分隔写出两个根。',
                    expected_answer=dict(local_rule='local_exact_v1', exact_answers=['2,3']))
    originals = [original]
    if with_parallel_original:
        originals.append({**original, 'prompt':'用求根公式解 x²-7x+12=0，按从小到大用逗号分隔写出两个根。',
            'expected_answer':dict(local_rule='local_exact_v1',exact_answers=['3,4'])})
    if with_spoken_tuple:
        for value in originals:
            value['expected_answer'].update(answer_format='reviewed_spoken_numeric_tuple_v1',numeric_kind='integer',tuple_size=2)
    source = KnowledgeBaseService(db).import_source(CHILD, dict(
        source_type='school_material', title='隔离合成求根公式原题，非家庭教材',
        subject='数学', grade_band='初三', version_label='合成2026',
        source_ref='isolated:first-source:quadratic', license_status='family_owned_index',
        parent_reviewed=True, content_text='\n'.join(json.dumps(value, ensure_ascii=False) for value in originals)))
    with db.connect() as c:
        c.execute("UPDATE curriculum_standard_requirement SET stage_scope='第四学段', requirement_text='理解一元二次方程求根公式并求出方程的根。' WHERE requirement_id='req'")
        c.execute("UPDATE curriculum_standard_page SET content_text='理解一元二次方程求根公式并求出方程的根。' WHERE source_id='std'")
        c.commit()
        excerpt = c.execute('SELECT excerpt_id FROM knowledge_excerpt WHERE source_id=? ORDER BY sequence', (source['source_id'],)).fetchone()[0]
        assert c.execute('SELECT COUNT(*) FROM diagnostic_item WHERE knowledge_node_id=?', (NODE,)).fetchone()[0] == 0
    CurriculumSupplyService(db).select_curriculum(CHILD, dict(grade='初三', subject='数学',
        edition='合成2026', semester='上学期', standard_scope='第四学段', textbook_source_id=source['source_id']),
        actor='parent', action_id='first-select', expected_version=0)
    spec = dict(knowledge_node_id=NODE, excerpt_id=excerpt, requirement_id='req',
                standard_page=12, standard_quote='理解一元二次方程求根公式并求出方程的根。',
                explanation='合成完整答案核对：代入求根公式得到两个根2和3。', difficulty=1)
    return db, source, spec, original


def app_client(tmp_path, db):
    settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False,
                        session_token='isolated-first-source')
    return TestClient(create_app(settings, model_gateway=NoCloud()), client=('127.0.0.1', 41200),
                      headers={'X-HSC-Session': settings.session_token})


def auth(client, *, restart=False):
    result = client.post('/v1/parent/unlock' if restart else '/v1/parent/setup', json={'pin': '7391'})
    assert result.status_code == 200, result.text
    client.headers['X-HSC-Parent'] = result.json()['parent_token']


def test_first_source_is_reachable_without_an_invented_seed(tmp_path):
    db, source, spec, original = first_source_fixture(tmp_path)
    with app_client(tmp_path, db) as client:
        auth(client)
        response = client.get(BASE + f"/sources/{source['source_id']}/options")
        assert response.status_code == 200, response.text
        options = response.json()
        assert not [v for v in options['seeds'] if v['title'] == '一元二次方程求根公式']
        assert any(v['knowledge_node_id'] == NODE and v['excerpt_id'] == spec['excerpt_id']
                   for v in options.get('first_sources', [])), options


def publish(client, spec):
    body = dict(action_id='first-draft', expected_version=0, expected_scope_version=1, source=spec)
    response = client.post(BASE+'/first-sources', json=body)
    assert response.status_code == 200, response.text
    draft = response.json()
    assert draft['payload']['seed_item_id'] is None
    assert draft['diagnostic_item_id'] is None and not draft['eligible']
    assert client.post(BASE+'/first-sources', json=body).json()['candidate_id'] == draft['candidate_id']
    cid = draft['candidate_id']
    for version, operation in enumerate(('validate','review','publish'),1):
        command = dict(action_id='first-'+operation, expected_version=version)
        if operation == 'review':
            command.update(decision='approve', note='核对合成原题、完整参考答案、求根公式节点和对应学段课标，不认证迁移。')
        response = client.post(BASE+f'/{cid}/{operation}', json=command)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result['state'] == {'validate':'validated','review':'reviewed','publish':'published'}[operation], result
    assert result['eligible']
    return result


def test_actual_first_source_publish_original_prompt_score_restart_withdraw(tmp_path):
    from home_study_agent.learning_obligations import qualified_exact_score
    db, source, spec, original = first_source_fixture(tmp_path)
    with app_client(tmp_path, db) as client:
        auth(client)
        result = publish(client, spec)
        cid, item = result['candidate_id'], result['diagnostic_item_id']
        assert client.get(BASE+'/audit').json()['eligible_curriculum_items'] == 1
        sid = client.post('/v1/sessions', json=dict(child_id=CHILD, client_action_id='first-session')).json()['session_id']
        path = f'/v1/sessions/{sid}/diagnostic-prompt'
        response = client.post(path+'/prepare',json=dict(track='school',preferred_item_id=item,client_action_id='first-prompt'))
        assert response.status_code == 200, response.text
        prompt = response.json()['prompt']
        assert prompt['item']['prompt'] == original['prompt']
        answer = dict(prompt_id=prompt['prompt_id'],item_version=prompt['item_version'],answer='2,3',client_action_id='first-answer')
        response = client.post(path+'/answer',json=answer)
        assert response.status_code == 200, response.text
        assert response.json()['attempt']['score_status'] == 'verified_local'
        assert response.json()['attempt']['correctness'] == 1
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM specialized_supply_candidate WHERE seed_item_id=?',(item,)).fetchone()[0] == 0
            assert qualified_exact_score(c,CHILD,item,'2,3')[1] == 1
    with app_client(tmp_path, db) as client:
        auth(client,restart=True)
        assert client.get(BASE+f'/{cid}').json()['eligible']
        assert client.get(BASE.replace(CHILD,'child-primary-4')+f'/{cid}').status_code == 404
        response = client.post(BASE+f'/{cid}/withdraw',json=dict(action_id='first-withdraw',expected_version=4,note='撤回原题'))
        assert response.status_code == 200, response.text
        assert not response.json()['eligible']
        old = client.post(BASE+f'/{cid}/publish',json=dict(action_id='first-publish',expected_version=3))
        assert old.json()['state'] == 'withdrawn' and not old.json()['eligible']
        with db.connect() as c:
            assert qualified_exact_score(c,CHILD,item,'2,3') is None
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 1


def test_first_root_then_existing_seeded_original_flow_no_same_question_or_transfer_claim(tmp_path):
    from home_study_agent.local_supply import validate_item, LocalSupplyError
    from home_study_agent.goal_question_inventory import practice_item
    from datetime import datetime, timezone
    db, source, spec, _ = first_source_fixture(tmp_path,with_parallel_original=True)
    with app_client(tmp_path, db) as client:
        auth(client)
        first = publish(client,spec)
        options = client.get(BASE+f"/sources/{source['source_id']}/options").json()
        assert not options['first_sources']
        assert any(row['seed_item_id'] == first['diagnostic_item_id'] for row in options['seeds'])
        second = next(row for row in options['excerpts'] if row['excerpt_id'] != spec['excerpt_id'])
        seeded_spec = {key:value for key,value in spec.items() if key != 'knowledge_node_id'}
        seeded_spec.update(seed_item_id=first['diagnostic_item_id'],excerpt_id=second['excerpt_id'],
                           stage='parallel',validator='curriculum_objective_exact_source_v1')
        response = client.post(BASE+'/generate',json=dict(action_id='second-d',expected_version=0,
                                                         expected_scope_version=1,source=seeded_spec))
        assert response.status_code == 200, response.text
        draft = response.json()
        assert draft['payload']['prompt'] != first['payload']['prompt']
        for version,kind in enumerate(('validate','review','publish'),1):
            values = dict(action_id='second-'+kind,expected_version=version)
            if kind == 'review':
                values.update(decision='approve',note='第二份原题完整核对，仅平行练习，不认证迁移。')
            response = client.post(BASE+f"/{draft['candidate_id']}/{kind}",json=values)
            assert response.status_code == 200, response.text
        published = response.json()
        assert published['eligible'] and published['diagnostic_item_id'] != first['diagnostic_item_id']
        assert published['stage_review_context'] is None or published['payload']['stage'] == 'parallel'
        with db.connect() as c:
            assert practice_item(c,CHILD,first['diagnostic_item_id'],datetime.now(timezone.utc))
            assert practice_item(c,CHILD,published['diagnostic_item_id'],datetime.now(timezone.utc))
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='started'").fetchone()[0] == 0
        revoked = client.post(BASE+f"/{first['candidate_id']}/withdraw",json=dict(action_id='withdraw-root',expected_version=4,note='根来源撤回'))
        assert revoked.status_code == 200
        assert not client.get(BASE+f"/{draft['candidate_id']}").json()['eligible']
        with db.connect() as c, pytest.raises(LocalSupplyError):
            validate_item(c,CHILD,published['diagnostic_item_id'])
