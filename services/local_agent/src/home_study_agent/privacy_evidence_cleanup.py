"""Borrowed-transaction erasure of source-bound private review copies.

PrivacyLifecycle owns authorization, source deletion, external-effect locking,
recovery-point rotation and commit. This module never opens another connection.
"""
import json
import sqlite3
from hashlib import sha256


class PrivacyEvidenceCleanupError(ValueError):
    pass


def _exists(c, table):
    return bool(c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def _object(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate key')
            result[key] = value
        return result
    try:
        value = json.loads(raw, object_pairs_hook=unique)
        if not isinstance(value, dict):
            raise ValueError('object required')
        return value
    except (ValueError, TypeError) as error:
        raise PrivacyEvidenceCleanupError('PRIVACY_EVIDENCE_JSON_INVALID') from error


def _ids(values):
    if isinstance(values, (str, bytes)):
        raise PrivacyEvidenceCleanupError('PRIVACY_EVIDENCE_SCOPE_INVALID')
    result = set(values)
    if any(not isinstance(v, str) or not v for v in result):
        raise PrivacyEvidenceCleanupError('PRIVACY_EVIDENCE_SCOPE_INVALID')
    return result


def _transaction(c):
    if not c.in_transaction:
        raise PrivacyEvidenceCleanupError('PRIVACY_EVIDENCE_TRANSACTION_REQUIRED')


def _audio_id(payload):
    audio = payload.get('audio_evidence')
    if audio is None:
        return None
    if not isinstance(audio, dict) or not isinstance(audio.get('media_id'), str) or not audio['media_id']:
        raise PrivacyEvidenceCleanupError('PRIVACY_EVIDENCE_JSON_INVALID')
    return audio['media_id']


def purge_rubric_sources(c, child_id, *, media_ids=(), diagnostic_item_ids=(), source_evidence_ids=(), all_child_audio=False,
                         reconcile=None, deferred_reconciliation=None):
    """Call before deleting media bindings/items, inside BEGIN IMMEDIATE.

The caller selects real IDs, not quotes or inferred subjects. A matching source
removes the entire assessment review chain, so an older approval cannot reappear.
    ``reconcile(child_id, connection=c)`` is the existing runtime coordinator.
    ``all_child_audio`` is only for an explicitly authorized whole-child media
    scope, including legacy audio references whose binding was already erased.
    ``deferred_reconciliation`` is an internal set owned by the outer deletion
    transaction. It defers gate consumption until all source copies are cleaned.
"""
    _transaction(c)
    if type(all_child_audio) is not bool:
        raise PrivacyEvidenceCleanupError('PRIVACY_EVIDENCE_SCOPE_INVALID')
    media, items, evidence = map(_ids, (media_ids, diagnostic_item_ids, source_evidence_ids))
    counts = {'rubric_review_event': 0, 'rubric_assessment': 0}
    tables = [_exists(c, table) for table in counts]
    if not any(tables):
        return counts
    if not all(tables):
        raise PrivacyEvidenceCleanupError('PRIVACY_EVIDENCE_SCHEMA_INCOMPLETE')
    if not (media or items or evidence or all_child_audio):
        return counts
    for table, field, ids in [('pet_media_binding', 'media_id', media), ('learning_evidence', 'evidence_id', evidence)]:
        for source_id in ids:
            if not _exists(c, table) or not c.execute(f'SELECT 1 FROM {table} WHERE {field}=? AND child_id=?', (source_id, child_id)).fetchone():
                raise PrivacyEvidenceCleanupError('PRIVACY_EVIDENCE_SOURCE_NOT_OWNED')
    def matches_audio(payload):
        media_id = _audio_id(payload)
        if media_id is None:
            return False
        if all_child_audio and _exists(c, 'pet_media_binding'):
            owner = c.execute('SELECT child_id FROM pet_media_binding WHERE media_id=?', (media_id,)).fetchone()
            if owner and owner[0] != child_id:
                raise PrivacyEvidenceCleanupError('PRIVACY_EVIDENCE_OWNER_MISMATCH')
        return all_child_audio or media_id in media

    targets = set()
    assessments = {row['assessment_id']: row for row in c.execute('SELECT * FROM rubric_assessment WHERE child_id=?', (child_id,))}
    for aid, row in assessments.items():
        payload = _object(row['payload_json'])
        material = payload.get('material') or {}
        if not isinstance(material, dict):
            raise PrivacyEvidenceCleanupError('PRIVACY_EVIDENCE_JSON_INVALID')
        item_id, evidence_id = material.get('diagnostic_item_id'), payload.get('source_evidence_id')
        if any(v is not None and not isinstance(v, str) for v in (item_id, evidence_id)):
            raise PrivacyEvidenceCleanupError('PRIVACY_EVIDENCE_JSON_INVALID')
        audio_match = matches_audio(payload)
        if item_id in items or evidence_id in evidence or audio_match:
            targets.add(aid)
    for row in c.execute('''SELECT e.* FROM rubric_review_event e LEFT JOIN rubric_assessment a USING(assessment_id)
                           WHERE e.child_id=? OR a.child_id=?''', (child_id, child_id)):
        if row['child_id'] != child_id or row['assessment_id'] not in assessments:
            raise PrivacyEvidenceCleanupError('PRIVACY_EVIDENCE_OWNER_MISMATCH')
        # Both immutable JSON copies can retain original-audio annotations.
        payload, request = _object(row['payload_json']), _object(row['request_json'])
        payload_matches, request_matches = matches_audio(payload), matches_audio(request)
        if payload_matches or request_matches:
            targets.add(row['assessment_id'])
    if not targets:
        return counts
    if deferred_reconciliation is not None and not isinstance(deferred_reconciliation, set):
        raise PrivacyEvidenceCleanupError('PRIVACY_EVIDENCE_SCOPE_INVALID')
    if not callable(reconcile) and deferred_reconciliation is None:
        raise PrivacyEvidenceCleanupError('PRIVACY_EVIDENCE_RECONCILE_REQUIRED')
    c.execute('SAVEPOINT privacy_rubric_erasure')
    try:
        for aid in sorted(targets):
            counts['rubric_review_event'] += c.execute('DELETE FROM rubric_review_event WHERE assessment_id=?', (aid,)).rowcount
            counts['rubric_assessment'] += c.execute('DELETE FROM rubric_assessment WHERE assessment_id=? AND child_id=?', (aid, child_id)).rowcount
        if deferred_reconciliation is None:
            reconcile(child_id, connection=c)
        c.execute('RELEASE SAVEPOINT privacy_rubric_erasure')
    except BaseException as error:
        c.execute('ROLLBACK TO SAVEPOINT privacy_rubric_erasure')
        c.execute('RELEASE SAVEPOINT privacy_rubric_erasure')
        if isinstance(error, (sqlite3.Error, OSError)):
            raise PrivacyEvidenceCleanupError('PRIVACY_EVIDENCE_STORAGE_FAILURE') from error
        raise
    if deferred_reconciliation is not None:
        deferred_reconciliation.add(child_id)
    return counts


def parent_textbook_sources(c, child_id, *, artifact_ids=None, source_refs=None):
    """Read exact owned source identities before PrivacyLifecycle removes links.

None selects all this child's captures; an empty collection selects none.
Returned identities are internal cleanup inputs, not family-facing text/paths.
"""
    artifacts = None if artifact_ids is None else _ids(artifact_ids)
    sources = None if source_refs is None else _ids(source_refs)
    if not _exists(c, 'kg_parent_textbook_ocr_artifact'):
        return []
    result = []
    for row in c.execute('SELECT * FROM kg_parent_textbook_ocr_artifact WHERE child_id=? ORDER BY artifact_id', (child_id,)):
        aid = row['artifact_id']
        ref = 'local_parent_textbook_capture:textbook-ocr-' + sha256(aid.encode()).hexdigest()[:24]
        if (artifacts is not None and aid not in artifacts) or (sources is not None and ref not in sources):
            continue
        for column in ('page_context_json', 'details_json', 'import_request_json'):
            if row[column] is not None:
                _object(row[column])
        for event in c.execute('SELECT payload_json FROM kg_parent_textbook_ocr_review_event WHERE artifact_id=?', (aid,)):
            _object(event[0])
        resources = [r[0] for r in c.execute("SELECT resource_id FROM kg_resource WHERE corpus_id='family-textbook-ocr-pages-v1' AND source_ref=? ORDER BY resource_id", (ref,))] if _exists(c, 'kg_resource') else []
        result.append(dict(artifact_id=aid, lease_id=row['lease_id'], source_ref=ref, resource_ids=resources))
    if artifacts is not None and {r['artifact_id'] for r in result} != artifacts:
        raise PrivacyEvidenceCleanupError('PRIVACY_EVIDENCE_SOURCE_NOT_OWNED')
    return result


def purge_parent_textbook_sources(c, child_id, *, artifact_ids=None, source_refs=None, purge_imported=None):
    """Erase selected OCR journals; imported source bodies need a coordinated purge.

``purge_imported(c, child_id, sources)`` must remove the exact KG resources and
their downstream references, not merely mark them retired while retaining text.
The savepoint also rolls back callback writes on failure. No optional schema init.
"""
    from .local_ocr.cache_lifecycle import delete_scope
    _transaction(c)
    sources = parent_textbook_sources(c, child_id, artifact_ids=artifact_ids, source_refs=source_refs)
    if any(c.execute("SELECT 1 FROM kg_parent_textbook_ocr_artifact WHERE artifact_id=? AND status='importing'", (row['artifact_id'],)).fetchone() for row in sources):
        raise PrivacyEvidenceCleanupError('PRIVACY_TEXTBOOK_IMPORT_IN_PROGRESS')
    imported = [row for row in sources if row['resource_ids']]
    if imported and not callable(purge_imported):
        raise PrivacyEvidenceCleanupError('PRIVACY_TEXTBOOK_IMPORT_CLEANUP_REQUIRED')
    counts = {'kg_parent_textbook_ocr_review_event': 0, 'kg_parent_textbook_ocr_artifact': 0,
              'kg_parent_textbook_capture_lease': 0, 'ocr_jobs': 0, 'ocr_cache': 0}
    c.execute('SAVEPOINT privacy_textbook_erasure')
    try:
        if imported:
            imported_counts = purge_imported(c, child_id, imported)
            if any(c.execute('SELECT 1 FROM kg_resource WHERE resource_id=?', (rid,)).fetchone()
                   for row in imported for rid in row['resource_ids']):
                raise PrivacyEvidenceCleanupError('PRIVACY_TEXTBOOK_IMPORTED_BODY_REMAINS')
            if not isinstance(imported_counts, dict) or any(type(n) is not int or n < 0 for n in imported_counts.values()):
                raise PrivacyEvidenceCleanupError('PRIVACY_TEXTBOOK_CLEANUP_RECEIPT_INVALID')
            for table, count in imported_counts.items():
                counts[table] = counts.get(table, 0) + count
        for row in sources:
            file_source = c.execute('SELECT file_source_id FROM kg_parent_textbook_ocr_artifact WHERE artifact_id=?', (row['artifact_id'],)).fetchone()
            removed = delete_scope(c, child_id, parent_artifact_id=row['artifact_id'])
            for name, count in removed.items():
                counts[name] += count
            counts['kg_parent_textbook_ocr_review_event'] += c.execute('DELETE FROM kg_parent_textbook_ocr_review_event WHERE artifact_id=?', (row['artifact_id'],)).rowcount
            counts['kg_parent_textbook_ocr_artifact'] += c.execute('DELETE FROM kg_parent_textbook_ocr_artifact WHERE artifact_id=? AND child_id=?', (row['artifact_id'], child_id)).rowcount
            if file_source and file_source[0]:
                counts['kg_parent_textbook_file_source'] = counts.get('kg_parent_textbook_file_source',0) + c.execute(
                    'DELETE FROM kg_parent_textbook_file_source WHERE file_source_id=? AND child_id=?', (file_source[0],child_id)).rowcount
        if _exists(c, 'kg_parent_textbook_capture_lease'):
            for lease_id in {row['lease_id'] for row in sources}:
                counts['kg_parent_textbook_capture_lease'] += c.execute('''DELETE FROM kg_parent_textbook_capture_lease WHERE lease_id=? AND child_id=?
                    AND NOT EXISTS(SELECT 1 FROM kg_parent_textbook_ocr_artifact WHERE lease_id=?)''', (lease_id, child_id, lease_id)).rowcount
        c.execute('RELEASE SAVEPOINT privacy_textbook_erasure')
    except BaseException as error:
        c.execute('ROLLBACK TO SAVEPOINT privacy_textbook_erasure')
        c.execute('RELEASE SAVEPOINT privacy_textbook_erasure')
        if isinstance(error, (sqlite3.Error, OSError)):
            raise PrivacyEvidenceCleanupError('PRIVACY_EVIDENCE_STORAGE_FAILURE') from error
        raise
    return counts
