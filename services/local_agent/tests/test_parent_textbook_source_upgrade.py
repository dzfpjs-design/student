"""v81/v33 upgrades protect complete camera rows, media pointers and events."""
import base64
import json
import sqlite3

import pytest

from home_study_agent.database import Database, LATEST_SCHEMA_VERSION
from home_study_agent.knowledge_graph import schema
from home_study_agent.knowledge_graph.parent_textbook_capture import ParentTextbookCaptureService
from test_parent_textbook_capture import setup
from test_parent_textbook_derivation import page, RecordingAdapter


OLD_ARTIFACT = '''CREATE TABLE kg_parent_textbook_ocr_artifact (
 artifact_id TEXT PRIMARY KEY,child_id TEXT NOT NULL REFERENCES child_profile(child_id),
 lease_id TEXT NOT NULL REFERENCES kg_parent_textbook_capture_lease(lease_id),
 page_context_json TEXT NOT NULL,extracted_text TEXT NOT NULL,confidence REAL NOT NULL,
 details_json TEXT NOT NULL,image_sha256 TEXT NOT NULL DEFAULT '',page_context_sha256 TEXT NOT NULL DEFAULT '',
 raw_image_path TEXT,raw_image_media_type TEXT,raw_image_byte_length INTEGER NOT NULL DEFAULT 0,
 raw_image_encryption_version TEXT,status TEXT NOT NULL CHECK(status IN ('processing','needs_review','ready','importing','imported','skipped','failed')),
 import_action_id TEXT,import_payload_sha256 TEXT,import_request_json TEXT,import_claimed_at TEXT,
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL,client_action_id TEXT NOT NULL UNIQUE,import_owner_boot_id TEXT)'''


def legacy_database(tmp_path):
    # Project the old OCR component from verified pre-authority v84 SQL.
    from historical_database_fixture import seed_empty_historical_v84
    db = Database(tmp_path/'parent.db')
    seed_empty_historical_v84(db.path)
    setup(db, initialize=False)
    service = ParentTextbookCaptureService(db,RecordingAdapter())
    lease = service.acquire('child-4','bound-document-hash',{})['lease_id']
    raw = page()
    artifact = service.recognize('child-4',lease,base64.b64encode(raw).decode(),{'title':'Original'},'old-camera-original')
    service.review('child-4',artifact['capture_ref'],'Words: apple','old-camera-review')
    service.reserve_page_import('child-4',artifact['capture_ref'],'old-camera-import',{'title':'Original'})
    with db.connect() as c:
        fields = [r[1] for r in c.execute('PRAGMA table_info(kg_parent_textbook_ocr_artifact)') if r[1] not in {'source_kind','file_source_id'}]
        rows = [tuple(r) for r in c.execute('SELECT '+','.join(fields)+' FROM kg_parent_textbook_ocr_artifact')]
        events = [tuple(r) for r in c.execute('SELECT * FROM kg_parent_textbook_ocr_review_event')]
        event_sql = c.execute("SELECT sql FROM sqlite_master WHERE name='kg_parent_textbook_ocr_review_event'").fetchone()[0]
        c.execute('DROP TABLE kg_parent_textbook_ocr_review_event')
        c.execute('DROP TABLE kg_parent_textbook_ocr_artifact')
        c.execute(OLD_ARTIFACT)
        c.executemany('INSERT INTO kg_parent_textbook_ocr_artifact('+','.join(fields)+') VALUES('+','.join('?' for _ in fields)+')',rows)
        c.execute(event_sql)
        c.executemany('INSERT INTO kg_parent_textbook_ocr_review_event VALUES(?,?,?,?,?,?,?)',events)
        c.execute('DELETE FROM schema_migration WHERE version>81')
        c.execute('DELETE FROM kg_schema_migration WHERE version>33'); c.commit()
    return db, service, artifact, raw, fields, rows, events


def assert_original(db, fields, rows, events):
    with db.connect() as c:
        assert [tuple(r) for r in c.execute('SELECT '+','.join(fields)+' FROM kg_parent_textbook_ocr_artifact')] == rows
        assert [tuple(r) for r in c.execute('SELECT * FROM kg_parent_textbook_ocr_review_event')] == events
        assert c.execute('PRAGMA foreign_key_check').fetchall() == []


def test_upgrade_preserves_every_column_import_owner_event_and_encrypted_original(tmp_path):
    db, service, artifact, raw, fields, rows, events = legacy_database(tmp_path)
    db.initialize(); db.initialize()
    assert_original(db,fields,rows,events)
    assert service.source_image('child-4',artifact['capture_ref'])[0] == raw
    assert db.latest_upgrade_status()['status'] == 'completed'
    with db.connect() as c:
        row = c.execute('SELECT * FROM kg_parent_textbook_ocr_artifact').fetchone()
        assert row['source_kind'] == 'camera' and row['file_source_id'] is None
        assert c.execute('SELECT MAX(version) FROM schema_migration').fetchone()[0] == LATEST_SCHEMA_VERSION
        assert c.execute('SELECT MAX(version) FROM kg_schema_migration').fetchone()[0] == schema.KG_SCHEMA_VERSION
    snapshot = next((tmp_path/'upgrade-backups').glob('*.db'))
    with sqlite3.connect(snapshot) as c:
        assert c.execute('SELECT MAX(version) FROM schema_migration').fetchone()[0] == 81
        assert c.execute('SELECT raw_image_path,import_owner_boot_id FROM kg_parent_textbook_ocr_artifact').fetchone() == (rows[0][fields.index('raw_image_path')],rows[0][fields.index('import_owner_boot_id')])


@pytest.mark.parametrize('phase',['rename','create','copy','drop'])
def test_interrupted_upgrade_restores_old_version_then_restarts_without_data_loss(tmp_path,monkeypatch,phase):
    db, service, artifact, raw, fields, rows, events = legacy_database(tmp_path)
    monkeypatch.setattr(schema,'_PARENT_SOURCE_MIGRATION_FAULT_AFTER',phase)
    with pytest.raises(sqlite3.OperationalError,match='injected parent source'):
        db.initialize()
    assert_original(db,fields,rows,events)
    assert db.latest_upgrade_status()['status'] == 'restored_after_failure'
    with db.connect() as c:
        assert 'file_source_id' not in {r[1] for r in c.execute('PRAGMA table_info(kg_parent_textbook_ocr_artifact)')}
        assert c.execute('SELECT MAX(version) FROM schema_migration').fetchone()[0] == 81
    monkeypatch.setattr(schema,'_PARENT_SOURCE_MIGRATION_FAULT_AFTER',None)
    db.initialize(); assert_original(db,fields,rows,events)
    assert service.source_image('child-4',artifact['capture_ref'])[0] == raw
