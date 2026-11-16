"""Real same-machine backup/restore, including immutable commands and quarantine."""
from synthetic_native_restore_fixture import KEY, signed_local_restore_post
import hashlib
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.database import Database, LATEST_SCHEMA_VERSION
from home_study_agent.report_adjustment_rebinding import ReportRebindingService
from home_study_agent.report_followup import ReportFollowupService
from test_period_feedback_explicit_contract import (
    journey, flow, learning_clock, consistent_action_clock, completed_report_pair, bind, base, CHILD)


def restore_operation(client, headers):
    response = client.get('/v1/parent/privacy-operations/generation', headers=headers)
    assert response.status_code == 200, response.text
    return {'request_id': str(uuid4()), 'expected_generation': response.json()['generation'],
            'cleanup_manifest_sha256': hashlib.sha256(b'[]').hexdigest()}


@pytest.mark.parametrize('journey,withdraw_before_backup',[(KEY,False),(KEY,True)],indirect=['journey'],ids=['False','True'])
def test_current_schema_backup_restore_restart_original_command_and_withdrawal(journey, withdraw_before_backup):
    f, settings, client, headers = journey
    _, target, _ = completed_report_pair(journey, 'weekly')
    day = f.clock[0][:10]
    confirmed = bind(client, headers, target, day, 'backup')
    root = base() + '/' + confirmed['adjustment_id'] + '/review'
    withdrawal = {'expected_revision': 2, 'decision': 'retract', 'review_hash': confirmed['review_hash'],
                  'evidence_seen': False, 'client_action_id': 'backup-withdrawal'}
    prior_withdrawal = None
    if withdraw_before_backup:
        response = client.post(root, headers=headers, json=withdrawal)
        assert response.status_code == 200, response.text
        prior_withdrawal = response.json()
    assert client.post(base() + '/commands/cancelled-before-backup/cancel', headers=headers,
        json={'target_snapshot_id': target['snapshot_id']}).json() == {'status': 'cancelled_not_committed'}
    tables = ('report_adjustment_binding', 'report_rebinding_command', 'report_rebinding_cancelled_command')
    with f.db.connect() as c:
        assert c.execute('SELECT MAX(version) FROM schema_migration').fetchone()[0] == LATEST_SCHEMA_VERSION
        stored = {table: [tuple(row) for row in c.execute('SELECT * FROM ' + table + ' ORDER BY rowid')] for table in tables}
        assert all(stored.values())
        nickname = c.execute('SELECT nickname FROM child_profile WHERE child_id=?', (CHILD,)).fetchone()[0]
        journal = [tuple(row) for row in c.execute('SELECT * FROM learning_evidence ORDER BY rowid')]
    response = client.post('/v1/parent/backups', headers=headers)
    assert response.status_code == 200, response.text
    backup = response.json()
    with sqlite3.connect('file:' + str(Path(backup['path'])) + '?mode=ro', uri=True) as c:
        assert c.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert not c.execute('PRAGMA foreign_key_check').fetchall()
        assert c.execute('SELECT MAX(version) FROM schema_migration').fetchone()[0] == LATEST_SCHEMA_VERSION
        assert {table: c.execute('SELECT * FROM ' + table + ' ORDER BY rowid').fetchall() for table in tables} == stored
    with f.db.connect() as c:
        c.execute("UPDATE child_profile SET nickname='Synthetic post-backup mutation' WHERE child_id=?", (CHILD,))
        c.commit()
    response = signed_local_restore_post(client, headers=headers, json={
        'backup_id': backup['backup_id'], 'confirmation': '确认恢复', 'operation': restore_operation(client, headers)})
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'restored'
    client.finish_original_lifecycle()
    with TestClient(create_app(settings)) as reopened:
        h = {'X-HSC-Parent': reopened.post('/v1/parent/unlock', json={'pin': '2468'}).json()['parent_token']}
        with f.db.connect() as c:
            assert c.execute('SELECT nickname FROM child_profile WHERE child_id=?', (CHILD,)).fetchone()[0] == nickname
            assert c.execute('SELECT MAX(version) FROM schema_migration').fetchone()[0] == LATEST_SCHEMA_VERSION
            assert [tuple(row) for row in c.execute('SELECT * FROM learning_evidence ORDER BY rowid')] == journal
            assert not c.execute('PRAGMA foreign_key_check').fetchall()
            assert {table: [tuple(row) for row in c.execute('SELECT * FROM ' + table + ' ORDER BY rowid')] for table in tables} == stored
        result = reopened.get(base() + '/commands/backup-confirm', headers=h)
        assert result.status_code == 200, result.text
        assert result.json()['receipt'] == confirmed
        # Restore quarantines approvals; historical command receipt is never live permission.
        assert not result.json()['current']['source_current']
        assert not result.json()['current']['effective_as_guard']
        assert not ReportFollowupService(f.db).project_for_plan(CHILD, day)['constraints']
        assert reopened.get(base() + '/commands/cancelled-before-backup', headers=h).json() == {'status': 'cancelled_not_committed'}
        first = reopened.post(root, headers=h, json=withdrawal)
        assert first.status_code == 200, first.text
        assert first.json()['operation'] == 'retract'
        if withdraw_before_backup:
            assert first.json() == prior_withdrawal
        assert reopened.post(root, headers=h, json=withdrawal).json() == first.json()
        original = reopened.get(base() + '/commands/backup-withdrawal', headers=h).json()
        assert original['receipt'] == first.json() and original['current']['operation'] == 'retract'
        with f.db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM report_plan_adjustment_event').fetchone()[0] == 3
    # Feature-owned ensure remains idempotent alongside the global migration.
    ReportRebindingService(Database(f.db.path, runtime_mode=f.db.runtime_mode)).initialize()
    ReportRebindingService(Database(f.db.path, runtime_mode=f.db.runtime_mode)).initialize()
    with f.db.connect() as c:
        assert c.execute('SELECT MAX(version) FROM schema_migration').fetchone()[0] == LATEST_SCHEMA_VERSION
        assert not c.execute('PRAGMA foreign_key_check').fetchall()
        for table in tables:
            with pytest.raises(sqlite3.IntegrityError, match='APPEND_ONLY'):
                c.execute('UPDATE ' + table + ' SET child_id=child_id')
            c.rollback()
