"""Neutral first check uses real Gateway transport and persisted public records."""
import asyncio
import json
import socket
from pathlib import Path

import httpx
import pytest
from normal_provider_owner import normal_provider_owner
from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.model_gateway import ModelGateway
from home_study_agent.repository import StudyRepository
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from home_study_agent.mistake_learning import MistakeLearningService,MistakeLearningError
from home_study_agent.mistake_learning_intake import MistakeLearningIntake

import home_study_agent.mistake_learning as _main_learning
assert Path(_main_learning.__file__).resolve()==Path(__file__).resolve().parents[1]/"src/home_study_agent/mistake_learning.py", "MAIN_SOURCE_IMPORT_REQUIRED"

CHILD='child-junior-3'
QUESTION='计算2/5+1/10，说明相加时分数单位为什么需要一致。'
FACTS='INTERNAL_FACT_CHECK：分母表示单位份的大小，不同分母应先统一单位再相加。'

@pytest.fixture
def flow(tmp_path,normal_provider_owner,monkeypatch):
    network=[]
    def deny(*a,**kw):network.append(True);raise AssertionError('EXTERNAL_NETWORK_FORBIDDEN')
    monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket.socket,'connect_ex',deny)
    db=Database(tmp_path/'public-neutral.db');db.initialize();repo=StudyRepository(db);repo.seed_family()
    sid=repo.start_session(CHILD,'public-neutral-sid')['session_id']
    privacy=PrivacyLifecycleService(db,tmp_path);privacy.initialize();privacy.update_preferences({'cloud_model_text_allowed':True})
    requests=[];identities=[]
    controls={'check':{'claim_status':'incorrect','facts':FACTS},'check_http':False,'coach_http':False,'after_check':None,'after_coach':None,'coach_text':'先核对分数单位，不能直接肯定分母相加。题目给了哪两个分数？'}
    class ObservedGateway(ModelGateway):
        async def complete(self,req):
            identities.append((req.request_id,req.request_type))
            return await super().complete(req)
    def transport(req):
        payload=json.loads(req.content);requests.append(payload);instruction=payload['messages'][0]['content']
        if '中立事实核查员' in instruction:
            if controls['after_check']:controls['after_check']()
            if controls['check_http']:return httpx.Response(503,request=req)
            value=controls['check']
        elif '逐步讲解核对员' in instruction:value={'valid':True}
        elif 'wrong_causes' in instruction:value={'summary':'OLD_ANALYSIS','wrong_causes':[{'label':'原题概念','explanation':'这是OLD_ANALYSIS原题分析。'}],'knowledge_points':[{'label':'分数单位','description':'先统一单位。'}],'correction_strategy':['先通分。']}
        elif 'candidates' in instruction:value={'candidates':[
            {'stage':'near_transfer','prompt':QUESTION,'expected_answer':{'exact_answers':['1/2'],'answer_format':'rational'},'explanation':'通分后相加得到5/10，约分为1/2。','difficulty':1},
            {'stage':'representation_transfer','prompt':'把3/8与1/8相加后得到的分数画成口头描述的线段，并写出最简分数。','expected_answer':{'exact_answers':['1/2'],'answer_format':'rational'},'explanation':'相同单位的份数相加是4/8，约分为1/2。','difficulty':1}]}
        elif '独立题目核对员' in instruction:
            value={'checks':[{'position':i+1,'valid':True} for i in range(len(json.loads(payload['messages'][1]['content'])))]}
        else:
            if controls['after_coach']:controls['after_coach']()
            if controls['coach_http']:return httpx.Response(503,request=req)
            value={'phase':'question','text':controls['coach_text']}
        return httpx.Response(200,request=req,json={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(value,ensure_ascii=False)}}],
            'usage':{'prompt_tokens':20,'completion_tokens':30}})
    gateway=ObservedGateway(Settings(data_dir=tmp_path,database_path=db.path,model_api_key='public-synthetic'),transport=httpx.MockTransport(transport))
    service=MistakeLearningService(db,gateway);intake=MistakeLearningIntake(db)
    mid=intake.create(child_id=CHILD,session_id=sid,question='计算3/4+1/2，我原来写4/6。',submitted_answer='4/6',subject='数学',client_action_id='public-neutral-case')['mistake_id']
    yield db,service,mid,sid,privacy,requests,identities,controls
    asyncio.run(gateway.close());assert network==[]


def call(f,key,utterance='我认为分母直接相加总是正确',**kw):
    _,service,mid,sid,*_=f
    return asyncio.run(service.coach(mid,child_id=CHILD,session_id=sid,client_action_id=key,utterance=utterance,**kw))


def test_two_phases_neutral_payload_private_trace_and_false_claim_constraints(flow,capsys):
    db,service,mid,sid,privacy,requests,identities,controls=flow
    asyncio.run(service.analyze(mid,child_id=CHILD,session_id=sid,client_action_id='analysis'))
    p=asyncio.run(service.practice(mid,child_id=CHILD,session_id=sid,client_action_id='practice',count=2))
    item=p['practice_items'][0]['practice_item_id']
    controls['coach_text']='OLD_AI_HISTORY 这一步先做什么？'
    call(flow,'old-coach','OLD_USER_HISTORY',practice_item_id=item)
    controls['coach_text']='不能直接把分母相加。先想想分母表示什么单位？'
    # Intermediate provider_json must not turn a truly active call into abandoned work.
    controls['after_coach']=lambda: (service.read_action('new-coach',child_id=CHILD,mistake_id=mid)['action_status']=='processing') or (_ for _ in ()).throw(AssertionError('ACTIVE_OWNER_LOST'))
    before=len(requests)
    result=call(flow,'new-coach','FALSE_STUDENT_CLAIM',practice_item_id=item)
    assert result['action_status']=='completed',result
    assert len(requests)==before+2
    check_body=json.loads(requests[-2]['messages'][1]['content']);coach_body=json.loads(requests[-1]['messages'][1]['content'])
    assert set(check_body)=={'question','utterance'}
    assert check_body=={'question':QUESTION,'utterance':'FALSE_STUDENT_CLAIM'}
    assert all(marker not in json.dumps(check_body) for marker in ('OLD_AI_HISTORY','OLD_ANALYSIS','OLD_USER_HISTORY','expected_answer','explanation','1/2','4/6'))
    assert set(coach_body)=={'question','utterance','style','history','coaching_step','previous_phase','independent_practice_allowed','model_check'}
    assert coach_body['model_check']=={'claim_status':'incorrect','facts':FACTS}
    assert 'incorrect时温和点出真实错误或引导孩子核对' in requests[-1]['messages'][0]['content']
    assert '不能顺着错误前提讲' in requests[-1]['messages'][0]['content']
    assert identities[-2:]==[('new-coach:coach-check','mistake_learning_verify'),('new-coach','mistake_learning_coach')]
    assert FACTS not in json.dumps(result,ensure_ascii=False)
    with db.connect() as c:
        value=json.loads(c.execute('SELECT provider_json FROM mistake_learning_action WHERE client_action_id=?',('new-coach',)).fetchone()[0])
        assert value['coach_fact_check']['result']==coach_body['model_check']
    assert FACTS not in capsys.readouterr().out


@pytest.mark.parametrize('change',['revoke','source'])
def test_phase_source_or_permission_change_stops_before_coach_and_keeps_original(flow,change):
    db,service,mid,sid,privacy,requests,identities,controls=flow
    def changed():
        if change=='revoke':privacy.update_preferences({'cloud_model_text_allowed':False})
        else:
            with db.connect() as c:c.execute('UPDATE mistake_learning_card SET prompt_summary=? WHERE mistake_id=?',('全新的公开题干',mid));c.commit()
    controls['after_check']=changed
    try:result=call(flow,'changed-between-phases','PRESERVE_ORIGINAL')
    except MistakeLearningError as e:assert e.code=='MISTAKE_LEARNING_CLOUD_CONSENT_REQUIRED'
    else:assert result['action_status']=='failed'
    assert len(requests)==1
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM mistake_learning_turn WHERE client_action_id=? AND role=? AND text=?',('changed-between-phases','user','PRESERVE_ORIGINAL')).fetchone()[0]==1
        assert c.execute('SELECT count(*) FROM mistake_learning_turn WHERE client_action_id=? AND role=?',('changed-between-phases','assistant')).fetchone()[0]==0


@pytest.mark.parametrize('phase',['check','coach'])
def test_unknown_phase_never_retries_either_stage_on_same_packet(flow,phase):
    db,service,mid,sid,privacy,requests,identities,controls=flow
    controls[phase+'_http']=True
    result=call(flow,'unknown-phase')
    assert result['action_status']=='unknown'
    expected=1 if phase=='check' else 2
    assert len(requests)==expected
    assert call(flow,'unknown-phase')['action_status']=='unknown'
    assert len(requests)==expected
    with db.connect() as c:
        row=c.execute('SELECT provider_json FROM mistake_learning_action WHERE client_action_id=?',('unknown-phase',)).fetchone()
        metadata=json.loads(row[0])
        if phase=='coach':assert metadata['coach_fact_check']['result']==controls['check']
        assert c.execute('SELECT count(*) FROM mistake_learning_turn WHERE role=?',('user',)).fetchone()[0]==1


def test_one_shared_deadline_and_uncertain_only_clarification_contract(flow,monkeypatch):
    import home_study_agent.mistake_learning as module
    db,service,mid,sid,privacy,requests,identities,controls=flow
    controls['check']={'claim_status':'uncertain','facts':'条件没有说明清楚，需要先澄清。'}
    timeouts=[];original=asyncio.wait_for
    async def bounded(coro,timeout):
        if getattr(getattr(coro,'cr_code',None),'co_name',None)=='complete':timeouts.append(timeout)
        return await original(coro,timeout)
    monkeypatch.setattr(module.asyncio,'wait_for',bounded)
    result=call(flow,'uncertain-two-phase','条件还没说清楚')
    assert result['action_status']=='completed'
    assert len(timeouts)==2 and 0<timeouts[1]<timeouts[0]<=65
    body=json.loads(requests[-1]['messages'][1]['content'])
    assert body['model_check']['claim_status']=='uncertain'
    assert 'uncertain时只问一个缺失条件' in requests[-1]['messages'][0]['content']


def test_malformed_neutral_result_is_closed_without_teacher_fallback(flow):
    db,service,mid,sid,privacy,requests,identities,controls=flow
    controls['check']={'claim_status':'looks_good','facts':'PRIVATE_INVALID_FACT'}
    result=call(flow,'invalid-neutral')
    assert result['action_status']=='failed'
    assert result['error_code']=='MISTAKE_LEARNING_COACH_CHECK_INVALID'
    assert len(requests)==1
    assert 'PRIVATE_INVALID_FACT' not in json.dumps(result,ensure_ascii=False)


def test_no_claim_with_empty_facts_can_ask_opening_question_and_stays_private(flow):
    db,service,mid,sid,privacy,requests,identities,controls=flow
    controls['check']={'claim_status':'no_claim','facts':''}
    result=call(flow,'opening-without-claim','这题我先怎么想？')
    assert result['action_status']=='completed',result
    assert identities==[('opening-without-claim:coach-check','mistake_learning_verify'),('opening-without-claim','mistake_learning_coach')]
    assert json.loads(requests[-1]['messages'][1]['content'])['model_check']==controls['check']
    assert 'no_claim不等于已经理解' in requests[-1]['messages'][0]['content']
    with db.connect() as c:
        saved=json.loads(c.execute('SELECT provider_json FROM mistake_learning_action WHERE client_action_id=?',('opening-without-claim',)).fetchone()[0])
        assert saved['coach_fact_check']['result']==controls['check']
    assert 'model_check' not in json.dumps(result,ensure_ascii=False)


@pytest.mark.parametrize('claim_status',['correct','incorrect','uncertain'])
def test_claim_or_uncertainty_still_requires_nonempty_facts(flow,claim_status):
    db,service,mid,sid,privacy,requests,identities,controls=flow
    controls['check']={'claim_status':claim_status,'facts':''}
    result=call(flow,'empty-facts-'+claim_status)
    assert result['action_status']=='failed'
    assert result['error_code']=='MISTAKE_LEARNING_COACH_CHECK_INVALID'
    assert identities==[('empty-facts-'+claim_status+':coach-check','mistake_learning_verify')]
    assert len(requests)==1
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM mistake_learning_turn WHERE client_action_id=? AND role=?',('empty-facts-'+claim_status,'assistant')).fetchone()[0]==0
