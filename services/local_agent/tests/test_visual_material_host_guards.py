"""Exact bytes/grants/source gates, independent from frontend display fixtures."""
import json
from pathlib import Path
import sqlite3

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.local_supply import LocalSupplyError
from home_study_agent.visual_material import resolve_visual_material
from test_visual_material_host import setup_case,headers,REGION,BASE
from test_visual_material_lifecycle import store


def displayed_and_confirm(client,h,args,prepared,region):
    image=client.get(REGION+'/image',headers=h,params={**args,'sha256':region['safe_png_sha256']})
    assert image.status_code==200,image.text
    displayed=client.post(REGION+'/displayed',headers=h,json={**args,'client_action_id':'display',
        'display_token':image.headers['X-HSC-Visual-Display-Token'],'sha256':region['safe_png_sha256'],
        'width':prepared.width,'height':prepared.height})
    assert displayed.status_code==200,displayed.text
    body={**args,'client_action_id':'confirm','display_receipt_id':displayed.json()['display_receipt_id']}
    result=client.post(REGION+'/confirm',headers=h,json=body)
    assert result.status_code==200,result.text
    return result.json(),body


@pytest.mark.parametrize('fault',['missing','tampered','regrant'])
def test_shared_visual_resolver_rechecks_durable_asset_and_original_grant(tmp_path,fault):
    db,settings,prepared,args,region=setup_case(tmp_path)
    with TestClient(create_app(settings),client=('127.0.0.1',42080)) as client:
        h,stored=store(client,prepared,args)
        accepted,body=displayed_and_confirm(client,h,args,prepared,region)
        bound=accepted['receipt']['binding']
        runtime_db=client.app.state.visual_materials.database
        with runtime_db.connect() as c:
            assert resolve_visual_material(c,child_id='child',binding=bound)['status']=='ready_local'
            asset=c.execute('SELECT local_path FROM capture_artifact WHERE artifact_id=?',(stored['receipt']['asset_id'],)).fetchone()[0]
        if fault=='missing':
            Path(asset).unlink()
        elif fault=='tampered':
            with Path(asset).open('r+b') as f:
                f.seek(20)
                f.write(b'bad')
        else:
            renewed=client.post(REGION+'/storage',headers=h,json={**args,'client_action_id':'new-store',
                'purpose':'local_visual_material_review_v1'})
            assert renewed.status_code==200,renewed.text
        with runtime_db.connect() as c,pytest.raises(LocalSupplyError,match='VISUAL_MATERIAL_'):
            resolve_visual_material(c,child_id='child',binding=bound)
        replay=client.post(REGION+'/confirm',headers=h,json=body)
        assert replay.status_code==200,replay.text
        assert replay.json()['receipt']==accepted['receipt']


def test_prior_confirm_receipt_is_not_reauthorized_when_source_changes(tmp_path):
    db,settings,prepared,args,region=setup_case(tmp_path)
    with TestClient(create_app(settings),client=('127.0.0.1',42080)) as client:
        h,_=store(client,prepared,args)
        accepted,body=displayed_and_confirm(client,h,args,prepared,region)
        with db.connect() as c:
            c.execute("UPDATE capture_artifact SET extracted_text='Changed page' WHERE artifact_id='page'")
            c.commit()
        replay=client.post(REGION+'/confirm',headers=h,json=body)
        assert replay.status_code==200,replay.text
        assert replay.json()['receipt']==accepted['receipt']
        assert replay.json()['detail']['source_current'] is False
        assert replay.json()['detail']['observations']==[]
        assert replay.json()['detail']['image_availability']=='unavailable'
        assert client.post(REGION+'/confirm',headers=h,json={**body,'client_action_id':'new-confirm'}).status_code==409
        assert client.get(REGION+'/image',headers=h,params={**args,'sha256':region['safe_png_sha256']}).status_code==409
        assert client.get(REGION+'/commands/confirm',headers=h).json()['receipt']==accepted['receipt']


@pytest.mark.parametrize('scope',['learning','knowledge','media','all_history'])
def test_real_child_export_and_scope_delete_clears_only_own_visuals(tmp_path,scope):
    db,settings,prepared,args,region=setup_case(tmp_path)
    with TestClient(create_app(settings),client=('127.0.0.1',42080)) as client:
        h,_=store(client,prepared,args)
        preview=client.get('/v1/parent/children/child/deletion-preview',headers=h)
        assert preview.status_code==200,preview.text
        assert preview.json()['visual_material_events']==1
        exported=client.post('/v1/parent/children/child/export',headers=h)
        assert exported.status_code==200,exported.text
        content=json.loads(Path(exported.json()['path']).read_text())
        assert len(content['visual_material_events'])==1
        assert prepared.png.hex() not in json.dumps(content)
        with db.connect() as c:
            # A separately owned audit is not a synthetic permission positive.
            c.execute("INSERT INTO audit_event SELECT 'other-audit',actor,action,'child-primary-4',summary_json,occurred_at FROM audit_event WHERE action='visual_material_host_v1' LIMIT 1")
            c.commit()
        response=client.post('/v1/parent/children/child/delete',headers=h,json={
            'scope':scope,'confirmation':'确认删除','client_action_id':'delete-'+scope})
        assert response.status_code==200,response.text
        assert response.json()['status']=='completed'
        assert list((tmp_path/'media').glob('*.png.enc'))==[]
        with db.connect() as c:
            assert c.execute("SELECT COUNT(*) FROM audit_event WHERE child_id='child' AND action='visual_material_host_v1'").fetchone()[0]==0
            assert c.execute("SELECT COUNT(*) FROM audit_event WHERE audit_id='other-audit'").fetchone()[0]==1
            assert c.execute("SELECT COUNT(*) FROM capture_artifact WHERE child_id='child' AND artifact_type='document_visual_region'").fetchone()[0]==0


def test_delete_helper_is_borrowed_atomic_and_does_not_swallow_sqlite(tmp_path):
    from home_study_agent.visual_material_lifecycle import delete_scope
    db,settings,prepared,args,_=setup_case(tmp_path)
    with TestClient(create_app(settings),client=('127.0.0.1',42080)) as client:
        _,stored=store(client,prepared,args)
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            counts,paths=delete_scope(c,'child',artifact_ids={'page'})
            assert counts['visual_material_assets']==1 and len(paths)==1
            c.rollback()
            assert c.execute('SELECT 1 FROM capture_artifact WHERE artifact_id=?',(stored['receipt']['asset_id'],)).fetchone()
            c.execute("CREATE TRIGGER stop_visual_delete BEFORE DELETE ON capture_artifact WHEN old.artifact_type='document_visual_region' BEGIN SELECT RAISE(ABORT,'synthetic_db_fault'); END")
            c.commit()
            c.execute('BEGIN IMMEDIATE')
            with pytest.raises(sqlite3.IntegrityError,match='synthetic_db_fault'):
                delete_scope(c,'child',artifact_ids={'page'})
            c.rollback()
            assert c.execute("SELECT COUNT(*) FROM audit_event WHERE action='visual_material_host_v1'").fetchone()[0]==1
