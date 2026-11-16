"""A reviewed closed question must remain reachable beside authored oral tasks.

Synthetic material and real HTTP/SQLite; not corpus approval or child scoring
calibration. Keep the existing open tasks qualified throughout the journey.
"""
import json
from datetime import datetime, timezone

from home_study_agent.curriculum_supply import CurriculumSupplyService
from home_study_agent.goal_question_inventory import practice_item
from home_study_agent.knowledge_base import KnowledgeBaseService
from home_study_agent.local_supply import validate_item
from test_curriculum_first_source_http import BASE, CHILD, app_client, auth, publish
from test_curriculum_supply import prepared


def test_reviewed_closed_first_source_is_not_hidden_by_incompatible_open_seed(tmp_path):
    db, _, _ = prepared(tmp_path)
    node = 'node-j3-chinese-reading-evidence'
    original = dict(schema='curriculum_objective_source_v1', subject='语文', grade='初三',
        knowledge_node_id=node, assessment_mode='objective', answer_type='text',
        prompt='读自编短句：“小禾把画从窗台搬到书架上。”画最后放在哪里？只回答地点。',
        expected_answer=dict(local_rule='local_exact_v1', exact_answers=['书架上', '书架']))
    quote = '结合文本具体信息，准确提取和理解阅读材料中的事实。'
    with db.connect() as c:
        # This installation has oral tasks but no eligible closed-answer seed
        # for the requested node. Never change the authored task or its rubric.
        c.execute("UPDATE diagnostic_item SET status='retired' WHERE knowledge_node_id=? AND answer_type='text'", (node,))
        c.execute("UPDATE curriculum_standard_source SET subject_title='语文' WHERE source_id='std'")
        c.execute("UPDATE curriculum_standard_requirement SET subject_title='语文', stage_scope='第四学段', requirement_text=? WHERE requirement_id='req'", (quote,))
        c.execute("UPDATE curriculum_standard_page SET content_text=? WHERE source_id='std'", (quote,))
        oral = [row[0] for row in c.execute("SELECT diagnostic_item_id FROM diagnostic_item WHERE knowledge_node_id=? AND answer_type='open_response' AND status='active'", (node,))]
        assert oral
        before = [dict(validate_item(c, CHILD, item)) for item in oral]
        c.commit()
    source = KnowledgeBaseService(db).import_source(CHILD, dict(source_type='school_material',
        title='隔离合成阅读事实题', subject='语文', grade_band='初三', version_label='合成2026',
        source_ref='isolated:closed-beside-open', license_status='family_owned_index',
        parent_reviewed=True, content_text=json.dumps(original, ensure_ascii=False)))
    CurriculumSupplyService(db).select_curriculum(CHILD, dict(grade='初三', subject='语文',
        edition='合成2026', semester='上学期', standard_scope='第四学段', textbook_source_id=source['source_id']),
        actor='parent', action_id='closed-beside-open-select', expected_version=0)
    with app_client(tmp_path, db) as client:
        auth(client)
        response = client.get(BASE + f"/sources/{source['source_id']}/options")
        assert response.status_code == 200, response.text
        options = response.json()
        assert not [seed for seed in options['seeds'] if seed['seed_item_id'] in oral]
        first = [row for row in options['first_sources'] if row['knowledge_node_id'] == node]
        assert len(first) == 1, 'an incompatible oral seed must not block the closed-answer route'
        published = publish(client, dict(knowledge_node_id=node, excerpt_id=first[0]['excerpt_id'],
            requirement_id='req', standard_page=12, standard_quote=quote,
            explanation='原文直接给出最后的位置是书架上；不据此评价完整阅读能力。', difficulty=1))
        cid, item = published['candidate_id'], published['diagnostic_item_id']
        with db.connect() as c:
            assert [dict(validate_item(c, CHILD, value)) for value in oral] == before
            assert practice_item(c, CHILD, item, datetime.now(timezone.utc)) is not None
        # A compatible, reviewed closed seed now exists: use the ordinary
        # seeded route for further questions, rather than duplicate first roots.
        current = client.get(BASE + f"/sources/{source['source_id']}/options").json()
        assert not [row for row in current['first_sources'] if row['knowledge_node_id'] == node]
        assert any(seed['seed_item_id'] == item for seed in current['seeds'])
        sid = client.post('/v1/sessions', json=dict(child_id=CHILD, client_action_id='closed-beside-open-session')).json()['session_id']
        path = f'/v1/sessions/{sid}/diagnostic-prompt'
        response = client.post(path+'/prepare', json=dict(track='school', preferred_item_id=item,
            client_action_id='closed-beside-open-prompt'))
        assert response.status_code == 200, response.text
        prompt = response.json()['prompt']
        answer = client.post(path+'/answer', json=dict(prompt_id=prompt['prompt_id'],
            item_version=prompt['item_version'], answer='书架上', client_action_id='closed-beside-open-answer'))
        assert answer.status_code == 200, answer.text
        assert answer.json()['attempt']['score_status'] == 'verified_local'
        assert answer.json()['attempt']['correctness'] == 1
    with app_client(tmp_path, db) as restarted:
        auth(restarted, restart=True)
        assert restarted.get(BASE+f'/{cid}').json()['eligible']
        assert restarted.get(BASE.replace(CHILD, 'child-primary-4')+f'/{cid}').status_code == 404
        with db.connect() as c:
            assert [dict(validate_item(c, CHILD, value)) for value in oral] == before
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 1


def test_mixed_textbook_keeps_closed_and_open_publication_routes(tmp_path):
    from test_open_response_supply_http import prepared as open_prepared
    from home_study_agent import curriculum_objective_source as objective
    from home_study_agent import open_response_source as opened
    db, _, spec, oral, _ = open_prepared(tmp_path, '数学')
    closed = dict(schema=objective.SCHEMA, subject='数学', grade='初三',
        knowledge_node_id=oral['knowledge_node_id'], assessment_mode='objective', answer_type='text',
        prompt='方程 x²+4x+5=0 的判别式是多少？只回答数值。',
        expected_answer=dict(local_rule='local_exact_v1', exact_answers=['-4']))
    source = KnowledgeBaseService(db).import_source(CHILD, dict(source_type='school_material',
        title='隔离合成同一知识点两种题型', subject='数学', grade_band='初三',
        version_label='isolated-open-2026', source_ref='isolated:mixed-closed-open',
        license_status='family_owned_index', parent_reviewed=True,
        content_text='\n'.join(json.dumps(value, ensure_ascii=False) for value in (oral, closed))))
    CurriculumSupplyService(db).select_curriculum(CHILD, dict(grade='初三', subject='数学',
        edition='isolated-open-2026', semester='上学期', standard_scope='第四学段',
        textbook_source_id=source['source_id']), actor='parent', action_id='mixed-select', expected_version=1)
    ids = []
    with app_client(tmp_path, db) as client:
        auth(client)
        response = client.get(BASE+f"/sources/{source['source_id']}/options")
        assert response.status_code == 200, response.text
        options = response.json()
        routes = [row for row in options['seeds'] if row['seed_item_id'] == spec['seed_item_id']]
        assert {row['validator'] for row in routes} == {objective.VALIDATOR, opened.VALIDATOR}, \
            'one source schema must not hide another supported question type'
        for validator, original in ((objective.VALIDATOR, closed), (opened.VALIDATOR, oral)):
            excerpt = next(row for row in options['excerpts'] if json.loads(row['text']) == original)
            body = dict(action_id='mixed-draft-'+validator, expected_version=0, expected_scope_version=2,
                source={**spec, 'excerpt_id': excerpt['excerpt_id'], 'validator': validator})
            response = client.post(BASE+'/generate', json=body)
            assert response.status_code == 200, response.text
            cid = response.json()['candidate_id']
            for version, operation in enumerate(('validate', 'review', 'publish'), 1):
                command = dict(action_id='mixed-'+operation+'-'+validator, expected_version=version)
                if operation == 'review':
                    command.update(decision='approve', note='隔离材料核对原题和对应参考合同，不认证整体能力。')
                response = client.post(BASE+f'/{cid}/{operation}', json=command)
                assert response.status_code == 200, response.text
            assert response.json()['eligible']
            ids.append(cid)
    with app_client(tmp_path, db) as restarted:
        auth(restarted, restart=True)
        for cid in ids:
            assert restarted.get(BASE+f'/{cid}').json()['eligible']
            assert restarted.get(BASE.replace(CHILD, 'child-primary-4')+f'/{cid}').status_code == 404
