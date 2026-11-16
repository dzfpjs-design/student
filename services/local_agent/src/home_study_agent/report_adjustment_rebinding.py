"""Explicit parent consent linking historical experience to a reviewed successor.

Binding/command records are not coaching inputs or learning evidence. Creating or
confirming one therefore does not change the source it just asked the parent to read.
"""
from __future__ import annotations

from datetime import date
import json
from uuid import uuid4

from .parent_reports import ParentReportService
from .report_followup import ReportFollowupConflict, ReportFollowupService, _hash, _json
from .repository import utc_now


VERSION = 'period_parent_rebinding_v1'


class ReportRebindingService:
    def __init__(self, database):
        self.database = database
        self.followup = ReportFollowupService(database)

    def initialize(self):
        self.followup.initialize()
        with self.database.connect() as c:
            self.ensure_schema(c)

    @staticmethod
    def ensure_schema(c):
        # Independent feature schema; central migration number is assigned at integration.
        c.executescript('''
        CREATE TABLE IF NOT EXISTS report_adjustment_binding (
          adjustment_id TEXT PRIMARY KEY,
          proposal_event_id TEXT NOT NULL UNIQUE REFERENCES report_plan_adjustment_event(event_id) ON DELETE CASCADE,
          child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
          historical_snapshot_id TEXT NOT NULL REFERENCES parent_report_snapshot(snapshot_id) ON DELETE CASCADE,
          target_snapshot_id TEXT NOT NULL REFERENCES parent_report_snapshot(snapshot_id) ON DELETE CASCADE,
          binding_hash TEXT NOT NULL,
          payload_json TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS report_rebinding_command (
          child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
          client_action_id TEXT NOT NULL,
          adjustment_id TEXT NOT NULL REFERENCES report_adjustment_binding(adjustment_id) ON DELETE CASCADE,
          request_hash TEXT NOT NULL,
          receipt_json TEXT NOT NULL,
          created_at TEXT NOT NULL,
          PRIMARY KEY(child_id,client_action_id)
        );
        CREATE TRIGGER IF NOT EXISTS report_adjustment_binding_immutable
          BEFORE UPDATE ON report_adjustment_binding BEGIN SELECT RAISE(ABORT,'APPEND_ONLY'); END;
        CREATE TRIGGER IF NOT EXISTS report_rebinding_command_immutable
          BEFORE UPDATE ON report_rebinding_command BEGIN SELECT RAISE(ABORT,'APPEND_ONLY'); END;
        CREATE TABLE IF NOT EXISTS report_rebinding_cancelled_command (
          child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
          client_action_id TEXT NOT NULL, created_at TEXT NOT NULL,
          PRIMARY KEY(child_id,client_action_id)
        );
        CREATE TRIGGER IF NOT EXISTS report_rebinding_cancelled_immutable
          BEFORE UPDATE ON report_rebinding_cancelled_command BEGIN SELECT RAISE(ABORT,'APPEND_ONLY'); END;
        ''')

    @staticmethod
    def _snapshot(c, child_id, snapshot_id):
        row = c.execute('SELECT * FROM parent_report_snapshot WHERE child_id=? AND snapshot_id=?',
                        (child_id, snapshot_id)).fetchone()
        if not row:
            raise KeyError(snapshot_id)
        return row

    @staticmethod
    def _explicitly_revoked(c, child_id, snapshot_id):
        return bool(c.execute('''SELECT 1 FROM audit_event WHERE child_id=? AND actor='parent'
            AND action='parent_report_snapshot_revoked' AND json_valid(summary_json)
            AND json_extract(summary_json,'$.snapshot_id')=? LIMIT 1''', (child_id, snapshot_id)).fetchone())

    @staticmethod
    def _approval(c, row):
        witness = c.execute('''SELECT audit_id,actor,occurred_at FROM audit_event
            WHERE child_id=? AND action='parent_report_snapshot_approved' AND actor=? AND occurred_at=?
            AND json_valid(summary_json) AND json_extract(summary_json,'$.snapshot_id')=?
            ORDER BY audit_id LIMIT 1''',
            (row['child_id'], row['approved_by'], row['approved_at'], row['snapshot_id'])).fetchone()
        if not witness:
            raise ReportFollowupConflict('REBINDING_APPROVAL_WITNESS_MISSING')
        return dict(witness)

    @staticmethod
    def _stamp(c, row):
        fields = ('snapshot_id', 'child_id', 'source_kind', 'source_id', 'source_version',
                  'evidence_version', 'template_version', 'report_type', 'approved_at', 'approved_by')
        return {**{key: row[key] for key in fields}, 'approval': ReportRebindingService._approval(c, row),
                'frozen_hash': _hash({key: json.loads(row[key]) for key in
                    ('facts_json', 'inferences_json', 'content_json', 'confidence_json', 'parent_edit_json')})}

    @classmethod
    def _context(cls, c, child_id, target_id, experience):
        target = cls._snapshot(c, child_id, target_id)
        old = cls._snapshot(c, child_id, experience['snapshot_id'])
        if (target_id == old['snapshot_id'] or target['source_kind'] != 'period_report'
                or old['source_kind'] != 'period_report'
                or old['source_id'].rpartition('@')[0] != target['source_id'].rpartition('@')[0]
                or target['status'] != 'approved'
                or cls._explicitly_revoked(c, child_id, old['snapshot_id'])
                or cls._explicitly_revoked(c, child_id, target_id)
                or not ParentReportService.snapshot_source_current(c, target)):
            raise ReportFollowupConflict('REBINDING_SOURCE_CHANGED')
        # Current qualification proves B was built from these exact experiences;
        # an unrefreshed B cannot acquire a fresh proof just by reading this route.
        old_stamp, target_stamp = cls._stamp(c, old), cls._stamp(c, target)
        if old_stamp['approval']['audit_id'] != experience['approval_ref']:
            raise ReportFollowupConflict('REBINDING_HISTORICAL_WITNESS_CHANGED')
        binding = {'version': VERSION, 'child_id': child_id,
            'feedback_event_id': experience['event_id'], 'feedback_revision': experience['revision'],
            'experience_hash': _hash(experience), 'historical': old_stamp, 'target': target_stamp}
        return {'binding': binding, 'feedback': experience,
            'historical_report': {'snapshot_id': old['snapshot_id'], 'content': json.loads(old['content_json']),
                                  'parent_edit': json.loads(old['parent_edit_json'])},
            'report': {'snapshot_id': target_id, 'content': json.loads(target['content_json']),
                       'parent_edit': json.loads(target['parent_edit_json'])}}

    def contexts(self, child_id, target_id):
        with self.database.connect() as c:
            c.execute('BEGIN')
            self._snapshot(c, child_id, target_id)
            items = []
            for experience in ReportFollowupService.coaching_experiences(c, child_id):
                try:
                    items.append(self._context(c, child_id, target_id, experience))
                except ReportFollowupConflict:
                    continue
            return {'items': items, 'local_only': True, 'cloud_transmission_allowed': False}

    @classmethod
    def _validate_binding(cls, c, child_id, binding):
        try:
            if binding['version'] != VERSION or binding['child_id'] != child_id:
                raise ReportFollowupConflict('REBINDING_PROOF_CHANGED')
            experience = next((item for item in ReportFollowupService.coaching_experiences(c, child_id)
                               if item['event_id'] == binding['feedback_event_id']), None)
            if not experience:
                raise ReportFollowupConflict('REBINDING_EXPERIENCE_NOT_INCLUDED')
            context = cls._context(c, child_id, binding['target']['snapshot_id'], experience)
            if context['binding'] != binding:
                raise ReportFollowupConflict('REBINDING_PROOF_CHANGED')
            return context
        except (KeyError, TypeError) as error:
            raise ReportFollowupConflict('REBINDING_PROOF_CHANGED') from error

    @classmethod
    def source_current(cls, c, row):
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='report_adjustment_binding' AND type='table'").fetchone():
            return False
        frozen = c.execute('SELECT * FROM report_adjustment_binding WHERE adjustment_id=? AND child_id=?',
                           (row['adjustment_id'], row['child_id'])).fetchone()
        if not frozen:
            return False
        try:
            stored, payload = json.loads(frozen['payload_json']), json.loads(row['payload_json'])
            if (frozen['binding_hash'] != _hash(stored) or payload != cls._payload(stored)
                    or row['snapshot_id'] != stored['binding']['target']['snapshot_id']):
                return False
            cls._validate_binding(c, row['child_id'], stored['binding'])
            return True
        except (KeyError, TypeError, ValueError):
            return False

    @staticmethod
    def _payload(stored):
        binding = stored['binding']
        return {**stored['terms'], 'rebinding_version': VERSION, 'binding_hash': _hash(stored),
                'source_event_id': binding['feedback_event_id'],
                'expected_feedback_revision': binding['feedback_revision']}

    def _read(self, c, child_id, adjustment_id):
        result = self.followup._adjustment(c, child_id, adjustment_id)
        frozen = c.execute('SELECT * FROM report_adjustment_binding WHERE adjustment_id=? AND child_id=?',
                           (adjustment_id, child_id)).fetchone()
        if not frozen:
            raise KeyError(adjustment_id)
        stored = json.loads(frozen['payload_json'])
        return {**result, **stored, 'review_hash': _hash({'adjustment_id': adjustment_id,
            'revision': result['revision'], 'binding_hash': frozen['binding_hash']})}

    def list(self, child_id, target_id):
        with self.database.connect() as c:
            c.execute('BEGIN')
            self._snapshot(c, child_id, target_id)
            ids = [row[0] for row in c.execute('SELECT adjustment_id FROM report_adjustment_binding WHERE child_id=? AND target_snapshot_id=? ORDER BY created_at,adjustment_id', (child_id, target_id))]
            return {'items': [self._read(c, child_id, key) for key in ids]}

    def command(self, child_id, client_action_id):
        with self.database.connect() as c:
            c.execute('BEGIN')
            prior = c.execute('SELECT * FROM report_rebinding_command WHERE child_id=? AND client_action_id=?',
                              (child_id, client_action_id)).fetchone()
            if not prior:
                if c.execute('SELECT 1 FROM report_rebinding_cancelled_command WHERE child_id=? AND client_action_id=?', (child_id, client_action_id)).fetchone():
                    return {'status': 'cancelled_not_committed'}
                raise KeyError(client_action_id)
            return {'receipt': json.loads(prior['receipt_json']),
                    'current': self._read(c, child_id, prior['adjustment_id'])}

    def cancel_command(self, child_id, client_action_id, target_snapshot_id):
        ReportFollowupService._action_key(client_action_id)
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            self._snapshot(c, child_id, target_snapshot_id)
            row = c.execute('SELECT * FROM report_rebinding_command WHERE child_id=? AND client_action_id=?', (child_id, client_action_id)).fetchone()
            if row:
                current = self._read(c, child_id, row['adjustment_id'])
                if current['snapshot_id'] != target_snapshot_id:
                    raise ReportFollowupConflict('REBINDING_TARGET_CHANGED')
                return {'receipt': json.loads(row['receipt_json']), 'current': current}
            c.execute('INSERT OR IGNORE INTO report_rebinding_cancelled_command VALUES (?,?,?)', (child_id, client_action_id, utc_now()))
            c.commit()
            return {'status': 'cancelled_not_committed'}

    @staticmethod
    def _prior(c, child_id, key, request):
        ReportFollowupService._action_key(key)
        if c.execute('SELECT 1 FROM report_rebinding_cancelled_command WHERE child_id=? AND client_action_id=?', (child_id, key)).fetchone():
            raise ReportFollowupConflict('REBINDING_COMMAND_CANCELLED')
        row = c.execute('SELECT * FROM report_rebinding_command WHERE child_id=? AND client_action_id=?', (child_id, key)).fetchone()
        if row:
            if row['request_hash'] != _hash(request):
                raise ReportFollowupConflict('REBINDING_KEY_CONFLICT')
            return json.loads(row['receipt_json'])
        if c.execute('SELECT 1 FROM report_plan_adjustment_event WHERE child_id=? AND client_action_id=?', (child_id, key)).fetchone():
            raise ReportFollowupConflict('REBINDING_KEY_CONFLICT')
        return None

    def _receipt(self, c, child_id, key, request, adjustment_id):
        result = self._read(c, child_id, adjustment_id)
        c.execute('INSERT INTO report_rebinding_command VALUES (?,?,?,?,?,?)',
                  (child_id, key, adjustment_id, _hash(request), _json(result), utc_now()))
        return result

    def propose(self, child_id, binding, terms, client_action_id):
        request = {'operation': 'propose', 'binding': binding, 'terms': terms}
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            previous = self._prior(c, child_id, client_action_id, request)
            if previous:
                return previous
            context = self._validate_binding(c, child_id, binding)
            if set(terms) != {'constraint_type', 'value_minutes', 'start_date', 'end_date'}:
                raise ValueError('REBINDING_TERMS_INVALID')
            start, end = date.fromisoformat(terms['start_date']), date.fromisoformat(terms['end_date'])
            value = terms['value_minutes']
            if (start.isoformat() != terms['start_date'] or end.isoformat() != terms['end_date']
                    or not 0 <= (end-start).days <= 366 or type(value) is not int or value < 1
                    or value > self.followup._limit(c, child_id, terms['constraint_type'])):
                raise ValueError('REBINDING_TERMS_INVALID')
            key, event, now = str(uuid4()), str(uuid4()), utc_now()
            stored = {'binding': binding, 'terms': terms, 'evidence': context}
            payload = self._payload(stored)
            c.execute('INSERT INTO report_plan_adjustment_event VALUES (?,?,?,?,?,?,?,?,?)',
                (event, key, child_id, binding['target']['snapshot_id'], 1, 'propose', _json(payload), client_action_id, now))
            c.execute('INSERT INTO report_adjustment_binding VALUES (?,?,?,?,?,?,?,?)',
                (key, event, child_id, binding['historical']['snapshot_id'], binding['target']['snapshot_id'], _hash(stored), _json(stored), now))
            result = self._receipt(c, child_id, client_action_id, request, key)
            c.commit()
            return result

    def review(self, child_id, adjustment_id, expected_revision, decision, review_hash, evidence_seen, client_action_id):
        request = {'operation': 'review', 'adjustment_id': adjustment_id, 'expected_revision': expected_revision,
                   'decision': decision, 'review_hash': review_hash, 'evidence_seen': evidence_seen}
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            previous = self._prior(c, child_id, client_action_id, request)
            if previous:
                return previous
            current = self._read(c, child_id, adjustment_id)
            if (type(expected_revision) is not int or current['revision'] != expected_revision
                    or current['review_hash'] != review_hash
                    or (current['operation'], decision) not in {('propose','approve'), ('propose','reject'), ('approve','retract')}):
                raise ReportFollowupConflict('REBINDING_REVIEW_CHANGED')
            if decision == 'approve':
                if evidence_seen is not True or not current['source_current']:
                    raise ReportFollowupConflict('REBINDING_REVIEW_SOURCE_CHANGED')
                if current['value_minutes'] > self.followup._limit(c, child_id, current['constraint_type']):
                    raise ReportFollowupConflict('FAMILY_RULE_CHANGED')
            c.execute('INSERT INTO report_plan_adjustment_event VALUES (?,?,?,?,?,?,?,?,?)',
                (str(uuid4()), adjustment_id, child_id, current['snapshot_id'], expected_revision+1,
                 decision, current['payload_json'], client_action_id, utc_now()))
            result = self._receipt(c, child_id, client_action_id, request, adjustment_id)
            c.commit()
            return result
