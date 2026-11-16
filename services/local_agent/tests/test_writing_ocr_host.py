"""Mounted local OCR receipts on synthetic document/voice evidence."""
from synthetic_native_restore_fixture import KEY, signed_local_restore_post
import json
from pathlib import Path

import pytest
from privacy_restore_operation import restore_operation
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from test_companion_activity_host import NoCloud
from test_writing_response_origin import writing, CHILD
from test_writing_ocr_authorization import ocr, confirm_turn


def host(ocr, *, native_launch_attestation_key=None):
    db = ocr[0][0]
    settings = Settings(data_dir=db.path.parent, database_path=db.path,
        maintenance_enabled=False, session_token='writing-ocr-host-isolated', data_encryption_key=b'W'*32,native_launch_attestation_key=native_launch_attestation_key)
    return create_app(settings, model_gateway=NoCloud()), {'X-HSC-Session':settings.session_token}


def test_actual_readback_host_and_executor_confirmation_are_not_an_answer(ocr):
    writing, _, packet = ocr
    db, sid, _, _, _ = writing
    app, headers = host(ocr)
    base = f'/v1/sessions/{sid}/writing-ocr'
    binding = {k:packet[k] for k in ('confirmation_id','binding_version')}
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get(base, params=binding).status_code in {401,403}
        read = client.get(base, params=binding, headers=headers)
        assert read.status_code == 200, read.text
        assert read.headers['cache-control'] == 'no-store'
        assert read.json()['cloud_transmission_allowed'] is False
        stale = client.get(base, params={**binding,'binding_version':'0'*64}, headers=headers)
        assert stale.status_code == 409 and stale.headers['cache-control'] == 'no-store'
        payload = {**binding,'playback_id':'host-read', 'readback_sha256':packet['readback_sha256']}
        rejected = client.post(base+'/readback', headers=headers,
            json={**payload,'event':'ended','client_action_id':'premature-end'})
        assert rejected.status_code == 409, rejected.text
        for event in ('started','ended'):
            body = {**payload,'event':event,'client_action_id':'host-'+event}
            response = client.post(base+'/readback', headers=headers,json=body)
            assert response.status_code == 200, response.text
            assert client.post(base+'/readback', headers=headers,json=body).json() == response.json()
        turn = confirm_turn((writing,app.state.writing_ocr_authorization,packet))
        executor = app.state.agent_capability_executor
        step = executor._load(turn)
        result = executor._dispatch(step)
        assert result['evidence'] == ['writing_original_use_authorized']
        assert result['result']['cloud_transmission_allowed'] is False
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
            assert c.execute("SELECT COUNT(*) FROM writing_ocr_event WHERE kind='authorize'").fetchone()[0] == 1
    restarted, _ = host(ocr)
    with TestClient(restarted) as client:
        assert client.get(base,params=binding,headers=headers).status_code == 200
        withdrawn = client.post(base+'/retract',headers=headers,
            json={**binding,'client_action_id':'host-withdraw'})
        assert withdrawn.status_code == 200, withdrawn.text
        assert client.get(base,params=binding,headers=headers).status_code == 409


@pytest.mark.parametrize('scope',['learning','media','all_history'])
def test_ocr_export_and_erasure_leave_no_confirmation_after_restart(ocr,scope):
    db,sid,*_ = ocr[0]
    app, headers = host(ocr)
    with TestClient(app,raise_server_exceptions=False) as client:
        token = client.post('/v1/parent/setup',headers=headers,json={'pin':'2468'}).json()['parent_token']
        export = app.state.privacy_lifecycle.export_child(CHILD)
        content = json.loads(Path(export['path']).read_text())
        assert len(content['writing_ocr_confirmations']) == 1
        response = client.post(f'/v1/parent/children/{CHILD}/delete',headers={**headers,'X-HSC-Parent':token},
            json={'scope':scope,'confirmation':'确认删除','client_action_id':'delete-ocr'})
        assert response.status_code == 200 and response.json()['status']=='completed', response.text
        assert response.json()['deleted_counts']['writing_ocr_confirmation'] == 1
    restarted,_ = host(ocr)
    with TestClient(restarted):
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM writing_ocr_confirmation').fetchone()[0] == 0
            assert c.execute('SELECT COUNT(*) FROM writing_ocr_event').fetchone()[0] == 0
            assert not c.execute('PRAGMA foreign_key_check').fetchall()


@pytest.mark.parametrize('kind',['local','portable'])
def test_same_process_restore_does_not_accept_a_pre_restore_playback_end(ocr,kind):
    db,sid,*_ = ocr[0]
    packet=ocr[2]
    app,headers=host(ocr,native_launch_attestation_key=KEY if kind=='local' else None)
    path=f'/v1/sessions/{sid}/writing-ocr'
    binding={k:packet[k] for k in ('confirmation_id','binding_version')}
    body={**binding,'playback_id':'before-restore','readback_sha256':packet['readback_sha256']}
    with TestClient(app,raise_server_exceptions=False) as client:
        parent=client.post('/v1/parent/setup',headers=headers,json={'pin':'2468'}).json()['parent_token']
        parent_headers={**headers,'X-HSC-Parent':parent}
        assert client.post(path+'/readback',headers=headers,
            json={**body,'event':'started','client_action_id':'before-restore-start'}).status_code==200
        passphrase='Synthetic writing backup phrase'
        backup=(client.post('/v1/parent/backups',headers=parent_headers) if kind=='local' else
            client.post('/v1/parent/backups/portable',headers=parent_headers,json={'passphrase':passphrase}))
        assert backup.status_code==200,backup.text
        # Restore is parent-only and forbidden during active study. Simulate a
        # completed host stop without changing the saved in-flight snapshot.
        with db.connect() as c:
            c.execute("UPDATE daily_session SET status='cancelled' WHERE session_id=?",(sid,))
            c.commit()
        restored=(signed_local_restore_post(client,headers=parent_headers,
            json={'backup_id':backup.json()['backup_id'],'confirmation':'确认恢复',
                  'operation':restore_operation(client,headers=parent_headers)}) if kind=='local' else
            client.post('/v1/parent/backups/portable/restore',headers=parent_headers,
                json={'path':backup.json()['path'],'passphrase':passphrase,'confirmation':'确认迁移恢复',
                      'operation':restore_operation(client,headers=parent_headers)}))
        assert restored.status_code==200,restored.text
        late=client.post(path+'/readback',headers=headers,
            json={**body,'event':'ended','client_action_id':'old-playback-end'})
        assert late.status_code==409,late.text
        assert late.json()['code']=='WRITING_READBACK_START_MISSING_OR_STALE'
        current={**body,'playback_id':'after-restore'}
        for event in ('started','ended'):
            response=client.post(path+'/readback',headers=headers,
                json={**current,'event':event,'client_action_id':'restored-'+event})
            assert response.status_code==200,response.text
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==0
