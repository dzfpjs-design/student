"""Read the frozen ordinary page, never a latest-page lookup or score grant."""
import hashlib
import json
from pathlib import Path
import re
from cryptography.exceptions import InvalidTag

from .document_monitoring import DocumentMonitoringService
from .paper_ocr_metadata import validate_page_source_sidecar


def validate_reference(c, session_id, child_id, binding, *, page_source_store=None):
    if (not isinstance(binding, dict) or set(binding) != {
            'revision_id', 'artifact_id', 'content_sha256', 'page_sha256'}
            or any(not isinstance(binding[k], str) or not re.fullmatch('[0-9a-f]{64}', binding[k])
                   for k in ('content_sha256', 'page_sha256'))):
        raise ValueError('ANSWER_VOICE_CAPTURE_REFERENCE_INVALID')
    row = c.execute('''SELECT r.*,a.child_id AS artifact_child,a.session_id AS artifact_session,
        a.artifact_type,a.status AS artifact_status,a.extracted_text,a.details_json,a.local_path
        FROM document_page_revision r JOIN capture_artifact a ON a.artifact_id=r.artifact_id
        WHERE r.revision_id=?''', (binding['revision_id'],)).fetchone()
    if row is None:
        raise ValueError('ANSWER_VOICE_CAPTURE_REFERENCE_UNAVAILABLE')
    if (row['child_id'], row['session_id'], row['artifact_child'], row['artifact_session']) != (
            child_id, session_id, child_id, session_id):
        raise ValueError('ANSWER_VOICE_CAPTURE_OWNER_CHANGED')
    try:
        details = json.loads(row['details_json'])
        if not isinstance(details, dict):
            raise ValueError('page_details_invalid')
        validate_page_source_sidecar(dict(row), details)
        text = DocumentMonitoringService._normalized_text(row['extracted_text'] or '')
        source = details['page_source']
        if (row['artifact_id'] != binding['artifact_id']
                or row['artifact_status'] not in {'completed', 'quality_warning'}
                or not text or row['content_sha256'] != binding['content_sha256']
                or hashlib.sha256(text.encode('utf-8')).hexdigest() != binding['content_sha256']
                or source['sha256'] != binding['page_sha256']
                or not row['local_path'] or not Path(row['local_path']).is_file()):
            raise ValueError('page_source_changed')
        if page_source_store is not None:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            from .pet_media_files import read_managed_media
            blob = read_managed_media(row['local_path'], media_root=page_source_store.data_dir / 'media',
                                      max_bytes=24 * 1024 * 1024)
            if not blob.startswith(b'HSC1') or len(blob) < 32:
                raise ValueError('page_encryption_invalid')
            original = AESGCM(page_source_store.data_encryption_key).decrypt(
                blob[4:16], blob[16:], b'hsc-raw-media-v1')
            if (len(original) != source['byte_length']
                    or hashlib.sha256(original).hexdigest() != binding['page_sha256']):
                raise ValueError('page_bytes_changed')
    except (ValueError, TypeError, KeyError, OSError, InvalidTag) as error:
        # Local corruption must not discard the child's entire utterance or
        # attach it to another page. No image/path is returned to the caller.
        raise ValueError('ANSWER_VOICE_CAPTURE_TARGET_CHANGED') from error
    return {'kind': 'document', 'binding': dict(binding)}


def read_for_turn(database, page_source_store, session_id, turn_id):
    from .voice_capture_binding import resolve_voice_capture
    with database.connect() as c:
        c.execute('BEGIN')
        row = c.execute('''SELECT t.* FROM agent_turn t JOIN daily_session ds
            ON ds.session_id=t.session_id AND ds.child_id=t.child_id
            WHERE t.agent_turn_id=? AND t.session_id=? AND ds.status='active' AND ds.state='study' ''',
            (turn_id, session_id)).fetchone()
        if row is None:
            raise ValueError('DOCUMENT_VOICE_SOURCE_UNAVAILABLE')
        action = json.loads(row['action_json'])
        if action.get('type') != 'control_document_monitor' or action.get('command') != 'review' or not action.get('voice_input_id'):
            raise ValueError('DOCUMENT_VOICE_SOURCE_UNAVAILABLE')
        capture = resolve_voice_capture(c, session_id=session_id, child_id=row['child_id'],
            voice_input_id=action['voice_input_id'], transcript=row['transcript'], page_source_store=page_source_store)
        reference = capture['question_reference']
        if (capture['question_validation'] != 'valid' or not reference or reference['kind'] != 'document'
                or reference != (action.get('voice_input_binding') or {}).get('question_reference')):
            raise ValueError('DOCUMENT_VOICE_SOURCE_CHANGED')
        result = DocumentMonitoringService._revision(c, reference['binding']['revision_id'])
        details = json.loads(c.execute('SELECT details_json FROM capture_artifact WHERE artifact_id=?',
                                      (reference['binding']['artifact_id'],)).fetchone()[0])
        fallback = details.get('cloud_vision_fallback') or {}
        blocked = (fallback.get('state') if fallback.get('status') is None else fallback.get('status')) == 'blocked'
        page_status = 'blocked' if blocked else 'text_available' if result['extracted_text'].strip() else 'empty'
        result.pop('intervention', None)
        return {**result, 'local_only': True, 'cloud_transmission_allowed': False,
                'mastery_update_allowed': False, 'source_voice_input_id': capture['voice_input_id'], 'page_status': page_status}
