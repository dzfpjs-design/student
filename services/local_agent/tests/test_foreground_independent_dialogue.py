"""Real isolated HTTP/SQLite, synthetic semantic replies; no supplier probe."""
from test_document_independent_dialogue import PET, setup_models
from test_document_voice_capture import page_context, begin, complete
from test_diagnostic_prompt_http import environment, prepare, CHILD
import json
import pytest
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.model_gateway import ModelResult


def foreground_models(j, monkeypatch, quote=PET, *, scope='independent', **kwargs):
    planner=setup_models(j,monkeypatch,quote,**kwargs)
    planner.payload['objective']={'mode':'none','type':None,'summary':None,'success_criteria':[],'phase':'execute'}
    original=j['gateway'].complete
    async def scoped(request):
        result=await original(request)
        if request.request_type=='voice_interaction':
            value=json.loads(result.content)
            if scope is not None:
                value['question_scope']=scope
            return ModelResult(content=json.dumps(value,ensure_ascii=False),provider='test',model='synthetic',
                latency_ms=1,prompt_tokens=20,completion_tokens=20)
        return result
    monkeypatch.setattr(j['gateway'],'complete',scoped)
    return planner


def ask(j, prompt, quote=PET, key='synthetic-foreground-pet-question'):
    reference={'kind':'diagnostic','binding':{k:prompt[k] for k in ('prompt_id','item_version')}}
    assert begin(j,reference=reference).status_code==200
    voice=complete(j,text=quote)
    body={'transcript':quote,'voice_input_id':voice['voice_input_id'],
          'client_action_id':key,'cloud_text_allowed':True}
    result=j['client'].post(f"/v1/sessions/{j['sid']}/agent-turn",json=body)
    return result,body


def assert_retained(j, prompt):
    with j['db'].connect() as c:
        assert c.execute('SELECT status FROM diagnostic_prompt WHERE prompt_id=?',(prompt['prompt_id'],)).fetchone()[0]=='waiting_answer'
        assert c.execute('SELECT count(*) FROM diagnostic_attempt WHERE child_id=?',(CHILD,)).fetchone()[0]==0


def test_question_about_pet_resources_does_not_become_an_answer_to_waiting_diagnostic(page_context, monkeypatch):
    j=page_context
    prompt=prepare(j['client'],j['sid'])['prompt']
    foreground_models(j,monkeypatch)
    result,body=ask(j,prompt)
    assert result.status_code==200,result.text
    assert_retained(j,prompt)
    value=result.json()
    assert value['intent']=='system.inspect_learning',value
    assert value['action']['scope']=='resources'
    assert value['action']['track']=='pet'
    execution=j['client'].post(f"/v1/agent-turns/{value['agent_turn_id']}/execute")
    assert execution.status_code==200,execution.text
    assert '一百万' not in execution.json()['execution']['response_text']
    j['client'].__exit__(None, None, None)
    with TestClient(create_app(j['settings'],model_gateway=j['gateway'])) as restarted:
        replay=restarted.post(f"/v1/sessions/{j['sid']}/agent-turn",json=body)
        assert replay.status_code==200 and replay.json()['agent_turn_id']==value['agent_turn_id']
        saved=restarted.post(f"/v1/agent-turns/{value['agent_turn_id']}/execute")
        assert saved.status_code==200 and saved.json()['execution']==execution.json()['execution']
    assert_retained(j,prompt)
    assert [r.request_type for r in j['gateway'].requests]==['voice_interaction','agent_plan']
    for request in j['gateway'].requests:
        exported=json.dumps({'messages':request.messages,'manifest':request.context_manifest,'refs':request.evidence_refs},ensure_ascii=False)
        for source in [prompt['prompt_id'],prompt['item_version'],j['original']['artifact_id'],j['original']['content_sha256'],
                       CHILD,j['sid'],'Calculate (24 + 6) x 3']:
            assert source not in exported
    payload=json.loads(j['gateway'].requests[1].messages[1]['content'])
    assert set(payload['context'])=={'session','child','conversation_state'}
    assert {i['name'] for i in payload['available_capabilities']}=={'conversation.respond','system.inspect_learning','learning.explain'}
    with j['db'].connect() as c:
        plan=c.execute('SELECT * FROM agent_plan WHERE agent_turn_id=?',(value['agent_turn_id'],)).fetchone()
        assert plan['objective_id'] is None
        assert json.loads(plan['context_manifest_json'])['foreground_independent_dialogue_v1']['diagnostic']=={k:prompt[k] for k in ('prompt_id','item_version')}


@pytest.mark.parametrize('role,scope,confidence', [('question','current',.97),('question','unknown',.97),
    ('question',None,.97),('mixed',None,.97),('unknown',None,.97),('question','independent',.2)])
def test_uncertain_or_original_question_does_not_gain_independent_lane(page_context,monkeypatch,role,scope,confidence):
    j=page_context; prompt=prepare(j['client'],j['sid'])['prompt']
    foreground_models(j,monkeypatch,role=role,scope=scope,confidence=confidence)
    result,_=ask(j,prompt)
    assert result.status_code==200,result.text
    assert result.json()['action']['type']=='none'
    assert not any(r.request_type=='agent_plan' for r in j['gateway'].requests)
    assert_retained(j,prompt)


@pytest.mark.parametrize('role,quote,response', [('greeting','你好小知，今天见到你很开心。','很高兴见到你。'),
    ('companionship','今天上课好累，想和你聊一会儿。','听起来今天挺累的。'),
    ('topic_mention','一元二次方程','你想聊今天学的内容还是有具体问题？')])
def test_companionship_does_not_replace_active_learning_goal(page_context,monkeypatch,role,quote,response):
    from test_cloud_first_agent import Planner
    j=page_context
    original_question='两个负数相乘为什么得到正数？'
    gateway=Planner('learning.explain',{'question':original_question,'subject':'数学'},original_question,role='question',objective_type='learning')
    j['gateway'].configured=True
    monkeypatch.setattr(j['gateway'],'complete',gateway.complete)
    opening=j['client'].post(f"/v1/sessions/{j['sid']}/agent-turn",json={
        'transcript':original_question,'client_action_id':'original-learning-goal','cloud_text_allowed':True})
    assert opening.status_code==200,opening.text
    with j['db'].connect() as c:
        before=[tuple(r) for r in c.execute('SELECT * FROM agent_objective WHERE child_id=?',(CHILD,))]
    assert before
    prompt=prepare(j['client'],j['sid'])['prompt']
    foreground_models(j,monkeypatch,quote,role=role,arguments={'response':response})
    result,_=ask(j,prompt,quote)
    assert result.status_code==200,result.text
    assert result.json()['response_text']=='我先回答你这次的问题。'
    assert result.json()['action']['source']=='agent_runtime_planner'
    executed=j['client'].post(f"/v1/agent-turns/{result.json()['agent_turn_id']}/execute")
    assert executed.status_code==200,executed.text
    with j['db'].connect() as c:
        assert [tuple(r) for r in c.execute('SELECT * FROM agent_objective WHERE child_id=?',(CHILD,))]==before
        assert c.execute('SELECT objective_id FROM agent_plan WHERE agent_turn_id=?',(result.json()['agent_turn_id'],)).fetchone()[0] is None
    assert_retained(j,prompt)


@pytest.mark.parametrize('violation', ['new_goal','await_reply','quote_swap','capability','continuation'])
def test_independent_planner_cannot_gain_answer_or_goal_authority(page_context,monkeypatch,violation):
    j=page_context; prompt=prepare(j['client'],j['sid'])['prompt']
    planner=foreground_models(j,monkeypatch)
    if violation=='new_goal':
        planner.payload['objective']={'mode':'new','type':'companion','summary':'new','success_criteria':['new'],'phase':'execute'}
    elif violation=='await_reply':
        planner.payload['requires_child_reply']=True
    elif violation=='quote_swap':
        planner.payload['utterance_binding']['quote']='PET学习资料'
    elif violation=='capability':
        planner.payload['next_step'].update(capability='observation.pause',arguments={})
    else:
        planner.payload['after_execution']='continue'
    result,_=ask(j,prompt)
    assert result.status_code==200,result.text
    assert result.json()['action']['type']=='none'
    assert result.json()['action']['source']=='cloud_planner_unavailable'
    assert_retained(j,prompt)


@pytest.mark.parametrize('timing', ['planning','execution'])
def test_original_prompt_cancellation_blocks_late_independent_result(page_context,monkeypatch,timing):
    j=page_context; prompt=prepare(j['client'],j['sid'])['prompt']
    foreground_models(j,monkeypatch)
    original=j['gateway'].complete
    def cancel():
        with j['db'].connect() as c:
            c.execute("UPDATE diagnostic_prompt SET status='cancelled' WHERE prompt_id=?",(prompt['prompt_id'],));c.commit()
    async def cancelled(request):
        value=await original(request)
        if request.request_type=='agent_plan':cancel()
        return value
    if timing=='planning':monkeypatch.setattr(j['gateway'],'complete',cancelled)
    result,_=ask(j,prompt)
    if timing=='planning':
        assert result.status_code==422,result.text
    else:
        assert result.status_code==200,result.text
        cancel()
        executed=j['client'].post(f"/v1/agent-turns/{result.json()['agent_turn_id']}/execute")
        assert executed.status_code in {409,422},executed.text
    with j['db'].connect() as c:
        assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0]==0


def test_current_hint_keeps_the_original_question_and_never_exports_it(page_context,monkeypatch):
    j=page_context; prompt=prepare(j['client'],j['sid'])['prompt']
    quote='这道题给我一个提示。'
    foreground_models(j,monkeypatch,quote,scope='current')
    result,_=ask(j,prompt,quote)
    assert result.status_code==200,result.text
    action=result.json()['action']
    assert action['type']=='ask_tutor',result.json()
    assert action['diagnostic_prompt_id']==prompt['prompt_id']
    assert action['diagnostic_item_version']==prompt['item_version']
    assert action['local_only'] and action['cloud_transmission_allowed'] is False
    assert not any(r.request_type=='agent_plan' for r in j['gateway'].requests)
    assert_retained(j,prompt)


def test_actual_answer_still_submits_to_original_prompt(page_context,monkeypatch):
    j=page_context; prompt=prepare(j['client'],j['sid'])['prompt']
    quote='答案是12。'
    foreground_models(j,monkeypatch,quote,role='answer',scope=None)
    result,_=ask(j,prompt,quote)
    assert result.status_code==200,result.text
    action=result.json()['action']
    assert action['type']=='submit_diagnostic_answer',result.json()
    assert action['diagnostic_prompt_id']==prompt['prompt_id']
    assert action['diagnostic_item_version']==prompt['item_version']
    assert not any(r.request_type=='agent_plan' for r in j['gateway'].requests)
    submitted=j['client'].post(f"/v1/sessions/{j['sid']}/diagnostic-prompt/answer",json={
        'prompt_id':action['diagnostic_prompt_id'],'item_version':action['diagnostic_item_version'],
        'answer':action['answer'],'client_action_id':'diagnostic-answer:'+prompt['prompt_id'],
        'authorized_turn_id':result.json()['agent_turn_id']})
    assert submitted.status_code==200,submitted.text
    with j['db'].connect() as c:
        assert c.execute('SELECT count(*) FROM diagnostic_attempt WHERE child_id=?',(CHILD,)).fetchone()[0]==1


def test_independent_explanation_binds_current_full_speech_not_pending_question(page_context,monkeypatch):
    j=page_context; prompt=prepare(j['client'],j['sid'])['prompt']
    quote='数学里两个负数相乘为什么得到正数？'
    foreground_models(j,monkeypatch,quote,capability='learning.explain',arguments={'question':quote,'subject':'数学'})
    result,_=ask(j,prompt,quote)
    assert result.status_code==200,result.text
    action=result.json()['action']
    assert action['type']=='ask_tutor' and action['question']==quote,result.json()
    assert not action.get('diagnostic_prompt_id') and not action.get('local_document_context')
    with j['db'].connect() as c:
        assert c.execute('SELECT objective_id FROM agent_plan WHERE agent_turn_id=?',(result.json()['agent_turn_id'],)).fetchone()[0] is None
    assert_retained(j,prompt)


def test_actual_tcp_sigkill_restart_preserves_pending_question_and_unique_receipt(page_context,tmp_path):
    from test_document_dialogue_process import test_tcp_process_restart_keeps_one_original_and_one_execution
    j=page_context; prompt=prepare(j['client'],j['sid'])['prompt']
    j['reference']={'kind':'diagnostic','binding':{k:prompt[k] for k in ('prompt_id','item_version')}}
    test_tcp_process_restart_keeps_one_original_and_one_execution(j,tmp_path)
    assert_retained(j,prompt)


@pytest.mark.parametrize('change', ['none','before_dispatch','after_response'])
def test_independent_explanation_runs_its_owned_tutor_request_without_original_sources(page_context,monkeypatch,change):
    j=page_context; prompt=prepare(j['client'],j['sid'])['prompt']
    quote='数学里两个负数相乘为什么得到正数？'
    foreground_models(j,monkeypatch,quote,capability='learning.explain',arguments={'question':quote,'subject':'数学'})
    result,_=ask(j,prompt,quote)
    assert result.status_code==200,result.text
    action=result.json()['action']
    assert action['tutor_request_action_id'].startswith('foreground-question:')
    from home_study_agent.foreground_independent_dialogue import validate_turn
    with j['db'].connect() as c:
        assert validate_turn(c,result.json()['agent_turn_id'])
    original=j['gateway'].complete
    def cancel():
        with j['db'].connect() as c:
            c.execute("UPDATE diagnostic_prompt SET status='cancelled' WHERE prompt_id=?",(prompt['prompt_id'],));c.commit()
    async def tutor(request):
        if request.request_type.startswith('tutor_hint_level_'):
            request.pre_dispatch()
            j['gateway'].requests.append(request)
            if change=='after_response':cancel()
            return ModelResult(content=json.dumps({'blackboard':{'title':'负数乘法','segments':[{
                'narration':'可以从乘法分配律理解。保持运算规律一致时，两个负数相乘就得到正数。',
                'blocks':[{'kind':'text','text':'负数乘负数得到正数，保持分配律一致。'}]}]}},ensure_ascii=False),
                provider='test',model='synthetic-tutor',latency_ms=1,prompt_tokens=20,completion_tokens=20)
        return await original(request)
    monkeypatch.setattr(j['gateway'],'complete',tutor)
    if change=='before_dispatch':cancel()
    body={'subject':action['subject'],'question':action['question'],'hint_level':action['hint_level'],
          'client_action_id':action['tutor_request_action_id']}
    if change=='none':
        from home_study_agent.foreground_independent_dialogue import tutor_request
        with j['db'].connect() as c:
            assert tutor_request(c,session_id=j['sid'],child_id=CHILD,action_id=body['client_action_id'],
                question=body['question'],subject=body['subject'],hint_level=body['hint_level']),action
    lesson=j['client'].post(f"/v1/sessions/{j['sid']}/tutor",json=body)
    if change=='none':
        assert lesson.status_code==200,lesson.text
        assert lesson.json()['question']==quote and lesson.json()['output_envelope']['result']['blackboard']['source']=='model_structured'
        assert_retained(j,prompt)
        j['client'].__exit__(None, None, None)
        with TestClient(create_app(j['settings'],model_gateway=j['gateway'])) as restarted:
            replay=restarted.post(f"/v1/sessions/{j['sid']}/tutor",json=body)
            assert replay.status_code==200 and replay.json()['turn_id']==lesson.json()['turn_id']
    else:
        assert lesson.status_code in {409,422},lesson.text
    tutor_requests=[r for r in j['gateway'].requests if r.request_type.startswith('tutor_hint_level_')]
    assert len(tutor_requests)==int(change!='before_dispatch')
    for request in tutor_requests:
        assert request.evidence_refs==()
        assert request.context_manifest['lane']=='foreground_independent_tutor_v1'
        exported=json.dumps({'messages':request.messages,'manifest':request.context_manifest},ensure_ascii=False)
        for private in [prompt['prompt_id'],prompt['item_version'],CHILD,j['sid'],j['original']['artifact_id'],'Calculate (24 + 6) x 3']:
            assert private not in exported
    with j['db'].connect() as c:
        assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0]==0


@pytest.mark.parametrize('violation',['board_question','board_text_question','spoken_question','request_reply','length_rewrite'])
def test_independent_tutor_cannot_open_another_answer_slot(page_context,monkeypatch,violation):
    j=page_context; prompt=prepare(j['client'],j['sid'])['prompt']
    quote='数学里两个负数相乘为什么得到正数？'
    foreground_models(j,monkeypatch,quote,capability='learning.explain',arguments={'question':quote,'subject':'数学'})
    result,_=ask(j,prompt,quote)
    assert result.status_code==200,result.text
    action=result.json()['action']; calls=[]
    async def tutor(request):
        request.pre_dispatch();calls.append(request)
        narration={'board_question':'两个负数相乘得到正数。',
                   'board_text_question':'两个负数相乘得到正数。',
                   'spoken_question':'两个负数相乘得到正数。你理解了吗？',
                   'request_reply':'两个负数相乘得到正数。请回答一个例子。',
                   'length_rewrite':'两个负数相乘得到正数。'*29}[violation]
        segment={
            'narration':narration,'blocks':[{'kind':'question' if violation=='board_question' else 'text',
                                         'text':'你理解了吗？' if violation=='board_text_question' else '负数乘法'}]}
        return ModelResult(content=json.dumps({'blackboard':{'title':'负数乘法','segments':
            [segment]*3 if violation=='length_rewrite' else [segment]}},ensure_ascii=False),
            provider='test',model='synthetic-tutor',latency_ms=1,prompt_tokens=20,completion_tokens=20)
    monkeypatch.setattr(j['gateway'],'complete',tutor)
    body={'subject':action['subject'],'question':action['question'],'hint_level':action['hint_level'],
          'client_action_id':action['tutor_request_action_id']}
    lesson=j['client'].post(f"/v1/sessions/{j['sid']}/tutor",json=body)
    assert lesson.status_code==409,lesson.text
    assert lesson.json()['code']=='FOREGROUND_TUTOR_REPLY_SCOPE_CHANGED'
    assert lesson.json()['recovery_action']=='resume_original_question'
    assert lesson.json()['retryable'] is False
    assert_retained(j,prompt)
    with j['db'].connect() as c:
        row=c.execute('SELECT status,last_error_code FROM tutor_request_reservation WHERE action_id=?',
                      (body['client_action_id'],)).fetchone()
        assert row['status']=='failed' and row['last_error_code']=='FOREGROUND_TUTOR_REPLY_SCOPE_CHANGED'
        assert c.execute('SELECT count(*) FROM tutor_turn').fetchone()[0]==0
    j['client'].__exit__(None, None, None)
    with TestClient(create_app(j['settings'],model_gateway=j['gateway'])) as restarted:
        replay=restarted.post(f"/v1/sessions/{j['sid']}/tutor",json=body)
        assert replay.status_code!=200,replay.text
    assert len(calls)==1


def test_model_failure_preserves_original_speech_without_automatic_reissue(page_context,monkeypatch):
    j=page_context; prompt=prepare(j['client'],j['sid'])['prompt']
    foreground_models(j,monkeypatch,fail_plan=True)
    result,body=ask(j,prompt)
    assert result.status_code==200,result.text
    assert result.json()['action']['source']=='cloud_planner_unavailable'
    j['client'].__exit__(None, None, None)
    with TestClient(create_app(j['settings'],model_gateway=j['gateway'])) as restarted:
        replay=restarted.post(f"/v1/sessions/{j['sid']}/agent-turn",json=body)
        assert replay.status_code==200 and replay.json()['agent_turn_id']==result.json()['agent_turn_id']
    assert len(j['gateway'].requests)==2
    assert_retained(j,prompt)


@pytest.mark.parametrize('change',['step_arguments','semantic_role','text_consent'])
def test_execution_revalidates_stored_semantic_and_source_authority(page_context,monkeypatch,change):
    j=page_context; prompt=prepare(j['client'],j['sid'])['prompt']
    foreground_models(j,monkeypatch)
    result,_=ask(j,prompt)
    assert result.status_code==200,result.text
    turn_id=result.json()['agent_turn_id']
    with j['db'].connect() as c:
        if change=='step_arguments':
            c.execute("UPDATE agent_step SET arguments_json=? WHERE plan_id IN (SELECT plan_id FROM agent_plan WHERE agent_turn_id=?)",
                (json.dumps({'scope':'plan','track':'all'}),turn_id))
        elif change=='semantic_role':
            c.execute("UPDATE voice_control_intent_request SET decision_json=? WHERE session_id=?",
                (json.dumps({'status':'non_control','role':'answer','role_version':'spoken_answer_role_v1'}),j['sid']))
        else:
            c.execute("UPDATE privacy_preference SET cloud_model_text_allowed=0 WHERE singleton_id=1")
        c.commit()
    execution=j['client'].post(f"/v1/agent-turns/{turn_id}/execute")
    assert execution.status_code in {409,422},execution.text
    assert_retained(j,prompt)


def test_explicit_not_an_answer_can_still_ask_an_independent_question(page_context,monkeypatch):
    j=page_context; prompt=prepare(j['client'],j['sid'])['prompt']
    quote='别把这句话算作答案，你有PET学习资料吗？'
    foreground_models(j,monkeypatch,quote)
    result,_=ask(j,prompt,quote)
    assert result.status_code==200,result.text
    assert result.json()['intent']=='system.inspect_learning',result.json()
    assert_retained(j,prompt)
