"""Restore old action ledgers, including actual feedback, without rewriting facts."""
import sqlite3

import pytest

from home_study_agent.parent_action_evidence import ParentActionEvidenceService
from test_parent_coaching_feedback_semantics import repository, report, CHILD


OLD_SCHEMA = (
    '''CREATE TABLE parent_action_snapshot (
      snapshot_id TEXT PRIMARY KEY, report_id TEXT NOT NULL REFERENCES daily_report(report_id) ON DELETE CASCADE,
      child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
      version INTEGER NOT NULL CHECK(version>0),fingerprint TEXT NOT NULL,payload_json TEXT NOT NULL,created_at TEXT NOT NULL,
      UNIQUE(report_id,version),UNIQUE(report_id,fingerprint))''',
    '''CREATE TABLE parent_action_feedback_event (
      event_id TEXT PRIMARY KEY,snapshot_id TEXT NOT NULL REFERENCES parent_action_snapshot(snapshot_id) ON DELETE CASCADE,
      child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
      revision INTEGER NOT NULL CHECK(revision>0),operation TEXT NOT NULL CHECK(operation IN ('record','retract')),
      payload_json TEXT NOT NULL,request_json TEXT NOT NULL,client_action_id TEXT NOT NULL,created_at TEXT NOT NULL,
      UNIQUE(snapshot_id,revision),UNIQUE(child_id,client_action_id))''',
)


def old_ledger(repo):
    saved = report(repo)
    actions = ParentActionEvidenceService(repo.database)
    original = saved['parent_action']
    receipt = actions.record_feedback(child_id=CHILD, report_id=saved['report_id'], snapshot_id=original['snapshot_id'],
        client_action_id='real-historical-feedback', expected_revision=0, executed=False,
        executed_at=None, effectiveness=None, stress_rating=None, notes_summary='合成：暂未执行')
    with repo.database.connect() as c:
        parent = c.execute('SELECT snapshot_id,report_id,child_id,version,fingerprint,payload_json,created_at FROM parent_action_snapshot').fetchall()
        events = c.execute('SELECT * FROM parent_action_feedback_event').fetchall()
        c.execute('BEGIN IMMEDIATE')
        c.execute('DROP TABLE parent_action_feedback_event')
        c.execute('DROP TABLE parent_action_snapshot')
        for statement in OLD_SCHEMA:
            c.execute(statement)
        c.executemany('INSERT INTO parent_action_snapshot VALUES (?,?,?,?,?,?,?)', parent)
        c.executemany('INSERT INTO parent_action_feedback_event VALUES (?,?,?,?,?,?,?,?,?)', events)
        c.commit()
    return actions, original, receipt, [tuple(row) for row in parent], [tuple(row) for row in events]


def test_upgrade_preserves_real_daily_ids_fingerprints_feedback_and_replay(repository):
    actions, original, receipt, parent, events = old_ledger(repository)
    actions.initialize()
    actions.initialize()
    with repository.database.connect() as c:
        columns = {row['name']: row for row in c.execute('PRAGMA table_info(parent_action_snapshot)')}
        assert columns['report_id']['notnull'] == 0
        assert 'period_report_id' in columns
        assert [tuple(row) for row in c.execute('SELECT snapshot_id,report_id,child_id,version,fingerprint,payload_json,created_at FROM parent_action_snapshot')] == parent
        assert [tuple(row) for row in c.execute('SELECT * FROM parent_action_feedback_event')] == events
        assert c.execute('PRAGMA foreign_key_check').fetchall() == []
        assert c.execute('PRAGMA foreign_keys').fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            c.execute('UPDATE parent_action_snapshot SET version=99')
    assert actions.get(CHILD, original['snapshot_id'])['feedback'] == receipt
    assert actions.record_snapshot_feedback(child_id=CHILD, snapshot_id=original['snapshot_id'],
        client_action_id='real-historical-feedback', expected_revision=0, executed=False,
        notes_summary='合成：暂未执行') == receipt


@pytest.mark.parametrize('phase', ['create', 'drop', 'rename'])
def test_schema_upgrade_failure_leaves_both_original_ledgers_and_foreign_keys_intact(repository, phase):
    actions, _, _, parent, events = old_ledger(repository)
    with repository.database.connect() as c:
        denied = False
        def authorize(operation, arg1, arg2, database, trigger):
            nonlocal denied
            target = {'create': sqlite3.SQLITE_CREATE_TABLE, 'drop': sqlite3.SQLITE_DROP_TABLE,
                      'rename': sqlite3.SQLITE_ALTER_TABLE}[phase]
            if not denied and operation == target and 'parent_action' in f'{arg1}:{arg2}':
                denied = True
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        c.set_authorizer(authorize)
        with pytest.raises(sqlite3.DatabaseError):
            actions.ensure_schema(c)
        c.set_authorizer(lambda *_: sqlite3.SQLITE_OK)
        assert denied and not c.in_transaction
        assert 'period_report_id' not in {row['name'] for row in c.execute('PRAGMA table_info(parent_action_snapshot)')}
        assert [tuple(row) for row in c.execute('SELECT * FROM parent_action_snapshot')] == parent
        assert [tuple(row) for row in c.execute('SELECT * FROM parent_action_feedback_event')] == events
        assert c.execute('PRAGMA foreign_key_check').fetchall() == []
        assert c.execute('PRAGMA foreign_keys').fetchone()[0] == 1
