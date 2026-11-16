from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import socket
import threading
import time
from contextlib import contextmanager

import httpx
import uvicorn

from home_study_agent.app import create_app
from home_study_agent.database import Database
from home_study_agent.repository import StudyRepository
from home_study_agent.reports import build_daily_report
from home_study_agent.report_source_fingerprint import report_source_fingerprint
from test_pet_oral_supply_http import setup, post, CHILD
from test_oral_runtime_feedback_http import OralGateway, begin_oral, ANSWER
from test_oral_followup_execution_http import answer
from test_voice_runtime_binding import capture, interpret
from test_pet_audio_work import loopback


@contextmanager
def tcp_client(app, headers):
    sock=socket.socket()
    sock.bind(('127.0.0.1',0))
    port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='error',access_log=False))
    errors=[]
    def serve():
        try: server.run(sockets=[sock])
        except BaseException as error: errors.append(error)
    thread=threading.Thread(target=serve,name='pet-audio-backend-tcp')
    thread.start()
    try:
        deadline=time.monotonic()+40
        while not server.started and thread.is_alive() and time.monotonic()<deadline:
            time.sleep(.02)
        assert server.started and not errors, errors
        with httpx.Client(base_url=f'http://127.0.0.1:{port}',headers=headers,trust_env=False,timeout=40) as client:
            yield client
    finally:
        server.should_exit=True
        thread.join(timeout=45)
        sock.close()
        assert not thread.is_alive() and not errors, errors


def test_original_audio_automatic_feedback_runtime_new_capture_and_restart(tmp_path,monkeypatch,loopback):
    initial,settings=setup(tmp_path)
    settings=replace(settings,data_encryption_key=b'A'*32)
    db=Database(initial.path,media_root=settings.data_dir,data_encryption_key=settings.data_encryption_key)
    planner=OralGateway()
    headers={'X-HSC-Session':settings.session_token}
    kwargs=dict(model_gateway=planner,pet_audio_gateway=loopback['gateway'])
    with tcp_client(create_app(settings,**kwargs),headers) as client:
        token=post(client,'/v1/parent/setup',{'pin':'7391'})['parent_token']
        client.headers['X-HSC-Parent']=token
        assert client.put('/v1/parent/privacy',json={'raw_media_storage_allowed':True,
            'cloud_model_text_allowed':True,'cloud_audio_allowed':True}).status_code==200
        path=f'/v1/parent/children/{CHILD}/pet-audio/authorization'
        grant=dict(allowed=True,expires_at=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat(),
            day_limit_micros=100000,request_limit_micros=10000,expected_revision=0,action_id='parent-grant')
        assert post(client,path,grant)['grant']['revision']==1
        assert post(client,path,grant)['grant']['revision']==1
        receipt=client.get(path+'/actions/parent-grant').json()
        assert receipt['child_id']==CHILD and receipt['state']=='applied' and receipt['grant']['action_id']=='parent-grant'
        assert client.get(path.replace(CHILD,'child-junior-3')+'/actions/parent-grant').json()['state']=='missing'
        sid,prompt,_,_=begin_oral(client,db,planner)
        saved,voice=answer(client,settings,monkeypatch,sid,prompt,planner,ANSWER,'automatic-first')
        assert saved['original_audio_teaching']['state']=='queued',saved.get('original_audio_teaching')
        tid=saved['local_learning_evaluation']['feedback_turn_id']
        def read_feedback(turn):
            deadline=time.monotonic()+12
            while time.monotonic()<deadline:
                status=client.get(f'/v1/agent-turns/{turn}/execution-status').json()
                if status['status']=='completed':return status['execution']['execution']
                waiting_receipt = (status['status']=='unknown'
                    and status.get('original_audio_teaching',{}).get('state')=='response_saved'
                    and status['original_audio_teaching'].get('delivery_status')=='awaiting_receipt')
                assert status['status']=='in_progress' or waiting_receipt,status
                time.sleep(.05)
            raise AssertionError('Managed original-audio worker did not deliver')
        feedback=read_feedback(tid)
        assert feedback['result']['oral_feedback']['status']=='formative_audio_feedback',feedback
        ref=feedback['result']['oral_feedback']['audio_work_ref']
        assert ref['work_id']==saved['original_audio_teaching']['work_id']
        assert feedback['result']['objective_update_applied'] is False
        assert len(loopback['posts'])==1
        assert post(client,f'/v1/agent-turns/{tid}/execute',{})['execution']['result']['oral_feedback']['audio_work_ref']==ref
        planner.quote='我想按刚才的建议补充一下，再说一次'
        planner.requested_control='oral.retry'
        control_voice=capture(client,settings,monkeypatch,sid,planner.quote,prompt,'automatic-retry-control')
        command=interpret(client,sid,planner.quote,'automatic-retry',control_voice)
        assert command['runtime_capability']=='oral.retry',command
        retry=post(client,f"/v1/agent-turns/{command['agent_turn_id']}/execute",{})['execution']['result']
        next_prompt=retry['prompt']
        assert next_prompt['prompt_id']!=prompt['prompt_id']
        with db.connect() as c:
            request=json.loads(c.execute('SELECT request_json FROM diagnostic_prompt WHERE prompt_id=?',(next_prompt['prompt_id'],)).fetchone()[0])
            assert request['oral_followup']['action']['audio_work_ref']==ref
        second,new_voice=answer(client,settings,monkeypatch,sid,next_prompt,planner,
            'I would visit the garden with my sister because we enjoy the flowers together.','automatic-second')
        assert new_voice not in {voice,control_voice}
        assert second['original_audio_teaching']['state']=='queued',second.get('original_audio_teaching')
        assert read_feedback(second['local_learning_evaluation']['feedback_turn_id'])['result']['oral_feedback']['status']=='formative_audio_feedback'
        assert second['attempt']['score_status']=='needs_review'
        assert len(loopback['posts'])==2
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM rubric_review_event').fetchone()[0]==0
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0]==2
    with tcp_client(create_app(settings,**kwargs),headers) as client:
        client.headers['X-HSC-Parent']=post(client,'/v1/parent/unlock',{'pin':'7391'})['parent_token']
        tid=second['local_learning_evaluation']['feedback_turn_id']
        replay=post(client,f'/v1/agent-turns/{tid}/execute',{})
        assert replay['execution']['result']['oral_feedback']['status']=='formative_audio_feedback'
        assert len(loopback['posts'])==2
        with db.connect() as c:
            c.execute("UPDATE daily_session SET state='reflection' WHERE session_id=?",(sid,))
            c.commit()
        repo=StudyRepository(db)
        repo.complete_with_reflection(sid,'','','','',True,'audio-reflection')
        evidence=repo.report_evidence(sid)
        assert len(evidence['pet_audio_practice'])==2
        assert sum(len(item['practices']) for item in evidence['pet_audio_practice'])==1
        summary,coaching=build_daily_report(evidence)
        assert any('不计为掌握' in fact for fact in summary['facts'])
        report=repo.save_daily_report(sid,summary,coaching)
        assert len(report['summary']['pet_audio_practice'])==2
        exported=post(client,f'/v1/parent/children/{CHILD}/export',{})
        contents=json.loads(Path(exported['path']).read_text())
        assert len(contents['pet_audio_work'])==2
        assert all(row['child_id']==CHILD for row in contents['pet_audio_work']+contents['pet_audio_grants']+contents['pet_audio_dispatches'])
        with db.connect() as c:
            c.execute('BEGIN')
            before=report_source_fingerprint(c,sid)
        assert post(client,path,{**grant,'allowed':False,'expected_revision':1,'action_id':'revoke-purpose'})['grant']['revision']==2
        current=client.get(f'/v1/agent-turns/{tid}/execution-status').json()['execution']['execution']
        assert current['result']['oral_feedback']['status']!='formative_audio_feedback'
        assert current['result']['oral_feedback'].get('audio_work_ref') is None
        with db.connect() as c:
            c.execute('BEGIN')
            assert report_source_fingerprint(c,sid)!=before
        assert repo.get_daily_report(report['report_id'])['summary']['pet_audio_practice']==[]
