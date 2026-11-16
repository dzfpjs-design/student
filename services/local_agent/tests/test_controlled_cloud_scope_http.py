"""Actual local ASR binding and HTTP scope; declared recognizer/model only."""
import base64
from datetime import datetime, timezone
import pytest
from home_study_agent.database import Database
from home_study_agent.local_media_ai import LocalMediaAI
from test_app import client_for
from test_learning_runtime_http import NoCloud
from test_controlled_safe_stop_http import wav_file
from test_controlled_cloud_semantics_http import prepare, provider, Semantic, request_start
from test_controlled_start_atomic_http import CHILD


def capture(client, tmp_path, monkeypatch, sid, quote, key):
    monkeypatch.setattr(LocalMediaAI, '_recognize_wav', lambda *a, **k: (quote,1000,'declared-local-synthetic',False))
    path=tmp_path/'controlled.wav'
    wav_file(path)
    result=client.post(f'/v1/sessions/{sid}/asr',json={'wav_base64':base64.b64encode(path.read_bytes()).decode(),
        'purpose':'agent_command','client_action_id':key,'capture_reference':{'child_id':CHILD}})
    assert result.status_code==200,result.text
    assert result.json()['transcript']==quote
    return result.json()['voice_input_id']


@pytest.mark.parametrize('kind',['content','games'])
def test_actual_asr_natural_stop_binds_current_original_only(tmp_path,monkeypatch,kind):
    db=Database(tmp_path/'test.db')
    with client_for(tmp_path,model_gateway=NoCloud(),agent_runtime_enabled=True) as client:
        sid,pid,path,_=prepare(client,db,tmp_path,kind)
        current=client.post(path+'/start',json={'query':'图形练习','client_action_id':'current-activity'})
        assert current.status_code==200,current.text
        identity='content_session_id' if kind=='content' else 'game_session_id'
        original=current.json()[identity]
        quote='先把这个关掉'
        voice=capture(client,tmp_path,monkeypatch,sid,quote,'original-stop-audio')
        gateway=Semantic('controlled_content.stop' if kind=='content' else 'controlled_game.stop',quote)
        provider(client,gateway)
        response=client.post(f'/v1/sessions/{sid}/agent-turn',json={'transcript':quote,'voice_input_id':voice,
            'client_action_id':'semantic-stop','cloud_text_allowed':True})
        assert response.status_code==200,response.text
        turn=response.json()
        assert turn['action']['type']==('stop_controlled_content' if kind=='content' else 'stop_controlled_game'),turn
        assert turn['action']['controlled_stop_binding']['activity_id']==original
        assert turn['action']['voice_input_binding']['voice_input_id']==voice
        assert len(gateway.requests)==1
        admission=client.post(f"/v1/sessions/{sid}/agent-turns/{turn['agent_turn_id']}/control-admission")
        assert admission.status_code==200,admission.text
        # Classification/admission is not a native-close receipt or backend end.
        with db.connect() as c:
            table='controlled_content_session' if kind=='content' else 'controlled_game_session'
            assert c.execute(f'SELECT status FROM {table} WHERE {identity}=?',(original,)).fetchone()[0]=='active'
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0]==0
            assert c.execute("SELECT count(*) FROM plan_item_event WHERE plan_item_id=? AND event_type='resumed'",(pid,)).fetchone()[0]==0
        # Protocol-only stand-in for a client bridge: this tests persisted end
        # reconciliation, not a native window/process closure claim.
        end_reason='time_limit' if kind=='content' else 'synthetic_protocol_close'
        ended=client.post(path+'/end',json={identity:original,'reason':end_reason,'actual_seconds':0})
        assert ended.status_code==200,ended.text
        outcome=client.put(f"/v1/agent-turns/{turn['agent_turn_id']}/outcome",json={
            'tool_status':'completed','final_response':'原活动结束记录已核对。',
            'result':{identity:original,'status':ended.json()['status']}})
        assert outcome.status_code==409,outcome.text
        assert outcome.json()['code']=='DEVICE_RECEIPT_REQUIRED'
        from home_study_agent.voice_control_intent import VoiceControlIntent
        with db.connect() as c:
            c.execute('BEGIN')
            assert VoiceControlIntent.inspect_execution(c,turn['agent_turn_id'])['control']==gateway.control
            assert c.execute('SELECT tool_status FROM agent_turn WHERE agent_turn_id=?',
                             (turn['agent_turn_id'],)).fetchone()[0]=='pending'
        receipt={'capability':gateway.control,'execution_location':'client_device_bridge',
            'action_type':turn['action']['type'],'status':'completed',
            'completed_at':datetime.now(timezone.utc).isoformat(),
            'output':{'child_id':CHILD,'session_id':sid,identity:original,'status':ended.json()['status'],
                      'device_closed':True,'task_resumed':False}}
        envelope={'tool_status':'completed','final_response':'原活动的关闭和结束记录已经核对。没有自动恢复学习。',
                  'result':{'execution_receipt':receipt,'client_bridge_receipt':receipt}}
        # Explicit synthetic bridge receipt with the exact production envelope.
        # Actual OS close/start/end is a separate Electron acceptance gate.
        completed=client.put(f"/v1/agent-turns/{turn['agent_turn_id']}/outcome",json=envelope)
        assert completed.status_code==200,completed.text
        assert completed.json()['tool_status']=='completed'
    with client_for(tmp_path,model_gateway=NoCloud(),agent_runtime_enabled=True) as client:
        replay=client.put(f"/v1/agent-turns/{turn['agent_turn_id']}/outcome",json=envelope)
        assert replay.status_code==200,replay.text
        assert replay.json()['agent_turn_id']==turn['agent_turn_id']
        assert replay.json()['action']['controlled_stop_binding']['activity_id']==original
        with db.connect() as c:
            assert c.execute(f'SELECT status FROM {table} WHERE {identity}=?',(original,)).fetchone()[0]==ended.json()['status']
            assert c.execute("SELECT count(*) FROM plan_item_event WHERE plan_item_id=? AND event_type='resumed'",(pid,)).fetchone()[0]==0


@pytest.mark.parametrize('change',['new_activity','source_replaced','child_switch'])
def test_late_captured_utterance_never_borrows_new_scope(tmp_path,monkeypatch,change):
    db=Database(tmp_path/'test.db')
    with client_for(tmp_path,model_gateway=NoCloud(),agent_runtime_enabled=True) as client:
        sid,_,path,_=prepare(client,db,tmp_path,'content')
        quote='我想看看图形练习这段视频' if change=='source_replaced' else '先把这个关掉'
        control='controlled_content.play' if change=='source_replaced' else 'controlled_content.stop'
        voice=capture(client,tmp_path,monkeypatch,sid,quote,'old-audio')
        if change=='new_activity':
            assert client.post(path+'/start',json={'query':'图形练习','client_action_id':'new-activity'}).status_code==200
        elif change=='source_replaced':
            (tmp_path/'video'/'inert.mp4').write_bytes(b'new synthetic source after audio')
        else:
            switched=client.post(f'/v1/sessions/{sid}/switch-child',json={'target_child_id':'child-primary-4','client_action_id':'switch-child'})
            assert switched.status_code==200,switched.text
            sid=switched.json()['session_id']
        gateway=Semantic(control,quote,'图形练习' if control.endswith('.play') else None)
        provider(client,gateway)
        response=client.post(f'/v1/sessions/{sid}/agent-turn',json={'transcript':quote,'voice_input_id':voice,
            'client_action_id':'late-request','cloud_text_allowed':True})
        assert response.status_code in (200,409),response.text
        if response.status_code==200:
            assert response.json()['action']['type']=='none',response.json()
        with db.connect() as c:
            expected=2 if change=='new_activity' else 1
            assert c.execute('SELECT count(*) FROM controlled_content_session').fetchone()[0]==expected


@pytest.mark.parametrize('quote',['结束播放','不看了'])
def test_historical_video_does_not_steal_ambiguous_stop(tmp_path,quote):
    db=Database(tmp_path/'test.db')
    with client_for(tmp_path,model_gateway=NoCloud(),agent_runtime_enabled=True) as client:
        sid,_,_,_=prepare(client,db,tmp_path,'content')
        gateway=Semantic('controlled_content.stop',quote)
        provider(client,gateway)
        response=client.post(f'/v1/sessions/{sid}/agent-turn',json={'transcript':quote,
            'client_action_id':'ambiguous-history','cloud_text_allowed':True})
        assert response.status_code==200,response.text
        assert response.json()['action']['type']=='none',response.json()
        assert len(gateway.requests)==1


def test_cloud_off_does_not_launch_or_classify(tmp_path):
    db=Database(tmp_path/'test.db')
    with client_for(tmp_path,model_gateway=NoCloud(),agent_runtime_enabled=True) as client:
        sid,_,_,headers=prepare(client,db,tmp_path,'content')
        assert client.put('/v1/parent/privacy',headers=headers,json={'cloud_model_text_allowed':False}).status_code==200
        gateway=Semantic('controlled_content.play','我想看看图形练习这段视频','图形练习')
        provider(client,gateway)
        turn=request_start(client,sid)
        assert turn['action']['type']=='none'
        assert gateway.requests==[]
