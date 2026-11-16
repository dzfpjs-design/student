"""Explicit, versioned consumption of unplaced work; no synthetic plan items."""
from contextlib import contextmanager
from datetime import date, timedelta
import json
import math
from uuid import uuid4

from . import learning_input_replan as replan
from .agent_continuation import ContinuationStore
from .learning_input_revision import LOCAL, _json, _version


class CarryoverConflict(replan.ReplanConflict):
    pass


class LearningInputCarryoverService:
    def __init__(self, database):
        self.database = database
        self.replanner = replan.LearningInputReplanService(database)

    def initialize(self):
        self.replanner.initialize()
        with self.database.connect() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS learning_input_carryover_event(
                event_id TEXT PRIMARY KEY,
                obligation_id TEXT NOT NULL REFERENCES learning_input_replan_unplaced(obligation_id) ON DELETE CASCADE,
                child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                source_session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
                target_session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
                revision INTEGER NOT NULL,operation TEXT NOT NULL,client_action_id TEXT NOT NULL,
                request_json TEXT NOT NULL,result_json TEXT NOT NULL,created_at TEXT NOT NULL,
                UNIQUE(obligation_id,revision),UNIQUE(child_id,client_action_id))""")
            c.execute("""CREATE TABLE IF NOT EXISTS learning_input_carryover_plan_receipt(
                receipt_id TEXT PRIMARY KEY,child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
                plan_id TEXT NOT NULL REFERENCES daily_plan(plan_id) ON DELETE CASCADE,
                client_action_id TEXT NOT NULL,request_json TEXT NOT NULL,result_json TEXT NOT NULL,
                UNIQUE(child_id,client_action_id),UNIQUE(plan_id))""")
            c.commit()

    @staticmethod
    def _available(c):
        return bool(c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='learning_input_carryover_event'").fetchone())

    @contextmanager
    def _write(self, connection):
        if connection is None:
            with self.database.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                with self._write(c) as borrowed:
                    yield borrowed
                c.commit()
            return
        if not connection.in_transaction:
            raise CarryoverConflict('CARRYOVER_WRITE_TRANSACTION_REQUIRED')
        point = 'carry_' + uuid4().hex
        connection.execute(f'SAVEPOINT {point}')
        try:
            yield connection
        except Exception:
            connection.execute(f'ROLLBACK TO {point}')
            connection.execute(f'RELEASE {point}')
            raise
        connection.execute(f'RELEASE {point}')

    def _target(self, c, child_id, sid):
        session = c.execute("""SELECT s.*,p.grade,p.status AS child_status FROM daily_session s
            JOIN child_profile p USING(child_id) WHERE s.session_id=? AND s.child_id=?""", (sid, child_id)).fetchone()
        today = replan.family_time(replan.utc_now()).date().isoformat()
        if (not session or session['status'] != 'active' or session['child_status'] != 'active'
                or session['state'] not in {'homework_capture', 'plan_review', 'study', 'break'}):
            raise CarryoverConflict('CARRYOVER_TARGET_INACTIVE')
        if replan.family_time(replan._instant(session['started_at'])).date().isoformat() != today:
            raise CarryoverConflict('CARRYOVER_TARGET_DAY_CHANGED')
        plans = c.execute("SELECT * FROM daily_plan WHERE child_id=? AND session_id=? AND status='active'", (child_id, sid)).fetchall()
        if len(plans) > 1 or plans and plans[0]['plan_date'] != today:
            raise CarryoverConflict('CARRYOVER_PLAN_DAY_OR_COUNT_INVALID')
        plan = dict(plans[0]) if plans else None
        rule = c.execute(f'SELECT {replan.RULE_FIELDS} FROM family_rule WHERE child_id=?', (child_id,)).fetchone()
        context = c.execute('SELECT * FROM session_planning_context WHERE session_id=?', (sid,)).fetchone()
        if not rule or not context:
            raise CarryoverConflict('CARRYOVER_BUDGET_UNKNOWN')
        items = [dict(row) for row in c.execute('SELECT * FROM plan_item WHERE plan_id=? ORDER BY position,plan_item_id', (plan['plan_id'],))] if plan else []
        from .report_followup import ReportFollowupService
        parent = ReportFollowupService(self.database).project_for_plan(child_id, today, connection=c)
        version = _version([child_id, sid, today, plan, items, dict(rule), dict(context), parent['fingerprint']])
        return dict(session), plan, dict(rule), dict(context), items, today, version

    def _origin(self, c, child_id, oid, _seen=None):
        seen = set(_seen or ())
        if oid in seen:
            raise CarryoverConflict('CARRYOVER_RECLAIM_CYCLE')
        seen.add(oid)
        row = c.execute("""SELECT u.*,r.event_id FROM learning_input_replan_unplaced u
            JOIN learning_input_replan_receipt r USING(receipt_id)
            WHERE u.obligation_id=? AND u.child_id=? AND r.child_id=u.child_id AND r.session_id=u.session_id""", (oid, child_id)).fetchone()
        if not row:
            raise CarryoverConflict('CARRYOVER_OBLIGATION_NOT_OWNED')
        payload = json.loads(row['payload_json'])
        projected = self.replanner.revisions.inspect_replan_obligation(child_id, row['event_id'], connection=c)
        source = payload['source_binding']['source']
        valid = (projected.get('status') == 'recorded' and projected.get('source_current') is True
                 and source['status'] == 'pending' and source['confirmation_status'] == 'child_confirmed')
        reclaim = payload.get('reclaim')
        scope = None
        if reclaim:
            current = c.execute('SELECT i.* FROM plan_item i JOIN daily_plan p USING(plan_id) WHERE i.plan_item_id=? AND p.child_id=?',
                                (reclaim['plan_item_id'], child_id)).fetchone()
            binding = c.execute('SELECT source_binding_json FROM learning_input_replan_item WHERE plan_item_id=? AND child_id=?',
                                (reclaim['plan_item_id'], child_id)).fetchone()
            event = c.execute('SELECT * FROM plan_item_event WHERE event_id=? AND plan_item_id=?',
                              (reclaim['reschedule_event_id'], reclaim['plan_item_id'])).fetchone()
            homework = c.execute('SELECT * FROM homework_item WHERE homework_id=? AND child_id=?',
                                 (reclaim['source_homework']['homework_id'], child_id)).fetchone()
            valid = bool(valid and current and dict(current) == reclaim['after_item'] and binding
                and _version(json.loads(binding[0])) == reclaim['source_binding_version']
                and event and dict(event) == reclaim['reschedule_event']
                and homework and dict(homework) == reclaim['source_homework'])
            if reclaim.get('parent_obligation_id'):
                parent = self._origin(c, child_id, reclaim['parent_obligation_id'], seen)
                valid = valid and parent['source_current'] and parent['source_version'] == reclaim['parent_source_version']
            scope_row = c.execute("SELECT * FROM learning_input_carryover_event WHERE obligation_id=? AND operation IN ('confirm_scope','withdraw_scope') ORDER BY revision DESC LIMIT 1", (oid,)).fetchone()
            scope = {**dict(scope_row), 'result': json.loads(scope_row['result_json'])} if scope_row else None
            if scope and scope['operation'] == 'withdraw_scope':
                valid = False
        from .learning_input_revision_scope import extend_origin
        return extend_origin(c, {**dict(row), 'payload': payload, 'source': source, 'source_current': bool(valid),
                'scope': scope, 'scope_review_required': bool(reclaim and reclaim['requires_scope_review'] and not scope),
                'source_version': _version([row['event_id'], source, payload['source_binding'].get('artifact_fingerprint')]
                    + ([reclaim, scope['event_id'] if scope else None] if reclaim else []))})

    @staticmethod
    def _latest(c, oid):
        row = c.execute('SELECT * FROM learning_input_carryover_event WHERE obligation_id=? ORDER BY revision DESC LIMIT 1', (oid,)).fetchone()
        return ({**dict(row), 'result': json.loads(row['result_json'])} if row else None)

    def _candidate(self, c, origin, target, latest=None):
        session, _, _, _, _, today, context_version = target
        latest = latest or self._latest(c, origin['obligation_id'])
        value = latest['result'] if latest else {}
        remaining = value.get('remaining_minutes', origin['minutes'])
        status = value.get('status', 'unplaced')
        reason = None
        actions = ['continue_today', 'drop', 'keep'] if remaining > 0 else []
        scope_required = origin.get('scope_review_required') and status != 'dropped'
        if scope_required:
            status, actions, reason = 'awaiting_scope_review', ['drop', 'keep'], 'executed_remaining_scope_unknown'
        if not origin['source_current']:
            status, actions, reason = 'source_invalid', [], 'original_source_changed'
        elif origin['reason'] in {'executed_scope_requires_review', 'remaining_scope_requires_review', 'no_cancelled_unstarted_school_obligation'} and not (origin.get('revision_scope') and origin.get('scope')):
            actions, reason = ['drop', 'keep'], origin['reason']
        if origin['source_current'] and scope_required and origin.get('revision_scope') and not origin['revision_scope']['execution_settled']:
            reason = 'original_execution_unsettled'
        if value.get('not_before') and today < value['not_before']:
            actions = [action for action in actions if action != 'continue_today']
            reason = 'kept_for_future_date'
        if status == 'accepted_for_planning':
            pending_session = c.execute('SELECT status FROM daily_session WHERE session_id=? AND child_id=?', (latest['target_session_id'], origin['child_id'])).fetchone()
            if pending_session and pending_session[0] == 'active':
                actions = [action for action in actions if action != 'continue_today']
                reason = 'accepted_homework_awaits_plan'
        binding = {'version': 'learning_input_unplaced_v1', 'child_id': origin['child_id'],
            'target_session_id': session['session_id'], 'obligation_id': origin['obligation_id'],
            'revision': latest['revision'] if latest else 0, 'source_version': origin['source_version'], 'context_version': context_version}
        review = origin.get('revision_scope') or origin['payload'].get('reclaim')
        return {**binding, 'binding': binding, 'status': status, 'source_current': origin['source_current'],
            'remaining_minutes': remaining, 'subject': origin['source']['subject'], 'title': origin['source']['title'],
            'source_session_id': origin['session_id'], 'source_event_id': origin['event_id'],
            'scope_review_required': bool(scope_required),
            'scope_confirmed': bool(origin.get('scope') and origin['scope']['operation'] == 'confirm_scope'),
            'scope_review': ({'maximum_remaining_minutes': review['maximum_remaining_minutes'],
                             'old_budget_remaining_minutes': review['maximum_remaining_minutes'],
                             'maximum_is_estimate_limit': False,
                             'plan_item_id': review['plan_item_id']} if scope_required else None),
            'not_before': value.get('not_before'), 'allowed_actions': actions, 'reason': reason, **LOCAL}

    def list_unplaced(self, child_id, target_session_id, *, connection=None):
        if connection is None:
            with self.database.connect() as c:
                c.execute('BEGIN')
                return self.list_unplaced(child_id, target_session_id, connection=c)
        c = connection
        if not c.in_transaction:
            c.execute('BEGIN')
        target = self._target(c, child_id, target_session_id)
        rows = c.execute('SELECT obligation_id FROM learning_input_replan_unplaced WHERE child_id=? ORDER BY rowid', (child_id,)).fetchall() if self._available(c) else []
        return {'items': [self._candidate(c, self._origin(c, child_id, row[0]), target) for row in rows],
                'context_version': target[-1], 'planning_date': target[-2], **LOCAL}

    @staticmethod
    def _authorize(c, request, binding, *, replay=False, capability='learning_inputs.review_unplaced'):
        row = c.execute("""SELECT t.*,p.child_id AS plan_child,p.session_id AS plan_session,p.status AS plan_status,
            p.context_manifest_json,s.status AS step_status,s.capability_name,s.capability_version,s.execution_location,s.arguments_json,s.idempotency_key
            FROM agent_turn t JOIN agent_plan p ON p.agent_turn_id=t.agent_turn_id JOIN agent_step s USING(plan_id)
            WHERE t.agent_turn_id=? AND s.step_index=0""", (request['authorized_turn_id'],)).fetchone()
        if (not row or row['child_id'] != request['child_id'] or row['plan_child'] != request['child_id']
                or row['session_id'] != request['target_session_id'] or row['plan_session'] != request['target_session_id']
                or row['transcript'] != request['request_quote'] or row['intent'] == 'runtime_continuation'
                or row['capability_name'] != capability
                or row['capability_version'] != '1' or row['execution_location'] != 'server'
                or row['idempotency_key'] != request['client_action_id']
                or json.loads(row['context_manifest_json']).get('learning_input_unplaced_binding') != binding):
            raise CarryoverConflict('CARRYOVER_COMMAND_BINDING_INVALID')
        args = json.loads(row['arguments_json'])
        fields = {'decision', 'request_quote', 'not_before'} if capability == 'learning_inputs.review_unplaced' else {'operation', 'remaining_minutes', 'remaining_scope_quote', 'request_quote'}
        if (set(args) - fields or any(args.get(k) != request.get(k) for k in fields)):
            raise CarryoverConflict('CARRYOVER_COMMAND_ARGUMENTS_INVALID')
        if not replay and (row['tool_status'] not in {'pending', 'failed'} or row['step_status'] != 'running' or row['plan_status'] != 'running'
                           or not ContinuationStore.execution_allowed(c, row['agent_turn_id'], running=True)):
            raise CarryoverConflict('CARRYOVER_AUTHORIZATION_REVOKED')
        if not replay and capability == 'learning_inputs.review_reclaimed_scope':
            manifest = json.loads(row['context_manifest_json'])
            if manifest.get('learning_input_arrival_id'):
                latest = c.execute('SELECT arrival_id FROM learning_input_arrival WHERE session_id=? ORDER BY rowid DESC LIMIT 1', (row['session_id'],)).fetchone()
                if not latest or latest[0] != manifest['learning_input_arrival_id']:
                    raise CarryoverConflict('CARRYOVER_SCOPE_ARRIVAL_SUPERSEDED')
        return row

    @staticmethod
    def _binding(request):
        return {'version': 'learning_input_unplaced_v1', 'child_id': request['child_id'],
            'target_session_id': request['target_session_id'], 'obligation_id': request['obligation_id'],
            'revision': request['expected_revision'], 'source_version': request['expected_source_version'],
            'context_version': request['expected_context_version']}

    def review_unplaced(self, child_id, obligation_id, target_session_id, decision, expected_revision,
                        expected_source_version, client_action_id, *, expected_context_version,
                        authorized_turn_id, request_quote, not_before=None, connection=None):
        request = dict(child_id=child_id, obligation_id=obligation_id, target_session_id=target_session_id,
            decision=decision, expected_revision=expected_revision, expected_source_version=expected_source_version,
            expected_context_version=expected_context_version, client_action_id=client_action_id,
            authorized_turn_id=authorized_turn_id, request_quote=request_quote, not_before=not_before)
        if (decision not in {'continue_today', 'drop', 'keep'} or type(expected_revision) is not int or expected_revision < 0
                or any(not isinstance(request[key], str) or not request[key].strip() or len(request[key]) > 200 for key in
                       ('child_id', 'obligation_id', 'target_session_id', 'client_action_id', 'authorized_turn_id', 'expected_source_version', 'expected_context_version'))
                or not isinstance(request_quote, str) or not request_quote.strip()):
            raise CarryoverConflict('CARRYOVER_REQUEST_INVALID')
        with self._write(connection) as c:
            old = c.execute('SELECT request_json,result_json FROM learning_input_carryover_event WHERE child_id=? AND client_action_id=?', (child_id, client_action_id)).fetchone()
            if old:
                if old['request_json'] != _json(request):
                    raise CarryoverConflict('CARRYOVER_IDEMPOTENCY_CONFLICT')
                self._authorize(c, request, self._binding(request), replay=True)
                return json.loads(old['result_json'])
            target = self._target(c, child_id, target_session_id)
            origin = self._origin(c, child_id, obligation_id)
            latest = self._latest(c, obligation_id)
            candidate = self._candidate(c, origin, target, latest)
            if candidate['binding'] != self._binding(request) or decision not in candidate['allowed_actions']:
                raise CarryoverConflict('CARRYOVER_SOURCE_VERSION_OR_ACTION_CHANGED')
            self._authorize(c, request, candidate['binding'])
            if decision == 'keep':
                try:
                    keep_date = date.fromisoformat(not_before)
                    today = date.fromisoformat(target[-2])
                    grounded = not_before in request_quote or ('明天' in request_quote and keep_date == today + timedelta(days=1)) or ('后天' in request_quote and keep_date == today + timedelta(days=2))
                    if keep_date.isoformat() != not_before or keep_date <= today or not grounded:
                        raise ValueError()
                except (ValueError, TypeError):
                    raise CarryoverConflict('CARRYOVER_KEEP_DATE_INVALID') from None
            elif not_before is not None:
                raise CarryoverConflict('CARRYOVER_UNEXPECTED_KEEP_DATE')
            self._cancel_unallocated_acceptance(c, latest)
            eid = str(uuid4())
            result = {'event_id': eid, 'obligation_id': obligation_id, 'revision': expected_revision + 1,
                'status': 'dropped' if decision == 'drop' else 'kept', 'remaining_minutes': 0 if decision == 'drop' else candidate['remaining_minutes'],
                'allocated_minutes': 0, 'newitems': [], 'target_homework_id': None, 'not_before': not_before,
                'kind': 'historical_write_receipt', 'learning_evidence_changed': False, **LOCAL}
            if decision == 'continue_today':
                if target[1] is None:
                    homework = self._homework(c, origin, target_session_id, candidate['remaining_minutes'], eid)
                    result.update(status='accepted_for_planning', target_homework_id=homework['homework_id'], target_homework=homework)
                else:
                    self._allocate(c, origin, target, eid, result)
            self._save(c, origin, target_session_id, decision, request, result)
            if result['newitems']:
                self._bind(c, origin, result, target[1])
            return result

    @staticmethod
    def _cancel_unallocated_acceptance(c, latest):
        if not latest or latest['result']['status'] != 'accepted_for_planning':
            return
        hid = latest['result']['target_homework_id']
        if c.execute('SELECT 1 FROM plan_item WHERE homework_id=?', (hid,)).fetchone():
            raise CarryoverConflict('CARRYOVER_ACCEPTED_PLAN_NOT_RECONCILED')
        c.execute("UPDATE homework_item SET status='cancelled' WHERE homework_id=? AND status='pending'", (hid,))

    @staticmethod
    def _homework(c, origin, sid, minutes, eid):
        source = origin['source']
        scope = (origin.get('scope') or {}).get('result', {}).get('remaining_scope_quote')
        if not scope and origin['payload'].get('reclaim'):
            source = origin['payload']['reclaim']['source_homework']
        hid = str(uuid4())
        c.execute("""INSERT INTO homework_item(homework_id,child_id,session_id,subject,task_type,title,range_text,
            expected_minutes,due_at,special_requirements,source_artifact_id,ocr_confidence,confirmation_status,status,created_at,client_action_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'child_confirmed','pending',?,?)""",
            (hid, origin['child_id'], sid, source['subject'], source['task_type'],
             source['title'] + f'（剩余：{scope}）' if scope else source['title'], scope or source.get('range_text'),
             minutes, source.get('due_at'), source.get('special_requirements'), source.get('source_artifact_id'), source.get('ocr_confidence'),
             replan.utc_now().isoformat(), 'carryover:' + eid))
        return dict(c.execute('SELECT * FROM homework_item WHERE homework_id=?', (hid,)).fetchone())

    def _allocate(self, c, origin, target, eid, result):
        session, plan, rule, context, items, _, _ = target
        budget_items = self._budget_items(c, origin['child_id'], items)
        budget, available, reason = self.replanner._budget(c, origin['child_id'], session, plan, budget_items, rule, context,
                                                          origin['source'], replan.utc_now().astimezone(replan.ZONE))
        if reason:
            available = 0
        segment, rest_minutes = budget['effective_task_segment_minutes_cap'], int(rule['break_minutes'])
        if segment < 1 or rest_minutes < 0:
            raise CarryoverConflict('CARRYOVER_RULE_INVALID')
        remaining = result['remaining_minutes']
        allocations = []
        needs_break = budget['has_prior_learning']
        while remaining and available > 0:
            rest = rest_minutes if needs_break else 0
            minutes = min(segment, remaining, max(0, available // 60 - rest))
            if minutes < 1:
                break
            allocations.extend([(kind, value) for kind, value in [('planned_break', rest), ('school_homework', minutes)] if value])
            remaining -= minutes
            available -= (rest + minutes) * 60
            needs_break = True
        allocated = result['remaining_minutes'] - remaining
        result.update(status='scheduled' if not remaining else 'partial' if allocated else 'deferred',
            remaining_minutes=remaining, allocated_minutes=allocated, budget=budget, reason=reason or (budget['limiting_reason'] if remaining else None))
        if not allocated:
            return
        homework = self._homework(c, origin, session['session_id'], allocated, eid)
        result.update(target_homework_id=homework['homework_id'], target_homework=homework)
        position = max((item['position'] for item in items), default=0)
        for kind, minutes in allocations:
            position += 1
            iid = str(uuid4())
            c.execute("""INSERT INTO plan_item(plan_item_id,plan_id,homework_id,subject,title,expected_minutes,priority,status,position,source_type,source_ref,completion_standard,task_type)
                VALUES (?,?,?,?,?,?,1,'pending',?,?,?,?,?)""", (iid, plan['plan_id'], homework['homework_id'],
                homework['subject'] if kind == 'school_homework' else '休息', homework['title'] if kind == 'school_homework' else '分段休息',
                minutes, position, kind, homework['homework_id'] if kind == 'school_homework' else origin['obligation_id'],
                '核对剩余作业并独立完成当前一段' if kind == 'school_homework' else '完成分段休息', homework['task_type'] if kind == 'school_homework' else 'break'))
            result['newitems'].append(dict(c.execute('SELECT * FROM plan_item WHERE plan_item_id=?', (iid,)).fetchone()))
        total = c.execute("SELECT COALESCE(SUM(expected_minutes),0) FROM plan_item WHERE plan_id=? AND status!='cancelled'", (plan['plan_id'],)).fetchone()[0]
        if c.execute("UPDATE daily_plan SET version=version+1,total_minutes=? WHERE plan_id=? AND version=? AND status='active'", (total, plan['plan_id'], plan['version'])).rowcount != 1:
            raise CarryoverConflict('CARRYOVER_PLAN_CHANGED')
        plan['version'] += 1

    @staticmethod
    def _save(c, origin, sid, operation, request, result):
        c.execute('INSERT INTO learning_input_carryover_event VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            (result['event_id'], origin['obligation_id'], origin['child_id'], origin['session_id'], sid,
             result['revision'], operation, request['client_action_id'], _json(request), _json(result), replan.utc_now().isoformat()))

    @staticmethod
    def _bind(c, origin, result, plan, *, allocated=True):
        for item in result['newitems']:
            binding = {'version': 'learning_input_carryover_source_v1', 'carryover_event_id': result['event_id'],
                'obligation_id': origin['obligation_id'], 'allocated': allocated, 'child_id': origin['child_id'], 'session_id': result['target_homework']['session_id'],
                'origin_source_version': origin['source_version'],
                'homework_id': result['target_homework_id'], 'target_homework': result['target_homework'],
                'plan_id': plan['plan_id'], 'plan_date': plan['plan_date'], 'to_plan_version': plan['version'],
                'item': {field: item[field] for field in ('source_type', 'source_ref', 'subject', 'title', 'expected_minutes', 'task_type')}}
            c.execute('INSERT INTO learning_input_replan_item VALUES (?,?,?,?,?)',
                (item['plan_item_id'], origin['receipt_id'], origin['child_id'], binding['session_id'], _json(binding)))
            c.execute("INSERT INTO plan_item_event VALUES (?,?,'revision_replanned',?,?,?)",
                (str(uuid4()), item['plan_item_id'], replan.utc_now().isoformat(), _json(binding), 'carryover-bind:' + item['plan_item_id']))

    def _budget_items(self, c, child_id, items):
        # Deferred generator placeholders are tracked by the root obligation, not today's capacity.
        return [item for item in items if not (item['status'] == 'rescheduled'
                and (self.owns_homework(c, child_id, item['homework_id']) or self.owns_unplaced_item(c, child_id, item['plan_item_id'])))]

    @staticmethod
    def _check_generated_rests(items, active, rest_minutes, spent):
        carried = {item['plan_item_id'] for item in active}
        prior, rest = spent > 0, 0
        for item in items:
            if item['status'] not in {'pending', 'active', 'paused'}:
                continue
            if item['source_type'] == 'planned_break':
                rest += item['expected_minutes']
                continue
            if item['plan_item_id'] in carried and prior and rest < rest_minutes:
                raise CarryoverConflict('CARRYOVER_GENERATED_REST_MISSING')
            prior, rest = True, 0

    def bind_generated_plan(self, child_id, session_id, plan_id, client_action_id, *, connection):
        if not isinstance(client_action_id, str) or not client_action_id.strip() or len(client_action_id) > 200:
            raise CarryoverConflict('CARRYOVER_PLAN_ACTION_REQUIRED')
        request = dict(child_id=child_id, session_id=session_id, plan_id=plan_id, client_action_id=client_action_id)
        with self._write(connection) as c:
            old = c.execute('SELECT * FROM learning_input_carryover_plan_receipt WHERE child_id=? AND (client_action_id=? OR plan_id=?)', (child_id, client_action_id, plan_id)).fetchone()
            if old:
                if old['request_json'] != _json(request):
                    raise CarryoverConflict('CARRYOVER_PLAN_REPLAY_CONFLICT')
                return json.loads(old['result_json'])
            target = self._target(c, child_id, session_id)
            session, plan, rule, context, items, _, _ = target
            if not plan or plan['plan_id'] != plan_id:
                raise CarryoverConflict('CARRYOVER_PLAN_NOT_CURRENT')
            rows = c.execute("""SELECT * FROM learning_input_carryover_event e WHERE child_id=? AND target_session_id=?
                AND revision=(SELECT MAX(revision) FROM learning_input_carryover_event WHERE obligation_id=e.obligation_id)
                AND json_extract(result_json,'$.status')='accepted_for_planning' ORDER BY rowid""", (child_id, session_id)).fetchall()
            receipts = []
            for row in rows:
                accepted = json.loads(row['result_json'])
                original_request = json.loads(row['request_json'])
                authority = self._authorize(c, original_request, self._binding(original_request), replay=True)
                if authority['plan_status'] == 'cancelled' or authority['step_status'] == 'cancelled':
                    raise CarryoverConflict('CARRYOVER_AUTHORIZATION_REVOKED')
                origin = self._origin(c, child_id, row['obligation_id'])
                homework = c.execute('SELECT * FROM homework_item WHERE homework_id=? AND child_id=? AND session_id=?', (accepted['target_homework_id'], child_id, session_id)).fetchone()
                if not origin['source_current'] or not homework or dict(homework) != accepted['target_homework']:
                    raise CarryoverConflict('CARRYOVER_ACCEPTED_SOURCE_CHANGED')
                bound = [item for item in items if item['homework_id'] == accepted['target_homework_id']]
                active = [item for item in bound if item['status'] == 'pending']
                if any(item['status'] not in {'pending', 'rescheduled', 'cancelled'} or item['started_at'] or item['completed_at'] or item['active_seconds'] or item['actual_minutes'] or item['active_segment_started_at'] for item in bound):
                    raise CarryoverConflict('CARRYOVER_PLAN_ALREADY_EXECUTED')
                if any(item['source_type'] != 'school_homework' or item['source_ref'] != homework['homework_id']
                       or item['subject'] != homework['subject'] or item['task_type'] != homework['task_type']
                       or item['title'] not in {homework['title'], homework['title'] + f'（续{index + 1}）'}
                       for index, item in enumerate(bound) if item['status'] == 'pending'):
                    raise CarryoverConflict('CARRYOVER_PLAN_SOURCE_MISMATCH')
                budget, _, reason = self.replanner._budget(c, child_id, session, plan, self._budget_items(c, child_id, items), rule, context, origin['source'], replan.utc_now().astimezone(replan.ZONE))
                spent = budget['actual_spent_seconds']
                reserved = sum(budget[key] for key in ('protected_remaining_seconds', 'fixed_reserved_seconds', 'pet_reserved_seconds'))
                wall = (replan._instant(budget['cutoff_at']) - replan.utc_now()).total_seconds()
                if active and (reason or spent + reserved > budget['effective_daily_total_minutes_cap'] * 60 or reserved > wall):
                    raise CarryoverConflict('CARRYOVER_GENERATED_PLAN_OVER_BUDGET')
                if any(item['expected_minutes'] > budget['effective_task_segment_minutes_cap'] for item in active):
                    raise CarryoverConflict('CARRYOVER_GENERATED_SEGMENT_TOO_LONG')
                self._check_generated_rests(items, active, int(rule['break_minutes']), spent)
                allocated = sum(item['expected_minutes'] for item in active)
                if allocated > accepted['remaining_minutes']:
                    raise CarryoverConflict('CARRYOVER_ALLOCATION_EXCEEDS_OBLIGATION')
                result = {**accepted, 'event_id': str(uuid4()), 'revision': row['revision'] + 1,
                    'status': 'scheduled' if allocated == accepted['remaining_minutes'] else 'partial' if allocated else 'deferred',
                    'allocated_minutes': allocated, 'remaining_minutes': accepted['remaining_minutes'] - allocated,
                    'newitems': active, 'budget': budget,
                    'reason': (reason or 'generated_plan_capacity') if allocated < accepted['remaining_minutes'] else None}
                allocation_request = {**request, 'client_action_id': 'allocation:' + _version([client_action_id, row['obligation_id']])}
                self._save(c, origin, session_id, 'allocate', allocation_request, result)
                self._bind(c, origin, result, plan)
                self._bind(c, origin, {**result, 'newitems': [item for item in bound if item['status'] != 'pending']}, plan, allocated=False)
                receipts.append(result)
            result = {'receipt_id': str(uuid4()), 'plan_id': plan_id, 'allocations': receipts,
                'allocated_minutes': sum(row['allocated_minutes'] for row in receipts), 'kind': 'historical_write_receipt', **LOCAL}
            c.execute('INSERT INTO learning_input_carryover_plan_receipt VALUES (?,?,?,?,?,?,?)',
                (result['receipt_id'], child_id, session_id, plan_id, client_action_id, _json(request), _json(result)))
            return result

    def inspect_item(self, child_id, plan_item_id, *, connection=None):
        if connection is None:
            with self.database.connect() as c:
                c.execute('BEGIN')
                return self.inspect_item(child_id, plan_item_id, connection=c)
        c = connection
        row = c.execute("""SELECT b.source_binding_json,i.*,p.child_id,p.session_id,p.status AS plan_status,p.plan_date,p.version AS plan_version,s.status AS session_status,cp.status AS child_status
            FROM learning_input_replan_item b JOIN plan_item i USING(plan_item_id) JOIN daily_plan p USING(plan_id)
            JOIN daily_session s ON s.session_id=p.session_id AND s.child_id=p.child_id JOIN child_profile cp ON cp.child_id=p.child_id
            WHERE i.plan_item_id=? AND b.child_id=? AND p.child_id=?""", (plan_item_id, child_id, child_id)).fetchone()
        if not row or not self._available(c):
            return {'status': 'unknown', 'eligible': False, **LOCAL}
        binding = json.loads(row['source_binding_json'])
        if binding.get('version') != 'learning_input_carryover_source_v1':
            return {'status': 'unknown', 'eligible': False, **LOCAL}
        try:
            origin = self._origin(c, child_id, binding['obligation_id'])
        except CarryoverConflict:
            return {'status': 'stale', 'eligible': False, **LOCAL}
        homework = c.execute('SELECT * FROM homework_item WHERE homework_id=? AND child_id=? AND session_id=?', (binding['homework_id'], child_id, binding['session_id'])).fetchone()
        event = c.execute('SELECT 1 FROM learning_input_carryover_event WHERE event_id=? AND child_id=?', (binding['carryover_event_id'], child_id)).fetchone()
        valid = bool(binding.get('allocated') is True and origin['source_current'] and event and homework and dict(homework) == binding['target_homework']
            and binding.get('origin_source_version', origin['source_version']) == origin['source_version']
            and not self._reclaimed_item(c, child_id, plan_item_id)
            and row['child_status'] == 'active'
            and row['session_status'] == 'active' and row['plan_status'] == 'active' and row['status'] in {'pending', 'active', 'paused'}
            and row['plan_date'] == replan.utc_now().astimezone().date().isoformat()
            and row['plan_date'] == binding['plan_date'] and row['plan_version'] >= binding['to_plan_version']
            and row['session_id'] == binding['session_id'] and row['homework_id'] == binding['homework_id']
            and all(row[key] == value for key, value in binding['item'].items()))
        return {'status': 'current' if valid else 'stale', 'eligible': valid, **LOCAL}

    @classmethod
    def owns_homework(cls, connection, child_id, homework_id):
        return bool(cls._available(connection) and connection.execute("SELECT 1 FROM learning_input_carryover_event WHERE child_id=? AND json_extract(result_json,'$.target_homework_id')=?", (child_id, homework_id)).fetchone())

    @classmethod
    def owns_unplaced_item(cls, connection, child_id, plan_item_id):
        if not cls._available(connection):
            return False
        row = connection.execute('SELECT source_binding_json FROM learning_input_replan_item WHERE child_id=? AND plan_item_id=?', (child_id, plan_item_id)).fetchone()
        binding = json.loads(row[0]) if row else {}
        return (binding.get('version') == 'learning_input_carryover_source_v1' and binding.get('allocated') is False
                or cls._reclaimed_item(connection, child_id, plan_item_id))

    @staticmethod
    def _reclaimed_item(c, child_id, plan_item_id):
        return bool(c.execute("SELECT 1 FROM learning_input_replan_unplaced WHERE child_id=? AND json_extract(payload_json,'$.reclaim.plan_item_id')=?", (child_id, plan_item_id)).fetchone())

    def prepare_reschedule(self, child_id, plan_item_id, *, connection):
        c = connection
        if not c.in_transaction:
            raise CarryoverConflict('CARRYOVER_WRITE_TRANSACTION_REQUIRED')
        record = c.execute('SELECT * FROM learning_input_replan_item WHERE child_id=? AND plan_item_id=?', (child_id, plan_item_id)).fetchone()
        if not record:
            if c.execute("SELECT 1 FROM plan_item_event WHERE plan_item_id=? AND event_type='revision_replanned'", (plan_item_id,)).fetchone():
                raise CarryoverConflict('CARRYOVER_SOURCE_BINDING_MISSING')
            return {'status': 'not_applicable', **LOCAL}
        source_binding = json.loads(record['source_binding_json'])
        item = c.execute('SELECT i.* FROM plan_item i JOIN daily_plan p USING(plan_id) WHERE i.plan_item_id=? AND p.child_id=?', (plan_item_id, child_id)).fetchone()
        if not item or item['source_type'] != 'school_homework' or source_binding.get('allocated') is False:
            return {'status': 'not_applicable', **LOCAL}
        if (item['status'] not in {'pending', 'paused'} or item['active_segment_started_at'] or item['completed_at']
                or self._reclaimed_item(c, child_id, plan_item_id)
                or not self.replanner.inspect_item(child_id, plan_item_id, connection=c)['eligible']):
            raise CarryoverConflict('CARRYOVER_RESCHEDULE_SOURCE_INVALID')
        parent = None
        if source_binding['version'] == 'learning_input_carryover_source_v1':
            parent = self._origin(c, child_id, source_binding['obligation_id'])
            original = parent['payload']['source_binding']
        else:
            original = source_binding
        spent = self._recorded_spent(item)
        sibling_started = bool(c.execute("SELECT 1 FROM plan_item WHERE homework_id=? AND plan_item_id!=? AND source_type='school_homework' AND (started_at IS NOT NULL OR completed_at IS NOT NULL OR active_seconds>0 OR actual_minutes>0)", (item['homework_id'], plan_item_id)).fetchone())
        homework = c.execute('SELECT * FROM homework_item WHERE homework_id=? AND child_id=?', (item['homework_id'], child_id)).fetchone()
        if not homework:
            raise CarryoverConflict('CARRYOVER_RESCHEDULE_HOMEWORK_MISSING')
        return {'status': 'prepared', 'version': 'carryover_reclaim_v1', 'child_id': child_id,
            'plan_item_id': plan_item_id, 'before_item': dict(item), 'source_binding_version': _version(source_binding),
            'receipt_id': record['receipt_id'], 'source_session_id': record['session_id'], 'source_binding': original,
            'parent_obligation_id': parent['obligation_id'] if parent else None,
            'parent_source_version': parent['source_version'] if parent else None,
            'source_homework': dict(homework), 'sibling_started': sibling_started,
            'requires_scope_review': bool(item['started_at'] or spent or item['status'] == 'paused' or sibling_started),
            'spent_seconds': spent, 'maximum_remaining_minutes': max(0, item['expected_minutes'] - math.ceil(spent / 60)), **LOCAL}

    @staticmethod
    def _recorded_spent(item):
        try:
            values = [float(item['active_seconds'] or 0), float(item['actual_minutes'] or 0) * 60]
            if any(not math.isfinite(value) or value < 0 for value in values):
                raise ValueError()
            return math.ceil(max(values))
        except (TypeError, ValueError, OverflowError):
            raise CarryoverConflict('CARRYOVER_RECORDED_TIME_INVALID') from None

    def reclaim_rescheduled(self, child_id, plan_item_id, reschedule_event_id, client_action_id, *, connection):
        if any(not isinstance(value, str) or not value.strip() or len(value) > 200 for value in (child_id, plan_item_id, reschedule_event_id, client_action_id)):
            raise CarryoverConflict('CARRYOVER_RECLAIM_REQUEST_INVALID')
        request = dict(child_id=child_id, plan_item_id=plan_item_id, reschedule_event_id=reschedule_event_id, client_action_id=client_action_id)
        with self._write(connection) as c:
            old = c.execute('SELECT request_json,result_json FROM learning_input_carryover_event WHERE child_id=? AND client_action_id=?', (child_id, client_action_id)).fetchone()
            if old:
                if json.loads(old['request_json']) != request:
                    raise CarryoverConflict('CARRYOVER_RECLAIM_REPLAY_CONFLICT')
                return json.loads(old['result_json'])
            event = c.execute('SELECT * FROM plan_item_event WHERE event_id=? AND plan_item_id=? AND event_type=?', (reschedule_event_id, plan_item_id, 'rescheduled')).fetchone()
            if not event:
                raise CarryoverConflict('CARRYOVER_RESCHEDULE_EVENT_REQUIRED')
            if not c.execute('SELECT 1 FROM plan_item i JOIN daily_plan p USING(plan_id) WHERE i.plan_item_id=? AND p.child_id=?', (plan_item_id, child_id)).fetchone():
                raise CarryoverConflict('CARRYOVER_RESCHEDULE_NOT_OWNED')
            frozen = json.loads(event['payload_json']).get('carryover_reclaim_binding')
            if frozen and frozen.get('status') == 'not_applicable':
                return {'status': 'not_applicable', 'reclaimed_minutes': 0, **LOCAL}
            if not frozen or frozen.get('version') != 'carryover_reclaim_v1' or frozen.get('child_id') != child_id or frozen.get('plan_item_id') != plan_item_id:
                raise CarryoverConflict('CARRYOVER_RESCHEDULE_SNAPSHOT_REQUIRED')
            item = c.execute('SELECT i.* FROM plan_item i JOIN daily_plan p USING(plan_id) WHERE i.plan_item_id=? AND p.child_id=?', (plan_item_id, child_id)).fetchone()
            after = {**frozen['before_item'], 'status': 'rescheduled', 'active_segment_started_at': None}
            source_binding = c.execute('SELECT * FROM learning_input_replan_item WHERE plan_item_id=? AND child_id=?', (plan_item_id, child_id)).fetchone()
            if (not item or dict(item) != after or not source_binding or _version(json.loads(source_binding['source_binding_json'])) != frozen['source_binding_version']
                    or self._reclaimed_item(c, child_id, plan_item_id)):
                raise CarryoverConflict('CARRYOVER_RESCHEDULE_CHANGED_OR_ALREADY_RECLAIMED')
            actual_binding = json.loads(source_binding['source_binding_json'])
            parent = self._origin(c, child_id, actual_binding['obligation_id']) if actual_binding['version'] == 'learning_input_carryover_source_v1' else None
            original = parent['payload']['source_binding'] if parent else actual_binding
            homework = c.execute('SELECT * FROM homework_item WHERE homework_id=? AND child_id=?', (item['homework_id'], child_id)).fetchone()
            bound_item = actual_binding['item'] if parent else {field: actual_binding[field] for field in ('expected_minutes', 'subject', 'title')}
            if (frozen['source_binding'] != original or frozen['receipt_id'] != source_binding['receipt_id']
                    or frozen['source_session_id'] != source_binding['session_id']
                    or frozen['parent_obligation_id'] != (parent['obligation_id'] if parent else None)
                    or frozen['parent_source_version'] != (parent['source_version'] if parent else None)
                    or not homework or dict(homework) != frozen['source_homework']
                    or any(item[field] != value for field, value in bound_item.items())):
                raise CarryoverConflict('CARRYOVER_RESCHEDULE_FROZEN_SOURCE_CHANGED')
            # Recompute the admissible pre-state, without mutating the durable plan item.
            before = frozen['before_item']
            spent = self._recorded_spent(before)
            maximum = max(0, before['expected_minutes'] - math.ceil(spent / 60))
            needs_scope = bool(before['started_at'] or spent or before['status'] == 'paused' or frozen['sibling_started'])
            if before['status'] not in {'pending', 'paused'} or before['completed_at'] or before['active_segment_started_at'] or before['source_type'] != 'school_homework':
                raise CarryoverConflict('CARRYOVER_RESCHEDULE_PRESTATE_INVALID')
            if maximum != frozen['maximum_remaining_minutes'] or needs_scope != frozen['requires_scope_review']:
                raise CarryoverConflict('CARRYOVER_RESCHEDULE_SNAPSHOT_CHANGED')
            oid = str(uuid4())
            minutes = 0 if needs_scope else maximum
            payload = {'obligation_id': oid, 'minutes': minutes, 'source_binding': frozen['source_binding'],
                'reclaim': {**frozen, 'reschedule_event_id': reschedule_event_id, 'reschedule_event': dict(event), 'after_item': after}}
            c.execute('INSERT INTO learning_input_replan_unplaced VALUES (?,?,?,?,?,?,?)',
                (oid, frozen['receipt_id'], child_id, frozen['source_binding']['session_id'], minutes,
                 'executed_remaining_scope_requires_review' if needs_scope else 'rescheduled_allocation', _json(payload)))
            origin = self._origin(c, child_id, oid)
            if not origin['source_current']:
                raise CarryoverConflict('CARRYOVER_RECLAIM_SOURCE_CHANGED')
            result = {'event_id': str(uuid4()), 'obligation_id': oid, 'revision': 1,
                'status': 'awaiting_scope_review' if needs_scope else 'unplaced', 'remaining_minutes': minutes,
                'reclaimed_minutes': minutes, 'allocated_minutes': 0, 'newitems': [], 'target_homework_id': None,
                'not_before': None, 'scope_review_required': needs_scope, 'maximum_remaining_minutes': maximum,
                'source_plan_item_id': plan_item_id, 'reschedule_event_id': reschedule_event_id,
                'kind': 'historical_write_receipt', 'learning_evidence_changed': False, **LOCAL}
            self._save(c, origin, frozen['source_session_id'], 'reclaim', request, result)
            return result

    def review_reclaimed_scope(self, child_id, obligation_id, target_session_id, operation, expected_revision,
                               expected_source_version, client_action_id, *, expected_context_version,
                               authorized_turn_id, request_quote, remaining_minutes=None, remaining_scope_quote=None, connection=None):
        request = dict(child_id=child_id, obligation_id=obligation_id, target_session_id=target_session_id,
            operation=operation, expected_revision=expected_revision, expected_source_version=expected_source_version,
            client_action_id=client_action_id, expected_context_version=expected_context_version,
            authorized_turn_id=authorized_turn_id, request_quote=request_quote, remaining_minutes=remaining_minutes,
            remaining_scope_quote=remaining_scope_quote)
        with self._write(connection) as c:
            old = c.execute('SELECT request_json,result_json FROM learning_input_carryover_event WHERE child_id=? AND client_action_id=?', (child_id, client_action_id)).fetchone()
            if old:
                if json.loads(old['request_json']) != request:
                    raise CarryoverConflict('CARRYOVER_SCOPE_REPLAY_CONFLICT')
                self._authorize(c, request, self._binding(request), replay=True, capability='learning_inputs.review_reclaimed_scope')
                return json.loads(old['result_json'])
            origin = self._origin(c, child_id, obligation_id)
            candidate = self._candidate(c, origin, self._target(c, child_id, target_session_id))
            reclaim = origin['payload'].get('reclaim') or origin.get('revision_scope')
            if (not reclaim or not reclaim['requires_scope_review'] or not origin['source_current'] or candidate['binding'] != self._binding(request)
                    or operation not in {'confirm_scope', 'withdraw_scope'}):
                raise CarryoverConflict('CARRYOVER_SCOPE_SOURCE_OR_VERSION_CHANGED')
            self._authorize(c, request, candidate['binding'], capability='learning_inputs.review_reclaimed_scope')
            if operation == 'confirm_scope':
                if origin.get('revision_scope') and not origin['revision_scope']['execution_settled']:
                    raise CarryoverConflict('CARRYOVER_ORIGINAL_EXECUTION_UNSETTLED')
                from .learning_input_revision import duration_minutes
                from .learning_input_scope_dialogue import validate_scope_offer
                import re
                estimates = [duration_minutes(match) for match in re.findall(r'([零〇一二两三四五六七八九十百0-9]+)分钟', request_quote)]
                offer = validate_scope_offer(c, authorized_turn_id, candidate['binding'], request)
                literal = (estimates == [remaining_minutes] and isinstance(remaining_scope_quote, str)
                           and bool(remaining_scope_quote.strip()) and remaining_scope_quote in request_quote)
                # Old expected-minus-spent is budget history, not a bound on
                # unfinished work. Allocation independently charges all actual time.
                if (candidate['status'] == 'dropped' or type(remaining_minutes) is not int or not 1 <= remaining_minutes <= 1440
                        or not (literal or offer)):
                    raise CarryoverConflict('CARRYOVER_REMAINING_SCOPE_NOT_GROUNDED')
                if c.execute("""SELECT 1 FROM learning_input_carryover_event WHERE obligation_id=? AND operation IN ('continue_today','allocate')
                    AND (json_extract(result_json,'$.allocated_minutes')>0 OR
                         (json_extract(result_json,'$.status')='accepted_for_planning' AND EXISTS
                          (SELECT 1 FROM homework_item WHERE homework_id=json_extract(result_json,'$.target_homework_id') AND status!='cancelled')))""", (obligation_id,)).fetchone():
                    raise CarryoverConflict('CARRYOVER_SCOPE_ALREADY_CONSUMED')
            elif remaining_minutes is not None or remaining_scope_quote is not None:
                raise CarryoverConflict('CARRYOVER_WITHDRAW_HAS_NO_REPLACEMENT')
            latest = self._latest(c, obligation_id)
            if latest and latest['result']['status'] == 'accepted_for_planning':
                self._cancel_unallocated_acceptance(c, latest)
            result = {'event_id': str(uuid4()), 'obligation_id': obligation_id, 'revision': expected_revision + 1,
                'status': 'scope_confirmed' if operation == 'confirm_scope' else 'scope_withdrawn',
                'remaining_minutes': remaining_minutes if operation == 'confirm_scope' else 0,
                'remaining_scope_quote': remaining_scope_quote, 'allocated_minutes': 0, 'newitems': [],
                'target_homework_id': None, 'not_before': latest['result'].get('not_before') if latest else None, 'kind': 'historical_write_receipt',
                'learning_evidence_changed': False, **LOCAL}
            result['old_budget_remaining_minutes'] = reclaim['maximum_remaining_minutes']
            result['estimate_basis'] = 'explicit_remaining_work_not_refunded_budget'
            if origin.get('revision_scope'):
                result['revision_execution'] = origin['revision_scope']['execution']
            if operation == 'confirm_scope' and offer:
                result['scope_provenance'] = offer
            self._save(c, origin, target_session_id, operation, request, result)
            return result
