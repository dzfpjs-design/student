"""Real restore/delete routes; synthetic issuer, not native closure evidence."""
import pytest
from uuid import uuid4
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.database import Database
from test_controlled_launch_intent_http import native_proof, KEY
from test_controlled_cloud_semantics_http import prepare
from test_controlled_start_atomic_http import CHILD
from test_learning_runtime_http import NoCloud
from test_privacy_operation_recovery_http import identity, auth
from test_native_privacy_permit_http import certificate, signed_header


def client_for(tmp_path,model_gateway):
    return TestClient(create_app(Settings(data_dir=tmp_path,database_path=tmp_path/'test.db',
        session_token='synthetic-api-token',native_launch_attestation_key=KEY,data_encryption_key=b'0'*32,
        agent_runtime_enabled=True,maintenance_enabled=False),model_gateway=model_gateway),headers={'X-HSC-Session':'synthetic-api-token'})


@pytest.mark.parametrize('portable',[False,True])
def test_restore_uses_live_permit_and_original_receipt_after_restart(tmp_path,portable):
    with client_for(tmp_path,model_gateway=NoCloud()) as client:
        headers=auth(client,True)
        if portable:
            created=client.post('/v1/parent/backups/portable',headers=headers,json={'passphrase':'Synthetic durable permission'})
            assert created.status_code==200,created.text
            values={'path':created.json()['path'],'passphrase':'Synthetic durable permission','confirmation':'确认迁移恢复'}
            path='/v1/parent/backups/portable/restore';kind='portable_restore'
        else:
            created=client.post('/v1/parent/backups',headers=headers)
            values={'backup_id':created.json()['backup_id'],'confirmation':'确认恢复'}
            path='/v1/parent/backups/restore';kind='restore'
        assert created.status_code==200,created.text
        operation=identity(client,headers);values['operation']=operation
        proof=certificate(path,values,operation,kind=kind,child_id=None,scope=None)
        assert client.post(path,headers=headers,json=values).status_code==409
        response=client.post(path,headers={**headers,**signed_header(proof)},json=values)
        assert response.status_code==200,response.text
        receipt=client.get('/v1/parent/privacy-operations/'+operation['request_id'],headers=headers)
        assert receipt.json()['status']=='committed',receipt.text
        assert receipt.json()['result_generation']!=operation['expected_generation']
    with client_for(tmp_path,model_gateway=NoCloud()) as client:
        headers=auth(client)
        permit=client.post('/v1/parent/native-privacy-permits/read',headers=headers,json=proof)
        assert permit.status_code==200 and permit.json()['status']=='finished',permit.text
        assert client.post(path,headers={**headers,**signed_header(proof)},json=values).status_code==409
        receipt=client.get('/v1/parent/privacy-operations/'+operation['request_id'],headers=headers)
        assert receipt.json()['status']=='committed'
        with Database(tmp_path/'test.db').connect() as c:
            rows=c.execute('SELECT metadata_json FROM native_privacy_permit').fetchall()
            assert len(rows)==1 and 'passphrase' not in rows[0][0]
            assert values.get('path','not-a-persisted-path') not in rows[0][0]


def test_all_history_transaction_failure_exact_retry_and_parent_scope_reads(tmp_path,monkeypatch):
    db=Database(tmp_path/'test.db')
    with client_for(tmp_path,model_gateway=NoCloud()) as client:
        sid,_,path,headers=prepare(client,db,tmp_path,'content')
        epoch=client.post(f'/v1/sessions/{sid}/controlled-launch-epoch').json()['epoch']
        start={'query':'图形练习','client_action_id':'durable-delete-start','native_intent_id':str(uuid4()),'native_epoch':epoch}
        response=client.post(path+'/start',json=start);assert response.status_code==200,response.text
        lookup=f"/v1/sessions/{sid}/controlled-launch-intents/{start['native_intent_id']}"
        binding=client.get(lookup).json();resource=f"/v1/parent/native-controlled-resources/video/{binding['activityId']}"
        assert client.get(resource).status_code in {401,403}
        view=client.get(resource,headers=headers)
        assert view.status_code==200 and view.headers['cache-control']=='no-store'
        assert set(view.json())=={'kind','activity_id','session_id','child_id','status'}
        other='child-primary-4' if CHILD!='child-primary-4' else 'child-junior-3'
        assert client.get('/v1/parent/native-controlled-resources?child_id='+other,headers=headers).json()['items']==[]
        assert client.post(lookup+'/native-not-dispatched',json=native_proof(binding)).status_code==200
        op=identity(client,headers);delete=f'/v1/parent/children/{CHILD}/delete'
        values={'scope':'all_history','confirmation':'确认删除','client_action_id':op['request_id'],'operation':op}
        proof=certificate(delete,values,op,scope='all_history')
        # Inject a real SQLite abort inside the borrowed destructive transaction.
        with db.connect() as c:
            c.execute("CREATE TRIGGER synthetic_delete_abort BEFORE DELETE ON daily_session BEGIN SELECT RAISE(ABORT,'synthetic rollback'); END")
            c.commit()
        failed=client.post(delete,headers={**headers,**signed_header(proof)},json=values)
        assert failed.status_code!=200,failed.text
        with db.connect() as c:
            assert c.execute('SELECT epoch FROM controlled_launch_epoch WHERE child_id=?',(CHILD,)).fetchone()[0]==epoch
            assert c.execute('SELECT 1 FROM controlled_launch_receipt WHERE intent_id=?',(binding['intentId'],)).fetchone()
            job=dict(c.execute('SELECT * FROM deletion_job WHERE client_action_id=?',(op['request_id'],)).fetchone())
            assert job['status']=='failed'
            c.execute('DROP TRIGGER synthetic_delete_abort');c.commit()
        retry=f"/v1/parent/deletions/{job['deletion_id']}/retry"
        context=client.get(retry.removesuffix('/retry')+'/native-context',headers=headers)
        assert context.status_code==200 and context.json()['operation']==op,context.text
        retry_body={'confirmation':'确认删除'}
        retry_proof=certificate(retry,retry_body,op,scope='all_history')
        success=client.post(retry,headers={**headers,**signed_header(retry_proof)},json=retry_body)
        assert success.status_code==200,success.text
        assert client.get(resource,headers=headers).status_code==404
        assert client.get(lookup).status_code==404
        assert client.post(path+'/start',json=start).status_code in {404,409}
        with db.connect() as c:
            assert c.execute('SELECT 1 FROM child_profile WHERE child_id=?',(other,)).fetchone()
    with client_for(tmp_path,model_gateway=NoCloud()) as client:
        token=client.post('/v1/parent/unlock',json={'pin':'4826'}).json()['parent_token']
        headers={'X-HSC-Parent':token}
        read=client.get('/v1/parent/privacy-operations/'+op['request_id'],headers=headers)
        assert read.json()['status']=='committed',read.text
        assert client.post(delete,headers={**headers,**signed_header(proof)},json=values).status_code==409
