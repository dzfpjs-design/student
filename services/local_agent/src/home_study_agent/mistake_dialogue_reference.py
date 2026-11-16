"""Read-only ownership for conversation, never an answer submission target."""
import hashlib
import json


def _candidate_source(connection, row, state):
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ocr_review_item'").fetchone():
        return None
    candidate = connection.execute('''SELECT * FROM mistake_capture_candidate
        WHERE candidate_id=? AND child_id=? AND status='pending' AND mistake_id IS NULL''',
        (row['candidate_id'], row['child_id'])).fetchone()
    if (not candidate or candidate['artifact_id'] != state.get('artifact_id')
            or candidate['question_text'] != state.get('question_text')
            or connection.execute('SELECT 1 FROM paper_scoring_request WHERE candidate_id=?',
                                  (row['candidate_id'],)).fetchone()):
        return None
    artifact = connection.execute('''SELECT * FROM capture_artifact
        WHERE artifact_id=? AND child_id=? AND session_id=?
        AND artifact_type='document_ocr' AND status IN ('completed','needs_review')''',
        (candidate['artifact_id'], row['child_id'], row['session_id'])).fetchone()
    if not artifact:
        return None
    try:
        details = json.loads(artifact['details_json'])
    except (ValueError, TypeError):
        return None
    if not isinstance(details, dict):
        return None
    details.pop('visual_capture', None)
    source = dict(artifact)
    source['details_json'] = json.dumps(details, ensure_ascii=False, sort_keys=True)
    reviews = [dict(review) for review in connection.execute('''SELECT * FROM ocr_review_item
        WHERE artifact_id=? AND child_id=? ORDER BY review_id''', (artifact['artifact_id'], row['child_id']))]
    return {'candidate': dict(candidate), 'artifact': source, 'reviews': reviews}


def _diagnostic_source(connection, mistake_id, child_id):
    case = connection.execute('SELECT * FROM mistake_case WHERE mistake_id=? AND child_id=?',
                              (mistake_id, child_id)).fetchone()
    if (not case or case['source_type'] != 'diagnostic'
            or case['source_ref'] != case['diagnostic_item_id'] or not case['source_evidence_id']):
        return None
    evidence = connection.execute('SELECT * FROM learning_evidence WHERE evidence_id=? AND child_id=?',
                                  (case['source_evidence_id'], child_id)).fetchone()
    attempts = connection.execute('''SELECT * FROM diagnostic_attempt
        WHERE evidence_id=? AND child_id=? AND diagnostic_item_id=?''',
        (case['source_evidence_id'], child_id, case['diagnostic_item_id'])).fetchall()
    if (not evidence or len(attempts) != 1 or evidence['knowledge_node_id'] != case['knowledge_node_id']
            or evidence['evidence_type'] != 'diagnostic_answer'
            or attempts[0]['session_id'] != evidence['session_id']
            or attempts[0]['submitted_answer'] != case['submitted_answer']
            or attempts[0]['correctness'] != evidence['correctness']):
        return None
    from .local_supply import LocalSupplyError, validate_item
    try:
        item = validate_item(connection, child_id, case['diagnostic_item_id'])
    except LocalSupplyError:
        return None
    if item['knowledge_node_id'] != case['knowledge_node_id'] or item['prompt'] != case['prompt_summary']:
        return None
    return {'case': dict(case), 'item': item, 'attempt': dict(attempts[0]), 'evidence': dict(evidence)}


def current_binding(connection, session_id, child_id):
    row = connection.execute('''SELECT d.* FROM mistake_dialogue d
        LEFT JOIN mistake_case m ON m.mistake_id=d.mistake_id AND m.child_id=d.child_id
        JOIN daily_session s ON s.session_id=d.session_id AND s.child_id=d.child_id
        JOIN child_profile p ON p.child_id=d.child_id
        WHERE d.session_id=? AND d.child_id=?
        AND ((d.phase IN ('diagnosis','remediation') AND m.status!='excluded')
          OR (d.phase IN ('candidate','answer','confirm') AND d.mistake_id IS NULL))
        AND s.status='active' AND s.state='study' AND p.status='active' ''',
        (session_id, child_id)).fetchone()
    if not row:
        return None
    state = json.loads(row['state_json'])
    if state.get('pending_operation') or state.get('foundation_hold'):
        return None
    if state.get('foundation_origin'):
        from .foundation_remediation import continuation_hold
        if continuation_hold(connection, {**dict(row), 'state': state}):
            return None
    if row['mistake_id'] is None:
        source = _candidate_source(connection, row, state)
    else:
        from .mistake_source_revision import snapshot
        source = snapshot(connection, row['mistake_id'], child_id)
        if source is None:
            source = _diagnostic_source(connection, row['mistake_id'], child_id)
    if not source:
        return None
    from .learning_obligations import withdrawn_evidence_ids
    if source.get('case', {}).get('source_evidence_id') in withdrawn_evidence_ids(connection, child_id):
        return None
    version = hashlib.sha256(json.dumps({'focus': dict(row), 'source': source},
        sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    binding = {'session_id': session_id, 'child_id': child_id, 'mistake_id': row['mistake_id'],
               'phase': row['phase'], 'item_id': 'source-dialogue', 'source_version': version}
    if row['mistake_id'] is None:
        binding.update(candidate_id=row['candidate_id'], artifact_id=source['artifact']['artifact_id'])
    return binding
