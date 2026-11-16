from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime,timedelta,timezone
import json
import threading
import time

from home_study_agent.app import create_app
from home_study_agent.database import Database
from test_pet_oral_supply_http import setup,post,CHILD
from test_oral_runtime_feedback_http import OralGateway,begin_oral,ANSWER
from test_oral_followup_execution_http import answer
from test_voice_runtime_binding import capture
from test_pet_audio_work import loopback
from test_pet_audio_vertical_http import tcp_client


def test_slow_sse_does_not_hold_original_ack_and_pause_fences_explicit_delivery(tmp_path,monkeypatch,loopback):
    initial,settings=setup(tmp_path)
    settings=replace(settings,data_encryption_key=b'S'*32)
    db=Database(initial.path,media_root=settings.data_dir,data_encryption_key=settings.data_encryption_key)
    planner=OralGateway();started=threading.Event()
    def slow():
        started.set();time.sleep(3)
    loopback['callback']=slow
    metrics={}
    try:
        with tcp_client(create_app(settings,model_gateway=planner,pet_audio_gateway=loopback['gateway']),{'X-HSC-Session':settings.session_token}) as client:
            client.headers['X-HSC-Parent']=post(client,'/v1/parent/setup',{'pin':'7391'})['parent_token']
            assert client.put('/v1/parent/privacy',json={'raw_media_storage_allowed':True,'cloud_model_text_allowed':True,'cloud_audio_allowed':True}).status_code==200
            post(client,f'/v1/parent/children/{CHILD}/pet-audio/authorization',dict(allowed=True,
                expires_at=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat(),day_limit_micros=100000,
                request_limit_micros=10000,expected_revision=0,action_id='slow-audio-purpose'))
            sid,prompt,_,_=begin_oral(client,db,planner)
            original_post=client.post
            def timed(path,*args,**kwargs):
                before=time.perf_counter();result=original_post(path,*args,**kwargs)
                if path.endswith('/diagnostic-prompt/answer'):metrics['original_ack_ms']=(time.perf_counter()-before)*1000
                return result
            monkeypatch.setattr(client,'post',timed)
            saved,_=answer(client,settings,monkeypatch,sid,prompt,planner,ANSWER,'slow-original')
            assert metrics['original_ack_ms']<2500,metrics
            assert saved['original_audio_teaching']['state']=='queued'
            assert saved['local_learning_evaluation']['status']=='audio_feedback_pending'
            turn=saved['local_learning_evaluation']['feedback_turn_id']
            with ThreadPoolExecutor(max_workers=1) as pool:
                # The lifespan worker owns the first dispatch; status GETs only read it.
                assert started.wait(10)
                assert client.get(f'/v1/agent-turns/{turn}/execution-status').json()['status']=='in_progress'
                before=time.perf_counter()
                assert client.get('/v1/health').status_code==200
                capture(client,settings,monkeypatch,sid,'先等一下',None,'new-speech-during-sse')
                metrics['new_voice_ms']=(time.perf_counter()-before)*1000
                before=time.perf_counter()
                assert client.post(f'/v1/sessions/{sid}/observation/pause',json={'client_action_id':'pause-during-sse'}).status_code==200
                metrics['pause_ms']=(time.perf_counter()-before)*1000
                assert metrics['new_voice_ms']<1500 and metrics['pause_ms']<1500,metrics
                deadline=time.monotonic()+12
                while time.monotonic()<deadline:
                    with db.connect() as c:
                        state=c.execute('SELECT state FROM pet_audio_work').fetchone()[0]
                    if state!='running':break
                    time.sleep(.05)
                assert state!='running'
            with db.connect() as c:
                row=c.execute('SELECT * FROM pet_audio_work').fetchone()
                assert row['result_json'] is None
                assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0]==1
            assert len(loopback['posts'])==1
    finally:
        print('PET_SLOW_SSE_METRICS '+json.dumps(metrics,sort_keys=True))
