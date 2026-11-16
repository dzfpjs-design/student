from __future__ import annotations

import hashlib
from pathlib import Path
import sqlite3

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import pytest

from home_study_agent.database import Database
from home_study_agent.knowledge_graph.parent_textbook_drafts import ParentTextbookDraftService
from home_study_agent.privacy_lifecycle import InvalidBackupError, PrivacyLifecycleService


OLD_KEY = b'o' * 32
NEW_KEY = b'n' * 32
PASSPHRASE = 'public synthetic portable backup passphrase'


def _store(root: Path, key: bytes):
    root.mkdir()
    database = Database(root / 'agent.db', media_root=root, data_encryption_key=key)
    database.initialize()
    with database.connect() as connection:
        for child in ('public-child-a', 'public-child-b'):
            connection.execute('''INSERT INTO child_profile
                (child_id,nickname,grade,curriculum_region,status,created_at,updated_at)
                VALUES(?,?, '4','public','active',datetime('now'),datetime('now'))''', (child, child))
        connection.commit()
    drafts = ParentTextbookDraftService(database)
    return database, drafts, PrivacyLifecycleService(database, root, key)


def _payload(label='public synthetic draft'):
    return {'page': {'title': label, 'ocr_text': label}, 'capture_ref': '',
            'capture_version': '', 'review_drafts': {}, 'action_ids': {}}


def _save(drafts, child, scope, revision, action, payload):
    generation = drafts.read(child, scope)['storage_generation']
    return drafts.save(child, scope, revision, action, payload, expected_generation=generation)


def _rows(database):
    with database.connect() as connection:
        return [dict(row) for row in connection.execute(
            'SELECT * FROM kg_parent_textbook_form_draft ORDER BY child_id,scope')]


def _public_without_generation(value):
    return {key: item for key, item in value.items() if key != 'storage_generation'}


def _damage(database, kind):
    with database.connect() as connection:
        row = connection.execute('''SELECT * FROM kg_parent_textbook_form_draft
            WHERE child_id='public-child-b' AND scope='capture' ''').fetchone()
        data = bytes(row['encrypted_payload'])
        if kind == 'header':
            field, value = 'encrypted_payload', b'BAD1' + data[4:]
        elif kind == 'cipher':
            field, value = 'encrypted_payload', data[:-1] + bytes([data[-1] ^ 1])
        elif kind == 'child':
            field, value = 'child_id', 'public-child-a'
            connection.execute("DELETE FROM kg_parent_textbook_form_draft WHERE child_id='public-child-a'")
        elif kind == 'scope':
            field, value = 'scope', 'language'
        elif kind == 'revision':
            field, value = 'revision', row['revision'] + 1
        elif kind == 'checksum':
            field, value = 'payload_sha256', '0' * 64
        elif kind == 'wrong_key':
            plaintext = ParentTextbookDraftService._bytes(_payload('second public draft'))
            nonce = b'k' * 12
            field, value = 'encrypted_payload', b'HSD1' + nonce + AESGCM(b'x' * 32).encrypt(
                nonce, plaintext, ParentTextbookDraftService._aad(row['child_id'], row['scope'], row['revision']))
        else:
            raise AssertionError('unknown synthetic damage')
        connection.execute(f'''UPDATE kg_parent_textbook_form_draft SET {field}=?
            WHERE child_id='public-child-b' AND scope='capture' ''', (value,))
        connection.commit()


def test_portable_restore_preserves_draft_cas_tombstone_and_edit_restart(tmp_path):
    source_db, source_drafts, source = _store(tmp_path / 'source', OLD_KEY)
    first = _save(source_drafts, 'public-child-a', 'capture', 0, 'public-source-save-a', _payload())
    _save(source_drafts, 'public-child-a', 'language', 0, 'public-language-save', _payload('language'))
    tombstone = _save(source_drafts, 'public-child-a', 'language', 1, 'public-language-clear', None)
    other = _save(source_drafts, 'public-child-b', 'capture', 0, 'public-source-save-b', _payload('second public draft'))
    before = _rows(source_db)
    archive = Path(source.create_portable_backup(PASSPHRASE)['path'])
    archive_sha = hashlib.sha256(archive.read_bytes()).hexdigest()
    target_db, target_drafts, target = _store(tmp_path / 'target', NEW_KEY)

    restored = target.restore_portable_backup(str(archive), PASSPHRASE, '确认迁移恢复')
    assert restored['status'] == 'restored'
    restored_first = target_drafts.read('public-child-a', 'capture')
    after = _rows(target_db)
    assert len(before) == len(after)
    for old, new in zip(before, after):
        assert {key: value for key, value in old.items() if key != 'encrypted_payload'} == {
            key: value for key, value in new.items() if key != 'encrypted_payload'}
        if old['encrypted_payload'] is None:
            assert new['encrypted_payload'] is None
        else:
            assert old['encrypted_payload'] != new['encrypted_payload']
            assert b'public synthetic draft' not in new['encrypted_payload']
            aad = ParentTextbookDraftService._aad(new['child_id'], new['scope'], new['revision'])
            data = bytes(new['encrypted_payload'])
            with pytest.raises(Exception):
                AESGCM(OLD_KEY).decrypt(data[4:16], data[16:], aad)
    assert _public_without_generation(restored_first) == _public_without_generation(first)
    assert _public_without_generation(target_drafts.read('public-child-a', 'language')) == _public_without_generation(tombstone)
    assert _public_without_generation(target_drafts.read('public-child-b', 'capture')) == _public_without_generation(other)
    assert _save(target_drafts, 'public-child-a', 'capture', 0, 'public-source-save-a', _payload()) == target_drafts.read('public-child-a', 'capture')
    updated = _save(target_drafts, 'public-child-a', 'capture', 1, 'public-after-restore-edit', _payload('updated public draft'))
    restarted = ParentTextbookDraftService(Database(target_db.path, media_root=tmp_path / 'target', data_encryption_key=NEW_KEY))
    assert restarted.read('public-child-a', 'capture') == updated
    assert _save(restarted, 'public-child-a', 'capture', 1, 'public-after-restore-edit', _payload('updated public draft')) == updated
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == archive_sha
    assert _rows(source_db) == before


@pytest.mark.parametrize('kind', ['header', 'cipher', 'child', 'scope', 'revision', 'checksum', 'wrong_key'])
def test_prepare_rejects_corrupt_draft_and_rolls_back_all_db_rekey(tmp_path, kind):
    source_db, drafts, source = _store(tmp_path / 'source', OLD_KEY)
    _save(drafts, 'public-child-a', 'capture', 0, 'public-a', _payload())
    _save(drafts, 'public-child-b', 'capture', 0, 'public-b', _payload('second public draft'))
    _damage(source_db, kind)
    original_rows = _rows(source_db)
    snapshot = Path(source.create_backup()['path'])
    with sqlite3.connect(snapshot) as connection:
        before = connection.execute('SELECT * FROM kg_parent_textbook_form_draft ORDER BY child_id,scope').fetchall()
    target_db, target_drafts, target = _store(tmp_path / 'target', NEW_KEY)
    retained = _save(target_drafts, 'public-child-a', 'capture', 0, 'public-retained-work', _payload('retained public draft'))
    target_rows = _rows(target_db)

    with pytest.raises(InvalidBackupError, match='草稿'):
        target._prepare_portable_snapshot(snapshot, OLD_KEY, NEW_KEY)

    with sqlite3.connect(snapshot) as connection:
        assert connection.execute('SELECT * FROM kg_parent_textbook_form_draft ORDER BY child_id,scope').fetchall() == before
    assert _rows(source_db) == original_rows
    assert _rows(target_db) == target_rows
    assert target_drafts.read('public-child-a', 'capture') == retained


def test_corrupt_portable_restore_preserves_live_work_archive_and_recovery_point(tmp_path):
    source_db, drafts, source = _store(tmp_path / 'source', OLD_KEY)
    _save(drafts, 'public-child-b', 'capture', 0, 'public-b', _payload('second public draft'))
    _damage(source_db, 'cipher')
    archive = Path(source.create_portable_backup(PASSPHRASE)['path'])
    archive_sha = hashlib.sha256(archive.read_bytes()).hexdigest()
    target_db, target_drafts, target = _store(tmp_path / 'target', NEW_KEY)
    retained = _save(target_drafts, 'public-child-a', 'capture', 0, 'public-retained-work', _payload('retained public draft'))
    before = _rows(target_db)

    with pytest.raises(InvalidBackupError, match='草稿'):
        target.restore_portable_backup(str(archive), PASSPHRASE, '确认迁移恢复')

    assert _rows(target_db) == before
    assert target_drafts.read('public-child-a', 'capture') == retained
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == archive_sha
    with target_db.connect() as connection:
        recovery = connection.execute("SELECT path FROM backup_manifest WHERE reason='restore_recovery_point'").fetchone()
    assert recovery is not None and Path(recovery['path']).is_file()
    with sqlite3.connect(recovery['path']) as connection:
        connection.row_factory = sqlite3.Row
        assert [dict(row) for row in connection.execute('SELECT * FROM kg_parent_textbook_form_draft ORDER BY child_id,scope')] == before
