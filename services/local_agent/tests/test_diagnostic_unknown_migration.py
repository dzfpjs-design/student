"""Real legacy SQLite parent/child references and upgrade recovery, no family DB."""
import sqlite3
from contextlib import closing

import pytest

from home_study_agent.database import Database
from home_study_agent.diagnostic_score_migration import CREATE_ATTEMPT, COLUMNS
from home_study_agent.learning_state import LearningStateService
from home_study_agent.repository import StudyRepository
from test_open_response_supply_http import CHILD, prepared, publish_open


def legacy_database(tmp_path):
    from historical_diagnostic_fixture import seed_public_pre70_business
    path = tmp_path.resolve() / 'pre70-core67.db'
    seed_public_pre70_business(path)
    db = Database(path)
    objective = {'attempt_id': 'known-objective-attempt',
                 'evidence_id': 'known-objective-evidence', 'correctness': 0}
    with db.connect() as c:
        item = c.execute("SELECT diagnostic_item_id FROM diagnostic_item WHERE answer_type='open_response' AND source_ref LIKE 'parent_approved_specialized:%'").fetchone()[0]
        c.execute("""INSERT INTO daily_session(session_id,child_id,status,state,started_at,last_checkpoint_at,client_action_id)
            VALUES ('legacy-session',?,'active','study','2026-09-01','2026-09-01','legacy-start')""", (CHILD,))
        c.execute("""INSERT INTO learning_evidence VALUES
            ('known-objective-evidence',?,'legacy-session','node-j3-math-discriminant',
             'diagnostic_answer',0,0,1,1,'Known original objective response','2026-09-01','objective')""", (CHILD,))
        c.execute("""INSERT INTO diagnostic_attempt VALUES
            ('known-objective-attempt','diag-j3-discriminant-1','known-objective-evidence',?,
             'legacy-session','不会',0,'Known original numeric score',NULL,'2026-09-01','objective')""", (CHILD,))
        c.execute("""INSERT INTO learning_evidence SELECT 'old-open-evidence',child_id,session_id,knowledge_node_id,
            evidence_type,0,0,0,.35,'unreliable_scoring',occurred_at,'old-open' FROM learning_evidence WHERE evidence_id=?""",
            (objective['evidence_id'],))
        c.execute("""INSERT INTO diagnostic_attempt SELECT 'old-open-attempt',?,'old-open-evidence',child_id,session_id,
            'Unassessed original response',0,'Needs review',NULL,created_at,'old-open' FROM diagnostic_attempt WHERE attempt_id=?""",
            (item,objective['attempt_id']))
        c.execute('CREATE TABLE fixture_child (id TEXT PRIMARY KEY, attempt_id TEXT REFERENCES diagnostic_attempt(attempt_id) ON DELETE CASCADE)')
        c.execute("INSERT INTO fixture_child VALUES ('bound','old-open-attempt')")
        c.execute('CREATE INDEX fixture_attempt_session ON diagnostic_attempt(session_id)')
        c.execute('CREATE TABLE fixture_update_log (attempt_id TEXT)')
        c.execute('CREATE TRIGGER fixture_attempt_updated AFTER UPDATE ON diagnostic_attempt BEGIN INSERT INTO fixture_update_log VALUES (NEW.attempt_id); END')
        c.execute('CREATE VIEW fixture_attempt_view AS SELECT attempt_id,correctness FROM diagnostic_attempt')
        assert c.execute('SELECT MAX(version) FROM schema_migration').fetchone()[0] == 67
        assert not c.execute("SELECT name FROM sqlite_master WHERE name LIKE 'restore_asset_%' OR name='restore_parent_authority'").fetchall()
        c.commit()
    return db, objective


def current88_falsely_marked69(tmp_path):
    db,service,spec,_,_ = prepared(tmp_path,'数学')
    item = publish_open(service,spec)['diagnostic_item_id']
    sid = StudyRepository(db).start_session(CHILD,'start')['session_id']
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?",(sid,))
        c.commit()
    objective = LearningStateService(db).submit_diagnostic(sid,spec['seed_item_id'],'不会','objective')
    with db.connect() as c:
        c.execute('''INSERT INTO learning_evidence SELECT 'old-open-evidence',child_id,session_id,knowledge_node_id,
            evidence_type,0,0,0,.35,'unreliable_scoring',occurred_at,'old-open' FROM learning_evidence WHERE evidence_id=?''',
            (objective['evidence_id'],))
        c.execute('''INSERT INTO diagnostic_attempt SELECT 'old-open-attempt',?,'old-open-evidence',child_id,session_id,
            'Unassessed original response',0,'Needs review',NULL,created_at,'old-open' FROM diagnostic_attempt WHERE attempt_id=?''',
            (item,objective['attempt_id']))
        c.commit()
    with sqlite3.connect(db.path) as c:
        # Build a real88 predecessor before applying the intentional69 lie.
        # Future receipt history must never be erased to fake a downgrade.
        from home_study_agent.pet_multipa_execution_receipt import OBJECTS, validate_schema as validate_receipts
        from home_study_agent.pet_speaking_candidate_store import TABLES
        validate_receipts(c)
        for kind, name, _ in reversed(OBJECTS):
            if kind == 'table':
                assert c.execute('SELECT count(*)FROM '+name).fetchone()[0] == 0
            c.execute('DROP '+kind.upper()+' '+name)
        for name in ('pet_speaking_candidate_job', *reversed(TABLES)):
            assert c.execute('SELECT count(*)FROM '+name).fetchone()[0] == 0
            c.execute('DROP TABLE '+name)
        c.execute('DELETE FROM schema_migration WHERE version>=89')
        assert c.execute('SELECT MAX(version)FROM schema_migration').fetchone()[0] == 88
        c.commit()
        c.execute('PRAGMA foreign_keys=OFF')
        c.execute('PRAGMA legacy_alter_table=ON')
        c.execute('BEGIN IMMEDIATE')
        c.execute(CREATE_ATTEMPT.replace('correctness REAL,','correctness REAL NOT NULL,'))
        names = ','.join(COLUMNS)
        c.execute('INSERT INTO diagnostic_attempt_unknown_v70('+names+') SELECT '+names+' FROM diagnostic_attempt')
        c.execute('DROP TABLE diagnostic_attempt')
        c.execute('ALTER TABLE diagnostic_attempt_unknown_v70 RENAME TO diagnostic_attempt')
        c.execute('DELETE FROM schema_migration WHERE version>=70')
        assert c.execute('SELECT MAX(version) FROM schema_migration').fetchone()[0] == 69
        c.execute('CREATE TABLE fixture_child (id TEXT PRIMARY KEY, attempt_id TEXT REFERENCES diagnostic_attempt(attempt_id) ON DELETE CASCADE)')
        c.execute("INSERT INTO fixture_child VALUES ('bound','old-open-attempt')")
        c.execute('CREATE INDEX fixture_attempt_session ON diagnostic_attempt(session_id)')
        c.execute('CREATE TABLE fixture_update_log (attempt_id TEXT)')
        c.execute('CREATE TRIGGER fixture_attempt_updated AFTER UPDATE ON diagnostic_attempt BEGIN INSERT INTO fixture_update_log VALUES (NEW.attempt_id); END')
        c.execute('CREATE VIEW fixture_attempt_view AS SELECT attempt_id,correctness FROM diagnostic_attempt')
        c.commit()
    return db,objective


def assert_migrated(db,objective):
    with db.connect() as c:
        assert c.execute('PRAGMA foreign_keys').fetchone()[0]==1
        assert c.execute('PRAGMA foreign_key_check').fetchall()==[]
        assert not next(row for row in c.execute('PRAGMA table_info(diagnostic_attempt)') if row['name']=='correctness')['notnull']
        assert c.execute("SELECT correctness FROM diagnostic_attempt WHERE attempt_id='old-open-attempt'").fetchone()[0] is None
        assert c.execute("SELECT correctness FROM learning_evidence WHERE evidence_id='old-open-evidence'").fetchone()[0] is None
        assert c.execute('SELECT correctness FROM diagnostic_attempt WHERE attempt_id=?',(objective['attempt_id'],)).fetchone()[0]==objective['correctness']
        assert c.execute('SELECT COUNT(*) FROM fixture_child').fetchone()[0]==1
        assert c.execute("SELECT COUNT(*) FROM fixture_attempt_view WHERE correctness IS NULL").fetchone()[0]==1
        assert c.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='fixture_attempt_session'").fetchone()
        assert c.execute('SELECT COUNT(*) FROM fixture_update_log').fetchone()[0]==1
        c.execute('UPDATE diagnostic_attempt SET correctness=0 WHERE attempt_id=?',(objective['attempt_id'],))
        assert c.execute('SELECT COUNT(*) FROM fixture_update_log').fetchone()[0]==2
        c.rollback()


def test_upgrade_preserves_fk_views_indexes_triggers_and_legacy_numeric_writes(tmp_path):
    db,objective = legacy_database(tmp_path)
    db.initialize()
    assert_migrated(db,objective)
    db.initialize()
    assert_migrated(db,objective)
    assert db.latest_upgrade_status()['source_schema_version']==67


def test_failed_upgrade_restores_original_schema_and_all_child_links(tmp_path,monkeypatch):
    db,objective = legacy_database(tmp_path)
    import home_study_agent.diagnostic_score_migration as migration
    original = migration._clear_verified_open_placeholders
    def crash(connection):
        raise RuntimeError('isolated failure after table rebuild')
    monkeypatch.setattr(migration,'_clear_verified_open_placeholders',crash)
    with pytest.raises(RuntimeError,match='isolated failure'):
        db.initialize()
    with db.connect() as c:
        assert next(row for row in c.execute('PRAGMA table_info(diagnostic_attempt)') if row['name']=='correctness')['notnull']==1
        assert c.execute("SELECT correctness FROM diagnostic_attempt WHERE attempt_id='old-open-attempt'").fetchone()[0]==0
        assert c.execute('SELECT COUNT(*) FROM fixture_child').fetchone()[0]==1
        assert c.execute('PRAGMA foreign_key_check').fetchall()==[]
    assert db.latest_upgrade_status()['status']=='restored_after_failure'
    monkeypatch.setattr(migration,'_clear_verified_open_placeholders',original)
    db.initialize()
    assert_migrated(db,objective)


def test_restored_legacy_snapshot_migrates_again_without_second_attempt(tmp_path):
    db,objective = legacy_database(tmp_path)
    saved = tmp_path/'isolated-pre70-v67-snapshot.db'
    with closing(sqlite3.connect(db.path)) as c,closing(sqlite3.connect(saved)) as backup:
        c.backup(backup)
    db.initialize()
    restored = Database(tmp_path/'restored.db')
    with closing(sqlite3.connect(saved)) as backup,closing(sqlite3.connect(restored.path)) as c:
        backup.backup(c)
        # Match privacy_lifecycle's isolated restore staging, not a raw WAL copy.
        c.execute('PRAGMA journal_mode=DELETE')
    restored.initialize()
    assert_migrated(restored,objective)
    with restored.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==2


def test_unknown_legacy_column_is_not_silently_lost(tmp_path):
    db,_ = legacy_database(tmp_path)
    with db.connect() as c:
        c.execute('ALTER TABLE diagnostic_attempt ADD COLUMN fixture_extension TEXT')
        c.execute("UPDATE diagnostic_attempt SET fixture_extension='must preserve'")
        c.commit()
    with pytest.raises(sqlite3.OperationalError,match='DIAGNOSTIC_MIGRATION_SCHEMA_CHANGED'):
        db.initialize()
    with db.connect() as c:
        assert c.execute('SELECT DISTINCT fixture_extension FROM diagnostic_attempt').fetchone()[0]=='must preserve'
        assert c.execute('SELECT COUNT(*) FROM fixture_child').fetchone()[0]==1


def test_completed_migration_does_not_rebuild_later_extensions(tmp_path):
    db = Database(tmp_path/'later.db')
    db.initialize()
    with db.connect() as c:
        c.execute('ALTER TABLE diagnostic_attempt ADD COLUMN fixture_extension TEXT')
        c.commit()
    db.initialize()
    with db.connect() as c:
        assert any(row['name']=='fixture_extension' for row in c.execute('PRAGMA table_info(diagnostic_attempt)'))


@pytest.mark.parametrize('existing_orphan',[
    pytest.param(False,id='full_database_upgrade'),
    pytest.param(True,id='module_unit_existing_orphan'),
])
def test_migration_preserves_rowid_gaps_references_and_existing_fk_identity(tmp_path,existing_orphan):
    """Healthy source upgrades fully; corrupt FK case proves module contract only."""
    db,objective = legacy_database(tmp_path)
    with sqlite3.connect(db.path) as c:
        c.execute("UPDATE diagnostic_attempt SET rowid=7 WHERE attempt_id='old-open-attempt'")
        if existing_orphan:
            c.execute("UPDATE diagnostic_attempt SET review_task_id='historical-missing-review' WHERE rowid=7")
        c.commit()
        original = c.execute('SELECT rowid,attempt_id FROM diagnostic_attempt ORDER BY rowid').fetchall()
        violations = c.execute('PRAGMA foreign_key_check').fetchall()
        assert [row[0] for row in original]==[1,7]
    if existing_orphan:
        # The original diagnostic migration explicitly preserves existing FK
        # violations. This is its module unit, not permission to boot a corrupt
        # whole DB through stricter school-review/global integrity guards.
        from home_study_agent.diagnostic_score_migration import migrate_unknown_diagnostic_scores
        with db.connect() as c:
            migrate_unknown_diagnostic_scores(c)
    else:
        db.initialize()
    with db.connect() as c:
        assert [tuple(row) for row in c.execute('SELECT rowid,attempt_id FROM diagnostic_attempt ORDER BY rowid')]==original
        assert [tuple(row) for row in c.execute('PRAGMA foreign_key_check')]==violations
        assert c.execute("SELECT attempt_id FROM fixture_child WHERE id='bound'").fetchone()[0]=='old-open-attempt'
        assert c.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='fixture_attempt_session'").fetchone()
        assert c.execute("SELECT correctness FROM diagnostic_attempt WHERE rowid=7").fetchone()[0] is None
        assert c.execute('SELECT correctness FROM diagnostic_attempt WHERE rowid=1').fetchone()[0]==objective['correctness']



def test_current88_falsely_marked69_is_rejected_without_changing_original_database(tmp_path):
    from home_study_agent.restore_publication import RestorePublicationError
    db, _ = current88_falsely_marked69(tmp_path)

    def original_dump():
        with closing(sqlite3.connect(db.path.resolve().as_uri() + '?mode=ro', uri=True)) as c:
            return tuple(c.iterdump())

    before = original_dump()
    with pytest.raises(RestorePublicationError, match='^RESTORE_ASSET_OWNERSHIP_UNCONFIRMED$'):
        db.initialize()
    assert original_dump() == before
    with closing(sqlite3.connect(db.path.resolve().as_uri() + '?mode=ro', uri=True)) as c:
        assert c.execute('SELECT MAX(version)FROM schema_migration').fetchone()[0] == 69
        assert c.execute("SELECT 1 FROM sqlite_master WHERE name='restore_asset_generation'").fetchone()
        assert not c.execute('PRAGMA foreign_key_check').fetchall()



def test_full_database_upgrade_holds_existing_fk_orphan_without_mutating_original(tmp_path):
    db, _ = legacy_database(tmp_path)
    with closing(sqlite3.connect(db.path)) as c:
        c.execute("UPDATE diagnostic_attempt SET rowid=7,review_task_id='historical-missing-review' WHERE attempt_id='old-open-attempt'")
        c.commit()

    def original_dump():
        with closing(sqlite3.connect(db.path.resolve().as_uri() + '?mode=ro', uri=True)) as c:
            return tuple(c.iterdump())

    before = original_dump()
    with pytest.raises(sqlite3.IntegrityError, match='^SCHOOL_REVIEW_MIGRATION_FOREIGN_KEY_FAILED$'):
        db.initialize()
    assert original_dump() == before
    with closing(sqlite3.connect(db.path.resolve().as_uri() + '?mode=ro', uri=True)) as c:
        assert c.execute('SELECT MAX(version)FROM schema_migration').fetchone()[0] == 67
        assert c.execute('PRAGMA foreign_key_check').fetchall()
        assert c.execute("SELECT correctness FROM diagnostic_attempt WHERE attempt_id='old-open-attempt'").fetchone()[0] == 0
