"""Report recovery history follows report export and erasure boundaries."""

from local_restore_operation_fixture import restore_local
import json
import sqlite3
from pathlib import Path

import pytest

from home_study_agent.database import Database
from home_study_agent.parent_reports import ParentReportService
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService, DeletionIncompleteError
from home_study_agent.repository import StudyRepository


CHILD = 'child-primary-4'
OTHER = 'child-junior-3'


@pytest.fixture
def issue_home(tmp_path):
    db = Database(tmp_path / 'issues.db')
    db.initialize()
    StudyRepository(db).seed_family()
    privacy = PrivacyLifecycleService(db, tmp_path, data_encryption_key=b'I' * 32)
    privacy.initialize()
    reports = ParentReportService(db)
    for child in (CHILD, OTHER):
        reports.record_source_issue(child, 'daily_report', 'synthetic-missing-'+child,
                                    'REPORT_SOURCE_MISSING')
    with db.connect() as c:
        c.execute("INSERT INTO audit_event(audit_id,actor,action,child_id,summary_json,occurred_at) VALUES ('unrelated','system','unrelated_local_audit',?,'{}',datetime('now'))", (CHILD,))
        c.commit()
    return db, privacy


@pytest.mark.parametrize('scope', ['reports', 'all_history'])
def test_issue_export_and_erasure_survive_restart_without_touching_sibling(issue_home, scope):
    db, privacy = issue_home
    assert privacy.deletion_preview(CHILD)['parent_report_source_issues'] == 1
    exported = json.loads(Path(privacy.export_child(CHILD)['path']).read_text())
    events = exported['parent_report_source_issues']
    assert len(events) == 1 and events[0]['child_id'] == CHILD
    assert json.loads(events[0]['summary_json'])['error_code'] == 'REPORT_SOURCE_MISSING'
    result = privacy.delete_child_scope(CHILD, scope, '确认删除', 'issue-erasure-'+scope)
    assert result['status'] == 'completed'
    reopened = Database(db.path)
    assert ParentReportService(reopened).source_issues(CHILD, include_resolved=True) == []
    assert len(ParentReportService(reopened).source_issues(OTHER, include_resolved=True)) == 1
    with reopened.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM audit_event WHERE audit_id='unrelated'").fetchone()[0] == 1
        assert not c.execute('PRAGMA foreign_key_check').fetchall()


@pytest.mark.parametrize('kind', ['local', 'portable'])
def test_restore_preserves_unresolved_issue_history(issue_home, kind):
    db, privacy = issue_home
    original = ParentReportService(db).source_issues(CHILD, include_resolved=True)
    assert len(original) == 1 and original[0]['status'] == 'open'
    password = 'synthetic-portable-backup-phrase-2026'
    backup = privacy.create_backup(prune=False) if kind == 'local' else privacy.create_portable_backup(password)
    if kind == 'local':
        result = restore_local(privacy,backup['backup_id'], '确认恢复')
    else:
        result = privacy.restore_portable_backup(backup['path'], password, '确认迁移恢复')
    assert result['status'] == 'restored'
    assert ParentReportService(Database(db.path)).source_issues(CHILD, include_resolved=True) == original


def test_failed_report_erasure_rolls_back_issue_history(issue_home):
    db, privacy = issue_home
    with db.connect() as c:
        c.execute("CREATE TRIGGER prevent_issue_erasure BEFORE DELETE ON audit_event WHEN OLD.action='parent_report_source_issue' BEGIN SELECT RAISE(ABORT, 'synthetic issue erasure failure'); END")
        c.commit()
    with pytest.raises(DeletionIncompleteError, match='PRIVACY_DATABASE_DELETE_FAILED') as failure:
        privacy.delete_child_scope(CHILD, 'reports', '确认删除', 'issue-erasure-failure')
    assert isinstance(failure.value.__cause__, sqlite3.IntegrityError)
    assert str(failure.value.__cause__) == 'synthetic issue erasure failure'
    with Database(db.path).connect() as c:
        job = dict(c.execute('SELECT * FROM deletion_job WHERE deletion_id=?',
            (failure.value.deletion_id,)).fetchone())
        assert job['status'] == 'failed'
        assert job['error_code'] == 'PRIVACY_DATABASE_DELETE_FAILED'
        assert job['client_action_id'] == 'issue-erasure-failure'
        assert 'synthetic issue erasure failure' not in job['error_message']
    assert len(ParentReportService(Database(db.path)).source_issues(CHILD, include_resolved=True)) == 1
    assert len(ParentReportService(Database(db.path)).source_issues(OTHER, include_resolved=True)) == 1
    with db.connect() as c:
        c.execute('DROP TRIGGER prevent_issue_erasure')
        c.commit()
    result = privacy.delete_child_scope(CHILD, 'reports', '确认删除', 'issue-erasure-failure')
    assert result['status'] == 'completed'
    assert result['deletion_id'] == failure.value.deletion_id
    assert ParentReportService(Database(db.path)).source_issues(CHILD, include_resolved=True) == []
