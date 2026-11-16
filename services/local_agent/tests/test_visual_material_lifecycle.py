"""Managed crop lifecycle uses real app and existing deletion/restore jobs."""
from synthetic_native_restore_fixture import KEY, signed_local_restore_post
from dataclasses import replace
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from privacy_restore_operation import restore_operation
from test_visual_material_host import setup_case, headers, BASE, REGION


def store(client, prepared, args):
    h=headers(client)
    host=client.app.state.visual_materials
    host.stage_prepared('child','page','figure',revision_id=args['revision_id'],region_version=args['region_version'],prepared=prepared)
    result=client.post(REGION+'/storage',headers=h,json={**args,'client_action_id':'store','purpose':'local_visual_material_review_v1'})
    assert result.status_code==200,result.text
    return h,result.json()


def test_actual_artifact_delete_removes_derived_crop_and_scope_audit(tmp_path):
    db,settings,prepared,args,_=setup_case(tmp_path)
    with TestClient(create_app(settings),client=('127.0.0.1',42080)) as client:
        h,stored=store(client,prepared,args)
        with db.connect() as c:
            path=Path(c.execute('SELECT local_path FROM capture_artifact WHERE artifact_id=?',(stored['receipt']['asset_id'],)).fetchone()[0])
        response=client.request('DELETE','/v1/parent/capture-artifacts/page',headers=h,
            json={'confirmation':'确认删除','client_action_id':'delete-page'})
        assert response.status_code==200,response.text
        assert not path.exists()
        with db.connect() as c:
            assert c.execute("SELECT COUNT(*) FROM capture_artifact WHERE artifact_type='document_visual_region'").fetchone()[0]==0
            assert c.execute("SELECT COUNT(*) FROM audit_event WHERE action='visual_material_host_v1'").fetchone()[0]==0
        assert client.get(REGION,params=args,headers=h).status_code==404


@pytest.mark.parametrize('portable',[False,True])
def test_backup_restores_crop_but_quarantines_original_storage_and_display(tmp_path,portable):
    db,settings,prepared,args,region=setup_case(tmp_path)
    settings=replace(settings,native_launch_attestation_key=KEY if not portable else None)
    with TestClient(create_app(settings),client=('127.0.0.1',42080)) as client:
        h,stored=store(client,prepared,args)
        image=client.get(REGION+'/image',headers=h,params={**args,'sha256':region['safe_png_sha256']})
        assert image.status_code==200
        old_display=dict(**args,display_token=image.headers['X-HSC-Visual-Display-Token'],
            sha256=region['safe_png_sha256'],width=prepared.width,height=prepared.height,client_action_id='old-display')
        backup=client.post('/v1/parent/backups/portable' if portable else '/v1/parent/backups',headers=h,
            json={'passphrase':'synthetic-visual-backup-phrase'} if portable else None)
        assert backup.status_code==200,backup.text
        restore_body=({'path':backup.json()['path'],'passphrase':'synthetic-visual-backup-phrase'} if portable
            else {'backup_id':backup.json()['backup_id']})
        request={**restore_body,'confirmation':'确认迁移恢复' if portable else '确认恢复','operation':restore_operation(client,headers=h)}
        restored=(client.post('/v1/parent/backups/portable/restore',headers=h,json=request) if portable else
            signed_local_restore_post(client,headers=h,json=request))
        assert restored.status_code==200,restored.text
        h=headers(client)
        detail=client.get(REGION,params=args,headers=h)
        assert detail.status_code==200,detail.text
        assert detail.json()['storage_status']=='quarantined'
        assert detail.json()['image_availability']=='unavailable'
        replay=client.post(REGION+'/storage',headers=h,json={**args,'client_action_id':'store','purpose':'local_visual_material_review_v1'})
        assert replay.status_code==200,replay.text
        assert replay.json()['receipt']==stored['receipt']
        assert replay.json()['detail']['storage_status']=='quarantined'
        assert client.post(REGION+'/displayed',headers=h,json=old_display).status_code==409
        renewed=client.post(REGION+'/storage',headers=h,json={**args,'client_action_id':'new-consent','purpose':'local_visual_material_review_v1'})
        assert renewed.status_code==200,renewed.text
        assert renewed.json()['detail']['image_availability']=='available'
        with db.connect() as c:
            assert c.execute("SELECT COUNT(*) FROM capture_artifact WHERE artifact_type='document_visual_region'").fetchone()[0]==1


def test_failed_file_write_has_existing_deletion_receipt_no_residual(tmp_path,monkeypatch):
    db,settings,prepared,args,_=setup_case(tmp_path)
    with TestClient(create_app(settings),client=('127.0.0.1',42080)) as client:
        h=headers(client)
        host=client.app.state.visual_materials
        host.stage_prepared('child','page','figure',revision_id=args['revision_id'],region_version=args['region_version'],prepared=prepared)
        from home_study_agent import visual_material_host as module
        original=module.write_new_managed_media
        def fail_after_file(*a,**kw):
            original(*a,**kw)
            raise OSError('synthetic_after_write')
        monkeypatch.setattr(module,'write_new_managed_media',fail_after_file)
        with pytest.raises(OSError,match='synthetic_after_write'):
            host.storage('child','page','figure',**args,client_action_id='store',purpose='local_visual_material_review_v1')
        assert list((tmp_path/'media').glob('*.png.enc'))==[]
        with db.connect() as c:
            assert c.execute("SELECT COUNT(*) FROM capture_artifact WHERE artifact_type='document_visual_region'").fetchone()[0]==0
            assert c.execute("SELECT COUNT(*) FROM deletion_job WHERE status='completed'").fetchone()[0]==1


def test_actual_single_source_delete_erases_linked_visuals_not_original_or_other_source(tmp_path):
    from home_study_agent.knowledge_base import KnowledgeBaseService
    from home_study_agent.config import Settings
    from home_study_agent.document_vision import DiagramRegionGate
    from test_document_vision import RegionEngine
    from test_visual_material import visual_case,registered
    db,monitor,raw,scope=visual_case(tmp_path)
    kb=KnowledgeBaseService(db)
    source=kb.import_source('child',dict(source_type='school_material',title='Original isolated figure',subject='数学',
        grade_band='初三',source_ref='isolated:figure',license_status='family_owned_index',parent_reviewed=True,
        content_text='A synthetic rectangle used only by this local test.'))
    other=kb.import_source('child',dict(source_type='school_material',title='Unrelated isolated source',subject='数学',
        grade_band='初三',source_ref='isolated:other',license_status='family_owned_index',parent_reviewed=True,
        content_text='An unrelated source that must remain.'))
    with db.connect() as c:
        details=json.loads(c.execute("SELECT details_json FROM capture_artifact WHERE artifact_id='page'").fetchone()[0])
        details['source_id']=source['source_id']
        c.execute("UPDATE capture_artifact SET details_json=? WHERE artifact_id='page'",(json.dumps(details),))
        c.commit()
        names=tuple(r[0] for r in c.execute('SELECT nickname FROM child_profile'))
    region=registered(db,raw,scope)
    prepared=DiagramRegionGate(RegionEngine()).prepare_region(raw,region_xyxy=[200,180,600,560],known_child_names=names)
    args=dict(revision_id=scope['revision_id'],region_version=region['region_version'],facts_version=None)
    monitor.stop('study','parent-review')
    with db.connect() as c:
        c.execute("UPDATE daily_session SET status='completed',state='completed' WHERE session_id='study'")
        c.commit()
    settings=Settings(data_dir=tmp_path,database_path=db.path,session_token='local-test',data_encryption_key=b'v'*32,maintenance_enabled=False)
    with TestClient(create_app(settings),client=('127.0.0.1',42080)) as client:
        h,_=store(client,prepared,args)
        response=client.post('/v1/parent/children/child/knowledge-sources/'+source['source_id']+'/delete',headers=h,
            json={'confirmation':'确认删除','client_action_id':'source-delete'})
        assert response.status_code==200,response.text
        assert response.json()['status']=='completed'
        assert list((tmp_path/'media').glob('*.png.enc'))==[]
        with db.connect() as c:
            assert c.execute("SELECT extracted_text FROM capture_artifact WHERE artifact_id='page'").fetchone()[0]=='Observe the figure.'
            assert c.execute('SELECT 1 FROM knowledge_source WHERE source_id=?',(other['source_id'],)).fetchone()
            assert not c.execute('SELECT 1 FROM knowledge_source WHERE source_id=?',(source['source_id'],)).fetchone()
            assert c.execute("SELECT COUNT(*) FROM capture_artifact WHERE artifact_type='document_visual_region'").fetchone()[0]==0
        assert client.get(REGION+'/image',params={**args,'sha256':region['safe_png_sha256']},headers=h).status_code==409
