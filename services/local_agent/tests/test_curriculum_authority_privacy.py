"""Local authority history is exportable, removable and inert after restore."""
import hashlib
import json
from pathlib import Path

import pytest

from home_study_agent.database import Database
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from home_study_agent.repository import StudyRepository

ACTIVE = 'curriculum_internalization_authority_v1'
QUARANTINED = 'curriculum_internalization_authority_quarantined_v1'
CHILD = 'child-primary-4'
OTHER = 'child-junior-3'


@pytest.fixture
def authority_home(tmp_path):
    db = Database(tmp_path / 'test.db')
    db.initialize()
    StudyRepository(db).seed_family()
    privacy = PrivacyLifecycleService(db, tmp_path, data_encryption_key=b'R' * 32)
    privacy.initialize()
    with db.connect() as c:
        for audit, child, action, payload in (
            ('grant-a', CHILD, ACTIVE, {'job_id':'synthetic-job-a','batch_id':'synthetic-batch-a','source_hash':'a'*64}),
            ('grant-b', OTHER, ACTIVE, {'job_id':'synthetic-job-b','batch_id':'synthetic-batch-b','source_hash':'b'*64}),
            ('old-quarantine-a', CHILD, QUARANTINED, {'authority_audit_id':'older-grant','reason':'backup_restore'}),
            ('unrelated', CHILD, 'unrelated_local_audit', {'status':'retained'}),
        ):
            c.execute("INSERT INTO audit_event(audit_id,actor,action,child_id,summary_json,occurred_at) VALUES (?,'system',?,?,?,datetime('now'))",
                      (audit, action, child, json.dumps(payload)))
        c.commit()
    return db, privacy


@pytest.mark.parametrize('scope',['learning','knowledge','media','all_history'])
def test_authority_export_preview_delete_and_restart_are_child_scoped(authority_home, scope):
    db, privacy = authority_home
    before = privacy.deletion_preview(CHILD)
    assert before['curriculum_internalization_authorities'] == 2
    exported = json.loads(Path(privacy.export_child(CHILD)['path']).read_text())
    assert {r['audit_id'] for r in exported['curriculum_internalization_authorities']} == {'grant-a','old-quarantine-a'}
    result = privacy.delete_child_scope(CHILD, scope, '确认删除', 'authority-delete-'+scope)
    assert result['status'] == 'completed', result
    restarted = PrivacyLifecycleService(Database(db.path), privacy.data_dir, data_encryption_key=b'R'*32)
    restarted.initialize()
    assert restarted.deletion_preview(CHILD)['curriculum_internalization_authorities'] == 0
    assert restarted.deletion_preview(OTHER)['curriculum_internalization_authorities'] == 1
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM audit_event WHERE audit_id='unrelated'").fetchone()[0] == 1
        assert not c.execute('PRAGMA foreign_key_check').fetchall()


@pytest.mark.parametrize('kind',['local','portable'])
def test_restore_quarantines_authorities_before_first_live_publish(authority_home, monkeypatch, kind):
    db, privacy = authority_home
    password = 'synthetic-d7e7cfe8a9df33e910e72a10'
    backup = privacy.create_backup(prune=False) if kind=='local' else privacy.create_portable_backup(password)
    digest = hashlib.sha256(Path(backup['path']).read_bytes()).hexdigest()
    published = []
    initialize = Database.initialize

    def inspect_snapshot(c):
        grants = list(c.execute('SELECT audit_id,child_id FROM audit_event WHERE action=?',(ACTIVE,)))
        for grant in grants:
            assert c.execute("SELECT 1 FROM audit_event WHERE child_id=? AND action=? AND json_extract(summary_json,'$.authority_audit_id')=?",
                             (grant['child_id'],QUARANTINED,grant['audit_id'])).fetchone()
        published.append(len(grants))

    def inspect_first_live(instance):
        if instance.path == db.path:
            with instance.connect() as c:
                inspect_snapshot(c)
        return initialize(instance)

    if kind == 'portable':
        # This DB-only legacy capsule retains its original direct restore path.
        monkeypatch.setattr(Database,'initialize',inspect_first_live)
        restored = privacy.restore_portable_backup(backup['path'],password,'确认迁移恢复')
    else:
        import sqlite3
        from fastapi.testclient import TestClient
        from home_study_agent.app import create_app
        from home_study_agent.config import Settings
        from home_study_agent import restore_publication as publication
        from test_learning_runtime_http import NoCloud
        from privacy_restore_operation import restore_operation
        from synthetic_native_restore_request import KEY, signed_restore_post

        settings = Settings(data_dir=privacy.data_dir, database_path=db.path,
                            data_encryption_key=b'R'*32, maintenance_enabled=False,
                            native_launch_attestation_key=KEY)
        with TestClient(create_app(settings, model_gateway=NoCloud())) as client:
            token = client.post('/v1/parent/setup',json={'pin':'7391'}).json()['parent_token']
            headers = {'X-HSC-Parent':token}
            original_publish = publication.publish_prepared_database

            def inspect_original_publication(owner):
                # Observe the actual prepared database immediately before the
                # unchanged publisher, then the first committed live database.
                journal = publication._decode(publication._read(owner.parent,publication._names(owner.path)[1]))
                prepared = owner.path.parent / journal['binding']['prepared_name']
                with publication._cold(prepared,owner=owner) as c:
                    c.row_factory = sqlite3.Row
                    inspect_snapshot(c)
                result = original_publish(owner)
                with db.connect() as c:
                    inspect_snapshot(c)
                return result

            monkeypatch.setattr(publication,'publish_prepared_database',inspect_original_publication)
            response = signed_restore_post(client,headers=headers,json={
                'backup_id':backup['backup_id'],'confirmation':'确认恢复',
                'operation':restore_operation(client,headers=headers)})
            assert response.status_code == 200,response.text
            restored = response.json()
    assert restored['status']=='restored'
    assert published and all(count == 2 for count in published)
    assert hashlib.sha256(Path(backup['path']).read_bytes()).hexdigest()==digest
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM audit_event WHERE action=?',(ACTIVE,)).fetchone()[0] == 2
        assert c.execute("SELECT COUNT(*) FROM audit_event WHERE action=? AND json_extract(summary_json,'$.authority_audit_id')='grant-a'",(QUARANTINED,)).fetchone()[0] == 1
        assert not c.execute('PRAGMA foreign_key_check').fetchall()
