"""Local original-audio readiness, separate from delivery and learning evidence."""
from .database import local_media_scope
from .diagnostic_prompt import DiagnosticPromptService, _version
from .pet_media_evidence import PetMediaEvidenceError, project_pet_media_evidence


def resolve_pet_listening_resource(c, session_id, child_id, prompt_id, item_version):
    if not c.in_transaction:
        c.execute('BEGIN')
        try:
            return resolve_pet_listening_resource(c, session_id, child_id, prompt_id, item_version)
        finally:
            c.rollback()
    result = {'status': 'awaiting_resources', 'reason': 'listening_material_missing', 'resource': None,
              'local_only': True, 'cloud_transmission_allowed': False}
    prompt = c.execute('''SELECT p.* FROM diagnostic_prompt p JOIN daily_session s
        ON s.session_id=p.origin_session_id JOIN child_profile cp ON cp.child_id=p.child_id
        WHERE p.prompt_id=? AND p.origin_session_id=? AND p.child_id=? AND s.child_id=p.child_id
        AND s.status='active' AND cp.status='active' ''', (prompt_id, session_id, child_id)).fetchone()
    if not prompt or prompt['item_version'] != item_version or prompt['status'] in {'cancelled', 'invalidated'}:
        return {**result, 'reason': 'listening_prompt_unavailable'}
    item = DiagnosticPromptService._item(c, child_id, prompt['diagnostic_item_id'])
    if not item or _version(item) != item_version:
        return {**result, 'reason': 'listening_prompt_unavailable'}
    node = c.execute('SELECT * FROM knowledge_node WHERE knowledge_node_id=?', (item['knowledge_node_id'],)).fetchone()
    from .listening_modality import listening_node
    if not node or not listening_node(node, item['answer_type']):
        return {**result, 'status': 'not_applicable', 'reason': None}
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pet_media_binding'").fetchone():
        return result
    rows = c.execute('''SELECT media_id FROM pet_media_binding WHERE child_id=? AND session_id=?
        AND prompt_id=? AND item_version=? AND diagnostic_item_id=? AND kind='listening' LIMIT 65''',
        (child_id, session_id, prompt_id, item_version, item['diagnostic_item_id'])).fetchall()
    if not rows:
        return result
    if len(rows) > 64:
        return {**result, 'reason': 'listening_material_ambiguous'}
    scope = local_media_scope(c)
    if not scope:
        return {**result, 'reason': 'listening_media_runtime_unavailable'}
    current = []
    for row in rows:
        try:
            media = project_pet_media_evidence(c, session_id, media_id=row['media_id'], **scope)
        except PetMediaEvidenceError:
            continue
        if media['status'] not in {'invalidated', 'cancelled'}:
            current.append(media)
    if not current:
        return {**result, 'reason': 'listening_material_invalid'}
    if len(current) != 1:
        return {**result, 'reason': 'listening_material_ambiguous'}
    media = current[0]
    if not media['source_reviewed']:
        return {**result, 'reason': 'listening_material_review_required'}
    q = media['qualification']
    if not q['audio_signal_usable']:
        return {**result, 'reason': 'listening_material_invalid'}
    if not q['requirement_current']:
        return {**result, 'reason': 'listening_requirement_unverified'}
    if q.get('full_audio_control_reference_available') is not True:
        return {**result, 'reason': 'full_audio_spoken_reference_unavailable'}
    return {**result, 'status': 'ready', 'reason': None, 'resource': {
        'media_id': media['media_id'], 'source_version': media['source_version'],
        'audio_sha256': media['audio']['sha256'], 'item_version': item_version,
        'requirement_version': q['delivery_requirement']['version'],
        'required_play_count': q['required_play_count']}}
