"""Frozen advice and append-only parent-reported action evidence, stored locally."""
from datetime import datetime, timezone
import hashlib
import json
from uuid import uuid4

from .database import Database

PARENT_ACTION_TABLES = ("parent_action_snapshot", "parent_action_feedback_event")


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _time(value):
    if not isinstance(value, str):
        raise ValueError("PARENT_ACTION_TIME_REQUIRED")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("PARENT_ACTION_TIME_INVALID") from error
    if parsed.tzinfo is None:
        raise ValueError("PARENT_ACTION_TIMEZONE_REQUIRED")
    return parsed.astimezone(timezone.utc)


class ParentActionEvidenceService:
    def __init__(self, database: Database):
        self.database = database

    def initialize(self):
        with self.database.connect() as c:
            self.ensure_schema(c)

    @classmethod
    def _create_tables(cls, c, suffix=""):
        parent = "parent_action_snapshot" + suffix
        feedback = "parent_action_feedback_event" + suffix
        c.execute(f"""CREATE TABLE IF NOT EXISTS {parent} (
          snapshot_id TEXT PRIMARY KEY,
          report_id TEXT REFERENCES daily_report(report_id) ON DELETE CASCADE,
          child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
          version INTEGER NOT NULL CHECK(version>0),
          fingerprint TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
          period_report_id TEXT REFERENCES period_report(period_report_id) ON DELETE CASCADE,
          CHECK((report_id IS NOT NULL) + (period_report_id IS NOT NULL) = 1),
          UNIQUE(report_id,version), UNIQUE(report_id,fingerprint),
          UNIQUE(period_report_id,version), UNIQUE(period_report_id,fingerprint))""")
        c.execute(f"""CREATE TABLE IF NOT EXISTS {feedback} (
          event_id TEXT PRIMARY KEY,
          snapshot_id TEXT NOT NULL REFERENCES {parent}(snapshot_id) ON DELETE CASCADE,
          child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
          revision INTEGER NOT NULL CHECK(revision>0),
          operation TEXT NOT NULL CHECK(operation IN ('record','retract')),
          payload_json TEXT NOT NULL, request_json TEXT NOT NULL,
          client_action_id TEXT NOT NULL, created_at TEXT NOT NULL,
          UNIQUE(snapshot_id,revision), UNIQUE(child_id,client_action_id))""")

    @classmethod
    def ensure_schema(cls, c):
        if c.in_transaction:
            raise ValueError("PARENT_ACTION_SCHEMA_REQUIRES_OUTSIDE_TRANSACTION")
        c.execute("BEGIN IMMEDIATE")
        try:
            columns = {row["name"] for row in c.execute("PRAGMA table_info(parent_action_snapshot)")}
            if columns and "period_report_id" not in columns:
                # Both ledgers migrate in one transaction with foreign keys on.
                # Copy the frozen bytes unchanged, including old idempotency requests.
                cls._create_tables(c, "_upgrade")
                c.execute("""INSERT INTO parent_action_snapshot_upgrade
                    (snapshot_id,report_id,child_id,version,fingerprint,payload_json,created_at)
                    SELECT snapshot_id,report_id,child_id,version,fingerprint,payload_json,created_at
                    FROM parent_action_snapshot""")
                c.execute("INSERT INTO parent_action_feedback_event_upgrade SELECT * FROM parent_action_feedback_event")
                c.execute("DROP TABLE parent_action_feedback_event")
                c.execute("DROP TABLE parent_action_snapshot")
                c.execute("ALTER TABLE parent_action_snapshot_upgrade RENAME TO parent_action_snapshot")
                c.execute("ALTER TABLE parent_action_feedback_event_upgrade RENAME TO parent_action_feedback_event")
            else:
                cls._create_tables(c)
            guards = c.execute("""SELECT name FROM sqlite_master WHERE type='trigger'
                AND tbl_name IN ('parent_action_snapshot','parent_action_feedback_event')""").fetchall()
            for row in guards:
                if row[0].lower().endswith("no_delete"):
                    c.execute('DROP TRIGGER "' + row[0].replace('"', '""') + '"')
            c.execute("CREATE INDEX IF NOT EXISTS idx_parent_action_child ON parent_action_snapshot(child_id,created_at)")
            c.execute("""CREATE TRIGGER IF NOT EXISTS parent_action_snapshot_no_update BEFORE UPDATE ON parent_action_snapshot
                BEGIN SELECT RAISE(ABORT,'PARENT_ACTION_SNAPSHOT_IMMUTABLE'); END""")
            c.execute("""CREATE TRIGGER IF NOT EXISTS parent_action_feedback_no_update BEFORE UPDATE ON parent_action_feedback_event
                BEGIN SELECT RAISE(ABORT,'PARENT_ACTION_FEEDBACK_APPEND_ONLY'); END""")
            if c.execute("PRAGMA foreign_key_check(parent_action_snapshot)").fetchone() or c.execute(
                    "PRAGMA foreign_key_check(parent_action_feedback_event)").fetchone():
                raise ValueError("PARENT_ACTION_SCHEMA_FOREIGN_KEY_INVALID")
            c.commit()
        except BaseException:
            c.rollback()
            raise

    @staticmethod
    def _available(c):
        return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='parent_action_snapshot'").fetchone() is not None

    @staticmethod
    def _payload(c, report_id):
        report = c.execute("SELECT * FROM daily_report WHERE report_id=?", (report_id,)).fetchone()
        if not report:
            raise ValueError("PARENT_ACTION_REPORT_NOT_FOUND")
        coaching = json.loads(report["parent_coaching_json"])
        action = coaching.get("action") if isinstance(coaching, dict) else None
        if not isinstance(action, dict) or not isinstance(action.get("what"), str) or not action["what"].strip():
            raise ValueError("PARENT_ACTION_CONTENT_REQUIRED")
        return {"contract_version": "parent_action_v1", "report_id": report_id,
                "child_id": report["child_id"], "source_version": report["source"],
                "target": {"actor": "parent", "child_id": report["child_id"]},
                "timing": {"report_date": report["report_date"], "when": action.get("when"),
                           "duration_minutes": action.get("duration_minutes"), "planned_at": None},
                "coaching": coaching}

    def freeze(self, c, report_id):
        """Only call during report generation, inside its write transaction."""
        if not c.in_transaction:
            raise ValueError("PARENT_ACTION_GENERATION_TRANSACTION_REQUIRED")
        payload = self._payload(c, report_id)
        return self._freeze_payload(c, report_id, payload)

    def _freeze_payload(self, c, report_id, payload):
        encoded = _json(payload)
        fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
        period_id = payload.get('period_report_id')
        source_column = 'period_report_id' if period_id else 'report_id'
        source_id = period_id or report_id
        existing = c.execute(f"SELECT snapshot_id FROM parent_action_snapshot WHERE {source_column}=? AND fingerprint=?", (source_id, fingerprint)).fetchone()
        if existing:
            return self._get(c, payload["child_id"], existing[0])
        version = c.execute(f"SELECT COALESCE(MAX(version),0)+1 FROM parent_action_snapshot WHERE {source_column}=?", (source_id,)).fetchone()[0]
        snapshot_id = str(uuid4())
        c.execute("""INSERT INTO parent_action_snapshot
            (snapshot_id,report_id,child_id,version,fingerprint,payload_json,created_at,period_report_id)
            VALUES (?,?,?,?,?,?,?,?)""",
                  (snapshot_id, report_id, payload["child_id"], version, fingerprint, encoded, datetime.now(timezone.utc).isoformat(), period_id))
        return self._get(c, payload["child_id"], snapshot_id)

    def _reviewed_payload(self, c, review):
        from .parent_reports import ParentReportService

        if review['status'] != 'approved' or not ParentReportService.snapshot_source_current(c, review):
            raise ValueError('PARENT_ACTION_REVIEW_NOT_CURRENT')
        try:
            if review['source_kind'] == 'daily_report':
                payload = self._payload(c, review['report_id'])
            elif review['source_kind'] == 'period_report':
                source_id = review['source_id'].rpartition('@')[0]
                period = c.execute('SELECT * FROM period_report WHERE period_report_id=? AND child_id=?',
                    (source_id, review['child_id'])).fetchone()
                if not period:
                    raise ValueError('PARENT_ACTION_REPORT_NOT_FOUND')
                content = json.loads(period['content_json'])
                coaching = content.get('parent_coaching') or {}
                if coaching != json.loads(review['content_json']).get('parent_coaching'):
                    raise ValueError('PARENT_ACTION_REVIEW_NOT_CURRENT')
                action = coaching.get('action')
                if not isinstance(action, dict) or not isinstance(action.get('what'), str) or not action['what'].strip():
                    raise ValueError('PARENT_ACTION_CONTENT_REQUIRED')
                payload = {'contract_version': 'parent_action_reviewed_v2', 'report_id': None,
                    'period_report_id': source_id, 'source_kind': 'period_report', 'source_id': source_id,
                    'child_id': period['child_id'], 'source_version': period['source'],
                    'target': {'actor': 'parent', 'child_id': period['child_id']},
                    'timing': {'report_date': period['period_end'], 'period_start': period['period_start'],
                        'period_end': period['period_end'], 'when': action.get('when'),
                        'duration_minutes': action.get('duration_minutes'), 'planned_at': None},
                    'coaching': coaching}
            else:
                raise ValueError('PARENT_ACTION_SOURCE_KIND_INVALID')
        except ValueError as error:
            if str(error) == 'PARENT_ACTION_CONTENT_REQUIRED':
                return None
            raise
        if payload['child_id'] != review['child_id']:
            raise ValueError('PARENT_ACTION_REPORT_MISMATCH')
        approval = c.execute("""SELECT audit_id FROM audit_event WHERE action IN ('parent_report_snapshot_approved','parent_report_snapshot_system_verified')
            AND child_id=? AND json_extract(summary_json,'$.snapshot_id')=? ORDER BY rowid DESC LIMIT 1""",
            (review['child_id'], review['snapshot_id'])).fetchone()
        if not approval:
            raise ValueError('PARENT_ACTION_APPROVAL_EVIDENCE_REQUIRED')
        return self._apply_review_edit(payload, review, approval[0])

    @staticmethod
    def _apply_review_edit(payload, review, approval_id):
        from .parent_reports import ParentReportService

        base_fingerprint = hashlib.sha256(_json(payload).encode()).hexdigest()
        edit = ParentReportService._normalized_parent_edit(review['parent_edit_json'])
        decision = edit['coaching_decision']
        coaching = payload['coaching']
        if decision == 'custom':
            if edit['coaching_script']:
                coaching['script'] = [edit['coaching_script']]
                coaching.pop('script_phases', None)
            action = coaching['action']
            if edit['coaching_action']:
                action['what'] = edit['coaching_action']
                action['steps'] = [edit['coaching_action']]
            if edit['coaching_duration_minutes'] is not None:
                action['duration_minutes'] = edit['coaching_duration_minutes']
            if edit['coaching_observation']:
                action['success_signal'] = edit['coaching_observation']
            # A different parent action is not evidence that the original teaching
            # protocol worked; retain the explicit proposal's identity for review.
            coaching['feedback_context'] = {**(coaching.get('feedback_context') or {}),
                'parent_custom_action_fingerprint': hashlib.sha256(_json({
                    'script': coaching['script'], 'action': action}).encode()).hexdigest()}
        elif decision == 'skip':
            coaching['action'] = None
            coaching['script'] = []
            coaching.pop('script_phases', None)
        payload.update(contract_version='parent_action_reviewed_v2', execution_allowed=decision != 'skip',
            review_binding={'snapshot_id': review['snapshot_id'], 'source_version': review['source_version'],
                'approval_event_id': approval_id, 'decision': decision,
                'edit_fingerprint': hashlib.sha256(_json(edit).encode()).hexdigest(),
                'base_action_fingerprint': base_fingerprint})
        payload['timing']['duration_minutes'] = coaching['action']['duration_minutes'] if coaching['action'] else 0
        return payload

    def freeze_reviewed(self, c, parent_report_snapshot_id):
        """Freeze the adopted action in the same transaction as parent approval."""
        if not c.in_transaction:
            raise ValueError('PARENT_ACTION_GENERATION_TRANSACTION_REQUIRED')
        review = c.execute('SELECT * FROM parent_report_snapshot WHERE snapshot_id=?',
            (parent_report_snapshot_id,)).fetchone()
        if not review:
            raise ValueError('PARENT_ACTION_REVIEW_NOT_FOUND')
        payload = self._reviewed_payload(c, review)
        return self._freeze_payload(c, review['report_id'], payload) if payload is not None else None

    def _review_current(self, c, frozen):
        binding = frozen.get('review_binding')
        if not binding:
            return True
        review = c.execute('SELECT * FROM parent_report_snapshot WHERE snapshot_id=? AND child_id=?',
            (binding['snapshot_id'], frozen['child_id'])).fetchone()
        try:
            return bool(review and self._reviewed_payload(c, review) == frozen)
        except (ValueError, TypeError, KeyError):
            return False

    def _historical_feedback_eligibility(self, c, row, frozen, *, _active_cache=None):
        """Verify the original approval; never grant current execution/plan authority."""
        denied = {'record_allowed': False, 'approval_witness_valid': False,
                  'current_execution_granted': False, 'reason': 'invalid_approval_witness'}
        from .parent_reports import ParentReportService
        try:
            binding = frozen['review_binding']
            if (frozen['contract_version'] != 'parent_action_reviewed_v2'
                    or hashlib.sha256(_json(frozen).encode()).hexdigest() != row['fingerprint']
                    or frozen['child_id'] != row['child_id'] or frozen['report_id'] != row['report_id']
                    or frozen.get('period_report_id') != row['period_report_id']):
                return denied
            review = c.execute('SELECT * FROM parent_report_snapshot WHERE snapshot_id=? AND child_id=?',
                (binding['snapshot_id'], row['child_id'])).fetchone()
            if not review or ParentReportService.snapshot_problem(review):
                return denied
            if binding['source_version'] != review['source_version']:
                return denied
            facts, inferences, content, confidence = (json.loads(review[k]) for k in
                ('facts_json', 'inferences_json', 'content_json', 'confidence_json'))
            if review['evidence_version'] != 'sha256:' + hashlib.sha256(_json({'facts': facts, 'inferences': inferences}).encode()).hexdigest():
                return denied
            source, separator, version = review['source_version'].rpartition(':')
            root, at, source_version = review['source_id'].rpartition('@')
            if not separator or not at or source != frozen['source_version'] or source_version != version:
                return denied
            approval = c.execute("""SELECT * FROM audit_event WHERE audit_id=? AND child_id=?
                AND action IN ('parent_report_snapshot_approved','parent_report_snapshot_system_verified')""",
                (binding['approval_event_id'], row['child_id'])).fetchone()
            if (not approval or approval['actor'] != review['approved_by']
                    or approval['occurred_at'] != review['approved_at']
                    or json.loads(approval['summary_json']) != {'snapshot_id': review['snapshot_id']}
                    or not _time(review['created_at']) <= _time(review['approved_at']) <= _time(row['created_at']) <= datetime.now(timezone.utc)):
                return denied
            coaching = content['parent_coaching']
            action = coaching['action']
            if not isinstance(action, dict) or not isinstance(action.get('what'), str) or not action['what'].strip():
                return denied
            if review['source_kind'] == 'daily_report':
                if root != row['report_id'] or row['period_report_id'] is not None:
                    return denied
                version_payload = {'reliable_learning_fingerprint': facts['evidence_manifest'].get('reliable_learning_fingerprint'),
                    'template_version': review['template_version'], 'source': source,
                    'facts': facts, 'inferences': inferences, 'content': content, 'confidence': confidence}
                if hashlib.sha256(_json(version_payload).encode()).hexdigest() != version:
                    return denied
                base = {'contract_version': 'parent_action_v1', 'report_id': root, 'child_id': row['child_id'],
                    'source_version': source, 'target': {'actor': 'parent', 'child_id': row['child_id']},
                    'timing': {'report_date': content['report_date'], 'when': action.get('when'),
                        'duration_minutes': action.get('duration_minutes'), 'planned_at': None}, 'coaching': coaching}
            elif review['source_kind'] == 'period_report':
                if row['report_id'] is not None or root != row['period_report_id']:
                    return denied
                period = c.execute('SELECT * FROM period_report WHERE period_report_id=? AND child_id=?', (root, row['child_id'])).fetchone()
                if not period or content['report_date'] != period['period_end']:
                    return denied
                version_payload = {'facts': facts, 'inferences': inferences, 'content': content['summary'],
                    'period_end': content['report_date'], 'source': source, 'template': review['template_version']}
                content_version = hashlib.sha256(_json(version_payload).encode()).hexdigest()[:16]
                if content_version != version and not ParentReportService._period_reissue_witness(c, review, content_version):
                    return denied
                base = {'contract_version': 'parent_action_reviewed_v2', 'report_id': None,
                    'period_report_id': root, 'source_kind': 'period_report', 'source_id': root,
                    'child_id': row['child_id'], 'source_version': source,
                    'target': {'actor': 'parent', 'child_id': row['child_id']},
                    'timing': {'report_date': period['period_end'], 'period_start': period['period_start'],
                        'period_end': period['period_end'], 'when': action.get('when'),
                        'duration_minutes': action.get('duration_minutes'), 'planned_at': None}, 'coaching': coaching}
            else:
                return denied
            if self._apply_review_edit(base, review, approval['audit_id']) != frozen:
                return denied
            # An explicit parent withdrawal also wins after automatic supersession.
            withdrawn = c.execute("""SELECT 1 FROM audit_event WHERE child_id=? AND actor='parent'
                AND action='parent_report_snapshot_revoked' AND json_valid(summary_json)
                AND json_extract(summary_json,'$.snapshot_id')=? LIMIT 1""", (row['child_id'], review['snapshot_id'])).fetchone()
            valid = {**denied, 'approval_witness_valid': True}
            if withdrawn:
                return {**valid, 'reason': 'explicit_parent_withdrawal'}
            # Historical approval is not permission to rely on withdrawn facts.
            # Validate only the original frozen references; later learning and
            # report-experience feedback may add sources without invalidating them.
            from .period_reports import PeriodReportService
            if review['source_kind'] == 'period_report':
                summary = facts['learning_summary']
                groups = [summary[key] for key in ('evidence', 'new_weaknesses',
                    'independent_variant_retests', 'pending_confirmation')]
                if (any(not isinstance(group, list) or any(not isinstance(item, dict)
                            or not isinstance(item.get('evidence_ref'), str) or not item['evidence_ref']
                            for item in group) for group in groups)
                        or not isinstance(summary['knowledge_support'], list)
                        or type(summary['coverage']['eligible_evidence_count']) is not int
                        or summary['coverage']['eligible_evidence_count'] != len(groups[0])):
                    return valid
                if not groups[0] and (summary['knowledge_support'] or any(
                        type(summary['coverage'][key]) is not int or summary['coverage'][key] != 0
                        for key in ('knowledge_nodes_covered', 'answered_evidence_count', 'independent_answered_count'))):
                    return valid
                coaching_refs = coaching['evidence_refs']
                if not isinstance(coaching_refs, list) or any(not isinstance(ref, str) or not ref for ref in coaching_refs):
                    return valid
                refs = {item['evidence_ref'] for group in groups for item in group}
                refs.update(ref for ref in coaching_refs if ref.startswith('record-'))
                ids = set()
                for evidence_ref in refs:
                    if not isinstance(evidence_ref, str) or not evidence_ref.startswith('record-'):
                        return valid
                    original = c.execute('''SELECT ref.evidence_id FROM period_report_evidence_reference ref
                        JOIN learning_evidence e ON e.evidence_id=ref.evidence_id AND e.child_id=ref.child_id
                        WHERE ref.evidence_ref=? AND ref.child_id=?''', (evidence_ref, row['child_id'])).fetchone()
                    if not original:
                        return valid
                    ids.add(original['evidence_id'])
            else:
                if ParentReportService.daily_source_problem(c, row['child_id'], root):
                    return valid
                original_learning = facts['evidence_detail']['reliable_learning']
                items, excluded = original_learning['items'], original_learning['excluded']
                if (not isinstance(items, list) or not isinstance(excluded, list)
                        or type(original_learning['unverified_count']) is not int
                        or original_learning['unverified_count'] != len(excluded)
                        or original_learning['mastery_claim_allowed'] is not False):
                    return valid
                ids = {item['evidence_id'] for item in items}
                if any(not isinstance(evidence_id, str) or not evidence_id for evidence_id in ids):
                    return valid
            if ids:
                # Reuse the original score/receipt/withdrawal/ancestry authority.
                # Daily-only legacy databases need no report-only withdrawal table.
                cache_key = (id(c), row['child_id'])
                if _active_cache is not None and c.in_transaction and cache_key in _active_cache:
                    active = _active_cache[cache_key]
                else:
                    if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='period_report_evidence_state'").fetchone():
                        active = PeriodReportService._effective_evidence(c, row['child_id'])
                    else:
                        from .learning_obligations import project_child_mastery_evidence
                        active = {item['evidence_id']: item for projection in
                            project_child_mastery_evidence(c, row['child_id']).values()
                            for item in projection['effective_evidence']}
                    if _active_cache is not None and c.in_transaction:
                        _active_cache[cache_key] = active
                if not ids <= active.keys():
                    return valid
            if review['status'] == 'approved':
                return {**valid, 'record_allowed': True, 'reason': 'original_approval'}
            # A reason string is not authority: require the original same-transaction
            # system event and its owned replacement, with no parent withdrawal.
            if review['status'] != 'revoked' or review['revoke_reason'] != '后续证据已更新；请审核新版草稿':
                return valid
            event_name = 'parent_daily_snapshot_superseded' if review['source_kind']=='daily_report' else 'parent_period_snapshot_superseded'
            events = c.execute("""SELECT * FROM audit_event WHERE child_id=? AND actor='system'
                AND action=? AND occurred_at=? AND json_valid(summary_json)
                AND json_extract(summary_json,'$.snapshot_id')=?""",
                (row['child_id'], event_name, review['revoked_at'], review['snapshot_id'])).fetchall()
            for event in events:
                summary = json.loads(event['summary_json'])
                keys = {'snapshot_id','replacement_snapshot_id'} | ({'report_id'} if review['source_kind']=='daily_report' else set())
                if set(summary) != keys or summary.get('report_id', root) != root:
                    continue
                replacement = c.execute('SELECT * FROM parent_report_snapshot WHERE snapshot_id=? AND child_id=?',
                    (summary['replacement_snapshot_id'], row['child_id'])).fetchone()
                if (replacement and replacement['snapshot_id'] != review['snapshot_id']
                        and replacement['source_kind'] == review['source_kind']
                        and replacement['source_id'].rpartition('@')[0] == root
                        and _time(review['approved_at']) <= _time(replacement['created_at']) <= _time(event['occurred_at'])
                        and _time(event['occurred_at']) >= _time(row['created_at'])):
                    return {**valid, 'record_allowed': True, 'reason': 'system_superseded_approval'}
            return valid
        except (ValueError, TypeError, KeyError):
            return denied

    def list_for_review(self, c, child_id, review_id):
        if not self._available(c):
            return []
        rows = c.execute("""SELECT snapshot_id FROM parent_action_snapshot WHERE child_id=?
            AND json_valid(payload_json) AND json_extract(payload_json,'$.review_binding.snapshot_id')=?
            ORDER BY version,snapshot_id""", (child_id, review_id)).fetchall()
        # Share only this list's already fixed transaction, never a later call.
        active_cache = {} if c.in_transaction else None
        return [self._get(c, child_id, row['snapshot_id'], _active_cache=active_cache) for row in rows]

    @staticmethod
    def _event(row, report_id, period_report_id=None):
        if not row:
            return None
        return {**json.loads(row["payload_json"]), "feedback_id": row["event_id"],
                "report_id": report_id,
                **({'period_report_id': period_report_id, 'source_kind': 'period_report', 'source_id': period_report_id} if period_report_id else {}),
                "action_snapshot_id": row["snapshot_id"], "child_id": row["child_id"],
                "revision": row["revision"], "operation": row["operation"],
                "client_action_id": row["client_action_id"], "created_at": row["created_at"],
                "updated_at": row["created_at"], "binding_status": "bound",
                "evidence_kind": "parent_reported_action"}

    def _get(self, c, child_id, snapshot_id, *, _active_cache=None):
        row = c.execute("SELECT * FROM parent_action_snapshot WHERE snapshot_id=? AND child_id=?", (snapshot_id, child_id)).fetchone()
        if not row:
            raise ValueError("PARENT_ACTION_NOT_FOUND_OR_NOT_OWNED")
        latest = c.execute("SELECT * FROM parent_action_feedback_event WHERE snapshot_id=? ORDER BY revision DESC LIMIT 1", (snapshot_id,)).fetchone()
        frozen = json.loads(row["payload_json"])
        feedback_eligibility = ({"feedback_eligibility": self._historical_feedback_eligibility(c, row, frozen, _active_cache=_active_cache)}
            if frozen.get("review_binding") else {})
        return {**frozen, **feedback_eligibility, "snapshot_id": snapshot_id,
                "version": row["version"], "fingerprint": row["fingerprint"],
                "created_at": row["created_at"], "binding_status": "bound", "feedback": self._event(latest, row["report_id"], dict(row).get('period_report_id'))}

    def get(self, child_id, snapshot_id):
        with self.database.connect() as c:
            if not self._available(c):
                raise ValueError("PARENT_ACTION_SNAPSHOT_UNAVAILABLE")
            return self._get(c, child_id, snapshot_id)

    def current(self, c, report_id):
        unknown = {"binding_status": "unknown", "reason": "action_snapshot_missing"}
        if not self._available(c):
            return unknown
        try:
            payload = self._payload(c, report_id)
        except (ValueError, TypeError):
            return unknown
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='parent_report_snapshot'").fetchone():
            review = c.execute("""SELECT * FROM parent_report_snapshot WHERE source_kind='daily_report'
                AND report_id=? AND child_id=? ORDER BY rowid DESC LIMIT 1""", (report_id, payload['child_id'])).fetchone()
            if review and review['status'] == 'approved':
                try:
                    payload = self._reviewed_payload(c, review)
                except (ValueError, TypeError, KeyError):
                    return {'binding_status': 'unknown', 'reason': 'review_source_not_current'}
                if payload is None:
                    return unknown
            elif review and c.execute("""SELECT 1 FROM parent_action_snapshot WHERE report_id=?
                AND json_extract(payload_json,'$.review_binding.snapshot_id')=? LIMIT 1""",
                (report_id, review['snapshot_id'])).fetchone():
                return {'binding_status': 'unknown', 'reason': 'review_approval_required'}
        fingerprint = hashlib.sha256(_json(payload).encode()).hexdigest()
        row = c.execute("SELECT snapshot_id FROM parent_action_snapshot WHERE report_id=? AND fingerprint=?", (report_id, fingerprint)).fetchone()
        return self._get(c, payload["child_id"], row[0]) if row else unknown

    def current_for_review(self, c, child_id, parent_report_snapshot_id):
        review = c.execute('SELECT * FROM parent_report_snapshot WHERE snapshot_id=? AND child_id=?',
            (parent_report_snapshot_id, child_id)).fetchone()
        if not review:
            raise ValueError('PARENT_ACTION_NOT_FOUND_OR_NOT_OWNED')
        unknown = {'binding_status': 'unknown', 'reason': 'review_approval_required'}
        if not self._available(c) or review['status'] != 'approved':
            return unknown
        try:
            payload = self._reviewed_payload(c, review)
        except (ValueError, TypeError, KeyError):
            return {'binding_status': 'unknown', 'reason': 'review_source_not_current'}
        if payload is None:
            return {'binding_status': 'unknown', 'reason': 'action_content_missing'}
        fingerprint = hashlib.sha256(_json(payload).encode()).hexdigest()
        row = c.execute('SELECT snapshot_id FROM parent_action_snapshot WHERE child_id=? AND fingerprint=?',
            (child_id, fingerprint)).fetchone()
        return self._get(c, child_id, row[0]) if row else unknown

    def history(self, child_id, snapshot_id):
        with self.database.connect() as c:
            if not self._available(c):
                raise ValueError("PARENT_ACTION_SNAPSHOT_UNAVAILABLE")
            snapshot = self._get(c, child_id, snapshot_id)
            return [self._event(row, snapshot["report_id"], snapshot.get('period_report_id')) for row in c.execute("SELECT * FROM parent_action_feedback_event WHERE snapshot_id=? ORDER BY revision", (snapshot_id,))]

    def list_actions(self, child_id, report_id):
        with self.database.connect() as c:
            if not self._available(c):
                return []
            return [self._get(c, child_id, row[0]) for row in c.execute(
                "SELECT snapshot_id FROM parent_action_snapshot WHERE report_id=? AND child_id=? ORDER BY version", (report_id, child_id)).fetchall()]

    def record_snapshot_feedback(self, *, child_id, snapshot_id, client_action_id, expected_revision,
                                 executed=None, executed_at=None, effectiveness=None, stress_rating=None,
                                 notes_summary=None, operation='record', reason=None):
        snapshot = self.get(child_id, snapshot_id)
        return self.record_feedback(child_id=child_id, report_id=snapshot['report_id'], snapshot_id=snapshot_id,
            client_action_id=client_action_id, expected_revision=expected_revision, executed=executed,
            executed_at=executed_at, effectiveness=effectiveness, stress_rating=stress_rating,
            notes_summary=notes_summary, operation=operation, reason=reason)

    def record_feedback(self, *, child_id, report_id, snapshot_id, client_action_id, expected_revision,
                        executed, executed_at, effectiveness, stress_rating, notes_summary=None,
                        operation="record", reason=None):
        if (not isinstance(client_action_id, str) or not client_action_id.strip() or len(client_action_id) > 200
                or type(expected_revision) is not int or expected_revision < 0):
            raise ValueError("PARENT_ACTION_REVISION_AND_KEY_REQUIRED")
        if operation not in {"record", "retract"} or (executed is not None and type(executed) is not bool):
            raise ValueError("PARENT_ACTION_FEEDBACK_INVALID")
        for rating in (effectiveness, stress_rating):
            if rating is not None and (type(rating) is not int or not 1 <= rating <= 5):
                raise ValueError("PARENT_ACTION_RATING_INVALID")
        for text in (notes_summary, reason):
            if text is not None and (not isinstance(text, str) or len(text) > 4000):
                raise ValueError("PARENT_ACTION_NOTE_INVALID")
        if operation == "retract":
            if not reason or not reason.strip() or any(value is not None for value in (executed, executed_at, effectiveness, stress_rating)):
                raise ValueError("PARENT_ACTION_RETRACTION_REASON_REQUIRED")
        elif executed is not True and any(value is not None for value in (executed_at, effectiveness, stress_rating)):
            raise ValueError("PARENT_ACTION_NOT_EXECUTED_HAS_NO_OUTCOME")
        if executed is True:
            executed_at = _time(executed_at).isoformat()
        payload = {"executed": executed, "executed_at": executed_at, "effectiveness": effectiveness,
                   "stress_rating": stress_rating, "notes_summary": notes_summary, "reason": reason}
        request = _json({**payload, "snapshot_id": snapshot_id, "report_id": report_id,
                         "operation": operation, "expected_revision": expected_revision})
        self.initialize()
        with self.database.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            snapshot = self._get(c, child_id, snapshot_id)
            if snapshot["report_id"] != report_id:
                raise ValueError("PARENT_ACTION_REPORT_MISMATCH")
            prior = c.execute("SELECT * FROM parent_action_feedback_event WHERE child_id=? AND client_action_id=?", (child_id, client_action_id)).fetchone()
            if prior:
                if prior["request_json"] != request:
                    raise ValueError("PARENT_ACTION_IDEMPOTENCY_CONFLICT")
                return self._event(prior, report_id, snapshot.get('period_report_id'))
            if operation == 'record' and snapshot.get('review_binding'):
                frozen = json.loads(c.execute('SELECT payload_json FROM parent_action_snapshot WHERE snapshot_id=?',
                    (snapshot_id,)).fetchone()[0])
                row = c.execute('SELECT * FROM parent_action_snapshot WHERE snapshot_id=? AND child_id=?', (snapshot_id, child_id)).fetchone()
                if not self._historical_feedback_eligibility(c, row, frozen)['record_allowed']:
                    raise ValueError('PARENT_ACTION_REVIEW_NOT_CURRENT')
                if snapshot.get('execution_allowed') is False and executed is True:
                    raise ValueError('PARENT_ACTION_SKIPPED_NOT_EXECUTABLE')
            elif operation == 'record':
                current = self.current(c, report_id)
                if current.get('review_binding') or current.get('reason') in {'review_source_not_current', 'review_approval_required'}:
                    raise ValueError('PARENT_ACTION_REVIEWED_VERSION_REQUIRED')
            latest = snapshot["feedback"]
            if expected_revision != (latest["revision"] if latest else 0):
                raise ValueError("PARENT_ACTION_REVISION_CONFLICT")
            if operation == "retract" and (not latest or latest["operation"] == "retract"):
                raise ValueError("PARENT_ACTION_NOT_ACTIVE")
            now = datetime.now(timezone.utc)
            if executed is True and not _time(snapshot["created_at"]) <= _time(executed_at) <= now:
                raise ValueError("PARENT_ACTION_EXECUTION_TIME_OUT_OF_RANGE")
            event_id = str(uuid4())
            c.execute("INSERT INTO parent_action_feedback_event VALUES (?,?,?,?,?,?,?,?,?)",
                      (event_id, snapshot_id, child_id, expected_revision + 1, operation, _json(payload), request, client_action_id, now.isoformat()))
            c.commit()
            return self._event(c.execute("SELECT * FROM parent_action_feedback_event WHERE event_id=?", (event_id,)).fetchone(), report_id, snapshot.get('period_report_id'))

    def feedback_for_child(self, c, child_id):
        if not self._available(c):
            return []
        rows = c.execute("""SELECT e.*,s.report_id,s.version,s.payload_json AS snapshot_json
            FROM parent_action_feedback_event e JOIN parent_action_snapshot s ON s.snapshot_id=e.snapshot_id AND s.child_id=e.child_id
            WHERE e.child_id=? AND e.operation='record' AND e.revision=(
              SELECT MAX(revision) FROM parent_action_feedback_event WHERE snapshot_id=e.snapshot_id)
            ORDER BY e.created_at DESC,e.rowid DESC""", (child_id,)).fetchall()
        items = []
        for row in rows:
            frozen = json.loads(row["snapshot_json"])
            if frozen.get('review_binding') and (not self._review_current(c, frozen) or frozen.get('execution_allowed') is False):
                continue
            context = dict(frozen["coaching"].get("feedback_context") or {})
            if frozen["source_version"] not in {"local_rules_v2", "local_rules_revalidated_v3"}:
                context["version"] = frozen["source_version"]
            items.append({**self._event(row, row["report_id"], frozen.get('period_report_id')), "action_version": row["version"],
                          "feedback_kind": "action_execution", "feedback_context": context})
        return items
