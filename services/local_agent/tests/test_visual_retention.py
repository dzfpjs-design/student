"""Actual parent consent + camera OCR/revision, isolated synthetic local pixels."""
from synthetic_native_restore_fixture import KEY, signed_local_restore_post
import json
from pathlib import Path
from datetime import timedelta
import sqlite3

import pytest
from home_study_agent import visual_retention as module

from test_visual_capture_producer import local_client, begin, parent_headers, capture, revise

POLICY = '/v1/parent/children/child-junior-3/visual-retention'


def retained_region_crops(root: Path):
    """Region crops live at media root; accepted full pages use child/session folders."""
    return list((root / 'media').glob('*.png.enc'))


def grant(client, headers, *, action='enable', revision=None, key='enable-retention', session_id=None, **kw):
    body = dict(action=action, expected_revision=revision, client_action_id=key)
    if action != 'revoke':
        body.update(session_id=session_id, retention_days=7, authorization_days=30, max_bytes=1048576)
        body.update(kw)
    result = client.post(POLICY, headers=headers, json=body)
    assert result.status_code == 200, result.text
    return result.json(), body


def image_request(client, item, headers):
    b = item['binding']
    url = f"/v1/parent/children/{b['child_id']}/visual-materials/{b['artifact_id']}/regions/{b['region_id']}/image"
    args = dict(revision_id=b['revision_id'], region_version=b['region_version'], sha256=item['image_descriptor']['sha256'])
    return client.get(url, headers=headers, params=args), url, args


def test_enable_real_capture_auto_retains_no_parent_storage_click_and_restart(tmp_path):
    with local_client(tmp_path) as client:
        sid, _ = begin(client)
        h = parent_headers(client)
        default = client.get(POLICY, headers=h)
        assert default.status_code == 200, default.text
        assert default.json()['policy']['status'] == 'disabled'
        enabled, body = grant(client, h)
        policy_id = enabled['policy']['revision']
        artifact, raw = capture(client, sid)
        response = revise(client, sid, artifact)
        assert response.status_code == 200, response.text
        item = response.json()['visual_capture']['items'][0]
        assert item['retention']['status'] == 'stored'
        image, url, args = image_request(client, item, h)
        assert image.status_code == 200, image.text
        png = image.content
        assert png != raw and item['facts_version'] is None
        db = client.app.state.visual_materials.database
        with db.connect() as c:
            assets = c.execute("SELECT * FROM capture_artifact WHERE artifact_type='document_visual_region'").fetchall()
            assert len(assets) == 1
            path = Path(assets[0]['local_path'])
            assert path.read_bytes().startswith(b'HSC1') and png not in path.read_bytes()
            metadata = json.loads(assets[0]['details_json'])
            assert metadata['retention']['policy_id'] == policy_id
            events = c.execute("SELECT actor,summary_json FROM audit_event WHERE action='visual_material_host_v1'").fetchall()
            assert len(events) == 1 and events[0]['actor'] == 'system_capture'
            assert json.loads(events[0]['summary_json'])['request']['delegation']['policy_id'] == policy_id
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 0
        assert revise(client, sid, artifact).json()['visual_capture']['items'][0]['retention']['status'] == 'stored'
    with local_client(tmp_path) as client:
        h = parent_headers(client)
        assert client.get(url, headers=h, params=args).content == png
        replay = client.post(POLICY, headers=h, json=body)
        assert replay.status_code == 200
        assert replay.json()['receipt'] == enabled['receipt']
        assert client.get(POLICY, headers=h).json()['policy']['revision'] == policy_id


def auto_image(client,sid,h,key='retained-frame'):
    artifact,_=capture(client,sid,key=key)
    result=revise(client,sid,artifact,key='revision:'+key)
    assert result.status_code==200,result.text
    item=result.json()['visual_capture']['items'][0]
    image,url,args=image_request(client,item,h)
    return artifact,item,image,url,args


@pytest.mark.parametrize('change',['revoke_regrant','update','expires'])
def test_capture_freezes_authority_new_grant_never_upgrades_old_crop(tmp_path,monkeypatch,change):
    with local_client(tmp_path) as client:
        sid,_=begin(client)
        h=parent_headers(client)
        enabled,body=grant(client,h)
        artifact,_=capture(client,sid)
        if change=='revoke_regrant':
            off,_=grant(client,h,action='revoke',revision=enabled['policy']['revision'],key='revoke-policy')
            grant(client,h,revision=off['policy']['revision'],key='new-policy')
        elif change=='update':
            grant(client,h,action='update',revision=enabled['policy']['revision'],key='update-policy',retention_days=5)
        else:
            captured_now=module.now()
            monkeypatch.setattr(module,'now',lambda:captured_now+timedelta(days=31))
        r=revise(client,sid,artifact)
        assert r.status_code==200,r.text
        item=r.json()['visual_capture']['items'][0]
        assert item['retention']['status']=='not_stored'
        assert item['image_availability']=='unavailable'
        assert client.app.state.visual_materials._staged
        assert not retained_region_crops(tmp_path)
        replay=client.post(POLICY,headers=h,json=body)
        assert replay.status_code==200,replay.text
        assert replay.json()['receipt']==enabled['receipt']
        assert replay.json()['replayed']
        assert replay.json()['policy']['status']==('expired' if change=='expires' else 'enabled')


def test_default_no_raw_or_cloud_inheritance_and_parent_auth_cas(tmp_path):
    with local_client(tmp_path) as client:
        sid,_=begin(client)
        assert client.get(POLICY).status_code==401
        h=parent_headers(client)
        assert client.get(POLICY.replace('child-junior-3','unknown'),headers=h).status_code==404
        artifact,item,image,_,_=auto_image(client,sid,h)
        assert item['retention']['reason']=='VISUAL_RETENTION_NOT_ENABLED'
        assert image.status_code==409
        enabled,body=grant(client,h)
        assert client.post(POLICY,headers=h,json={**body,'max_bytes':2097152}).status_code==409
        assert client.post(POLICY,headers=h,json={**body,'client_action_id':'stale-other-key'}).status_code==409
        assert client.post(POLICY,headers=h,json={**body,'actor':'parent_ui'}).status_code==422
        assert client.post(POLICY,headers=h,json={**body,'max_bytes':True}).status_code==422
        # Merely enabling later cannot retroactively auto-retain the old frame.
        assert revise(client,sid,artifact).json()['visual_capture']['items'][0]['retention']['status']=='not_stored'
        other=client.get(POLICY.replace('child-junior-3','child-primary-4'),headers=h).json()
        assert other['policy']['status']=='disabled'
        scope={**body,'expected_revision':enabled['policy']['revision'],'client_action_id':'wrong-session-policy','session_id':'other-session'}
        assert client.post(POLICY,headers=h,json=scope).status_code==409


@pytest.mark.parametrize('change',['revoke','expiry'])
def test_permission_or_retention_expiry_blocks_bytes_and_deletes_exact_asset(tmp_path,monkeypatch,change):
    with local_client(tmp_path) as client:
        sid,_=begin(client)
        h=parent_headers(client)
        enabled,_=grant(client,h)
        artifact,item,image,url,args=auto_image(client,sid,h)
        assert image.status_code==200,image.text
        if change=='revoke':
            grant(client,h,action='revoke',revision=enabled['policy']['revision'],key='off-and-clean')
        else:
            before=module.now()
            monkeypatch.setattr(module,'now',lambda:before+timedelta(days=8))
            assert client.get(url,headers=h,params=args).status_code==409
            client.app.state.visual_retention.cleanup()
        assert client.get(url,headers=h,params=args).status_code==409
        assert not retained_region_crops(tmp_path)
        with client.app.state.visual_retention.database.connect() as c:
            assert c.execute('SELECT 1 FROM capture_artifact WHERE artifact_id=?',(artifact['artifact_id'],)).fetchone()
            assert c.execute("SELECT count(*) FROM deletion_job WHERE status='completed'").fetchone()[0]>=1
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0]==0


def test_revoke_during_file_write_cannot_finish_delegated_grant(tmp_path,monkeypatch):
    from home_study_agent import visual_material_host as host_module
    with local_client(tmp_path) as client:
        sid,_=begin(client)
        h=parent_headers(client)
        enabled,_=grant(client,h)
        artifact,_=capture(client,sid)
        write=host_module.write_new_managed_media
        def revoke_after_file(*args,**kwargs):
            write(*args,**kwargs)
            client.app.state.visual_retention.command('child-junior-3',action='revoke',
                expected_revision=enabled['policy']['revision'],client_action_id='revoke-inside-worker')
        monkeypatch.setattr(host_module,'write_new_managed_media',revoke_after_file)
        r=revise(client,sid,artifact)
        assert r.status_code==200,r.text
        assert r.json()['visual_capture']['items'][0]['retention']['status']=='not_stored'
        assert not retained_region_crops(tmp_path)
        with client.app.state.visual_retention.database.connect() as c:
            assert c.execute("SELECT count(*) FROM audit_event WHERE action='visual_material_host_v1' AND actor='system_capture'").fetchone()[0]==0


@pytest.mark.parametrize('portable',[False,True])
def test_actual_backup_restore_quarantines_policy_and_old_key_without_regrant(tmp_path,portable):
    from privacy_restore_operation import restore_operation
    with local_client(tmp_path, native_launch_attestation_key=KEY if not portable else None) as client:
        sid,root=begin(client)
        h=parent_headers(client)
        enabled,body=grant(client,h)
        _,_,image,url,args=auto_image(client,sid,h)
        assert image.status_code==200
        # Real stop and ended isolated fixture permit a backup restore, not live camera recovery.
        assert client.post(root+'/stop',json={'client_action_id':'before-backup-stop'}).status_code==200
        with client.app.state.visual_retention.database.connect() as c:
            c.execute("UPDATE daily_session SET status='completed',state='completed' WHERE session_id=?",(sid,))
            c.commit()
        backup=client.post('/v1/parent/backups/portable' if portable else '/v1/parent/backups',headers=h,
            json={'passphrase':'isolated-retention-backup-phrase'} if portable else None)
        assert backup.status_code==200,backup.text
        request=({'path':backup.json()['path'],'passphrase':'isolated-retention-backup-phrase'} if portable
                 else {'backup_id':backup.json()['backup_id']})
        request.update(confirmation='确认迁移恢复' if portable else '确认恢复',operation=restore_operation(client,headers=h))
        restored=(client.post('/v1/parent/backups/portable/restore',headers=h,json=request) if portable else
            signed_local_restore_post(client,headers=h,json=request))
        assert restored.status_code==200,restored.text
        h=parent_headers(client)
        state=client.get(POLICY,headers=h).json()['policy']
        assert state['status']=='quarantined'
        assert state['reason']=='VISUAL_RETENTION_RESTORE_REAUTHORIZATION_REQUIRED'
        assert client.get(url,headers=h,params=args).status_code==409
        retry=client.post(POLICY,headers=h,json=body)
        assert retry.status_code==200,retry.text
        assert retry.json()['receipt']==enabled['receipt'] and retry.json()['policy']['status']=='quarantined'


@pytest.mark.parametrize('scope',['learning','knowledge','media','all_history'])
def test_export_scope_delete_removes_policy_not_other_child(tmp_path,scope):
    with local_client(tmp_path) as client:
        sid,_=begin(client)
        h=parent_headers(client)
        grant(client,h)
        other=client.post(POLICY.replace('child-junior-3','child-primary-4'),headers=h,json=dict(
            action='enable',expected_revision=None,client_action_id='other-retention',session_id=None,
            retention_days=7,authorization_days=30,max_bytes=1048576))
        assert other.status_code==200,other.text
        auto_image(client,sid,h)
        exported=client.post('/v1/parent/children/child-junior-3/export',headers=h)
        assert exported.status_code==200,exported.text
        data=json.loads(Path(exported.json()['path']).read_text())
        assert module.ACTION in json.dumps(data)
        deleted=client.post('/v1/parent/children/child-junior-3/delete',headers=h,json={
            'scope':scope,'confirmation':'确认删除','client_action_id':'delete-retention:'+scope})
        assert deleted.status_code==200,deleted.text
        assert client.get(POLICY,headers=h).json()['policy']['status']=='disabled'
        assert client.get(POLICY.replace('child-junior-3','child-primary-4'),headers=h).json()['policy']['status']=='enabled'
        assert not retained_region_crops(tmp_path)


def test_full_quota_projection_keeps_real_http_candidate_transient(tmp_path,monkeypatch):
    """Conservative full-ledger fault injection, not fabricated image/permission."""
    with local_client(tmp_path) as client:
        sid,_=begin(client)
        h=parent_headers(client)
        grant(client,h)
        original=module.current
        def full(c,child):
            policy=original(c,child)
            if policy['status']=='enabled': policy['used_bytes']=policy['max_bytes']
            return policy
        with monkeypatch.context() as m:
            m.setattr(module,'current',full)
            _,item,image,_,_=auto_image(client,sid,h)
            assert item['retention']['reason']=='VISUAL_RETENTION_CAPACITY'
            assert image.status_code==409
            assert client.app.state.visual_materials._staged
            assert not retained_region_crops(tmp_path)
        # New frame after capacity is available can use the same current policy.
        _,item,image,_,_=auto_image(client,sid,h,key='after-capacity-frame')
        assert image.status_code==200,image.text
        with client.app.state.visual_retention.database.connect() as c:
            used=sum(Path(r[0]).stat().st_size for r in c.execute(
                "SELECT local_path FROM capture_artifact WHERE artifact_type='document_visual_region'").fetchall())
        assert client.get(POLICY,headers=h).json()['policy']['used_bytes']==used


def test_changed_source_stops_read_and_current_retention_without_changing_facts(tmp_path):
    with local_client(tmp_path) as client:
        sid,_=begin(client)
        h=parent_headers(client)
        grant(client,h)
        artifact,item,image,url,args=auto_image(client,sid,h)
        assert image.status_code==200
        with client.app.state.visual_retention.database.connect() as c:
            c.execute('UPDATE capture_artifact SET extracted_text=? WHERE artifact_id=?',('changed original source',artifact['artifact_id']))
            c.commit()
        assert client.get(url,headers=h,params=args).status_code==409
        client.app.state.visual_retention.cleanup()
        assert not retained_region_crops(tmp_path)


def test_policy_cleanup_fault_keeps_committed_revoke_receipt_and_deletion_retry(tmp_path,monkeypatch):
    from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
    with local_client(tmp_path) as client:
        sid,_=begin(client)
        h=parent_headers(client)
        enabled,_=grant(client,h)
        _,_,image,url,args=auto_image(client,sid,h)
        assert image.status_code==200
        original=PrivacyLifecycleService.delete_capture_artifact
        def failed(*a,**kw): raise sqlite3.OperationalError('isolated cleanup unavailable')
        body=dict(action='revoke',expected_revision=enabled['policy']['revision'],client_action_id='revoke-with-cleanup-fault')
        with monkeypatch.context() as m:
            m.setattr(PrivacyLifecycleService,'delete_capture_artifact',failed)
            with pytest.raises(sqlite3.OperationalError,match='isolated cleanup unavailable'):
                client.post(POLICY,headers=h,json=body)
        assert client.get(url,headers=h,params=args).status_code==409
        replay=client.post(POLICY,headers=h,json=body)
        assert replay.status_code==200 and replay.json()['replayed']
        assert replay.json()['receipt']['status']=='revoked'
        client.app.state.visual_retention.cleanup()
        assert not retained_region_crops(tmp_path)


@pytest.mark.parametrize('scope',['reports','tutor'])
def test_unrelated_privacy_scopes_keep_policy(tmp_path,scope):
    with local_client(tmp_path) as client:
        begin(client)
        h=parent_headers(client)
        enabled,_=grant(client,h)
        deleted=client.post('/v1/parent/children/child-junior-3/delete',headers=h,json={
            'scope':scope,'confirmation':'确认删除','client_action_id':'unrelated-delete:'+scope})
        assert deleted.status_code==200,deleted.text
        assert client.get(POLICY,headers=h).json()['policy']['revision']==enabled['policy']['revision']
