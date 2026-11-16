"""Actual source publication to original-goal continuation, not catalog toggles.

The two authored tasks/standard excerpt, PCM and model responses are synthetic;
the import, validation, review, publication, reminder and runtime are real hosts.
"""
from datetime import datetime, timedelta, timezone
import json
from synthetic_oral_app_lifecycle import fresh_oral_case

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.knowledge_base import KnowledgeBaseService
from home_study_agent.open_response_source import VALIDATOR
from test_open_response_spoken_runtime_http import (
    test_natural_spoken_process_asr_bound_answer_formative_and_restart as journey,
)
from test_voice_runtime_binding import capture, interpret


@pytest.mark.parametrize('subject', ['数学', '物理'])
def test_published_source_releases_saved_supply_request_without_losing_original_goal(tmp_path, monkeypatch, subject, after_missing=None):
    from home_study_agent import agent_runtime, agent_orchestrator, voice_control_intent
    original_import = KnowledgeBaseService.import_source
    source_info = {}
    clock = [datetime.now(timezone.utc)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)

    def import_pair(service, child, values):
        if values.get('source_ref') == 'isolated-spoken-source':
            first = json.loads(values['content_text'])
            second = {**first, 'prompt':
                '不直接求根，口头分析 4x²+4x+1=0 在实数范围内的解的情况，并说明判断依据。'
                if subject == '数学' else
                '两根同材料、同长度导线粗细不同。口头说明检验横截面积影响电阻的方案、需要控制的条件及结论边界。'}
            values = {**values, 'content_text':json.dumps(first,ensure_ascii=False)+'\n'+json.dumps(second,ensure_ascii=False)}
            source_info['second'] = second
        result = original_import(service, child, values)
        if values.get('source_ref') == 'isolated-spoken-source':
            source_info['id'] = result['source_id']
        return result

    monkeypatch.setattr(KnowledgeBaseService, 'import_source', import_pair)

    def continue_after_answer(j):
        client, db, sid, child, prompt = j['client'], j['db'], j['sid'], j['child'], j['prompt']
        client.headers.update(j['headers'])
        assert client.post('/v1/agent-turns/'+j['follow']['feedback_turn_id']+'/execute').status_code == 200
        quote = '换一道没做过的题，我自己试试'
        j['command_state'].update(control='oral.independent_check',quote=quote)
        voice = capture(client,j['settings'],monkeypatch,sid,quote,prompt,'published-gap-audio',child=child)
        gap = interpret(client,sid,quote,'published-gap-request',voice)
        assert gap['action']['type'] == 'none', gap
        assert '记在刚才的学习目标下' in gap['response_text'], gap
        assert client.post('/v1/agent-turns/'+gap['agent_turn_id']+'/execute').status_code == 200
        with db.connect() as c:
            commitment = dict(c.execute("SELECT * FROM agent_commitment WHERE child_id=? AND json_extract(payload_json,'$.origin')='oral_question_supply_v1'",(child,)).fetchone())
            selection = c.execute('SELECT * FROM curriculum_supply_selection WHERE child_id=? AND subject=? ORDER BY version DESC LIMIT 1',(child,subject)).fetchone()
            version = selection['version']
        followup_path = f"/v1/parent/children/{child}/supply-followups/{commitment['commitment_id']}"
        detail = client.get(followup_path)
        assert detail.status_code == 200, detail.text
        detail = detail.json()
        assert detail['status']=='missing_source' and detail['subject']==subject
        assert detail['original_question']==j['source']['prompt']
        queued = client.get('/v1/parent/work-queue',params={'child_id':child,'limit':50})
        assert queued.status_code==200, queued.text
        queue = queued.json()
        assert any(row['kind']=='supply_followup' and row['object_id']==commitment['commitment_id'] for row in queue['items'])
        if after_missing:
            return after_missing({**j,'commitment':commitment,'followup_path':followup_path,
                'supply_source_id':source_info['id'],'second_source':source_info['second']})
        clock[0] = datetime.now(timezone.utc)+timedelta(seconds=70)
        for module in (agent_runtime,agent_orchestrator,voice_control_intent):
            monkeypatch.setattr(module,'datetime',Clock)
        agent = f'/v1/sessions/{sid}/agent'
        base = f'/api/parent/children/{child}/curriculum-supply'
        assert client.get(agent+'/proactive-prompt').json()['prompt'] is None
        options = client.get(base+'/sources/'+source_info['id']+'/options')
        assert options.status_code == 200, options.text
        options = options.json()
        requirement = next(row for row in options['standards'] if row['requirement_id']=='open-requirement')
        spec = dict(seed_item_id='diag-j3-discriminant-1' if subject=='数学' else 'diag-j3-physics-experiment-1',
            stage='parallel',excerpt_id=options['excerpts'][1]['excerpt_id'],requirement_id='open-requirement',
            standard_page=requirement['page_number'],standard_quote=requirement['quote'],validator=VALIDATOR,
            difficulty=2,explanation='Synthetic authored task reviewed as a source, not a mastery judgment.')
        body = dict(action_id='publish-gap-draft',expected_version=0,expected_scope_version=version,source=spec)
        candidate = client.post(base+'/generate',json=body)
        assert candidate.status_code == 200, candidate.text
        candidate = candidate.json()
        path = base+'/'+candidate['candidate_id']
        assert client.post(base+'/generate',json=body).json()['candidate_id'] == candidate['candidate_id']
        premature = client.post(path+'/publish',json=dict(action_id='premature-publish',expected_version=1))
        assert premature.status_code in (409,422), premature.text
        for expected,operation in enumerate(('validate','review'),1):
            command = dict(action_id='supply-'+operation,expected_version=expected)
            if operation == 'review':
                command.update(decision='approve',note='Checked synthetic original, exact source and task family.')
            result = client.post(path+'/'+operation,json=command)
            assert result.status_code == 200, result.text
            assert client.get(agent+'/proactive-prompt').json()['prompt'] is None

        with fresh_oral_case({**j,'client_options':{'raise_server_exceptions':False}},TestClient,
                client=('127.0.0.1',41972)) as reopened_case:
            reopened=reopened_case['client']
            unlocked = reopened.post('/v1/parent/unlock',json={'pin':'8264'})
            assert unlocked.status_code == 200, unlocked.text
            reopened.headers['X-HSC-Parent'] = unlocked.json()['parent_token']
            assert reopened.get(path).json()['state'] == 'reviewed'
            published = reopened.post(path+'/publish',json=dict(action_id='supply-publish',expected_version=3))
            assert published.status_code == 200, published.text
            published = published.json()
            assert published['eligible'] and published['state']=='published'
            ready_detail = reopened.get(followup_path).json()
            assert ready_detail['status']=='ready' and ready_detail['version']!=detail['version']
            assert ready_detail['original_question']==detail['original_question']
            offer = reopened.get(agent+'/proactive-prompt').json()['prompt']
            assert offer and offer['source_type']=='oral_supply_ready'
            assert reopened.post(agent+'/proactive-prompts/'+offer['prompt_id']+'/acknowledge').json()['status']=='acknowledged'
            with db.connect() as c:
                assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt WHERE child_id=?',(child,)).fetchone()[0]==1
                assert c.execute('SELECT status FROM agent_commitment WHERE commitment_id=?',(commitment['commitment_id'],)).fetchone()[0]=='pending'
            j['command_state']['quote']='好'
            voice = capture(reopened,j['settings'],monkeypatch,sid,'好',prompt,'published-ready-audio',child=child)
            accepted = interpret(reopened,sid,'好','published-ready-answer',voice)
            assert accepted['runtime_capability']=='oral.independent_check', accepted
            executed = reopened.post('/v1/agent-turns/'+accepted['agent_turn_id']+'/execute')
            assert executed.status_code==200, executed.text
            prepared = executed.json()['execution']['result']['prompt']
            assert prepared['diagnostic_item_id']==published['diagnostic_item_id']
            with db.connect() as c:
                done = c.execute('SELECT * FROM agent_commitment WHERE commitment_id=?',(commitment['commitment_id'],)).fetchone()
                resolution = json.loads(done['payload_json'])['resolution']
                assert done['status']=='completed' and resolution['prompt_id']==prepared['prompt_id']
                assert not resolution['learning_completed']
                assert c.execute('SELECT objective_id FROM agent_plan WHERE agent_turn_id=?',(accepted['agent_turn_id'],)).fetchone()[0]==commitment['objective_id']
                assert c.execute('SELECT submitted_answer FROM diagnostic_attempt WHERE attempt_id=?',(j['attempt']['attempt_id'],)).fetchone()[0]==j['quote']
                assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE child_id=?',(child,)).fetchone()[0]==1
            delivered_detail = reopened.get(followup_path).json()
            assert delivered_detail['status']=='ready'
            assert '等待孩子作答' in delivered_detail['next_action']
            assert delivered_detail['version']!=ready_detail['version']
            assert source_info['second']['prompt'] not in json.dumps(j['posts'],ensure_ascii=False)
            assert reopened.get(followup_path.replace(child,'child-primary-4')).status_code==404
            parent_token = reopened.headers.pop('X-HSC-Parent')
            assert reopened.get(followup_path).status_code==401
            reopened.headers['X-HSC-Parent'] = parent_token
            # Synthetic lifecycle fixtures exercise the read-only parent projection,
            # not a claim that a child submitted this second answer.
            with db.connect() as c:
                c.execute("UPDATE agent_objective SET status='paused' WHERE objective_id=?",(commitment['objective_id'],))
                c.commit()
            paused = reopened.get(followup_path)
            assert paused.status_code==200, paused.text
            assert paused.json()['status']=='paused'
            with db.connect() as c:
                c.execute("UPDATE agent_objective SET status='completed' WHERE objective_id=?",(commitment['objective_id'],))
                c.commit()
            ended = reopened.get(followup_path)
            assert ended.status_code==200, ended.text
            assert ended.json()['status']=='cancelled'
            with db.connect() as c:
                c.execute("UPDATE diagnostic_prompt SET status='answered' WHERE prompt_id=?",(prepared['prompt_id'],))
                c.commit()
            historical = reopened.get(followup_path)
            assert historical.status_code==200, historical.text
            assert historical.json()['status']=='practice_completed'
            assert '作答不等于掌握' in historical.json()['next_action']

    journey(tmp_path,monkeypatch,subject,None,False,None,after_saved=continue_after_answer,
        http_client_address=('127.0.0.1',41971))
