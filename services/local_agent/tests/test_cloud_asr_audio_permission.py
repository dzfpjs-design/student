from pathlib import Path
import sqlite3
import httpx
import pytest

from normal_provider_owner import normal_provider_owner
from home_study_agent.cloud_asr import CloudAsrError, CloudAsrGateway, CloudAsrResult
from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.local_media_ai import ASR_MODEL_NAME, LocalMediaAI
from home_study_agent import money_egress

ROOT = next(path for path in Path(__file__).resolve().parents
            if (path / 'services/local_agent/tests/fixtures').is_dir())
WAV = (ROOT / 'services/local_agent/tests/fixtures/short-mandarin-dui-trimmed.wav').read_bytes()


def consent_db(tmp_path, allowed):
    database = Database(tmp_path / 'consent.db'); database.initialize()
    set_permission(database, allowed)
    return database


def set_permission(database, allowed):
    with database.connect() as c:
        c.execute('''INSERT INTO privacy_preference(singleton_id,cloud_model_text_allowed,cloud_audio_allowed,
            raw_media_storage_allowed,model_improvement_allowed,model_call_retention_days,tutor_turn_retention_days,
            report_retention_days,updated_at) VALUES(1,1,?,0,0,90,180,730,'synthetic')
            ON CONFLICT(singleton_id) DO UPDATE SET cloud_audio_allowed=excluded.cloud_audio_allowed''', (allowed,))
        c.commit()


def permitted(database):
    with database.connect() as c:
        row = c.execute('SELECT cloud_audio_allowed FROM privacy_preference WHERE singleton_id=1').fetchone()
    return bool(row and type(row[0]) is int and row[0] == 1)


def revoke(database):
    with database.connect() as c:
        c.execute('UPDATE privacy_preference SET cloud_audio_allowed=0 WHERE singleton_id=1'); c.commit()


def settings(tmp_path):
    return Settings(data_dir=tmp_path, database_path=tmp_path/'consent.db',cloud_asr_provider='dashscope',
        cloud_asr_api_key='synthetic-key',cloud_asr_model='qwen3-asr-flash',cloud_asr_base_url='https://synthetic.invalid/v1')


def transport(monkeypatch, before_enter=None, after_response=None):
    calls=[]
    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self):
            if before_enter: before_enter()
            return self
        def __exit__(self, *_): pass
        def post(self, *args, **kwargs):
            calls.append(True)
            if after_response: after_response()
            return httpx.Response(200,request=httpx.Request('POST','https://synthetic.invalid/'),json={
                'choices':[{'message':{'content':'小知开始学习'}}],
                'usage':{'prompt_tokens':5,'completion_tokens':2}})
    monkeypatch.setattr(httpx,'Client',Client)
    return calls


def local_media(tmp_path, monkeypatch, database):
    media=LocalMediaAI(database,tmp_path,tmp_path,settings=settings(tmp_path))
    monkeypatch.setattr(media,'_asr_files',lambda _: {})
    monkeypatch.setattr(media,'_recognize_locally',lambda *_: '本机原始识别内容')
    return media


@pytest.mark.parametrize('state',['false','missing_row','missing_table','missing_database','read_failure'])
def test_missing_or_false_permission_uses_local_without_any_http(tmp_path,monkeypatch,normal_provider_owner,state):
    database=consent_db(tmp_path,0)
    if state=='missing_row':
        with database.connect() as c:c.execute('DELETE FROM privacy_preference');c.commit()
    if state=='missing_table':
        with database.connect() as c:c.execute('DROP TABLE privacy_preference');c.commit()
    if state=='missing_database': database=None
    if state=='read_failure':
        class Unreadable:
            def connect(self):raise sqlite3.OperationalError('synthetic unavailable')
        database=Unreadable()
    calls=transport(monkeypatch);media=local_media(tmp_path,monkeypatch,database)
    result=media._recognize_wav(WAV,prefer_cloud=True,purpose='agent_command')
    assert result[0]=='本机原始识别内容' and result[2:]==(ASR_MODEL_NAME,False)
    assert calls==[]


def test_standby_remains_local_even_with_explicit_cloud_permission(tmp_path,monkeypatch,normal_provider_owner):
    database=consent_db(tmp_path,1);calls=transport(monkeypatch)
    media=local_media(tmp_path,monkeypatch,database)
    result=media.transcribe_standby(WAV,'synthetic-wake-action')
    assert result['cloud_used'] is False and result['persisted'] is False
    assert result['model_version']==ASR_MODEL_NAME and result['transcript']=='本机原始识别内容'
    assert calls==[] and permitted(database) is True


def test_explicit_permission_keeps_cloud_dialogue_available(tmp_path,monkeypatch,normal_provider_owner):
    database=consent_db(tmp_path,1);calls=transport(monkeypatch)
    media=local_media(tmp_path,monkeypatch,database)
    result=media._recognize_wav(WAV,prefer_cloud=True,purpose='agent_command')
    assert result[0]=='小知开始学习' and result[2:]==('cloud:qwen:qwen3-asr-flash',True)
    assert len(calls)==1 and permitted(database) is True


def test_unbound_gateway_refuses_cloud_upload(tmp_path,monkeypatch,normal_provider_owner):
    calls=transport(monkeypatch)
    with pytest.raises(CloudAsrError,match='CLOUD_ASR_AUDIO_NOT_ALLOWED'):
        CloudAsrGateway(settings(tmp_path)).transcribe(WAV)
    assert calls==[]


@pytest.mark.parametrize('boundary',['dispatch','client_enter'])
def test_revocation_before_actual_send_refuses_queued_upload_and_recomputes_locally(tmp_path,monkeypatch,normal_provider_owner,boundary):
    database=consent_db(tmp_path,1)
    calls=transport(monkeypatch,before_enter=(lambda:revoke(database)) if boundary=='client_enter' else None)
    if boundary=='dispatch':
        def dispatch(wav,settings,send,until):revoke(database);return send(wav)
        monkeypatch.setattr(money_egress,'dispatch_asr',dispatch)
    media=local_media(tmp_path,monkeypatch,database)
    result=media._recognize_wav(WAV,prefer_cloud=True,purpose='agent_command')
    assert result[0]=='本机原始识别内容' and result[2:]==(ASR_MODEL_NAME,False)
    assert calls==[] and permitted(database) is False


def test_response_received_after_revocation_is_discarded_and_recomputed_locally(tmp_path,monkeypatch,normal_provider_owner):
    database=consent_db(tmp_path,1)
    calls=transport(monkeypatch,after_response=lambda:revoke(database))
    media=local_media(tmp_path,monkeypatch,database);completions=[]
    monkeypatch.setattr(media,'_start_cloud_asr_call',lambda *a,**k:'synthetic-call')
    monkeypatch.setattr(media,'_finish_cloud_asr_call',lambda call_id,**values:completions.append(values))
    result=media._recognize_wav(WAV,prefer_cloud=True,purpose='agent_command')
    assert result[0]=='本机原始识别内容' and result[2:]==(ASR_MODEL_NAME,False)
    assert len(calls)==1 and permitted(database) is False
    assert len(completions)==1 and completions[0]['error_code']=='CLOUD_ASR_AUDIO_NOT_ALLOWED'
    assert completions[0]['prompt_tokens']==5 and completions[0]['completion_tokens']==2


def test_media_rechecks_permission_after_adapter_returns(tmp_path,monkeypatch):
    database=consent_db(tmp_path,1)
    class Adapter:
        configured=True
        context_supported=False
        def transcribe(self,*a,**kw):
            revoke(database)
            return CloudAsrResult('不得使用的云端文本','synthetic-model',3,4,2)
    media=local_media(tmp_path,monkeypatch,database);media._cloud_asr=Adapter()
    result=media._recognize_wav(WAV,prefer_cloud=True,purpose='agent_command')
    assert result[0]=='本机原始识别内容' and result[2:]==(ASR_MODEL_NAME,False)


def test_revocation_between_recognition_and_publication_is_not_persisted_and_action_can_retry_locally(tmp_path,monkeypatch):
    from home_study_agent.local_media_ai import LocalCapabilityNotReadyError
    database=consent_db(tmp_path,1)
    with database.connect() as c:
        c.execute("INSERT INTO child_profile(child_id,nickname,grade,curriculum_region,status,created_at,updated_at) VALUES('child','合成档案','四年级','合成地区','active','synthetic','synthetic')")
        c.execute("INSERT INTO daily_session(session_id,child_id,status,state,started_at,last_checkpoint_at,client_action_id) VALUES('session','child','active','study','synthetic','synthetic','session-action')")
        c.commit()
    media=local_media(tmp_path,monkeypatch,database)
    def revoke_before_publication(*a,**kw):
        revoke(database)
        return '不得持久化的云端文本',195,'cloud:qwen:qwen3-asr-flash',True
    monkeypatch.setattr(media,'_recognize_wav',revoke_before_publication)
    with pytest.raises(LocalCapabilityNotReadyError) as caught:
        media.transcribe('session',WAV,'agent_command','same-action-after-revocation',False)
    assert caught.value.code=='ASR_CLOUD_AUDIO_REVOKED'
    with database.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM voice_input').fetchone()[0]==0
        assert c.execute('SELECT COUNT(*) FROM voice_transcription_action').fetchone()[0]==0
        assert c.execute('SELECT cloud_audio_allowed FROM privacy_preference').fetchone()[0]==0
    monkeypatch.setattr(media,'_recognize_wav',lambda *a,**kw:('本机原始识别内容',195,ASR_MODEL_NAME,False))
    result=media.transcribe('session',WAV,'agent_command','same-action-after-revocation',False)
    assert result['transcript']=='本机原始识别内容' and result['cloud_used'] is False
    with database.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM voice_input').fetchone()[0]==1


@pytest.mark.parametrize('allowed',[0,1])
def test_status_matches_current_audio_permission_without_changing_it(tmp_path,monkeypatch,allowed):
    database=consent_db(tmp_path,allowed)
    media=local_media(tmp_path,monkeypatch,database)
    from home_study_agent.local_media_ai import RapidOcrEngine
    monkeypatch.setattr(RapidOcrEngine,'health',lambda *_:{'ready':True})
    status=media.status()['asr']
    assert status['active_dialogue_engine']==('cloud' if allowed else 'local_fallback')
    assert status['cloud_ready'] is True
    assert media.cloud_context_supported is bool(allowed)
    assert permitted(database) is bool(allowed)


def test_false_or_raising_permission_callback_is_not_a_service_failure(tmp_path,monkeypatch,normal_provider_owner):
    calls=transport(monkeypatch)
    class Policy:
        def endpoint_available(self,*a):return True
        def record_failure(self,*a):pytest.fail('permission denial must not count as model failure')
        def record_success(self,*a):pytest.fail('permission denial must not count as provider success')
    for callback in (lambda:False,lambda:(_ for _ in ()).throw(sqlite3.OperationalError('synthetic'))):
        gateway=CloudAsrGateway(settings(tmp_path),runtime_policy=Policy())
        gateway.cloud_audio_authorized=callback
        with pytest.raises(CloudAsrError,match='CLOUD_ASR_AUDIO_NOT_ALLOWED'):gateway.transcribe(WAV)
    assert calls==[]


@pytest.mark.parametrize('usage',[[], 'invalid', {'prompt_tokens': True}, {'prompt_tokens':-1},
    {'completion_tokens':1.5}, {'completion_tokens':'2'}])
def test_malformed_usage_remains_response_failure_without_marking_route_healthy(tmp_path,monkeypatch,normal_provider_owner,usage):
    calls=[]
    class Client:
        def __init__(self,**kw):pass
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def post(self,*a,**kw):return httpx.Response(200,request=httpx.Request('POST','https://synthetic.invalid/'),
            json={'choices':[{'message':{'content':'小知'}}],'usage':usage})
    class Policy:
        def endpoint_available(self,*a):return True
        def record_failure(self,*a):calls.append(a[-1])
        def record_success(self,*a):pytest.fail('malformed usage must not clear route health')
    monkeypatch.setattr(httpx,'Client',Client)
    gateway=CloudAsrGateway(settings(tmp_path),runtime_policy=Policy());gateway.cloud_audio_authorized=lambda:True
    with pytest.raises(CloudAsrError,match='CLOUD_ASR_REQUEST_FAILED'):gateway.transcribe(WAV)
    assert calls==['MODEL_RESPONSE_INVALID']
