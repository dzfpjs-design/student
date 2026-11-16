"""Actual HTTP + binding/SQLite + actual route; only decoder/cloud output are fake."""
import base64
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.local_media_ai import LocalMediaAI, ASR_MODEL_NAME, LocalCapabilityNotReadyError
from test_diagnostic_prompt_http import environment, start, CHILD, OTHER
from test_learning_runtime_http import NoCloud
from test_pet_media_evidence import wav_file


def payload(settings,key='local-response',**extra):
    path=settings.data_dir/'public-synthetic.wav';wav_file(path)
    return dict(wav_base64=base64.b64encode(path.read_bytes()).decode(),purpose='wake_word_setup',
                client_action_id=key,capture_reference={'child_id':CHILD},local_only=True,**extra)

@pytest.fixture
def route(monkeypatch,tmp_path):
    import home_study_agent.local_media_ai as module
    calls={'local':0,'cloud':0}
    # Synthetic tone is not an acoustic speech proof. Only routing is under test.
    monkeypatch.setattr(module,'voice_activity_evidence',lambda *_:{'human_voice_detected':True})
    files={'fixture':tmp_path/'fake-model'};files['fixture'].write_bytes(b'fixture-not-a-model')
    monkeypatch.setattr(LocalMediaAI,'_asr_files',lambda *_:files)
    def decode(*_):calls['local']+=1;return '今天学了分数。'
    monkeypatch.setattr(LocalMediaAI,'_recognize_locally',decode)
    class Cloud:
        configured=True
        context_supported=False
        def transcribe(self,*_,**__):
            calls['cloud']+=1
            return SimpleNamespace(transcript='普通原路由',latency_ms=1,prompt_tokens=0,completion_tokens=0,model='fake-cloud')
    original=LocalMediaAI._recognize_wav
    def recognize(self,*a,**kw):
        self._cloud_asr=Cloud()
        return original(self,*a,**kw)
    monkeypatch.setattr(LocalMediaAI,'_recognize_wav',recognize)
    return calls


def test_real_http_forces_local_with_plain_same_child_capture_and_replay(environment,route):
    settings,db=environment
    with TestClient(create_app(settings,model_gateway=NoCloud())) as client:
        sid=start(client);body=payload(settings)
        result=client.post(f'/v1/sessions/{sid}/asr',json=body)
        assert result.status_code==200,result.text
        j=result.json();assert j['cloud_used'] is False and j['local_only'] is True
        assert j['model_version']==ASR_MODEL_NAME and j['local_path'] is None
        assert j['capture_binding']['status']=='completed' and j['capture_binding']['question_reference'] is None
        assert not j['capture_binding'].get('local_recovery_only')
        assert client.post(f'/v1/sessions/{sid}/asr',json=body).json()['voice_input_id']==j['voice_input_id']
        assert route=={'local':1,'cloud':0}
    with TestClient(create_app(settings,model_gateway=NoCloud())) as client:
        assert client.post(f'/v1/sessions/{sid}/asr',json=body).json()['cloud_used'] is False
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM voice_input').fetchone()[0]==1
        assert c.execute('SELECT COUNT(*) FROM model_call').fetchone()[0]==0
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==0

@pytest.mark.parametrize('value',['true',1,None])
def test_strict_local_only_rejects_string_integer_null_before_any_capture(environment,route,value):
    settings,db=environment
    with TestClient(create_app(settings,model_gateway=NoCloud())) as client:
        sid=start(client);body=payload(settings);body['local_only']=value
        assert client.post(f'/v1/sessions/{sid}/asr',json=body).status_code==422
    assert route=={'local':0,'cloud':0}
    with db.connect() as c:assert c.execute('SELECT COUNT(*) FROM voice_capture_binding').fetchone()[0]==0

@pytest.mark.parametrize('case',['no_reference','other_child','question','activity','evaluation','choice','recovery','purpose'])
def test_local_lane_never_borrows_learning_or_recovery_permission(environment,route,case):
    settings,db=environment;body=payload(settings)
    if case=='no_reference':body.pop('capture_reference')
    elif case=='other_child':body['capture_reference']['child_id']=OTHER
    elif case=='purpose':body['purpose']='agent_command'
    else:
        field={'question':'question_reference','activity':'activity_reference','evaluation':'pending_evaluation_turn_id',
               'choice':'task_choice_reference','recovery':'local_recovery_only'}[case]
        body['capture_reference'][field]={'question':{},'activity':{'activity_id':'a','version':'v','last_turn_id':'t'},
                                         'evaluation':'t','choice':{},'recovery':True}[case]
    with TestClient(create_app(settings,model_gateway=NoCloud())) as client:
        sid=start(client);response=client.post(f'/v1/sessions/{sid}/asr',json=body)
        assert response.status_code in (409,422),response.text
    assert route=={'local':0,'cloud':0}
    with db.connect() as c:assert c.execute('SELECT COUNT(*) FROM voice_input').fetchone()[0]==0


def test_unavailable_local_model_cannot_fall_back_to_configured_cloud(environment,route,monkeypatch):
    settings,db=environment
    monkeypatch.setattr(LocalMediaAI,'_asr_files',lambda *_:{'missing':settings.data_dir/'missing'})
    with TestClient(create_app(settings,model_gateway=NoCloud())) as client:
        sid=start(client);response=client.post(f'/v1/sessions/{sid}/asr',json=payload(settings))
        assert response.status_code==503 and response.json()['code']=='ASR_MODEL_NOT_READY',response.text
    assert route=={'local':0,'cloud':0}
    with db.connect() as c:assert c.execute('SELECT COUNT(*) FROM voice_input').fetchone()[0]==0


def test_default_route_still_uses_cloud_and_cannot_relabel_existing_receipt_local(environment,route):
    settings,db=environment
    with TestClient(create_app(settings,model_gateway=NoCloud())) as client:
        sid=start(client);body=payload(settings);body.pop('local_only')
        result=client.post(f'/v1/sessions/{sid}/asr',json=body)
        assert result.status_code==200,result.text
        assert result.json()['cloud_used'] is True and not result.json().get('local_only')
        body['local_only']=True
        assert client.post(f'/v1/sessions/{sid}/asr',json=body).status_code==409
    assert route=={'local':0,'cloud':1}


def test_cancelled_capture_cannot_be_recreated_by_late_local_reply(environment,route):
    settings,db=environment
    with TestClient(create_app(settings,model_gateway=NoCloud())) as client:
        sid=start(client);body=payload(settings)
        assert client.post(f'/v1/sessions/{sid}/asr',json=body).status_code==200
        assert client.post(f'/v1/sessions/{sid}/asr/actions/local-response/cancel').status_code==200
        rejected=client.post(f'/v1/sessions/{sid}/asr',json=body)
        assert rejected.status_code==409,rejected.text
    assert route=={'local':1,'cloud':0}

@pytest.mark.parametrize('version,cloud', [('cloud:fake',True),('foreign-local-model',False)])
def test_local_adapter_wrong_provenance_cannot_save_or_upgrade_qualification(environment,monkeypatch,version,cloud):
    settings,db=environment
    monkeypatch.setattr(LocalMediaAI,'_recognize_wav',lambda *a,**k:('公开回答',320,version,cloud))
    with TestClient(create_app(settings,model_gateway=NoCloud())) as client:
        sid=start(client);response=client.post(f'/v1/sessions/{sid}/asr',json=payload(settings))
        assert response.status_code==409 and response.json()['code']=='VOICE_CAPTURE_SOURCE_MODEL_INVALID',response.text
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM voice_input').fetchone()[0]==0
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==0
