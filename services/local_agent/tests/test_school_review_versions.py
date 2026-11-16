"""Known old schema, real FK rows and atomic migration; no family database."""
import sqlite3

import pytest

from home_study_agent.database import SCHEMA
from home_study_agent.school_review_versions import migrate


def legacy(tmp_path, extra=''):
    c = sqlite3.connect(tmp_path/'legacy.db')
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    for table, key in [('child_profile','child_id'),('knowledge_node','knowledge_node_id'),
                       ('diagnostic_item','diagnostic_item_id'),('learning_evidence','evidence_id')]:
        c.execute(f'CREATE TABLE {table}({key} TEXT PRIMARY KEY)')
        c.execute(f'INSERT INTO {table} VALUES (?)', ('one',))
    ddl = SCHEMA.split('CREATE TABLE IF NOT EXISTS review_task (',1)[1].split('\n);',1)[0]
    c.execute('CREATE TABLE review_task (' + ddl + extra + ')')
    c.execute("INSERT INTO review_task VALUES ('old','one','one','one','one','school_open_near','2026-09-10','pending',NULL,NULL,'2026-09-09')")
    c.execute('CREATE INDEX retained_review_date ON review_task(due_at)')
    c.execute('CREATE TABLE diagnostic_attempt(attempt_id TEXT PRIMARY KEY, review_task_id TEXT REFERENCES review_task(review_task_id))')
    c.execute("INSERT INTO diagnostic_attempt VALUES ('answer','old')")
    c.execute("CREATE TRIGGER retain_old_review BEFORE DELETE ON review_task WHEN OLD.review_task_id='old' BEGIN SELECT RAISE(ABORT,'keep historical task'); END")
    c.commit()
    return c


def test_real_old_fk_schema_migrates_without_rebinding_answer_and_restarts(tmp_path):
    with legacy(tmp_path) as c:
        c.execute("UPDATE review_task SET rowid=7 WHERE review_task_id='old'")
        c.commit()
        migrate(c)
        assert c.execute("SELECT rowid FROM review_task WHERE review_task_id='old'").fetchone()[0] == 7
        assert not c.execute('PRAGMA foreign_key_check').fetchall()
        assert c.execute('SELECT review_task_id FROM diagnostic_attempt').fetchone()[0] == 'old'
        assert c.execute("SELECT sql FROM sqlite_master WHERE name='retained_review_date'").fetchone()
        assert c.execute('SELECT score_authority_id FROM review_task').fetchone()[0] is None
        with pytest.raises(sqlite3.IntegrityError, match='keep historical'):
            c.execute("DELETE FROM review_task WHERE review_task_id='old'")
        c.rollback()
        c.execute("UPDATE review_task SET status='cancelled' WHERE review_task_id='old'")
        c.execute("INSERT INTO review_task SELECT 'new',child_id,knowledge_node_id,diagnostic_item_id,source_evidence_id,interval_label,due_at,'pending',NULL,NULL,created_at,'human-event','reviewed_human','old' FROM review_task WHERE review_task_id='old'")
        c.commit()
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("INSERT INTO review_task SELECT 'duplicate',child_id,knowledge_node_id,diagnostic_item_id,source_evidence_id,interval_label,due_at,'pending',NULL,NULL,created_at,'different','reviewed_human',NULL FROM review_task WHERE review_task_id='new'")
        c.rollback()
        migrate(c)
        assert c.execute('SELECT COUNT(*) FROM review_task').fetchone()[0] == 2
        assert c.execute("SELECT rowid FROM review_task WHERE review_task_id='old'").fetchone()[0] == 7


def test_migration_failure_rolls_back_ddl_rows_and_restores_foreign_keys(tmp_path, monkeypatch):
    from home_study_agent import school_review_versions
    with legacy(tmp_path) as c:
        c.execute("UPDATE review_task SET rowid=7 WHERE review_task_id='old'")
        c.commit()
        before = c.execute("SELECT type,name,sql FROM sqlite_master ORDER BY name").fetchall()
        def fail(c):
            raise sqlite3.OperationalError('injected index write fault')
        with monkeypatch.context() as fault:
            fault.setattr(school_review_versions, '_indexes', fail)
            with pytest.raises(sqlite3.OperationalError, match='index write fault'):
                migrate(c)
        assert c.execute('PRAGMA foreign_keys').fetchone()[0] == 1
        assert c.execute("SELECT type,name,sql FROM sqlite_master ORDER BY name").fetchall() == before
        assert c.execute('SELECT COUNT(*) FROM review_task').fetchone()[0] == 1
        assert c.execute("SELECT rowid FROM review_task WHERE review_task_id='old'").fetchone()[0] == 7
        migrate(c)
        assert c.execute("SELECT rowid FROM review_task WHERE review_task_id='old'").fetchone()[0] == 7


def test_unknown_old_check_is_rejected_without_modification(tmp_path):
    with legacy(tmp_path, ", CHECK(status IN ('pending','cancelled'))") as c:
        before = c.total_changes
        with pytest.raises(RuntimeError, match='UNSUPPORTED_SCHEMA'):
            migrate(c)
        assert c.total_changes == before
        assert len(c.execute('PRAGMA table_info(review_task)').fetchall()) == 11


def test_borrowed_transaction_is_never_committed(tmp_path):
    with legacy(tmp_path) as c:
        c.execute('BEGIN IMMEDIATE')
        with pytest.raises(RuntimeError, match='REQUIRES_IDLE'):
            migrate(c)
        assert c.in_transaction
        c.rollback()
