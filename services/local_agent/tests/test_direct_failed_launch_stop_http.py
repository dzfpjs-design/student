"""Actual local HTTP with synthetic ASR; no native closure or model-network claim."""
import json
from datetime import datetime, timezone
from uuid import uuid4
import pytest
from home_study_agent.database import Database
from test_app import client_for
from test_learning_runtime_http import NoCloud
from test_controlled_failed_launch_cancel_http import setup, fail_start, capture
from test_controlled_cloud_semantics_http import provider, CHILD


class NeverCalled:
    configured = True
    def __init__(self): self.calls = 0
    async def complete(self, *_args, **_kwargs):
        self.calls += 1
        raise AssertionError('Direct safety stop must not require a model')


def stop(client, sid, voice, kind, *, key='direct-stop', cloud=False, quote=None):
    return client.post(f'/v1/sessions/{sid}/agent-turn', json={
        'transcript': quote or ('停止视频' if kind == 'content' else '停止游戏'),
        'client_action_id': key, 'voice_input_id': voice, 'cloud_text_allowed': cloud})


def facts(db, sid, kind):
    table = 'controlled_content_session' if kind == 'content' else 'controlled_game_session'
    with db.connect() as c:
        return {'resources': [tuple(r) for r in c.execute(f'SELECT * FROM {table} WHERE session_id=? ORDER BY rowid', (sid,))],
                'attempts': c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0],
                'events': [tuple(r) for r in c.execute('SELECT * FROM plan_item_event ORDER BY rowid')],
                'learning_goals': [tuple(r) for r in c.execute("SELECT objective_id,status FROM agent_objective WHERE session_id=? AND objective_type='learning' ORDER BY objective_id", (sid,))]}


@pytest.mark.parametrize('kind', ['content', 'games'])
@pytest.mark.parametrize('history', [False, True])
def test_direct_stop_cancels_original_failed_request_not_single_history(tmp_path, monkeypatch, kind, history):
    db = Database(tmp_path/'test.db')
    with client_for(tmp_path, model_gateway=NoCloud(), agent_runtime_enabled=True) as client:
        sid, _, path, parent = setup(client, db, tmp_path, kind)
        if history:
            old = client.post(path+'/start', json={'query':'图形练习二','client_action_id':'old-history'})
            assert old.status_code == 200, old.text
            field = 'content_session_id' if kind == 'content' else 'game_session_id'
            ended = client.post(path+'/end', json={field:old.json()[field], 'reason':'child_requested', 'actual_seconds':0})
            assert ended.status_code == 200, ended.text
        source = fail_start(client, sid, path, 'failed-original')
        quote = '停止视频' if kind == 'content' else '停止游戏'
        voice = capture(client, tmp_path, monkeypatch, sid, 'stop-asr', quote)
        # Alternate cloud-on/off request and persisted preference; neither is authority to stop.
        if not history:
            assert client.put('/v1/parent/privacy', headers=parent, json={'cloud_model_text_allowed':False}).status_code == 200
        gateway = NeverCalled(); provider(client, gateway)
        before = facts(db, sid, kind)
        response = stop(client, sid, voice, kind, cloud=history)
        assert response.status_code == 200, response.text
        turn = response.json(); binding = turn['action'].get('controlled_launch_cancel_binding')
        assert binding and binding['request_id'] == binding['source_turn_id'] == source, turn['action']
        assert 'controlled_stop_binding' not in turn['action']
        assert turn['requires_control_admission'] is True and gateway.calls == 0
        admission = client.post(f"/v1/sessions/{sid}/agent-turns/{turn['agent_turn_id']}/control-admission")
        assert admission.status_code == 200 and admission.json()['status'] == 'admitted', admission.text
        assert facts(db, sid, kind) == before
    with client_for(tmp_path, model_gateway=NoCloud(), agent_runtime_enabled=True) as client:
        replay = stop(client, sid, voice, kind, cloud=history)
        assert replay.status_code == 200, replay.text
        assert replay.json()['agent_turn_id'] == turn['agent_turn_id']
        assert replay.json()['action']['controlled_launch_cancel_binding'] == binding


@pytest.mark.parametrize('kind,case', [
    ('content','early'), ('games','multiple'), ('content','missing_capture'),
    ('games','new_active_before'), ('content','new_active_after'),
    ('games','source_changed'), ('content','wrong_request'), ('games','new_utterance'),
    ('content','other_child'), ('games','old_session'), ('content','cloud_revoked'),
])
def test_direct_failed_cancel_exact_scope_guards(tmp_path, monkeypatch, kind, case):
    db = Database(tmp_path/'test.db')
    with client_for(tmp_path, model_gateway=NoCloud(), agent_runtime_enabled=True) as client:
        sid, _, path, parent = setup(client, db, tmp_path, kind)
        quote = '停止视频' if kind == 'content' else '停止游戏'
        voice = capture(client,tmp_path,monkeypatch,sid,'early-voice',quote) if case=='early' else None
        source = fail_start(client,sid,path,'original-failed')
        if case not in {'early','missing_capture'}:
            voice = capture(client,tmp_path,monkeypatch,sid,'stop-voice',quote)
        if case=='multiple': fail_start(client,sid,path,'other-failed')
        if case=='new_active_before':
            r=client.post(path+'/start',json={'query':'图形练习二','client_action_id':'later-active'})
            assert r.status_code==200,r.text
        if case in {'other_child','old_session'}:
            switched=client.post(f'/v1/sessions/{sid}/switch-child',json={
                'target_child_id':'child-primary-4','client_action_id':'switch-child'})
            assert switched.status_code==200,switched.text
            target_sid=switched.json()['session_id'] if case=='other_child' else sid
            r=stop(client,target_sid,voice,kind)
            assert r.status_code in {404,409},r.text
            return
        provider(client,NeverCalled())
        before=facts(db,sid,kind)
        response=stop(client,sid,voice,kind)
        assert response.status_code==200,response.text
        action=response.json()['action']
        if case in {'early','multiple','missing_capture','new_active_before'}:
            assert action['type']=='none' and 'controlled_launch_cancel_binding' not in action,action
            assert facts(db,sid,kind)==before
            return
        turn=response.json()['agent_turn_id']
        assert action['controlled_launch_cancel_binding']['request_id']==source
        if case=='new_active_after':
            r=client.post(path+'/start',json={'query':'图形练习二','client_action_id':'later-active'})
            assert r.status_code==200,r.text
        elif case=='source_changed':
            with db.connect() as c:
                c.execute("UPDATE agent_turn SET action_json=json_set(action_json,'$.query','changed') WHERE agent_turn_id=?",(source,));c.commit()
        elif case=='wrong_request':
            with db.connect() as c:
                c.execute("UPDATE agent_turn SET action_json=json_set(action_json,'$.controlled_launch_cancel_binding.request_id','other-request') WHERE agent_turn_id=?",(turn,));c.commit()
        elif case=='new_utterance':
            r=client.post(f'/v1/sessions/{sid}/agent-turn',json={'transcript':'我想再想一想','client_action_id':'later-sentence','cloud_text_allowed':False})
            assert r.status_code==200,r.text
        elif case=='cloud_revoked':
            assert client.put('/v1/parent/privacy',headers=parent,json={'cloud_model_text_allowed':False}).status_code==200
        before=facts(db,sid,kind)
        admitted=client.post(f'/v1/sessions/{sid}/agent-turns/{turn}/control-admission')
        assert admitted.status_code==(200 if case=='cloud_revoked' else 409),admitted.text
        assert facts(db,sid,kind)==before


@pytest.mark.parametrize('kind',['content','games'])
def test_direct_cancel_retains_binding_after_server_fence_and_device_result(tmp_path,monkeypatch,kind):
    db=Database(tmp_path/'test.db')
    with client_for(tmp_path,model_gateway=NoCloud(),agent_runtime_enabled=True) as client:
        sid,_,path,_=setup(client,db,tmp_path,kind)
        source=fail_start(client,sid,path,'original-failed',unknown=False)
        quote='停止视频' if kind=='content' else '停止游戏'
        voice=capture(client,tmp_path,monkeypatch,sid,'stop-voice',quote)
        provider(client,NeverCalled())
        response=stop(client,sid,voice,kind);assert response.status_code==200,response.text
        turn=response.json();tid=turn['agent_turn_id'];native_kind='video' if kind=='content' else 'game'
        assert turn['action']['controlled_launch_cancel_binding']['request_id']==source
        assert client.post(f'/v1/sessions/{sid}/agent-turns/{tid}/control-admission').status_code==200
        intent=str(uuid4())
        cancelled=client.post(f'/v1/sessions/{sid}/controlled-launch-intents/{intent}/cancel-unstarted',
            json={'kind':native_kind,'request_id':source})
        assert cancelled.status_code==200 and cancelled.json()['activityId'] is None,cancelled.text
        # Exact production bridge envelope; synthetic device protocol, not OS closure proof.
        receipt={'capability':'controlled_content.stop' if kind=='content' else 'controlled_game.stop',
            'execution_location':'client_device_bridge','action_type':turn['action']['type'],
            'status':'completed','completed_at':datetime.now(timezone.utc).isoformat(),
            'output':{'request_id':source,'native_intent_id':intent,'status':'cancelled_before_start',
                'device_closed':True,'no_activity':True,'task_resumed':False}}
        envelope={'tool_status':'completed','final_response':'原启动请求已取消，没有打开活动，也没有自动恢复学习。',
            'result':{'execution_receipt':receipt,'client_bridge_receipt':receipt}}
        before=facts(db,sid,kind)
        completed=client.put(f'/v1/agent-turns/{tid}/outcome',json=envelope)
        assert completed.status_code==200,completed.text
        assert completed.json()['tool_status']=='completed'
        assert facts(db,sid,kind)==before
    with client_for(tmp_path,model_gateway=NoCloud(),agent_runtime_enabled=True) as client:
        replay=client.put(f'/v1/agent-turns/{tid}/outcome',json=envelope)
        assert replay.status_code==200,replay.text
        assert replay.json()['agent_turn_id']==tid
