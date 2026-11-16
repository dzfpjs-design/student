"""Original review -> published open task -> actual saved response and evaluation.

Disposable databases, authored sources, controlled OCR/ASR/model boundaries.
"""
from contextlib import contextmanager
import json

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.writing_response_origin import WritingOriginalService
from home_study_agent.local_supply import validate_item
from home_study_agent.open_response_profile import profile_for_item
import test_document_goal_http as original
from test_document_revisit import next_day, offer, say
from test_primary_open_response_http import prepared, publish, grant_question, CHILD, CASES
from test_voice_runtime_binding import capture, interpret


@contextmanager
def opened(tmp_path, monkeypatch, mode, *, raw_voice=True):
    db, specs, source, _, answer = prepared(tmp_path, 'math-reasoning', response_mode=mode)
    settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False,
        data_encryption_key=b'8' * 32)
    monkeypatch.setenv('HSC_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('HSC_DATABASE_PATH', str(db.path))
    monkeypatch.setattr(original, 'BODY', CASES['math-reasoning'][2][0])
    generator = original.journey.__wrapped__((settings, db), monkeypatch, raw_media=raw_voice)
    j = next(generator)
    try:
        client = j['client']
        token = client.post('/v1/parent/unlock', json={'pin':'8264'}).json()['parent_token']
        client.headers['X-HSC-Parent'] = token
        published = [publish(client, spec) for spec in specs]
        for i, item in enumerate(published):
            grant_question(client, item['diagnostic_item_id'], 'open-grant-'+str(i))
        del client.headers['X-HSC-Parent']
        j['responses'].append('先说明每一步计算的意思，再用题目条件检查。')
        # Only due-offer scheduling uses the fixture's accelerated clock.
        # New task/answer/revision timestamps must share the normal host clock.
        with monkeypatch.context() as due_clock:
            j['cid'] = next_day(j, due_clock)
            offer(j)
            say(j, due_clock, '现在先试试原题', 'start', 'open-original-start')
        j.update(source=source, answer=answer, published=published)
        yield j
    finally:
        generator.close()


def snapshot(j):
    with j['db'].connect() as c:
        return json.loads(c.execute('SELECT payload_json FROM agent_commitment WHERE commitment_id=?',
            (j['cid'],)).fetchone()[0])


def answer_open(j, monkeypatch, prompt, mode, key='open-answer'):
    text = j['answer']
    body = dict(prompt_id=prompt['prompt_id'], item_version=prompt['item_version'], answer=text, client_action_id=key)
    quote = text
    role, control, action = 'answer', None, 'submit_diagnostic_answer'
    if mode == 'written':
        manuscript = WritingOriginalService(j['db']).capture_native_response(j['sid'],
            prompt_id=prompt['prompt_id'], item_version=prompt['item_version'], text=text, client_action_id=key+'-original')
        body['writing_original_artifact_id'] = manuscript['artifact_id']
        from home_study_agent.native_writing import original_receipt
        with j['db'].connect() as c:
            row = c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=?', (prompt['prompt_id'],)).fetchone()
            body['writing_original_revision'] = original_receipt(c, row, manuscript['artifact_id'], text)['original_revision']
        quote = '继续提交本题'
        role, control, action = 'control', 'native_writing.submit_retained', 'submit_retained_native_writing'
    j['responses'].append(json.dumps({'role':role,'control':control,'quote':quote,'confidence':.99}))
    voice = capture(j['client'], j['settings'], monkeypatch, j['sid'], quote, prompt, key+'-voice')
    turn = interpret(j['client'], j['sid'], quote, key+'-turn', voice)
    assert turn['action']['type'] == action, turn
    body['authorized_turn_id'] = turn['agent_turn_id']
    if mode == 'written':
        admission = j['client'].post(f'/v1/sessions/{j["sid"]}/agent-turns/{turn["agent_turn_id"]}/control-admission')
        assert admission.status_code == 200 and admission.json()['status'] == 'admitted', admission.text
    result = j['client'].post(f'/v1/sessions/{j["sid"]}/diagnostic-prompt/answer', json=body)
    assert result.status_code == 200, result.text
    return result.json(), body


def evaluation(j, prompt):
    with j['db'].connect() as c:
        item = validate_item(c, CHILD, prompt['diagnostic_item_id'])
        node = dict(c.execute('SELECT * FROM knowledge_node WHERE knowledge_node_id=?', (item['knowledge_node_id'],)).fetchone())
        dimensions = profile_for_item(item, node)['dimensions']
        points = json.loads(item['expected_answer_json'])['task_points']
    result = dict(understanding='partial', confidence=.8,
        evidence_summary='Synthetic evidence-bound formative response.',
        spoken_feedback='你说明了先求总量再平均分，请用这道题的具体数值检查。',
        next_check='怎样用这道题的原条件检查？',
        rubric_dimensions=[dict(dimension_id=d['dimension_id'], score=1, response_quotes=[j['answer']],
            rationale='Synthetic reference anchored to the saved response.') for d in dimensions],
        criterion_evidence=[dict(criterion=p, response_quote=j['answer']) for p in points])
    from formative_response_fixture import criterion_reply
    payload = {
        'response': j['answer'],
        'rubric': {'task_points': points},
    }
    return json.dumps(criterion_reply(payload, result), ensure_ascii=False)


@pytest.mark.parametrize('mode', ['written', 'spoken_explanation'])
def test_original_new_open_task_formative_and_restart(tmp_path, monkeypatch, mode):
    with opened(tmp_path, monkeypatch, mode) as j:
        result = say(j, monkeypatch, '换道新题，我要解释我的思路', 'independent_check', 'open-new')
        check = result['execution']['result'].get('diagnostic_prompt')
        assert check and check['status'] == 'waiting_answer', result
        prompt = check['prompt']
        assert prompt['item']['answer_type'] == 'open_response'
        assert prompt['item']['response_mode'] == mode
        assert 'expected_answer' not in json.dumps(check)
        assert prompt['item']['prompt'] != original.BODY
        assert prompt['diagnostic_item_id'] in {p['diagnostic_item_id'] for p in j['published']}
        saved, body = answer_open(j, monkeypatch, prompt, mode)
        assert saved['attempt']['correctness'] is None
        assert saved['attempt']['score_status'] == 'needs_review'
        follow = saved['local_learning_evaluation']
        assert follow['status'] == 'feedback_required', follow
        j['responses'].append(evaluation(j, prompt))
        evaluated = j['client'].post('/v1/agent-turns/'+follow['feedback_turn_id']+'/execute')
        assert evaluated.status_code == 200, evaluated.text
        assessment = evaluated.json()['execution']['result']
        assert assessment['formative_only'] is True, assessment
        projected = json.loads(j['calls'][-1]['messages'][1]['content'])
        assert set(projected) == {'question','response','rubric'}
        assert projected['response'] == j['answer'] and projected['question'] == prompt['item']['prompt']
        assert original.BODY not in json.dumps(projected, ensure_ascii=False)
        assert snapshot(j)['practice']['independent_results'][-1]['attempt_id'] == saved['attempt']['attempt_id']
        j['client'].__exit__(None, None, None)
        with TestClient(create_app(j['settings'], model_gateway=j['gateway'])) as restarted:
            j['client'] = restarted
            count = len(j['calls'])
            # Recovery reads the committed answer; it must not replay an old
            # native submission control into a later formative-feedback scope.
            replay = restarted.get(f'/v1/sessions/{j["sid"]}/diagnostic-prompt', params={'prompt_id':prompt['prompt_id']})
            assert replay.status_code == 200, replay.text
            assert replay.json()['attempt']['attempt_id'] == saved['attempt']['attempt_id']
            assert replay.json()['prompt']['item']['response_mode'] == mode
            repeat = restarted.post('/v1/agent-turns/'+follow['feedback_turn_id']+'/execute')
            assert repeat.status_code == 200, repeat.text
            assert len(j['calls']) == count
            with j['db'].connect() as c:
                assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 1
                assert c.execute('SELECT count(*) FROM rubric_assessment').fetchone()[0] == 1
                assert not c.execute('SELECT 1 FROM mastery_state WHERE child_id=? AND knowledge_node_id=? AND evidence_count>0',
                    (CHILD,j['source']['knowledge_node_id'])).fetchone()
            if mode == 'written':
                quote = '我想按刚才的反馈自己改一遍'
                j['responses'].append(json.dumps(dict(role='control', control='writing.revise_original', quote=quote, confidence=.99)))
                voice = capture(j['client'], j['settings'], monkeypatch, j['sid'], quote, prompt, 'open-revise-voice')
                turn = interpret(j['client'], j['sid'], quote, 'open-revise', voice)
                assert turn['runtime_capability'] == 'writing.revise_original', turn
                revised = j['client'].post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
                assert revised.status_code == 200, revised.text
                target = revised.json()['execution']['result']['prompt']
                assert target['prompt_id'] != prompt['prompt_id']
                assert target['diagnostic_item_id'] == prompt['diagnostic_item_id']
                j['answer'] += '我还要乘回人数，再核对每袋数量和袋数相乘是否相等。'
                second, _ = answer_open(j, monkeypatch, target, mode, key='open-revised-answer')
                assert second['local_learning_evaluation']['status'] == 'feedback_required', second['local_learning_evaluation']
                j['responses'].append(evaluation(j, target))
                again = j['client'].post('/v1/agent-turns/'+second['local_learning_evaluation']['feedback_turn_id']+'/execute')
                assert again.status_code == 200, again.text
                assert again.json()['execution']['result']['formative_only'] is True
                with j['db'].connect() as c:
                    assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 2
                    assert c.execute('SELECT count(*) FROM rubric_assessment').fetchone()[0] == 2


@pytest.mark.parametrize('fault', ['withdraw_source', 'extra_local_material', 'alter_criteria', 'request_voice'])
def test_open_feedback_refuses_changed_lineage_without_losing_the_answer(tmp_path, monkeypatch, fault):
    with opened(tmp_path, monkeypatch, 'written') as j:
        result = say(j, monkeypatch, '换道新题检查我的思路', 'independent_check', 'open-negative-new')
        prompt = result['execution']['result']['diagnostic_prompt']['prompt']
        saved, _ = answer_open(j, monkeypatch, prompt, 'written')
        follow = saved['local_learning_evaluation']
        assert follow['status'] == 'feedback_required', follow
        turn_id = result['turn']['agent_turn_id']
        with j['db'].connect() as c:
            if fault == 'withdraw_source':
                c.execute("UPDATE diagnostic_item SET status='inactive' WHERE diagnostic_item_id=?", (prompt['diagnostic_item_id'],))
            elif fault == 'extra_local_material':
                c.execute("UPDATE agent_turn SET action_json=json_set(action_json,'$.private_material','PRIVATE_UNAUTHORIZED_ORIGINAL') WHERE agent_turn_id=?", (turn_id,))
            elif fault == 'alter_criteria':
                c.execute("UPDATE agent_plan SET context_manifest_json=json_set(context_manifest_json,'$.document_open_check.success_criteria',json('[\"forged\"]')) WHERE agent_turn_id=?", (turn_id,))
            else:
                c.execute("UPDATE voice_input SET transcript='changed authority' WHERE voice_input_id=?", (result['turn']['action']['voice_input_id'],))
            c.commit()
        calls = len(j['calls'])
        response = j['client'].post('/v1/agent-turns/'+follow['feedback_turn_id']+'/execute')
        assert response.status_code in {200,409,422}, response.text
        if response.status_code == 200:
            assert not response.json().get('execution', {}).get('result', {}).get('rubric_assessment'), response.text
        assert len(j['calls']) == calls
        with j['db'].connect() as c:
            row = c.execute('SELECT * FROM diagnostic_attempt').fetchone()
            assert row['attempt_id'] == saved['attempt']['attempt_id'] and row['submitted_answer'] == j['answer']
            assert row['correctness'] is None
            assert c.execute('SELECT count(*) FROM rubric_assessment').fetchone()[0] == 0


def test_open_selection_rollback_and_child_ownership(tmp_path, monkeypatch):
    from home_study_agent import document_open_check
    from home_study_agent.document_independent_check import _qualified
    with opened(tmp_path, monkeypatch, 'spoken_explanation') as j:
        with j['db'].connect() as c:
            assert all(_qualified(c, CHILD, p['diagnostic_item_id']) for p in j['published'])
            assert not any(_qualified(c, 'child-junior-3', p['diagnostic_item_id']) for p in j['published'])
            assert _qualified(c, CHILD, 'diag-p4-open-math-reasoning-1') is None
        turn = say(j, monkeypatch, '换道新题检查', 'independent_check', 'open-rollback', execute=False)
        with j['db'].connect() as c:
            before = [tuple(r) for r in c.execute('SELECT * FROM agent_objective ORDER BY rowid')]
        real = document_open_check.bind_before_answer
        def broken(*args):
            real(*args)
            raise ValueError('DOCUMENT_OPEN_SYNTHETIC_FAILURE')
        with monkeypatch.context() as failure:
            failure.setattr(document_open_check, 'bind_before_answer', broken)
            response = j['client'].post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
            assert response.status_code == 409, response.text
        with j['db'].connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_prompt').fetchone()[0] == 0
            assert [tuple(r) for r in c.execute('SELECT * FROM agent_objective ORDER BY rowid')] == before
        assert snapshot(j)['practice']['phase'] == 'waiting_answer'
