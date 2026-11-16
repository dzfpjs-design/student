import asyncio
import base64
from io import BytesIO
from pathlib import Path
import threading
import wave
import httpx
import pytest
from fastapi import FastAPI, HTTPException
from home_study_agent.local_media_ai import LocalMediaAI, ASR_MODEL_NAME
import home_study_agent.local_media_ai as production_media

from home_study_agent import local_playback_control as candidate
def wav():
    b=BytesIO()
    with wave.open(b,'wb') as f:
        f.setnchannels(1);f.setsampwidth(2);f.setframerate(16000);f.writeframes(b'\x01\x00'*8000)
    return base64.b64encode(b.getvalue()).decode()
def payload(**delta):
    return candidate.PlaybackControlRequest(**dict(child_id='public-child',playback_lease_id='00000000-0000-4000-8000-000000000001',playback_generation=1,capture_epoch=1,source_kind='local_sapi',current_playback_text='现在继续原来保存的任务。',wav_base64=wav(),**delta))
class Media:
    def __init__(self, text='暂停', cloud=False):self.text=text;self.cloud=cloud;self.calls=[];self.child='public-child'
    def _session(self, sid):return {'session_id':sid,'child_id':self.child,'status':'active'}
    def _recognize_wav(self, raw, **kwargs):self.calls.append(kwargs);return self.text,500,'synthetic-local-decoder',self.cloud
def run(m,p=None):return asyncio.run(candidate.LocalPlaybackControl(m).recognize('public-session',p or payload()))

def test_exact_control_is_ephemeral_local_and_has_no_transcript():
    m=Media();r=run(m);assert r['control']=='stop_playback'
    assert m.calls==[{'prefer_cloud':False,'session':None,'purpose':'agent_command'}]
    assert r['cloud_used'] is False and r['persisted'] is False and r['learning_evidence'] is False
    assert set(r)=={'status','control','playback_lease_id','playback_generation','capture_epoch','cloud_used','persisted','learning_evidence'}

@pytest.mark.parametrize('text', ['继续学习','暂停然后提交答案','暂停。停止','停一下妈妈说的内容',''])
def test_nonexact_or_combined_commands_never_become_control(text):assert run(Media(text))['control'] is None

@pytest.mark.parametrize('text', ['停一下', '等一下', '先暂停'])
def test_common_exact_controls_stop_only_playback_and_still_reject_prompt_echo(text):
    p=payload();r=run(Media(text),p);assert r['control']=='stop_playback' and r['learning_evidence'] is False
    p.current_playback_text+='你刚才说'+text;assert run(Media(text),p)['status']=='echo_rejected'

@pytest.mark.parametrize('text', ['停一下并提交答案', '等一下继续下一题', '先暂停然后进入家长报告'])
def test_common_control_with_additional_intent_is_not_exact_control(text):
    assert run(Media(text))['control'] is None

def test_current_prompt_containing_control_is_echo_rejected():
    p=payload();p.current_playback_text='你可以说暂停，或继续。';assert run(Media(),p)['status']=='echo_rejected'

def test_normal_long_local_sapi_reference_is_supported_without_echo_truncation():
    p=payload();p.current_playback_text='继续核对当前保存的学习内容。'*500
    assert run(Media(),p)['control']=='stop_playback'
    p.current_playback_text+='暂停';assert run(Media(),p)['status']=='echo_rejected'

def test_pcm_without_reference_and_foreign_child_do_not_decode():
    m=Media();p=payload();p.source_kind='protected_pcm';assert run(m,p)['status']=='reference_unavailable';assert m.calls==[]
    p=payload();p.child_id='other-child';assert run(m,p)['status']=='rejected';assert m.calls==[]

def test_cloud_flag_or_model_failure_never_falls_back_or_publishes():
    assert run(Media(cloud=True))['control'] is None
    m=Media();m._recognize_wav=lambda *a,**k:(_ for _ in ()).throw(ValueError('PRIVATE_PROVIDER_BODY'))
    r=run(m);assert r['control'] is None;assert 'PRIVATE' not in str(r)

def test_scope_change_after_decode_rejects_late_result():
    m=Media();original=m._recognize_wav
    def changed(*a,**k):m.child='other-child';return original(*a,**k)
    m._recognize_wav=changed;assert run(m)['status']=='rejected'

def test_absolute_deadline_rejects_completion_even_if_timer_callback_was_delayed():
    async def scenario():
        now=[0];m=Media();original=m._recognize_wav
        def delayed_callback(*a,**k):now[0]=5;return original(*a,**k)
        m._recognize_wav=delayed_callback;s=candidate.LocalPlaybackControl(m,clock=lambda:now[0])
        assert (await s.recognize('public-session',payload()))['status']=='timed_out'
    asyncio.run(scenario())

def test_timeout_keeps_single_worker_occupied_and_discards_late_decoder():
    async def scenario():
        event=threading.Event();m=Media();original=m._recognize_wav
        def slow(*a,**k):event.wait(1);return original(*a,**k)
        m._recognize_wav=slow;s=candidate.LocalPlaybackControl(m,timeout_seconds=.01)
        assert (await s.recognize('public-session',payload()))['status']=='timed_out'
        assert (await s.recognize('public-session',payload()))['status']=='busy'
        event.set();await asyncio.sleep(.04);assert s.worker is None;assert len(m.calls)==1
    asyncio.run(scenario())

def test_actual_production_recognition_branch_is_local_only_without_database_or_cloud(monkeypatch,tmp_path):
    class ForbiddenCloud:
        def __getattr__(self,name):raise AssertionError('CLOUD_WAS_ACCESSED')
    m=LocalMediaAI(None,tmp_path,tmp_path,cloud_asr_gateway=ForbiddenCloud())
    for p in m._asr_files(tmp_path/ASR_MODEL_NAME).values():p.parent.mkdir(exist_ok=True);p.touch()
    m._session=lambda sid:{'session_id':sid,'child_id':'public-child','status':'active'}
    m._recognize_locally=lambda samples,rate:'暂停'
    monkeypatch.setattr(production_media,'voice_activity_evidence',lambda samples,rate:{'human_voice_detected':True})
    assert run(m)['control']=='stop_playback'  # decoder is explicitly stubbed; no model run

def test_actual_missing_local_model_never_accesses_cloud_or_publishes_control(tmp_path):
    class ForbiddenCloud:
        def __getattr__(self,name):raise AssertionError('CLOUD_WAS_ACCESSED')
    m=LocalMediaAI(None,tmp_path,tmp_path,cloud_asr_gateway=ForbiddenCloud())
    m._session=lambda sid:{'session_id':sid,'child_id':'public-child','status':'active'}
    r=run(m);assert r['status']=='local_recognition_unavailable' and r['control'] is None

def test_invalid_audio_never_reaches_decoder():
    m=Media();p=payload();p.wav_base64=base64.b64encode(b'PRIVATE_NOT_WAV').decode()
    assert run(m,p)['status']=='invalid_audio';assert m.calls==[]

def test_real_asgi_route_requires_auth_loopback_and_forbids_extra_answer_fields():
    async def scenario():
        m=Media();app=FastAPI()
        def require(request):
            if request.headers.get('X-HSC-Session')!='public-test-token':raise HTTPException(401)
        app.include_router(candidate.create_router(m,require))
        route='/v1/sessions/public-session/voice/local-playback-control';data=payload().model_dump(mode='json')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app,client=('127.0.0.1',123)),base_url='http://127.0.0.1') as c:
            assert (await c.post(route,json=data)).status_code==401
            r=await c.post(route,json=data,headers={'X-HSC-Session':'public-test-token'});assert r.status_code==200 and r.json()['control']=='stop_playback'
            invalid=await c.post(route,json={**data,'question_reference':{'raw':'PRIVATE_INPUT'}},headers={'X-HSC-Session':'public-test-token'});assert invalid.status_code==400;assert 'PRIVATE_INPUT' not in invalid.text;assert data['current_playback_text'] not in invalid.text
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app,client=('192.0.2.1',123)),base_url='http://127.0.0.1') as c:
            assert (await c.post(route,json=data,headers={'X-HSC-Session':'public-test-token'})).status_code==403
        assert len(m.calls)==1
    asyncio.run(scenario())
