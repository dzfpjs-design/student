"""Real App/Parent/native restore, finite public PDF fixture, no cloud."""
from pathlib import Path
import hashlib,json,os,socket,sqlite3
import pytest
from home_study_agent.database import Database
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from home_study_agent import restore_publication as pub,parent_restore_ownership as ownership
from home_study_agent import restore_incomplete_protection as protection
from test_curriculum_supply import prepared
from test_manual_standard_scope import client,auth
from privacy_restore_operation import restore_operation
from synthetic_native_restore_request import KEY,signed_restore_post

@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(socket.socket,'connect',lambda *a,**k:(_ for _ in ()).throw(AssertionError('NETWORK_FORBIDDEN')))

def seed(tmp):
    db,_,_=prepared(tmp)
    root=tmp/'standards';pdf=root/'standard.pdf';pdf.write_bytes(b'%PDF-public-synthetic-inventory-only\n')
    digest=hashlib.sha256(pdf.read_bytes()).hexdigest()
    (root/'manifest.json').write_text(json.dumps({'format':'hsc_official_curriculum_sources_v1','sources':[{'source_key':'fixture','pdf_filename':'standard.pdf','pages_filename':None}]}))
    with db.connect()as c:
        c.execute("UPDATE curriculum_standard_source SET pdf_filename='standard.pdf',pdf_sha256=?,text_sha256=?WHERE source_id='std'",(digest,'b'*64));c.commit()
    life=PrivacyLifecycleService(db,tmp,data_encryption_key=b's'*32);life.initialize()
    return db,life,pdf

def request(c,backup):
    return signed_restore_post(c,headers={},json={'backup_id':backup['backup_id'],'confirmation':'确认恢复','operation':restore_operation(c)})

def abort(db):
    with pub.publication_owner(db)as owner:
        current=ownership.current(owner)
        assert current is not None
        with pub._cold(db.path,owner=owner)as c:metadata=pub._binding_facts(c,current['binding'])[3]
        result=pub.abort_unapplied_publication(owner,metadata)
        assert result['state']=='ABORTED_NOT_APPLIED'

def test_healthy_current_retains_original_full_undo_and_no_internal_roles(tmp_path):
    db,life,pdf=seed(tmp_path)
    with client(tmp_path,db,data_encryption_key=b's'*32,native_launch_attestation_key=KEY)as c:
        auth(c);backup=life.create_backup(prune=False)
        result=request(c,backup);assert result.status_code==200,result.text
        assert type(result.json()['recovery_point_backup_id'])is str
        assert 'restore_protection'not in result.json()
        with db.connect()as sql:
            assert sql.execute("SELECT count(*)FROM backup_manifest WHERE reason='restore_recovery_point'AND integrity_status='ok'").fetchone()[0]==1
        assert not list(tmp_path.glob('.restore-work-*/rollback-current.*'))

def test_missing_pdf_internal_only_null_public_id_and_durable_audit(tmp_path):
    db,life,pdf=seed(tmp_path)
    with client(tmp_path,db,data_encryption_key=b's'*32,native_launch_attestation_key=KEY)as c:
        auth(c);backup=life.create_backup(prune=False);pdf.unlink()
        result=request(c,backup);assert result.status_code==200,result.text
        assert result.json()['recovery_point_backup_id']is None
        assert result.json()['restore_protection']=='internal_database_only'
        assert not pdf.exists()  # No repair/rebinding of old external file.
        with db.connect()as sql:
            assert sql.execute("SELECT count(*)FROM backup_manifest WHERE reason='restore_recovery_point'").fetchone()[0]==0
            rows=sql.execute("SELECT summary_json FROM audit_event WHERE action='restore_database_protection'").fetchall()
            assert len(rows)==1 and json.loads(rows[0][0])['full_undo_available']is False

@pytest.mark.parametrize('bad',['alias','unmatched'])
def test_absence_never_authorizes_alias_changed_record_or_unknown_io(tmp_path,monkeypatch,bad):
    db,life,pdf=seed(tmp_path)
    with client(tmp_path,db,data_encryption_key=b's'*32,native_launch_attestation_key=KEY)as c:
        auth(c);backup=life.create_backup(prune=False);pdf.unlink()
        if bad=='alias':pdf.symlink_to('missing-target.pdf')
        if bad=='unmatched':
            with db.connect()as sql:sql.execute("UPDATE curriculum_standard_source SET text_sha256=?WHERE source_id='std'",('c'*64,));sql.commit()
        result=request(c,backup)
        assert result.status_code in{409,422},result.text
        assert 'synthetic-private-error'not in result.text
        # Original request remains unapplied, never a fake full recovery point.
        with pub.publication_owner(db)as owner:
            with pub._cold(db.path,owner=owner)as sql:
                assert sql.execute("SELECT count(*)FROM audit_event WHERE action='backup_restored'").fetchone()[0]==0
        if bad=='alias':pdf.unlink()
        abort(db)

@pytest.mark.parametrize('fault',['after_incomplete_current_protection','before_prepare_journal'])
def test_durable_preimage_then_real_unapplied_rollback_keeps_current_db(tmp_path,monkeypatch,fault):
    db,life,pdf=seed(tmp_path);seen=[]
    with client(tmp_path,db,data_encryption_key=b's'*32,native_launch_attestation_key=KEY)as c:
        auth(c);backup=life.create_backup(prune=False);pdf.unlink()
        with db.connect()as sql:sql.execute("UPDATE curriculum_standard_source SET title='CURRENT-PREIMAGE'WHERE source_id='std'");sql.commit()
        def stop(point):
            if point!=fault:return
            if fault=='before_prepare_journal':
                assert pub._gate(db.path).sql_connections==[]  # Witness closed before original cold publisher.
            paths=list(tmp_path.glob('.restore-work-*/rollback-current.json'));assert len(paths)==1
            metadata=json.loads(paths[0].read_bytes());image=paths[0].with_suffix('.db')
            assert hashlib.sha256(image.read_bytes()).hexdigest()==metadata['database']['sha256']
            assert metadata['kind']=='internal_database_only'and metadata['missing_pdf_count']==1
            with pub._sql(image,owner=pub._gate(db.path).owner,readonly=True)as sql:
                assert sql.execute("SELECT title FROM curriculum_standard_source WHERE source_id='std'").fetchone()[0]=='CURRENT-PREIMAGE'
                assert sql.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
            seen.append(True);raise RuntimeError('FIXED_FAILURE_BEFORE_PUBLICATION')
        monkeypatch.setattr(pub,'_fault',stop)
        if fault=='after_incomplete_current_protection':
            response=request(c,backup)
            assert response.status_code==500,response.text
            assert response.headers['content-type'].startswith('application/json'),response.text
            assert response.json()=={'code':'RESTORE_OPERATION_INTERRUPTED',
                'message':'恢复过程被中断，请核对原操作，完成检查前不要重复恢复。',
                'retryable':False,'recovery_action':'review_original_operation'}
            assert response.headers['cache-control']=='no-store'
        else:
            response=request(c,backup)
            assert response.status_code==409 and seen==[True],(response.text,seen)
        monkeypatch.setattr(pub,'_fault',lambda point:None)
        assert seen==[True]
        abort(db)
        with db.connect()as sql:
            assert sql.execute("SELECT title FROM curriculum_standard_source WHERE source_id='std'").fetchone()[0]=='CURRENT-PREIMAGE'
            assert sql.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
            assert not sql.execute('PRAGMA foreign_key_check').fetchall()
        assert not pdf.exists()and not list(tmp_path.glob('.restore-work-*'))
        retried=request(c,backup)
        assert retried.status_code==200,retried.text
        assert retried.json()['recovery_point_backup_id']is None
        assert retried.json()['restore_protection']=='internal_database_only'
        with db.connect()as sql:
            assert sql.execute("SELECT title FROM curriculum_standard_source WHERE source_id='std'").fetchone()[0]!='CURRENT-PREIMAGE'
            assert sql.execute('PRAGMA integrity_check').fetchone()[0]=='ok'

def test_foreign_commit_after_snapshot_is_rejected_without_publishing(tmp_path,monkeypatch):
    db,life,pdf=seed(tmp_path);changed=[]
    with client(tmp_path,db,data_encryption_key=b's'*32,native_launch_attestation_key=KEY)as c:
        auth(c);backup=life.create_backup(prune=False);pdf.unlink()
        def mutate(point):
            if point=='after_incomplete_current_protection':
                with pub._sql(db.path,owner=pub._gate(db.path).owner)as sql:
                    sql.execute("UPDATE curriculum_standard_source SET title='FOREIGN-COMMIT'WHERE source_id='std'");sql.commit()
                changed.append(True)
        monkeypatch.setattr(pub,'_fault',mutate)
        result=request(c,backup)
        assert result.status_code==409,result.text
        assert changed==[True]
        monkeypatch.setattr(pub,'_fault',lambda point:None);abort(db)
        with db.connect()as sql:assert sql.execute("SELECT title FROM curriculum_standard_source WHERE source_id='std'").fetchone()[0]=='FOREIGN-COMMIT'

def test_original_sql_close_unknown_retains_same_connection_and_never_recloses(tmp_path):
    db=Database(tmp_path/'original-close.db');db.initialize();seen=[]
    class UnknownClose(sqlite3.Connection):
        def close(self):
            seen.append(self)
            raise OSError('synthetic-close-ACK-unknown')
    instance=object.__new__(protection.Protection)
    with pytest.raises(pub.RestorePublicationError,match='CLOSE_UNCONFIRMED'):
        with pub.publication_owner(db)as owner:
            instance.owner=owner;instance.close_attempted=False
            instance.cm=pub._sql(db.path,owner=owner,readonly=True,factory=UnknownClose)
            instance.witness=instance.cm.__enter__()
            original=instance.witness
            assert original.execute('SELECT 1').fetchone()[0]==1
            try:instance.close()
            finally:
                instance.failed()  # No second call even when finally runs.
                assert seen==[original]and instance.witness is original
                assert owner.gate.poisoned and owner.gate.sql_connections==[original]
                assert any(record[0]=='original_sql_connection'and record[1]is original for record in owner.gate.retained)
    assert seen==[original]and pub._gate(db.path).poisoned
