"""Isolated follow-on candidate; no supplier or family data."""
from fastapi.testclient import TestClient
import json
import pytest
from home_study_agent.app import create_app
from home_study_agent.model_gateway import ModelResult
from test_pet_vocabulary_prompt import flow, rows
from test_pet_vocabulary_prompt_integration import environment, next_prompt, binding, Planner
from test_document_voice_capture import begin, complete
from test_foreground_independent_dialogue import foreground_models


@pytest.fixture
def vocabulary_context(environment):
    settings,db,sid=environment;gateway=Planner()
    with TestClient(create_app(settings,model_gateway=gateway)) as client:
        with db.connect() as c:
            c.execute("UPDATE daily_session SET state='study' WHERE session_id=?",(sid,));c.commit()
        original=next_prompt(client,sid)
        yield {'settings':settings,'db':db,'sid':sid,'client':client,'gateway':gateway,'original':original}


def independent_turn(j,quote,key='owned-vocabulary-independent'):
    accepted=begin(j,reference={'kind':'vocabulary','binding':binding(j['original'])})
    assert accepted.status_code==200,accepted.text
    voice=complete(j,text=quote)
    result=j['client'].post(f"/v1/sessions/{j['sid']}/agent-turn",json={
        'transcript':quote,'voice_input_id':voice['voice_input_id'],'client_action_id':key,'cloud_text_allowed':True})
    assert result.status_code==200,result.text
    return result.json()


def change_source(j):
    with j['db'].connect() as c:
        c.execute("UPDATE pet_vocabulary_card SET source_ref='revised-synthetic-source' WHERE card_id=?",
                  (j['original']['card_id'],));c.commit()


@pytest.mark.parametrize('utterance,intent', [
    ('你听到我的声音了吗？', 'audio_check'),
    ('我想伤害自己', 'urgent_safety_support'),
])
def test_real_vocabulary_prompt_does_not_block_conversation_priority(vocabulary_context, monkeypatch, utterance, intent):
    j = vocabulary_context
    monkeypatch.setattr(j['gateway'], 'configured', False)
    turn = j['client'].post(f"/v1/sessions/{j['sid']}/agent-turn", json={
        'transcript': utterance, 'client_action_id': f'real-card-priority-{intent}',
    })
    assert turn.status_code == 200, turn.text
    assert turn.json()['intent'] == intent
    assert turn.json()['action']['type'] == ('find_parent_urgent' if intent == 'urgent_safety_support' else 'none')
    assert rows(j['db'], 'pet_vocabulary_attempt') == []
    assert binding(next_prompt(j['client'], j['sid'])) == binding(j['original'])


@pytest.mark.parametrize('broken_field,value', [
    ('prompt_session_id', None), ('prompt_session_id', ''),
    ('card_version', None), ('card_version', ' '), ('card_version', 3),
])
def test_malformed_vocabulary_reference_cannot_be_selected(vocabulary_context, monkeypatch, broken_field, value):
    j = vocabulary_context
    malformed = {**j['original'], broken_field: value}
    monkeypatch.setattr(j['client'].app.state.agent_orchestrator.pet_vocabulary,
                        'active_prompt', lambda _: malformed)
    turn = j['client'].post(f"/v1/sessions/{j['sid']}/agent-turn", json={
        'transcript': '先回答词汇卡', 'client_action_id': f'malformed-card-{broken_field}-{value}',
    })
    assert turn.status_code == 200, turn.text
    assert turn.json()['action']['type'] not in {'start_pet_vocabulary_review', 'submit_pet_vocabulary_answer', 'ask_tutor'}
    assert rows(j['db'], 'pet_vocabulary_attempt') == []
    assert rows(j['db'], 'tutor_turn') == []


@pytest.mark.parametrize('change',['none','source_before','cancel_before','cancel_after'])
def test_vocabulary_independent_tutor_never_uses_the_original_card(vocabulary_context,monkeypatch,change):
    j=vocabulary_context;quote='数学里两个负数相乘为什么得到正数？'
    foreground_models(j,monkeypatch,quote,capability='learning.explain',arguments={'question':quote,'subject':'数学'})
    turn=independent_turn(j,quote);action=turn['action']
    assert action['type']=='ask_tutor',action
    assert action['tutor_request_action_id'].startswith('foreground-question:')
    assert action.get('diagnostic_prompt_id') is None and action.get('ocr_confirmation_id') is None
    def cancel():
        with j['db'].connect() as c:
            c.execute("UPDATE pet_vocabulary_prompt_session SET status='cancelled' WHERE prompt_session_id=?",
                      (j['original']['prompt_session_id'],));c.commit()
    original_complete=j['gateway'].complete
    async def tutor(request):
        if not request.request_type.startswith('tutor_hint_level_'):return await original_complete(request)
        request.pre_dispatch();j['gateway'].requests.append(request)
        if change=='cancel_after':cancel()
        return ModelResult(content=json.dumps({'blackboard':{'title':'负数乘法','segments':[{
            'narration':'保持分配律一致时，两个负数相乘就得到正数。',
            'blocks':[{'kind':'text','text':'负数乘负数得到正数。'}]}]}},ensure_ascii=False),
            provider='test',model='synthetic-tutor',latency_ms=1,prompt_tokens=20,completion_tokens=20)
    monkeypatch.setattr(j['gateway'],'complete',tutor)
    if change=='source_before':change_source(j)
    if change=='cancel_before':cancel()
    body={'subject':action['subject'],'question':action['question'],'hint_level':action['hint_level'],
          'client_action_id':action['tutor_request_action_id']}
    lesson=j['client'].post(f"/v1/sessions/{j['sid']}/tutor",json=body)
    if change=='none':
        assert lesson.status_code==200,lesson.text
        assert lesson.json()['question']==quote
        assert binding(next_prompt(j['client'],j['sid']))==binding(j['original'])
        submitted=j['client'].post(f"/v1/sessions/{j['sid']}/pet-vocabulary/prompt/answer",json={
            **binding(j['original']),'answer':'unrelated','client_action_id':'actual-answer-after-independent-lesson'})
        assert submitted.status_code==200,submitted.text
        assert len(rows(j['db'],'pet_vocabulary_attempt'))==1
        j['client'].__exit__(None, None, None)
        with TestClient(create_app(j['settings'],model_gateway=j['gateway'])) as restarted:
            replay=restarted.post(f"/v1/sessions/{j['sid']}/tutor",json=body)
            assert replay.status_code==200 and replay.json()['turn_id']==lesson.json()['turn_id']
    else:
        assert lesson.status_code in {409,422},lesson.text
        assert rows(j['db'],'pet_vocabulary_attempt')==[]
        assert rows(j['db'],'tutor_turn')==[]
    tutor_requests=[request for request in j['gateway'].requests if request.request_type.startswith('tutor_hint_level_')]
    assert len(tutor_requests)==int(change in {'none','cancel_after'})
    for request in tutor_requests:
        exported=json.dumps({'messages':request.messages,'manifest':request.context_manifest,'refs':request.evidence_refs})
        assert request.context_manifest['lane']=='foreground_independent_tutor_v1'
        for private in [j['sid'],j['original']['prompt_session_id'],j['original']['card_version'],
                        j['original']['card_id'],j['original']['prompt']]:
            assert private not in exported


def test_changed_card_blocks_independent_resource_execution(vocabulary_context,monkeypatch):
    j=vocabulary_context;quote='先不看这张词卡，你有PET学习资料吗？'
    foreground_models(j,monkeypatch,quote)
    turn=independent_turn(j,quote)
    assert turn['action']['type']=='inspect_learning_system',turn['action']
    change_source(j)
    result=j['client'].post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
    assert result.status_code in {409,422},result.text
    assert rows(j['db'],'pet_vocabulary_attempt')==[]


@pytest.mark.parametrize('quote',['我们先不看这张词卡，你有PET学习资料可以给我介绍吗？',
                                '先不看这张词卡，你有PET学习资料吗？'])
def test_vocabulary_waiting_answer_can_answer_an_independent_resource_question(environment, monkeypatch,quote):
    settings, db, sid = environment
    gateway = Planner()
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        with db.connect() as c:
            c.execute("UPDATE daily_session SET state='study' WHERE session_id=?",(sid,));c.commit()
        original = next_prompt(client, sid)
        j = {'settings':settings, 'db':db, 'sid':sid, 'client':client, 'gateway':gateway}
        foreground_models(j, monkeypatch, quote)
        started = begin(j, reference={'kind':'vocabulary', 'binding':binding(original)})
        assert started.status_code == 200, started.text
        voice = complete(j, text=quote)
        result = client.post(f'/v1/sessions/{sid}/agent-turn', json={
            'transcript':quote, 'voice_input_id':voice['voice_input_id'],
            'client_action_id':'independent-vocabulary-resource', 'cloud_text_allowed':True})
        assert result.status_code == 200, result.text
        assert rows(db,'pet_vocabulary_attempt') == []
        current = next_prompt(client,sid)
        assert binding(current) == binding(original)
        with db.connect() as c:
            calls=[dict(row) for row in c.execute('SELECT purpose,status,error_code FROM model_call')]
        assert result.json()['action']['type'] == 'inspect_learning_system',calls
        assert result.json()['action']['scope'] == 'resources'
        assert result.json()['action']['track'] == 'pet'
        execution=client.post(f"/v1/agent-turns/{result.json()['agent_turn_id']}/execute")
        assert execution.status_code==200,execution.text
        for request in gateway.requests:
            exported=json.dumps({'messages':request.messages,'manifest':request.context_manifest,'refs':request.evidence_refs})
            for private in [sid,original['prompt_session_id'],original['card_version'],original['card_id'],original['prompt']]:
                assert private not in exported
        client.__exit__(None, None, None)
        with TestClient(create_app(settings,model_gateway=gateway)) as restarted:
            replay=restarted.post(f"/v1/agent-turns/{result.json()['agent_turn_id']}/execute")
            assert replay.status_code==200 and replay.json()['execution']==execution.json()['execution']
        assert len(gateway.requests)==2
        assert rows(db,'pet_vocabulary_attempt')==[]


@pytest.mark.parametrize('timing',['planning','execution'])
def test_cancelled_vocabulary_cannot_authorize_an_independent_reply(environment,monkeypatch,timing):
    settings,db,sid=environment;gateway=Planner()
    quote='我们先不看这张词卡，你有PET学习资料可以给我介绍吗？'
    with TestClient(create_app(settings,model_gateway=gateway)) as client:
        with db.connect() as c:
            c.execute("UPDATE daily_session SET state='study' WHERE session_id=?",(sid,));c.commit()
        original=next_prompt(client,sid)
        j={'settings':settings,'db':db,'sid':sid,'client':client,'gateway':gateway}
        foreground_models(j,monkeypatch,quote)
        original_complete=gateway.complete
        def cancel():
            if timing=='planning':
                # Model callback runs on the app loop; a nested blocking
                # TestClient call is not a second real HTTP caller.
                with db.connect() as c:
                    c.execute("UPDATE pet_vocabulary_prompt_session SET status='cancelled' WHERE prompt_session_id=?",
                              (original['prompt_session_id'],));c.commit()
            else:
                response=client.post(f'/v1/sessions/{sid}/pet-vocabulary/prompt/cancel',json=binding(original))
                assert response.status_code==200,response.text
        async def delayed(request):
            if request.request_type=='agent_plan' and timing=='planning':cancel()
            return await original_complete(request)
        monkeypatch.setattr(gateway,'complete',delayed)
        started=begin(j,reference={'kind':'vocabulary','binding':binding(original)})
        assert started.status_code==200,started.text
        voice=complete(j,text=quote)
        result=client.post(f'/v1/sessions/{sid}/agent-turn',json={'transcript':quote,
            'voice_input_id':voice['voice_input_id'],'client_action_id':'cancel-vocabulary-independent','cloud_text_allowed':True})
        if timing=='planning':
            with db.connect() as c:
                calls=[dict(row) for row in c.execute('SELECT purpose,status,error_code FROM model_call')]
            assert result.status_code in {409,422},calls
        else:
            assert result.status_code==200,result.text
            assert result.json()['action']['type']=='inspect_learning_system',result.json()['action']
            cancel()
            dispatched=client.post(f"/v1/agent-turns/{result.json()['agent_turn_id']}/execute")
            assert dispatched.status_code in {409,422},dispatched.text
        assert rows(db,'pet_vocabulary_attempt')==[]
