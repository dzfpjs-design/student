"""A real synthetic native-issued restore; unexpected failure must stay actionable."""
import hashlib,json,os
from pathlib import Path
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.database import Database
from privacy_restore_operation import restore_operation
from synthetic_native_restore_fixture import KEY
from test_learning_runtime_http import NoCloud
from test_native_privacy_permit_http import certificate,signed_header
from home_study_agent import restore_publication as publication
from home_study_agent.parent_restore_workflow import original_unapplied_interruption

def test_unexpected_prepublication_audit_failure_keeps_original_and_explains_recovery(tmp_path,monkeypatch):
    settings=Settings(data_dir=tmp_path,database_path=tmp_path/'family.db',data_encryption_key=b'0'*32,
        maintenance_enabled=False,agent_runtime_enabled=False,native_launch_attestation_key=KEY)
    db=Database(settings.database_path)
    with TestClient(create_app(settings,model_gateway=NoCloud()),raise_server_exceptions=False)as client:
        token=client.post('/v1/parent/setup',json={'pin':'4826'}).json()['parent_token']
        headers={'X-HSC-Parent':token}
        backup=client.post('/v1/parent/backups',headers=headers);assert backup.status_code==200,backup.text
        source_hash=hashlib.sha256(Path(backup.json()['path']).read_bytes()).hexdigest()
        with db.connect()as c:
            c.execute("UPDATE child_profile SET nickname='Synthetic current original'WHERE child_id='child-primary-4'");c.commit()
        operation=restore_operation(client,headers=headers)
        with publication.publication_owner(db)as owner:
            assert original_unapplied_interruption(owner,operation['request_id'])is False
        body={'backup_id':backup.json()['backup_id'],'confirmation':'确认恢复','operation':operation}
        proof=certificate('/v1/parent/backups/restore',body,operation,kind='restore',child_id=None,scope=None)
        audit=client.app.state.privacy_lifecycle._audit
        calls=[]
        def fail_audit(c,actor,action,*args,**kwargs):
            if action=='backup_restored':calls.append(action);raise RuntimeError('public synthetic audit failure')
            return audit(c,actor,action,*args,**kwargs)
        monkeypatch.setattr(client.app.state.privacy_lifecycle,'_audit',fail_audit)
        response=client.post('/v1/parent/backups/restore',headers={**headers,**signed_header(proof)},json=body)
        assert calls==['backup_restored']
        with publication.publication_owner(db)as owner:
            assert original_unapplied_interruption(owner,operation['request_id'])is True
            assert original_unapplied_interruption(owner,'foreign-public-synthetic-request')is False
            assert original_unapplied_interruption(owner,None)is False
        foreign=Database(tmp_path/'foreign.db');foreign.initialize()
        with publication.publication_owner(foreign)as owner:
            assert original_unapplied_interruption(owner,operation['request_id'])is False
        health=client.get('/v1/health');receipt=client.get('/v1/parent/privacy-operations/'+operation['request_id'],headers=headers)
        state=client.get('/v1/parent/recovery-status',headers=headers)
        fence=client.post('/v1/parent/native-privacy-permits/fence',headers=headers,json=proof)
        evidence={'HTTP':response.status_code,'content_type':response.headers.get('content-type'),'cache_control':response.headers.get('cache-control'),
            'body':response.text[:1024],'health_HTTP':health.status_code,'health':health.json(),'receipt_HTTP':receipt.status_code,'receipt':receipt.json(),
            'recovery_HTTP':state.status_code,'recovery':state.json(),'original_native_fence_HTTP':fence.status_code,'original_native_fence':fence.json(),
            'source_hash_unchanged':hashlib.sha256(Path(backup.json()['path']).read_bytes()).hexdigest()==source_hash}
        output_dir=os.environ.get('HSC_FIXTURE_EVIDENCE_OUT')
        if output_dir is not None:
            out=Path(output_dir);(out/'actual-unexpected-restore-failure.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+'\n')
        assert health.status_code==200 and health.json()['status']=='recovery_only'
        assert receipt.status_code==200 and receipt.json()['cleanup_allowed']is False
        assert state.status_code==200 and state.json()['state']=='UNAPPLIED'
        assert fence.status_code==200 and fence.json()['status']=='failed',fence.text
        with publication.publication_owner(db)as owner:
            assert original_unapplied_interruption(owner,operation['request_id'])is False
        assert evidence['source_hash_unchanged']
    with TestClient(create_app(settings,model_gateway=NoCloud()))as fresh:
        assert fresh.get('/v1/health').json()['status']=='ok'
        with db.connect()as c:
            assert c.execute("SELECT nickname FROM child_profile WHERE child_id='child-primary-4'").fetchone()[0]=='Synthetic current original'
            assert not c.execute('PRAGMA foreign_key_check').fetchall()
    assert response.status_code==500
    assert response.headers['content-type'].startswith('application/json'),response.text
    assert response.json()['code']=='RESTORE_OPERATION_INTERRUPTED'
    assert response.json()['recovery_action']=='review_original_operation'
    assert response.json()['retryable']is False
    assert response.headers['cache-control']=='no-store'
