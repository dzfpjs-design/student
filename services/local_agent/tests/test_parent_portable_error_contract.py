"""Synthetic original Parent HTTP/authority failures, never native/Windows credit."""
import hashlib,json,os
from pathlib import Path
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.build_info import BUILD_COMPONENT, BUILD_VERSION
from home_study_agent.config import Settings
from home_study_agent import restore_publication as pub,parent_restore_parent as parent
from privacy_restore_operation import restore_operation
from test_learning_runtime_http import NoCloud

ROUTE='/v1/parent/backups/portable/restore'
PHRASE='public-parent-error-contract-passphrase'

def settings(root):
    return Settings(data_dir=root,database_path=root/'family.db',data_encryption_key=b'p'*32,
        native_launch_attestation_key=None,maintenance_enabled=False,agent_runtime_enabled=False)

def authenticate(c,setup=False):
    response=c.post('/v1/parent/'+('setup'if setup else'unlock'),json={'pin':'4826'})
    assert response.status_code==200,response.text
    return {'X-HSC-Parent':response.json()['parent_token']}

def test_parent_unexpected_unapplied_error_preserves_original_and_fresh_finalize(tmp_path,monkeypatch):
    root=tmp_path/'data';s=settings(root);seen=[];qualified=[]
    with TestClient(create_app(s,model_gateway=NoCloud()),raise_server_exceptions=False)as c:
        h=authenticate(c,True);db=c.app.state.privacy_lifecycle.database
        backup=c.post('/v1/parent/backups/portable',headers=h,json={'passphrase':PHRASE})
        assert backup.status_code==200,backup.text
        source=Path(backup.json()['path']);source_hash=hashlib.sha256(source.read_bytes()).hexdigest()
        with db.connect()as sql:
            sql.execute("UPDATE child_profile SET nickname='Synthetic current primary'WHERE child_id='child-primary-4'")
            sql.execute("UPDATE child_profile SET nickname='Synthetic current junior'WHERE child_id='child-junior-3'");sql.commit()
        operation=restore_operation(c,headers=h)
        body={'path':str(source),'passphrase':PHRASE,'confirmation':'确认迁移恢复','operation':operation}
        qualifier=getattr(parent,'original_unapplied_interruption',None)
        if qualifier is not None:
            def observe(database,owner,request_id):
                assert parent.current(database).owner is owner
                from home_study_agent.parent_restore_workflow import original_unapplied_interruption as native_qualifier
                assert native_qualifier(owner,request_id)is False
                assert qualifier(database,owner,'foreign-public-request')is False
                assert qualifier(database,owner,None)is False
                from home_study_agent.database import Database
                assert qualifier(Database(tmp_path/'foreign.db'),owner,request_id)is False
                value=qualifier(database,owner,request_id);qualified.append(value);return value
            monkeypatch.setattr(parent,'original_unapplied_interruption',observe)
        audit=c.app.state.privacy_lifecycle._audit
        def fail(sql,actor,action,*args,**kwargs):
            if action=='portable_backup_restored':seen.append(action);raise RuntimeError('public synthetic Parent audit fault')
            return audit(sql,actor,action,*args,**kwargs)
        monkeypatch.setattr(c.app.state.privacy_lifecycle,'_audit',fail)
        response=c.post(ROUTE,headers=h,json=body)
        assert seen==['portable_backup_restored']
        receipt=c.get('/v1/parent/privacy-operations/'+operation['request_id'],headers=h)
        recovery=c.get('/v1/parent/recovery-status',headers=h)
        assert receipt.status_code==200 and receipt.json()['status']=='unknown'and receipt.json()['cleanup_allowed']is False
        assert recovery.status_code==200 and recovery.json()['state']=='UNAPPLIED'
        health=c.get('/v1/health').json()
        assert health['status']=='recovery_only' and health['normal_business_ready'] is False
        assert (health['version'],health['build_version'],health['build_component'])==(BUILD_VERSION,BUILD_VERSION,BUILD_COMPONENT)
        with pub.publication_owner(db)as owner:
            from home_study_agent import parent_restore_ownership as ownership
            work=ownership.current(owner);assert work['state']=='ACTIVE'and work['request_id']==operation['request_id']
            assert pub._is_parent_binding(work['binding'])
            row,generation,authority,metadata=ownership._facts(owner,work)
            assert authority['state']=='ACTIVE'and metadata['authority_kind']=='PARENT_SESSION_RESTORE'
            assert row['state']=='processing'and row['origin_generation']==generation
            if qualifier is not None:assert qualifier(db,owner,operation['request_id'])is False
        assert hashlib.sha256(source.read_bytes()).hexdigest()==source_hash
        evidence={'HTTP':response.status_code,'content_type':response.headers.get('content-type'),
            'body':response.text[:1024],'cache_control':response.headers.get('cache-control'),
            'state':recovery.json()['state'],'receipt_status':receipt.json()['status'],
            'cleanup_allowed':receipt.json()['cleanup_allowed'],'authority_state':'ACTIVE',
            'work_authority_kind':'PARENT_SESSION_RESTORE','source_unchanged':True,'qualified':qualified}
    # Recovery must use original durable authority; lost source/passphrase is not
    # resubmitted and no new restore identity or native permit is manufactured.
    source.unlink()
    with TestClient(create_app(s,model_gateway=NoCloud()))as recovery_client:
        assert recovery_client.get('/v1/health').json()['status']=='recovery_only'
        h=authenticate(recovery_client)
        done=recovery_client.post('/v1/parent/privacy-operations/'+operation['request_id']+'/finalize',headers=h)
        assert done.status_code==200 and done.json()['status']=='failed',done.text
        repeat=recovery_client.post('/v1/parent/privacy-operations/'+operation['request_id']+'/finalize',headers=h)
        assert repeat.status_code==200 and repeat.json()==done.json(),repeat.text
    with TestClient(create_app(s,model_gateway=NoCloud()))as fresh:
        assert fresh.get('/v1/health').json()['status']=='ok'
        db=fresh.app.state.privacy_lifecycle.database
        with db.connect()as sql:
            assert sql.execute("SELECT nickname FROM child_profile WHERE child_id='child-primary-4'").fetchone()[0]=='Synthetic current primary'
            assert sql.execute("SELECT nickname FROM child_profile WHERE child_id='child-junior-3'").fetchone()[0]=='Synthetic current junior'
            assert not sql.execute('PRAGMA foreign_key_check').fetchall()
            assert sql.execute('SELECT state FROM restore_parent_authority WHERE request_id=?',(operation['request_id'],)).fetchone()[0]=='FAILED'
            assert not sql.execute("SELECT 1 FROM sqlite_master WHERE name='native_privacy_permit'").fetchone()or sql.execute('SELECT count(*)FROM native_privacy_permit').fetchone()[0]==0
    evidence['fresh_original_data_and_FK_preserved']=True
    output_dir=os.environ.get('HSC_FIXTURE_EVIDENCE_OUT')
    if output_dir is not None:
        out=Path(output_dir);(out/'parent-unexpected.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+'\n')
    assert response.status_code==500
    assert response.headers['content-type'].startswith('application/json'),response.text
    assert response.json()=={'code':'RESTORE_OPERATION_INTERRUPTED','message':'恢复过程被中断，请核对原操作，完成检查前不要重复恢复。',
        'retryable':False,'recovery_action':'review_original_operation'}
    assert response.headers['cache-control']=='no-store'
    assert qualified==[True]

def test_parent_confirmation_auth_and_body_errors_never_mint_authority(tmp_path,monkeypatch):
    with TestClient(create_app(settings(tmp_path/'data'),model_gateway=NoCloud()))as c:
        h=authenticate(c,True);db=c.app.state.privacy_lifecycle.database;calls=[]
        original=parent.original_parent_restore
        def observe(*args,**kwargs):calls.append(True);return original(*args,**kwargs)
        monkeypatch.setattr(parent,'original_parent_restore',observe)
        operation=restore_operation(c,headers=h)
        body={'path':str(tmp_path/'absent.hsc'),'passphrase':PHRASE,'confirmation':'wrong','operation':operation}
        denied=c.post(ROUTE,json=body);assert denied.status_code==401,denied.text
        wrong=c.post(ROUTE,headers=h,json=body)
        assert wrong.status_code==422,wrong.text
        assert wrong.json()=={'code':'RESTORE_CONFIRMATION_REQUIRED','message':'请输入“确认迁移恢复”。'}
        malformed=c.post(ROUTE,headers=h,json={'confirmation':'确认迁移恢复'})
        assert malformed.status_code==422,malformed.text
        assert calls==[]
        with db.connect()as sql:
            assert not sql.execute('SELECT 1 FROM privacy_operation_intent').fetchone()
            assert not sql.execute('SELECT 1 FROM restore_parent_authority').fetchone()
            assert not sql.execute("SELECT 1 FROM sqlite_master WHERE name='native_privacy_permit'").fetchone()or sql.execute('SELECT count(*)FROM native_privacy_permit').fetchone()[0]==0

def test_unqualified_parent_failure_and_handled_domain_error_keep_original_semantics(tmp_path,monkeypatch):
    from home_study_agent.privacy_lifecycle import RestoreConfirmationError
    with TestClient(create_app(settings(tmp_path/'data'),model_gateway=NoCloud()),raise_server_exceptions=False)as c:
        h=authenticate(c,True);db=c.app.state.privacy_lifecycle.database
        operation=restore_operation(c,headers=h)
        body={'path':str(tmp_path/'absent.hsc'),'passphrase':PHRASE,'confirmation':'确认迁移恢复','operation':operation}
        service=c.app.state.privacy_lifecycle
        def fail(*args,**kwargs):raise RuntimeError('public prework error')
        monkeypatch.setattr(service,'restore_portable_backup',fail)
        response=c.post(ROUTE,headers=h,json=body)
        assert response.status_code==500 and response.headers['content-type'].startswith('text/plain'),response.text
        def handled(*args,**kwargs):raise RestoreConfirmationError('确认迁移恢复')
        monkeypatch.setattr(service,'restore_portable_backup',handled)
        domain=c.post(ROUTE,headers=h,json=body)
        assert domain.status_code==422 and domain.json()=={'code':'RESTORE_CONFIRMATION_REQUIRED','message':'请输入“确认迁移恢复”。'},domain.text
        assert parent.current(db)is None
        with db.connect()as sql:
            assert not sql.execute('SELECT 1 FROM privacy_operation_intent').fetchone()
            assert not sql.execute('SELECT 1 FROM restore_parent_authority').fetchone()
