"""Real local parent HTTP, synthetic cropped pixels, no model dispatch."""
import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.document_vision import DiagramRegionGate
from home_study_agent.visual_material import record_facts
from test_document_vision import RegionEngine
from test_visual_material import visual_case, registered


def setup_case(tmp_path):
    db, monitor, raw, scope = visual_case(tmp_path)
    region = registered(db, raw, scope)
    with db.connect() as c:
        names = tuple(r[0] for r in c.execute('SELECT nickname FROM child_profile'))
        c.execute('BEGIN IMMEDIATE')
        result = record_facts(c, **scope, region_id='figure', region_version=region['region_version'],
            facts=dict(version='visual_facts_v1', observations=[dict(id='outline', kind='shape',
            description='A closed outline.', bbox=[.1,.1,.9,.9])], uncertainties=[]),
            expected_facts_version=None, client_action_id='facts')
        c.commit()
    prepared = DiagramRegionGate(RegionEngine()).prepare_region(raw,
        region_xyxy=[200,180,600,560], known_child_names=names)
    monitor.stop('study', 'stop-for-parent')
    with db.connect() as c:
        c.execute("UPDATE daily_session SET status='completed',state='completed' WHERE session_id='study'")
        c.commit()
    settings = Settings(data_dir=tmp_path, database_path=db.path, session_token='local-test',
        data_encryption_key=b'v'*32, maintenance_enabled=False)
    args = dict(revision_id=scope['revision_id'], region_version=region['region_version'],
        facts_version=result['facts_version'])
    return db, settings, prepared, args, region


def headers(client):
    response = client.post('/v1/parent/setup', json={'pin':'246810'}, headers={'X-HSC-Session':'local-test'})
    if response.status_code != 200:
        response = client.post('/v1/parent/unlock', json={'pin':'246810'}, headers={'X-HSC-Session':'local-test'})
    assert response.status_code == 200, response.text
    return {'X-HSC-Session':'local-test', 'X-HSC-Parent':response.json()['parent_token']}


BASE = '/v1/parent/children/child/visual-materials'
REGION = BASE + '/page/regions/figure'


def test_historical_parent_storage_display_confirmation_restart_and_retract(tmp_path):
    db, settings, prepared, args, region = setup_case(tmp_path)
    with TestClient(create_app(settings),client=('127.0.0.1',42080)) as client:
        h = headers(client)
        host = client.app.state.visual_materials
        host.stage_prepared('child', 'page', 'figure', **{k:args[k] for k in ('revision_id','region_version')}, prepared=prepared)
        listing = client.get(BASE, headers=h)
        assert listing.status_code == 200, listing.text
        item = listing.json()['items'][0]
        assert all(item[k] for k in ('artifact_id','region_id','revision_id','region_version','title','captured_at'))
        before = client.get(REGION, params={k:args[k] for k in ('revision_id','region_version')}, headers=h).json()
        assert before['source_current'] and before['storage_status'] == 'not_stored'
        assert not before['parent_confirmation_ready']
        stored = client.post(REGION+'/storage', json={**args,'client_action_id':'store',
            'purpose':'local_visual_material_review_v1'}, headers=h)
        assert stored.status_code == 200, stored.text
        assert stored.json()['detail']['image_availability'] == 'available'
        confirm = {**args,'client_action_id':'confirm','display_receipt_id':'not-real'}
        assert client.post(REGION+'/confirm', json=confirm, headers=h).status_code == 409
        image = client.get(REGION+'/image', params={**{k:args[k] for k in ('revision_id','region_version')},
            'sha256':region['safe_png_sha256']}, headers={**h,'Origin':'http://localhost:5173'})
        assert image.status_code == 200, image.text
        assert image.content == prepared.png
        assert hashlib.sha256(image.content).hexdigest() == region['safe_png_sha256']
        assert 'x-hsc-visual-display-token' in image.headers['access-control-expose-headers'].lower()
        assert image.headers['cache-control'] == 'no-store'
        range_response=client.get(REGION+'/image',params={**args,'sha256':region['safe_png_sha256']},headers={**h,'Range':'bytes=0-8'})
        assert range_response.status_code==416
        displayed = client.post(REGION+'/displayed', json={**args,'client_action_id':'display',
            'display_token':image.headers['X-HSC-Visual-Display-Token'],
            'sha256':region['safe_png_sha256'],'width':prepared.width,'height':prepared.height}, headers=h)
        assert displayed.status_code == 200, displayed.text
        confirm['display_receipt_id'] = displayed.json()['display_receipt_id']
        accepted = client.post(REGION+'/confirm', json=confirm, headers=h)
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()['detail']['status'] == 'confirmed'
        assert not accepted.json()['detail']['cloud_transmission_allowed']
    with TestClient(create_app(settings),client=('127.0.0.1',42080)) as client:
        h = headers(client)
        replay = client.post(REGION+'/confirm', json=confirm, headers=h)
        assert replay.status_code == 200, replay.text
        assert replay.json()['receipt'] == accepted.json()['receipt']
        with db.connect() as c:
            row = c.execute("SELECT local_path FROM capture_artifact WHERE artifact_type='document_visual_region'").fetchone()
            path = Path(row[0])
            assert path.read_bytes().startswith(b'HSC1') and prepared.png not in path.read_bytes()
            path.unlink()
        detail = client.get(REGION, params={k:args[k] for k in ('revision_id','region_version')}, headers=h).json()
        assert detail['image_availability'] == 'unavailable' and detail['storage_status'] == 'missing'
        assert not detail['parent_confirmation_ready']
        revoked = client.post(REGION+'/retract', json={**args,'client_action_id':'retract'}, headers=h)
        assert revoked.status_code == 200, revoked.text
        assert revoked.json()['detail']['status'] == 'withdrawn'
        assert client.get(REGION.replace('/child/','/other/'), params=args, headers=h).status_code == 404
        assert client.get(BASE).status_code == 401
        assert client.get(BASE,headers={'X-HSC-Session':'local-test'}).status_code==401
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM learning_evidence').fetchone()[0] == 0


def test_remote_client_cannot_read_visuals_even_with_real_parent_token(tmp_path):
    db,settings,_,args,_=setup_case(tmp_path)
    app=create_app(settings)
    with TestClient(app,client=('192.0.2.8',42080)) as client:
        h=headers(client)
        result=client.get(BASE,headers=h)
        assert result.status_code==403,result.text
        assert result.json()['detail']['code']=='VISUAL_MATERIAL_LOCAL_ONLY'
