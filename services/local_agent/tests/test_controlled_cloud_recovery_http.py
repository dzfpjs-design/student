"""Recovery/negative boundary probes, synthetic transports, no native launch."""
import json
import pytest
from home_study_agent.database import Database
from home_study_agent.model_gateway import ModelResult
from test_app import client_for
from test_learning_runtime_http import NoCloud
from test_controlled_cloud_semantics_http import prepare, provider, Semantic
from test_controlled_cloud_scope_http import capture
from test_pending_evaluation_control import host, ask, facts


@pytest.mark.parametrize('control',['controlled_content.play','controlled_game.play'])
def test_pending_actual_http_never_grants_new_external_launch(tmp_path,control):
    class RejectedProvider:
        configured=True
        def __init__(self): self.calls=[]
        async def complete(self,request):
            self.calls.append(request)
            assert control not in request.messages[0]['content']
            quote=json.loads(request.messages[-1]['content'])['transcript']
            return ModelResult(content=json.dumps({'role':'control','control':control,
                'quote':quote,'confidence':.99,'query':'图形练习'}),provider='synthetic',model='invalid-choice',latency_ms=1)
    with host(tmp_path) as (db,sid,client,_):
        model=RejectedProvider()
        provider(client,model)
        before=facts(db)
        response=ask(client,sid,'现在打开图形练习吧')
        assert response.status_code==200,response.text
        assert response.json()['action']['type']=='none'
        assert response.json()['action']['pending_evaluation_control']['evaluation_turn_id']=='eval'
        assert len(model.calls)==1 and facts(db)==before
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM controlled_content_session').fetchone()[0]==0
            assert c.execute('SELECT count(*) FROM controlled_game_session').fetchone()[0]==0


def test_quoted_name_real_asr_start_response_loss_restart_keeps_original(tmp_path,monkeypatch):
    db=Database(tmp_path/'test.db')
    quote='我想播放“图形练习”这段视频'
    gateway=Semantic('controlled_content.play',quote,'图形练习')
    with client_for(tmp_path,model_gateway=NoCloud(),agent_runtime_enabled=True) as client:
        sid,_,path,_=prepare(client,db,tmp_path,'content')
        voice=capture(client,tmp_path,monkeypatch,sid,quote,'named-original-audio')
        provider(client,gateway)
        response=client.post(f'/v1/sessions/{sid}/agent-turn',json={'transcript':quote,'voice_input_id':voice,
            'client_action_id':'named-start','cloud_text_allowed':True})
        assert response.status_code==200,response.text
        turn=response.json()
        assert turn['action']['type']=='start_controlled_content',turn
        assert turn['action']['voice_input_binding']['voice_input_id']==voice
        assert client.post(f"/v1/sessions/{sid}/agent-turns/{turn['agent_turn_id']}/control-admission").status_code==200
        body={'query':turn['action']['query'],'client_action_id':turn['agent_turn_id'],
              'selection_binding':turn['action']['selection_binding']}
        first=client.post(path+'/start',json=body)
        assert first.status_code==200,first.text
        activity=first.json()['content_session_id']
        with db.connect() as c:
            original=[tuple(r) for r in c.execute('SELECT * FROM plan_item_event')]
    with client_for(tmp_path,model_gateway=gateway,agent_runtime_enabled=True) as client:
        replay=client.post(path+'/start',json=body)
        assert replay.status_code==200,replay.text
        assert replay.json()['content_session_id']==activity
        assert replay.json()['launch_allowed'] is False
        with db.connect() as c:
            # Runtime may add truthful shutdown lifecycle, but cannot add a new
            # media pause/start or duplicate the already committed original.
            assert c.execute('SELECT count(*) FROM controlled_content_session').fetchone()[0]==2
            assert [tuple(r) for r in c.execute('SELECT * FROM plan_item_event')][:len(original)]==original
        assert len(gateway.requests)==1
