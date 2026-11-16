import sqlite3

import pytest

from home_study_agent.database import Database, LATEST_SCHEMA_VERSION
from historical_managed_fixture import seed_pre_managed_v84_core


TABLES = {
    'ocr_job', 'ocr_cache', 'ocr_cache_epoch', 'ocr_request_tombstone',
    'ocr_restore_fence', 'ocr_legacy_reference', 'ocr_review_item',
    'ocr_review_event', 'ocr_review_batch', 'kg_companion_graph_meta',
    'kg_companion_graph_context_event', 'kg_local_course_context_event',
}


def legacy_database(tmp_path):
    database = Database(tmp_path / 'synthetic.db')
    seed_pre_managed_v84_core(database.path)
    with database.connect() as c:
        for table in TABLES:
            c.execute(f'DROP TABLE IF EXISTS {table}')
        c.execute('DELETE FROM schema_migration WHERE version>82')
        c.execute('''CREATE TABLE ocr_job(job_id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
            image_hash TEXT NOT NULL, preferred_route TEXT NOT NULL, status TEXT NOT NULL,
            created_at TEXT NOT NULL, started_at TEXT, completed_at TEXT, result_json TEXT, error_code TEXT)''')
        c.execute("INSERT INTO ocr_job VALUES('old-job','old-request','synthetic-hash','cpu','completed','synthetic',NULL,NULL,'{\"text\":\"keep raw\"}',NULL)")
        c.execute('CREATE TABLE ocr_cache(image_hash TEXT PRIMARY KEY,result_json TEXT,created_at TEXT)')
        c.execute("INSERT INTO ocr_cache VALUES('synthetic-hash','{\"text\":\"keep old cache\"}','synthetic')")
        c.execute('''CREATE TABLE ocr_review_event(event_id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL,
            review_id TEXT, event_type TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL)''')
        c.execute("INSERT INTO ocr_review_event VALUES('old-event','synthetic-artifact',NULL,'created','{}','synthetic')")
        c.execute('CREATE TABLE kg_companion_graph_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL,created_at TEXT NOT NULL)')
        c.execute("INSERT INTO kg_companion_graph_meta VALUES('privacy_salt','synthetic-preserved-salt','synthetic')")
        c.commit()
    return database


def assert_old_state(database):
    with database.connect() as c:
        assert c.execute('SELECT MAX(version) FROM schema_migration').fetchone()[0] == 82
        assert c.execute('SELECT status,result_json FROM ocr_job').fetchone()[:] == ('completed', '{"text":"keep raw"}')
        assert 'owner_key' not in {r[1] for r in c.execute('PRAGMA table_info(ocr_job)')}
        assert c.execute('SELECT result_json FROM ocr_cache').fetchone()[0] == '{"text":"keep old cache"}'
        assert 'client_action_id' not in {r[1] for r in c.execute('PRAGMA table_info(ocr_review_event)')}


def test_new_database_contains_managed_schema_before_service_construction(tmp_path):
    database = Database(tmp_path / 'synthetic.db')
    database.initialize()
    assert LATEST_SCHEMA_VERSION >= 84
    with database.connect() as c:
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert TABLES <= tables
        assert c.execute('SELECT MAX(version) FROM schema_migration').fetchone()[0] == LATEST_SCHEMA_VERSION


def test_old_shared_ocr_and_context_are_migrated_under_core_backup(tmp_path):
    database = legacy_database(tmp_path)
    database.initialize()
    status = database.latest_upgrade_status()
    assert status and status['source_schema_version'] == 82 and status['target_schema_version'] == LATEST_SCHEMA_VERSION
    with database.connect() as c:
        assert c.execute('SELECT status,error_code,owner_key FROM ocr_job').fetchone()[:] == (
            'quarantined', 'OCR_LEGACY_UNOWNED', 'isolated')
        assert c.execute('SELECT result_json FROM ocr_cache_legacy_quarantine').fetchone()[0] == '{"text":"keep old cache"}'
        assert c.execute('SELECT COUNT(*) FROM ocr_cache').fetchone()[0] == 0
        assert c.execute('SELECT event_id,client_action_id FROM ocr_review_event').fetchone()[:] == ('old-event', None)
        salt = c.execute("SELECT value FROM kg_companion_graph_meta WHERE key='privacy_salt'").fetchone()[0]
        epoch = c.execute('SELECT epoch FROM ocr_cache_epoch').fetchone()[0]
    before = sorted((tmp_path / 'upgrade-backups').glob('pre-upgrade-*.json'))
    database.initialize()
    assert sorted((tmp_path / 'upgrade-backups').glob('pre-upgrade-*.json')) == before
    with database.connect() as c:
        assert c.execute("SELECT value FROM kg_companion_graph_meta WHERE key='privacy_salt'").fetchone()[0] == salt == 'synthetic-preserved-salt'
        assert c.execute('SELECT epoch FROM ocr_cache_epoch').fetchone()[0] == epoch


def test_committed_ocr_migration_failure_restores_original_and_can_retry(tmp_path, monkeypatch):
    from home_study_agent.local_ocr import cache_lifecycle
    database = legacy_database(tmp_path)
    ensure = cache_lifecycle.ensure_schema
    def fail_after_commit(c):
        ensure(c)
        c.commit()
        raise RuntimeError('synthetic local schema failure')
    monkeypatch.setattr(cache_lifecycle, 'ensure_schema', fail_after_commit)
    with pytest.raises(RuntimeError, match='synthetic local schema failure'):
        database.initialize()
    assert_old_state(database)
    monkeypatch.setattr(cache_lifecycle, 'ensure_schema', ensure)
    database.initialize()
    with database.connect() as c:
        assert c.execute('SELECT MAX(version) FROM schema_migration').fetchone()[0] == LATEST_SCHEMA_VERSION
        assert c.execute('SELECT result_json FROM ocr_cache_legacy_quarantine').fetchone()[0] == '{"text":"keep old cache"}'


def test_latest_version_marker_does_not_skip_backup_for_incomplete_local_schema(tmp_path):
    database = legacy_database(tmp_path)
    with database.connect() as c:
        c.execute("INSERT INTO schema_migration VALUES(84,'synthetic incomplete schema')")
        c.commit()
    database.initialize()
    status = database.latest_upgrade_status()
    assert status and status['source_schema_version'] == 84 and status['target_schema_version'] == LATEST_SCHEMA_VERSION
    with database.connect() as c:
        assert c.execute('SELECT result_json FROM ocr_cache_legacy_quarantine').fetchone()[0] == '{"text":"keep old cache"}'


@pytest.mark.parametrize('component', ['review', 'context'])
def test_local_schema_helpers_do_not_implicitly_commit(tmp_path, component):
    from home_study_agent.local_ocr.review_schema import ensure_schema as review
    from home_study_agent.knowledge_graph.context_schema import ensure_schema as context
    c = sqlite3.connect(tmp_path / 'synthetic-transaction.db')
    try:
        c.execute('BEGIN')
        c.execute('CREATE TABLE caller_sentinel(value TEXT)')
        c.execute("INSERT INTO caller_sentinel VALUES('uncommitted')")
        (review if component == 'review' else context)(c)
        assert c.in_transaction
        c.rollback()
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='caller_sentinel'").fetchone()
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name IN ('ocr_review_event','kg_companion_graph_meta')").fetchone()
    finally:
        c.close()


def test_current_latest_marker_still_backs_up_incomplete_managed_component(tmp_path):
    database = Database(tmp_path / 'current-latest.db')
    database.initialize()
    with database.connect() as c:
        c.execute('DROP TABLE kg_local_course_context_event')
        c.commit()
    database.initialize()
    status = database.latest_upgrade_status()
    assert status and status['source_schema_version'] == status['target_schema_version'] == LATEST_SCHEMA_VERSION
    with database.connect() as c:
        assert c.execute("SELECT 1 FROM sqlite_master WHERE name='kg_local_course_context_event'").fetchone()
        assert not c.execute('PRAGMA foreign_key_check').fetchall()
