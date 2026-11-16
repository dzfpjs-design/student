"""Actual exam state/HTTP; independent semantic replies are synthetic."""
import json
import pytest
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.exam_simulation import ExamSimulationService
from test_exam_bound_runtime_http import environment, start
from test_pet_vocabulary_prompt_integration import Planner
from test_document_voice_capture import begin, complete
from test_foreground_independent_dialogue import foreground_models


@pytest.mark.parametrize('role,quote,capability,arguments',[
    ('question','考试结束以后有哪些PET学习资料可以用？','system.inspect_learning',{'scope':'resources','track':'pet'}),
    ('greeting','小知你好，今天见到你很开心。','conversation.respond',{'response':'很高兴见到你。'}),
])
def test_exam_independent_reply_keeps_original_slot_and_clock(environment,monkeypatch,role,quote,capability,arguments):
    settings,db,sid=environment;gateway=Planner()
    with TestClient(create_app(settings,model_gateway=gateway)) as client:
        service=ExamSimulationService(db);run=start(service,sid)
        original=service.arrival_binding(sid)
        with db.connect() as c:
            before=dict(c.execute('SELECT * FROM specialized_exam_run WHERE run_id=?',(run['run_id'],)).fetchone())
        j={'settings':settings,'db':db,'sid':sid,'client':client,'gateway':gateway}
        foreground_models(j,monkeypatch,quote,role=role,capability=capability,arguments=arguments)
        accepted=begin(j,reference={'kind':'exam','binding':original})
        assert accepted.status_code==200,accepted.text
        voice=complete(j,text=quote)
        body={'transcript':quote,'voice_input_id':voice['voice_input_id'],
              'client_action_id':'independent-exam-'+role,'cloud_text_allowed':True}
        result=client.post(f'/v1/sessions/{sid}/agent-turn',json=body)
        assert result.status_code==200,result.text
        assert service.arrival_binding(sid)==original
        with db.connect() as c:
            after=dict(c.execute('SELECT * FROM specialized_exam_run WHERE run_id=?',(run['run_id'],)).fetchone())
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0]==0
        for key in ('current_position','started_at','active_since','time_limit_seconds','elapsed_seconds','status'):
            assert before[key]==after[key]
        if role=='question':
            assert result.json()['action']['type']=='inspect_learning_system',result.json()['action']
            assert result.json()['action']['scope']=='resources'
            assert result.json()['action']['track']=='pet'
        else:
            assert result.json()['action']['source']=='agent_runtime_planner',result.json()['action']
        execution=client.post(f"/v1/agent-turns/{result.json()['agent_turn_id']}/execute")
        assert execution.status_code==200,execution.text
        if role=='greeting':
            assert execution.json()['execution']['response_text']=='很高兴见到你。'
        planned_request=next(request for request in gateway.requests if request.request_type=='agent_plan')
        payload=json.loads(planned_request.messages[1]['content'])
        assert {entry['name'] for entry in payload['available_capabilities']}==(
            {'system.inspect_learning'} if role=='question' else {'conversation.respond','system.inspect_learning'})
        client.__exit__(None, None, None)
        with TestClient(create_app(settings,model_gateway=gateway)) as restarted:
            replay=restarted.post(f"/v1/agent-turns/{result.json()['agent_turn_id']}/execute")
            assert replay.status_code==200 and replay.json()['execution']==execution.json()['execution']
        for request in gateway.requests:
            exported=json.dumps({'messages':request.messages,'manifest':request.context_manifest,'refs':request.evidence_refs})
            for private in (sid,original['run_id'],original['item_id'],original['source_version']):
                assert private not in exported
        assert len(gateway.requests)==2


@pytest.fixture
def exam_context(environment):
    settings,db,sid=environment;gateway=Planner()
    with TestClient(create_app(settings,model_gateway=gateway)) as client:
        service=ExamSimulationService(db);run=start(service,sid)
        yield {'settings':settings,'db':db,'sid':sid,'client':client,'gateway':gateway,
               'service':service,'run':run,'binding':service.arrival_binding(sid)}


def ask(j,quote,key='independent-exam-probe'):
    assert begin(j,reference={'kind':'exam','binding':j['binding']}).status_code==200
    voice=complete(j,text=quote)
    return j['client'].post(f"/v1/sessions/{j['sid']}/agent-turn",json={
        'transcript':quote,'voice_input_id':voice['voice_input_id'],'client_action_id':key,'cloud_text_allowed':True})


def change_owner(j,change):
    if change=='cancel':
        j['service'].cancel(j['run']['run_id'])
    elif change=='pause':
        j['service'].pause(j['run']['run_id'])
    elif change=='replacement':
        j['service'].cancel(j['run']['run_id']);start(j['service'],j['sid'],'new-original-exam')
    else:
        with j['db'].connect() as c:
            if change=='source':
                c.execute("UPDATE diagnostic_item SET prompt=prompt || ' revised' WHERE diagnostic_item_id=?",
                          (j['binding']['item_id'],))
            elif change=='timeout':
                c.execute('UPDATE specialized_exam_run SET elapsed_seconds=time_limit_seconds WHERE run_id=?',
                          (j['run']['run_id'],))
            c.commit()


@pytest.mark.parametrize('change',['cancel','pause','replacement','source','timeout'])
def test_exam_changed_owner_rejects_late_independent_execution(exam_context,monkeypatch,change):
    j=exam_context;quote='考试结束以后有哪些PET学习资料可以用？'
    foreground_models(j,monkeypatch,quote,capability='system.inspect_learning',arguments={'scope':'resources','track':'pet'})
    planned=ask(j,quote)
    assert planned.status_code==200,planned.text
    assert planned.json()['action']['type']=='inspect_learning_system',planned.json()['action']
    change_owner(j,change)
    executed=j['client'].post(f"/v1/agent-turns/{planned.json()['agent_turn_id']}/execute")
    assert executed.status_code in {409,422},executed.text
    with j['db'].connect() as c:
        assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0]==0


@pytest.mark.parametrize('capability',['learning.explain','conversation.respond'])
def test_exam_cannot_launch_an_independent_tutor(exam_context,monkeypatch,capability):
    j=exam_context;quote='请先帮我讲一下两个负数相乘为什么是正数。'
    foreground_models(j,monkeypatch,quote,capability=capability,arguments=(
        {'question':quote,'subject':'数学'} if capability=='learning.explain' else {'response':'负数乘负数得到正数。'}))
    planned=ask(j,quote)
    assert planned.status_code==200,planned.text
    assert planned.json()['action']['type']=='none',planned.json()['action']
    direct=j['client'].post(f"/v1/sessions/{j['sid']}/tutor",json={
        'subject':'数学','question':quote,'hint_level':6,'client_action_id':'foreground-question:forged-exam'})
    assert direct.status_code in {409,422},direct.text
    with j['db'].connect() as c:
        assert c.execute('SELECT count(*) FROM tutor_turn').fetchone()[0]==0
        assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0]==0
    assert j['service'].arrival_binding(j['sid'])==j['binding']


def test_exam_original_answer_still_submits_after_independent_reply(exam_context,monkeypatch):
    j=exam_context;quote='考试结束以后有哪些PET学习资料可以用？'
    foreground_models(j,monkeypatch,quote,capability='system.inspect_learning',arguments={'scope':'resources','track':'pet'})
    result=ask(j,quote)
    assert result.status_code==200,result.text
    executed=j['client'].post(f"/v1/agent-turns/{result.json()['agent_turn_id']}/execute")
    assert executed.status_code==200,executed.text
    answered=j['client'].post(f"/v1/sessions/{j['sid']}/exam-simulations/bound-answer",json={
        **j['binding'],'answer':'我的答案是十三','client_action_id':'original-exam-answer-after-independent'})
    assert answered.status_code==200,answered.text
    assert j['service'].arrival_binding(j['sid'])['position']==2
    j['client'].__exit__(None, None, None)
    with TestClient(create_app(j['settings'],model_gateway=j['gateway'])) as restarted:
        replay=restarted.post(f"/v1/agent-turns/{result.json()['agent_turn_id']}/execute")
        assert replay.status_code==200 and replay.json()['execution']==executed.json()['execution']
    with j['db'].connect() as c:
        assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0]==1
        assert c.execute('SELECT submitted_answer FROM diagnostic_attempt').fetchone()[0]=='我的答案是十三'
    assert len(j['gateway'].requests)==2


@pytest.mark.parametrize('change',['cancel','source','timeout'])
def test_exam_changes_during_planning_do_not_create_an_independent_plan(exam_context,monkeypatch,change):
    j=exam_context;quote='考试结束以后有哪些PET学习资料可以用？'
    foreground_models(j,monkeypatch,quote,capability='system.inspect_learning',arguments={'scope':'resources','track':'pet'})
    original=j['gateway'].complete
    async def changed(request):
        if request.request_type=='agent_plan':change_owner(j,change)
        return await original(request)
    monkeypatch.setattr(j['gateway'],'complete',changed)
    result=ask(j,quote)
    assert result.status_code in {409,422},result.text
    with j['db'].connect() as c:
        assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0]==0
        assert c.execute('SELECT count(*) FROM agent_plan').fetchone()[0]==0


def test_independent_reply_returns_to_the_original_canonical_voice_answer(exam_context,monkeypatch):
    j=exam_context;quote='考试结束以后有哪些PET学习资料可以用？'
    foreground_models(j,monkeypatch,quote,capability='system.inspect_learning',arguments={'scope':'resources','track':'pet'})
    independent=ask(j,quote)
    assert independent.status_code==200,independent.text
    executed=j['client'].post(f"/v1/agent-turns/{independent.json()['agent_turn_id']}/execute")
    assert executed.status_code==200,executed.text
    answer='我的答案是十三'
    j['gateway'].select('exam.submit',answer,role='answer',arguments={'answer':answer})
    monkeypatch.setattr(j['gateway'],'complete',Planner.complete.__get__(j['gateway'],Planner))
    key='canonical-exam-answer-after-independent'
    assert begin(j,key=key,reference={'kind':'exam','binding':j['binding']}).status_code==200
    voice=complete(j,key=key,text=answer)
    planned=j['client'].post(f"/v1/sessions/{j['sid']}/agent-turn",json={
        'transcript':answer,'voice_input_id':voice['voice_input_id'],'client_action_id':key+'-turn','cloud_text_allowed':True})
    assert planned.status_code==200,planned.text
    assert planned.json()['action']['type']=='submit_exam_answer',planned.json()['action']
    scored=j['client'].post(f"/v1/agent-turns/{planned.json()['agent_turn_id']}/execute")
    assert scored.status_code==200,scored.text
    assert j['service'].arrival_binding(j['sid'])['position']==2
    with j['db'].connect() as c:
        saved=c.execute('SELECT submitted_answer FROM diagnostic_attempt').fetchall()
        # The existing local explicit-answer parser removes only the spoken
        # command prefix. Full canonical speech and the turn remain immutable.
        assert [row[0] for row in saved]==['十三']
        assert c.execute('SELECT transcript FROM voice_input WHERE voice_input_id=?',
                         (voice['voice_input_id'],)).fetchone()[0]==answer
        assert c.execute('SELECT transcript FROM agent_turn WHERE agent_turn_id=?',
                         (planned.json()['agent_turn_id'],)).fetchone()[0]==answer
    j['client'].__exit__(None, None, None)
    with TestClient(create_app(j['settings'],model_gateway=j['gateway'])) as restarted:
        repeated=restarted.post(f"/v1/agent-turns/{planned.json()['agent_turn_id']}/execute")
        assert repeated.status_code==200 and repeated.json()['execution']==scored.json()['execution']
    assert len(j['gateway'].requests)==2
