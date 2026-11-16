"""Real curriculum jobs consume the same grant lifecycle as family maintenance."""
import asyncio
import json
from pathlib import Path

import pytest

from home_study_agent.curriculum_internalization import CurriculumInternalizationService
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from test_curriculum_internalization_dispatch import dispatch_case, make_service, create, CHILD
from normal_provider_owner import normal_provider_owner


@pytest.mark.parametrize('kind',['local','portable'])
def test_restored_pending_job_cannot_reuse_original_transport_authority(dispatch_case, kind):
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    from privacy_restore_operation import restore_operation
    from synthetic_native_restore_request import KEY
    from test_native_privacy_permit_http import certificate, signed_header

    db, corpus, _, root = dispatch_case
    settings = Settings(data_dir=root, database_path=db.path, maintenance_enabled=False,
                        data_encryption_key=b'C'*32, native_launch_attestation_key=KEY)
    with TestClient(create_app(settings)) as client:
        authenticated = client.post('/v1/parent/setup', json={'pin':'4286'})
        assert authenticated.status_code == 200, authenticated.text
        headers = {'X-HSC-Parent':authenticated.json()['parent_token']}
        service, posts = make_service(dispatch_case)
        job = create(service, corpus)
        privacy = client.app.state.privacy_lifecycle
        password = 'synthetic pending curriculum restore'
        backup = privacy.create_backup(prune=False) if kind=='local' else privacy.create_portable_backup(password)
        path = '/v1/parent/backups/restore' if kind=='local' else '/v1/parent/backups/portable/restore'
        body = ({'backup_id':backup['backup_id'], 'confirmation':'确认恢复'} if kind=='local'
                else {'path':backup['path'], 'passphrase':password, 'confirmation':'确认迁移恢复'})
        body['operation'] = restore_operation(client, headers=headers)
        proof = certificate(path, body, body['operation'], kind='restore' if kind=='local' else 'portable_restore', child_id=None, scope=None)
        restored = client.post(path, headers={**headers, **signed_header(proof)}, json=body)
        assert restored.status_code == 200, restored.text
        restarted = CurriculumInternalizationService(db, service.gateway)
        asyncio.run(restarted.run_job(job['job_id']))
        receipt = restarted.get_job(job['job_id'])
        assert receipt['status']=='failed' and receipt['error_code']=='INTERNALIZATION_RESTORE_REAUTHORIZATION_REQUIRED', receipt
        assert not posts
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM curriculum_capability').fetchone()[0]==0
            assert c.execute("SELECT COUNT(*) FROM audit_event WHERE child_id=? AND action='curriculum_internalization_authority_v1'",(CHILD,)).fetchone()[0]==1


@pytest.mark.parametrize('scope',['learning','knowledge','media','all_history'])
def test_actual_scope_delete_during_response_prevents_curriculum_capability_persistence(dispatch_case, scope, normal_provider_owner):
    db, corpus, _, root = dispatch_case
    privacy = PrivacyLifecycleService(db, root, data_encryption_key=b'C'*32)
    privacy.initialize()
    deletes = []

    def remove_source_permission():
        exported = json.loads(Path(privacy.export_child(CHILD)['path']).read_text())
        assert len(exported['curriculum_internalization_authorities'])==1
        deletes.append(privacy.delete_child_scope(CHILD,scope,'确认删除','curriculum-live-delete-'+scope))

    service, posts = make_service(dispatch_case, remove_source_permission)
    job = create(service, corpus)
    asyncio.run(service.run_job(job['job_id']))
    assert len(posts)==1
    assert len(deletes)==1 and deletes[0]['status']=='completed', deletes
    assert service.get_job(job['job_id'])['status']=='failed'
    assert privacy.deletion_preview(CHILD)['curriculum_internalization_authorities']==0
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM curriculum_capability').fetchone()[0]==0
        assert not c.execute('PRAGMA foreign_key_check').fetchall()
