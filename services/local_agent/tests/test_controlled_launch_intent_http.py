from uuid import uuid4
import json
import hashlib
import hmac
import pytest
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.database import Database
from test_learning_runtime_http import NoCloud
from test_controlled_cloud_semantics_http import prepare
from test_controlled_end_identity_http import identity_key

KEY='a'*64  # Synthetic main/backend-only key, not a renderer session token.
def client_for(tmp_path,model_gateway,agent_runtime_enabled=True,key=KEY):
    return TestClient(create_app(Settings(data_dir=tmp_path,database_path=tmp_path/'test.db',
        session_token='synthetic-api-token',native_launch_attestation_key=key,data_encryption_key=b'0'*32,
        agent_runtime_enabled=agent_runtime_enabled,maintenance_enabled=False),model_gateway=model_gateway),headers={'X-HSC-Session':'synthetic-api-token'})

def native_proof(binding,key=KEY):
    nonce=str(uuid4());revision=3;proof='cancelled_before_dispatch'
    values=['native_launch_absence_v1',binding['childId'],binding['sessionId'],binding['kind'],binding['requestId'],binding['intentId'],binding['activityId'],proof,nonce,revision]
    signature=hmac.new(bytes.fromhex(key),json.dumps(values,ensure_ascii=False,separators=(',',':')).encode(),hashlib.sha256).hexdigest()
    return {'activity_id':binding['activityId'],'proof':proof,'nonce':nonce,'native_revision':revision,'signature':signature}


@pytest.mark.parametrize('kind',['content','games'])
def test_start_intent_exact_receipt_restart_no_repeat_pause_or_retrofit(tmp_path,kind):
    db=Database(tmp_path/'test.db');intent=str(uuid4())
    with client_for(tmp_path,model_gateway=NoCloud(),agent_runtime_enabled=True) as client:
        sid,pid,path,_=prepare(client,db,tmp_path,kind)
        body={'query':'图形练习','client_action_id':'intent-original-key','native_intent_id':intent}
        started=client.post(path+'/start',json=body)
        assert started.status_code==200,started.text
        original=started.json()[identity_key(kind)]
        lookup=f'/v1/sessions/{sid}/controlled-launch-intents/{intent}'
        read=client.get(lookup)
        assert read.status_code==200,read.text
        assert read.headers['cache-control']=='no-store'
        assert read.json()['activityId']==original
        assert read.json()['requestId']==body['client_action_id']
        assert read.json()['intentId']==intent
        assert 'local_path' not in read.json() and 'executable_path' not in read.json()
        assert client.get(lookup.replace(sid,'wrong-session')).status_code==404
        with db.connect() as c:
            before=c.execute('SELECT count(*) FROM plan_item_event WHERE plan_item_id=?',(pid,)).fetchone()[0]
        repeated=client.post(path+'/start',json=body)
        assert repeated.status_code==200,repeated.text
        assert repeated.json()[identity_key(kind)]==original and repeated.json()['launch_allowed'] is False
        mismatch=client.post(path+'/start',json={**body,'native_intent_id':str(uuid4())})
        assert mismatch.status_code==409,mismatch.text
        legacy=client.post(path+'/start',json={'query':'图形练习','client_action_id':'original-activity','native_intent_id':str(uuid4())})
        assert legacy.status_code==409,legacy.text
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM plan_item_event WHERE plan_item_id=?',(pid,)).fetchone()[0]==before
    with client_for(tmp_path,model_gateway=NoCloud(),agent_runtime_enabled=True) as client:
        restored=client.get(lookup)
        assert restored.status_code==200,restored.text
        assert restored.json()['activityId']==original
        assert restored.json()['status']=='interrupted'
        signed=native_proof(restored.json())
        for mutation in ({'signature':'0'*64},{'nonce':str(uuid4())},{'native_revision':4},{'activity_id':'different'},{'proof':'owner_exit_before_registration'}):
            rejected=client.post(lookup+'/native-not-dispatched',json={**signed,**mutation})
            assert rejected.status_code==409,rejected.text
        cancelled=client.post(lookup+'/native-not-dispatched',json=signed)
        assert cancelled.status_code==200,cancelled.text
        assert cancelled.json()['status']=='stopped'
        end=client.post(path+'/end',json={identity_key(kind):original,'reason':'launch_cancelled','actual_seconds':0})
        assert end.status_code==200,end.text
        assert end.json()[identity_key(kind)]==original and end.json()['task_resumed'] is False
        assert end.json()['actual_seconds']==0
        assert client.post(lookup+'/native-not-dispatched',json=signed).status_code==200
    with client_for(tmp_path,model_gateway=NoCloud(),key='b'*64) as client:
        assert client.post(lookup+'/native-not-dispatched',json=signed).status_code==409
        current=client.get(lookup).json()
        assert client.post(lookup+'/native-not-dispatched',json=native_proof(current,'b'*64)).status_code==200


def test_start_intent_insert_failure_rolls_back_whole_start_then_original_key_recovers(tmp_path):
    from home_study_agent.controlled_launch_receipt import ensure_schema
    db=Database(tmp_path/'test.db')
    with client_for(tmp_path,model_gateway=NoCloud(),agent_runtime_enabled=True) as client:
        sid,pid,path,_=prepare(client,db,tmp_path,'content')
        with db.connect() as c:
            ensure_schema(c)
            c.execute("CREATE TRIGGER deny_launch BEFORE INSERT ON controlled_launch_receipt BEGIN SELECT RAISE(ABORT,'synthetic-launch-ledger-failure'); END")
            c.commit()
        body={'query':'图形练习','client_action_id':'rollback-original','native_intent_id':str(uuid4())}
        with pytest.raises(Exception,match='synthetic-launch-ledger-failure'):
            client.post(path+'/start',json=body)
        with db.connect() as c:
            assert c.execute("SELECT count(*) FROM controlled_content_session WHERE client_action_id='rollback-original'").fetchone()[0]==0
            c.execute('DROP TRIGGER deny_launch');c.commit()
        retry=client.post(path+'/start',json=body)
        assert retry.status_code==200,retry.text
        assert client.get(f"/v1/sessions/{sid}/controlled-launch-intents/{body['native_intent_id']}").json()['activityId']==retry.json()['content_session_id']


def test_native_cancel_before_delayed_http_start_fences_request_without_creating_activity(tmp_path):
    db=Database(tmp_path/'test.db');intent=str(uuid4())
    with client_for(tmp_path,model_gateway=NoCloud(),agent_runtime_enabled=True) as client:
        sid,_,path,_=prepare(client,db,tmp_path,'content')
        body={'query':'图形练习','client_action_id':'late-start-key','native_intent_id':intent}
        cancellation=client.post(f'/v1/sessions/{sid}/controlled-launch-intents/{intent}/cancel-unstarted',json={'kind':'video','request_id':'late-start-key'})
        assert cancellation.status_code==200,cancellation.text
        assert cancellation.json()['status']=='cancelled_before_start'
        assert cancellation.json()['activityId'] is None
        late=client.post(path+'/start',json=body)
        assert late.status_code==409,late.text
        no_marker=client.post(path+'/start',json={key:value for key,value in body.items() if key!='native_intent_id'})
        assert no_marker.status_code==409,no_marker.text
        with db.connect() as c:
            assert c.execute("SELECT count(*) FROM controlled_content_session WHERE client_action_id='late-start-key'").fetchone()[0]==0


def test_renderer_token_cannot_forge_no_dispatch_proof_or_clear_observed_duration(tmp_path):
    db=Database(tmp_path/'test.db');intent=str(uuid4())
    with client_for(tmp_path,model_gateway=NoCloud(),agent_runtime_enabled=True) as client:
        sid,_,path,_=prepare(client,db,tmp_path,'content')
        start=client.post(path+'/start',json={'query':'图形练习','client_action_id':'observed-playback','native_intent_id':intent})
        assert start.status_code==200,start.text
        original=start.json()['content_session_id']
        end=client.post(path+'/end',json={'content_session_id':original,'reason':'child_requested','actual_seconds':12})
        assert end.status_code==200,end.text
        with db.connect() as c:
            before=dict(c.execute('SELECT * FROM controlled_content_session WHERE content_session_id=?',(original,)).fetchone())
        forged=client.post(f'/v1/sessions/{sid}/controlled-launch-intents/{intent}/native-not-dispatched',json={
            'activity_id':original,'proof':'cancelled_before_dispatch'})
        assert forged.status_code==409,forged.text
        with db.connect() as c:
            after=dict(c.execute('SELECT * FROM controlled_content_session WHERE content_session_id=?',(original,)).fetchone())
        assert after==before
