"""Real HTTP/SQLite learning answers; controlled model transport, no fixture provenance mint."""
from datetime import datetime, timezone
import json
from contextvars import ContextVar
import asyncio
import pytest
from home_study_agent.database import Database
from home_study_agent.agent_runtime import AgentRuntimeStore
from home_study_agent.canonical_voice_execution import inspect as canonical
import httpx
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.model_gateway import ModelGateway
from test_content_free_voice_control_http import ControlGateway
@pytest.fixture
def env(tmp_path):
    db=Database(tmp_path/'public-synthetic.db');db.initialize()
    from home_study_agent.pet_vocabulary import PetVocabularyService
    from home_study_agent.exam_simulation import ExamSimulationService
    PetVocabularyService(db).ensure_schema();ExamSimulationService(db).ensure_schema()
    return db,AgentRuntimeStore(db)


QUESTION = '请给我讲解正弦和余弦有什么区别。'
CHECK = '正弦和余弦各自比较的是哪一条边与斜边？'
NARRATION = '先以选定角为准找到对边和邻边，再观察它们与斜边的关系。' + CHECK

class OriginalGatewayWithControlledTransport:
    configured = True
    def __init__(self, settings):
        self.control = ControlGateway(QUESTION, control=None, role='question')
        self.kind = ContextVar('synthetic_request_kind', default=None)
        self.requests=[]; self.transports=[]; self.evaluation_payloads=[]
        self.original = ModelGateway(settings,transport=httpx.MockTransport(self.transport))
    def transport(self, request):
        kind=self.kind.get(); self.transports.append(kind)
        assert request.url.host=='synthetic.invalid'
        if kind=='agent_plan':
            content=json.dumps({'utterance_binding':{'role':'question','quote':QUESTION},'objective':{'mode':'new','type':'learning','summary':'理解正弦和余弦的区别','success_criteria':['能独立区分对边和邻边'],'phase':'study'},'next_step':{'capability':'learning.explain','arguments':{'question':QUESTION,'subject':'数学','support_level':6},'reason':'围绕合成概念讲解','expected_evidence':['independent_response']},'spoken_transition':'我们先比较这两个概念。','requires_child_reply':True,'confidence':0.94},ensure_ascii=False)
        elif kind=='learning_outcome_evaluation':
            body=json.loads(request.read());payload=json.loads(body['messages'][-1]['content'])
            assert set(payload)=={'question_asked','child_response','subject'}
            self.evaluation_payloads.append(payload)
            content=json.dumps({'understanding':'partial','evidence_summary':'本次合成回答已经说出边的对应关系。','spoken_feedback':'我们把选定的角换一下，再检查刚才的区分。','next_check':'换一个选定角后，原来的对边会变成哪一条边？','confidence':0.8},ensure_ascii=False)
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
def real_source(env,tmp_path,monkeypatch):
    db, _ = env
    monkeypatch.setenv('HSC_DATA_DIR',str(tmp_path));monkeypatch.setenv('HSC_DATABASE_PATH',str(db.path))
    settings=Settings(data_dir=tmp_path,database_path=db.path,maintenance_enabled=False,
        model_api_key='SYNTHETIC_NO_REAL_ACCOUNT',model_base_url='https://synthetic.invalid',
        fallback_model_api_key=None)
    gateway=OriginalGatewayWithControlledTransport(settings)
    app=create_app(settings,model_gateway=gateway)
    with TestClient(app) as client:
        auth=client.post('/v1/parent/setup',json={'pin':'8264'})
        assert auth.status_code==200,auth.text
        headers={'X-HSC-Parent':auth.json()['parent_token']}
        consent=client.put('/v1/parent/privacy',headers=headers,json={'cloud_model_text_allowed':True,'cloud_audio_allowed':False})
        assert consent.status_code==200,consent.text
        session=client.post('/v1/sessions',json={'child_id':'child-primary-4','client_action_id':'source-pipeline-session'})
        assert session.status_code==201,session.text
        sid=session.json()['session_id'];root='/v1/sessions/'+sid
        started=client.post(root+'/voice/realtime/begin',json={'client_action_id':'source-pipeline-question-onset','capture_reference':{'child_id':'child-primary-4','question_reference':None},'model_label':'PUBLIC_SYNTHETIC_REALTIME','captured_at_ms':int(datetime.now(timezone.utc).timestamp()*1000)})
        assert started.status_code==200,started.text
        voice=client.post(root+'/voice/realtime/complete',json={'client_action_id':'source-pipeline-question-onset','transcript':QUESTION})
        assert voice.status_code==200,voice.text
        vid=voice.json()['voice_input_id']
        planned=client.post(root+'/agent-turn',json={'transcript':QUESTION,'voice_input_id':vid,'client_action_id':'realtime-'+vid,'cloud_text_allowed':True})
        assert planned.status_code==200,planned.text
        turn=planned.json();assert turn['runtime_capability']=='learning.explain',turn
        assert turn['action']['local_only'] is True and turn['action']['cloud_transmission_allowed'] is False
        action=turn['action']
        taught=client.post(root+'/tutor',json={'subject':action['subject'],'question':action['question'],'hint_level':action['hint_level'],'client_action_id':'source-pipeline-original-tutor'})
        assert taught.status_code==200,taught.text
        tutor=taught.json();assert CHECK in tutor['answer']
        board=tutor['output_envelope']['result']['blackboard']
        questions=[b['text'] for s in board['segments'] for b in s['blocks'] if b['kind']=='question']
        assert questions==[CHECK]
        output={k:tutor[k] for k in ('provider','model','response_kind','verification_status','safety_status')}
        output.update(tutor_turn_id=tutor['turn_id'],blackboard_present=True)
        receipt={'capability':'learning.explain','execution_location':'local_model','action_type':'ask_tutor','status':'completed','completed_at':datetime.now(timezone.utc).isoformat(),'output':output}
        completed=client.put('/v1/agent-turns/'+turn['agent_turn_id']+'/outcome',json={'final_response':tutor['answer'],'tool_status':'completed','result':{'execution_receipt':receipt}})
        assert completed.status_code==200,completed.text
        status=client.get(root+'/agent-runtime');assert status.status_code==200,status.text
        capture=status.json()['pending_learning_capture'];assert capture is not None,status.json()
        assert capture['reference']['binding']['source_agent_turn_id']==turn['agent_turn_id']
        with db.connect() as c:
            row=c.execute('SELECT s.result_json FROM agent_plan p JOIN agent_step s USING(plan_id) WHERE p.agent_turn_id=?',(turn['agent_turn_id'],)).fetchone()
            result=json.loads(row['result_json']);assert result['execution_receipt']['output']['tutor_turn_id']==tutor['turn_id']
            model=c.execute('SELECT status,purpose,child_id,session_id FROM model_call WHERE call_id=?',(tutor['output_envelope']['request_id'],)).fetchone()
            assert model is not None and model['status']=='completed' and model['purpose'].startswith('tutor_hint_level_')
            assert model['child_id']=='child-primary-4' and model['session_id']==sid
        assert gateway.transports and any(x.startswith('tutor_hint_level_') for x in gateway.transports)
        yield client,db,gateway,root,capture,turn,tutor,headers
    asyncio.run(gateway.close())


WORDS='我的答案是正弦看对边，余弦看邻边，都要除以斜边。'

def capture_text(client,root,reference,key,words):
    first=client.post(root+'/voice/realtime/begin',json={'client_action_id':key,'capture_reference':{'child_id':'child-primary-4','question_reference':reference},'model_label':'PUBLIC_SYNTHETIC_REALTIME','captured_at_ms':int(datetime.now(timezone.utc).timestamp()*1000)})
    assert first.status_code==200,first.text
    done=client.post(root+'/voice/realtime/complete',json={'client_action_id':key,'transcript':words})
    assert done.status_code==200,done.text
    assert done.json()['source_kind']=='realtime_transcript'
    return done.json()

def interpret_voice(client,root,gateway,voice,words,role='answer'):
    gateway.control.quote=words;gateway.control.role=role
    body={'transcript':words,'voice_input_id':voice['voice_input_id'],'client_action_id':'realtime-'+voice['voice_input_id'],'cloud_text_allowed':True}
    result=client.post(root+'/agent-turn',json=body)
    assert result.status_code==200,result.text
    return result.json()

def inspect_current(db,turn,voice):
    assert turn['runtime_capability']=='learning.evaluate_response',(turn['intent'],turn['action'].get('source'))
    assert turn['action']['voice_input_binding']['source_kind']=='realtime_transcript'
    assert turn['action']['local_only'] is True and turn['action']['cloud_transmission_allowed'] is False
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        assert c.execute('SELECT client_action_id FROM agent_turn WHERE agent_turn_id=?',(turn['agent_turn_id'],)).fetchone()[0]=='realtime-'+voice['voice_input_id']
        assert canonical(c,turn['agent_turn_id']) is True
        c.rollback()

def execute_save_replay(client,db,gateway,turn,expected_words,expected_question):
    response=client.post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
    assert response.status_code==200,response.text
    value=response.json()
    assert value['turn']['tool_status']=='completed'
    assert value['execution']['capability']=='learning.evaluate_response'
    assert value['execution']['result']['certification_status']=='candidate'
    assert value['execution']['result']['outcome_level']!='mastered'
    assert len(gateway.evaluation_payloads)==1
    assert gateway.evaluation_payloads[0]=={'question_asked':expected_question,'child_response':expected_words,'subject':'数学'}
    # A separate reopened actual DB connection verifies the durable result and original raw local flags.
    with Database(db.path).connect() as c:
        c.execute('BEGIN')
        stored=c.execute('SELECT action_json,response_text,tool_status FROM agent_turn WHERE agent_turn_id=?',(turn['agent_turn_id'],)).fetchone()
        assert stored['tool_status']=='completed' and stored['response_text']==value['execution']['response_text']
        action=json.loads(stored['action_json']);assert action['local_only'] is True and action['cloud_transmission_allowed'] is False
        assert c.execute('SELECT COUNT(*) FROM agent_outcome').fetchone()[0]==1
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==0
        assert c.execute('SELECT state FROM learning_evaluation_execution').fetchone()[0]=='completed'
        with pytest.raises(ValueError,match='^CANONICAL_VOICE_EXECUTION_SOURCE_CHANGED$'):
            canonical(c,turn['agent_turn_id'])
        c.rollback()
    replay=client.post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
    assert replay.status_code==200,replay.text
    assert replay.json()['execution']['response_text']==value['execution']['response_text']
    assert len(gateway.evaluation_payloads)==1
    return value

def test_real_source_direct_realtime_answer_persists_replays_once_and_old_onset_retires(real_source):
    client,db,gateway,root,source,*_=real_source;reference=source['reference']
    current=capture_text(client,root,reference,'real-source-direct-current',WORDS)
    old=capture_text(client,root,reference,'real-source-old-onset-held',WORDS)
    turn=interpret_voice(client,root,gateway,current,WORDS);inspect_current(db,turn,current)
    execute_save_replay(client,db,gateway,turn,WORDS,NARRATION)
    state=client.get(root+'/agent-runtime');assert state.status_code==200,state.text
    newref=state.json()['pending_learning_capture']['reference'];assert newref!=reference
    rejected=interpret_voice(client,root,gateway,old,WORDS)
    assert rejected['intent']=='pending_answer_clarification' and rejected['action']['type']=='none'
    assert rejected['action'].get('mastery_update_allowed') is False
    assert len(gateway.evaluation_payloads)==1
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM agent_outcome').fetchone()[0]==1
        assert c.execute('SELECT question_validation FROM voice_capture_binding WHERE voice_input_id=?',(old['voice_input_id'],)).fetchone()[0]=='valid'

def test_real_source_two_turn_confirmation_grades_original_answer_only(real_source):
    client,db,gateway,root,source,*_=real_source
    words='正弦看对边，余弦看邻边，都要除以斜边。'
    original=capture_text(client,root,source['reference'],'real-source-candidate-first',words)
    candidate=interpret_voice(client,root,gateway,original,words)
    assert candidate['intent']=='pending_answer_clarification'
    assert candidate['action']['pending_answer_candidate']['answer']==words
    spoken=client.post('/v1/agent-turns/'+candidate['agent_turn_id']+'/execute');assert spoken.status_code==200,spoken.text
    state=client.get(root+'/agent-runtime');assert state.status_code==200,state.text
    assert state.json()['pending_learning_capture']==source
    confirmation='刚才那句就是我的答案'
    confirmed=capture_text(client,root,source['reference'],'real-source-candidate-confirm',confirmation)
    turn=interpret_voice(client,root,gateway,confirmed,confirmation)
    assert turn['action']['answer_source_turn_id']==candidate['agent_turn_id']
    with db.connect() as c:
        c.execute('BEGIN')
        resolved=AgentRuntimeStore.resolve_local_answer_submission(c,turn['agent_turn_id'],'learning.evaluate_response')
        assert resolved['voice_input_id']==confirmed['voice_input_id'] and resolved['source_voice_input_id']==original['voice_input_id']
        assert resolved['answer']==words
        c.rollback()
    inspect_current(db,turn,confirmed)
    execute_save_replay(client,db,gateway,turn,words,NARRATION)

def test_real_source_ack_prompt_repeated_ack_stays_quiet_then_answer_persists(real_source):
    client,db,gateway,root,source,source_turn,tutor,headers=real_source
    voice=capture_text(client,root,source['reference'],'real-source-ack-first','我懂了')
    ack=interpret_voice(client,root,gateway,voice,'我懂了','acknowledgement')
    assert ack['runtime_capability']=='learning.request_response'
    result=client.post('/v1/agent-turns/'+ack['agent_turn_id']+'/execute');assert result.status_code==200,result.text
    state=client.get(root+'/agent-runtime');assert state.status_code==200,state.text
    asked=state.json()['pending_learning_capture'];binding=asked['reference']['binding']
    assert binding['capability_name']=='learning.request_response' and binding['source_agent_turn_id']==ack['agent_turn_id']
    with db.connect() as c:
        goal=dict(c.execute('SELECT * FROM agent_objective WHERE objective_id=?',(binding['objective_id'],)).fetchone())
        prompt=json.loads(c.execute('SELECT s.arguments_json FROM agent_step s JOIN agent_plan p USING(plan_id) WHERE p.agent_turn_id=?',(ack['agent_turn_id'],)).fetchone()[0])['prompt']
        assert prompt=='先别急着说懂了。请不用照着我刚才的话，用自己的话讲一遍关键点。'
    second_voice=capture_text(client,root,asked['reference'],'real-source-ack-second','好的')
    neutral=interpret_voice(client,root,gateway,second_voice,'好的','acknowledgement')
    assert neutral.get('runtime_capability') is None and neutral['action']['type']=='none'
    assert neutral['action']['suppress_speech'] is True and 'pending_answer_candidate' not in neutral['action']
    done=client.put('/v1/agent-turns/'+neutral['agent_turn_id']+'/outcome',json={'final_response':neutral['response_text'],'tool_status':'completed'})
    assert done.status_code==200,done.text
    latest=client.get(root+'/agent-runtime');assert latest.status_code==200,latest.text
    assert latest.json()['pending_learning_capture']==asked
    with db.connect() as c:
        assert dict(c.execute('SELECT * FROM agent_objective WHERE objective_id=?',(binding['objective_id'],)).fetchone())==goal
        assert c.execute('SELECT COUNT(*) FROM agent_plan WHERE agent_turn_id=?',(neutral['agent_turn_id'],)).fetchone()[0]==0
    answer=capture_text(client,root,asked['reference'],'real-source-after-ack-answer',WORDS)
    turn=interpret_voice(client,root,gateway,answer,WORDS);inspect_current(db,turn,answer)
    execute_save_replay(client,db,gateway,turn,WORDS,tutor['question']+'\n'+prompt)


def test_current_parent_text_revocation_blocks_dispatch_but_retains_answer(real_source):
    client,db,gateway,root,source,source_turn,tutor,headers=real_source
    voice=capture_text(client,root,source['reference'],'real-source-parent-revoke-onset',WORDS)
    turn=interpret_voice(client,root,gateway,voice,WORDS);inspect_current(db,turn,voice)
    denied=client.put('/v1/parent/privacy',headers=headers,json={'cloud_model_text_allowed':False})
    assert denied.status_code==200,denied.text
    assert denied.json()['cloud_model_text_allowed'] is False and denied.json()['cloud_audio_allowed'] is False
    execution=client.post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
    assert execution.status_code==409,execution.text
    assert gateway.evaluation_payloads==[]
    assert not any(x=='learning_outcome_evaluation' for x in gateway.transports)
    with Database(db.path).connect() as c:
        stored=c.execute('SELECT transcript,action_json FROM agent_turn WHERE agent_turn_id=?',(turn['agent_turn_id'],)).fetchone()
        assert stored['transcript']==WORDS
        action=json.loads(stored['action_json'])
        assert action['voice_input_id']==voice['voice_input_id']
        assert action['local_only'] is True and action['cloud_transmission_allowed'] is False
        assert c.execute('SELECT COUNT(*) FROM agent_outcome').fetchone()[0]==0
