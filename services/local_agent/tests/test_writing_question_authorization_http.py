from local_restore_operation_fixture import restore_local
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from home_study_agent.database import Database
"""Parent purpose decisions through the application, not self-asserted actors."""
from synthetic_native_restore_fixture import KEY, signed_local_restore_post
from dataclasses import replace
import pytest
from privacy_restore_operation import restore_operation
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from test_writing_question_authorization import published_question, LICENSE
from test_writing_source_publication import CHILD


def host(case, monkeypatch):
    from secrets import token_bytes
    db = case[0]
    monkeypatch.setenv('HSC_DATA_DIR', str(db.path.parent))
    monkeypatch.setenv('HSC_DATABASE_PATH', str(db.path))
    return Settings(data_dir=db.path.parent, database_path=db.path, maintenance_enabled=False,
                    data_encryption_key=token_bytes(32))


def parent(client, setup=False):
    response = client.post('/v1/parent/'+('setup' if setup else 'unlock'), json={'pin':'8264'})
    assert response.status_code == 200, response.text
    return {'X-HSC-Parent':response.json()['parent_token']}


def path(case, child=CHILD):
    return f'/v1/parent/children/{child}/writing-question-authorizations/{case[3]["diagnostic_item_id"]}'


def grant_body(view):
    return {**{key:view[key] for key in ('item_version','source_version','curriculum_version')},
        'license_basis':LICENSE, 'expected_revision':view['revision'], 'client_action_id':'http-purpose-grant'}


def test_parent_grant_restart_revoke_and_lost_receipt_replay(published_question, monkeypatch):
    settings = host(published_question,monkeypatch)
    url = path(published_question)
    with TestClient(create_app(settings)) as client:
        assert client.get(url).status_code == 401
        headers = parent(client,True)
        response = client.get(url,headers=headers)
        assert response.status_code == 200,response.text
        assert response.headers['cache-control']=='no-store'
        view = response.json()
        assert view['status']=='authorization_required'
        body = grant_body(view)
        assert client.post(url+'/grant',headers=headers,json={**body,'actor_id':'parent'}).status_code == 422
        granted = client.post(url+'/grant',headers=headers,json=body)
        assert granted.status_code==200,granted.text
        assert granted.json()['current']['status']=='authorized'
        receipt = granted.json()['receipt']
    with TestClient(create_app(settings)) as client:
        headers = parent(client)
        assert client.get(url,headers=headers).json()['status']=='authorized'
        revoked = client.post(url+'/revoke',headers=headers,json={'expected_revision':1,'client_action_id':'http-purpose-revoke'})
        assert revoked.status_code==200,revoked.text
        replay = client.post(url+'/grant',headers=headers,json=body)
        assert replay.status_code==200,replay.text
        assert replay.json()['receipt']==receipt
        assert replay.json()['current']['status']=='revoked'
        assert replay.json()['current']['cloud_transmission_allowed'] is False
        conflict = client.post(url+'/grant',headers=headers,json={**body,'client_action_id':'new-old-version'})
        assert conflict.status_code==409,conflict.text
        other = client.get(path(published_question,'child-junior-3'),headers=headers)
        assert other.status_code==200 and other.json()['preview'] is None
        assert client.post(path(published_question,'child-junior-3')+'/grant',headers=headers,json=body).status_code==409
    with published_question[0].connect() as c:
        rows = c.execute('SELECT actor_id,command FROM writing_question_authorization_event ORDER BY revision').fetchall()
        assert [tuple(row) for row in rows]==[('parent_ui','grant'),('parent_ui','revoke')]


@pytest.mark.parametrize('scope',['learning','knowledge','media','all_history'])
def test_purpose_authority_is_in_export_preview_and_all_relevant_deletion_scopes(published_question,scope):
    from test_writing_question_authorization import grant
    db,service,_,published,privacy = published_question
    privacy = PrivacyLifecycleService(db, db.path.parent, b'0' * 32)
    grant(published_question)
    assert privacy.deletion_preview(CHILD)['writing_question_authorizations']==1
    exported = privacy.export_child(CHILD)
    # Export is a local artifact; inspect its durable payload below.
    import json
    from pathlib import Path
    payload = json.loads(Path(exported['path']).read_text())
    assert len(payload['writing_question_authorizations'])==1
    result = privacy.delete_child_scope(CHILD,scope,'确认删除','delete-purpose-'+scope)
    assert result['status']=='completed',result
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM writing_question_authorization_event WHERE child_id=?',(CHILD,)).fetchone()[0]==0
        assert not c.execute('PRAGMA foreign_key_check').fetchall()


def test_actual_backup_restore_requires_new_purpose_grant(published_question):
    from test_writing_question_authorization import grant
    db,service,_,published,privacy = published_question
    configured = Database(db.path, runtime_mode=db.runtime_mode, media_root=db.path.parent, data_encryption_key=b'0' * 32)
    privacy = PrivacyLifecycleService(configured, db.path.parent, b'0' * 32)
    view = service.read(CHILD,diagnostic_item_id=published['diagnostic_item_id'])
    before = grant(published_question)
    backup = privacy.create_backup()
    service.revoke(CHILD,diagnostic_item_id=published['diagnostic_item_id'],expected_revision=1,
        client_action_id='after-backup-revoke',actor_id='authenticated-parent')
    restore_local(privacy,backup['backup_id'],'确认恢复')
    current = service.read(CHILD,diagnostic_item_id=published['diagnostic_item_id'])
    assert current['status']=='revoked'
    assert current['reason']=='WRITING_QUESTION_RESTORE_REAUTHORIZATION_REQUIRED'
    replay = service.grant(CHILD,diagnostic_item_id=published['diagnostic_item_id'],
        **{key:view[key] for key in ('item_version','source_version','curriculum_version')},
        license_basis=LICENSE,expected_revision=0,client_action_id='grant',actor_id='authenticated-parent')
    assert replay['receipt']==before['receipt'] and replay['current']['status']=='revoked'
    with db.connect() as c:
        assert not c.execute('PRAGMA foreign_key_check').fetchall()


@pytest.mark.parametrize('portable',[False,True])
def test_parent_restore_http_never_reactivates_backed_up_question_permission(published_question,monkeypatch,portable):
    settings = host(published_question,monkeypatch)
    settings = replace(settings,native_launch_attestation_key=KEY if not portable else None)
    url = path(published_question)
    with TestClient(create_app(settings)) as client:
        headers = parent(client,True)
        view = client.get(url,headers=headers).json()
        body = grant_body(view)
        original = client.post(url+'/grant',headers=headers,json=body)
        assert original.status_code==200,original.text
        passphrase = 'Synthetic isolated purpose backup 4721'
        endpoint = '/v1/parent/backups'+('/portable' if portable else '')
        backup = client.post(endpoint,headers=headers,**({'json':{'passphrase':passphrase}} if portable else {}))
        assert backup.status_code==200,backup.text
        revoked = client.post(url+'/revoke',headers=headers,json={'expected_revision':1,'client_action_id':'before-restore-revoke'})
        assert revoked.status_code==200,revoked.text
        payload = {'path':backup.json()['path'],'passphrase':passphrase,'confirmation':'确认迁移恢复'} if portable else {
            'backup_id':backup.json()['backup_id'],'confirmation':'确认恢复'}
        payload['operation'] = restore_operation(client, headers=headers)
        restored = (client.post(endpoint+'/restore',headers=headers,json=payload) if portable else
            signed_local_restore_post(client,headers=headers,json=payload))
        assert restored.status_code==200,restored.text
        current = client.get(url,headers=headers).json()
        assert current['status']=='revoked' and current['reason']=='WRITING_QUESTION_RESTORE_REAUTHORIZATION_REQUIRED'
        replay = client.post(url+'/grant',headers=headers,json=body)
        assert replay.status_code==200,replay.text
        assert replay.json()['receipt']==original.json()['receipt']
        assert replay.json()['current']['status']=='revoked'
    with TestClient(create_app(settings)) as client:
        current = client.get(url,headers=parent(client)).json()
        assert current['status']=='revoked' and current['revision']==2
