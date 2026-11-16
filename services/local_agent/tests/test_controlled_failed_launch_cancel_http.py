"""Actual HTTP/SQLite authority tests; native closure is tested by the Electron harness."""
import base64
import json
from uuid import uuid4

import pytest
from home_study_agent.database import Database
from home_study_agent.local_media_ai import LocalMediaAI
from test_app import client_for
from test_controlled_cloud_semantics_http import prepare, provider, Semantic, CHILD
from test_learning_runtime_http import NoCloud
from test_pet_media_evidence import wav_file


@pytest.fixture(params=['content', 'games'])
def kind(request):
    return request.param


def setup(client, db, folder, kind):
    sid, pid, path, parent = prepare(client, db, folder, kind)
    if kind == 'games':
        executable = folder / 'second-inert.exe'
        executable.write_bytes(b'synthetic protocol only; never launched')
        result = client.post(f'/v1/parent/children/{CHILD}/controlled-games/approvals', headers=parent,
            json={'title': '图形练习二', 'executable_path': str(executable), 'max_minutes': 6})
        assert result.status_code == 200, result.text
        return sid, pid, path, parent
    with db.connect() as c:
        row = dict(c.execute("SELECT * FROM curriculum_resource WHERE corpus_id='semantic-media'").fetchone())
        row.pop('row_id')
        row.update(external_resource_id='102', title='图形练习二')
        keys = list(row)
        rid = c.execute('INSERT INTO curriculum_resource('+','.join(keys)+') VALUES('+','.join('?' for _ in keys)+')',
                        [row[k] for k in keys]).lastrowid
        c.commit()
    r = client.post(f'/v1/parent/children/{CHILD}/controlled-content/approvals', headers=parent,
                    json={'curriculum_row_id': rid, 'max_minutes': 6})
    assert r.status_code == 200, r.text
    return sid, pid, path, parent


def fail_start(client, sid, path, key, *, unknown=True):
    game = path.endswith('controlled-games')
    quote = '我想玩玩图形这个游戏' if game else '我想看看图形这段视频'
    provider(client, Semantic('controlled_game.play' if game else 'controlled_content.play', quote, '图形'))
    r = client.post(f'/v1/sessions/{sid}/agent-turn', json={'transcript': quote,
        'client_action_id': key, 'cloud_text_allowed': True})
    assert r.status_code == 200, r.text
    turn = r.json()
    assert turn['action']['type'] == ('start_controlled_game' if game else 'start_controlled_content'), turn
    tid = turn['agent_turn_id']
    r = client.post(f'/v1/sessions/{sid}/agent-turns/{tid}/control-admission')
    assert r.status_code == 200, r.text
    r = client.post(path+'/start', json={'query': '图形', 'client_action_id': tid})
    assert r.status_code == 409 and r.json()['code'] == (
        'CONTROLLED_GAME_SELECTION_REQUIRED' if game else 'CONTROLLED_CONTENT_SELECTION_REQUIRED'), r.text
    r = client.put(f'/v1/agent-turns/{tid}/outcome', json={'tool_status':'failed',
        'tool_error_code':'CONTROLLED_START_NATIVE_UNCONFIRMED' if unknown else 'CLIENT_TOOL_ACTION_FAILED',
        'final_response':'原启动是否执行仍待核对。' if unknown else '原请求已拒绝。'})
    assert r.status_code == 200, r.text
    return tid


def capture(client, folder, monkeypatch, sid, key, quote='停止原活动'):
    monkeypatch.setattr(LocalMediaAI, '_recognize_wav', lambda *a, **k: (quote, 1000, 'synthetic-no-acoustics', False))
    path = folder/'synthetic.wav'
    wav_file(path)
    r = client.post(f'/v1/sessions/{sid}/asr', json={'wav_base64':base64.b64encode(path.read_bytes()).decode(),
        'purpose':'agent_command','client_action_id':key,'capture_reference':{'child_id':CHILD}})
    assert r.status_code == 200, r.text
    return r.json()['voice_input_id']


def cancel_turn(client, sid, voice, kind, key='cancel-original', quote='停止原活动'):
    gateway = Semantic('controlled_game.stop' if kind == 'games' else 'controlled_content.stop',quote)
    provider(client, gateway)
    r = client.post(f'/v1/sessions/{sid}/agent-turn', json={'transcript':quote,
        'client_action_id':key,'voice_input_id':voice,'cloud_text_allowed':True})
    assert r.status_code == 200, r.text
    wire = json.dumps([x.messages for x in gateway.requests], ensure_ascii=False)
    assert all(secret not in wire for secret in (sid, CHILD, voice, 'source_action_sha256', 'request_id', 'Synthetic reference'))
    return r.json()


def test_original_failed_request_asr_binding_restart_no_activity_or_new_scoring(tmp_path, monkeypatch, kind):
    db = Database(tmp_path/'test.db')
    with client_for(tmp_path,model_gateway=NoCloud(),agent_runtime_enabled=True) as client:
        sid, pid, path, _ = setup(client,db,tmp_path,kind)
        source = fail_start(client,sid,path,'failed-start')
        voice = capture(client,tmp_path,monkeypatch,sid,'cancel-asr')
        turn = cancel_turn(client,sid,voice,kind)
        binding = turn['action']['controlled_launch_cancel_binding']
        assert binding['request_id'] == binding['source_turn_id'] == source
        assert binding['kind'] == ('game' if kind == 'games' else 'video')
        assert turn['requires_control_admission'] is True
        with db.connect() as c:
            table = 'controlled_game_session' if kind == 'games' else 'controlled_content_session'
            assert c.execute(f'SELECT COUNT(*) FROM {table} WHERE client_action_id=?',(source,)).fetchone()[0] == 0
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
    with client_for(tmp_path,model_gateway=NoCloud(),agent_runtime_enabled=True) as client:
        replay = cancel_turn(client,sid,voice,kind)
        assert replay['agent_turn_id'] == turn['agent_turn_id']
        assert replay['action']['controlled_launch_cancel_binding'] == binding


@pytest.mark.parametrize('case',['before_failure','multiple','source_changed','new_active','other_child','consent_withdrawn'])
def test_failed_launch_does_not_borrow_later_different_or_revoked_request(tmp_path,monkeypatch,case,kind):
    db=Database(tmp_path/'test.db')
    with client_for(tmp_path,model_gateway=NoCloud(),agent_runtime_enabled=True) as client:
        sid,pid,path,parent=setup(client,db,tmp_path,kind)
        if case=='before_failure': voice=capture(client,tmp_path,monkeypatch,sid,'early-voice')
        source=fail_start(client,sid,path,'failed-source')
        if case!='before_failure': voice=capture(client,tmp_path,monkeypatch,sid,'cancel-voice')
        if case=='multiple': fail_start(client,sid,path,'second-failure')
        turn=cancel_turn(client,sid,voice,kind)
        if case in {'before_failure','multiple'}:
            assert 'controlled_launch_cancel_binding' not in turn['action']
            return
        assert turn['action']['controlled_launch_cancel_binding']['request_id']==source
        if case=='source_changed':
            with db.connect() as c:
                c.execute("UPDATE agent_turn SET action_json=json_set(action_json,'$.query','changed') WHERE agent_turn_id=?",(source,));c.commit()
        elif case=='new_active':
            r=client.post(path+'/start',json={'query':'图形练习二','client_action_id':'different-active'})
            assert r.status_code==200,r.text
        elif case=='other_child':
            r=client.post(f'/v1/sessions/{sid}/switch-child',json={'target_child_id':'child-primary-4','client_action_id':'switch-child'})
            assert r.status_code==200,r.text
        else:
            r=client.put('/v1/parent/privacy',headers=parent,json={'cloud_model_text_allowed':False})
            assert r.status_code==200,r.text
        r=client.post(f"/v1/sessions/{sid}/agent-turns/{turn['agent_turn_id']}/control-admission")
        assert r.status_code==409,r.text
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==0


@pytest.mark.parametrize('quote', ['停止原活动', '停止原视频'])
def test_two_failed_resource_kinds_require_explicit_type(tmp_path, monkeypatch, quote):
    db = Database(tmp_path/'test.db')
    with client_for(tmp_path,model_gateway=NoCloud(),agent_runtime_enabled=True) as client:
        sid, _, video_path, parent = setup(client, db, tmp_path, 'content')
        video = fail_start(client, sid, video_path, 'unknown-video')
        game_parent = f'/v1/parent/children/{CHILD}/controlled-games'
        rule = client.put(game_parent+'/rule', headers=parent, json={
            'enabled':True, 'max_daily_minutes':20, 'max_item_minutes':8})
        assert rule.status_code == 200, rule.text
        for number in (1, 2):
            executable = tmp_path/f'never-launched-{number}.exe'
            executable.write_bytes(b'synthetic protocol only; never launched')
            approved = client.post(game_parent+'/approvals', headers=parent, json={
                'title':f'图形游戏{number}', 'executable_path':str(executable), 'max_minutes':6})
            assert approved.status_code == 200, approved.text
        game = fail_start(client, sid, f'/v1/sessions/{sid}/controlled-games', 'unknown-game')
        voice = capture(client, tmp_path, monkeypatch, sid, 'two-kinds-asr', quote)
        turn = cancel_turn(client, sid, voice, 'content', quote=quote)
        if quote == '停止原活动':
            assert 'controlled_launch_cancel_binding' not in turn['action'], turn['action']
        else:
            assert turn['action']['controlled_launch_cancel_binding']['request_id'] == video
        with db.connect() as c:
            from home_study_agent.controlled_launch_cancel import failed_starts
            assert [row['request_id'] for row in failed_starts(c, CHILD, sid, 'content')] == [video]
            assert [row['request_id'] for row in failed_starts(c, CHILD, sid, 'games')] == [game]
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0


def test_server_cancel_receipt_alone_keeps_original_unknown_not_old_confirmed_failures(tmp_path,monkeypatch,kind):
    db=Database(tmp_path/'test.db')
    with client_for(tmp_path,model_gateway=NoCloud(),agent_runtime_enabled=True) as client:
        sid,pid,path,parent=setup(client,db,tmp_path,kind)
        native_kind = 'game' if kind == 'games' else 'video'
        old=fail_start(client,sid,path,'old-known-rejected',unknown=False)
        old_intent=str(uuid4())
        r=client.post(f'/v1/sessions/{sid}/controlled-launch-intents/{old_intent}/cancel-unstarted',
            json={'kind':native_kind,'request_id':old})
        assert r.status_code==200,r.text
        source=fail_start(client,sid,path,'new-unknown-rejected')
        intent=str(uuid4())
        r=client.post(f'/v1/sessions/{sid}/controlled-launch-intents/{intent}/cancel-unstarted',
            json={'kind':native_kind,'request_id':source})
        assert r.status_code==200,r.text
        assert r.json()['status']=='cancelled_before_start' and r.json()['activityId'] is None
        # No native completion is fabricated here; actual IPC loss/retry lives in the native harness.
        voice=capture(client,tmp_path,monkeypatch,sid,'after-server-cancel')
        turn=cancel_turn(client,sid,voice,kind)
        assert turn['action']['controlled_launch_cancel_binding']['request_id']==source
        r=client.post(f"/v1/sessions/{sid}/agent-turns/{turn['agent_turn_id']}/control-admission")
        assert r.status_code==200,r.text
        with db.connect() as c:
            from home_study_agent.controlled_launch_cancel import failed_starts
            assert [x['request_id'] for x in failed_starts(c,CHILD,sid,kind)]==[source]
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==0
