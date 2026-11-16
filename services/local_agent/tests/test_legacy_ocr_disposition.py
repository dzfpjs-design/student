import json
import sqlite3

import pytest

from home_study_agent.database import Database
from home_study_agent.local_ocr.cache_lifecycle import ensure_schema, quarantine_after_restore
from home_study_agent.local_ocr.legacy_disposition import (
    LegacyOcrDispositionService, LegacyDispositionConflict, LegacyDispositionInput,
    LegacyDispositionIncomplete,
)


@pytest.fixture
def scope(tmp_path):
    db = Database(tmp_path / 'family.db')
    db.initialize()
    side = tmp_path / 'local-ocr.db'
    with sqlite3.connect(side) as c:
        c.execute('CREATE TABLE ocr_job(job_id TEXT PRIMARY KEY,request_id TEXT,image_hash TEXT,result_json TEXT)')
        c.execute('CREATE TABLE ocr_cache(image_hash TEXT PRIMARY KEY,result_json TEXT)')
        c.execute("INSERT INTO ocr_job VALUES('unknown','unknown-key','unknown-hash','synthetic unknown text')")
        c.execute("INSERT INTO ocr_job VALUES('known','known-key','known-hash','synthetic protected text')")
        c.execute("INSERT INTO ocr_cache VALUES('unknown-hash','synthetic unknown text')")
        c.execute("INSERT INTO ocr_cache VALUES('known-hash','synthetic protected text')")
    service = LegacyOcrDispositionService(db, side)
    with db.connect() as c:
        c.execute('INSERT INTO ocr_legacy_reference VALUES(?,?,?,?,?,?)',
            (service.store_id, 'known', 'retained-child', 'retained-page', 'capture_artifact', 'known-hash'))
        c.commit()
    return db, side, service


def command(service, preview, **changes):
    return service.dispose_unknown(**dict(preview_id=preview['preview_id'],
        expected_fingerprint=preview['snapshot_fingerprint'], client_action_id='explicit-parent-disposition',
        reviewer_id='authenticated-household-parent', confirmation='确认清理无法归属的旧OCR缓存', **changes))


def test_exact_parent_preview_preserves_owned_and_main_database_and_restarts(scope):
    db, side, service = scope
    preview = service.preview_unknown()
    assert preview['counts'] == {'unattributed_jobs': 1, 'unattributed_cache': 1, 'protected_jobs': 1, 'protected_cache': 1}
    assert all(text not in json.dumps(preview) for text in ('synthetic unknown text', 'unknown-hash', str(side)))
    result = command(service, preview)
    assert result['status'] == 'completed'
    assert result['remaining_counts']['unattributed_jobs'] == 0
    with sqlite3.connect(side) as c:
        assert c.execute('SELECT job_id FROM ocr_job').fetchall() == [('known',)]
        assert c.execute('SELECT result_json FROM ocr_cache').fetchall() == [('synthetic protected text',)]
    restarted = LegacyOcrDispositionService(Database(db.path), side)
    assert command(restarted, preview) == result
    assert restarted.get_receipt(result['receipt_id']) == result


@pytest.mark.parametrize('change', ['body', 'new_job', 'ownership'])
def test_stale_preview_cannot_delete_changed_or_newly_owned_data(scope, change):
    db, side, service = scope
    preview = service.preview_unknown()
    if change == 'ownership':
        with db.connect() as c:
            c.execute('INSERT INTO ocr_legacy_reference VALUES(?,?,?,?,?,?)',
                (service.store_id, 'unknown', 'retained-child', 'newly-bound-page', 'capture_artifact', 'unknown-hash'))
            c.commit()
    else:
        with sqlite3.connect(side) as c:
            if change == 'body':
                c.execute("UPDATE ocr_job SET result_json='changed text' WHERE job_id='unknown'")
            else:
                c.execute("INSERT INTO ocr_job VALUES('new','new-key','new-hash','new text')")
    with pytest.raises(LegacyDispositionConflict):
        command(service, preview)
    with sqlite3.connect(side) as c:
        assert c.execute("SELECT 1 FROM ocr_job WHERE job_id='unknown'").fetchone()


def test_missing_confirmation_never_executes_and_key_is_bound_to_exact_actor(scope):
    _, side, service = scope
    preview = service.preview_unknown()
    with pytest.raises(LegacyDispositionInput):
        service.dispose_unknown(preview_id=preview['preview_id'], expected_fingerprint=preview['snapshot_fingerprint'],
            client_action_id='explicit-parent-disposition', reviewer_id='parent', confirmation='')
    command(service, preview)
    with pytest.raises(LegacyDispositionConflict):
        service.dispose_unknown(preview_id=preview['preview_id'], expected_fingerprint=preview['snapshot_fingerprint'],
            client_action_id='explicit-parent-disposition', reviewer_id='another-actor', confirmation='确认清理无法归属的旧OCR缓存')


def test_lost_receipt_after_purge_recovers_same_frozen_command_without_expanding(scope, monkeypatch):
    db, side, service = scope
    preview = service.preview_unknown()
    original = service._complete_receipt
    def fail(*args, **kwargs):
        raise OSError('synthetic receipt failure')
    monkeypatch.setattr(service, '_complete_receipt', fail)
    with pytest.raises(LegacyDispositionIncomplete):
        command(service, preview)
    with sqlite3.connect(side) as c:
        assert c.execute("SELECT 1 FROM ocr_job WHERE job_id='unknown'").fetchone() is None
        c.execute("INSERT INTO ocr_job VALUES('later','later-key','later-hash','later body')")
    restarted = LegacyOcrDispositionService(Database(db.path), side)
    receipt = command(restarted, preview)
    assert receipt['status'] == 'completed'
    assert receipt['remaining_counts']['unattributed_jobs'] == 1
    with sqlite3.connect(side) as c:
        assert c.execute("SELECT result_json FROM ocr_job WHERE job_id='later'").fetchone()[0] == 'later body'


def test_restore_invalidates_confirmation_not_replaying_it(scope):
    db, side, service = scope
    preview = service.preview_unknown()
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        quarantine_after_restore(c, 'synthetic-restore')
        c.commit()
    with pytest.raises(LegacyDispositionConflict):
        command(service, preview)
    with sqlite3.connect(side) as c:
        assert c.execute("SELECT 1 FROM ocr_job WHERE job_id='unknown'").fetchone()


def test_explicit_legacy_owner_without_main_reference_is_not_unknown(scope):
    _, side, service = scope
    with sqlite3.connect(side) as c:
        c.execute('ALTER TABLE ocr_job ADD COLUMN child_id TEXT')
        c.execute("UPDATE ocr_job SET child_id='retained-child' WHERE job_id='unknown'")
    preview = service.preview_unknown()
    assert preview['counts']['unattributed_jobs'] == 0
    assert preview['counts']['unattributed_cache'] == 0
    with pytest.raises(LegacyDispositionInput):
        command(service, preview)


def test_main_database_hardlink_is_never_accepted_as_legacy(scope, tmp_path):
    import os
    db, _, _ = scope
    alias = tmp_path / 'not-really-a-legacy.db'
    os.link(db.path, alias)
    with pytest.raises(ValueError, match='OCR_LEGACY_MUST_BE_SEPARATE'):
        LegacyOcrDispositionService(db, alias)
