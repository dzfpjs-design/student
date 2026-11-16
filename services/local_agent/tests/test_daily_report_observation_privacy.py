"""Synthetic tests of real privacy, backup and restore paths; never send mail."""
import json
import sqlite3

import pytest

from home_study_agent import daily_report_observation
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from local_restore_operation_fixture import restore_local
from test_daily_report_candidate import synthetic


def _seed(db, day):
    with db.connect() as c:
        first = daily_report_observation.prepare(c, 'a', day, 'recipient-a')
        second = daily_report_observation.prepare(c, 'b', day, 'recipient-b')
    return first, second


def _rows(db):
    with db.connect() as c:
        sources = {row['child_id']: row['source_id'] for row in c.execute('SELECT child_id,source_id FROM daily_observation_report_source')}
        outbox = {row['child_id']: row['source_id'] for row in c.execute('SELECT child_id,source_id FROM daily_observation_report_outbox')}
        assert c.execute('PRAGMA foreign_key_check').fetchall() == []
    return sources, outbox


def test_capture_deletion_purges_source_and_keeps_other_child(synthetic, tmp_path):
    db, day = synthetic
    _, second = _seed(db, day)
    privacy = PrivacyLifecycleService(db, tmp_path)
    privacy.initialize()
    result = privacy.delete_capture_artifact('art', '确认删除', 'delete-art')
    assert result['status'] == 'completed'
    assert _rows(db) == ({'b': second}, {'b': second})
    with db.connect() as c:
        assert daily_report_observation.inspect_current(c, 'b', day, 'recipient-b')['current']


@pytest.mark.parametrize('scope', ['learning', 'media', 'reports', 'all_history'])
def test_scope_deletion_purges_one_child(synthetic, tmp_path, scope):
    db, day = synthetic
    _, second = _seed(db, day)
    privacy = PrivacyLifecycleService(db, tmp_path)
    privacy.initialize()
    result = privacy.delete_child_scope('a', scope, '确认删除', 'delete-' + scope)
    assert result['status'] == 'completed'
    assert _rows(db) == ({'b': second}, {'b': second})


def test_export_includes_only_selected_child(synthetic, tmp_path):
    db, day = synthetic
    first, second = _seed(db, day)
    privacy = PrivacyLifecycleService(db, tmp_path)
    privacy.initialize()
    exported = privacy.export_child('a')
    with open(exported['path'], encoding='utf-8') as f:
        data = json.load(f)
    assert [row['source_id'] for row in data['daily_observation_report_sources']] == [first]
    assert [row['source_id'] for row in data['daily_observation_report_outbox']] == [first]
    assert second not in json.dumps(data['daily_observation_report_sources'])
    assert second not in json.dumps(data['daily_observation_report_outbox'])


def test_restore_quarantines_old_prepared_sources(synthetic, tmp_path):
    db, day = synthetic
    _seed(db, day)
    privacy = PrivacyLifecycleService(db, tmp_path)
    privacy.initialize()
    backup = privacy.create_backup(prune=False)
    with db.connect() as c:
        c.execute("UPDATE daily_session SET status='completed',state='completed',ended_at=last_checkpoint_at WHERE session_id='s'")
        c.commit()
    assert restore_local(privacy, backup['backup_id'], '确认恢复')
    assert _rows(db) == ({}, {})
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM report_delivery_recipient WHERE consent_status='consented'").fetchone()[0] == 0


def test_capture_deletion_failure_rolls_back_sources_and_artifact(synthetic, tmp_path):
    db, day = synthetic
    first, second = _seed(db, day)
    privacy = PrivacyLifecycleService(db, tmp_path)
    privacy.initialize()
    with db.connect() as c:
        c.execute("CREATE TRIGGER synthetic_block_source_delete BEFORE DELETE ON daily_observation_report_source BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END")
        c.commit()
    with pytest.raises(sqlite3.IntegrityError, match='synthetic failure'):
        privacy.delete_capture_artifact('art', '确认删除', 'delete-failure')
    assert _rows(db) == ({'a':first, 'b':second}, {'a':first, 'b':second})
    with db.connect() as c:
        assert c.execute("SELECT count(*) FROM capture_artifact WHERE artifact_id='art'").fetchone()[0] == 1
        assert c.execute("SELECT status FROM deletion_job WHERE client_action_id='delete-failure'").fetchone()[0] != 'completed'


@pytest.mark.parametrize('operation', ['capture', 'reports'])
def test_deleting_invalidates_affected_managed_export_only(synthetic, tmp_path, operation):
    db, day = synthetic
    _seed(db, day)
    privacy = PrivacyLifecycleService(db, tmp_path)
    privacy.initialize()
    a = privacy.export_child('a')
    b = privacy.export_child('b')
    from pathlib import Path
    a_export, b_export = Path(a['path']), Path(b['path'])
    assert a_export.is_file() and b_export.is_file()
    if operation == 'capture':
        result = privacy.delete_capture_artifact('art', '确认删除', 'erase-capture-with-export')
    else:
        result = privacy.delete_child_scope('a', 'reports', '确认删除', 'erase-reports-with-export')
    assert result['status'] == 'completed'
    assert not a_export.exists()
    assert b_export.is_file()
    with b_export.open(encoding='utf-8') as f:
        assert json.load(f)['child'][0]['child_id'] == 'b'
