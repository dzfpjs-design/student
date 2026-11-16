"""Original same-App nested TestClients; no model/worker installation claim."""
import time
import pytest
from home_study_agent.pet_audio_gateway import AudioEndpoint,PetAudioGateway
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.database import LATEST_SCHEMA_VERSION
from test_companion_activity_host import NoCloud

@pytest.mark.parametrize("audio_enabled",[False,True])
def test_nested_original_app_lifespans_keep_owned_wake_and_close_naturally(tmp_path,audio_enabled):
    assert LATEST_SCHEMA_VERSION==95
    settings=Settings(data_dir=tmp_path,database_path=tmp_path/'home.db',maintenance_enabled=False,
        session_token='public-synthetic-owned-loop',data_encryption_key=b'K'*32)
    audio=(PetAudioGateway(AudioEndpoint('http://127.0.0.1:9/v1','synthetic-omni','synthetic',
        input_second_micros=10,input_text_token_micros=1,output_token_micros=1,
        tariff_version='synthetic-no-model'),allow_loopback=True)if audio_enabled else None)
    app=create_app(settings,model_gateway=NoCloud(),pet_audio_gateway=audio)
    with TestClient(app,client=('127.0.0.1',42010))as outer:
        time.sleep(.1)
        with TestClient(app,client=('127.0.0.1',42013))as inner:
            # The original worker's wait budget is2s. Let both unmodified
            # lifespans perform their original empty-queue wait/recheck.
            time.sleep(2.4)
            assert outer.app is inner.app is app


# Inspect only the actual private wake closures; no business response is mocked.
def original_worker_helpers(app):
    import inspect
    endpoint=next(r.endpoint for r in app.routes if getattr(r,'name',None)=='answer_diagnostic_prompt')
    complete=inspect.getclosurevars(endpoint).nonlocals['complete_prompt_result']
    values=inspect.getclosurevars(complete).nonlocals
    speaking=values['wake_speaking_candidate'];audio=values['wake_pet_audio']
    current=inspect.getclosurevars(speaking).nonlocals['current_background_worker_owner']
    fields=inspect.getclosurevars(current).nonlocals
    return speaking,audio,current,fields['background_worker_owners'],fields['background_worker_lock']


def observed_empty_worker(monkeypatch):
    from threading import Condition
    from home_study_agent.pet_speaking_candidate_jobs import SpeakingCandidateJobs
    original=SpeakingCandidateJobs.process_one;seen=[];changed=Condition()
    def observed(self,*args,**kwargs):
        value=original(self,*args,**kwargs)
        with changed:
            seen.append(value);changed.notify_all()
        return value
    monkeypatch.setattr(SpeakingCandidateJobs,'process_one',observed)
    def wait_for_new(before):
        with changed:
            assert changed.wait_for(lambda:len(seen)>before,timeout=2)
    return seen,wait_for_new


def test_same_app_reopens_with_fresh_worker_and_no_retained_loop(tmp_path,monkeypatch):
    seen,wait_for_new=observed_empty_worker(monkeypatch)
    settings=Settings(data_dir=tmp_path,database_path=tmp_path/'home.db',maintenance_enabled=False,
        session_token='public-synthetic-reopen',data_encryption_key=b'K'*32)
    app=create_app(settings,model_gateway=NoCloud())
    wake,_,current,registry,lock=original_worker_helpers(app);previous=None
    for _ in range(2):
        before=len(seen)
        with TestClient(app,client=('127.0.0.1',42014))as client:
            owner=client.portal.call(current)
            assert owner is not previous and not owner['work'].is_stopping()
            wait_for_new(before)
            before=len(seen);client.portal.call(wake);wait_for_new(before)
        assert owner['work'].is_stopping()and owner['work']._active==set()
        with lock:assert registry=={}
        previous=owner


@pytest.mark.parametrize('audio_enabled',[False,True])
def test_inner_close_keeps_outer_worker_wake_and_owned_drain(tmp_path,monkeypatch,audio_enabled):
    import asyncio
    seen,wait_for_new=observed_empty_worker(monkeypatch)
    settings=Settings(data_dir=tmp_path,database_path=tmp_path/'home.db',maintenance_enabled=False,
        session_token='public-synthetic-outer-alive',data_encryption_key=b'K'*32)
    audio=(PetAudioGateway(AudioEndpoint('http://127.0.0.1:9/v1','synthetic-omni','synthetic',
        input_second_micros=10,input_text_token_micros=1,output_token_micros=1,
        tariff_version='synthetic-no-model'),allow_loopback=True)if audio_enabled else None)
    app=create_app(settings,model_gateway=NoCloud(),pet_audio_gateway=audio)
    speaking,audio_wake,current,registry,lock=original_worker_helpers(app)
    def signal_and_owned_event():
        owner=current();assert owner['loop']is asyncio.get_running_loop()
        speaking();assert owner['speaking_wake'].is_set()
        if audio_enabled:audio_wake();assert owner['pet_audio_wake'].is_set()
        return owner
    with TestClient(app,client=('127.0.0.1',42015))as outer:
        wait_for_new(0);outer_owner=outer.portal.call(current)
        with TestClient(app,client=('127.0.0.1',42016))as inner:
            inner_owner=inner.portal.call(signal_and_owned_event)
            assert inner_owner is not outer_owner and inner_owner['loop']is not outer_owner['loop']
            assert outer.portal.call(signal_and_owned_event)is outer_owner
        assert inner_owner['work'].is_stopping()and inner_owner['work']._active==set()
        assert not outer_owner['work'].is_stopping()
        before=len(seen)
        assert outer.portal.call(signal_and_owned_event)is outer_owner
        wait_for_new(before)
        with lock:
            assert set(registry)=={outer_owner['loop']}
            assert registry[outer_owner['loop']]==[outer_owner]
    assert outer_owner['work'].is_stopping()and outer_owner['work']._active==set()
    with lock:assert registry=={}
