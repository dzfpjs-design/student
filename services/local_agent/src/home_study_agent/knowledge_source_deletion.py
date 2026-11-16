"""Exact native-source cleanup support; no independent jobs or commits."""
import json
import hashlib
from datetime import datetime

from .privacy_evidence_cleanup import PrivacyEvidenceCleanupError, _exists
from .privacy_native_sources import _linked


def export_copies(data_dir, child_id, source_id=None, *, connection, include_hashes=False):
    """Composite managed exports must be invalidated, not silently edited."""
    copies = {}
    try:
        audit_hashes = {json.loads(row[0]).get('sha256') for row in connection.execute(
            "SELECT summary_json FROM audit_event WHERE child_id=? AND actor='parent' AND action='child_data_exported'", (child_id,))}
        root = (data_dir / 'exports').resolve()
        if root.parent != data_dir.resolve() or (data_dir / 'exports').is_symlink():
            raise PrivacyEvidenceCleanupError('PRIVACY_EVIDENCE_EXPORT_UNVERIFIABLE')
        for path in root.glob('child-export-*.json'):
            if not path.name.startswith('child-export-' + child_id + '-'):
                continue
            if path.is_symlink() or path.resolve().parent != root or path.stat().st_size > 256 * 1024 * 1024:
                raise PrivacyEvidenceCleanupError('PRIVACY_EVIDENCE_EXPORT_UNVERIFIABLE')
            content = path.read_bytes()
            sha = hashlib.sha256(content).hexdigest()
            if sha not in audit_hashes:
                continue
            payload = json.loads(content.decode('utf-8'))
            when = datetime.fromisoformat(payload['exported_at'].replace('Z', '+00:00'))
            expected_name = 'child-export-' + child_id + '-' + when.strftime('%Y%m%dT%H%M%SZ') + '.json'
            if (payload.get('export_version') != 'hsc_child_export_v1' or when.tzinfo is None
                    or path.name != expected_name or not isinstance(payload.get('child'), list)
                    or len(payload['child']) != 1 or payload['child'][0].get('child_id') != child_id):
                continue
            if source_id is None or _linked(payload, {source_id}):
                copies[str(path.resolve())] = sha
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        raise PrivacyEvidenceCleanupError('PRIVACY_EVIDENCE_EXPORT_UNVERIFIABLE') from error
    return copies if include_hashes else list(copies)


def verify_export_copy(data_dir, child_id, path, expected_hash):
    """Recheck the registered bytes immediately before residual-file deletion."""
    root = data_dir / 'exports'
    if (root.is_symlink() or root.resolve().parent != data_dir.resolve() or path.is_symlink()
            or path.resolve().parent != root.resolve()
            or not path.name.startswith('child-export-' + child_id + '-') or path.suffix != '.json'):
        raise OSError('export path outside managed root')
    if not path.exists():
        return
    if path.stat().st_size > 256 * 1024 * 1024 or not expected_hash or hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash:
        raise OSError('registered export changed')


def remaining(c, child_id, source_id, manifest):
    counts = {
        'knowledge_source': c.execute('SELECT COUNT(*) FROM knowledge_source WHERE source_id=? AND child_id=?', (source_id, child_id)).fetchone()[0],
        'knowledge_excerpt': c.execute('SELECT COUNT(*) FROM knowledge_excerpt WHERE source_id=?', (source_id,)).fetchone()[0],
        'source_tombstone_missing': int(not c.execute('SELECT 1 FROM privacy_source_erasure WHERE child_id=? AND source_id=?', (child_id, source_id)).fetchone()),
        'published_material_copies': 0, 'question_authorizations': 0, 'prompt_copies': 0,
    }
    for item in manifest.get('item_ids', []):
        if _exists(c, 'writing_question_authorization_event'):
            counts['question_authorizations'] += c.execute('SELECT COUNT(*) FROM writing_question_authorization_event WHERE child_id=? AND diagnostic_item_id=?', (child_id, item)).fetchone()[0]
    for item in manifest.get('erased_item_ids', []):
        row = c.execute('SELECT status,stimulus_text,stimulus_asset FROM diagnostic_item WHERE diagnostic_item_id=?', (item,)).fetchone()
        counts['published_material_copies'] += int(bool(row and (row['status'] != 'retired' or row['stimulus_text'] or row['stimulus_asset'])))
    for cid in manifest.get('candidate_ids', []):
        row = c.execute('SELECT state,payload_json,source_snapshot_json FROM curriculum_supply_package WHERE child_id=? AND candidate_id=?', (child_id, cid)).fetchone()
        counts['published_material_copies'] += int(bool(row and tuple(row) != ('withdrawn', '{}', '{}')))
        counts['published_material_copies'] += c.execute('SELECT COUNT(*) FROM curriculum_supply_event WHERE child_id=? AND candidate_id=?', (child_id, cid)).fetchone()[0]
    for pid in manifest.get('prompt_ids', []):
        row = c.execute('SELECT status,item_json FROM diagnostic_prompt WHERE child_id=? AND prompt_id=?', (child_id, pid)).fetchone()
        counts['prompt_copies'] += int(bool(row and tuple(row) != ('invalidated', '{}')))
    return counts
