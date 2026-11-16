from __future__ import annotations

"""Local report feedback handoff. Read receipts are not evidence of effectiveness."""

from contextlib import nullcontext
from datetime import date, datetime
import hashlib
import json
from uuid import uuid4

from .database import Database
from .family_clock import family_today
from .repository import utc_now


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


class ReportFollowupConflict(ValueError):
    pass


class ReportFollowupService:
    def __init__(self, database: Database):
        self.database = database

    def initialize(self):
        from .parent_reports import ParentReportService
        ParentReportService(self.database).initialize()
        self.sync_legacy_feedback()

    @staticmethod
    def ensure_schema(connection):
        # Called outside business transactions, after the report snapshot tables exist.
        connection.executescript("""
        CREATE TABLE IF NOT EXISTS report_feedback_revision (
          event_id TEXT PRIMARY KEY,
          child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
          snapshot_id TEXT NOT NULL REFERENCES parent_report_snapshot(snapshot_id) ON DELETE CASCADE,
          revision INTEGER NOT NULL CHECK(revision>0),
          operation TEXT NOT NULL CHECK(operation IN ('record','retract')),
          feedback_id TEXT REFERENCES parent_report_snapshot_feedback(feedback_id) ON DELETE CASCADE,
          payload_json TEXT NOT NULL,
          client_action_id TEXT NOT NULL,
          created_at TEXT NOT NULL,
          UNIQUE(snapshot_id,revision), UNIQUE(child_id,client_action_id), UNIQUE(feedback_id)
        );
        CREATE TABLE IF NOT EXISTS report_plan_feedback_receipt (
          receipt_id TEXT PRIMARY KEY,
          child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
          plan_id TEXT NOT NULL REFERENCES daily_plan(plan_id) ON DELETE CASCADE,
          planning_date TEXT NOT NULL,
          input_fingerprint TEXT NOT NULL,
          inputs_json TEXT NOT NULL,
          plan_fingerprint TEXT NOT NULL,
          client_action_id TEXT NOT NULL,
          created_at TEXT NOT NULL,
          UNIQUE(child_id,client_action_id)
        );
        CREATE TRIGGER IF NOT EXISTS report_feedback_revision_no_update
        BEFORE UPDATE ON report_feedback_revision BEGIN SELECT RAISE(ABORT,'APPEND_ONLY'); END;
        CREATE TRIGGER IF NOT EXISTS report_plan_feedback_receipt_no_update
        BEFORE UPDATE ON report_plan_feedback_receipt BEGIN SELECT RAISE(ABORT,'APPEND_ONLY'); END;
        CREATE TABLE IF NOT EXISTS report_plan_adjustment_event (
          event_id TEXT PRIMARY KEY,
          adjustment_id TEXT NOT NULL,
          child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
          snapshot_id TEXT NOT NULL REFERENCES parent_report_snapshot(snapshot_id) ON DELETE CASCADE,
          revision INTEGER NOT NULL CHECK(revision>0),
          operation TEXT NOT NULL CHECK(operation IN ('propose','approve','reject','retract')),
          payload_json TEXT NOT NULL,
          client_action_id TEXT NOT NULL,
          created_at TEXT NOT NULL,
          UNIQUE(adjustment_id,revision), UNIQUE(child_id,client_action_id)
        );
        CREATE TRIGGER IF NOT EXISTS report_plan_adjustment_no_update
        BEFORE UPDATE ON report_plan_adjustment_event BEGIN SELECT RAISE(ABORT,'APPEND_ONLY'); END;
        """)

    @staticmethod
    def append_feedback(connection, feedback_id):
        row = connection.execute("""SELECT f.*,s.child_id,s.source_version FROM parent_report_snapshot_feedback f
            JOIN parent_report_snapshot s USING(snapshot_id) WHERE f.feedback_id=?""", (feedback_id,)).fetchone()
        if not row:
            raise KeyError(feedback_id)
        existing = connection.execute('SELECT event_id FROM report_feedback_revision WHERE feedback_id=?', (feedback_id,)).fetchone()
        if existing:
            return existing[0]
        revision = connection.execute('SELECT COALESCE(MAX(revision),0)+1 FROM report_feedback_revision WHERE snapshot_id=?', (row['snapshot_id'],)).fetchone()[0]
        event_id = str(uuid4())
        payload = {key: row[key] for key in ('helpfulness', 'stress_rating', 'notes', 'source_version')}
        connection.execute('INSERT INTO report_feedback_revision VALUES (?,?,?,?,?,?,?,?,?)',
            (event_id, row['child_id'], row['snapshot_id'], revision, 'record', feedback_id,
             _json(payload), 'feedback:' + feedback_id, row['created_at']))
        return event_id

    def sync_legacy_feedback(self):
        """Seed only the latest legacy state; never supersede a versioned revision."""
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            for row in c.execute('''SELECT f.feedback_id FROM parent_report_snapshot_feedback f
                WHERE NOT EXISTS (SELECT 1 FROM report_feedback_revision WHERE snapshot_id=f.snapshot_id)
                  AND f.rowid=(SELECT rowid FROM parent_report_snapshot_feedback
                    WHERE snapshot_id=f.snapshot_id ORDER BY created_at DESC,rowid DESC LIMIT 1)''').fetchall():
                self.append_feedback(c, row[0])
            c.commit()

    @staticmethod
    def _owned(c, child_id, snapshot_id):
        if not c.execute('SELECT 1 FROM parent_report_snapshot WHERE snapshot_id=? AND child_id=?', (snapshot_id, child_id)).fetchone():
            raise KeyError(snapshot_id)

    def feedback_history(self, child_id, snapshot_id):
        with self.database.connect() as c:
            self._owned(c, child_id, snapshot_id)
            return [{**dict(row), 'payload': json.loads(row['payload_json'])} for row in c.execute(
                'SELECT * FROM report_feedback_revision WHERE snapshot_id=? AND child_id=? ORDER BY revision', (snapshot_id, child_id))]

    @staticmethod
    def current_feedback(c, snapshot):
        from .parent_reports import ParentReportService
        latest = None
        if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='report_feedback_revision'").fetchone():
            latest = c.execute('SELECT * FROM report_feedback_revision WHERE snapshot_id=? AND child_id=? ORDER BY revision DESC LIMIT 1',
                (snapshot['snapshot_id'], snapshot['child_id'])).fetchone()
        result = {'feedback_current': None, 'feedback_current_revision': latest['revision'] if latest else 0,
                  'feedback_current_status': 'none'}
        if not latest:
            if c.execute('SELECT 1 FROM parent_report_snapshot_feedback WHERE snapshot_id=?', (snapshot['snapshot_id'],)).fetchone():
                result['feedback_current_status'] = 'legacy_unversioned'
            return result
        if latest['operation']=='retract':
            result['feedback_current_status'] = 'retracted'
            return result
        original = c.execute('SELECT * FROM parent_report_snapshot_feedback WHERE feedback_id=? AND snapshot_id=?',
            (latest['feedback_id'], snapshot['snapshot_id'])).fetchone()
        payload = json.loads(latest['payload_json'])
        if (not original or payload.get('source_version') != snapshot['source_version']
                or any(original[key]!=payload[key] for key in ('helpfulness','stress_rating','notes'))
                or snapshot['status']!='approved' or not ParentReportService.snapshot_source_current(c, snapshot)):
            result['feedback_current_status'] = 'source_invalid'
            return result
        result['feedback_current_status'] = 'active'
        result['feedback_current'] = {**payload, 'revision': latest['revision'], 'feedback_id': latest['feedback_id'],
            'event_id': latest['event_id'], 'created_at': latest['created_at'],
            'evidence_kind': 'report_experience', 'execution_verified': False, 'learning_effect_verified': False}
        return result

    @staticmethod
    def coaching_experiences(c, child_id, limit=5):
        """Historical subjective experience only; never authorizes learning or plans."""
        from .parent_reports import ParentReportService

        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'parent_report_snapshot', 'parent_report_snapshot_feedback',
                'report_feedback_revision', 'audit_event'} <= tables:
            return []

        def instant(value):
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if parsed.tzinfo is None:
                raise ValueError('EXPERIENCE_TIME_NOT_AWARE')
            return parsed

        now = instant(utc_now())
        candidates = c.execute('''SELECT s.* FROM parent_report_snapshot s WHERE s.child_id=?
            AND EXISTS(SELECT 1 FROM report_feedback_revision r WHERE r.snapshot_id=s.snapshot_id)
            ORDER BY s.snapshot_id''', (child_id,)).fetchall()
        result = []
        for snapshot in candidates:
            try:
                if ParentReportService.snapshot_problem(snapshot) or not snapshot['approved_by']:
                    continue
                approved = instant(snapshot['approved_at'])
                if not instant(snapshot['created_at']) <= approved <= now:
                    continue
                facts, inferences = json.loads(snapshot['facts_json']), json.loads(snapshot['inferences_json'])
                if snapshot['evidence_version'] != 'sha256:' + _hash({'facts': facts, 'inferences': inferences}):
                    continue
                if (not snapshot['source_version'] or '@' not in snapshot['source_id']
                        or snapshot['source_version'].rsplit(':', 1)[-1] != snapshot['source_id'].rpartition('@')[2]):
                    continue
                # Approval is a historical witness, not the current qualification
                # of the learning report whose wording this experience can change.
                approval = c.execute('''SELECT audit_id FROM audit_event WHERE child_id=?
                    AND action='parent_report_snapshot_approved' AND actor=? AND occurred_at=?
                    AND json_valid(summary_json) AND json_extract(summary_json,'$.snapshot_id')=?
                    ORDER BY audit_id LIMIT 1''',
                    (child_id, snapshot['approved_by'], snapshot['approved_at'], snapshot['snapshot_id'])).fetchone()
                if not approval:
                    continue
                history = c.execute('SELECT * FROM report_feedback_revision WHERE snapshot_id=? ORDER BY revision',
                                    (snapshot['snapshot_id'],)).fetchall()
                if not history or any(r['child_id'] != child_id or r['revision'] != i
                                      for i, r in enumerate(history, 1)):
                    continue
                latest = history[-1]
                if latest['operation'] != 'record':
                    continue
                original = c.execute('''SELECT * FROM parent_report_snapshot_feedback
                    WHERE feedback_id=? AND snapshot_id=?''', (latest['feedback_id'], snapshot['snapshot_id'])).fetchone()
                payload = json.loads(latest['payload_json'])
                if (not original or not isinstance(payload, dict)
                        or set(payload) != {'source_version', 'helpfulness', 'stress_rating', 'notes'}
                        or payload['source_version'] != snapshot['source_version']
                        or not isinstance(payload['source_version'], str) or not payload['source_version']
                        or any(original[key] != payload[key] for key in ('helpfulness', 'stress_rating', 'notes'))
                        or original['created_at'] != latest['created_at']
                        or not approved <= instant(latest['created_at']) <= now
                        or any(payload[key] is not None and (type(payload[key]) is not int or not 1 <= payload[key] <= 5)
                               for key in ('helpfulness', 'stress_rating'))
                        or (payload['notes'] is not None and not isinstance(payload['notes'], str))):
                    continue
                result.append({**payload, 'snapshot_id': snapshot['snapshot_id'], 'feedback_id': original['feedback_id'],
                    'event_id': latest['event_id'], 'revision': latest['revision'], 'created_at': latest['created_at'],
                    'approval_ref': approval['audit_id'], 'evidence_kind': 'subjective_report_experience',
                    'execution_verified': False, 'learning_effect_verified': False})
            except (ValueError, TypeError, AttributeError, KeyError):
                # Malformed historical proof is excluded; SQLite failures propagate.
                continue
        result.sort(key=lambda item: (instant(item['created_at']), item['event_id']), reverse=True)
        return result[:limit]

    def retract_feedback(self, child_id, snapshot_id, expected_revision, client_action_id, reason):
        if (not isinstance(client_action_id, str) or not client_action_id.strip() or len(client_action_id)>200
                or not isinstance(reason, str) or not reason.strip() or len(reason)>1000
                or type(expected_revision) is not int or expected_revision<1):
            raise ValueError('FEEDBACK_RETRACTION_INPUT_INVALID')
        payload = _json({'reason': reason, 'expected_revision': expected_revision})
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            self._owned(c, child_id, snapshot_id)
            prior = c.execute('SELECT * FROM report_feedback_revision WHERE child_id=? AND client_action_id=?', (child_id, client_action_id)).fetchone()
            if prior:
                if prior['snapshot_id'] != snapshot_id or prior['operation'] != 'retract' or prior['payload_json'] != payload:
                    raise ReportFollowupConflict('FEEDBACK_KEY_CONFLICT')
                return dict(prior)
            latest = c.execute('SELECT * FROM report_feedback_revision WHERE snapshot_id=? ORDER BY revision DESC LIMIT 1', (snapshot_id,)).fetchone()
            if not latest or latest['revision'] != expected_revision or latest['operation'] != 'record':
                raise ReportFollowupConflict('FEEDBACK_REVISION_CONFLICT')
            event_id = str(uuid4())
            c.execute('INSERT INTO report_feedback_revision VALUES (?,?,?,?,?,?,?,?,?)',
                (event_id, child_id, snapshot_id, expected_revision+1, 'retract', None, payload, client_action_id, utc_now()))
            c.commit()
            return dict(c.execute('SELECT * FROM report_feedback_revision WHERE event_id=?', (event_id,)).fetchone())

    def project_for_plan(self, child_id, planning_date, *, connection=None):
        if not isinstance(planning_date, str) or date.fromisoformat(planning_date).isoformat() != planning_date:
            raise ValueError('PLANNING_DATE_INVALID')
        with nullcontext(connection) if connection is not None else self.database.connect() as c:
            if not c.in_transaction:
                c.execute('BEGIN')
            if not c.execute('SELECT 1 FROM child_profile WHERE child_id=?', (child_id,)).fetchone():
                raise KeyError(child_id)
            items = []
            if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='report_feedback_revision'").fetchone():
                rows = c.execute("""SELECT e.*,s.source_version,s.status,f.helpfulness,f.stress_rating,f.notes
                    FROM report_feedback_revision e JOIN parent_report_snapshot s USING(snapshot_id)
                    JOIN parent_report_snapshot_feedback f ON f.feedback_id=e.feedback_id
                    WHERE e.child_id=? AND s.child_id=e.child_id AND e.operation='record' AND s.status='approved'
                      AND e.revision=(SELECT MAX(revision) FROM report_feedback_revision WHERE snapshot_id=e.snapshot_id)
                    ORDER BY e.event_id""", (child_id,)).fetchall()
                for row in rows:
                    from .parent_reports import ParentReportService
                    snapshot = c.execute('SELECT * FROM parent_report_snapshot WHERE snapshot_id=? AND child_id=?', (row['snapshot_id'], child_id)).fetchone()
                    if not ParentReportService.snapshot_source_current(c, snapshot):
                        continue
                    payload = json.loads(row['payload_json'])
                    if datetime.fromisoformat(row['created_at']).astimezone().date().isoformat() >= planning_date or payload != {key: row[key] for key in ('source_version','helpfulness','stress_rating','notes')}:
                        continue
                    items.append({'event_id': row['event_id'], 'snapshot_id': row['snapshot_id'],
                        'revision': row['revision'], 'created_at': row['created_at'], **payload,
                        'evidence_kind': 'report_experience', 'execution_verified': False,
                        'learning_effect_verified': False, 'allowed_use': 'parent_communication_review'})
            from .parent_action_evidence import ParentActionEvidenceService
            # Action execution feedback stays separate from report quality feedback.
            actions = ParentActionEvidenceService(self.database)
            for feedback in actions.feedback_for_child(c, child_id):
                action = c.execute('SELECT payload_json FROM parent_action_snapshot WHERE snapshot_id=? AND child_id=?',
                    (feedback['action_snapshot_id'], child_id)).fetchone()
                if not action:
                    continue
                frozen = json.loads(action[0])
                if frozen.get('review_binding'):
                    source_current = actions._review_current(c, frozen)
                else:
                    report = c.execute('SELECT * FROM daily_report WHERE report_id=? AND child_id=?',
                        (feedback['report_id'], child_id)).fetchone()
                    source_current = bool(report and self._action_source_current(c, report, frozen))
                if source_current and datetime.fromisoformat(feedback['created_at']).astimezone().date().isoformat() < planning_date:
                    items.append({**feedback, 'evidence_kind': 'parent_reported_action',
                        'execution_verified': False, 'learning_effect_verified': False,
                        'allowed_use': 'parent_action_candidate_review'})
            candidates, constraints = self._project_adjustments(c, child_id, planning_date)
            payload = {'child_id': child_id, 'planning_date': planning_date, 'items': items,
                'candidates': candidates, 'constraints': constraints,
                'approved_constraints_requiring_review': [item for item in constraints if item['requires_review']],
                'plan_adjustment_verified': False, 'local_only': True, 'cloud_transmission_allowed': False,
                'limitations': ['反馈是家长报告，不是独立成效证据。', '读取回执不证明计划采纳或孩子改善。']}
            return {**payload, 'fingerprint': _hash(payload)}

    @staticmethod
    def _action_source_current(c, report, frozen):
        from .parent_reports import ParentReportService
        if frozen['source_version'] != report['source'] or frozen['coaching'] != json.loads(report['parent_coaching_json']):
            return False
        return ParentReportService.source_qualification_fingerprint(c, report['child_id'], 'daily_report', report['report_id']) is not None

    @staticmethod
    def _plan(c, child_id, plan_id):
        row = c.execute('SELECT * FROM daily_plan WHERE plan_id=? AND child_id=?', (plan_id, child_id)).fetchone()
        if not row:
            raise ReportFollowupConflict('PLAN_CHILD_MISMATCH')
        items = [{**dict(item), 'included_today': item['status'] in {'pending','scheduled','active','paused','completed'}} for item in c.execute(
            'SELECT plan_item_id,subject,title,expected_minutes,priority,position,source_type,source_ref,status FROM plan_item WHERE plan_id=? ORDER BY position,plan_item_id', (plan_id,))]
        for item in items:
            item.pop('status')
        # Lifecycle/timer changes do not erase the historical planning receipt.
        return dict(row), _hash({**{key: row[key] for key in ('plan_id','child_id','plan_date','version','total_minutes','generator')}, 'items': items})

    def record_plan_read(self, child_id, plan_id, expected_fingerprint, client_action_id, *, connection=None):
        if not isinstance(client_action_id, str) or not client_action_id.strip() or len(client_action_id)>200:
            raise ValueError('PLAN_READ_KEY_REQUIRED')
        with nullcontext(connection) if connection is not None else self.database.connect() as c:
            if connection is None:
                c.execute('BEGIN IMMEDIATE')
            elif not c.in_transaction:
                raise ValueError('PLAN_READ_TRANSACTION_REQUIRED')
            plan, plan_hash = self._plan(c, child_id, plan_id)
            prior = c.execute('SELECT * FROM report_plan_feedback_receipt WHERE child_id=? AND client_action_id=?', (child_id, client_action_id)).fetchone()
            if prior:
                if prior['plan_id'] != plan_id or prior['input_fingerprint'] != expected_fingerprint:
                    raise ReportFollowupConflict('PLAN_READ_KEY_CONFLICT')
                return self._receipt(c, prior)
            inputs = self.project_for_plan(child_id, plan['plan_date'], connection=c)
            if inputs['fingerprint'] != expected_fingerprint:
                raise ReportFollowupConflict('PLAN_FEEDBACK_CHANGED')
            if not self._constraints_satisfied(c, plan, inputs['constraints']):
                raise ReportFollowupConflict('PLAN_CONSTRAINT_VIOLATION')
            if any(item['created_at'] > plan['created_at'] for item in inputs['items']):
                raise ReportFollowupConflict('PLAN_PREDATES_FEEDBACK')
            receipt_id = str(uuid4())
            c.execute('INSERT INTO report_plan_feedback_receipt VALUES (?,?,?,?,?,?,?,?,?)',
                (receipt_id, child_id, plan_id, plan['plan_date'], expected_fingerprint, _json(inputs), plan_hash, client_action_id, utc_now()))
            if connection is None:
                c.commit()
            return self._receipt(c, c.execute('SELECT * FROM report_plan_feedback_receipt WHERE receipt_id=?', (receipt_id,)).fetchone())

    def _receipt(self, c, row):
        inputs = self.project_for_plan(row['child_id'], row['planning_date'], connection=c)
        plan, plan_hash = self._plan(c, row['child_id'], row['plan_id'])
        return {**dict(row), 'inputs': json.loads(row['inputs_json']), 'status': 'read_only',
            'inputs_current': inputs['fingerprint'] == row['input_fingerprint'],
            'plan_current': plan_hash == row['plan_fingerprint'], 'plan_adjustment_verified': False,
            'constraints_satisfied': self._constraints_satisfied(c, plan, inputs['constraints']),
            'learning_effect_verified': False, 'local_only': True, 'cloud_transmission_allowed': False}

    def get_plan_receipt(self, child_id, receipt_id):
        with self.database.connect() as c:
            row = c.execute('SELECT * FROM report_plan_feedback_receipt WHERE receipt_id=? AND child_id=?', (receipt_id, child_id)).fetchone()
            if not row:
                raise KeyError(receipt_id)
            return self._receipt(c, row)

    @staticmethod
    def _constraints_satisfied(c, plan, constraints):
        items = c.execute("""SELECT expected_minutes,source_type FROM plan_item WHERE plan_id=?
            AND status IN ('pending','scheduled','active','paused','completed')""", (plan['plan_id'],)).fetchall()
        values = {'daily_total_minutes_cap': max(plan['total_minutes'], sum(row['expected_minutes'] for row in items)),
                  'task_segment_minutes_cap': max((row['expected_minutes'] for row in items if row['source_type']!='planned_break'), default=0)}
        return all(values[item['constraint_type']] <= item['value_minutes'] for item in constraints)

    @staticmethod
    def _limit(c, child_id, kind):
        fields = {'daily_total_minutes_cap': 'max_learning_minutes', 'task_segment_minutes_cap': 'task_segment_minutes'}
        if kind not in fields:
            raise ValueError('PLAN_CONSTRAINT_TYPE_INVALID')
        rule = c.execute('SELECT max_learning_minutes,task_segment_minutes FROM family_rule WHERE child_id=?', (child_id,)).fetchone()
        if not rule:
            raise ValueError('FAMILY_RULE_REQUIRED')
        return rule[fields[kind]]

    @staticmethod
    def _action_key(key):
        if not isinstance(key, str) or not key.strip() or len(key)>200:
            raise ValueError('PLAN_ADJUSTMENT_KEY_REQUIRED')

    def propose_plan_adjustment(self, child_id, snapshot_id, expected_feedback_revision, constraint_type,
                                value_minutes, start_date, end_date, client_action_id):
        """Explicit parent proposal; never call from inferred model sentiment."""
        self._action_key(client_action_id)
        if type(value_minutes) is not int or value_minutes<1 or type(expected_feedback_revision) is not int:
            raise ValueError('PLAN_ADJUSTMENT_VALUE_INVALID')
        start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
        if start.isoformat()!=start_date or end.isoformat()!=end_date or not 0 <= (end-start).days <= 366:
            raise ValueError('PLAN_ADJUSTMENT_DATES_INVALID')
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            self._owned(c, child_id, snapshot_id)
            proposal = {'expected_feedback_revision': expected_feedback_revision, 'constraint_type': constraint_type,
                'value_minutes': value_minutes, 'start_date': start_date, 'end_date': end_date}
            prior = c.execute('SELECT * FROM report_plan_adjustment_event WHERE child_id=? AND client_action_id=?', (child_id, client_action_id)).fetchone()
            if prior:
                previous = json.loads(prior['payload_json'])
                if prior['snapshot_id'] != snapshot_id or prior['operation'] != 'propose' or any(previous.get(k)!=v for k,v in proposal.items()):
                    raise ReportFollowupConflict('PLAN_ADJUSTMENT_KEY_CONFLICT')
                return self._adjustment(c, child_id, prior['adjustment_id'])
            if value_minutes > self._limit(c, child_id, constraint_type):
                raise ValueError('PLAN_ADJUSTMENT_CANNOT_LOOSEN_FAMILY_RULE')
            feedback = c.execute('SELECT * FROM report_feedback_revision WHERE snapshot_id=? ORDER BY revision DESC LIMIT 1', (snapshot_id,)).fetchone()
            if not feedback or feedback['operation'] != 'record' or feedback['revision'] != expected_feedback_revision:
                raise ReportFollowupConflict('FEEDBACK_REVISION_CONFLICT')
            proposal.update(source_event_id=feedback['event_id'])
            synthetic = {'child_id': child_id, 'snapshot_id': snapshot_id, 'payload_json': _json(proposal)}
            if not self._adjustment_source_current(c, synthetic):
                raise ReportFollowupConflict('PLAN_ADJUSTMENT_SOURCE_CHANGED')
            adjustment_id = str(uuid4())
            c.execute('INSERT INTO report_plan_adjustment_event VALUES (?,?,?,?,?,?,?,?,?)',
                (str(uuid4()), adjustment_id, child_id, snapshot_id, 1, 'propose', _json(proposal), client_action_id, utc_now()))
            c.commit()
            return self._adjustment(c, child_id, adjustment_id)

    def review_plan_adjustment(self, child_id, adjustment_id, expected_revision, decision, client_action_id):
        self._action_key(client_action_id)
        if decision not in {'approve','reject','retract'} or type(expected_revision) is not int or expected_revision<1:
            raise ValueError('PLAN_ADJUSTMENT_REVIEW_INVALID')
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            current = c.execute('SELECT * FROM report_plan_adjustment_event WHERE child_id=? AND adjustment_id=? ORDER BY revision DESC LIMIT 1', (child_id, adjustment_id)).fetchone()
            if not current:
                raise KeyError(adjustment_id)
            if json.loads(current['payload_json']).get('rebinding_version'):
                raise ReportFollowupConflict('REBINDING_EXPLICIT_REVIEW_REQUIRED')
            prior = c.execute('SELECT * FROM report_plan_adjustment_event WHERE child_id=? AND client_action_id=?', (child_id, client_action_id)).fetchone()
            if prior:
                if prior['adjustment_id']!=adjustment_id or prior['revision']!=expected_revision+1 or prior['operation']!=decision:
                    raise ReportFollowupConflict('PLAN_ADJUSTMENT_KEY_CONFLICT')
                return self._adjustment(c, child_id, adjustment_id)
            if current['revision']!=expected_revision or (current['operation'], decision) not in {('propose','approve'),('propose','reject'),('approve','retract')}:
                raise ReportFollowupConflict('PLAN_ADJUSTMENT_REVISION_CONFLICT')
            if decision=='approve':
                payload = json.loads(current['payload_json'])
                if not self._adjustment_source_current(c, current):
                    raise ReportFollowupConflict('PLAN_ADJUSTMENT_SOURCE_CHANGED')
                if payload['value_minutes'] > self._limit(c, child_id, payload['constraint_type']):
                    raise ReportFollowupConflict('FAMILY_RULE_CHANGED')
            c.execute('INSERT INTO report_plan_adjustment_event VALUES (?,?,?,?,?,?,?,?,?)',
                (str(uuid4()), adjustment_id, child_id, current['snapshot_id'], expected_revision+1,
                 decision, current['payload_json'], client_action_id, utc_now()))
            c.commit()
            return self._adjustment(c, child_id, adjustment_id)

    @staticmethod
    def _adjustment_source_current(c, row):
        from .parent_reports import ParentReportService
        payload = json.loads(row['payload_json'])
        if payload.get('rebinding_version'):
            from .report_adjustment_rebinding import ReportRebindingService
            return ReportRebindingService.source_current(c, row)
        latest = c.execute('SELECT * FROM report_feedback_revision WHERE snapshot_id=? AND child_id=? ORDER BY revision DESC LIMIT 1', (row['snapshot_id'],row['child_id'])).fetchone()
        snapshot = c.execute('SELECT * FROM parent_report_snapshot WHERE snapshot_id=? AND child_id=?', (row['snapshot_id'],row['child_id'])).fetchone()
        return bool(latest and latest['event_id']==payload['source_event_id'] and latest['operation']=='record'
            and snapshot and ReportFollowupService.current_feedback(c, snapshot)['feedback_current_status']=='active')

    def _adjustment(self, c, child_id, adjustment_id):
        rows = c.execute('SELECT * FROM report_plan_adjustment_event WHERE child_id=? AND adjustment_id=? ORDER BY revision', (child_id,adjustment_id)).fetchall()
        if not rows:
            raise KeyError(adjustment_id)
        current = rows[-1]
        return {**dict(current), **json.loads(current['payload_json']), 'history': [dict(row) for row in rows],
            **self._adjustment_authority(c, current, family_today().isoformat()), 'learning_effect_verified': False,
            'local_only': True, 'cloud_transmission_allowed': False}

    def _adjustment_authority(self, c, row, planning_date):
        payload = json.loads(row['payload_json'])
        source_current = self._adjustment_source_current(c, row)
        if payload.get('rebinding_version'):
            approved = row['operation'] == 'approve'
            in_window = payload['start_date'] <= planning_date <= payload['end_date']
            release = ('adjustment_retracted' if row['operation'] == 'retract' else
                       'rejected' if row['operation'] == 'reject' else
                       'expired' if planning_date > payload['end_date'] else
                       'binding_source_changed' if not source_current else None)
            return {'source_current': source_current, 'feedback_source_current': source_current,
                'authority': 'explicit_parent_rebinding' if approved and source_current else 'unapproved_parent_proposal' if row['operation']=='propose' else 'released',
                'requires_review': bool(not source_current and row['operation'] in {'propose','approve'}),
                'effective_as_guard': bool(approved and source_current and in_window), 'release_reason': release}
        # Source qualification can revoke learning claims, not a parent's limit.
        # Any explicit withdrawal after that authorization permanently releases it.
        withdrawn = c.execute('''SELECT 1 FROM report_feedback_revision
            WHERE child_id=? AND snapshot_id=? AND operation='retract' AND revision>? LIMIT 1''',
            (row['child_id'], row['snapshot_id'], payload['expected_feedback_revision'])).fetchone()
        approved = row['operation'] == 'approve' and not withdrawn
        in_window = payload['start_date'] <= planning_date <= payload['end_date']
        release = ('feedback_retracted' if withdrawn else 'adjustment_retracted' if row['operation']=='retract'
            else 'rejected' if row['operation']=='reject' else 'expired' if planning_date > payload['end_date'] else None)
        retained = approved and not source_current and release is None
        return {'source_current': source_current, 'feedback_source_current': source_current,
            'authority': ('retained_parent_safety_guard' if retained else 'explicit_parent_review' if approved
                else 'unapproved_parent_proposal' if row['operation']=='propose' else 'released'),
            'requires_review': bool(retained), 'effective_as_guard': bool(approved and in_window),
            'release_reason': release}

    def get_adjustment(self, child_id, adjustment_id):
        with self.database.connect() as c:
            c.execute('BEGIN')
            return self._adjustment(c, child_id, adjustment_id)

    def _project_adjustments(self, c, child_id, planning_date):
        candidates, constraints = [], []
        if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='report_plan_adjustment_event'").fetchone():
            return candidates, constraints
        for row in c.execute('''SELECT * FROM report_plan_adjustment_event e WHERE child_id=?
            AND revision=(SELECT MAX(revision) FROM report_plan_adjustment_event WHERE adjustment_id=e.adjustment_id)
            ORDER BY adjustment_id''', (child_id,)).fetchall():
            payload = json.loads(row['payload_json'])
            authority = self._adjustment_authority(c, row, planning_date)
            if not payload['start_date'] <= planning_date <= payload['end_date']:
                continue
            item = {'adjustment_id': row['adjustment_id'], 'revision': row['revision'], **payload,
                'learning_effect_verified': False, **authority}
            if row['operation']=='propose' and authority['source_current']:
                candidates.append(item)
            elif authority['effective_as_guard']:
                item['value_minutes'] = min(payload['value_minutes'], self._limit(c, child_id, payload['constraint_type']))
                constraints.append(item)
        return candidates, constraints

    def constraint_windows(self, child_id, start_date, end_date, *, connection):
        """Qualify each date regime once within the caller's read snapshot."""
        from datetime import timedelta
        start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
        if end < start:
            return []
        if not connection.in_transaction:
            raise ValueError('CONSTRAINT_CALENDAR_TRANSACTION_REQUIRED')
        boundaries = {start, end + timedelta(days=1)}
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='report_plan_adjustment_event'").fetchone():
            for row in connection.execute('SELECT payload_json FROM report_plan_adjustment_event WHERE child_id=?', (child_id,)):
                payload = json.loads(row[0])
                for value in (date.fromisoformat(payload['start_date']), date.fromisoformat(payload['end_date'])+timedelta(days=1)):
                    if start < value <= end:
                        boundaries.add(value)
        ordered = sorted(boundaries)
        windows = []
        for left, right in zip(ordered, ordered[1:]):
            _, constraints = self._project_adjustments(connection, child_id, left.isoformat())
            payload = {'start_date': left.isoformat(), 'end_date': (right-timedelta(days=1)).isoformat(), 'constraints': constraints}
            windows.append({**payload, 'source_version': 'report_constraint_window_v1:'+_hash(payload)})
        return windows
