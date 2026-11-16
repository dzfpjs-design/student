"""Real pre-revision schema, incoming FKs, explicit index and trigger."""
import hashlib
import sqlite3

import pytest

from home_study_agent.database import Database, SCHEMA, LATEST_SCHEMA_VERSION
from home_study_agent.repository import StudyRepository
from home_study_agent.knowledge_source_revision import KnowledgeSourceRevisionService, migrate


def old_database(tmp_path, compact=False, extra_constraint=''):
    path = tmp_path / 'actual-v8.db'
    schema = SCHEMA
    if compact:
        schema = schema.replace('UNIQUE(child_id, content_hash)', 'UNIQUE ( child_id , content_hash )')
    if extra_constraint:
        schema = schema.replace('UNIQUE(child_id, content_hash)', 'UNIQUE(child_id, content_hash), ' + extra_constraint)
    with sqlite3.connect(path) as c:
        c.executescript(schema)
    db = Database(path)
    StudyRepository(db).seed_family()
    with db.connect() as c:
        assert 'current_version' not in {r['name'] for r in c.execute('PRAGMA table_info(knowledge_source)')}
        c.execute("INSERT INTO schema_migration VALUES (70,'2026-09-01T12:00:00+00:00')")
        c.execute('''INSERT INTO knowledge_source(source_id,child_id,source_type,title,subject,grade_band,
            source_ref,license_status,review_status,content_hash,imported_at)
            VALUES ('old','child-primary-4','family_note','Historical source','数学','四年级',
            'isolated:old','private_note','approved',?,'2026-09-01T12:00:00+00:00')''',
            (hashlib.sha256(('x'*1721).encode()).hexdigest(),))
        c.execute("INSERT INTO knowledge_excerpt VALUES ('excerpt','old',1,NULL,?,?)",
                  ('x'*1000, hashlib.sha256(('x'*1000).encode()).hexdigest()))
        c.execute('CREATE INDEX owned_source_title ON knowledge_source(child_id,title)')
        c.execute('CREATE TABLE migration_probe(log TEXT)')
        c.execute("CREATE TRIGGER owned_source_update AFTER UPDATE ON knowledge_source BEGIN INSERT INTO migration_probe VALUES ('source updated'); END")
        c.commit()
    return db


def test_unknown_old_check_constraint_is_rejected_without_any_write(tmp_path):
    db = old_database(tmp_path, extra_constraint="CHECK (length(title) > 0)")
    with db.connect() as c:
        before = '\n'.join(c.iterdump())
        changes = c.total_changes
        with pytest.raises(RuntimeError, match='UNSUPPORTED_SCHEMA_CONSTRAINT'):
            migrate(c)
        assert c.total_changes == changes
        assert '\n'.join(c.iterdump()) == before
        assert c.execute('PRAGMA foreign_keys').fetchone()[0] == 1


@pytest.mark.parametrize('compact', [False, True])
def test_actual_v8_initialize_upgrade_retains_fk_index_trigger_restart(tmp_path, compact):
    db = old_database(tmp_path, compact)
    db.initialize()
    Database(db.path).initialize()
    with db.connect() as c:
        assert c.execute('SELECT source_id FROM knowledge_excerpt').fetchone()[0] == 'old'
        assert not c.execute('PRAGMA foreign_key_check').fetchall()
        assert c.execute("SELECT COUNT(*) FROM sqlite_master WHERE name IN ('owned_source_title','owned_source_update')").fetchone()[0] == 2
        assert c.execute('SELECT COUNT(*) FROM migration_probe').fetchone()[0] == 0
        c.execute("UPDATE knowledge_source SET title='checked' WHERE source_id='old'")
        assert c.execute('SELECT COUNT(*) FROM migration_probe').fetchone()[0] == 1
        c.rollback()


def test_fault_after_old_table_drop_rolls_back_old_unique_children_and_fk_mode(tmp_path, monkeypatch):
    db = old_database(tmp_path, True)
    import home_study_agent.knowledge_source_revision as module
    def fail(c):
        raise sqlite3.OperationalError('isolated migration fault')
    monkeypatch.setattr(module, 'ensure_schema', fail)
    with db.connect() as c:
        before = '\n'.join(c.iterdump())
        with pytest.raises(sqlite3.OperationalError, match='isolated migration fault'):
            migrate(c)
        assert '\n'.join(c.iterdump()) == before
        assert c.execute('PRAGMA foreign_keys').fetchone()[0] == 1
        assert not c.execute('PRAGMA foreign_key_check').fetchall()


def test_initialize_failure_restores_actual_v70_upgrade_snapshot_then_retry(tmp_path, monkeypatch):
    import home_study_agent.knowledge_source_revision as module
    db = old_database(tmp_path, True)
    ensure = module.ensure_schema
    def fail(c):
        raise sqlite3.OperationalError('isolated initialize fault')
    monkeypatch.setattr(module, 'ensure_schema', fail)
    with pytest.raises(sqlite3.OperationalError, match='isolated initialize fault'):
        db.initialize()
    with db.connect() as c:
        assert 'current_version' not in {r['name'] for r in c.execute('PRAGMA table_info(knowledge_source)')}
        assert c.execute('SELECT MAX(version) FROM schema_migration').fetchone()[0] == 70
        assert c.execute('SELECT content_text FROM knowledge_excerpt').fetchone()[0] == 'x'*1000
        assert c.execute('SELECT COUNT(*) FROM migration_probe').fetchone()[0] == 0
    assert list((tmp_path/'upgrade-backups').glob('*.db'))
    monkeypatch.setattr(module, 'ensure_schema', ensure)
    db.initialize()
    with db.connect() as c:
        assert c.execute('SELECT MAX(version) FROM schema_migration').fetchone()[0] == LATEST_SCHEMA_VERSION
        assert c.execute('SELECT COUNT(*) FROM schema_migration WHERE version=71').fetchone()[0] == 1
        assert not c.execute('PRAGMA foreign_key_check').fetchall()


@pytest.mark.parametrize('portable', [False, True])
def test_revision_actual_export_delete_and_backup_restore(tmp_path, portable):
    from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
    db = old_database(tmp_path)
    db.initialize()
    privacy = PrivacyLifecycleService(db, tmp_path, b'R'*32)
    privacy.initialize()
    service = KnowledgeSourceRevisionService(db)
    preview = service.preview('child-primary-4', 'old', 'x'*1721)
    body = dict(content_text='x'*1721, expected_version=preview['source_version'], client_action_id='revise')
    result = service.revise('child-primary-4', 'old', **body)
    import json
    from pathlib import Path
    exported = privacy.export_child('child-primary-4')
    data = json.loads(Path(exported['path']).read_text())
    assert len(data['knowledge_sources']) == 2
    assert len(data['knowledge_source_revisions']) == 1
    password = 'synthetic-6527540121e3d978215126ee'
    backup = privacy.create_portable_backup(password) if portable else privacy.create_backup()
    # Mutate only the synthetic current title, never the immutable historical excerpt.
    with db.connect() as c:
        c.execute('UPDATE knowledge_source SET title=? WHERE source_id=?', ('After backup', result['current']['source_id']))
        c.commit()
    if portable:
        privacy.restore_portable_backup(backup['path'], password, '确认迁移恢复')
    else:
        _restore_local_through_original_http(tmp_path, db, b'R'*32, backup['backup_id'])
    restarted = KnowledgeSourceRevisionService(Database(db.path))
    repeated = restarted.revise('child-primary-4', 'old', **body)
    assert repeated['receipt'] == result['receipt']
    assert repeated['current']['title'] == 'Historical source'
    assert repeated['current']['review_status'] == 'quarantined'
    deletion = privacy.delete_child_scope('child-primary-4', 'knowledge', '确认删除', 'delete-revisions')
    assert deletion['status'] == 'completed', deletion
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM knowledge_source_revision_event').fetchone()[0] == 0
        assert not c.execute('PRAGMA foreign_key_check').fetchall()


@pytest.mark.parametrize('portable', [False, True])
def test_actual_legacy_backup_is_upgraded_inside_restore_staging(tmp_path, portable):
    from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
    db = old_database(tmp_path, True)
    privacy = PrivacyLifecycleService(db, tmp_path, b'S'*32)
    privacy.initialize()
    password = 'synthetic-180870b257aa71740256570a'
    backup = privacy.create_portable_backup(password) if portable else privacy.create_backup()
    with db.connect() as c:
        assert 'current_version' not in {r['name'] for r in c.execute('PRAGMA table_info(knowledge_source)')}
    db.initialize()
    if portable:
        privacy.restore_portable_backup(backup['path'], password, '确认迁移恢复')
    else:
        _restore_local_through_original_http(tmp_path, db, b'S'*32, backup['backup_id'])
    Database(db.path).initialize()
    with db.connect() as c:
        assert c.execute('SELECT current_version FROM knowledge_source').fetchone()[0] == 1
        assert c.execute('SELECT content_text FROM knowledge_excerpt').fetchone()[0] == 'x'*1000
        assert not c.execute('PRAGMA foreign_key_check').fetchall()
    preview = KnowledgeSourceRevisionService(db).preview('child-primary-4', 'old', 'x'*1721)
    assert preview['status'] == 'incomplete'


@pytest.mark.parametrize('scope', ['knowledge', 'all_history', 'learning'])
def test_revision_scope_cleanup_and_other_child_retention(tmp_path, scope):
    from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
    from home_study_agent.knowledge_base import KnowledgeBaseService
    db = old_database(tmp_path)
    db.initialize()
    service = KnowledgeSourceRevisionService(db)
    p = service.preview('child-primary-4', 'old', 'x'*1721)
    service.revise('child-primary-4', 'old', content_text='x'*1721, expected_version=p['source_version'], client_action_id='scope-repair')
    other = KnowledgeBaseService(db).import_source('child-junior-3', dict(source_type='family_note', title='Other child original',
        subject='数学', grade_band='初三', content_text='x'*1721, license_status='private_note', parent_reviewed=True))
    privacy = PrivacyLifecycleService(db, tmp_path, b'P'*32)
    privacy.initialize()
    assert privacy.deletion_preview('child-primary-4')['knowledge_source_revisions'] == 1
    result = privacy.delete_child_scope('child-primary-4', scope, '确认删除', 'scope-delete')
    assert result['status'] == 'completed', result
    assert KnowledgeBaseService(db).get_source(other['source_id']) == other
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM knowledge_source_revision_event WHERE child_id='child-primary-4'").fetchone()[0] == int(scope == 'learning')
        assert not c.execute('PRAGMA foreign_key_check').fetchall()


def test_deleting_old_source_preserves_new_source_and_its_review_history(tmp_path):
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    from test_knowledge_source_revision_http import parent
    db = old_database(tmp_path)
    db.initialize()
    service = KnowledgeSourceRevisionService(db)
    p = service.preview('child-primary-4', 'old', 'x'*1721)
    result = service.revise('child-primary-4', 'old', content_text='x'*1721, expected_version=p['source_version'], client_action_id='repair-before-delete')
    with TestClient(create_app(Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False)),
                    client=('127.0.0.1', 53001)) as client:
        response = client.post('/v1/parent/children/child-primary-4/knowledge-sources/old/delete',
            headers=parent(client, True), json={'confirmation': '确认删除', 'client_action_id': 'delete-only-old'})
        assert response.status_code == 200, response.text
        assert response.json()['status'] == 'completed', response.text
    assert service.get('child-primary-4', result['current']['source_id'])['current']['review_status'] == 'quarantined'
    with db.connect() as c:
        assert c.execute('SELECT old_source_id FROM knowledge_source_revision_event').fetchone()[0] is None
        assert not c.execute('PRAGMA foreign_key_check').fetchall()


def _restore_local_through_original_http(tmp_path, db, encryption_key, backup_id):
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    from test_knowledge_source_revision_http import parent
    from privacy_restore_operation import restore_operation
    from synthetic_native_restore_request import KEY, signed_restore_post

    settings = Settings(data_dir=tmp_path, database_path=db.path,
                        maintenance_enabled=False, data_encryption_key=encryption_key,
                        native_launch_attestation_key=KEY)
    with TestClient(create_app(settings), client=('127.0.0.1', 53002)) as client:
        headers = parent(client, True)
        body = {'backup_id': backup_id, 'confirmation': '确认恢复',
                'operation': restore_operation(client, headers=headers)}
        response = signed_restore_post(client, headers=headers, json=body)
        assert response.status_code == 200, response.text


@pytest.mark.parametrize('ledger',['partial','malformed_empty','adopted','valid_empty','legacy_extra_authority'])
def test_no_kg_export_requires_matching_complete_empty_restore_state(tmp_path,monkeypatch,ledger):
    from home_study_agent.privacy_lifecycle import PrivacyLifecycleService,InvalidBackupError
    from home_study_agent import parent_restore_assets as assets
    from home_study_agent import standard_relocation_provenance as provenance
    from home_study_agent import registered_fingerprint_relocation as relocation
    db=old_database(tmp_path,True);privacy=PrivacyLifecycleService(db,tmp_path,b'Q'*32);privacy.initialize()
    with db.connect()as c:
        if ledger=='partial':c.execute('CREATE TABLE restore_asset_generation(state TEXT)')
        elif ledger=='malformed_empty':
            c.execute('CREATE TABLE restore_asset_generation(state TEXT)')
            c.execute('CREATE TABLE restore_asset_member(state TEXT)')
        else:
            for _,_,sql in assets.LEGACY_OBJECTS_87:c.execute(sql)
            if ledger=='legacy_extra_authority':c.execute('CREATE TABLE restore_parent_authority(unknown TEXT)')
            if ledger=='adopted':
                identifier='a'*32
                c.execute('INSERT INTO restore_asset_generation VALUES('+','.join('?'for _ in range(13))+')',
                  (identifier,'synthetic-request','synthetic-nonce','b'*64,'c'*64,
                   'restore-generation-'+identifier,'[1,2]','[3,4]','d'*64,'e'*64,0,0,'ADOPTED'))
        c.commit()
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE lower(name)='kg_corpus'").fetchone()
        schema=[tuple(row)for row in c.execute('SELECT type,name,sql FROM sqlite_master ORDER BY type,name')]
        original_count=c.execute('SELECT count(*) FROM knowledge_source').fetchone()[0]
    # Observe only boolean guard outcomes, retaining the original decision.
    original_need=relocation.need;guards=[]
    def observe(value,code):
        if code==relocation.CODE:guards.append(bool(value))
        return original_need(value,code)
    monkeypatch.setattr(relocation,'need',observe)
    tickets=len(provenance._EXPORTS)
    if ledger=='valid_empty':assert privacy.create_backup()['backup_id']
    else:
        with pytest.raises(InvalidBackupError):privacy.create_backup()
    assert guards and guards[0]is True and len(provenance._EXPORTS)==tickets
    with db.connect()as c:
        assert [tuple(row)for row in c.execute('SELECT type,name,sql FROM sqlite_master ORDER BY type,name')]==schema
        assert c.execute('SELECT count(*) FROM knowledge_source').fetchone()[0]==original_count
        assert 'current_version'not in {row['name']for row in c.execute('PRAGMA table_info(knowledge_source)')}
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE lower(name)='kg_corpus'").fetchone()
