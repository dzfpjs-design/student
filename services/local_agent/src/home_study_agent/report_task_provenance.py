"""Bind report task claims to their recorded source eligibility.

Never delete or reinterpret historical records here. A nonreportable source
requires local review; it does not establish that a child did not do the work.
"""

import hashlib
import json
import sqlite3


class _NamedReadConnection:
    """Offer named SELECT rows without changing the borrowed connection."""
    def __init__(self, connection):
        self.connection = connection

    def execute(self, sql, parameters=()):
        cursor = self.connection.cursor()
        cursor.row_factory = sqlite3.Row
        return cursor.execute(sql, parameters)

    @property
    def in_transaction(self):
        return self.connection.in_transaction


def carryover_source_events(connection, child_id, target_session_id, homework_id):
    """Read carryover receipts without exposing their child-provided text."""
    if not homework_id or not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='learning_input_carryover_event'").fetchone():
        return []
    events = []
    for row in connection.execute("""SELECT event_id,source_session_id,target_session_id,result_json
        FROM learning_input_carryover_event WHERE child_id=? AND target_session_id=? AND operation='continue_today'""",
        (child_id, target_session_id)):
        event_id, source_session_id, target, raw = tuple(row)
        if not isinstance(raw, str):
            continue
        try:
            result = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(result, dict) and result.get('target_homework_id') == homework_id:
            events.append((event_id, source_session_id, target, hashlib.sha256(raw.encode('utf-8')).hexdigest(), result))
    return events


def _carryover_lineage(connection, child_id, target_session, homework_id, artifact_id, artifact_session):
    """Trace each accepted homework to its source; detect cycles and stale links."""
    # A chain spans multiple SELECTs. The caller owns the snapshot; never
    # start a transaction implicitly on a borrowed connection.
    if not connection.in_transaction:
        raise ValueError('REPORT_TASK_SOURCE_UNCONFIRMED')
    chain, visited, expected_parent = [], set(), None
    session, current_homework = target_session, homework_id
    while True:
        identity = (session, current_homework)
        if identity in visited:
            raise ValueError('REPORT_TASK_SOURCE_UNCONFIRMED')
        visited.add(identity)
        if len(visited) > 256:
            raise ValueError('REPORT_TASK_SOURCE_UNCONFIRMED')
        source_row = connection.execute("""SELECT child_id,session_id,source_artifact_id,confirmation_status
            FROM homework_item WHERE homework_id=?""", (current_homework,)).fetchone()
        if source_row is None or tuple(source_row) != (child_id, session, artifact_id, 'child_confirmed'):
            raise ValueError('REPORT_TASK_SOURCE_UNCONFIRMED')
        chain.append(('homework_identity', current_homework, child_id, session, artifact_id, 'child_confirmed'))
        if session == artifact_session:
            if expected_parent is not None:
                raise ValueError('REPORT_TASK_SOURCE_UNCONFIRMED')
            return chain
        events = carryover_source_events(connection, child_id, session, current_homework)
        if len(events) != 1:
            raise ValueError('REPORT_TASK_SOURCE_UNCONFIRMED')
        event_id, origin_session, target_session, digest, receipt = events[0]
        target = receipt.get('target_homework')
        if (not isinstance(target, dict) or target.get('homework_id') != current_homework
                or target.get('child_id') != child_id or target.get('session_id') != session
                or target.get('source_artifact_id') != artifact_id):
            raise ValueError('REPORT_TASK_SOURCE_UNCONFIRMED')
        for table in ('learning_input_replan_unplaced', 'learning_input_replan_receipt', 'learning_input_revision_event'):
            if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                raise ValueError('REPORT_TASK_SOURCE_UNCONFIRMED')
        row = connection.execute("""SELECT e.obligation_id,u.child_id,u.session_id,u.payload_json,u.reason,
                r.child_id,r.session_id,r.event_id,r.source_record_json,
                v.child_id,v.session_id,v.target_kind,v.target_id,v.after_json
            FROM learning_input_carryover_event e
            JOIN learning_input_replan_unplaced u ON u.obligation_id=e.obligation_id
            JOIN learning_input_replan_receipt r ON r.receipt_id=u.receipt_id
            JOIN learning_input_revision_event v ON v.event_id=r.event_id
            WHERE e.event_id=? AND e.child_id=? AND e.source_session_id=? AND e.target_session_id=?""",
            (event_id, child_id, origin_session, target_session)).fetchone()
        if row is None:
            raise ValueError('REPORT_TASK_SOURCE_UNCONFIRMED')
        (obligation, owner, obligation_session, raw, obligation_reason, receipt_owner, receipt_session, original_event,
         source_record_raw, revision_owner, revision_session, revision_kind, revision_target, revision_raw) = tuple(row)
        if (not all(isinstance(value,str) for value in (raw,source_record_raw,revision_raw))
                or owner != child_id or receipt_owner != child_id or revision_owner != child_id
                or revision_session != origin_session or revision_kind != 'homework'
                or obligation_session != origin_session or receipt_session != origin_session
                or (expected_parent is not None and obligation != expected_parent)):
            raise ValueError('REPORT_TASK_SOURCE_UNCONFIRMED')
        # A later withdrawal (or a newer scope decision) invalidates the old
        # acceptance without rewriting the historical task or receipt.
        acceptance = connection.execute("""SELECT revision,client_action_id,request_json
            FROM learning_input_carryover_event WHERE event_id=? AND child_id=?""", (event_id,child_id)).fetchone()
        if acceptance is None or type(acceptance[0]) is not int or not isinstance(acceptance[2],str):
            raise ValueError('REPORT_TASK_SOURCE_UNCONFIRMED')
        accepted_revision, accepted_action, accepted_request_raw = tuple(acceptance)
        scope = connection.execute("""SELECT event_id,child_id,revision,operation,request_json,result_json,
                client_action_id,source_session_id,target_session_id
            FROM learning_input_carryover_event WHERE obligation_id=? AND operation IN ('confirm_scope','withdraw_scope')
            ORDER BY revision DESC LIMIT 1""", (obligation,)).fetchone()
        scope_digest = None
        if scope is not None:
            (scope_id,scope_child,scope_revision,scope_op,scope_request,scope_result,
             scope_action,scope_origin,scope_target) = tuple(scope)
            if (scope_child != child_id or scope_origin != origin_session or type(scope_revision) is not int
                    or scope_revision >= accepted_revision or scope_op != 'confirm_scope'
                    or not isinstance(scope_request,str) or not isinstance(scope_result,str)):
                raise ValueError('REPORT_TASK_SOURCE_UNCONFIRMED')
            try:
                scope_data = json.loads(scope_result)
                scope_command = json.loads(scope_request)
                if (not isinstance(scope_data,dict) or scope_data.get('status') != 'scope_confirmed'
                        or scope_data.get('event_id') != scope_id or scope_data.get('obligation_id') != obligation
                        or scope_data.get('revision') != scope_revision or not isinstance(scope_command,dict)
                        or scope_command.get('operation') != 'confirm_scope' or scope_command.get('child_id') != child_id
                        or type(scope_command.get('expected_revision')) is not int or scope_command['expected_revision'] + 1 != scope_revision
                        or scope_command.get('obligation_id') != obligation or scope_command.get('target_session_id') != scope_target
                        or scope_command.get('client_action_id') != scope_action):
                    raise ValueError('REPORT_TASK_SOURCE_UNCONFIRMED')
                from .learning_input_carryover import LearningInputCarryoverService
                scope_authorization = LearningInputCarryoverService._authorize(_NamedReadConnection(connection),scope_command,
                    LearningInputCarryoverService._binding(scope_command),replay=True,
                    capability='learning_inputs.review_reclaimed_scope')
                auth_fields = ('agent_turn_id','child_id','session_id','transcript','intent','context_manifest_json',
                               'capability_name','capability_version','execution_location','arguments_json','idempotency_key')
                scope_digest = hashlib.sha256(json.dumps([tuple(scope),[scope_authorization[key] for key in auth_fields]],
                    ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
            except (TypeError, ValueError, KeyError, IndexError, sqlite3.Error):
                raise ValueError('REPORT_TASK_SOURCE_UNCONFIRMED') from None
        try:
            request = json.loads(accepted_request_raw)
            if (not isinstance(request,dict) or request.get('child_id') != child_id
                    or request.get('target_session_id') != session or request.get('obligation_id') != obligation
                    or request.get('client_action_id') != accepted_action or request.get('decision') != 'continue_today'
                    or type(request.get('expected_revision')) is not int or request['expected_revision'] + 1 != accepted_revision
                    or receipt.get('event_id') != event_id or receipt.get('obligation_id') != obligation
                    or receipt.get('revision') != accepted_revision):
                raise ValueError('REPORT_TASK_SOURCE_UNCONFIRMED')
            from .learning_input_carryover import LearningInputCarryoverService
            authorization = LearningInputCarryoverService._authorize(_NamedReadConnection(connection),request,
                LearningInputCarryoverService._binding(request),replay=True)
            origin = LearningInputCarryoverService(None)._origin(_NamedReadConnection(connection),child_id,obligation)
            if origin['source_current'] is not True or origin['source_version'] != request.get('expected_source_version'):
                raise ValueError('REPORT_TASK_SOURCE_UNCONFIRMED')
            # Preserve the recorded authorization binding, without exposing its
            # transcript in any fingerprint or log.
            auth_keys = ('agent_turn_id','child_id','session_id','transcript','intent','context_manifest_json',
                         'capability_name','capability_version','execution_location','arguments_json','idempotency_key')
            auth_digest = hashlib.sha256(json.dumps([authorization[key] for key in auth_keys],
                ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
            payload = json.loads(raw)
            binding = payload['source_binding']
            original = binding['source']
            source_record = json.loads(source_record_raw)
            revision_after = json.loads(revision_raw)
            reclaim = payload.get('reclaim')
            previous = reclaim['source_homework'] if reclaim else original
        except (TypeError, ValueError, KeyError, IndexError, sqlite3.Error):
            raise ValueError('REPORT_TASK_SOURCE_UNCONFIRMED') from None
        if not isinstance(binding,dict) or not isinstance(original,dict) or not isinstance(previous,dict):
            raise ValueError('REPORT_TASK_SOURCE_UNCONFIRMED')
        from .learning_input_revision_scope import REASONS as REVISION_SCOPE_REASONS
        if reclaim and (not isinstance(reclaim,dict) or type(reclaim.get('requires_scope_review')) is not bool):
            raise ValueError('REPORT_TASK_SOURCE_UNCONFIRMED')
        if scope is None and ((reclaim and reclaim['requires_scope_review'])
                              or (not reclaim and obligation_reason in REVISION_SCOPE_REASONS)):
            raise ValueError('REPORT_TASK_SOURCE_UNCONFIRMED')
        if obligation_reason in REVISION_SCOPE_REASONS and not reclaim:
            cursor = connection.cursor()
            cursor.row_factory = sqlite3.Row
            items = [dict(item) for item in cursor.execute("""SELECT i.* FROM plan_item i JOIN daily_plan p USING(plan_id)
                WHERE p.plan_id=? AND p.child_id=? AND p.session_id=? AND i.homework_id=?
                  AND i.source_type='school_homework' AND i.status!='cancelled' ORDER BY i.plan_item_id""",
                (binding.get('plan_id'),child_id,origin_session,original.get('homework_id')))]
            if not items or scope_data.get('revision_execution') != items:
                raise ValueError('REPORT_TASK_SOURCE_UNCONFIRMED')
        if (not isinstance(binding, dict) or not isinstance(original, dict) or not isinstance(previous, dict)
                or binding.get('version') != 'learning_input_replan_source_v1'
                or binding.get('child_id') != child_id or binding.get('session_id') != origin_session
                or binding.get('event_id') != original_event
                or binding.get('homework_id') != original.get('homework_id')
                or revision_target != original.get('homework_id')
                or source_record != original or revision_after != original
                or original.get('child_id') != child_id or original.get('session_id') != origin_session
                or original.get('source_artifact_id') != artifact_id
                or previous.get('child_id') != child_id or previous.get('source_artifact_id') != artifact_id
                or not isinstance(previous.get('homework_id'), str) or not previous['homework_id']
                or not isinstance(previous.get('session_id'), str) or not previous['session_id']):
            raise ValueError('REPORT_TASK_SOURCE_UNCONFIRMED')
        # A receipt proves what was accepted at the time; it does not prove
        # that its source revision is still eligible today. Match the current
        # root record and latest revision without mutating historical facts.
        cursor = connection.cursor()
        cursor.row_factory = sqlite3.Row
        root_row = cursor.execute('SELECT * FROM homework_item WHERE homework_id=? AND child_id=? AND session_id=?',
                                  (original['homework_id'],child_id,origin_session)).fetchone()
        previous_row = cursor.execute('SELECT * FROM homework_item WHERE homework_id=? AND child_id=? AND session_id=?',
                                      (previous['homework_id'],child_id,previous['session_id'])).fetchone()
        latest = connection.execute("""SELECT event_id FROM learning_input_revision_event
            WHERE child_id=? AND target_kind='homework' AND target_id=? ORDER BY rowid DESC LIMIT 1""",
            (child_id,original['homework_id'])).fetchone()
        artifact = cursor.execute('SELECT * FROM capture_artifact WHERE artifact_id=? AND child_id=?',
                                  (artifact_id,child_id)).fetchone()
        from .learning_input_revision import _version
        if (root_row is None or dict(root_row) != original or previous_row is None
                or dict(previous_row) != previous or latest is None or latest[0] != original_event
                or artifact is None or binding.get('artifact_fingerprint') != _version(dict(artifact))):
            raise ValueError('REPORT_TASK_SOURCE_UNCONFIRMED')
        if reclaim:
            if (not isinstance(reclaim, dict) or reclaim.get('source_session_id') != previous['session_id']):
                raise ValueError('REPORT_TASK_SOURCE_UNCONFIRMED')
            expected_parent = reclaim.get('parent_obligation_id')
            if expected_parent is not None and (not isinstance(expected_parent, str) or not expected_parent):
                raise ValueError('REPORT_TASK_SOURCE_UNCONFIRMED')
        else:
            expected_parent = None
        chain.append(('carryover', event_id, origin_session, target_session, digest,
                      obligation, original_event, hashlib.sha256(raw.encode('utf-8')).hexdigest(),
                      hashlib.sha256(accepted_request_raw.encode('utf-8')).hexdigest(),auth_digest,scope_digest,
                      hashlib.sha256(source_record_raw.encode('utf-8')).hexdigest(),
                      hashlib.sha256(revision_raw.encode('utf-8')).hexdigest()))
        session, current_homework = previous['session_id'], previous['homework_id']


def task_source_state(connection, child_id, item_id):
    row = connection.execute('''SELECT p.session_id,i.homework_id,i.completion_artifact_id,
          h.child_id,h.session_id,h.source_artifact_id,i.source_type,i.source_ref
        FROM plan_item i JOIN daily_plan p ON p.plan_id=i.plan_id
        LEFT JOIN homework_item h ON h.homework_id=i.homework_id
        WHERE p.child_id=? AND i.plan_item_id=?''', (child_id, item_id)).fetchone()
    if row is None:
        raise ValueError('REPORT_TASK_SOURCE_MISSING')
    session_id, homework_id, completion_id, homework_child, homework_session, homework_artifact, source_type, source_ref = tuple(row)
    if source_type == 'school_homework' and source_ref and source_ref != homework_id:
        raise ValueError('REPORT_TASK_SOURCE_UNCONFIRMED')
    if homework_id and (homework_child != child_id or homework_session != session_id):
        raise ValueError('REPORT_TASK_SOURCE_UNCONFIRMED')
    result = []
    if homework_id:
        # The task title may be an older plan copy. Bind the source homework
        # and its latest revision even when the OCR artifact is unchanged.
        from .learning_input_revision import _version
        cursor = connection.cursor()
        cursor.row_factory = sqlite3.Row
        homework = cursor.execute('SELECT * FROM homework_item WHERE homework_id=?', (homework_id,)).fetchone()
        if homework is None or 'confirmation_status' not in homework.keys() or homework['confirmation_status'] != 'child_confirmed':
            raise ValueError('REPORT_TASK_SOURCE_UNCONFIRMED')
        latest = None
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='learning_input_revision_event'").fetchone():
            latest = cursor.execute("""SELECT * FROM learning_input_revision_event
                WHERE child_id=? AND target_kind='homework' AND target_id=? ORDER BY rowid DESC LIMIT 1""",
                (child_id,homework_id)).fetchone()
        if latest is not None and ('session_id' not in latest.keys() or latest['session_id'] != session_id):
            raise ValueError('REPORT_TASK_SOURCE_UNCONFIRMED')
        result.append(('homework_record',homework_id,_version(dict(homework)),
                       _version(dict(latest)) if latest is not None else None))
    for kind, artifact_id in (('completion', completion_id), ('homework', homework_artifact)):
        if not artifact_id:
            continue
        cursor = connection.cursor()
        cursor.row_factory = sqlite3.Row
        artifact = cursor.execute('SELECT * FROM capture_artifact WHERE artifact_id=?', (artifact_id,)).fetchone()
        if artifact is None:
            raise ValueError('REPORT_TASK_SOURCE_UNCONFIRMED')
        owner, source_session, artifact_type, status = (artifact['child_id'],artifact['session_id'],artifact['artifact_type'],artifact['status'])
        if (owner != child_id or artifact_type == 'document_ocr_candidate'
                or (kind == 'homework' and artifact_type != 'document_ocr')
                or status not in ('completed', 'quality_warning')):
            raise ValueError('REPORT_TASK_SOURCE_UNCONFIRMED')
        if source_session != session_id:
            # A child-confirmed carryover makes a new homework in the target
            # session while retaining the original same-child OCR reference.
            # Require the original receipt and its target snapshot; completion
            # evidence itself never inherits a different session.
            if kind != 'homework':
                raise ValueError('REPORT_TASK_SOURCE_UNCONFIRMED')
            try:
                result.extend(_carryover_lineage(connection, child_id, session_id, homework_id, artifact_id, source_session))
            except sqlite3.Error as error:
                raise ValueError('REPORT_TASK_SOURCE_UNCONFIRMED') from error
        from .learning_input_revision import _version
        result.append((kind, artifact_id, owner, source_session, artifact_type, status, _version(dict(artifact))))
    return result


def aggregate_task_ids(connection, child_id, day, report_end):
    """Completed tasks actually included by weekly/active-cycle projections."""
    exists = lambda name: bool(connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())
    if not all(exists(name) for name in ('weekly_goal', 'daily_plan', 'plan_item', 'daily_session')):
        return []
    ids = {row[0] for row in connection.execute('''SELECT DISTINCT pi.plan_item_id FROM weekly_goal wg
        JOIN daily_plan dp ON dp.child_id=wg.child_id AND dp.plan_date BETWEEN wg.week_start AND wg.week_end AND dp.plan_date<=?
        JOIN plan_item pi ON pi.plan_id=dp.plan_id AND pi.source_type=CASE wg.track WHEN 'pet' THEN 'pet_daily' ELSE 'competition_math' END
        WHERE wg.child_id=? AND ? BETWEEN wg.week_start AND wg.week_end AND pi.status='completed'
          AND julianday(COALESCE(pi.completed_at,(SELECT ended_at FROM daily_session
            WHERE session_id=dp.session_id AND child_id=wg.child_id)))<=julianday(?)''', (day,child_id,day,report_end))}
    if exists('goal_cycle') and exists('goal_program'):
        ids.update(row[0] for row in connection.execute('''SELECT DISTINCT pi.plan_item_id FROM goal_cycle gc
            JOIN goal_program gp ON gp.program_id=gc.program_id
            JOIN daily_plan dp ON dp.child_id=gc.child_id AND dp.plan_date BETWEEN gc.week_start AND gc.week_end AND dp.plan_date<=?
            JOIN plan_item pi ON pi.plan_id=dp.plan_id AND ((gp.track='pet_path' AND pi.source_type='pet_daily')
              OR (gp.track='competition_math_path' AND pi.source_type='competition_math')
              OR (gp.track='junior_exam_readiness' AND pi.source_type IN ('junior_exam_goal','mistake_review','due_review')))
            WHERE gc.child_id=? AND gp.status='active' AND ? BETWEEN gc.week_start AND gc.week_end AND pi.status='completed'
              AND julianday(COALESCE(pi.completed_at,(SELECT ended_at FROM daily_session
                WHERE session_id=dp.session_id AND child_id=gc.child_id)))<=julianday(?)''', (day,child_id,day,report_end)))
    return sorted(ids)


def report_task_source_fingerprint(connection, child_id, session_id):
    """Bind report tasks and their qualified origins in the caller's snapshot."""
    session = connection.execute('SELECT child_id,status,ended_at FROM daily_session WHERE session_id=?', (session_id,)).fetchone()
    if session is None or session[0] != child_id or session[1] != 'completed':
        raise ValueError('REPORT_TASK_SCOPE_MISMATCH')
    if connection.execute('SELECT 1 FROM daily_plan WHERE session_id=? AND child_id!=? LIMIT 1', (session_id,child_id)).fetchone():
        raise ValueError('REPORT_TASK_SCOPE_MISMATCH')
    from .report_calendar import study_day
    day = study_day(connection, session_id)
    ids = {row[0] for row in connection.execute("""SELECT i.plan_item_id FROM plan_item i
        JOIN daily_plan p ON p.plan_id=i.plan_id
        WHERE p.session_id=? AND p.child_id=? AND i.source_type!='planned_break'""", (session_id, child_id))}
    ids.update(aggregate_task_ids(connection, child_id, day, session[2]))
    tasks = []
    for item_id in sorted(ids):
        row = connection.execute("""SELECT i.plan_item_id,p.session_id,i.subject,i.title,i.expected_minutes,
            i.actual_minutes,i.status,i.position,i.completion_evidence_type,i.completion_note,i.task_type,
            i.completion_checklist_json,i.homework_id,i.completion_artifact_id,i.source_type,i.source_ref,
            i.started_at,i.completed_at,i.active_seconds FROM plan_item i JOIN daily_plan p ON p.plan_id=i.plan_id
            WHERE p.child_id=? AND i.plan_item_id=?""", (child_id,item_id)).fetchone()
        if row is None:
            raise ValueError('REPORT_TASK_SOURCE_MISSING')
        tasks.append([tuple(row),task_source_state(connection,child_id,item_id)])
    encoded=json.dumps(['report_task_source_v1',child_id,session_id,day,session[2],tasks],
                       ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')
    return 'report_task_source_v1:'+hashlib.sha256(encoded).hexdigest()
