"""School open-response practice obligations, using existing sources and receipts.

No scores are created here. A review task records an execution obligation, not
mastery. Source stage approval and answer review remain separate authorities.
All writes borrow the caller's transaction; no new persistence tables.
"""
from datetime import datetime, timedelta, timezone
import json

from .local_supply import LocalSupplyError, digest, validate_item

VERSION = 'school_open_review_v1'
INTERVALS = {'school_open_near': 'near_transfer', 'school_open_far': 'far_transfer',
             'school_open_7d': 'delayed_retention'}
MINUTES = 12


class SchoolReviewError(ValueError):
    pass


def require(value, reason):
    if not value:
        raise SchoolReviewError('SCHOOL_REVIEW_' + reason)


def _object(value, reason):
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        parsed = None
    require(isinstance(parsed, dict), reason)
    return parsed


def _time(value):
    at = datetime.fromisoformat(value)
    require(at.tzinfo is not None, 'TIME_INVALID')
    return at


def _now(as_of=None):
    return _time(as_of) if as_of else datetime.now(timezone.utc)


def is_school_item(c, item_id):
    row = c.execute('''SELECT di.answer_type,di.assessment_mode,n.track FROM diagnostic_item di
        JOIN knowledge_node n USING(knowledge_node_id) WHERE diagnostic_item_id=?''', (item_id,)).fetchone()
    return bool(row and tuple(row) == ('open_response', 'open_response_rubric', 'school'))


def is_school_review(c, item):
    if item['source_type'] != 'due_review':
        return False
    row = c.execute('SELECT interval_label FROM review_task WHERE review_task_id=?', (item['source_ref'],)).fetchone()
    return bool(row and row[0] in INTERVALS)


def _source(c, child, item_id, now):
    from .specialized_stages import _item_source
    from .writing_response_origin import writing_item
    from .open_response_profile import profile_for_item
    require(is_school_item(c, item_id), 'SOURCE_KIND_CHANGED')
    item = validate_item(c, child, item_id)
    node = dict(c.execute('SELECT * FROM knowledge_node WHERE knowledge_node_id=?',
        (item['knowledge_node_id'],)).fetchone())
    profile = profile_for_item(item, node)
    spoken = profile['response_mode'] == 'spoken_explanation'
    if not spoken:
        writing_item(c, child, item_id)
    source, error = _item_source(c, child, item, now)
    # A valid oral question is reviewable. Its answer still needs the separate
    # original-voice and current reasoning-review authority checked by _anchor.
    require(source and not error and (source['score_qualified'] or spoken), 'SOURCE_UNAVAILABLE')
    rubric = json.loads(item['expected_answer_json'])
    return item, source, {k: rubric[k] for k in ('review_profile', 'response_mode', 'task_family', 'profile_version')}


class _ReviewCandidates:
    """Candidate IDs only, privately borrowed for one reconcile transaction.

    No source, score or receipt is cached. The synchronous caller must not end
    the transaction while this context is active; this is not an epoch detector.
    """
    def __init__(self, c, child):
        self.c, self.child, self.rows = c, child, None

    def __enter__(self):
        from .writing_rubric import has_writing_review_evidence
        require(self.c.in_transaction, 'WRITE_TRANSACTION_REQUIRED')
        rows = self.c.execute('''SELECT a.assessment_id,a.step_id,a.payload_json,
                r.operation AS review_operation,r.payload_json AS review_payload
            FROM rubric_assessment a LEFT JOIN rubric_review_event r ON r.rowid=(
                SELECT e.rowid FROM rubric_review_event e WHERE e.assessment_id=a.assessment_id
                ORDER BY e.revision DESC LIMIT 1)
            WHERE a.child_id=? AND (r.operation='approve' OR
             (r.rowid IS NULL
              AND json_type(CASE WHEN json_valid(a.payload_json) THEN a.payload_json ELSE '{}' END,
                  '$.calibration_binding.protocol_id')='text')) ''', (self.child,)).fetchall()
        self.rows = {}
        for row in rows:
            try:
                if row['review_operation'] is not None:
                    review = json.loads(row['review_payload'])
                    if not isinstance(review, dict):
                        continue
                    spoken_binding = review.get('school_spoken_original_version')
                    spoken = (review.get('assessment_modality') == 'reviewed_spoken_reasoning_text'
                        and isinstance(spoken_binding, str) and len(spoken_binding) == 64)
                    if not spoken and not has_writing_review_evidence({
                            'review': {**review, 'operation': row['review_operation']}}):
                        continue
                payload = json.loads(row['payload_json'])
            except (ValueError, TypeError):
                continue
            evidence = payload.get('source_evidence_id') if isinstance(payload, dict) else None
            if isinstance(evidence, str) and evidence:
                self.rows.setdefault(evidence, []).append(row)
        return self

    def for_evidence(self, c, child, evidence):
        require(self.rows is not None and c is self.c and child == self.child
                and c.in_transaction, 'REVIEW_READ_SCOPE_CHANGED')
        return self.rows.get(evidence, ())

    def __exit__(self, *_):
        self.rows = None


def _anchor(c, child, evidence_id, now, *, _candidates=None):
    from .writing_response_origin import original_written_response
    from .writing_score import current_written_score
    from .learning_obligations import withdrawn_evidence_ids
    candidates = (_candidates.for_evidence(c, child, evidence_id)
                  if _candidates is not None else None)
    if candidates is not None:
        require(candidates, 'CURRENT_ORIGINAL_REVIEW_REQUIRED')
    require(evidence_id not in withdrawn_evidence_ids(c, child), 'SOURCE_WITHDRAWN')
    rows = c.execute('''SELECT a.*,e.knowledge_node_id,e.hint_level,e.occurred_at FROM diagnostic_attempt a
        JOIN learning_evidence e ON e.evidence_id=a.evidence_id AND e.child_id=a.child_id
          AND e.session_id=a.session_id AND e.evidence_type='diagnostic_answer'
        WHERE a.evidence_id=? AND a.child_id=?''', (evidence_id, child)).fetchall()
    require(len(rows) == 1, 'ORIGINAL_ATTEMPT_REQUIRED')
    attempt = dict(rows[0])
    require(_time(attempt['created_at']) == _time(attempt['occurred_at']) <= now, 'ANSWER_TIME_CHANGED')
    require(now - _time(attempt['created_at']) <= timedelta(days=90), 'SOURCE_EXPIRED')
    item, source, family = _source(c, child, attempt['diagnostic_item_id'], now)
    require(item['knowledge_node_id'] == attempt['knowledge_node_id'], 'SOURCE_NODE_CHANGED')
    # Legacy saved answers may predate prompt persistence. Missing provenance
    # is unqualified evidence, never permission to manufacture a binding/schema.
    prompt_columns = {row[1] for row in c.execute('PRAGMA table_info(diagnostic_prompt)')}
    require({'child_id', 'attempt_id', 'status', 'origin_session_id', 'prompt_id',
             'diagnostic_item_id', 'item_version', 'created_at'} <= prompt_columns, 'ORIGINAL_PROMPT_REQUIRED')
    prompts = c.execute('SELECT * FROM diagnostic_prompt WHERE child_id=? AND attempt_id=?',
        (child, attempt['attempt_id'])).fetchall()
    require(len(prompts) == 1 and prompts[0]['status'] == 'answered'
        and prompts[0]['origin_session_id'] == attempt['session_id']
        and prompts[0]['diagnostic_item_id'] == attempt['diagnostic_item_id'], 'ORIGINAL_PROMPT_REQUIRED')
    spoken = family['response_mode'] == 'spoken_explanation'
    if not spoken:
        original_written_response(c, prompts[0], attempt)
    accepted = []
    if candidates is None:
        candidates = c.execute('SELECT assessment_id,step_id,payload_json FROM rubric_assessment WHERE child_id=?', (child,)).fetchall()
    for row in candidates:
        try:
            payload = json.loads(row['payload_json'])
        except (ValueError, TypeError):
            continue
        if not isinstance(payload, dict) or payload.get('source_evidence_id') != evidence_id:
            continue
        try:
            if spoken:
                from .school_spoken_score import current_score
                score = current_score(c, child, row['assessment_id'], prompts[0], attempt, now=now)
            else:
                score = current_written_score(c, child, row['assessment_id'], now=now)
        except (ValueError, KeyError, TypeError):
            continue
        accepted.append(score)
    require(len(accepted) == 1, 'CURRENT_ORIGINAL_REVIEW_REQUIRED')
    score = accepted[0]
    return {'attempt': attempt, 'source': source, 'family': family, 'current': score['current'],
        'receipt': score['receipt'], 'score_decision': score['decision'], 'prompt': dict(prompts[0])}


def _next_interval(c, child, anchor, now, visited, *, _candidates=None):
    attempt = anchor['attempt']
    if not attempt['review_task_id']:
        return 'school_open_near'
    previous = c.execute('SELECT * FROM review_task WHERE review_task_id=? AND child_id=?',
        (attempt['review_task_id'], child)).fetchone()
    if not previous or previous['interval_label'] not in INTERVALS:
        return 'school_open_near'
    require(previous['status'] == 'completed' and previous['result_evidence_id'] == attempt['evidence_id'],
        'PREVIOUS_REVIEW_NOT_COMPLETED')
    validate_review_source(c, child, previous, as_of=now.isoformat(), _visited=visited, _candidates=_candidates)
    independent = anchor['receipt']['independent'] and anchor['attempt']['hint_level'] == 0
    if not (independent and anchor['score_decision']['passed']):
        return 'school_open_near'
    return {'school_open_near': 'school_open_far', 'school_open_far': 'school_open_7d',
            'school_open_7d': None}[previous['interval_label']]


def _optional_prompt_identity(c, child, prompt, target_identity):
    from .diagnostic_history_identity import historical_identity, HistoryIdentityUnavailable
    try:
        return historical_identity(c, child, prompt, target_identity)
    except HistoryIdentityUnavailable as error:
        raise SchoolReviewError(str(error)) from error


def _target(c, child, anchor, target_id, interval, now, before):
    from .learning_obligations import project_item_exposure, question_identity, _question_catalog, _tutor_exposure_index
    item, source, family = _source(c, child, target_id, now)
    original = anchor['source']
    require(item['knowledge_node_id'] == anchor['attempt']['knowledge_node_id'] and family == anchor['family']
        and source['source_root_item_id'] == original['source_root_item_id'], 'TARGET_FAMILY_CHANGED')
    require(not source['reviewed_at'] or _time(source['reviewed_at']) <= before, 'TARGET_REVIEW_TOO_LATE')
    if interval == 'school_open_7d':
        require(target_id == anchor['attempt']['diagnostic_item_id'] and source['stage'] == 'far_transfer', 'RETENTION_TARGET_CHANGED')
    else:
        require(source['stage'] == INTERVALS[interval] and source['question_identity'] != original['question_identity'],
            'DISTINCT_REVIEWED_STAGE_REQUIRED')
    exposure = project_item_exposure(c, child, target_id, as_of=before.isoformat())
    require(not exposure.get('tutor_assisted'), 'TARGET_HINT_EXPOSED')
    # Full owned history, not a rolling window, determines whether transfer is new.
    cutoff = anchor['attempt']['created_at'] if interval == 'school_open_7d' else None
    own_review_id = _task_id(child,anchor['attempt']['evidence_id'],interval,anchor,source)
    for prompt in c.execute('SELECT * FROM diagnostic_prompt WHERE child_id=? AND created_at<=?',
            (child,before.isoformat())).fetchall():
        if cutoff and _time(prompt['created_at']) <= _time(cutoff):
            continue
        if _optional_prompt_identity(c, child, prompt, source['question_identity']) != source['question_identity']:
            continue
        # Only this obligation's already prepared original is not prior exposure.
        own = c.execute('''SELECT 1 FROM agent_plan WHERE child_id=? AND session_id=?
            AND json_extract(CASE WHEN json_valid(context_manifest_json) THEN context_manifest_json ELSE '{}' END,'$.goal_prompt_binding.school_review')=1
            AND json_extract(CASE WHEN json_valid(context_manifest_json) THEN context_manifest_json ELSE '{}' END,'$.goal_prompt_binding.prompt_id')=?
            AND json_extract(CASE WHEN json_valid(context_manifest_json) THEN context_manifest_json ELSE '{}' END,'$.goal_prompt_binding.item_version')=?
            AND json_extract(CASE WHEN json_valid(context_manifest_json) THEN context_manifest_json ELSE '{}' END,'$.goal_prompt_binding.binding.review_task_id')=?''',
            (child,prompt['origin_session_id'],prompt['prompt_id'],prompt['item_version'],own_review_id)).fetchone()
        require(prompt['review_task_id']==own_review_id and own, 'TARGET_ALREADY_SHOWN')
    for row in c.execute('''SELECT a.diagnostic_item_id,a.created_at FROM diagnostic_attempt a
        WHERE a.child_id=? AND a.created_at<=? UNION ALL
        SELECT a.diagnostic_item_id,a.created_at FROM mistake_attempt a WHERE a.child_id=? AND a.created_at<=?''',
        (child, before.isoformat(), child, before.isoformat())).fetchall():
        if cutoff and _time(row['created_at']) <= _time(cutoff):
            continue
        raw = c.execute('SELECT * FROM diagnostic_item WHERE diagnostic_item_id=?', (row['diagnostic_item_id'],)).fetchone()
        require(raw is not None, 'PRIOR_SOURCE_UNAVAILABLE')
        if question_identity(raw, connection=c) == source['question_identity']:
            raise SchoolReviewError('SCHOOL_REVIEW_TARGET_ALREADY_PRACTICED')
    catalog, identities = _question_catalog(c)
    exact, bound = _tutor_exposure_index(c, child, catalog, identities)
    hints = list(bound.get(source['question_identity'], []))
    if not item.get('stimulus_text') and not item.get('stimulus_asset'):
        hints += exact.get(' '.join(item['prompt'].split()), [])
    require(not any(at <= before and (not cutoff or at > _time(cutoff)) for _, at, _ in hints), 'TARGET_HINT_EXPOSED')
    return item, source


def _task_id(child, evidence_id, interval, anchor, source):
    return 'school-review:' + digest({'version': VERSION, 'child_id': child,
        'evidence_id': evidence_id, 'interval': interval, 'source': anchor['source'], 'target': source,
        'assessment_id': anchor['current']['assessment_id'], 'review_event_id': anchor['score_decision']['authority_id']})


def _producer_predecessor(c, child, row, anchor, now):
    authority = row['score_authority_id']
    if not authority or row['score_status'] not in {'calibrated_model', 'reviewed_human'}:
        return False
    if row['score_status'] == 'reviewed_human':
        assessment_rows = c.execute('''SELECT a.assessment_id,r.created_at
            FROM rubric_review_event r JOIN rubric_assessment a USING(assessment_id)
            WHERE r.event_id=? AND r.child_id=? AND a.child_id=? AND r.operation='approve'
            AND json_extract(CASE WHEN json_valid(a.payload_json) THEN a.payload_json ELSE '{}' END,
                '$.source_evidence_id')=?''',
            (authority, child, child, row['source_evidence_id'])).fetchall()
        if len(assessment_rows) != 1 or _time(assessment_rows[0]['created_at']) > _time(row['created_at']):
            return False
    else:
        assessment_rows = c.execute('''SELECT assessment_id FROM rubric_assessment
            WHERE child_id=? AND json_extract(CASE WHEN json_valid(payload_json) THEN payload_json ELSE '{}' END,
                '$.source_evidence_id')=?''',
            (child, row['source_evidence_id'])).fetchall()
    _, source, _ = _source(c, child, row['diagnostic_item_id'], now)
    # Verify a past producer identity, not past score qualification. A new current
    # human decision is still required by the caller before creating a successor.
    return any(_task_id(child, row['source_evidence_id'], row['interval_label'],
        {**anchor, 'current': {'assessment_id': old['assessment_id']},
         'score_decision': {'authority_id': authority}}, source) == row['review_task_id']
        for old in assessment_rows)


def validate_review_source(c, child, review, *, as_of=None, _visited=(), _candidates=None):
    if not c.in_transaction:
        c.execute('BEGIN')
        try:
            return validate_review_source(c,child,review,as_of=as_of,_visited=_visited,_candidates=_candidates)
        finally:
            c.rollback()
    row = c.execute('SELECT * FROM review_task WHERE review_task_id=?', (review['review_task_id'],)).fetchone()
    require(row and dict(row) == dict(review) and row['child_id'] == child
        and row['interval_label'] in INTERVALS and row['status'] in {'pending', 'completed'}, 'BINDING_CHANGED')
    require(row['review_task_id'] not in _visited, 'ANCESTRY_CYCLE')
    visited = (*_visited, row['review_task_id'])
    now = _now(as_of)
    anchor = _anchor(c, child, row['source_evidence_id'], now, _candidates=_candidates)
    require(_next_interval(c, child, anchor, now, visited, _candidates=_candidates) == row['interval_label'], 'STAGE_CHANGED')
    require(row['knowledge_node_id'] == anchor['attempt']['knowledge_node_id'], 'NODE_CHANGED')
    due = _time(anchor['score_decision']['created_at']) + timedelta(days=7 if row['interval_label']=='school_open_7d' else 1)
    require(_time(row['due_at']) == due, 'DUE_CHANGED')
    prior = c.execute('SELECT created_at FROM diagnostic_attempt WHERE review_task_id=? AND child_id=?',
        (row['review_task_id'], child)).fetchall()
    require(len(prior) <= 1, 'RESULT_AMBIGUOUS')
    before = _time(prior[0][0]) - timedelta(microseconds=1) if prior else now
    _, source = _target(c, child, anchor, row['diagnostic_item_id'], row['interval_label'], now, before)
    require(_task_id(child, row['source_evidence_id'], row['interval_label'], anchor, source) == row['review_task_id'],
        'SOURCE_VERSION_CHANGED')
    return dict(row)


def reconcile_school_reviews(c, child, *, as_of=None, cancel_only=False, _reauthorizing_assessment=None):
    require(c.in_transaction, 'WRITE_TRANSACTION_REQUIRED')
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='rubric_assessment'").fetchone():
        return {'created': [], 'retained': [], 'cancelled': [], 'blocked': [],
            'local_only': True, 'cloud_transmission_allowed': False, 'mastery_claim_allowed': False}
    with _ReviewCandidates(c, child) as candidates:
        return _reconcile(c, child, as_of=as_of, cancel_only=cancel_only, candidates=candidates,
                          reauthorizing_assessment=_reauthorizing_assessment)


def _reconcile(c, child, *, as_of, cancel_only, candidates, reauthorizing_assessment):
    now = _now(as_of)
    result = {'created': [], 'retained': [], 'cancelled': [], 'blocked': [],
        'local_only': True, 'cloud_transmission_allowed': False, 'mastery_claim_allowed': False}
    for row in c.execute("SELECT * FROM review_task WHERE child_id=? AND status='pending'", (child,)).fetchall():
        if row['interval_label'] not in INTERVALS:
            continue
        try:
            validate_review_source(c, child, row, as_of=now.isoformat(), _candidates=candidates)
        except (SchoolReviewError, LocalSupplyError, ValueError, TypeError, KeyError) as error:
            if str(error) == 'SCHOOL_REVIEW_HISTORY_UNAVAILABLE':
                result['blocked'].append(_history_blocked(row['source_evidence_id'], row['diagnostic_item_id'], row['interval_label']))
                continue
            c.execute("UPDATE review_task SET status='cancelled' WHERE review_task_id=? AND status='pending'", (row['review_task_id'],))
            from .learning_obligations import cancel_unstarted_plans
            cancel_unstarted_plans(c, child, 'due_review', row['review_task_id'], now.isoformat())
            result['cancelled'].append(row['review_task_id'])
    if cancel_only:
        return result
    evidence = c.execute('''SELECT a.evidence_id,a.diagnostic_item_id FROM diagnostic_attempt a
        JOIN diagnostic_item d USING(diagnostic_item_id) JOIN knowledge_node n ON n.knowledge_node_id=d.knowledge_node_id
        WHERE a.child_id=? AND d.answer_type='open_response' AND n.track='school' ORDER BY a.created_at,a.attempt_id''', (child,)).fetchall()
    for raw in evidence:
        try:
            anchor = _anchor(c, child, raw['evidence_id'], now, _candidates=candidates)
            interval = _next_interval(c, child, anchor, now, (), _candidates=candidates)
            if interval is None:
                continue
            previous = c.execute('''SELECT * FROM review_task WHERE child_id=? AND source_evidence_id=?
                AND interval_label=? ORDER BY rowid''', (child, raw['evidence_id'], interval)).fetchall()
            existing = next((r for r in previous if r['status'] != 'cancelled'), None)
            if existing:
                result['retained'].append(existing['review_task_id'])
                continue
            predecessor = previous[-1] if previous else None
            decision = anchor['score_decision']
            if predecessor and not (
                    reauthorizing_assessment == anchor['current']['assessment_id']
                    and decision['status'] == 'reviewed_human'
                    and decision['authority_id'] != predecessor['score_authority_id']
                    and _producer_predecessor(c, child, predecessor, anchor, now)):
                # Reads/restarts and renewed certificates cannot revive cancelled work.
                continue
            target_candidates = ([raw['diagnostic_item_id']] if interval == 'school_open_7d' else
                [r[0] for r in c.execute("SELECT diagnostic_item_id FROM diagnostic_item WHERE knowledge_node_id=? AND status='active' ORDER BY diagnostic_item_id",
                    (anchor['attempt']['knowledge_node_id'],))])
            chosen = None
            history_unavailable = False
            for item_id in target_candidates:
                try:
                    _, source = _target(c, child, anchor, item_id, interval, now, now)
                except (SchoolReviewError, LocalSupplyError, ValueError, TypeError, KeyError) as error:
                    history_unavailable |= str(error) == 'SCHOOL_REVIEW_HISTORY_UNAVAILABLE'
                    continue
                chosen = (item_id, source)
                break
            if not chosen:
                if history_unavailable:
                    result['blocked'].append(_history_blocked(raw['evidence_id'], raw['diagnostic_item_id'], interval))
                    continue
                result['blocked'].append({'source_evidence_id': raw['evidence_id'], 'diagnostic_item_id': raw['diagnostic_item_id'],
                    'stage': INTERVALS[interval], 'reason': '缺少已审核且未练过的对应迁移题；保留原审核依据，不用同题冒充独立检查。'})
                continue
            item_id, source = chosen
            due = _time(anchor['score_decision']['created_at']) + timedelta(days=7 if interval=='school_open_7d' else 1)
            rid = _task_id(child, raw['evidence_id'], interval, anchor, source)
            if any(r['review_task_id'] == rid for r in previous):
                continue
            c.execute('''INSERT INTO review_task(review_task_id,child_id,knowledge_node_id,diagnostic_item_id,
                source_evidence_id,interval_label,due_at,status,created_at,score_authority_id,score_status,
                predecessor_review_task_id) VALUES(?,?,?,?,?,?,?,'pending',?,?,?,?)''',
                (rid, child, anchor['attempt']['knowledge_node_id'], item_id, raw['evidence_id'], interval,
                 due.isoformat(), now.isoformat(), decision['authority_id'], decision['status'],
                 predecessor['review_task_id'] if predecessor else None))
            result['created'].append(rid)
        except (SchoolReviewError, LocalSupplyError, ValueError, TypeError, KeyError):
            continue
    return result


def _history_blocked(evidence_id, item_id, interval):
    return dict(source_evidence_id=evidence_id, diagnostic_item_id=item_id, stage=INTERVALS[interval],
        error_code='SCHOOL_REVIEW_HISTORY_UNAVAILABLE', retryable=True,
        reason='历史题目的原版本暂时无法核对，尚不能确认这是一道未练过的新题。保留原复习事项；恢复原版本证据后可重新核对，不重复作答或自动换题。')


def _result(c, child, evidence_id, now):
    anchor = _anchor(c, child, evidence_id, now)
    attempt, prompt = anchor['attempt'], anchor['prompt']
    task = c.execute('SELECT * FROM review_task WHERE review_task_id=?', (attempt['review_task_id'],)).fetchone()
    require(task and task['child_id'] == child, 'RESULT_BINDING_CHANGED')
    validate_review_source(c, child, task, as_of=now.isoformat())
    require(prompt['review_task_id'] == task['review_task_id'] and task['diagnostic_item_id'] == attempt['diagnostic_item_id']
        and task['knowledge_node_id'] == attempt['knowledge_node_id'], 'RESULT_BINDING_CHANGED')
    require(_time(task['due_at']) <= _time(attempt['created_at']) <= now, 'RESULT_NOT_DUE')
    require(anchor['receipt']['independent'] and attempt['hint_level'] == 0, 'INDEPENDENT_ORIGINAL_REQUIRED')
    require(task['result_evidence_id'] in (None, evidence_id), 'RESULT_CONFLICT')
    return {k: attempt[k] for k in ('evidence_id', 'attempt_id', 'session_id', 'diagnostic_item_id', 'created_at')} | {
        'prompt_id': prompt['prompt_id'], 'review_task_id': task['review_task_id'], 'assessment_id': anchor['current']['assessment_id'],
        'review_revision': anchor['score_decision']['review_revision'], 'performed': True, 'passed': anchor['score_decision']['passed'],
        'score_status': anchor['score_decision']['status'], 'score_authority_id': anchor['score_decision']['authority_id'],
        'mastery_claim_allowed': False}


def apply_school_review(c, child, assessment_id, *, as_of=None):
    require(c.in_transaction, 'WRITE_TRANSACTION_REQUIRED')
    from .learning_rubric import RubricEvidenceService
    now = _now(as_of)
    current = RubricEvidenceService(None).get(child, assessment_id, connection=c)
    raw = c.execute('SELECT review_task_id FROM diagnostic_attempt WHERE evidence_id=? AND child_id=?',
        (current['source_evidence_id'], child)).fetchone()
    if raw and raw[0]:
        task = c.execute('SELECT * FROM review_task WHERE review_task_id=?', (raw[0],)).fetchone()
        if task and task['interval_label'] in INTERVALS:
            try:
                result = _result(c, child, current['source_evidence_id'], now)
            except (SchoolReviewError, LocalSupplyError, ValueError, TypeError, KeyError):
                result = None
            if result and task['status'] == 'pending':
                c.execute("UPDATE review_task SET status='completed',completed_at=?,result_evidence_id=? WHERE review_task_id=? AND status='pending'",
                    (now.isoformat(), result['evidence_id'], task['review_task_id']))
    return reconcile_school_reviews(c, child, as_of=now.isoformat(), _reauthorizing_assessment=assessment_id)
