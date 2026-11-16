"""Recognize an already publishable cloud page before changing its metadata."""
from hashlib import sha256
import json

from .daily_visual_evidence import cloud_page_candidate, cloud_page_dependency_bytes


def is_report_cloud_page(connection, artifact):
    """Caller owns a SQLite transaction; never call after opening a network request."""
    text = artifact['extracted_text']
    if (artifact['artifact_type'] != 'document_ocr' or artifact['status'] not in ('completed', 'quality_warning')
            or not isinstance(text, str) or text.strip() or not cloud_page_candidate(artifact['details_json'])):
        return False
    return bool(connection.execute('''SELECT 1 FROM document_page_revision
        WHERE artifact_id=? AND child_id=? AND session_id=? AND content_sha256=? LIMIT 1''',
        (artifact['artifact_id'], artifact['child_id'], artifact['session_id'], sha256(b'').hexdigest())).fetchone())


def persist_monitor_details(database, session_id, artifact_id, changes, *, baseline):
    """Merge this capture's fields against its original read; preserve other writers."""
    allowed = {'monitor_frame_sha256', 'monitor_generation', 'monitor_capture_phase', 'cloud_vision_fallback'}
    if not isinstance(changes, dict) or not isinstance(baseline, dict) or not set(changes) <= allowed:
        raise ValueError('DOCUMENT_OCR_MONITOR_FIELDS_INVALID')

    def value(obj, key):
        return json.dumps([key in obj, obj.get(key)], ensure_ascii=False, sort_keys=True,
                          separators=(',', ':'), allow_nan=False)

    def publish(*, guarded):
        with database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('SELECT * FROM capture_artifact WHERE artifact_id=? AND session_id=?',
                            (artifact_id, session_id)).fetchone()
            if not row:
                raise ValueError('DOCUMENT_OCR_SOURCE_UNAVAILABLE')
            current = json.loads(row['details_json'] or '{}')
            if not isinstance(current, dict):
                raise ValueError('DOCUMENT_OCR_SOURCE_INVALID')
            merged = dict(current)
            publication = None
            for key, proposed in changes.items():
                before, live = value(baseline, key), value(current, key)
                target = value({key: proposed}, key)
                if live != before and live != target:
                    if key != 'cloud_vision_fallback':
                        raise ValueError('DOCUMENT_OCR_MONITOR_SOURCE_CHANGED')
                    # A different cloud result won the race. Keep its complete
                    # receipt and privacy object; never claim our result saved.
                    publication = {'status': 'existing_preserved_conflict'}
                    continue
                merged[key] = proposed
                if key == 'cloud_vision_fallback':
                    publication = {'status': 'applied'}
            if publication is not None:
                merged['monitor_cloud_publication'] = publication
            encoded = json.dumps(merged, ensure_ascii=False)
            if (not guarded and is_report_cloud_page(c, row)
                    and cloud_page_dependency_bytes(encoded) != cloud_page_dependency_bytes(row['details_json'])):
                c.rollback()
                return None
            c.execute('UPDATE capture_artifact SET details_json=? WHERE artifact_id=? AND session_id=?',
                      (encoded, artifact_id, session_id))
            c.commit()
            return {'details': merged, 'cloud_candidate_publication': publication}
    result = publish(guarded=False)
    if result is not None:
        return result
    with database.external_effect_guard(timeout=2):
        return publish(guarded=True)
