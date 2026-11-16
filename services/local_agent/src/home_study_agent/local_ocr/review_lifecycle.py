"""Managed OCR review migration and deletion; source copies never re-import on restore."""
from __future__ import annotations
from pathlib import Path
import os
import sqlite3
import tempfile


TABLES = ('ocr_review_item', 'ocr_review_event', 'ocr_review_batch')
MARKER = b'hsc-ocr-review-imported-v1\n'


def migrate_review_store(database, legacy_path: Path) -> None:
    legacy_path = Path(legacy_path)
    if legacy_path.resolve() == database.path.resolve() or not legacy_path.is_file():
        return
    marker = legacy_path.with_name(legacy_path.name + '.imported')
    if marker.is_symlink():
        raise ValueError('OCR migration marker must be a regular file')
    if marker.exists():
        if marker.read_bytes() != MARKER:
            raise ValueError('OCR migration marker is invalid')
        return
    with database.connect() as target:
        target.execute('CREATE TABLE IF NOT EXISTS ocr_review_legacy_import(source_path TEXT PRIMARY KEY)')
        target.commit()
        imported = target.execute('SELECT 1 FROM ocr_review_legacy_import WHERE source_path=?',
                                  (str(legacy_path.resolve()),)).fetchone()
        if not imported:
            source = sqlite3.connect(legacy_path.resolve().as_uri() + '?mode=ro', uri=True)
            source.row_factory = sqlite3.Row
            try:
                source.execute('BEGIN')
                target.execute('BEGIN IMMEDIATE')
                available = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for table in TABLES:
                    if table not in available:
                        continue
                    source_columns = {row[1] for row in source.execute(f'PRAGMA table_info({table})')}
                    info = list(target.execute(f'PRAGMA table_info({table})'))
                    columns = [row['name'] for row in info if row['name'] in source_columns]
                    keys = [row['name'] for row in info if row['pk']]
                    if not keys or any(key not in columns for key in keys):
                        raise ValueError('OCR legacy schema lacks stable identity')
                    for row in source.execute(f'SELECT {",".join(columns)} FROM {table}'):
                        existing = target.execute(f'SELECT {",".join(columns)} FROM {table} WHERE '
                            + ' AND '.join(f'{key}=?' for key in keys), tuple(row[key] for key in keys)).fetchone()
                        if existing:
                            if any(existing[column] != row[column] for column in columns):
                                raise ValueError('OCR legacy migration identity conflict')
                        else:
                            target.execute(f'INSERT INTO {table} ({",".join(columns)}) VALUES '
                                f'({",".join("?" for _ in columns)})', tuple(row[column] for column in columns))
                target.execute('INSERT INTO ocr_review_legacy_import VALUES(?)', (str(legacy_path.resolve()),))
                target.commit()
            except Exception:
                target.rollback()
                raise
            finally:
                source.close()
    # This non-content marker survives restoring an older main database. The
    # original source stays available for explicit recovery, but cannot resurrect
    # old reviews automatically. Privacy deletion purges it before completion.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=marker.parent, prefix=marker.name + '.', delete=False) as output:
            temporary = Path(output.name)
            output.write(MARKER)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, marker)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def delete_review_scope(connection, child_id: str, artifact_id: str | None = None) -> dict:
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'ocr_review_item' not in tables:
        return {}
    clause, values = ('child_id=?', (child_id,)) if artifact_id is None else ('child_id=? AND artifact_id=?', (child_id, artifact_id))
    artifacts = {row[0] for row in connection.execute(f'SELECT DISTINCT artifact_id FROM ocr_review_item WHERE {clause}', values)}
    if artifact_id is not None:
        artifacts.add(artifact_id)
    elif 'capture_artifact' in tables:
        artifacts.update(row[0] for row in connection.execute('SELECT artifact_id FROM capture_artifact WHERE child_id=?', (child_id,)))
    ids = [row[0] for row in connection.execute(f'SELECT review_id FROM ocr_review_item WHERE {clause}', values)]
    counts = {'ocr_review_event': 0, 'ocr_review_batch': 0}
    if 'ocr_review_event' in tables:
        for artifact in artifacts:
            counts['ocr_review_event'] += connection.execute('DELETE FROM ocr_review_event WHERE artifact_id=?', (artifact,)).rowcount
    if 'ocr_review_batch' in tables:
        if artifact_id is None:
            counts['ocr_review_batch'] = connection.execute('DELETE FROM ocr_review_batch WHERE child_id=?', (child_id,)).rowcount
        else:
            for review_id in ids:
                counts['ocr_review_batch'] += connection.execute("""DELETE FROM ocr_review_batch WHERE child_id=?
                    AND EXISTS (SELECT 1 FROM json_each(ocr_review_batch.request_json,'$.review_ids') WHERE value=?)""", (child_id, review_id)).rowcount
    counts['ocr_review_item'] = connection.execute(f'DELETE FROM ocr_review_item WHERE {clause}', values).rowcount
    return counts


def purge_legacy_review_scope(data_dir: Path, child_id: str, artifact_id: str | None = None) -> dict:
    path = Path(data_dir) / 'ocr-review-queue.db'
    if not path.is_file():
        return {}
    connection = sqlite3.connect(path)
    try:
        connection.execute('BEGIN IMMEDIATE')
        counts = delete_review_scope(connection, child_id, artifact_id)
        connection.commit()
        return counts
    finally:
        connection.close()
