"""Main-only integration: source revisions, finite Gateway and private records.

Only public synthetic data and MockTransport are used. No production DB or
external provider is touched. Source-revision rows below are controlled storage
fixtures for the intake resolver, not claims of formal OCR/grading authority.
"""
import asyncio
import hashlib
import json
import socket
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from normal_provider_owner import normal_provider_owner
from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.learning_state import LearningStateService
from home_study_agent.mistake_book import MistakeBookService
from home_study_agent.mistake_learning import MistakeLearningService
from home_study_agent.mistake_learning_intake import MistakeIntakeError, MistakeLearningIntake
from home_study_agent.mistake_learning_privacy import TABLES, export_learning, validate_renderer_drafts
from home_study_agent.model_gateway import ModelGateway, ModelRequest, ModelResponseError
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from home_study_agent.repository import StudyRepository

A = 'child-primary-4'
B = 'child-junior-3'
LIMITS = {'analyze':1800,'practice':3600,'coach':1600,'answer':1800,'verify':1200}


@pytest.fixture(autouse=True)
def forbid_external_network(monkeypatch):
    calls=[]
    def deny(*args, **kwargs):
        calls.append(True)
        raise AssertionError('PUBLIC_TEST_EXTERNAL_NETWORK_FORBIDDEN')
    monkeypatch.setattr(socket.socket,'connect',deny)
    monkeypatch.setattr(socket.socket,'connect_ex',deny)
    yield
    assert calls==[]


def model_request(operation):
    return ModelRequest(request_id='public-'+str(uuid4()),child_id=A,session_id='public-session',
        request_type='mistake_learning_'+operation,risk_level='medium',
        context_manifest={'selected_public_case_only':True},provider_preference='primary',
        policy_version='selected_mistake_formative_v1',prompt_version='public-integration-v1',
        knowledge_version=None,output_type='diagnosis',evidence_refs=(),
        messages=({'role':'system','content':'Return a bounded JSON object.'},
                  {'role':'user','content':'公开合成：解释同分母分数相加。'}))


@pytest.mark.parametrize('operation,maximum',list(LIMITS.items()))
def test_main_gateway_five_finite_json_budgets(tmp_path,normal_provider_owner,operation,maximum):
    sent=[]
    def transport(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200,request=request,json={
            'choices':[{'finish_reason':'stop','message':{'content':'{"ok":true}'}}],
            'usage':{'prompt_tokens':12,'completion_tokens':4}})
    gateway=ModelGateway(Settings(data_dir=tmp_path,database_path=tmp_path/'gateway.db',
        model_api_key='public-synthetic'),transport=httpx.MockTransport(transport))
    async def run():
        try:return await gateway.complete(model_request(operation))
        finally:await gateway.close()
    result=asyncio.run(run())
    assert result.content=='{"ok":true}'
    assert len(sent)==1
    assert ModelGateway._max_output_tokens('mistake_learning_'+operation)==maximum
    assert sent[0]['max_tokens']==6144
    assert sent[0]['enable_thinking'] is True
    assert sent[0]['thinking_budget']==256
    assert 'temperature' not in sent[0]
    assert sent[0]['response_format']=={'type':'json_object'}
    assert not ({'child_id','session_id','context_manifest','evidence_refs'} & set(sent[0]))


@pytest.mark.parametrize('operation',list(LIMITS))
def test_main_gateway_rejects_valid_json_that_provider_marks_truncated(tmp_path,normal_provider_owner,operation):
    posts=[]
    def transport(request):
        posts.append(request)
        # Valid JSON is insufficient when the provider says the output stopped early.
        return httpx.Response(200,request=request,json={
            'choices':[{'finish_reason':'length','message':{'content':'{"ok":true}'}}],
            'usage':{'prompt_tokens':12,'completion_tokens':4}})
    gateway=ModelGateway(Settings(data_dir=tmp_path,database_path=tmp_path/'gateway.db',
        model_api_key='public-synthetic'),transport=httpx.MockTransport(transport))
    async def run():
        try:
            with pytest.raises(ModelResponseError) as failure:
                await gateway.complete(model_request(operation))
            assert failure.value.code=='MODEL_OUTPUT_TRUNCATED'
        finally:await gateway.close()
    asyncio.run(run())
    assert len(posts)==1


def test_intake_uses_latest_effective_source_and_replay_never_returns_changed_material(tmp_path):
    from home_study_agent.mistake_source_revision import migrate
    db=Database(tmp_path/'revisions.db');db.initialize()
    repo=StudyRepository(db);repo.seed_family();LearningStateService(db).seed_catalog()
    sid=repo.start_session(B,'public-source-session')['session_id']
    original='方程 x²-4x+4=0 的判别式是多少？'
    corrected='方程 x²-2x-3=0 的判别式是多少？'
    answer='4'
    mid=MistakeBookService(db).capture(child_id=B,knowledge_node_id='node-j3-math-discriminant',
        subject='数学',title='公开源修订',prompt_summary=original,submitted_answer=answer,
        source_type='diagnostic',source_ref='diag-j3-discriminant-1',confidence=.6,
        client_action_id='public-source-capture',diagnostic_item_id='diag-j3-discriminant-1')['mistake_id']
    intake=MistakeLearningIntake(db)
    old=intake.create(child_id=B,session_id=sid,question=original,submitted_answer=answer,client_action_id='public-old-intake')
    assert old['mistake_id']==mid
    def revise(version,question):
        with db.connect() as c:
            migrate(c)
            c.execute('INSERT INTO mistake_source_revision VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (str(uuid4()),mid,B,sid,version,question,'public-no-image-artifact',
                 json.dumps({'case':{'prompt_summary':original}}),'public-source-hash',
                 '{}','public-source-confirm-'+str(version),'2026-10-30T00:00:00+00:00'))
            c.commit()
    revise(1,corrected)
    current=intake.create(child_id=B,session_id=sid,question=corrected,submitted_answer=answer,client_action_id='public-corrected-intake')
    assert current['mistake_id']==mid
    assert current['prompt_summary']==corrected
    reopened=MistakeLearningIntake(Database(db.path))
    assert reopened.create(child_id=B,session_id=sid,question=corrected,submitted_answer=answer,client_action_id='public-corrected-intake')['mistake_id']==mid
    with pytest.raises(MistakeIntakeError,match='SOURCE_CHANGED'):
        reopened.create(child_id=B,session_id=sid,question=original,submitted_answer=answer,client_action_id='public-old-intake')
    # The old text is a distinct explicitly supplied formative card, not the revised legacy case.
    distinct=intake.create(child_id=B,session_id=sid,question=original,submitted_answer=answer,client_action_id='public-old-text-new-request')
    assert distinct['mistake_id']!=mid
    revise(2,'方程 x²+6x+9=0 的判别式是多少？')
    with pytest.raises(MistakeIntakeError,match='SOURCE_CHANGED'):
        reopened.create(child_id=B,session_id=sid,question=corrected,submitted_answer=answer,client_action_id='public-corrected-intake')


ANALYSIS={'summary':'先统一分母，解释每一份的大小。','wrong_causes':[{'label':'单位不同','explanation':'分母表示每份大小，不能直接相加。'}],
 'knowledge_points':[{'label':'分数单位','description':'先把不同单位转成相同单位。'}],'correction_strategy':['画出相同大小的整体再比较。']}
CANDIDATES=[
 {'stage':'near_transfer','prompt':'一块披萨平均分成八份，先吃了三份，又吃了两份。总共吃了整块披萨的几分之几？','expected_answer':{'exact_answers':['5/8'],'answer_format':'rational'},'explanation':'同分母表示相同大小的份，3+2=5，共吃了五份，所以是5/8。','difficulty':1},
 {'stage':'representation_transfer','prompt':'数轴上，把从0到1这段距离平均分为六份，第四个等分点用最简分数表示是多少？','expected_answer':{'exact_answers':['2/3'],'answer_format':'rational'},'explanation':'四个六分之一合起来是4/6，约分得到2/3。','difficulty':1}]
FEEDBACK={'judgement':'correct','feedback':'同分母的份数算对了。','explanation':'整块的八分之五表示吃了五份。','reference_answer':'5/8','next_hint':'下一题换成数轴表示。'}


@pytest.fixture
def populated(tmp_path,normal_provider_owner):
    db=Database(tmp_path/'public-notebook.db');db.initialize()
    repo=StudyRepository(db);repo.seed_family()
    from home_study_agent.document_monitoring import DocumentMonitoringService
    DocumentMonitoringService(db)  # Same typed schema initialization used by create_app.
    privacy=PrivacyLifecycleService(db,tmp_path);privacy.initialize()
    privacy.update_preferences({'cloud_model_text_allowed':True})
    calls=[]
    def transport(request):
        payload=json.loads(request.content);calls.append(payload)
        instruction=payload['messages'][0]['content']
        if '中立事实核查员' in instruction:value={'claim_status':'no_claim','facts':'分数相加需要先统一每一份的大小。'}
        elif '逐步讲解核对员' in instruction:value={'valid':True}
        elif '独立题目核对员' in instruction:
            value={'checks':[{'position':i+1,'valid':True} for i in range(len(json.loads(payload['messages'][1]['content'])))]}
        elif 'wrong_causes' in instruction:value=ANALYSIS
        elif 'candidates' in instruction:value={'candidates':CANDIDATES}
        elif 'judgement' in instruction:value=FEEDBACK
        else:value={'phase':'question','text':'分母告诉我们每份的大小。两个分数的分数单位分别是多少？'}
        return httpx.Response(200,request=request,json={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(value,ensure_ascii=False)}}],
            'usage':{'prompt_tokens':20,'completion_tokens':30}})
    gateway=ModelGateway(Settings(data_dir=tmp_path,database_path=db.path,model_api_key='public-synthetic'),transport=httpx.MockTransport(transport))
    service=MistakeLearningService(db,gateway);intake=MistakeLearningIntake(db)
    sessions={A:repo.start_session(A,'public-a-session')['session_id']};cards={}
    for child in (A,B):
        if child==B:sessions[B]=repo.switch_active_session_child(sessions[A],B,'public-profile-switch')['session_id']
        sid=sessions[child]
        cards[child]=intake.create(child_id=child,session_id=sid,question='计算1/3+1/4。',submitted_answer='2/7',subject='数学',client_action_id='public-intake-'+child)['mistake_id']
        mid=cards[child]
        service.focus(mid,child_id=child,session_id=sid,client_action_id='public-focus-'+child)
        async def learn():
            assert (await service.analyze(mid,child_id=child,session_id=sid,client_action_id='public-analysis-'+child))['action_status']=='completed'
            practice=await service.practice(mid,child_id=child,session_id=sid,client_action_id='public-practice-'+child,count=2)
            assert practice['action_status']=='completed'
            first=practice['practice_items'][0]['practice_item_id']
            assert (await service.answer(mid,child_id=child,session_id=sid,client_action_id='public-answer-'+child,practice_item_id=first,utterance='5/8'))['action_status']=='completed'
            assert (await service.coach(mid,child_id=child,session_id=sid,client_action_id='public-coach-'+child,utterance='为什么分母不能直接相加？'))['action_status']=='completed'
        asyncio.run(learn())
        review=service.review(mid,child_id=child,session_id=sid,client_action_id='public-original-review-'+child,
                              modality='voice',utterance='十二分之七')
        assert review['review_result']['judgement']=='correct'
    with db.connect() as c:
        # Operational audit storage fixtures only; no fake execution/mastery is credited.
        for child in (A,B):
            c.execute('INSERT INTO audit_event VALUES(?,?,?,?,?,?)',('public-mistake-audit-'+child,'native_mistake_learning_effect_v1','mistake_learning_saved',child,'{}','2026-10-30T00:00:00Z'))
        c.commit()
    yield db,service,privacy,cards,sessions,calls
    asyncio.run(gateway.close())


def test_privacy_export_and_actual_learning_purge_include_every_new_table_and_isolate_other_child(populated):
    db,service,privacy,cards,sessions,calls=populated
    with db.connect() as c:
        a=export_learning(c,A);b=export_learning(c,B)
        assert set(a)==set(TABLES)|{'mistake_learning_card_revision'}
        assert all(a[table] for table in a)
        assert all(b[table] for table in b)
    exported=privacy.export_child(A)
    data=json.loads(Path(exported['path']).read_text())
    assert data['scope']=='local_only' and data['cloud_transmission_allowed'] is False
    assert data['mistake_learning']==a
    assert cards[B] not in json.dumps(data['mistake_learning'])
    assert hashlib.sha256(Path(exported['path']).read_bytes()).hexdigest()==exported['sha256']
    result=privacy.delete_child_scope(A,'learning','确认删除','public-mistake-learning-delete')
    assert result['status']=='completed',result
    with db.connect() as c:
        assert all(not rows for rows in export_learning(c,A).values())
        assert export_learning(c,B)==b
        assert c.execute('SELECT 1 FROM audit_event WHERE audit_id=?',('public-mistake-audit-'+A,)).fetchone() is None
        assert c.execute('SELECT 1 FROM audit_event WHERE audit_id=?',('public-mistake-audit-'+B,)).fetchone()
        assert c.execute('PRAGMA foreign_key_check').fetchall()==[]
    assert service.state(cards[B],child_id=B)['today']['completed_count']==1
    # Idempotent deletion replays without touching the other child's saved work.
    assert privacy.delete_child_scope(A,'learning','确认删除','public-mistake-learning-delete')['status']=='completed'
    with db.connect() as c:assert export_learning(c,B)==b


def test_renderer_drafts_export_exact_raw_strings_and_reject_other_child_before_writing(populated):
    db,service,privacy,cards,sessions,calls=populated
    entries=[{'key':'hsc-cloud-mistake-intake-v1:'+A,'value':'{原样未解析的草稿\n引号"和中文'} ,
             {'key':'hsc-cloud-mistake-draft-v1:'+A+':'+cards[A],'value':'{"answer":"仍在输入\\n"}'},
             {'key':'hsc-cloud-mistake-pending-v1:'+A+':'+cards[A],'value':'{"client_action_id":"pending-public"}'}]
    result=privacy.export_child(A,entries)
    payload=json.loads(Path(result['path']).read_text())
    assert payload['mistake_learning_renderer_drafts']==entries
    before=Path(result['path']).read_bytes()
    other=[{'key':'hsc-cloud-mistake-intake-v1:'+B,'value':'另一个孩子的公开草稿'}]
    with pytest.raises(ValueError,match='CHILD_SCOPE'):privacy.export_child(A,other)
    assert Path(result['path']).read_bytes()==before
    with db.connect() as c:
        assert c.execute("SELECT count(*) FROM audit_event WHERE child_id=? AND action='child_data_exported'",(A,)).fetchone()[0]==1


@pytest.mark.parametrize('entries,error',[
    ([{'key':'hsc-cloud-mistake-intake-v1:'+B,'value':'wrong child'}],'CHILD_SCOPE'),
    ([{'key':'hsc-cloud-mistake-intake-v1:'+A,'value':True}],'INVALID'),
    ([{'key':'hsc-cloud-mistake-intake-v1:'+A,'value':'v','extra':'x'}],'INVALID'),
    ([{'key':'hsc-cloud-mistake-intake-v1:'+A,'value':'a'},{'key':'hsc-cloud-mistake-intake-v1:'+A,'value':'b'}],'CHILD_SCOPE'),
])
def test_renderer_draft_closed_shape_and_owner(entries,error):
    with pytest.raises(ValueError,match=error):validate_renderer_drafts(A,entries)


def test_renderer_draft_count_and_actual_utf8_byte_limits():
    allowed=[{'key':'hsc-cloud-mistake-draft-v1:'+A+':'+str(i),'value':'v'} for i in range(4096)]
    assert validate_renderer_drafts(A,allowed)==allowed
    with pytest.raises(ValueError,match='LIMIT'):
        validate_renderer_drafts(A,allowed+[{'key':'hsc-cloud-mistake-draft-v1:'+A+':4096','value':'v'}])
    with pytest.raises(ValueError,match='LIMIT'):
        validate_renderer_drafts(A,[{'key':'hsc-cloud-mistake-intake-v1:'+A,'value':'汉'*(8*1024*1024//3+1)}])
