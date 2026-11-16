"""New privacy outlet controls: actual text pipeline, synthetic transport/temp DB."""
from datetime import datetime, timezone
import json
import httpx
import pytest
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.config import Settings
from contextvars import ContextVar
from home_study_agent.model_gateway import ModelGateway
from home_study_agent.database import Database
from home_study_agent.agent_runtime import AgentRuntimeStore
from test_content_free_voice_control_http import ControlGateway

QUESTION = '请给我讲解正弦和余弦有什么区别。'
CHECK = '正弦和余弦各自比较的是哪一条边与斜边？'
NARRATION = '先以选定角为准找到对边和邻边，再观察它们与斜边的关系。' + CHECK

class OriginalGatewayWithControlledTransport:
    configured = True
    def __init__(self, settings):
        self.control = ControlGateway(QUESTION, control=None, role='question')
        self.kind = ContextVar('synthetic_request_kind', default=None)
        self.requests=[]; self.transports=[]
        self.original = ModelGateway(settings,transport=httpx.MockTransport(self.transport))
    def transport(self, request):
        kind=self.kind.get(); self.transports.append(kind)
        assert request.url.host=='synthetic.invalid'
        if kind=='agent_plan':
            content=json.dumps({'utterance_binding':{'role':'question','quote':QUESTION},'objective':{'mode':'new','type':'learning','summary':'理解正弦和余弦的区别','success_criteria':['能独立区分对边和邻边'],'phase':'study'},'next_step':{'capability':'learning.explain','arguments':{'question':QUESTION,'subject':'数学','support_level':6},'reason':'围绕合成概念讲解','expected_evidence':['independent_response']},'spoken_transition':'我们先比较这两个概念。','requires_child_reply':True,'confidence':0.94},ensure_ascii=False)
        elif kind.startswith('tutor_hint_level_'):
            content=json.dumps({'blackboard':{'title':'正弦和余弦','segments':[{'narration':NARRATION,'blocks':[{'kind':'text','text':'先找选定角的对边和邻边。'},{'kind':'question','text':CHECK}]}]}},ensure_ascii=False)
        else:
            raise AssertionError('UNEXPECTED_SOURCE_MODEL_PURPOSE:'+str(kind))
        return httpx.Response(200,request=request,json={'choices':[{'finish_reason':'stop','message':{'content':content}}],'usage':{'prompt_tokens':30,'completion_tokens':40}})
    async def complete(self, request):
        self.requests.append(request)
        if request.request_type=='voice_interaction':
            return await self.control.complete(request)
        token=self.kind.set(request.request_type)
        try:return await self.original.complete(request)
        finally:self.kind.reset(token)
    async def close(self):
        await self.original.close()

@pytest.fixture
def env(tmp_path):
    db = Database(tmp_path / 'public-synthetic.db'); db.initialize()
    store = AgentRuntimeStore(db)
    from home_study_agent.pet_vocabulary import PetVocabularyService
    from home_study_agent.exam_simulation import ExamSimulationService
    PetVocabularyService(db).ensure_schema(); ExamSimulationService(db).ensure_schema()
    return db, store

INPUT = '_ordinary_learning_text_input_policy_v1'
TUTOR = '_ordinary_learning_text_tutor_source_v1'
RECORD = '_ordinary_learning_text_projection_v1'
ANSWER = '正弦是选定角的对边与斜边的比，余弦是邻边与斜边的比。'


class PrivacyGateway(OriginalGatewayWithControlledTransport):
    def transport(self, request):
        if self.kind.get() != 'learning_outcome_evaluation':
            return super().transport(request)
        self.transports.append(self.kind.get())
        content = {'understanding':'partial','evidence_summary':'这次说清了两种边的对应关系。',
            'spoken_feedback':'这次边的对应关系保留了。我们再换一个角核对。',
            'next_check':'换一个选定角后，原来的对边会变成哪一条边？','confidence':.8}
        if getattr(self, 'inject_reserved', False):content[RECORD]={'caller_or_model_claim':True}
        return httpx.Response(200,request=request,json={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(content,ensure_ascii=False)}}],
            'usage':{'prompt_tokens':20,'completion_tokens':25}})


def begin_voice(client, sid, text, key, reference):
    path='/v1/sessions/'+sid+'/voice/realtime'
    started=client.post(path+'/begin',json={'client_action_id':key,'capture_reference':{'child_id':'child-primary-4','question_reference':reference},
        'model_label':'PUBLIC_SYNTHETIC_REALTIME','captured_at_ms':int(datetime.now(timezone.utc).timestamp()*1000)})
    assert started.status_code==200,started.text
    completed=client.post(path+'/complete',json={'client_action_id':key,'transcript':text})
    assert completed.status_code==200,completed.text
    return completed.json()['voice_input_id']


def interpret_voice(client,g,sid,text,key,reference,role='answer'):
    g.control.quote=text;g.control.role=role
    vid=begin_voice(client,sid,text,key,reference)
    r=client.post('/v1/sessions/'+sid+'/agent-turn',json={'transcript':text,'voice_input_id':vid,'client_action_id':'realtime-'+vid,'cloud_text_allowed':True})
    assert r.status_code==200,r.text
    return r.json(),vid


def pending(client,sid):
    result=client.get('/v1/sessions/'+sid+'/agent-runtime');assert result.status_code==200,result.text
    return result.json()['pending_learning_capture']['reference']


def source_pipeline(client,g,db):
    auth=client.post('/v1/parent/setup',json={'pin':'8264'});assert auth.status_code==200,auth.text
    headers={'X-HSC-Parent':auth.json()['parent_token']}
    r=client.put('/v1/parent/privacy',headers=headers,json={'cloud_model_text_allowed':True,'cloud_audio_allowed':False});assert r.status_code==200,r.text
    started=client.post('/v1/sessions',json={'child_id':'child-primary-4','client_action_id':'private-projection-start-session'});assert started.status_code==201,started.text
    sid=started.json()['session_id']
    explain,_=interpret_voice(client,g,sid,QUESTION,'private-projection-lesson-onset',None,'question')
    assert explain['runtime_capability']=='learning.explain'
    assert explain['action']['local_only'] is True and explain['action']['cloud_transmission_allowed'] is False
    assert INPUT not in json.dumps(explain)
    action=explain['action']
    taught=client.post('/v1/sessions/'+sid+'/tutor',json={'subject':action['subject'],'question':action['question'],
        'hint_level':action['hint_level'],'client_action_id':'private-projection-original-tutor'})
    assert taught.status_code==200,taught.text
    tutor=taught.json()
    output={k:tutor[k] for k in ('provider','model','response_kind','verification_status','safety_status')}
    output.update(tutor_turn_id=tutor['turn_id'],blackboard_present=True)
    receipt={'capability':'learning.explain','execution_location':'local_model','action_type':'ask_tutor','status':'completed',
        'completed_at':datetime.now(timezone.utc).isoformat(),'output':output}
    completed=client.put('/v1/agent-turns/'+explain['agent_turn_id']+'/outcome',json={'final_response':tutor['answer'],
        'tool_status':'completed','result':{'execution_receipt':receipt}})
    assert completed.status_code==200,completed.text
    assert all(key not in json.dumps(completed.json()) for key in (INPUT,TUTOR,RECORD))
    return sid,headers,explain,tutor


def client_fixture(env,tmp_path,monkeypatch):
    db,_=env;monkeypatch.setenv('HSC_DATA_DIR',str(tmp_path));monkeypatch.setenv('HSC_DATABASE_PATH',str(db.path))
    settings=Settings(data_dir=tmp_path,database_path=db.path,maintenance_enabled=False,
        model_api_key='SYNTHETIC_NO_REAL_ACCOUNT',model_base_url='https://synthetic.invalid',fallback_model_api_key=None)
    g=PrivacyGateway(settings)
    return db,g,TestClient(create_app(settings,model_gateway=g))


def evaluation_requests(g):return [r for r in g.requests if r.request_type=='learning_outcome_evaluation']


def assert_no_evaluation_write(db):
    with db.connect() as c:
        assert c.execute("SELECT count(*) FROM agent_outcome").fetchone()[0]==0
        assert c.execute("SELECT count(*) FROM model_call WHERE purpose='learning_outcome_evaluation'").fetchone()[0]==0


def execute(client,turn):return client.post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')


def test_actual_voice_text_projection_exports_exact_three_fields_and_preserves_local_history(env,tmp_path,monkeypatch,caplog,capsys):
    db,g,client=client_fixture(env,tmp_path,monkeypatch)
    with client:
        sid,_,explain,tutor=source_pipeline(client,g,db)
        turn,_=interpret_voice(client,g,sid,'我的答案是'+ANSWER,'private-direct-answer-onset',pending(client,sid))
        assert turn['runtime_capability']=='learning.evaluate_response',turn
        result=execute(client,turn);assert result.status_code==200,result.text
        request=evaluation_requests(g);assert len(request)==1
        payload=json.loads(request[0].messages[-1]['content'])
        assert payload=={'question_asked':NARRATION,'child_response':'我的答案是'+ANSWER,'subject':'数学'}
        assert request[0].prompt_version=='ordinary_learning_text_projection_v1'
        assert request[0].context_manifest['fields']==['question_asked','child_response','subject']
        assert all(k not in json.dumps({'messages':request[0].messages,'context_manifest':request[0].context_manifest},ensure_ascii=False) for k in(INPUT,TUTOR,RECORD))
        with db.connect() as c:
            rows=c.execute('SELECT action_json FROM agent_turn WHERE session_id=?',(sid,)).fetchall()
            for row in rows:
                a=json.loads(row[0]);assert INPUT in a;assert a['local_only'] is True and a['cloud_transmission_allowed'] is False
            model=c.execute('SELECT context_manifest_json FROM model_call WHERE call_id=?',(tutor['output_envelope']['request_id'],)).fetchone();assert TUTOR in json.loads(model[0])
            p=c.execute('SELECT cloud_model_text_allowed,cloud_audio_allowed FROM privacy_preference').fetchone();assert tuple(p)==(1,0)
            e=json.loads(c.execute('SELECT record_json FROM learning_evaluation_execution').fetchone()[0]);assert RECORD in e
            assert set(e[RECORD]['payload'])=={'question_asked','child_response','subject'}
        public=json.dumps(result.json(),ensure_ascii=False)
        assert all(k not in public for k in(INPUT,TUTOR,RECORD))
        out=capsys.readouterr();assert all(k not in out.out+out.err+caplog.text for k in(INPUT,TUTOR,RECORD))


def test_real_candidate_confirmation_uses_original_capture_with_minimal_projection(env,tmp_path,monkeypatch):
    db,g,client=client_fixture(env,tmp_path,monkeypatch)
    with client:
        sid,_,_,_=source_pipeline(client,g,db);ref=pending(client,sid)
        candidate,original=interpret_voice(client,g,sid,ANSWER,'private-candidate-original-onset',ref)
        assert candidate['action']['source']=='pending_answer_clarification'
        assert candidate['action']['pending_answer_candidate']['answer']==ANSWER
        first=execute(client,candidate);assert first.status_code==200,first.text
        turn,_=interpret_voice(client,g,sid,'刚才那句就是我的答案','private-confirmation-onset',ref)
        assert turn['action']['answer_source_turn_id']==candidate['agent_turn_id']
        result=execute(client,turn);assert result.status_code==200,result.text
        assert json.loads(evaluation_requests(g)[0].messages[-1]['content'])=={'question_asked':NARRATION,'child_response':ANSWER,'subject':'数学'}
        with db.connect() as c:
            saved=json.loads(c.execute('SELECT action_json FROM agent_turn WHERE agent_turn_id=?',(candidate['agent_turn_id'],)).fetchone()[0])
            assert saved[INPUT]['voice_input_id']==original
            record=json.loads(c.execute('SELECT record_json FROM learning_evaluation_execution').fetchone()[0])
            assert len(record[RECORD]['local_audit']['input_policies'])==2


@pytest.mark.parametrize('change',['parent_off','parent_off_then_on','same_text_private_source','legacy_missing_proof','changed_answer','cross_child_proof'])
def test_privacy_revocation_source_and_payload_changes_cannot_dispatch(env,tmp_path,monkeypatch,change):
    db,g,client=client_fixture(env,tmp_path,monkeypatch)
    with client:
        sid,headers,source,_=source_pipeline(client,g,db)
        turn,_=interpret_voice(client,g,sid,'我的答案是'+ANSWER,'private-negative-answer-onset',pending(client,sid))
        assert turn['runtime_capability']=='learning.evaluate_response'
        if change.startswith('parent_off'):
            x=client.put('/v1/parent/privacy',headers=headers,json={'cloud_model_text_allowed':False});assert x.status_code==200,x.text
            if change.endswith('then_on'):
                x=client.put('/v1/parent/privacy',headers=headers,json={'cloud_model_text_allowed':True});assert x.status_code==200,x.text
        else:
            with db.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                target=source['agent_turn_id'] if change in {'same_text_private_source','legacy_missing_proof'} else turn['agent_turn_id']
                row=c.execute('SELECT action_json FROM agent_turn WHERE agent_turn_id=?',(target,)).fetchone();action=json.loads(row[0])
                if change=='same_text_private_source':action['document_context']={'local_only':True,'cloud_transmission_allowed':False,'source_ref':'synthetic_private_book'}
                elif change=='legacy_missing_proof':action.pop(INPUT)
                elif change=='cross_child_proof':action[INPUT]['child_id']='child-junior-3'
                elif change=='changed_answer':
                    args=c.execute('SELECT s.step_id,s.arguments_json,p.plan_id,p.plan_json FROM agent_plan p JOIN agent_step s USING(plan_id) WHERE p.agent_turn_id=?',(target,)).fetchone()
                    arguments=json.loads(args['arguments_json']);arguments['response']='不同原话'
                    plan=json.loads(args['plan_json']);plan['next_step']['arguments']=arguments
                    c.execute('UPDATE agent_step SET arguments_json=? WHERE step_id=?',(json.dumps(arguments,ensure_ascii=False),args['step_id']))
                    c.execute('UPDATE agent_plan SET plan_json=? WHERE plan_id=?',(json.dumps(plan,ensure_ascii=False),args['plan_id']))
                c.execute('UPDATE agent_turn SET action_json=? WHERE agent_turn_id=?',(json.dumps(action,ensure_ascii=False),target));c.commit()
        result=execute(client,turn)
        assert result.status_code in {409,422,403},result.text
        assert not evaluation_requests(g)
        assert_no_evaluation_write(db)


def test_application_authored_ack_question_uses_its_authorized_lesson_not_goal_summary(env,tmp_path,monkeypatch):
    from home_study_agent.ordinary_learning_text_projection import APP_PROMPT
    db,g,client=client_fixture(env,tmp_path,monkeypatch)
    with client:
        sid,_,_,_=source_pipeline(client,g,db)
        ack,_=interpret_voice(client,g,sid,'我懂了','private-ack-onset',pending(client,sid),'acknowledgement')
        assert ack['runtime_capability']=='learning.request_response',ack
        done=execute(client,ack);assert done.status_code==200,done.text
        ref=pending(client,sid);assert ref['binding']['capability_name']=='learning.request_response'
        turn,_=interpret_voice(client,g,sid,'我的答案是'+ANSWER,'private-after-ack-answer-onset',ref)
        assert turn['runtime_capability']=='learning.evaluate_response',turn
        result=execute(client,turn);assert result.status_code==200,result.text
        payload=json.loads(evaluation_requests(g)[0].messages[-1]['content'])
        assert payload=={'question_asked':QUESTION+'\n'+APP_PROMPT,'child_response':'我的答案是'+ANSWER,'subject':'数学'}


def test_parent_export_strips_only_six_structured_namespaces_and_retains_original_text_and_database(env,tmp_path,monkeypatch):
    from pathlib import Path
    from home_study_agent.ordinary_learning_text_projection import PRIVATE_EXPORT_KEYS,STRUCTURED_JSON_COLUMNS
    db,g,client=client_fixture(env,tmp_path,monkeypatch)
    with client:
        sid,headers,source,tutor=source_pipeline(client,g,db)
        candidate,_=interpret_voice(client,g,sid,ANSWER,'private-export-candidate-onset',pending(client,sid))
        completed=execute(client,candidate);assert completed.status_code==200,completed.text
        raw='孩子原话保留：'+json.dumps({k:'original-child-text'for k in PRIVATE_EXPORT_KEYS},ensure_ascii=False)
        # Synthetic labor content only; never rewrite any production/family row.
        with db.connect() as c:
            c.execute('UPDATE agent_turn SET transcript=? WHERE agent_turn_id=?',(raw,candidate['agent_turn_id']));c.commit()
            before_actions=[tuple(r)for r in c.execute('SELECT agent_turn_id,action_json,transcript FROM agent_turn ORDER BY rowid')]
            before_models=[tuple(r)for r in c.execute('SELECT call_id,context_manifest_json FROM model_call ORDER BY rowid')]
        exported=client.post('/v1/parent/children/child-primary-4/export',headers=headers)
        assert exported.status_code==200,exported.text
        path=Path(exported.json()['path']);assert path.resolve().is_relative_to(tmp_path.resolve())
        data=json.loads(path.read_text())
        def closed(value):
            if type(value)is list:
                for v in value:closed(v)
            elif type(value)is dict:
                assert not (set(value)&PRIVATE_EXPORT_KEYS)
                for k,v in value.items():
                    if k in STRUCTURED_JSON_COLUMNS and type(v)is str:
                        try:v=json.loads(v)
                        except ValueError:pass
                    closed(v)
        closed(data)
        assert next(r['transcript']for r in data['agent_turns']if r['agent_turn_id']==candidate['agent_turn_id'])==raw
        with db.connect() as c:
            assert before_actions==[tuple(r)for r in c.execute('SELECT agent_turn_id,action_json,transcript FROM agent_turn ORDER BY rowid')]
            assert before_models==[tuple(r)for r in c.execute('SELECT call_id,context_manifest_json FROM model_call ORDER BY rowid')]
            assert any(INPUT in r[1]for r in before_actions) and any(TUTOR in r[1]for r in before_models)


def test_export_exact_json_column_filter_does_not_parse_child_json_text_or_unknown_fields():
    from home_study_agent.ordinary_learning_text_projection import public_export_copy,PRIVATE_EXPORT_KEYS
    literal=json.dumps({k:'verbatim'for k in PRIVATE_EXPORT_KEYS},ensure_ascii=False,indent=2)
    unchanged=' { "unrelated_unknown_field" : [1,2], "child_text" : '+json.dumps(literal,ensure_ascii=False)+' } '
    value={'transcript':literal,'draft_text':literal,'message':literal,'action_json':unchanged,
           'context_manifest_json':json.dumps({'ordinary':{'safe':1},**{k:{'internal':True}for k in PRIVATE_EXPORT_KEYS}}),
           'unrelated_unknown_field':{'safe':[1,2]}}
    copy=public_export_copy(value)
    assert copy['transcript']==copy['draft_text']==copy['message']==literal
    assert copy['action_json']==unchanged and copy['unrelated_unknown_field']==value['unrelated_unknown_field']
    assert json.loads(copy['context_manifest_json'])=={'ordinary':{'safe':1}}
    assert value['context_manifest_json']!=copy['context_manifest_json']


def test_model_cannot_supply_private_policy_namespace_as_extra_output(env,tmp_path,monkeypatch):
    db,g,client=client_fixture(env,tmp_path,monkeypatch);g.inject_reserved=True
    with client:
        sid,_,_,_=source_pipeline(client,g,db)
        turn,_=interpret_voice(client,g,sid,'我的答案是'+ANSWER,'private-model-lookalike-onset',pending(client,sid))
        result=execute(client,turn)
        assert result.status_code==409,result.text
        assert len(evaluation_requests(g))==1
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM agent_outcome').fetchone()[0]==0
            assert c.execute("SELECT status FROM model_call WHERE purpose='learning_outcome_evaluation'").fetchone()[0]=='failed'


def test_accepted_evaluation_next_question_is_a_fresh_versioned_text_source(env,tmp_path,monkeypatch):
    db,g,client=client_fixture(env,tmp_path,monkeypatch)
    with client:
        sid,_,_,_=source_pipeline(client,g,db)
        first,_=interpret_voice(client,g,sid,'我的答案是'+ANSWER,'private-first-evaluation-onset',pending(client,sid))
        done=execute(client,first);assert done.status_code==200,done.text
        reference=pending(client,sid)
        assert reference['binding']['capability_name']=='learning.evaluate_response'
        assert reference['binding']['source_agent_turn_id']==first['agent_turn_id']
        words='我的答案是原来的对边需要按新的选定角重新判断。'
        second,_=interpret_voice(client,g,sid,words,'private-second-evaluation-onset',reference)
        result=execute(client,second);assert result.status_code==200,result.text
        requests=evaluation_requests(g);assert len(requests)==2
        payload=json.loads(requests[-1].messages[-1]['content'])
        assert payload=={'question_asked':'换一个选定角后，原来的对边会变成哪一条边？','child_response':words,'subject':'数学'}
        assert set(payload)=={'question_asked','child_response','subject'}
