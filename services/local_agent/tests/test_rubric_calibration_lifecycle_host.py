"""Real app privacy endpoints with unqualified protocols, never model traffic."""
from synthetic_native_restore_fixture import KEY, signed_local_restore_post
from datetime import datetime, timezone
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest
from privacy_restore_operation import restore_operation

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.rubric_calibration import RubricCalibrationService, digest
from test_companion_activity_host import NoCloud
from test_material_source_deletion_host import material_family, headers, settings_for, delete
from test_rubric_calibration import scorer, scope

CHILD = 'child-primary-4'
OTHER = 'child-junior-3'


@pytest.fixture
def home(tmp_path, request):
    settings = Settings(data_dir=tmp_path,database_path=tmp_path/'home.db',maintenance_enabled=False,
                        session_token='calibration-local-test',data_encryption_key=b'K'*32,native_launch_attestation_key=getattr(request,'param',None))
    with TestClient(create_app(settings,model_gateway=NoCloud()),client=('127.0.0.1',42010),raise_server_exceptions=False) as client:
        h=headers(client,settings)
        service=client.app.state.rubric_calibration
        db=service.database
        receipts={}
        for child in (CHILD,OTHER):
            with db.connect() as c:
                grade=c.execute('SELECT grade FROM child_profile WHERE child_id=?',(child,)).fetchone()[0]
            receipts[child]=service.freeze(child,scorer=scorer(),scope={**scope(),'grade':grade},
                holdout_receipt_id='missing-real-holdout',client_action_id='freeze-'+child)
            assert receipts[child]['current']['status']=='insufficient'
        yield db,service,settings,client,h,receipts


@pytest.mark.parametrize('scope_name',['learning','knowledge','media','all_history'])
def test_actual_parent_export_preview_delete_and_other_child_survive(home,scope_name):
    db,service,settings,client,h,receipts=home
    prefix=f'/v1/parent/children/{CHILD}'
    assert client.get(prefix+'/deletion-preview',headers={'X-HSC-Session':settings.session_token}).status_code==401
    preview=client.get(prefix+'/deletion-preview',headers=h)
    assert preview.status_code==200,preview.text
    assert preview.json()['rubric_calibrations']==2
    exported=client.post(prefix+'/export',headers=h)
    assert exported.status_code==200,exported.text
    path=Path(exported.json()['path'])
    content=json.loads(path.read_text())
    assert len(content['rubric_calibration_protocols'])==1
    assert len(content['rubric_calibration_events'])==1
    assert content['rubric_calibration_protocols'][0]['child_id']==CHILD
    other_export=client.post(f'/v1/parent/children/{OTHER}/export',headers=h)
    other_path=Path(other_export.json()['path'])
    other_bytes=other_path.read_bytes()
    saved_copy=settings.data_dir/'user-saved-copy.json'
    saved_copy.write_bytes(path.read_bytes())
    unregistered=path.parent/('child-export-'+CHILD+'-20000101T000000Z.json')
    unregistered.write_bytes(path.read_bytes())
    body={'scope':scope_name,'confirmation':'确认删除','client_action_id':'delete-'+scope_name}
    result=client.post(prefix+'/delete',headers=h,json=body)
    assert result.status_code==200 and result.json()['status']=='completed',result.text
    assert client.get(prefix+'/deletion-preview',headers=h).json()['rubric_calibrations']==0
    assert not path.exists(), 'Managed exported calibration authority must not survive scope deletion'
    assert other_path.read_bytes()==other_bytes
    assert saved_copy.exists() and unregistered.exists()
    with db.connect() as c:
        verification=json.loads(c.execute('SELECT verification_json FROM deletion_job WHERE deletion_id=?',
                                           (result.json()['deletion_id'],)).fetchone()[0])
        assert verification['remaining_counts'].get('rubric_calibrations',0)==0
        assert c.execute('SELECT COUNT(*) FROM rubric_calibration_protocol WHERE child_id=?',(CHILD,)).fetchone()[0]==0
        assert c.execute('SELECT COUNT(*) FROM rubric_calibration_event WHERE child_id=?',(CHILD,)).fetchone()[0]==0
        assert c.execute('SELECT COUNT(*) FROM rubric_calibration_protocol WHERE child_id=?',(OTHER,)).fetchone()[0]==1
        assert not c.execute('PRAGMA foreign_key_check').fetchall()
    reopened=RubricCalibrationService(Database(db.path))
    assert reopened.read(OTHER,receipts[OTHER]['current']['protocol_id'])['status']=='insufficient'
    retry=client.post(prefix+'/delete',headers=h,json=body)
    assert retry.status_code==200 and retry.json()['deletion_id']==result.json()['deletion_id']


@pytest.mark.parametrize('home,kind',[(KEY,'local'),(None,'portable')],indirect=['home'],ids=['local','portable'])
def test_actual_restore_quarantines_both_children_without_rewriting_protocols(home,kind):
    db,service,_,client,h,receipts=home
    with db.connect() as c:
        original=[dict(r) for r in c.execute('SELECT * FROM rubric_calibration_protocol ORDER BY protocol_id')]
    if kind=='local':
        backup=client.post('/v1/parent/backups',headers=h)
        assert backup.status_code==200,backup.text
        response=signed_local_restore_post(client,headers=h,json={
            'backup_id':backup.json()['backup_id'],'confirmation':'确认恢复',
            'operation':restore_operation(client,headers=h)})
    else:
        password='calibration isolated portable test'
        backup=client.post('/v1/parent/backups/portable',headers=h,json={'passphrase':password})
        assert backup.status_code==200,backup.text
        response=client.post('/v1/parent/backups/portable/restore',headers=h,json={'path':backup.json()['path'],
                             'passphrase':password,'confirmation':'确认迁移恢复',
                             'operation':restore_operation(client,headers=h)})
    assert response.status_code==200 and response.json()['status']=='restored',response.text
    with db.connect() as c:
        assert [dict(r) for r in c.execute('SELECT * FROM rubric_calibration_protocol ORDER BY protocol_id')]==original
        assert c.execute("SELECT COUNT(*) FROM rubric_calibration_event WHERE operation='restore_quarantine'").fetchone()[0]==2
    for child in (CHILD,OTHER):
        current=RubricCalibrationService(Database(db.path)).read(child,receipts[child]['current']['protocol_id'])
        assert current['status']=='quarantined' and not current['score_qualified']


def bind_material_protocol(f):
    """Only exact original/source fixture proof; no scorer or reviewer authority."""
    from test_open_response_materials import CHILD as material_child
    db=f['db']
    service=RubricCalibrationService(db)
    service.initialize()
    with db.connect() as c:
        grade=c.execute('SELECT grade FROM child_profile WHERE child_id=?',(material_child,)).fetchone()[0]
    frozen_scope={**scope(),'grade':grade,'subject':'物理','task_family':'scientific_explanation'}
    receipt=dict(receipt_id='local-fixture-holdout',child_id=material_child,status='current',corpus_kind='observed_original',
        blind_protocol_version='blind_labels_v1',provenance_receipt_hash='a'*64,scope_hash=digest(frozen_scope),
        frozen_at=datetime.now(timezone.utc).isoformat(),development_group_hashes=[],
        cases=[dict(evidence_id=f['attempt']['evidence_id'],group_hash=digest(f['item']),stratum='ordinary',
                    source_ids=[f['binding']['source_id']])])
    def exact_source(c,*,child_id,kind,reference_id):
        if child_id==material_child and kind=='holdout' and reference_id==receipt['receipt_id']:
            return receipt
        return None
    service=RubricCalibrationService(db,authority_resolver=exact_source)
    result=service.freeze(material_child,scorer={**scorer(),'modality':'original_written_text'},scope=frozen_scope,
                          holdout_receipt_id=receipt['receipt_id'],client_action_id='source-protocol')
    assert not result['current']['score_qualified']
    return material_child,service,result['current']['protocol_id']


def test_single_material_source_hook_is_in_same_purge_transaction_and_retryable(material_family):
    f=material_family
    settings=settings_for(f)
    with TestClient(create_app(settings,model_gateway=NoCloud()),client=('127.0.0.1',42012),raise_server_exceptions=False) as client:
        h=headers(client,settings)
        child,service,pid=bind_material_protocol(f)
        with f['db'].connect() as c:
            c.execute("""CREATE TRIGGER fail_calibration_invalidation BEFORE INSERT ON rubric_calibration_event
                WHEN NEW.operation='source_invalidated' BEGIN SELECT RAISE(ABORT,'test rollback'); END""")
            c.commit()
        failed=delete(client,h,f)
        assert failed.status_code==500,failed.text
        with f['db'].connect() as c:
            assert c.execute('SELECT 1 FROM knowledge_source WHERE source_id=?',(f['binding']['source_id'],)).fetchone()
            assert not c.execute("SELECT 1 FROM rubric_calibration_event WHERE operation='source_invalidated'").fetchone()
            assert c.execute("SELECT status FROM deletion_job WHERE client_action_id='delete-only-material'").fetchone()[0]=='failed'
            c.execute('DROP TRIGGER fail_calibration_invalidation')
            c.commit()
        result=delete(client,h,f)
        assert result.status_code==200 and result.json()['status']=='completed',result.text
        assert RubricCalibrationService(Database(f['db'].path)).read(child,pid)['status']=='revoked'
        with f['db'].connect() as c:
            assert c.execute('SELECT 1 FROM knowledge_source WHERE source_id=?',(f['question_source'],)).fetchone()
            assert c.execute('SELECT 1 FROM diagnostic_attempt WHERE attempt_id=?',(f['attempt']['attempt_id'],)).fetchone()
            assert c.execute("SELECT COUNT(*) FROM rubric_calibration_event WHERE operation='source_invalidated'").fetchone()[0]==1
            assert not c.execute('PRAGMA foreign_key_check').fetchall()


@pytest.mark.parametrize('fault',['symlink_to_media','changed_bytes'])
def test_managed_export_swap_after_commit_preserves_target_and_original_job_retry(home,monkeypatch,fault):
    db,_,settings,client,h,_=home
    prefix=f'/v1/parent/children/{CHILD}'
    response=client.post(prefix+'/export',headers=h)
    assert response.status_code==200,response.text
    path=Path(response.json()['path'])
    original=path.read_bytes()
    target=settings.data_dir/'media'/'user-file-must-remain.bin'
    target.parent.mkdir(exist_ok=True)
    target.write_bytes(b'user-owned synthetic bytes')
    privacy=client.app.state.privacy_lifecycle
    finish=privacy._finish_deletion_job
    replaced=[]
    def swap_once(deletion_id):
        if not replaced:
            replaced.append(True)
            if fault=='symlink_to_media':
                path.unlink()
                path.symlink_to(target)
            else:
                path.write_bytes(b'changed after original export authorization')
        return finish(deletion_id)
    monkeypatch.setattr(privacy,'_finish_deletion_job',swap_once)
    body=dict(scope='learning',confirmation='确认删除',client_action_id='swap-delete')
    failed=client.post(prefix+'/delete',headers=h,json=body)
    assert failed.status_code==500,failed.text
    assert target.read_bytes()==b'user-owned synthetic bytes'
    with db.connect() as c:
        job=dict(c.execute("SELECT * FROM deletion_job WHERE client_action_id='swap-delete'").fetchone())
        assert job['status']=='failed' and json.loads(job['residual_paths_json'])==[str(path)]
    path.unlink()
    path.write_bytes(original)
    retried=client.post(prefix+'/delete',headers=h,json=body)
    assert retried.status_code==200 and retried.json()['deletion_id']==job['deletion_id'],retried.text
    assert not path.exists() and target.read_bytes()==b'user-owned synthetic bytes'


def test_concurrent_export_cannot_publish_pre_deletion_calibration_after_cleanup(home,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor, wait
    from threading import Event
    _,_,_,client,h,_=home
    prefix=f'/v1/parent/children/{CHILD}'
    reached,release=Event(),Event()
    write=Path.write_text
    def paused_export(path,*args,**kwargs):
        if path.name.startswith('child-export-'+CHILD+'-') and not reached.is_set():
            reached.set()
            assert release.wait(10), 'Timed out waiting for isolated export barrier'
        return write(path,*args,**kwargs)
    monkeypatch.setattr(Path,'write_text',paused_export)
    with TestClient(client.app,client=('127.0.0.1',42013),raise_server_exceptions=False) as second:
        with ThreadPoolExecutor(max_workers=2) as pool:
            exporting=pool.submit(client.post,prefix+'/export',headers=h)
            assert reached.wait(5)
            deleting=pool.submit(second.post,prefix+'/delete',headers=h,
                json=dict(scope='learning',confirmation='确认删除',client_action_id='concurrent-export-delete'))
            try:
                done,_=wait([deleting],timeout=.5)
                deleted_before_export_finished=bool(done)
            finally:
                release.set()
            exported=exporting.result(timeout=10)
            deleted=deleting.result(timeout=10)
    assert exported.status_code==200,exported.text
    assert deleted.status_code==200 and deleted.json()['status']=='completed',deleted.text
    assert not deleted_before_export_finished, 'Deletion must share the export publication guard'
    assert not Path(exported.json()['path']).exists(), 'Pre-deletion authority leaked through a late export'
