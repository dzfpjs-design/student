from synthetic_native_restore_fixture import KEY, signed_local_restore_post
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest
from privacy_restore_operation import restore_operation
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.local_ocr.review_queue import OcrReviewQueue
from home_study_agent.local_ocr.review_lifecycle import delete_review_scope
from test_ocr_review_queue import artifact


def seed(queue, child='child-primary-4', suffix='one'):
    value = {**artifact(), 'artifact_id': f'artifact-{suffix}'}
    return queue.prepare(value, child, None, f'prepare-{suffix}')['items']


def test_legacy_copy_preserves_exact_ids_text_and_retries_and_main_backup(tmp_path):
    source = tmp_path / 'ocr-review-queue.db'
    legacy = OcrReviewQueue(source)
    rows = seed(legacy)
    corrected = legacy.update(rows[0]['review_id'], 'child-primary-4', 'reviewed original', 'original-review-key')
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    db = Database(tmp_path / 'managed.db')
    db.initialize()
    managed = OcrReviewQueue(db, legacy_path=source)
    assert managed.update(rows[0]['review_id'], 'child-primary-4', 'reviewed original', 'original-review-key') == corrected
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert (tmp_path / 'ocr-review-queue.db.imported').is_file()
    copied = tmp_path / 'backup.db'
    target = sqlite3.connect(copied)
    with db.connect() as connection:
        connection.backup(target)
    target.close()
    assert OcrReviewQueue(Database(copied)).recent('child-primary-4')['items'] == managed.recent('child-primary-4')['items']
    with db.connect() as connection:
        delete_review_scope(connection, 'child-primary-4')
        connection.execute('DELETE FROM ocr_review_legacy_import')
        connection.commit()
    # Even restoring a primary version without its SQL migration marker must not
    # automatically resurrect reviews from the retained historical sidecar.
    assert OcrReviewQueue(db, legacy_path=source).pending('child-primary-4')['items'] == []


def test_migration_conflict_rolls_back_all_copied_rows_without_marker(tmp_path):
    source = tmp_path / 'ocr-review-queue.db'
    legacy = OcrReviewQueue(source)
    first, second = seed(legacy)[:2]
    db = Database(tmp_path / 'managed.db')
    db.initialize()
    OcrReviewQueue(db)
    old = sqlite3.connect(source)
    old.row_factory = sqlite3.Row
    row = dict(old.execute('SELECT * FROM ocr_review_item WHERE review_id=?', (second['review_id'],)).fetchone())
    old.close()
    row['proposed_text'] = 'different current review'
    with db.connect() as connection:
        connection.execute(f'INSERT INTO ocr_review_item ({",".join(row)}) VALUES ({",".join("?" for _ in row)})', tuple(row.values()))
        connection.commit()
    with pytest.raises(ValueError, match='identity conflict'):
        OcrReviewQueue(db, legacy_path=source)
    with db.connect() as connection:
        assert not connection.execute('SELECT 1 FROM ocr_review_item WHERE review_id=?', (first['review_id'],)).fetchone()
        assert connection.execute('SELECT COUNT(*) FROM ocr_review_item').fetchone()[0] == 1
        assert connection.execute('SELECT COUNT(*) FROM ocr_review_legacy_import').fetchone()[0] == 0
    assert not (tmp_path / 'ocr-review-queue.db.imported').exists()


@pytest.mark.parametrize('kind', ['local', 'portable'])
def test_actual_backup_restore_and_restart_retain_original_review_and_retry(tmp_path, kind):
    source_dir = tmp_path / 'source'
    source_dir.mkdir()
    legacy = OcrReviewQueue(source_dir / 'ocr-review-queue.db')
    original = seed(legacy)[0]
    source = Settings(data_dir=source_dir, database_path=source_dir / 'family.db', maintenance_enabled=False, data_encryption_key=b'S' * 32, native_launch_attestation_key=KEY if kind == 'local' else None)
    passphrase = 'synthetic OCR migration phrase'
    with TestClient(create_app(source)) as client:
        headers = {'X-HSC-Parent': client.post('/v1/parent/setup', json={'pin': '2468'}).json()['parent_token']}
        payload = {'child_id': 'child-primary-4', 'corrected_text': 'preserved reviewed formula', 'client_action_id': 'retained-correction'}
        path = f"/v1/ocr-review-items/{original['review_id']}"
        corrected = client.put(path, headers=headers, json=payload)
        assert corrected.status_code == 200, corrected.text
        backup = (client.post('/v1/parent/backups', headers=headers) if kind == 'local'
                  else client.post('/v1/parent/backups/portable', headers=headers, json={'passphrase': passphrase}))
        assert backup.status_code == 200, backup.text
        saved = backup.json()
        source_sha = hashlib.sha256(Path(saved['path']).read_bytes()).hexdigest()
        if kind == 'local':
            undone = client.post(path + '/undo', headers=headers, json={'child_id': 'child-primary-4', 'client_action_id': 'undo-before-restore'})
            assert undone.status_code == 200, undone.text
            restored = signed_local_restore_post(client, headers=headers, json={
                'backup_id': saved['backup_id'], 'confirmation': '确认恢复',
                'operation': restore_operation(client, headers=headers)})
            assert restored.status_code == 200, restored.text
    destination = source
    if kind == 'portable':
        target_dir = tmp_path / 'target'
        destination = Settings(data_dir=target_dir, database_path=target_dir / 'family.db', maintenance_enabled=False, data_encryption_key=b'T' * 32)
        with TestClient(create_app(destination)) as client:
            headers = {'X-HSC-Parent': client.post('/v1/parent/setup', json={'pin': '2468'}).json()['parent_token']}
            restored = client.post('/v1/parent/backups/portable/restore', headers=headers, json={
                'path': saved['path'], 'passphrase': passphrase, 'confirmation': '确认迁移恢复',
                'operation': restore_operation(client, headers=headers)})
            assert restored.status_code == 200, restored.text
    with TestClient(create_app(destination)) as client:
        unlocked = client.post('/v1/parent/unlock', json={'pin': '2468'})
        assert unlocked.status_code == 200, unlocked.text
        headers = {'X-HSC-Parent': unlocked.json()['parent_token']}
        recent = client.get('/v1/children/child-primary-4/ocr-review-history', headers=headers)
        assert recent.status_code == 200
        assert corrected.json() in recent.json()['items']
        replay = client.put(path, headers=headers, json=payload)
        assert replay.status_code == 200 and replay.json() == corrected.json()
        with client.app.state.ocr_review_queue.database.connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM ocr_review_event WHERE client_action_id='retained-correction'").fetchone()[0] == 1
    assert hashlib.sha256(Path(saved['path']).read_bytes()).hexdigest() == source_sha


def test_interrupted_marker_publication_resumes_without_recopying_successful_records(tmp_path, monkeypatch):
    from home_study_agent.local_ocr import review_lifecycle
    source = tmp_path / 'ocr-review-queue.db'
    legacy = OcrReviewQueue(source)
    original = seed(legacy)[0]
    db = Database(tmp_path / 'family.db')
    db.initialize()
    replace = review_lifecycle.os.replace
    monkeypatch.setattr(review_lifecycle.os, 'replace', lambda *_: (_ for _ in ()).throw(OSError('injected publish failure')))
    with pytest.raises(OSError, match='injected'):
        OcrReviewQueue(db, legacy_path=source)
    assert not (tmp_path / 'ocr-review-queue.db.imported').exists()
    managed = OcrReviewQueue(db)
    corrected = managed.update(original['review_id'], 'child-primary-4', 'newer managed review', 'newer-edit')
    monkeypatch.setattr(review_lifecycle.os, 'replace', replace)
    resumed = OcrReviewQueue(db, legacy_path=source)
    assert resumed.recent('child-primary-4')['items'][0] == corrected


def test_legacy_cleanup_failure_keeps_deletion_retryable_until_source_is_purged(tmp_path, monkeypatch):
    from home_study_agent.local_ocr import review_lifecycle
    legacy = OcrReviewQueue(tmp_path / 'ocr-review-queue.db')
    seed(legacy)
    settings = Settings(data_dir=tmp_path, database_path=tmp_path / 'family.db', maintenance_enabled=False)
    with TestClient(create_app(settings)) as client:
        token = client.post('/v1/parent/setup', json={'pin': '2468'}).json()['parent_token']
        headers = {'X-HSC-Parent': token}
        purge = review_lifecycle.purge_legacy_review_scope
        monkeypatch.setattr(review_lifecycle, 'purge_legacy_review_scope', lambda *_: (_ for _ in ()).throw(sqlite3.OperationalError('injected lock')))
        result = client.post('/v1/parent/children/child-primary-4/delete', headers=headers, json={
            'scope': 'learning', 'confirmation': '确认删除', 'client_action_id': 'ocr-delete-failure'})
        assert result.status_code != 200
        with client.app.state.ocr_review_queue.database.connect() as connection:
            job = dict(connection.execute("SELECT * FROM deletion_job WHERE client_action_id='ocr-delete-failure'").fetchone())
        assert job['status'] == 'failed' and job['error_code'] == 'OCR_LEGACY_DELETE_FAILED'
        assert legacy.pending('child-primary-4')['items']
        monkeypatch.setattr(review_lifecycle, 'purge_legacy_review_scope', purge)
        retried = client.post(f"/v1/parent/deletions/{job['deletion_id']}/retry", headers=headers, json={'confirmation': '确认删除'})
        assert retried.status_code == 200, retried.text
        assert retried.json()['status'] == 'completed'
        assert legacy.pending('child-primary-4')['items'] == []


def test_single_capture_delete_removes_review_text_events_and_batch_receipt_but_not_other_work(tmp_path):
    legacy = OcrReviewQueue(tmp_path / 'ocr-review-queue.db')
    rows = seed(legacy)
    remaining = seed(legacy, suffix='two')
    legacy.batch([rows[0]['review_id'], remaining[0]['review_id']], 'child-primary-4', 'skip', 'mixed-artifact-batch')
    settings = Settings(data_dir=tmp_path, database_path=tmp_path / 'family.db', maintenance_enabled=False)
    with TestClient(create_app(settings)) as client:
        token = client.post('/v1/parent/setup', json={'pin': '2468'}).json()['parent_token']
        queue = client.app.state.ocr_review_queue
        session = client.post('/v1/sessions', json={'child_id': 'child-primary-4', 'client_action_id': 'single-ocr-session'})
        assert session.status_code == 201, session.text
        with queue.database.connect() as connection:
            connection.execute("""INSERT INTO capture_artifact
                (artifact_id,child_id,session_id,artifact_type,details_json,status,created_at,client_action_id)
                VALUES('artifact-one','child-primary-4',?,'document','{}','completed','2026-09-07T00:00:00Z','capture-one')""", (session.json()['session_id'],))
            connection.commit()
        response = client.request('DELETE', '/v1/parent/capture-artifacts/artifact-one', headers={'X-HSC-Parent': token},
            json={'confirmation': '确认删除', 'client_action_id': 'delete-single-ocr'})
        assert response.status_code == 200, response.text
        for store in (queue, legacy):
            assert store.list('artifact-one', 'child-primary-4')['items'] == []
            assert {row['review_id'] for row in store.list('artifact-two', 'child-primary-4')['items']} == {row['review_id'] for row in remaining}
            with store._connect() as connection:
                assert connection.execute("SELECT COUNT(*) FROM ocr_review_event WHERE artifact_id='artifact-one'").fetchone()[0] == 0
                assert not connection.execute("SELECT 1 FROM ocr_review_batch WHERE client_action_id='mixed-artifact-batch'").fetchone()


@pytest.mark.parametrize('scope', ['learning', 'media', 'all_history'])
def test_actual_app_export_and_delete_include_managed_and_retained_legacy_reviews(tmp_path, scope):
    legacy = OcrReviewQueue(tmp_path / 'ocr-review-queue.db')
    rows = seed(legacy)
    other = seed(legacy, 'child-junior-3', 'other')
    legacy.batch([row['review_id'] for row in rows], 'child-primary-4', 'skip', 'old-batch')
    settings = Settings(data_dir=tmp_path, database_path=tmp_path / 'family.db', maintenance_enabled=False)
    with TestClient(create_app(settings)) as client:
        token = client.post('/v1/parent/setup', json={'pin': '2468'}).json()['parent_token']
        headers = {'X-HSC-Parent': token}
        queue = client.app.state.ocr_review_queue
        assert queue.path == settings.database_path
        export = client.app.state.privacy_lifecycle.export_child('child-primary-4')
        payload = json.loads(Path(export['path']).read_text())
        assert {row['review_id'] for row in payload['ocr_review_items']} == {row['review_id'] for row in rows}
        assert len(payload['ocr_review_batches']) == 1
        assert payload['ocr_review_events']
        result = client.post('/v1/parent/children/child-primary-4/delete', headers=headers, json={
            'scope': scope, 'confirmation': '确认删除', 'client_action_id': f'ocr-delete-{scope}'})
        assert result.status_code == 200, result.text
        assert result.json()['status'] == 'completed'
        assert queue.pending('child-primary-4')['items'] == []
        assert queue.recent('child-primary-4')['items'] == []
        assert legacy.pending('child-primary-4')['items'] == []
        assert legacy.recent('child-primary-4')['items'] == []
        assert {row['review_id'] for row in queue.pending('child-junior-3')['items']} == {row['review_id'] for row in other}
        assert {row['review_id'] for row in legacy.pending('child-junior-3')['items']} == {row['review_id'] for row in other}
        with queue.database.connect() as connection:
            assert connection.execute('SELECT COUNT(*) FROM ocr_review_batch WHERE child_id=?', ('child-primary-4',)).fetchone()[0] == 0
