"""Outgoing actual Gateway payload, public SQLite sources, no cloud egress."""
import asyncio
import json
import os
import socket
from pathlib import Path

import home_study_agent
import httpx
import pytest
from normal_provider_owner import normal_provider_owner

from home_study_agent.database import Database
from home_study_agent.config import Settings
from home_study_agent.model_gateway import ModelGateway
from home_study_agent.repository import StudyRepository
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from home_study_agent.mistake_learning import MistakeLearningService
from home_study_agent.mistake_learning_intake import MistakeLearningIntake

CHILD='child-junior-3'
ORIGINAL='计算3/4+1/2，我写4/6，觉得把分子分母直接相加。'
CURRENT='计算2/5+1/10，先通分，再把结果写成最简分数。'
OTHER='把3/8+1/8的结果画成一条线段，并写出最简分数。'
ANALYSIS={'summary':'ORIGINAL_ANALYSIS：先理解分数单位。','wrong_causes':[{'label':'原题单位','explanation':'原口述明确写了4/6，需要核对分数单位。'}],
 'knowledge_points':[{'label':'通分','description':'把不同单位转成相同单位。'}],'correction_strategy':['先通分再相加。']}
CANDIDATES=[{'stage':'near_transfer','prompt':CURRENT,'expected_answer':{'exact_answers':['1/2'],'answer_format':'rational'},'explanation':'通分后4/10+1/10=5/10，约分得到1/2。','difficulty':1},
 {'stage':'representation_transfer','prompt':OTHER,'expected_answer':{'exact_answers':['1/2'],'answer_format':'rational'},'explanation':'同分母相加得到4/8，约分得到1/2。','difficulty':1}]
FEEDBACK={'judgement':'correct','feedback':'本题的通分和约分结果正确。','explanation':'2/5与1/10相加，通分后得到5/10，也就是1/2。','reference_answer':'1/2','next_hint':'检查两项相加时单位是否相同。'}

@pytest.fixture
def learning(tmp_path,normal_provider_owner,monkeypatch):
    sockets=[]
    def deny(*args,**kw):sockets.append(True);raise AssertionError('EXTERNAL_NETWORK_FORBIDDEN')
    monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket.socket,'connect_ex',deny)
    db=Database(tmp_path/'public-current-material.db');db.initialize()
    repo=StudyRepository(db);repo.seed_family();sid=repo.start_session(CHILD,'public-current-sid')['session_id']
    privacy=PrivacyLifecycleService(db,tmp_path);privacy.initialize();privacy.update_preferences({'cloud_model_text_allowed':True})
    requests=[]
    def transport(request):
        body=json.loads(request.content);requests.append(body)
        instruction=body['messages'][0]['content'];context=json.loads(body['messages'][1]['content'])
        if '中立事实核查员' in instruction:value={'claim_status':'no_claim','facts':'没有可核实的断言时，先按当前题干澄清。'}
        elif '逐步讲解核对员' in instruction:value={'valid':True}
        elif '独立题目核对员' in instruction:value={'checks':[{'position':i+1,'valid':True} for i in range(len(context))]}
        elif 'wrong_causes' in instruction:value=ANALYSIS
        elif 'candidates' in instruction:value={'candidates':CANDIDATES}
        elif 'judgement' in instruction:value=FEEDBACK
        else:value={'phase':'question','text':'这道题的关键条件是什么？'}
        return httpx.Response(200,request=request,json={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(value,ensure_ascii=False)}}],
          'usage':{'prompt_tokens':20,'completion_tokens':30}})
    gateway=ModelGateway(Settings(data_dir=tmp_path,database_path=db.path,model_api_key='public-synthetic'),transport=httpx.MockTransport(transport))
    service=MistakeLearningService(db,gateway);intake=MistakeLearningIntake(db)
    mid=intake.create(child_id=CHILD,session_id=sid,question=ORIGINAL,submitted_answer='4/6',subject='数学',client_action_id='public-original')['mistake_id']
    yield db,service,intake,mid,sid,requests
    asyncio.run(gateway.close());assert sockets==[]


def call(world,operation,key,**kw):
    _,service,_,mid,sid,_=world
    return asyncio.run(getattr(service,operation)(mid,child_id=CHILD,session_id=sid,client_action_id=key,**kw))


def test_answer_transport_has_only_frozen_current_item_and_same_item_dialogue(learning):
    db,service,intake,mid,sid,requests=learning
    call(learning,'coach','original-dialogue',utterance='ORIGINAL_ONLY_DIALOGUE')
    call(learning,'analyze','original-analysis')
    items=call(learning,'practice','two-practices',count=2)['practice_items']
    call(learning,'coach','other-item-dialogue',practice_item_id=items[1]['practice_item_id'],utterance='OTHER_ITEM_DIALOGUE')
    call(learning,'coach','current-item-dialogue',practice_item_id=items[0]['practice_item_id'],utterance='SAME_ITEM_DIALOGUE')
    result=call(learning,'answer','current-answer',practice_item_id=items[0]['practice_item_id'],utterance='1/2')
    assert result['action_status']=='completed'
    wire=requests[-1];context=json.loads(wire['messages'][1]['content'])
    assert set(context)=={'practice','utterance','history'}
    assert context['practice']['question']==CURRENT
    assert context['practice']['expected_answer']['exact_answers']==['1/2']
    assert isinstance(context['practice']['expected_answer'],dict)
    assert context['utterance']=='1/2'
    serialized=json.dumps(context,ensure_ascii=False)
    assert 'SAME_ITEM_DIALOGUE' in serialized
    assert all(text not in serialized for text in (ORIGINAL,OTHER,'4/6','ORIGINAL_ANALYSIS','ORIGINAL_ONLY_DIALOGUE','OTHER_ITEM_DIALOGUE'))
    assert all(text not in wire['messages'][0]['content'] for text in ('缺少原答时','只使用本次选中题目和原答'))
    assert result['practice_items'][0]['feedback']['reference_answer']=='1/2'
    assert result['today']['completed_count']==1


def test_current_coach_payload_and_saved_auto_selected_question_are_scoped(learning):
    db,service,intake,mid,sid,requests=learning
    call(learning,'analyze','analyze')
    items=call(learning,'practice','practice',count=2)['practice_items']
    result=call(learning,'coach','auto-current-coach',utterance='AUTO_CURRENT_GUIDANCE',style='socratic')
    assert result['action_status']=='completed'
    context=json.loads(requests[-1]['messages'][1]['content'])
    assert set(context)=={'question','utterance','history','style','coaching_step','previous_phase','independent_practice_allowed','model_check'}
    assert context['question']==CURRENT
    assert context['model_check']['claim_status']=='no_claim'
    assert context['style']=='socratic'
    assert '只问一个最能推进的关键问题' in requests[-1]['messages'][0]['content']
    assert '不能顺着错误前提讲' in requests[-1]['messages'][0]['content']
    assert ORIGINAL not in json.dumps(context,ensure_ascii=False)
    with db.connect() as c:
        saved=c.execute('SELECT practice_item_id,text FROM mistake_learning_turn WHERE client_action_id=? AND role=?',('auto-current-coach','user')).fetchone()
        assert saved['practice_item_id']==items[0]['practice_item_id']
        assert saved['text']=='AUTO_CURRENT_GUIDANCE'


def test_capture_original_answer_in_spoken_question_not_independently_entered_is_not_missing(learning):
    db,service,intake,mid,sid,requests=learning
    card=intake.create(child_id=CHILD,session_id=sid,question=ORIGINAL,submitted_answer='',source_type='spoken',client_action_id='public-voice-capture')
    result=asyncio.run(service.analyze(card['mistake_id'],child_id=CHILD,session_id=sid,client_action_id='captured-analysis'))
    assert result['action_status']=='completed'
    context=json.loads(requests[-1]['messages'][1]['content']);instruction=requests[-1]['messages'][0]['content']
    assert context['source']['question']==ORIGINAL
    assert context['source']['original_answer']==''
    assert 'original_answer为空仅表示未独立录入原答' in instruction
    assert '原始口述已明确包含孩子原答案或过程' in instruction
    assert '不重复索要' in instruction
    assert '只有原话确实未提到原答时才自然澄清' in instruction
    assert service.state(card['mistake_id'],child_id=CHILD)['source']==result['source']


def test_coach_opening_and_followup_use_only_actual_same_item_assistant_history(learning):
    db,service,intake,mid,sid,requests=learning
    # Analysis/practice commands are not evidence of a prior coaching exchange.
    call(learning,'analyze','command-analysis',utterance='请分析我刚才说的题')
    items=call(learning,'practice','command-practice',utterance='请给两道类似题',count=2)['practice_items']
    call(learning,'coach','other-coach',practice_item_id=items[1]['practice_item_id'],utterance='另一题先说一点')
    result=call(learning,'coach','current-opening',practice_item_id=items[0]['practice_item_id'],utterance='这题先怎么想？')
    assert result['action_status']=='completed'
    context=json.loads(requests[-1]['messages'][1]['content']);instruction=requests[-1]['messages'][0]['content']
    assert context['coaching_step']=='opening'
    assert 'opening用phase=question' in instruction
    assert '简短开放问题' in instruction and '不先公布答案或整套解法' in instruction and '不要求额外道具' in instruction
    call(learning,'coach','current-followup',practice_item_id=items[0]['practice_item_id'],utterance='我觉得先统一分母')
    assert json.loads(requests[-1]['messages'][1]['content'])['coaching_step']=='follow_up'
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM mistake_learning_turn WHERE client_action_id IN (?,?)',('command-analysis','command-practice')).fetchone()[0]==0


def test_canonical_commands_keep_hash_and_payload_without_becoming_coach_turns(learning,monkeypatch):
    db,service,intake,mid,sid,requests=learning
    transport=service.gateway._client._transport
    successful=transport.handle_async_request
    async def unavailable(request):
        requests.append(json.loads(request.content))
        return httpx.Response(503,request=request)
    for operation in ('analyze','practice'):
        first='请'+('分析' if operation=='analyze' else '出两道练习')+'这道题的第一条公开原话'
        second='刚才未确认，现在明确改为'+('重新分析思路' if operation=='analyze' else '给两道新的类似题')
        monkeypatch.setattr(transport,'handle_async_request',unavailable)
        before=len(requests)
        initial=call(learning,operation,operation+'-unknown',utterance=first,count=2)
        assert initial['action_status']=='unknown'
        assert len(requests)==before+1
        assert json.loads(requests[-1]['messages'][1]['content'])['utterance']==first
        assert call(learning,operation,operation+'-unknown',utterance=first,count=2)['action_status']=='unknown'
        assert call(learning,operation,operation+'-same-packet-new-id',utterance=first,count=2)['action_status']=='unknown'
        assert len(requests)==before+1
        monkeypatch.setattr(transport,'handle_async_request',successful)
        resumed=call(learning,operation,operation+'-new-canonical',utterance=second,count=2)
        assert resumed['action_status']=='completed',resumed
        assert json.loads(requests[before+1]['messages'][1]['content'])['utterance']==second
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM mistake_learning_turn').fetchone()[0]==0
        rows=c.execute('SELECT operation,request_sha256 FROM mistake_learning_action WHERE client_action_id IN (?,?,?,?)',('analyze-unknown','analyze-new-canonical','practice-unknown','practice-new-canonical')).fetchall()
        assert len({r['request_sha256'] for r in rows})==4


def test_empty_optional_exact_answer_placeholder_uses_real_keywords_and_verifier(learning,monkeypatch):
    db,service,intake,mid,sid,requests=learning
    transport=service.gateway._client._transport
    original=transport.handle_async_request
    seen=[]
    async def placeholders(request):
        response=await original(request)
        body=json.loads(request.content);instruction=body['messages'][0]['content']
        if 'candidates' in instruction:
            value=response.json();payload=json.loads(value['choices'][0]['message']['content'])
            for candidate in payload['candidates']:
                candidate['expected_answer']={'keywords':['先统一分母','正确相加并约分'],'exact_answers':[],'answer_format':'text'}
            value['choices'][0]['message']['content']=json.dumps(payload,ensure_ascii=False)
            response=httpx.Response(200,request=request,json=value)
        if '独立题目核对员' in instruction:seen.append('verify')
        return response
    monkeypatch.setattr(transport,'handle_async_request',placeholders)
    result=call(learning,'practice','geo-wire-shape-public',count=2)
    assert result['action_status']=='completed',result
    assert len(result['practice_items'])==2
    assert seen==['verify']
    with db.connect() as c:
        expected=[json.loads(r[0]) for r in c.execute('SELECT expected_json FROM mistake_learning_item')]
        assert all('exact_answers' not in item and item['keywords'] for item in expected)
        assert all(json.loads(r[0])['independent_model_check'] is True for r in c.execute('SELECT validation_json FROM mistake_learning_item'))
    import home_study_agent.mistake_learning as module
    for expected in ({'keywords':[],'exact_answers':[],'answer_format':'text'},{'keywords':['仅一个要点'],'exact_answers':[]}):
        candidate={**CANDIDATES[0],'expected_answer':expected}
        with pytest.raises(ValueError,match='MODEL_OUTPUT_INVALID'):
            service._parse('practice',json.dumps({'candidates':[candidate]},ensure_ascii=False),{'question':ORIGINAL},1)
