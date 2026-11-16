"""Storage crash and no-facts recovery; service calls are trusted parent hooks."""
import json

import pytest

from home_study_agent.document_vision import DiagramRegionGate
from home_study_agent.visual_material_host import VisualMaterialHost, PURPOSE
from home_study_agent.local_supply import LocalSupplyError
from test_visual_material import visual_case,registered
from test_document_vision import RegionEngine


def staged(tmp_path):
    db,monitor,raw,scope=visual_case(tmp_path)
    region=registered(db,raw,scope)
    with db.connect() as c:
        names=tuple(r[0] for r in c.execute('SELECT nickname FROM child_profile'))
    prepared=DiagramRegionGate(RegionEngine()).prepare_region(raw,region_xyxy=[200,180,600,560],known_child_names=names)
    host=VisualMaterialHost(db,data_dir=tmp_path,data_encryption_key=b'z'*32)
    args=dict(revision_id=scope['revision_id'],region_version=region['region_version'],facts_version=None)
    host.stage_prepared('child','page','figure',revision_id=args['revision_id'],region_version=args['region_version'],prepared=prepared)
    return db,host,args,prepared


def test_no_facts_storage_can_be_retracted_and_reauthorized_without_fact_claim(tmp_path):
    db,host,args,prepared=staged(tmp_path)
    stored=host.storage('child','page','figure',**args,client_action_id='retain',purpose=PURPOSE)
    assert stored['detail']['image_availability']=='available'
    assert not stored['detail']['parent_confirmation_ready']
    withdrawn=host.decide('child','page','figure',**args,client_action_id='withdraw',decision='retract')
    assert withdrawn['detail']['status']=='withdrawn'
    assert withdrawn['detail']['storage_status']=='revoked'
    assert host.storage('child','page','figure',**args,client_action_id='retain',purpose=PURPOSE)['receipt']==stored['receipt']
    renewed=host.storage('child','page','figure',**args,client_action_id='retain-again',purpose=PURPOSE)
    assert renewed['detail']['image_availability']=='available'
    assert renewed['detail']['fact_status']=='unconfirmed'
    assert not renewed['detail']['parent_confirmation_ready']


def test_file_written_process_loss_retries_original_reserved_asset_without_preparation(tmp_path,monkeypatch):
    from home_study_agent import visual_material_host as module
    db,host,args,prepared=staged(tmp_path)
    class SimulatedCrash(BaseException):
        pass
    real=module.write_new_managed_media
    def crash(*a,**kw):
        real(*a,**kw)
        raise SimulatedCrash()
    monkeypatch.setattr(module,'write_new_managed_media',crash)
    with pytest.raises(SimulatedCrash):
        host.storage('child','page','figure',**args,client_action_id='retain',purpose=PURPOSE)
    with db.connect() as c:
        before=c.execute("SELECT artifact_id FROM capture_artifact WHERE artifact_type='document_visual_region'").fetchone()[0]
    monkeypatch.setattr(module,'write_new_managed_media',real)
    reopened=VisualMaterialHost(db,data_dir=tmp_path,data_encryption_key=b'z'*32)
    recovered=reopened.storage('child','page','figure',**args,client_action_id='retain',purpose=PURPOSE)
    assert recovered['receipt']['asset_id']==before
    assert recovered['detail']['image_availability']=='available'
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM capture_artifact WHERE artifact_type='document_visual_region'").fetchone()[0]==1
    assert len(list((tmp_path/'media').glob('*.png.enc')))==1


def test_display_token_tamper_wrong_shape_and_range_are_not_display_receipts(tmp_path):
    db,host,args,prepared=staged(tmp_path)
    stored=host.storage('child','page','figure',**args,client_action_id='retain',purpose=PURPOSE)
    descriptor=stored['detail']['image_descriptor']
    raw,token=host.image('child','page','figure',revision_id=args['revision_id'],region_version=args['region_version'],sha256=descriptor['sha256'])
    assert raw==prepared.png
    body=dict(**args,client_action_id='display',display_token=token,sha256=descriptor['sha256'],width=prepared.width,height=prepared.height)
    for delta in ({'display_token':token+'0'},{'width':prepared.width+1},{'revision_id':'other'}):
        with pytest.raises(LocalSupplyError):
            host.displayed('child','page','figure',**{**body,**delta})
    displayed=host.displayed('child','page','figure',**body)
    assert displayed['display_receipt_id']
    with db.connect() as c:
        events=[json.loads(r[0]) for r in c.execute("SELECT summary_json FROM audit_event WHERE action='visual_material_host_v1'")]
        assert len(events)==2
        assert all('display_token' not in e['request'] for e in events)
    with pytest.raises(LocalSupplyError,match='REQUEST_CONFLICT'):
        host.displayed('child','page','figure',**{**body,'height':prepared.height+1})
