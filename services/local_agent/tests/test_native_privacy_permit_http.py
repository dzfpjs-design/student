"""Actual local API, declared synthetic native issuer; not native-device evidence."""
import base64
import hashlib
import hmac
import json
from uuid import uuid4

from home_study_agent.database import Database
from home_study_agent.native_privacy import canonical, command_hash
from test_controlled_cloud_semantics_http import prepare
from test_controlled_launch_intent_http import client_for, native_proof, KEY
from test_controlled_start_atomic_http import CHILD
from test_learning_runtime_http import NoCloud
from test_privacy_operation_recovery_http import identity


def certificate(path, body, operation, kind='delete', child_id=CHILD, scope='media'):
    metadata={'nonce':str(uuid4()),'request_id':operation['request_id'],
              'expected_generation':operation['expected_generation'],'kind':kind,
              'child_id':child_id,'scope':scope,'command_sha256':command_hash(path,body),
              'cleanup_manifest_sha256':operation['cleanup_manifest_sha256']}
    signature=hmac.new(bytes.fromhex(KEY),canonical(['native_privacy_permit_v1',metadata]).encode(),hashlib.sha256).hexdigest()
    return {**metadata,'signature':signature}


def signed_header(proof):
    return {'X-HSC-Native-Privacy':base64.urlsafe_b64encode(json.dumps(proof).encode()).decode().rstrip('=')}


def test_delete_requires_settlement_single_use_proof_and_late_start_keeps_original_epoch(tmp_path):
    db=Database(tmp_path/'test.db')
    with client_for(tmp_path,model_gateway=NoCloud()) as client:
        sid,pid,path,headers=prepare(client,db,tmp_path,'content')
        original_epoch=client.post(f'/v1/sessions/{sid}/controlled-launch-epoch').json()['epoch']
        body={'query':'图形练习','client_action_id':'native-before-delete','native_intent_id':str(uuid4()),'native_epoch':original_epoch}
        started=client.post(path+'/start',json=body)
        assert started.status_code==200,started.text
        lookup=f"/v1/sessions/{sid}/controlled-launch-intents/{body['native_intent_id']}"
        operation=identity(client,headers)
        delete_path=f'/v1/parent/children/{CHILD}/delete'
        values={'scope':'media','confirmation':'确认删除','client_action_id':operation['request_id'],'operation':operation}
        proof=certificate(delete_path,values,operation)
        rejected=client.post(delete_path,headers={**headers,**signed_header(proof)},json=values)
        assert rejected.status_code==409,rejected.text
        assert 'RESOURCE_UNSETTLED' in rejected.text
        assert client.post(delete_path,headers=headers,json=values).status_code==409
        binding=client.get(lookup).json()
        # Fixture owns no native window; this explicitly simulates the main issuer.
        settled=client.post(lookup+'/native-not-dispatched',json=native_proof(binding))
        assert settled.status_code==200,settled.text
        with db.connect() as c:
            before=c.execute('SELECT count(*) FROM plan_item_event WHERE plan_item_id=?',(pid,)).fetchone()[0]
        deleted=client.post(delete_path,headers={**headers,**signed_header(proof)},json=values)
        assert deleted.status_code==200,deleted.text
        assert deleted.json()['status']=='completed'
        assert client.post(delete_path,headers={**headers,**signed_header(proof)},json=values).status_code==409
        read=client.post('/v1/parent/native-privacy-permits/read',headers=headers,json=proof)
        assert read.status_code==200 and read.json()['status']=='finished',read.text
        assert read.headers['cache-control']=='no-store'
        late={**body,'client_action_id':'held-old-http-request','native_intent_id':str(uuid4())}
        assert client.post(path+'/start',json=late).status_code==409
        assert client.post(path+'/start',json={k:v for k,v in late.items() if k not in {'native_intent_id','native_epoch'}}).status_code==409
        with db.connect() as c:
            assert c.execute("SELECT count(*) FROM controlled_content_session WHERE client_action_id='held-old-http-request'").fetchone()[0]==0
            assert c.execute('SELECT count(*) FROM plan_item_event WHERE plan_item_id=?',(pid,)).fetchone()[0]==before
        current=client.post(f'/v1/sessions/{sid}/controlled-launch-epoch').json()['epoch']
        assert current!=original_epoch
        fresh=client.post(path+'/start',json={**late,'client_action_id':'fresh-parent-preserved-launch','native_epoch':current})
        assert fresh.status_code==200,fresh.text


def test_cancel_never_arrived_destructive_post_is_durable_and_cannot_be_replayed(tmp_path):
    db=Database(tmp_path/'test.db')
    with client_for(tmp_path,model_gateway=NoCloud()) as client:
        _,_,_,headers=prepare(client,db,tmp_path,'content')
        operation=identity(client,headers)
        path=f'/v1/parent/children/{CHILD}/delete'
        body={'scope':'all_history','confirmation':'确认删除','client_action_id':operation['request_id'],'operation':operation}
        proof=certificate(path,body,operation,scope='all_history')
        unsigned=client.post('/v1/parent/native-privacy-permits/fence',headers=headers,json={**proof,'signature':'0'*64})
        assert unsigned.status_code==409
        assert client.post('/v1/parent/native-privacy-permits/read',json=proof).status_code in {401,403}
        missing=client.post('/v1/parent/native-privacy-permits/read',headers=headers,json=proof)
        assert missing.json()['status']=='missing'
        fenced=client.post('/v1/parent/native-privacy-permits/fence',headers=headers,json=proof)
        assert fenced.status_code==200 and fenced.json()['status']=='cancelled',fenced.text
        assert client.post(path,headers={**headers,**signed_header(proof)},json=body).status_code==409
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM deletion_job WHERE client_action_id=?',(operation['request_id'],)).fetchone()[0]==0
    with client_for(tmp_path,model_gateway=NoCloud()) as client:
        token=client.post('/v1/parent/unlock',json={'pin':'4826'}).json()['parent_token']
        headers={'X-HSC-Parent':token}
        receipt=client.get('/v1/parent/privacy-operations/'+operation['request_id'],headers=headers)
        assert receipt.json()['status']=='failed' and not receipt.json()['cleanup_allowed']
        assert client.post(path,headers={**headers,**signed_header(proof)},json=body).status_code==409
