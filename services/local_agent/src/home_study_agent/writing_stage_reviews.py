"""Schedule existing review_task rows from qualified writing-stage receipts only.

The caller owns the write transaction. Review completion records performance,
not mastery. This module never scores or grants original-manuscript permission.
"""
from datetime import datetime, timedelta, timezone

from .local_supply import digest
from .specialized_stages import POLICY, _item_source, project_specialized_stages
from .writing_response_origin import require, writing_item


INTERVAL = 'writing_stage_7d'


def _far_authority(far):
    refs = far.get('review_refs', [])
    require(len(refs) == 1, 'REVIEW_AUTHORITY_AMBIGUOUS')
    ref = refs[0]
    status = ref.get('score_status', 'reviewed_human')
    authority = ref.get('score_authority_id') if status == 'calibrated_model' else ref.get('event_id')
    require(status in {'reviewed_human', 'calibrated_model'} and authority and ref.get('assessment_id'),
            'REVIEW_AUTHORITY_REQUIRED')
    return ref['assessment_id'], status, authority


def _versioned_id(child_id, node_id, evidence_id, status, authority):
    return 'writing-stage:' + digest([child_id, node_id, evidence_id, INTERVAL, status, authority])


def _same_authority(row, far):
    if not row['score_authority_id']:
        return True  # Existing legacy rows keep their original, stricter source gates.
    _, status, authority = _far_authority(far)
    return (row['score_status'], row['score_authority_id']) == (status, authority)


def _producer_predecessor(c, child_id, row):
    if row['score_status'] not in {'calibrated_model', 'reviewed_human'} or not row['score_authority_id']:
        return False
    if row['review_task_id'] != _versioned_id(child_id, row['knowledge_node_id'],
            row['source_evidence_id'], row['score_status'], row['score_authority_id']):
        return False
    if row['score_status'] == 'reviewed_human':
        event = c.execute('''SELECT r.created_at FROM rubric_review_event r
            JOIN rubric_assessment a USING(assessment_id) WHERE r.child_id=? AND a.child_id=?
            AND r.event_id=? AND r.operation='approve'
            AND json_extract(CASE WHEN json_valid(a.payload_json) THEN a.payload_json ELSE '{}' END,
                '$.source_evidence_id')=?''',
            (child_id, child_id, row['score_authority_id'], row['source_evidence_id'])).fetchall()
        return len(event) == 1 and datetime.fromisoformat(event[0][0]) <= datetime.fromisoformat(row['created_at'])
    return True


def validate_writing_review_source(connection, child_id, review, *, as_of=None):
    """Read the exact durable review and its current original-writing authority."""
    c = connection
    if not c.in_transaction:
        c.execute('BEGIN')
        try:
            return validate_writing_review_source(c, child_id, review, as_of=as_of)
        finally:
            c.rollback()
    supplied = dict(review)
    row = c.execute('SELECT * FROM review_task WHERE review_task_id=?', (supplied.get('review_task_id'),)).fetchone()
    require(row and dict(row) == supplied and row['child_id'] == child_id, 'REVIEW_BINDING_CHANGED')
    require(row['status'] in {'pending','completed'} and row['interval_label'] in {INTERVAL,'7d'}, 'REVIEW_NOT_ACTIVE')
    from .learning_obligations import withdrawn_evidence_ids
    withdrawn = withdrawn_evidence_ids(c, child_id)
    require(row['source_evidence_id'] not in withdrawn, 'REVIEW_SOURCE_WITHDRAWN')
    now = datetime.fromisoformat(as_of) if as_of else datetime.now(timezone.utc)
    due = datetime.fromisoformat(row['due_at'])
    require(now.tzinfo and due.tzinfo and due <= now, 'REVIEW_NOT_DUE')
    item, _ = writing_item(c, child_id, row['diagnostic_item_id'])
    require(item['knowledge_node_id'] == row['knowledge_node_id'], 'REVIEW_TARGET_CHANGED')
    basis_at = now
    if row['status'] == 'completed':
        original_result = c.execute('''SELECT a.created_at FROM diagnostic_attempt a
            JOIN learning_evidence e ON e.evidence_id=a.evidence_id AND e.child_id=a.child_id
                AND e.session_id=a.session_id AND e.knowledge_node_id=? AND e.evidence_type='diagnostic_answer'
            WHERE a.evidence_id=? AND a.review_task_id=? AND a.child_id=? AND a.diagnostic_item_id=?''',
            (row['knowledge_node_id'],row['result_evidence_id'],row['review_task_id'],child_id,row['diagnostic_item_id'])).fetchall()
        require(len(original_result) == 1, 'REVIEW_RESULT_BINDING_CHANGED')
        at = datetime.fromisoformat(original_result[0]['created_at'])
        require(at.tzinfo and due <= at <= now, 'REVIEW_RESULT_TIME_CHANGED')
        # A failed real check must not erase its own historical admission. Current
        # source/review withdrawal still applies to this time-filtered projection.
        basis_at = at - timedelta(microseconds=1)
    projection = project_specialized_stages(c, child_id, 'pet', as_of=basis_at.isoformat())
    nodes = [n for d in projection['domains'] if d['domain_id'].startswith('pet.writing.')
             for n in d['nodes'] if n['knowledge_node_id'] == row['knowledge_node_id']]
    require(len(nodes) == 1, 'REVIEW_STAGE_UNAVAILABLE')
    far = nodes[0]['gates']['far_transfer']
    require(far['evidence_refs'] == [row['source_evidence_id']] and len(far['diagnostic_item_ids']) == 1,
            'REVIEW_FAR_SOURCE_UNQUALIFIED')
    require(_same_authority(row, far), 'REVIEW_AUTHORITY_CHANGED')
    anchor = datetime.fromisoformat(far['observed_at'])
    require(due >= anchor + timedelta(days=POLICY['retention_delay_days']), 'REVIEW_DELAY_CHANGED')
    original, _ = writing_item(c, child_id, far['diagnostic_item_ids'][0])
    target_source, error = _item_source(c, child_id, item, now)
    source, original_error = _item_source(c, child_id, original, now)
    require(target_source and source and not error and not original_error and target_source['score_qualified']
        and target_source['source_root_item_id'] == source['source_root_item_id'], 'REVIEW_SOURCE_CHANGED')
    return dict(row)


def validate_writing_review_result(c, *, child_id, session_id, review_task_id,
                                   prompt_id, evidence_id, as_of=None):
    """Read-only completion proof. A reliable failed check still performed the task."""
    if not c.in_transaction:
        c.execute('BEGIN')
        try:
            return validate_writing_review_result(c, child_id=child_id, session_id=session_id,
                review_task_id=review_task_id, prompt_id=prompt_id, evidence_id=evidence_id, as_of=as_of)
        finally:
            c.rollback()
    from .writing_rubric import writing_review_gate
    from .learning_rubric import RubricEvidenceService
    now = as_of or datetime.now(timezone.utc).isoformat()
    task = validate_writing_stage_review(c,child_id,review_task_id,allowed_statuses=('completed',),as_of=now)
    require(task['result_evidence_id']==evidence_id, 'REVIEW_RESULT_CONFLICT')
    gate = writing_review_gate(c,child_id,evidence_id,as_of=now)
    require(gate.get('assessment_scope') in {'reviewed_human_original_writing', 'calibrated_model_original_writing'},
            'REVIEW_RELIABLE_RUBRIC_REQUIRED')
    current = RubricEvidenceService(None).get(child_id,gate['assessment_id'],connection=c)
    result = _review_result(c,child_id,current,now)
    require(result['session_id']==session_id and result['prompt_id']==prompt_id
        and result['review_task_id']==review_task_id, 'REVIEW_RESULT_BINDING_CHANGED')
    return result


def validate_writing_stage_review(connection, child_id, review_task_id, *,
                                  allowed_statuses=('pending','completed'), as_of=None):
    row = connection.execute('SELECT * FROM review_task WHERE review_task_id=?', (review_task_id,)).fetchone()
    require(row and row['status'] in allowed_statuses, 'REVIEW_NOT_ACTIVE')
    return validate_writing_review_source(connection, child_id, row, as_of=as_of)


def validate_writing_review_answer(c, *, child_id, session_id, item_id, review_task_id,
                                   prompt_id, answer, as_of):
    """Submission admission checks the newly captured original, not ASR body text."""
    from .writing_response_origin import VERSION, _original_text
    import json
    require(prompt_id, 'REVIEW_TRUSTED_PROMPT_REQUIRED')
    validate_writing_stage_review(c,child_id,review_task_id,allowed_statuses=('pending',),as_of=as_of)
    prompt = c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=? AND child_id=? AND origin_session_id=?',
                       (prompt_id,child_id,session_id)).fetchone()
    require(prompt and prompt['status']=='answer_saved' and prompt['review_task_id']==review_task_id
            and prompt['diagnostic_item_id']==item_id, 'REVIEW_PROMPT_BINDING_CHANGED')
    require(not c.execute('SELECT 1 FROM diagnostic_attempt WHERE review_task_id=?', (review_task_id,)).fetchone(),
            'REVIEW_ANSWER_ALREADY_SAVED')
    artifact = c.execute("SELECT * FROM capture_artifact WHERE artifact_id=? AND child_id=? AND session_id=? AND artifact_type='writing_original' AND status='completed'",
                         (prompt['writing_original_artifact_id'],child_id,session_id)).fetchone()
    require(artifact, 'REVIEW_NEW_ORIGINAL_REQUIRED')
    details = json.loads(artifact['details_json'])
    require(details.get('schema')==VERSION and details.get('prompt_id')==prompt_id
        and details.get('item_version')==prompt['item_version'] and not details.get('attempt_binding')
        and details.get('response_hash')==digest(answer), 'REVIEW_ORIGINAL_BINDING_CHANGED')
    require(_original_text(c,prompt,artifact,details['origin'])==answer, 'REVIEW_ORIGINAL_RESPONSE_CHANGED')
    times = [datetime.fromisoformat(v) for v in (prompt['created_at'],artifact['created_at'],as_of)]
    require(all(t.tzinfo for t in times) and times[0] <= times[1] <= times[2], 'REVIEW_ORIGINAL_TIME_CHANGED')


def _review_result(c, child_id, current, now):
    from .writing_score import current_written_score
    from .writing_response_origin import original_written_response
    from .learning_obligations import project_item_exposure, withdrawn_evidence_ids
    attempt = c.execute('''SELECT a.*,e.hint_level,e.occurred_at,e.knowledge_node_id FROM diagnostic_attempt a
        JOIN learning_evidence e ON e.evidence_id=a.evidence_id AND e.child_id=a.child_id AND e.session_id=a.session_id
        WHERE a.evidence_id=? AND a.child_id=? AND e.evidence_type='diagnostic_answer' ''',
        (current['source_evidence_id'],child_id)).fetchone()
    authority = current_written_score(c, child_id, current['assessment_id'], now=datetime.fromisoformat(now))
    receipt, decision = authority['receipt'], authority['decision']
    require(attempt and attempt['review_task_id']
        and receipt['independent'] is True and receipt['hint_level']==0 and attempt['hint_level']==0,
        'REVIEW_RELIABLE_INDEPENDENT_RUBRIC_REQUIRED')
    require(attempt['evidence_id'] not in withdrawn_evidence_ids(c,child_id), 'REVIEW_RESULT_WITHDRAWN')
    at = datetime.fromisoformat(attempt['created_at'])
    # Validate admission before this answer, so its own later failed review
    # cannot retroactively erase the original far-transfer qualification.
    task = c.execute('SELECT * FROM review_task WHERE review_task_id=? AND child_id=?',
        (attempt['review_task_id'],child_id)).fetchone()
    require(task and task['status'] in {'pending','completed'}, 'REVIEW_NOT_ACTIVE')
    if task['status']=='pending':
        validate_writing_review_source(c,child_id,task,as_of=(at-timedelta(microseconds=1)).isoformat())
    else:
        validate_writing_review_source(c,child_id,task,as_of=now)
    prompt_rows = c.execute('SELECT * FROM diagnostic_prompt WHERE child_id=? AND attempt_id=?',
                                (child_id,attempt['attempt_id'])).fetchall()
    require(len(prompt_rows)==1, 'REVIEW_EXACT_PROMPT_REQUIRED')
    prompt = prompt_rows[0]
    require(prompt['status']=='answered' and prompt['review_task_id']==task['review_task_id']
            and prompt['origin_session_id']==attempt['session_id'] and task['diagnostic_item_id']==attempt['diagnostic_item_id']
            and task['knowledge_node_id']==attempt['knowledge_node_id'], 'REVIEW_RESULT_BINDING_CHANGED')
    for table in ('writing_followup','writing_reference_revision'):
        if c.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
            assisted = c.execute('SELECT 1 FROM '+table+' WHERE prompt_id=?'+(" AND kind='revision'" if table=='writing_followup' else ''),
                                     (prompt['prompt_id'],)).fetchone()
            require(not assisted, 'REVIEW_REVISION_IS_NOT_INDEPENDENT')
    original_written_response(c,prompt,attempt)
    require(at.isoformat()==datetime.fromisoformat(attempt['occurred_at']).isoformat()
            and datetime.fromisoformat(task['due_at']) <= at <= datetime.fromisoformat(now), 'REVIEW_RESULT_TIME_CHANGED')
    exposure = project_item_exposure(c,child_id,attempt['diagnostic_item_id'],as_of=(at-timedelta(microseconds=1)).isoformat())
    require(not exposure['tutor_assisted'], 'REVIEW_RESULT_NOT_INDEPENDENT')
    # The far original is intentionally repeated after seven days. Any intervening
    # attempt at this retained item is rehearsal, regardless of the rolling window.
    anchor = c.execute('SELECT occurred_at FROM learning_evidence WHERE evidence_id=? AND child_id=?',
        (task['source_evidence_id'],child_id)).fetchone()
    require(anchor and not c.execute('''SELECT 1 FROM diagnostic_attempt WHERE child_id=?
        AND diagnostic_item_id=? AND created_at>? AND created_at<? AND attempt_id!=?''',
        (child_id,attempt['diagnostic_item_id'],anchor[0],attempt['created_at'],attempt['attempt_id'])).fetchone(),
        'REVIEW_INTERVENING_REHEARSAL')
    require(not c.execute('SELECT 1 FROM diagnostic_attempt WHERE review_task_id=? AND attempt_id!=?',
                (task['review_task_id'],attempt['attempt_id'])).fetchone(), 'REVIEW_RESULT_AMBIGUOUS')
    if task['status']=='completed':
        require(task['result_evidence_id']==attempt['evidence_id'], 'REVIEW_RESULT_CONFLICT')
    return dict(review_task_id=task['review_task_id'],evidence_id=attempt['evidence_id'],attempt_id=attempt['attempt_id'],
        prompt_id=prompt['prompt_id'],item_version=prompt['item_version'],session_id=attempt['session_id'],child_id=child_id,
        diagnostic_item_id=attempt['diagnostic_item_id'],assessment_id=current['assessment_id'],
        review_revision=current['review_revision'],performed=True,passed=decision['passed'],
        score_status=decision['status'],score_authority_id=decision['authority_id'],
        mastery_claim_allowed=False,local_only=True,cloud_transmission_allowed=False)


def apply_writing_stage_review(c, child_id, assessment_id, *, as_of=None):
    """Apply current qualified original-writing scores in the caller's transaction."""
    from .learning_rubric import RubricEvidenceService
    from .writing_score import current_written_score
    from .learning_state import LearningStateService
    require(c.in_transaction, 'REVIEW_WRITE_TRANSACTION_REQUIRED')
    now = as_of or datetime.now(timezone.utc).isoformat()
    current = RubricEvidenceService(None).get(child_id,assessment_id,connection=c)
    attempt = c.execute('''SELECT a.*,e.knowledge_node_id,e.hint_level FROM diagnostic_attempt a
        JOIN learning_evidence e USING(evidence_id) WHERE a.evidence_id=? AND a.child_id=? AND e.child_id=a.child_id''',
        (current['source_evidence_id'],child_id)).fetchone()
    completed = None
    try:
        authority = current_written_score(c, child_id, assessment_id, now=datetime.fromisoformat(now))
    except (ValueError, TypeError, KeyError):
        authority = None
    if (attempt and attempt['review_task_id'] and authority
            and authority['receipt']['independent'] is True and authority['receipt']['hint_level']==0 and attempt['hint_level']==0):
        result = _review_result(c,child_id,current,now)
        task = c.execute('SELECT * FROM review_task WHERE review_task_id=?', (result['review_task_id'],)).fetchone()
        if task['status']=='pending':
            c.execute("UPDATE review_task SET status='completed',completed_at=?,result_evidence_id=? WHERE review_task_id=? AND status='pending'",
                (now,attempt['evidence_id'],task['review_task_id']))
        completed = task['review_task_id']
    reconciliation = reconcile_writing_stage_reviews(c,child_id,as_of=now,
        _reauthorizing_assessment=assessment_id if authority and authority['decision']['status']=='reviewed_human' else None)
    if attempt:
        LearningStateService(None).replay_mastery(child_id,attempt['knowledge_node_id'],as_of=now,
            connection=c,reason='writing_rubric_review')
    return {'completed_review_task_id':completed, 'reconciliation':reconciliation,
            'local_only':True,'cloud_transmission_allowed':False}


def reconcile_writing_stage_reviews(connection, child_id, *, as_of=None, _reauthorizing_assessment=None):
    c = connection
    require(c.in_transaction, 'REVIEW_WRITE_TRANSACTION_REQUIRED')
    now = datetime.fromisoformat(as_of) if as_of is not None else datetime.now(timezone.utc)
    require(now.tzinfo is not None, 'REVIEW_TIME_INVALID')
    projection = project_specialized_stages(c, child_id, 'pet', as_of=now.isoformat())
    result = dict(status='reconciled', created=[], retained=[], cancelled=[], blocked=[],
        local_only=True, cloud_transmission_allowed=False, mastery_claim_allowed=False)
    anchors = {}
    for domain in projection['domains']:
        if not domain['domain_id'].startswith('pet.writing.'):
            continue
        for node in domain['nodes']:
            far = node['gates']['far_transfer']
            if len(far['evidence_refs']) != 1 or len(far['diagnostic_item_ids']) != 1:
                continue
            anchors[far['evidence_refs'][0]] = (node, far)

    # Only this helper's pending tasks are cancelled. Completed attempts and
    # their historical receipts are not deleted or converted into new attempts.
    for row in c.execute("SELECT * FROM review_task WHERE child_id=? AND interval_label=? AND status='pending'",
                         (child_id, INTERVAL)).fetchall():
        anchor = anchors.get(row['source_evidence_id'])
        if (not anchor or row['knowledge_node_id'] != anchor[0]['knowledge_node_id']
                or not _same_authority(row, anchor[1])):
            c.execute("UPDATE review_task SET status='cancelled' WHERE review_task_id=? AND child_id=? AND status='pending'",
                      (row['review_task_id'], child_id))
            result['cancelled'].append(row['review_task_id'])

    for evidence_id, (node, far) in sorted(anchors.items()):
        node_id = node['knowledge_node_id']
        item, _ = writing_item(c, child_id, far['diagnostic_item_ids'][0])
        source, reason = _item_source(c, child_id, item, now)
        require(source and source['score_qualified'] and not reason, 'REVIEW_SOURCE_CHANGED')
        due = datetime.fromisoformat(far['observed_at']) + timedelta(days=POLICY['retention_delay_days'])
        assessment_id, score_status, authority_id = _far_authority(far)
        existing = c.execute("SELECT * FROM review_task WHERE source_evidence_id=? AND interval_label IN (?, '7d') ORDER BY rowid",
                             (evidence_id, INTERVAL)).fetchall()
        live = [r for r in existing if r['status'] != 'cancelled']
        predecessor = None
        if existing and not live:
            prior = existing[-1]
            can_reauthorize = (score_status == 'reviewed_human' and _reauthorizing_assessment == assessment_id
                and all(r['child_id']==child_id and r['knowledge_node_id']==node_id and r['interval_label']==INTERVAL for r in existing)
                and _producer_predecessor(c, child_id, prior) and authority_id != prior['score_authority_id']
                and not c.execute('SELECT 1 FROM review_task WHERE predecessor_review_task_id=?', (prior['review_task_id'],)).fetchone())
            if can_reauthorize:
                predecessor = prior['review_task_id']
            else:
                result['blocked'].append({'knowledge_node_id':node_id, 'source_evidence_id':evidence_id,
                    'reason_code':'writing_retention_reauthorization_required'})
                continue
        if live:
            valid = len(live) == 1
            row = live[0]
            valid = valid and (row['child_id'], row['knowledge_node_id']) == (child_id, node_id)
            valid = valid and row['status'] in {'pending', 'completed'}
            try:
                prior_due = datetime.fromisoformat(row['due_at'])
                target, _ = writing_item(c, child_id, row['diagnostic_item_id'])
                target_source, error = _item_source(c, child_id, target, now)
                valid = bool(valid and prior_due.tzinfo and prior_due >= due and target_source and not error
                    and target['knowledge_node_id'] == node_id and target_source['score_qualified']
                    and target_source['source_root_item_id'] == source['source_root_item_id'])
            except (ValueError, TypeError):
                valid = False
            if valid:
                result['retained'].append(row['review_task_id'])
            else:
                result['blocked'].append({'knowledge_node_id':node_id, 'source_evidence_id':evidence_id,
                    'reason_code':'writing_retention_existing_binding_changed'})
            continue
        if node['current_stage'] != 'delayed_retention':
            continue
        review_id = _versioned_id(child_id, node_id, evidence_id, score_status, authority_id)
        c.execute('''INSERT INTO review_task(review_task_id,child_id,knowledge_node_id,diagnostic_item_id,
            source_evidence_id,interval_label,due_at,status,created_at,score_authority_id,score_status,predecessor_review_task_id)
            VALUES(?,?,?,?,?,?,?,'pending',?,?,?,?)''',
            (review_id,child_id,node_id,item['diagnostic_item_id'],evidence_id,INTERVAL,due.isoformat(),now.isoformat(),
             authority_id,score_status,predecessor))
        result['created'].append(review_id)
    return result
