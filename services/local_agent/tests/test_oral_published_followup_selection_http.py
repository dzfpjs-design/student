"""Two lawful published sources; selection is not an executed retry receipt."""
from dataclasses import replace
import json

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.agent_runtime import AgentRuntimeStore
from home_study_agent.database import Database
import test_oral_source_revision_combined_http as source_fixture
from test_pet_oral_supply_http import setup, post, CHILD, BASE, FAMILIES, VALIDATOR
from test_voice_runtime_binding import capture, interpret, submit
from test_oral_runtime_feedback_http import begin_oral, OralGateway, ANSWER


@pytest.mark.parametrize('same_question', [False, True])
def test_actual_two_published_sources_independent_candidate_rejects_alias_and_revoke(tmp_path,monkeypatch,same_question):
    initial,settings = setup(tmp_path)
    settings = replace(settings,data_encryption_key=b'I'*32)
    db = Database(initial.path,media_root=settings.data_dir,data_encryption_key=settings.data_encryption_key)
    original_values = source_fixture.values
    original_author = source_fixture.author
    imported = {}
    def values(text):
        first = json.loads(text)
        second = {**first,'prompt':first['prompt'] if same_question else
            'Tell your friend about a hobby you would like to try. Explain why and what you would do first.',
            'task_points':['说出想尝试的活动','解释理由并说出第一步']}
        return original_values(text+'\n'+json.dumps(second,ensure_ascii=False))
    def author(client,database,**kwargs):
        result = original_author(client,database,**kwargs)
        imported['source'] = result[1]
        return result
    monkeypatch.setattr(source_fixture,'values',values)
    monkeypatch.setattr('test_oral_runtime_feedback_http.author',author)
    gateway = OralGateway()
    with TestClient(create_app(settings,model_gateway=gateway),headers={'X-HSC-Session':settings.session_token},
                    client=('127.0.0.1',41944)) as client:
        client.headers['X-HSC-Parent'] = post(client,'/v1/parent/setup',{'pin':'7391'})['parent_token']
        assert client.put('/v1/parent/privacy',json={'raw_media_storage_allowed':True,
            'cloud_model_text_allowed':True,'cloud_audio_allowed':False}).status_code==200
        sid,prompt,_,_ = begin_oral(client,db,gateway)
        options = client.get(BASE+f"/sources/{imported['source']['source_id']}/options").json()
        standard = next(row for row in options['standards'] if row['requirement_id']=='c03-req')
        generated = client.post(BASE+'/generate',json=dict(action_id='second-oral-source',expected_version=0,expected_scope_version=1,
            source=dict(seed_item_id=FAMILIES['topic_response'],stage='parallel',excerpt_id=options['excerpts'][1]['excerpt_id'],
                requirement_id='c03-req',standard_page=standard['page_number'],standard_quote=standard['quote'],
                validator=VALIDATOR,difficulty=1,explanation='Synthetic second original task.')))
        if same_question:
            # The real producer rejects this alias before it can reach the
            # selector. Do not bypass that guard to manufacture a published one.
            assert generated.status_code==422,generated.text
            assert generated.json()['detail']['code']=='CURRICULUM_DUPLICATE_QUESTION'
            with db.connect() as c:
                assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==0
            return
        assert generated.status_code==200,generated.text
        second=generated.json()
        for version,op in enumerate(('validate','review','publish'),1):
            body = dict(action_id='second-'+op,expected_version=version)
            if op=='review': body.update(decision='approve',note='Synthetic source review, not an answer score.')
            second = post(client,BASE+'/'+second['candidate_id']+'/'+op,body)
        gateway.quote = ANSWER
        voice = capture(client,settings,monkeypatch,sid,ANSWER,prompt,'two-sources-original')
        heard = interpret(client,sid,ANSWER,'two-sources-answer',voice)
        if heard['intent']=='pending_answer_clarification':
            post(client,f"/v1/agent-turns/{heard['agent_turn_id']}/execute",{})
            gateway.quote='刚才那句就是我的答案'
            confirmed=capture(client,settings,monkeypatch,sid,gateway.quote,prompt,'two-sources-confirm')
            heard=interpret(client,sid,gateway.quote,'two-sources-confirmed',confirmed)
        saved=submit(client,sid,prompt,heard,answer=ANSWER,key='two-sources-submit')
        assert saved.status_code==200,saved.text
        assert saved.json()['local_learning_evaluation']['status']=='feedback_ready'
        with db.connect() as c:
            c.execute('BEGIN')
            before=c.total_changes
            selected=AgentRuntimeStore.resolve_oral_followup(c,child_id=CHILD,session_id=sid,
                prompt_id=prompt['prompt_id'],item_version=prompt['item_version'],kind='independent_check')
            assert c.total_changes==before
        assert selected['status']=='ready',selected
        assert selected['action']['diagnostic_item_id']==second['diagnostic_item_id']
        assert selected['action']['required_hint_level']==0
        assert not selected['action']['mastery_claim_allowed']
        gateway.quote='换一道我没有做过的口语题，让我自己试试'
        gateway.requested_control='oral.independent_check'
        next_voice=capture(client,settings,monkeypatch,sid,gateway.quote,prompt,'independent-request-audio')
        requested=interpret(client,sid,gateway.quote,'independent-request',next_voice)
        assert requested['runtime_capability']=='oral.independent_check',requested
        executed=post(client,f"/v1/agent-turns/{requested['agent_turn_id']}/execute",{})
        value=executed['execution']['result']
        assert value['status']=='waiting_answer',value
        next_prompt=value['prompt']
        assert next_prompt['diagnostic_item_id']==second['diagnostic_item_id']
        assert next_prompt['prompt_id']!=prompt['prompt_id']
        assert value['required_hint_level']==0
        from test_oral_followup_execution_http import answer
        saved,_=answer(client,settings,monkeypatch,sid,next_prompt,gateway,
            'I would like to try painting because I enjoy colours. First I would draw a picture of a tree.',
            'independent-new-answer')
        assert saved['local_learning_evaluation']['status']=='feedback_ready',saved
        assert saved['attempt']['score_status']=='needs_review'
        with db.connect() as c:
            evidence=c.execute('SELECT independent FROM learning_evidence WHERE evidence_id=?',(saved['attempt']['evidence_id'],)).fetchone()
            assert not evidence[0], 'New question is not a calibrated audio assessment.'
        post(client,BASE+'/'+second['candidate_id']+'/withdraw',dict(action_id='withdraw-next-source',
            expected_version=second['version'],note='Withdraw exact selected source.'))
        with db.connect() as c:
            c.execute('BEGIN')
            assert AgentRuntimeStore.revalidate_oral_followup(c,action=selected['action'],
                binding_version=selected['binding_version'])['status']=='blocked'
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==2
            assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt').fetchone()[0]==2
