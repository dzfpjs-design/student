from __future__ import annotations

"""Immutable, parent-approved report snapshots.

The legacy daily/period report tables remain the live learning view. This module is
the delivery boundary: content is copied and versioned before it can leave the
device, so later corrections never silently rewrite an already-approved report.
"""

from datetime import date, datetime, timezone
from contextlib import ExitStack, contextmanager, nullcontext
from functools import wraps
import hashlib
import json
import sqlite3
from typing import Any
from uuid import uuid4

from .database import Database, ExternalEffectBusyError, _EFFECT_LOCKS, _EFFECT_LOCKS_LOCK
from .repository import utc_now
from .report_source_reads import report_read_connection, report_source_connection, scoped_validation


def _held_original_effect_transaction(database, connection):
    """Reject a borrowed writer unless its real database guard is already held."""
    if database._original_connections.get(id(connection)) is not connection or not connection.in_transaction:
        return False
    with _EFFECT_LOCKS_LOCK:
        state = _EFFECT_LOCKS.get(database.path.resolve())
    return bool(state and getattr(state.owner, 'active', False))


class ReportEffectBusyError(Exception):
    """The report operation did not acquire the effect guard on this attempt."""


@contextmanager
def _report_effect_guard(database, timeout=10):
    with ExitStack() as stack:
        try:
            stack.enter_context(database.external_effect_guard(timeout=timeout))
        except ExternalEffectBusyError as error:
            raise ReportEffectBusyError() from error
        yield


def _guard_report_change(function):
    @wraps(function)
    def guarded(self, *args, **kwargs):
        with _report_effect_guard(self.database):
            return function(self, *args, **kwargs)
    return guarded


def _guard_report_issue(function):
    @wraps(function)
    def guarded(self, *args, **kwargs):
        borrowed = kwargs.get('_connection')
        if borrowed is not None:
            if not _held_original_effect_transaction(self.database, borrowed):
                raise ValueError('REPORT_BORROWED_TRANSACTION_GUARD_REQUIRED')
            return function(self, *args, **kwargs)
        with _report_effect_guard(self.database):
            return function(self, *args, **kwargs)
    return guarded


class UnknownReportSnapshotError(KeyError):
    pass


class SnapshotStateError(ValueError):
    pass


class SavedEditCarryConflictError(SnapshotStateError):
    """This transfer was rejected before any write; prior commands remain intact."""


class DailySnapshotSourceChangedError(SnapshotStateError):
    """Daily evidence changed between reading the report and freezing it."""


class UnreadableReportSnapshotError(SnapshotStateError):
    pass


class SnapshotFeedbackConflictError(ValueError):
    pass


class SnapshotFeedbackValidationError(ValueError):
    pass


SNAPSHOT_TEMPLATE_VERSION = "parent_report_snapshot_v1"

SOURCE_ISSUE_MESSAGES = {
    'REPORT_SOURCE_MISSING': '报告的原始来源已不存在，旧稿已保留但不能批准或发送。',
    'REPORT_REFLECTION_MISSING': '这份历史日报缺少原始反思记录，不能继续作为有效报告。',
    'REPORT_SESSION_INCOMPLETE': '报告与已完成会话的对应关系不完整，请先核对原始记录。',
    'REPORT_SOURCE_INVALID_JSON': '这份报告的来源数据损坏，旧稿已保留，需从完整备份恢复后重新核对。',
    'REPORT_SOURCE_NOT_READY': '报告来源暂时不能通过核验，旧稿已保留，请核对后刷新。',
    'REPORT_SNAPSHOT_INVALID_JSON': '这份历史快照损坏，已停止批准和发送；其他完整报告不受影响。',
}

DAILY_STRUCTURE_SELECT = """SELECT r.*, s.child_id AS session_child_id, s.status AS session_status,
    EXISTS(SELECT 1 FROM daily_reflection f WHERE f.session_id=r.session_id AND f.child_id=r.child_id) AS has_reflection
    FROM daily_report r LEFT JOIN daily_session s ON s.session_id=r.session_id"""


class ParentReportService:
    def __init__(self, database: Database):
        self.database = database
        self._period_reports = None

    def set_period_report_service(self, service) -> None:
        """Share the host's authoritative behavior context during revalidation."""
        self._period_reports = service

    def prepare_daily_observation_source(self, child_id: str, day: str, recipient_id: str, expected_source_id: str | None = None) -> dict:
        """Freeze real same-day observation identities locally, without a reflection or delivery."""
        from .daily_report_observation import DailyReportObservationService
        return DailyReportObservationService(self.database).prepare(child_id, day, recipient_id, expected_source_id)

    def daily_observation_source_status(self, child_id: str, day: str, recipient_id: str) -> dict:
        from .daily_report_observation import DailyReportObservationService
        return DailyReportObservationService(self.database).inspect_current(child_id, day, recipient_id)

    @staticmethod
    def daily_source_problem(connection, child_id: str, report_id: str) -> str | None:
        """Structural source checks, including facts outside learning fingerprints."""
        row = connection.execute(DAILY_STRUCTURE_SELECT + ' WHERE r.report_id=? AND r.child_id=?',
                                 (report_id, child_id)).fetchone()
        return ParentReportService._daily_row_problem(row)

    @staticmethod
    def _daily_row_problem(row):
        if not row:
            return 'REPORT_SOURCE_MISSING'
        if row['session_child_id'] != row['child_id'] or row['session_status'] != 'completed':
            return 'REPORT_SESSION_INCOMPLETE'
        if not row['has_reflection']:
            return 'REPORT_REFLECTION_MISSING'
        try:
            for column in ('evidence_json', 'summary_json', 'parent_coaching_json'):
                if not isinstance(json.loads(row[column]), dict):
                    return 'REPORT_SOURCE_INVALID_JSON'
            count = json.loads(row['summary_json']).get('evidence_count')
            if type(count) is not int or count < 0:
                return 'REPORT_SOURCE_INVALID_JSON'
        except json.JSONDecodeError:
            return 'REPORT_SOURCE_INVALID_JSON'
        return None

    @staticmethod
    def snapshot_problem(row):
        try:
            for column in ('facts_json', 'inferences_json', 'content_json', 'confidence_json', 'parent_edit_json'):
                if not isinstance(json.loads(row[column]), dict):
                    return 'REPORT_SNAPSHOT_INVALID_JSON'
        except json.JSONDecodeError:
            return 'REPORT_SNAPSHOT_INVALID_JSON'
        return None

    @staticmethod
    def period_source_problem(connection, child_id, report_id):
        row = connection.execute('SELECT * FROM period_report WHERE child_id=? AND period_report_id=?', (child_id, report_id)).fetchone()
        return ParentReportService.period_row_problem(row)

    @staticmethod
    def period_row_problem(row):
        if row is None:
            return 'REPORT_SOURCE_MISSING'
        try:
            content = json.loads(row['content_json'])
            if not isinstance(content, dict):
                return 'REPORT_SOURCE_INVALID_JSON'
            start, end = date.fromisoformat(row['period_start']), date.fromisoformat(row['period_end'])
            date.fromisoformat(content.get('as_of_date') or row['period_end'])
            if start > end:
                return 'REPORT_SOURCE_INVALID_JSON'
        except (json.JSONDecodeError, ValueError, TypeError):
            return 'REPORT_SOURCE_INVALID_JSON'
        return None

    @staticmethod
    def _source_root(row):
        return row['report_id'] or row['source_id'].rpartition('@')[0] or row['source_id']

    def source_issues(self, child_id: str, *, include_resolved=False, connection=None) -> list[dict]:
        with nullcontext(connection) if connection is not None else self.database.connect() as c:
            rows = c.execute("""SELECT audit_id,summary_json,occurred_at FROM audit_event
                WHERE child_id=? AND action='parent_report_source_issue' ORDER BY rowid DESC""", (child_id,)).fetchall()
            daily = c.execute(DAILY_STRUCTURE_SELECT + ' WHERE r.child_id=?', (child_id,)).fetchall()
            periods = c.execute('SELECT * FROM period_report WHERE child_id=?', (child_id,)).fetchall()
            current_problems = {('daily_report', item['report_id']): self._daily_row_problem(item) for item in daily}
            current_problems.update({('period_report', item['period_report_id']): self.period_row_problem(item) for item in periods})
            metadata = {('daily_report', item['report_id']): {'report_type': 'daily', 'report_date': item['report_date']}
                        for item in daily}
            metadata.update({('period_report', item['period_report_id']): {'report_type': item['report_type'], 'report_date': item['period_end']}
                             for item in periods})
            snapshots = {}
            if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='parent_report_snapshot'").fetchone():
                for item in c.execute('SELECT * FROM parent_report_snapshot WHERE child_id=? ORDER BY rowid', (child_id,)):
                    metadata[('parent_report_snapshot', item['snapshot_id'])] = {'report_type': item['report_type'], 'report_date': item['created_at'][:10]}
                    current_problems[('parent_report_snapshot', item['snapshot_id'])] = self.snapshot_problem(item)
                    if not self.snapshot_problem(item):
                        snapshots[(item['source_kind'], self._source_root(item))] = item['snapshot_id']
        latest = {}
        for row in rows:
            issue = json.loads(row['summary_json'])
            identity = issue['source_kind'], issue['source_id']
            if identity not in latest:
                latest[identity] = {**issue, 'issue_id': row['audit_id'], 'child_id': child_id,
                    **metadata.get(identity, {}), 'snapshot_id': snapshots.get(identity),
                    'updated_at': row['occurred_at'], 'automatic_delivery': False,
                    'message': SOURCE_ISSUE_MESSAGES.get(issue.get('error_code'), '来源已重新核对，旧快照仍保持撤销。'),
                    'recovery_hint': '核对原始会话或恢复完整备份后刷新；不要补造反思，也不要重发旧稿。'}
        return [item for identity, item in latest.items() if include_resolved or (
            item['status'] == 'open' and (identity in metadata or identity in snapshots)
            and (item['error_code'] == 'REPORT_SOURCE_NOT_READY'
                 or current_problems.get(identity, 'REPORT_SOURCE_MISSING') is not None))]

    @_guard_report_issue
    def record_source_issue(self, child_id: str, source_kind: str, source_id: str, error_code: str | None,
                            *, _connection=None, _issue_states=None, _source_snapshots=None) -> dict | None:
        """Deduplicate durable source problems and revoke only this source's copies."""
        if source_kind not in ('daily_report', 'period_report', 'parent_report_snapshot'):
            raise ValueError('Unknown report source kind')
        if error_code is not None and error_code not in SOURCE_ISSUE_MESSAGES:
            raise ValueError('Unknown report source issue code')
        now = utc_now()
        with nullcontext(_connection) if _connection is not None else self.database.connect() as c:
            if _connection is None:
                c.execute('BEGIN IMMEDIATE')
            if error_code is None and source_kind == 'daily_report':
                error_code = self.daily_source_problem(c, child_id, source_id)
            if error_code is None and source_kind == 'period_report':
                error_code = self.period_source_problem(c, child_id, source_id)
            if error_code is None and source_kind == 'parent_report_snapshot':
                row = c.execute('SELECT * FROM parent_report_snapshot WHERE snapshot_id=? AND child_id=?', (source_id, child_id)).fetchone()
                error_code = self.snapshot_problem(row) if row else None
            if _issue_states is None:
                previous_row = c.execute("""SELECT summary_json FROM audit_event
                    WHERE child_id=? AND action='parent_report_source_issue'
                      AND json_extract(summary_json,'$.source_kind')=?
                      AND json_extract(summary_json,'$.source_id')=? ORDER BY rowid DESC LIMIT 1""",
                    (child_id, source_kind, source_id)).fetchone()
                previous = json.loads(previous_row['summary_json']) if previous_row else None
            else:
                previous = _issue_states.get((source_kind, source_id))
            status = 'open' if error_code else 'resolved'
            if error_code and c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='parent_report_snapshot'").fetchone():
                if _source_snapshots is not None:
                    rows = _source_snapshots.get((source_kind, source_id), ())
                elif source_kind == 'parent_report_snapshot':
                    rows = c.execute('SELECT * FROM parent_report_snapshot WHERE child_id=? AND snapshot_id=?', (child_id, source_id)).fetchall()
                else:
                    rows = c.execute('SELECT * FROM parent_report_snapshot WHERE child_id=? AND source_kind=? AND (report_id=? OR source_id=? OR substr(source_id,1,length(?)+1)=? || \'@\')',
                                     (child_id, source_kind, source_id, source_id, source_id, source_id)).fetchall()
                has_outbox = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='report_delivery_outbox'").fetchone()
                for row in rows:
                    c.execute("""UPDATE parent_report_snapshot SET status='revoked',revoked_at=?,revoke_reason=?
                        WHERE snapshot_id=? AND status!='revoked'""", (now, SOURCE_ISSUE_MESSAGES[error_code], row['snapshot_id']))
                    if has_outbox:
                        c.execute("""UPDATE report_delivery_outbox SET status='cancelled',cancelled_at=?,
                            cancel_reason='report_source_unavailable',updated_at=?
                            WHERE snapshot_id=? AND status IN ('scheduled','failed')""", (now, now, row['snapshot_id']))
            if (previous is None and error_code is None) or (previous and previous['status'] == status and previous['error_code'] == error_code):
                if _connection is None:
                    c.commit()
                return previous
            issue = {'source_kind': source_kind, 'source_id': source_id, 'status': status, 'error_code': error_code}
            c.execute("""INSERT INTO audit_event(audit_id,actor,action,child_id,summary_json,occurred_at)
                VALUES (?,'system','parent_report_source_issue',?,?,?)""",
                (str(uuid4()), child_id, json.dumps(issue, ensure_ascii=False), now))
            if _issue_states is not None:
                _issue_states[(source_kind, source_id)] = issue
            if _connection is None:
                c.commit()
        return issue

    @_guard_report_change
    def reconcile_daily_sources(self, child_id: str, limit: int | None = 30) -> list[dict]:
        from .repository import ReportNotReadyError, StudyRepository, UnknownReportError, UnknownSessionError
        # History gets only structural inspection. Expensive learning/catalog
        # materialization is bounded; approval and sending still check exact sources.
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            rows = c.execute(DAILY_STRUCTURE_SELECT + ' WHERE r.child_id=?', (child_id,)).fetchall()
            problems = {row['report_id']: self._daily_row_problem(row) for row in rows}
            snapshots = c.execute('SELECT * FROM parent_report_snapshot WHERE child_id=?', (child_id,)).fetchall()
            by_source = {}
            for row in snapshots:
                by_source.setdefault((row['source_kind'], self._source_root(row)), []).append(row)
                by_source[('parent_report_snapshot', row['snapshot_id'])] = [row]
            states = {}
            for event in c.execute("SELECT summary_json FROM audit_event WHERE child_id=? AND action='parent_report_source_issue' ORDER BY rowid DESC", (child_id,)):
                state = json.loads(event['summary_json'])
                states.setdefault((state['source_kind'], state['source_id']), state)
            for row in snapshots:
                problem = self.snapshot_problem(row)
                identity = ('parent_report_snapshot', row['snapshot_id'])
                if problem or states.get(identity, {}).get('status') == 'open':
                    self.record_source_issue(child_id, *identity, problem, _connection=c, _issue_states=states, _source_snapshots=by_source)
                if row['source_kind'] == 'daily_report':
                    problems.setdefault(self._source_root(row), 'REPORT_SOURCE_MISSING')
            for root, problem in problems.items():
                previous = states.get(('daily_report', root), {})
                structurally_recovered = (previous.get('status') == 'open' and previous.get('error_code') in {
                    'REPORT_SOURCE_MISSING', 'REPORT_REFLECTION_MISSING', 'REPORT_SESSION_INCOMPLETE', 'REPORT_SOURCE_INVALID_JSON'})
                if problem or structurally_recovered:
                    self.record_source_issue(child_id, 'daily_report', root, problem, _connection=c, _issue_states=states, _source_snapshots=by_source)
            c.commit()
        reports = StudyRepository(self.database).list_daily_reports(child_id, max(1, min(limit or 30, 100)))
        for report in reports:
            try:
                self._refresh_daily_snapshot(report['report_id'], child_id=child_id, initial_report=report)
            except UnreadableReportSnapshotError:
                continue
            except json.JSONDecodeError:
                self.record_source_issue(child_id, 'daily_report', report['report_id'], 'REPORT_SOURCE_INVALID_JSON')
            except (SnapshotStateError, ReportNotReadyError, UnknownReportError, UnknownSessionError):
                self.record_source_issue(child_id, 'daily_report', report['report_id'], 'REPORT_SOURCE_NOT_READY')
        return self.source_issues(child_id)

    def _refresh_daily_snapshot(self, report_id: str, *, child_id: str, initial_report: dict | None = None) -> dict:
        from .repository import StudyRepository

        # Only a fresh coordinator read may retry; historical caller payloads and
        # unrelated validation failures must never acquire current approval.
        for attempt in range(2):
            report = initial_report if attempt == 0 and initial_report is not None else StudyRepository(self.database).get_daily_report(report_id)
            if report['report_id'] != report_id or report['child_id'] != child_id:
                raise SnapshotStateError('报告快照不属于当前孩子。')
            try:
                return self.create_from_daily(report)
            except DailySnapshotSourceChangedError:
                if attempt == 1:
                    raise

    def _current_period_report(self, report_id):
        from .period_reports import PeriodReportService
        service = self._period_reports or PeriodReportService(self.database)
        return service.get(report_id, persist_refresh=True)

    def initialize(self) -> None:
        with self.database.connect() as connection:
            connection.executescript("""
            CREATE TABLE IF NOT EXISTS parent_report_snapshot (
              snapshot_id TEXT PRIMARY KEY,
              source_kind TEXT NOT NULL CHECK(source_kind IN ('daily_report', 'period_report')),
              source_id TEXT NOT NULL,
              report_id TEXT REFERENCES daily_report(report_id) ON DELETE RESTRICT,
              child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE RESTRICT,
              report_type TEXT NOT NULL CHECK(report_type IN ('daily', 'weekly', 'monthly')),
              source_version TEXT NOT NULL,
              template_version TEXT NOT NULL,
              evidence_version TEXT NOT NULL,
              facts_json TEXT NOT NULL,
              inferences_json TEXT NOT NULL,
              content_json TEXT NOT NULL,
              confidence_json TEXT NOT NULL,
              status TEXT NOT NULL CHECK(status IN ('draft', 'approved', 'revoked')),
              parent_edit_json TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL,
              approved_at TEXT,
              approved_by TEXT,
              revoked_at TEXT,
              revoke_reason TEXT,
              UNIQUE(source_kind, source_id)
            );
            CREATE INDEX IF NOT EXISTS idx_parent_report_snapshot_child_created
              ON parent_report_snapshot(child_id, created_at DESC);
            CREATE TABLE IF NOT EXISTS parent_report_snapshot_feedback (
              feedback_id TEXT PRIMARY KEY,
              snapshot_id TEXT NOT NULL REFERENCES parent_report_snapshot(snapshot_id) ON DELETE CASCADE,
              helpfulness INTEGER CHECK(helpfulness BETWEEN 1 AND 5),
              stress_rating INTEGER CHECK(stress_rating BETWEEN 1 AND 5),
              notes TEXT,
              client_action_id TEXT,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS parent_report_snapshot_correction (
              correction_id TEXT PRIMARY KEY,
              snapshot_id TEXT NOT NULL REFERENCES parent_report_snapshot(snapshot_id) ON DELETE CASCADE,
              note TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            """)
            from .report_followup import ReportFollowupService
            ReportFollowupService.ensure_schema(connection)
            feedback_columns = {row["name"] for row in connection.execute("PRAGMA table_info(parent_report_snapshot_feedback)").fetchall()}
            if "client_action_id" not in feedback_columns:
                connection.execute("ALTER TABLE parent_report_snapshot_feedback ADD COLUMN client_action_id TEXT")
            connection.execute("DROP INDEX IF EXISTS idx_parent_report_snapshot_feedback_action")
            connection.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_parent_report_snapshot_feedback_action
                ON parent_report_snapshot_feedback(client_action_id)
                WHERE client_action_id IS NOT NULL
            """)
            connection.commit()

    @_guard_report_change
    def create_from_daily(self, report: dict) -> dict:
        """Idempotently freezes the complete report payload at generation time."""
        self.initialize()
        with self.database.connect() as connection:
            owner = connection.execute('SELECT * FROM daily_report WHERE report_id=?', (report['report_id'],)).fetchone()
            if owner and owner['child_id'] != report['child_id']:
                raise SnapshotStateError('报告快照不属于当前孩子。')
        evidence = report.get("evidence") or {}
        evidence_detail = report.get("evidence_detail") or {}
        facts = {
            "facts": (report.get("summary") or {}).get("facts", []),
            "evidence_manifest": evidence,
            "evidence_detail": evidence_detail,
        }
        with self.database.connect() as connection:
            facts['source_qualification_fingerprint'] = self.source_qualification_fingerprint(connection, report['child_id'], 'daily_report', report['report_id'])
        inferences = {
            "interpretation": (report.get("summary") or {}).get("interpretation"),
            "parent_coaching": report.get("parent_coaching") or {},
            "limitations": ["报告基于已确认的学习记录；未记录的信息不作推断。", "不用于医学诊断、人格标签或与他人比较。"],
        }
        content = {
            "headline": (report.get("summary") or {}).get("headline", "学习报告"),
            "report_date": report.get("report_date"),
            "summary": report.get("summary") or {},
            "parent_coaching": report.get("parent_coaching") or {},
            "evidence_detail": evidence_detail,
        }
        confidence = self._confidence(report)
        live_content_matches = bool(owner and json.loads(owner['summary_json'])==report.get('summary')
            and json.loads(owner['parent_coaching_json'])==report.get('parent_coaching')
            and json.loads(owner['evidence_json'])==report.get('evidence'))
        # An exact historical replay reads its immutable old version; it must not
        # pair old report text with today's qualification fingerprint as a new draft.
        with self.database.connect() as connection:
            history = connection.execute("SELECT * FROM parent_report_snapshot WHERE source_kind='daily_report' AND report_id=? AND child_id=?", (report['report_id'], report['child_id'])).fetchall()
        for previous in history:
            if self.snapshot_problem(previous):
                self.record_source_issue(report['child_id'], 'parent_report_snapshot', previous['snapshot_id'], 'REPORT_SNAPSHOT_INVALID_JSON')
                continue
            old_facts = json.loads(previous['facts_json'])
            previous_qualification = old_facts.pop('source_qualification_fingerprint', None)
            if (previous_qualification and (previous_qualification==facts['source_qualification_fingerprint'] or not live_content_matches)
                    and previous['template_version']==SNAPSHOT_TEMPLATE_VERSION
                    and previous['source_version'].rsplit(':',1)[0]==str(report.get('source') or 'unknown')
                    and old_facts=={key: value for key,value in facts.items() if key!='source_qualification_fingerprint'}
                    and json.loads(previous['inferences_json'])==inferences
                    and json.loads(previous['content_json'])==content
                    and json.loads(previous['confidence_json'])==confidence):
                return self.get(previous['snapshot_id'])
        version_payload = json.dumps({
            "reliable_learning_fingerprint": evidence.get("reliable_learning_fingerprint"),
            "template_version": SNAPSHOT_TEMPLATE_VERSION, "source": report.get("source"),
            "facts": facts, "inferences": inferences, "content": content, "confidence": confidence,
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        version = hashlib.sha256(version_payload.encode("utf-8")).hexdigest()
        return self._insert_snapshot(
            source_kind="daily_report", source_id=f"{report['report_id']}@{version}", report_id=report["report_id"], child_id=report["child_id"], report_type="daily",
            source_version=f"{report.get('source') or 'unknown'}:{version}", facts=facts, inferences=inferences,
            content=content, confidence=confidence, supersede_daily=True,
        )

    @_guard_report_change
    @scoped_validation
    def create_from_period(self, report: dict) -> dict:
        self.initialize()
        report_type = report["report_type"]
        content = report.get("content") or {}
        learning_summary = content.get("learning_summary") or {}
        coaching = content.get("parent_coaching") or {"action": content.get("next_action") if isinstance(content.get("next_action"), dict) else None, "script": content.get("parent_script") or []}
        facts = {
            "coverage": content.get("coverage") or {},
            "behavior_source_fingerprint": content.get("behavior_source_fingerprint"),
            "daily_signals": content.get("daily_signals") or [],
            "learning_summary": learning_summary,
            "goal_progress": content.get("goal_progress") or [],
        }
        with report_source_connection(self.database) as connection:
            facts['source_qualification_fingerprint'] = self.source_qualification_fingerprint(connection, report['child_id'], 'period_report', report['period_report_id'])
        inferences = {"interpretation": content.get("interpretation"), "parent_coaching": coaching, "limitations": ["趋势只在记录天数与可追溯证据达到门槛时可解释。", "单次正确或一次变式复测不等同稳定掌握；未作答不作负面推断。", "不用于能力、人格或医学判断。"]}
        version_payload = json.dumps({"facts": facts, "inferences": inferences, "content": content,
            "period_end": report.get("period_end"), "source": report.get("source"),
            "template": SNAPSHOT_TEMPLATE_VERSION}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        version = hashlib.sha256(version_payload.encode("utf-8")).hexdigest()[:16]
        snapshot = self._insert_snapshot(
            source_kind="period_report", source_id=f"{report['period_report_id']}@{version}", report_id=None, child_id=report["child_id"], report_type=report_type,
            source_version=f"{str(report.get('source') or 'unknown')}:{version}", facts=facts, inferences=inferences,
            content={"headline": content.get("headline", "阶段报告"), "report_date": report.get("period_end"), "summary": content, "parent_coaching": coaching, "evidence_detail": {"period_learning": learning_summary}},
            confidence={"overall": "high" if (content.get("coverage") or {}).get("sufficient_for_trend") else "limited", "coverage": content.get("coverage") or {}, "uncertainty": learning_summary.get("uncertainty") or "记录不足时不解释趋势；未记录的信息不作推断。"},
            supersede_period=report['period_report_id'],
        )
        return snapshot

    def _supersede_period_drafts(self, connection, child_id: str, period_report_id: str, current_snapshot_id: str) -> None:
        """A changed live period makes every earlier version ineligible for new sends."""
        now = utc_now()
        previous = connection.execute("""SELECT snapshot_id FROM parent_report_snapshot
                WHERE child_id = ? AND source_kind = 'period_report' AND source_id LIKE ?
                  AND snapshot_id != ? AND status != 'revoked'""", (child_id, f"{period_report_id}@%", current_snapshot_id)).fetchall()
        for row in previous:
            connection.execute("UPDATE parent_report_snapshot SET status = 'revoked', revoked_at = ?, revoke_reason = '后续证据已更新；请审核新版草稿' WHERE snapshot_id = ?", (now, row["snapshot_id"]))
            if connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'report_delivery_outbox'").fetchone():
                connection.execute("""UPDATE report_delivery_outbox SET status = 'cancelled', cancelled_at = ?, cancel_reason = 'period_snapshot_superseded', updated_at = ?
                      WHERE snapshot_id = ? AND status IN ('scheduled', 'failed')""", (now, now, row["snapshot_id"]))
            connection.execute("INSERT INTO audit_event(audit_id, actor, action, child_id, summary_json, occurred_at) VALUES (?, 'system', 'parent_period_snapshot_superseded', ?, ?, ?)", (str(uuid4()), child_id, json.dumps({"snapshot_id": row["snapshot_id"], "replacement_snapshot_id": current_snapshot_id}, ensure_ascii=False), now))

    def _supersede_daily_drafts(self, connection, child_id: str, report_id: str, current_snapshot_id: str, now: str) -> None:
        previous = connection.execute("""SELECT snapshot_id FROM parent_report_snapshot
            WHERE child_id=? AND source_kind='daily_report' AND (report_id=? OR source_id=?)
              AND snapshot_id!=? AND status!='revoked'""",
            (child_id, report_id, report_id, current_snapshot_id)).fetchall()
        has_outbox = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='report_delivery_outbox'").fetchone()
        for row in previous:
            connection.execute("""UPDATE parent_report_snapshot SET status='revoked',revoked_at=?,
                revoke_reason='后续证据已更新；请审核新版草稿' WHERE snapshot_id=?""", (now, row["snapshot_id"]))
            if has_outbox:
                # Submitted/uncertain attempts retain their delivery history. The
                # dispatcher's approval check blocks processing not yet submitted.
                connection.execute("""UPDATE report_delivery_outbox SET status='cancelled',cancelled_at=?,
                    cancel_reason='daily_snapshot_superseded',updated_at=?
                    WHERE snapshot_id=? AND status IN ('scheduled','failed')""", (now, now, row["snapshot_id"]))
            connection.execute("""INSERT INTO audit_event(audit_id,actor,action,child_id,summary_json,occurred_at)
                VALUES (?,'system','parent_daily_snapshot_superseded',?,?,?)""",
                (str(uuid4()), child_id, json.dumps({"snapshot_id": row["snapshot_id"],
                    "replacement_snapshot_id": current_snapshot_id, "report_id": report_id}, ensure_ascii=False), now))

    @staticmethod
    def _period_reissue_issuance(c, family, existing_id, source_id, child_id, report_type,
                                *, not_after=None):
        """Read the original system-correction graph; never grant current authority."""
        # Only original, same-family system edges qualify the corrective
        # predecessor. An arbitrary latest pointer or reason string cannot.
        edges = []
        for event in c.execute("""SELECT summary_json,occurred_at FROM audit_event
                WHERE child_id=? AND actor='system' AND action='parent_period_snapshot_superseded'
                ORDER BY rowid""", (child_id,)):
            try:
                proof = json.loads(event['summary_json'])
                if not isinstance(proof,dict) or set(proof)!={'snapshot_id','replacement_snapshot_id'}:
                    continue
                old_id, new_id = proof['snapshot_id'], proof['replacement_snapshot_id']
                if not isinstance(old_id,str) or not isinstance(new_id,str) or old_id==new_id:
                    continue
                prior, target = family.get(old_id), family.get(new_id)
                if prior is None or target is None:
                    continue
                times = [datetime.fromisoformat(value.replace('Z','+00:00')) for value in
                         (prior['created_at'],target['created_at'],event['occurred_at'])]
                if (prior['status']=='revoked' and prior['revoked_at']==event['occurred_at']
                        and prior['report_type']==target['report_type']==report_type
                        and all(instant.tzinfo is not None for instant in times)
                        and times[0]<=times[1]<=times[2]
                        and (not_after is None or times[2]<=not_after)):
                    edges.append((old_id,new_id))
            except (ValueError,TypeError,KeyError,AttributeError):
                continue
        reachable = {existing_id}
        for _ in range(len(family)):
            before = len(reachable)
            reachable.update(target for prior,target in edges if prior in reachable)
            if len(reachable)==before:
                break
        predecessor = next(reversed(family))
        if predecessor==existing_id or predecessor not in reachable:
            return None
        issuance = hashlib.sha256(json.dumps({'schema':'period_reissue_v1',
            'content_source_id':source_id,'system_corrective_predecessor':predecessor},
            sort_keys=True,separators=(',',':')).encode()).hexdigest()[:16]
        return issuance

    @staticmethod
    def _period_reissue_witness(c, review, content_version):
        """Reconstruct issuance from its original family prefix, not today's latest row."""
        try:
            root, at, issuance = review['source_id'].rpartition('@')
            source, colon, version = review['source_version'].rpartition(':')
            if (not root or not at or not colon or issuance!=version
                    or review['source_kind']!='period_report'
                    or review['template_version']!=SNAPSHOT_TEMPLATE_VERSION
                    or ParentReportService.snapshot_problem(review)):
                return False
            issued_at = datetime.fromisoformat(review['created_at'].replace('Z','+00:00'))
            if issued_at.tzinfo is None:
                return False
            family = {}
            for prior in c.execute("""SELECT * FROM parent_report_snapshot
                    WHERE child_id=? AND source_kind='period_report'
                      AND substr(source_id,1,length(?)+1)=? || '@' ORDER BY rowid""",
                    (review['child_id'],root,root)):
                if prior['snapshot_id']==review['snapshot_id']:
                    break
                family[prior['snapshot_id']] = prior
            else:
                return False
            content_source_id = root+'@'+content_version
            origin = next((row for row in family.values() if row['source_id']==content_source_id),None)
            if not origin or origin['source_version']!=source+':'+content_version:
                return False
            columns = ('facts_json','inferences_json','content_json','confidence_json')
            expected = {key:json.loads(review[key]) for key in columns}
            def same_content(row):
                return (row['report_type']==review['report_type']
                    and row['template_version']==review['template_version']
                    and row['evidence_version']==review['evidence_version']
                    and row['source_version'].rsplit(':',1)[0]==source
                    and row['source_version'].rsplit(':',1)[-1]==row['source_id'].rpartition('@')[2]
                    and ParentReportService.snapshot_problem(row) is None
                    and all(json.loads(row[key])==value for key,value in expected.items()))
            if not same_content(origin):
                return False
            # Reproduce the issuer's active/explicit-withdrawal gates at issuance.
            # A later withdrawal of another row cannot rewrite this original identity.
            for row in family.values():
                if not same_content(row):
                    continue
                created = datetime.fromisoformat(row['created_at'].replace('Z','+00:00'))
                revoked = datetime.fromisoformat(row['revoked_at'].replace('Z','+00:00'))
                if (row['status']!='revoked' or created.tzinfo is None or revoked.tzinfo is None
                        or not created<=revoked<=issued_at):
                    return False
                for event in c.execute("""SELECT occurred_at FROM audit_event
                        WHERE child_id=? AND actor='parent' AND action='parent_report_snapshot_revoked'
                          AND json_valid(summary_json) AND json_extract(summary_json,'$.snapshot_id')=?""",
                        (review['child_id'],row['snapshot_id'])):
                    withdrawn = datetime.fromisoformat(event['occurred_at'].replace('Z','+00:00'))
                    if withdrawn.tzinfo is None or withdrawn<=issued_at:
                        return False
            expected_issuance = ParentReportService._period_reissue_issuance(c,family,
                origin['snapshot_id'],content_source_id,review['child_id'],review['report_type'],
                not_after=issued_at)
            return expected_issuance==issuance
        except (ValueError,TypeError,KeyError,AttributeError):
            return False

    def _period_reissue_identity(self, c, existing, source_id, source_version,
                                 child_id, report_type, facts, inferences, content, confidence,
                                 evidence_version):
        """Issue a new draft when a system-superseded content version returns.

        Runs only after the original live-source checks, inside BEGIN IMMEDIATE.
        A content fingerprint is not permission to resurrect a revoked receipt.
        """
        root = source_id.rpartition('@')[0]
        family = {row['snapshot_id']:row for row in c.execute("""SELECT * FROM parent_report_snapshot
            WHERE child_id=? AND source_kind='period_report'
              AND substr(source_id,1,length(?)+1)=? || '@' ORDER BY rowid""", (child_id, root, root))}
        expected = {'facts_json':facts, 'inferences_json':inferences,
                    'content_json':content, 'confidence_json':confidence}
        def same_current_content(row):
            try:
                return (row['report_type']==report_type and row['template_version']==SNAPSHOT_TEMPLATE_VERSION
                    and row['evidence_version']==evidence_version
                    and row['source_version'].rsplit(':',1)[0]==source_version.rsplit(':',1)[0]
                    and row['source_version'].rsplit(':',1)[-1]==row['source_id'].rpartition('@')[2]
                    and all(json.loads(row[key])==value for key,value in expected.items())
                    and self.snapshot_problem(row) is None and self.snapshot_source_current(c,row))
            except (ValueError,TypeError,KeyError):
                return False
        matching = [row for row in family.values() if same_current_content(row)]
        active = [row for row in matching if row['status']!='revoked']
        if len(active)>1:
            raise SnapshotStateError('当前阶段报告版本关系需要核对；未创建或批准报告。')
        if active:
            row = active[0]
            return row['source_id'], row['source_version'], row
        # A parent's explicit withdrawal is never a system reissue permission,
        # including a prior reissued instance of this same current content.
        for row in matching:
            if c.execute("""SELECT 1 FROM audit_event WHERE child_id=? AND actor='parent'
                    AND action='parent_report_snapshot_revoked' AND json_valid(summary_json)
                    AND json_extract(summary_json,'$.snapshot_id')=? LIMIT 1""",
                    (child_id,row['snapshot_id'])).fetchone():
                return source_id, source_version, existing
        if existing['snapshot_id'] not in family or not same_current_content(existing):
            return source_id, source_version, existing
        issuance = self._period_reissue_issuance(c,family,existing['snapshot_id'],
            source_id,child_id,report_type)
        if issuance is None:
            return source_id, source_version, existing
        new_source = root+'@'+issuance
        if c.execute('SELECT 1 FROM parent_report_snapshot WHERE source_kind=? AND source_id=?',
                     ('period_report',new_source)).fetchone():
            raise SnapshotStateError('阶段报告签发版本已变化，请重新核对；原稿均保留。')
        return new_source, source_version.rsplit(':',1)[0]+':'+issuance, None

    def _insert_snapshot(self, *, source_kind: str, source_id: str, report_id: str | None, child_id: str, report_type: str, source_version: str, facts: dict, inferences: dict, content: dict, confidence: dict, supersede_daily: bool = False, supersede_period: str | None = None) -> dict:
        now = utc_now()
        version_payload = json.dumps({"facts": facts, "inferences": inferences}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        evidence_version = "sha256:" + hashlib.sha256(version_payload.encode("utf-8")).hexdigest()
        with (report_source_connection(self.database) if source_kind == 'period_report'
              else self.database.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            source_root = report_id or source_id.rpartition('@')[0]
            period_owner = connection.execute('SELECT child_id FROM period_report WHERE period_report_id=?',
                (source_root,)).fetchone() if source_kind == 'period_report' else None
            if period_owner and period_owner['child_id'] != child_id:
                raise SnapshotStateError('报告快照不属于当前孩子。')
            # Legacy source-less copies remain readable, but are born revoked.
            # This cannot qualify stale content for an existing source.
            historical_only = (source_kind == 'period_report' and period_owner is None
                               and facts.get('source_qualification_fingerprint') is None)
            if not historical_only and (not facts.get('source_qualification_fingerprint')
                    or facts['source_qualification_fingerprint'] != self.source_qualification_fingerprint(connection, child_id, source_kind, source_root)):
                if source_kind == 'daily_report':
                    raise DailySnapshotSourceChangedError('日报证据已更新，请重新读取后生成快照。')
                raise SnapshotStateError('报告来源在冻结期间已变更，请重新读取。')
            if source_kind == 'period_report' and not historical_only:
                from .period_reports import PeriodReportService
                from .period_written_observations import observations_for_period
                period = connection.execute('SELECT period_start,period_end,content_json,source,report_type FROM period_report WHERE period_report_id=? AND child_id=?',
                                            (source_root, child_id)).fetchone()
                if not period:
                    raise SnapshotStateError('阶段报告来源已不存在，请重新读取。')
                if (json.loads(period['content_json']) != content.get('summary')
                        or period['period_end'] != content.get('report_date')
                        or period['report_type'] != report_type
                        or source_version.rsplit(':', 1)[0] != period['source']):
                    raise SnapshotStateError('阶段报告内容已更新，请重新读取后生成快照。')
                from .period_behavior_source import period_behavior_fingerprint
                if facts.get('behavior_source_fingerprint') != period_behavior_fingerprint(
                        connection, child_id, period['period_start'], period['period_end']):
                    raise SnapshotStateError('行为依据已变化，请重新读取阶段报告后生成快照。')
                current_observations = observations_for_period(connection, child_id,
                    period['period_start'], period['period_end'],
                    PeriodReportService._effective_evidence(connection, child_id),
                    now=datetime.fromisoformat(utc_now()))
                # A fresh fingerprint must never qualify an older caller's text.
                if (facts.get('learning_summary') or {}).get('qualified_observations', []) != current_observations:
                    raise SnapshotStateError('原作答的评分依据已更新，请重新读取阶段报告后生成快照。')
            existing = connection.execute("SELECT * FROM parent_report_snapshot WHERE source_kind = ? AND source_id = ?", (source_kind, source_id)).fetchone()
            if existing and existing["child_id"] != child_id:
                raise SnapshotStateError("报告快照不属于当前孩子。")
            if source_kind=='period_report' and not historical_only and existing and existing['status']=='revoked':
                source_id, source_version, existing = self._period_reissue_identity(
                    connection,existing,source_id,source_version,child_id,report_type,
                    facts,inferences,content,confidence,evidence_version)
            if supersede_daily and not existing:
                live = connection.execute("SELECT child_id,session_id,evidence_json,summary_json,parent_coaching_json FROM daily_report WHERE report_id=?", (report_id,)).fetchone()
                if (not live or live["child_id"] != child_id
                    or json.loads(live["evidence_json"]) != facts["evidence_manifest"]
                    or json.loads(live["summary_json"]) != content["summary"]
                    or json.loads(live["parent_coaching_json"]) != content["parent_coaching"]):
                    raise DailySnapshotSourceChangedError("日报内容已更新，请重新读取后生成快照。")
                fingerprint = facts["evidence_manifest"].get("reliable_learning_fingerprint")
                if fingerprint is not None:
                    from .learning_feedback import report_dependencies_match, session_report_feedback

                    if (session_report_feedback(connection, live["session_id"])["evidence_fingerprint"] != fingerprint
                        or not report_dependencies_match(connection, child_id, facts["evidence_manifest"].get("reliable_learning_dependencies"))):
                        raise DailySnapshotSourceChangedError("日报证据已更新，请重新读取后生成快照。")
            if not existing:
                connection.execute("""
                    INSERT INTO parent_report_snapshot(
                      snapshot_id, source_kind, source_id, report_id, child_id, report_type, source_version, template_version,
                      evidence_version, facts_json, inferences_json, content_json, confidence_json,
                      status, created_at, revoked_at, revoke_reason
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    str(uuid4()), source_kind, source_id, report_id, child_id, report_type, source_version, SNAPSHOT_TEMPLATE_VERSION,
                    evidence_version, json.dumps(facts, ensure_ascii=False), json.dumps(inferences, ensure_ascii=False),
                    json.dumps(content, ensure_ascii=False), json.dumps(confidence, ensure_ascii=False),
                    'revoked' if historical_only else 'draft', now,
                    now if historical_only else None,
                    '历史报告来源已不存在，仅保留查阅，不得批准或发送。' if historical_only else None,
                ))
                connection.execute("""
                    INSERT INTO audit_event(audit_id, actor, action, child_id, summary_json, occurred_at)
                    VALUES (?, 'system', 'parent_report_snapshot_created', ?, ?, ?)
                """, (str(uuid4()), child_id, json.dumps({"report_id": report_id, "evidence_version": evidence_version}, ensure_ascii=False), now))
                snapshot_id = connection.execute("SELECT snapshot_id FROM parent_report_snapshot WHERE source_kind = ? AND source_id = ?", (source_kind, source_id)).fetchone()["snapshot_id"]
            else:
                snapshot_id = existing["snapshot_id"]
            if supersede_daily and (not existing or existing["status"] != "revoked"):
                self._supersede_daily_drafts(connection, child_id, report_id, snapshot_id, now)
            if supersede_period and not historical_only and (not existing or existing["status"] != "revoked"):
                self._supersede_period_drafts(connection, child_id, supersede_period, snapshot_id)
            connection.commit()
        return self.get(snapshot_id)

    def _confidence(self, report: dict) -> dict:
        count = int((report.get("summary") or {}).get("evidence_count") or 0)
        return {
            "overall": "high" if count >= 4 else "limited",
            "evidence_count": count,
            "rule": "confirmed_learning_records_only",
            "uncertainty": "未记录的信息不代表没有发生；趋势需在多天记录后才可解释。",
        }

    @staticmethod
    def period_learning_sources(connection, child_id, start, end, *, effective=None, observations=None,
                                coaching_experiences=None, goal_progress=None):
        """Source-only proof, excluding the aggregate and its approval snapshots."""
        if report_read_connection(connection) and not connection.in_transaction:
            connection.execute('BEGIN')
            try:
                return ParentReportService.period_learning_sources(
                    connection, child_id, start, end, effective=effective, observations=observations,
                    coaching_experiences=coaching_experiences, goal_progress=goal_progress)
            finally:
                connection.rollback()
        return ParentReportService._period_learning_sources(connection, child_id, start, end,
            effective=effective, observations=observations, coaching_experiences=coaching_experiences,
            goal_progress=goal_progress)

    @staticmethod
    def _period_source_fingerprints(connection, child_id, start, end):
        if report_read_connection(connection) and not connection.in_transaction:
            connection.execute('BEGIN')
            try:
                return ParentReportService._period_source_fingerprints(connection, child_id, start, end)
            finally:
                connection.rollback()
        from .daily_behavior_source import _with_behavior_source_batch

        def consume(daily, period):
            behavior = period(connection, child_id, str(start), str(end))
            learning = ParentReportService._period_learning_sources(
                connection, child_id, start, end, daily_reader=daily)
            return behavior, learning

        # Pin the complete period read before its behavior batch records an
        # owned epoch. Nested curriculum/source reads share this original
        # marker; they must not look like a caller's transaction replacement.
        from .report_source_reads import report_read_epoch
        if report_read_epoch(connection) is not None:
            from .original_connection_read import registered_view
            if registered_view(connection):
                from .standard_relocation_provenance import business_snapshot
                with business_snapshot(connection):
                    return _with_behavior_source_batch(connection, child_id, consume)
        return _with_behavior_source_batch(connection, child_id, consume)

    @staticmethod
    def _period_learning_sources(connection, child_id, start, end, *, effective=None, observations=None,
                                 coaching_experiences=None, goal_progress=None, daily_reader=None):
        from .period_reports import PeriodReportService
        from .period_written_observations import observations_for_period
        from .family_clock import EVIDENCE_CALENDAR_VERSION, FAMILY_ZONE

        start, end = str(start), str(end)
        if effective is None:
            effective = PeriodReportService._effective_evidence(connection, child_id)
        if observations is None:
            observations = observations_for_period(connection, child_id, start, end, effective,
                now=datetime.fromisoformat(utc_now()))
        effective = {key: item for key, item in effective.items()
            if start <= PeriodReportService._evidence_date(item['occurred_at']).isoformat() <= end}
        from .report_followup import ReportFollowupService
        if coaching_experiences is None:
            coaching_experiences = ReportFollowupService.coaching_experiences(connection, child_id)
        if goal_progress is None:
            goal_progress = PeriodReportService._goal_progress(connection, child_id, start, end)
        child = connection.execute('SELECT grade FROM child_profile WHERE child_id=?', (child_id,)).fetchone()
        payload = {
            'version': 'period_learning_sources_v3', 'child_id': child_id,
            'period_start': start, 'period_end': end,
            'evidence_calendar': {'version': EVIDENCE_CALENDAR_VERSION, 'timezone': FAMILY_ZONE.key},
            'effective_learning': effective, 'qualified_written_observations': observations,
            'evidence_action_versions': PeriodReportService._evidence_action_versions(connection, child_id, start, end),
            'coaching_experiences': coaching_experiences, 'goal_progress': goal_progress,
            'child_grade': child['grade'] if child else None,
        }
        # Pure diagnostic periods have no daily source to witness current labels.
        node_ids = sorted({item['knowledge_node_id'] for item in effective.values()})
        payload['knowledge_nodes'] = []
        for offset in range(0, len(node_ids), 500):
            batch = node_ids[offset:offset + 500]
            payload['knowledge_nodes'].extend(dict(row) for row in connection.execute(
                'SELECT knowledge_node_id,title,subject,track,grade_band,knowledge_version,source_ref '
                'FROM knowledge_node WHERE knowledge_node_id IN (' + ','.join('?' for _ in batch) + ') ORDER BY knowledge_node_id', batch))
        mistake_ids = {item.get('mistake_id') for item in effective.values() if item.get('mistake_id')}
        payload['mistake_state'] = [dict(item) for item in connection.execute(
            'SELECT mistake_id,source_evidence_id,knowledge_node_id,status,created_at FROM mistake_case WHERE child_id=? ORDER BY mistake_id', (child_id,))
            if item['source_evidence_id'] in effective or item['mistake_id'] in mistake_ids
            or start <= PeriodReportService._evidence_date(item['created_at']).isoformat() <= end]
        payload['daily_sources'] = [{key: item[key] for key in ('report_id','evidence_json','summary_json','parent_coaching_json')}
            for item in connection.execute('SELECT * FROM daily_report WHERE child_id=? AND report_date BETWEEN ? AND ? ORDER BY report_id',
                (child_id, start, end))]
        def qualify(behavior_reader):
            return [{**item, 'qualification': ParentReportService._daily_source_qualification(
                connection, child_id, item['report_id'], behavior_reader)} for item in payload['daily_sources']]
        from .daily_behavior_source import _with_daily_behavior_batch, daily_behavior_fingerprint
        payload['daily_sources'] = (qualify(daily_reader) if daily_reader is not None else
            _with_daily_behavior_batch(connection, child_id, qualify) if len(payload['daily_sources']) > 1
            else qualify(daily_behavior_fingerprint))
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    @staticmethod
    def _daily_source_qualification(connection, child_id, source_id, behavior_reader):
        """Every daily source keeps its own non-behavior and session checks."""
        from .learning_feedback import report_dependencies_match, session_report_feedback
        if ParentReportService.daily_source_problem(connection, child_id, source_id):
            return None
        row = connection.execute('SELECT * FROM daily_report WHERE report_id=? AND child_id=?', (source_id, child_id)).fetchone()
        if not row:
            return None
        evidence = json.loads(row['evidence_json'])
        from .parent_supply_followup import report_supply_matches
        if not report_supply_matches(connection, child_id, row['session_id'], json.loads(row['summary_json'])):
            return None
        reliable = session_report_feedback(connection, row['session_id'])
        current = reliable['evidence_fingerprint']
        from .repository import StudyRepository
        from .agent_runtime import _RuntimeTransactionDatabase
        from .specialized_program import report_reference_fingerprint
        repository = StudyRepository(_RuntimeTransactionDatabase(connection))
        mistake = repository._report_mistake_projection(connection, row['session_id'], reliable)['fingerprint']
        specialized = report_reference_fingerprint(connection, child_id)
        behavior_fingerprint = behavior_reader(connection, child_id, row['session_id'])
        from .document_revisit_memory import report_memory as revisit_memory, EMPTY_FINGERPRINT
        revisit_fingerprint = revisit_memory(connection, child_id, row['session_id'])['fingerprint']
        if (evidence.get('reliable_learning_fingerprint') != current
                or evidence.get('document_revisit_practice_fingerprint', EMPTY_FINGERPRINT) != revisit_fingerprint
                or evidence.get('behavior_source_fingerprint') != behavior_fingerprint
                or evidence.get('mistake_mastery_fingerprint') != mistake
                or evidence.get('specialized_reference_fingerprint') != specialized
                or not report_dependencies_match(connection, child_id, evidence.get('reliable_learning_dependencies'))):
            return None
        payload = {key: row[key] for key in ('report_id','child_id','source','report_date','evidence_json','summary_json','parent_coaching_json')}
        payload['reliable_learning_fingerprint'] = current
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    @staticmethod
    def source_qualification_fingerprint(connection, child_id, source_kind, source_id):
        """Read-only source qualification for borrowed planner transactions."""
        if source_kind == 'daily_report':
            from .daily_behavior_source import daily_behavior_fingerprint
            return ParentReportService._daily_source_qualification(connection, child_id, source_id, daily_behavior_fingerprint)
        if source_kind == 'period_report':
            from .period_reports import PeriodReportService
            from .family_clock import EVIDENCE_CALENDAR_VERSION, FAMILY_ZONE
            row = connection.execute('SELECT * FROM period_report WHERE period_report_id=? AND child_id=?', (source_id, child_id)).fetchone()
            if ParentReportService.period_row_problem(row):
                return None
            current_behavior, learning_sources = ParentReportService._period_source_fingerprints(
                connection, child_id, row['period_start'], row['period_end'])
            if json.loads(row['content_json']).get('behavior_source_fingerprint') != current_behavior:
                return None
            history = json.loads(row['content_json']).get('supply_history')
            if history is not None:
                from .parent_supply_followup import period_supply_facts
                try:
                    if history != period_supply_facts(connection, child_id,
                            row['period_start'], row['period_end'], history['as_of']):
                        return None
                except (ValueError, KeyError, TypeError):
                    return None
            if json.loads(row['content_json']).get('learning_source_fingerprint') != learning_sources:
                return None
            payload = {key: row[key] for key in ('period_report_id','child_id','report_type','period_start','period_end','source','content_json')}
            payload['evidence_calendar'] = {'version': EVIDENCE_CALENDAR_VERSION, 'timezone': FAMILY_ZONE.key}
            payload['behavior_sources'] = current_behavior
            payload['learning_sources'] = learning_sources
        else:
            return None
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    @staticmethod
    def snapshot_source_current(connection, snapshot):
        if ParentReportService.snapshot_problem(snapshot):
            return False
        expected = json.loads(snapshot['facts_json']).get('source_qualification_fingerprint')
        root = snapshot['report_id'] or snapshot['source_id'].rpartition('@')[0]
        return bool(expected) and expected == ParentReportService.source_qualification_fingerprint(
            connection, snapshot['child_id'], snapshot['source_kind'], root)

    def validate_current_for_delivery(self, snapshot_id: str) -> bool:
        """Revalidate live sources without rewriting approved content."""
        from .repository import ReportNotReadyError, StudyRepository, UnknownReportError
        from .period_reports import PeriodReportService, PeriodReportSourceError, UnknownPeriodReportError

        snapshot = self.get(snapshot_id)
        if snapshot["status"] != "approved":
            return False
        if snapshot["source_kind"] == "period_report":
            try:
                source = self._current_period_report(snapshot["source_id"].rpartition("@")[0])
                current = self.create_from_period(source)
            except (UnknownPeriodReportError, PeriodReportSourceError, SnapshotStateError):
                return False
            return current["snapshot_id"] == snapshot_id and self._approved_source_current(snapshot_id)
        if snapshot["source_kind"] != "daily_report":
            return False
        report_id = snapshot["report_id"] or snapshot["source_id"].partition("@")[0]
        try:
            report = StudyRepository(self.database).get_daily_report(report_id)
            current = self.create_from_daily(report)
        except (ReportNotReadyError, UnknownReportError, SnapshotStateError):
            # An unavailable or concurrently changing source cannot authorize a send.
            return False
        return current["snapshot_id"] == snapshot_id and self._approved_source_current(snapshot_id)

    def _approved_source_current(self, snapshot_id):
        with self.database.connect() as connection:
            connection.execute('BEGIN')
            row = connection.execute('SELECT * FROM parent_report_snapshot WHERE snapshot_id=?', (snapshot_id,)).fetchone()
            return bool(row and row['status']=='approved' and self.snapshot_source_current(connection, row))

    @_guard_report_change
    def refresh_sources(self, child_id: str, *, source_kind: str | None = None) -> list[dict]:
        """Local correction reconciliation; never approves or submits a replacement."""
        from .repository import ReportNotReadyError, UnknownReportError, UnknownSessionError
        from .period_reports import PeriodReportService, PeriodReportSourceError, UnknownPeriodReportError

        with self.database.connect() as connection:
            rows = connection.execute("SELECT * FROM parent_report_snapshot WHERE child_id=? ORDER BY created_at", (child_id,)).fetchall()
        results = []
        seen = set()
        for row in rows:
            if source_kind is not None and row['source_kind'] != source_kind:
                continue
            source_id = row['report_id'] or row['source_id'].rpartition('@')[0] or row['source_id']
            identity = (row['source_kind'], source_id)
            if identity in seen:
                continue
            seen.add(identity)
            if row['source_kind'] == 'daily_report':
                with self.database.connect() as connection:
                    problem = self.daily_source_problem(connection, child_id, source_id)
                if problem:
                    self.record_source_issue(child_id, row['source_kind'], source_id, problem)
                    results.append({'source_kind': row['source_kind'], 'source_id': source_id,
                        'status': 'blocked_source_unavailable', 'automatic_delivery': False})
                    continue
            try:
                if row['source_kind'] == 'daily_report':
                    current = self._refresh_daily_snapshot(source_id, child_id=child_id)
                else:
                    current = self.create_from_period(self._current_period_report(source_id))
                results.append({'source_kind': row['source_kind'], 'source_id': source_id,
                    'snapshot_id': current['snapshot_id'], 'status': current['status'], 'automatic_delivery': False})
            except UnreadableReportSnapshotError:
                results.append({'source_kind': row['source_kind'], 'source_id': source_id,
                    'status': 'blocked_snapshot_unreadable', 'automatic_delivery': False})
            except PeriodReportSourceError:
                results.append({'source_kind': row['source_kind'], 'source_id': source_id,
                    'status': 'blocked_source_unavailable', 'automatic_delivery': False})
            except (ReportNotReadyError, UnknownReportError, UnknownSessionError, UnknownPeriodReportError, SnapshotStateError, json.JSONDecodeError) as error:
                self.record_source_issue(child_id, row['source_kind'], source_id,
                    'REPORT_SOURCE_INVALID_JSON' if isinstance(error, json.JSONDecodeError) else 'REPORT_SOURCE_NOT_READY')
                results.append({'source_kind': row['source_kind'], 'source_id': source_id,
                    'status': 'blocked_source_unavailable', 'automatic_delivery': False})
        return results

    def correction_status(self, child_id: str, snapshot_id: str, *, connection=None) -> dict:
        with nullcontext(connection) if connection is not None else self.database.connect() as c:
            if not c.in_transaction:
                c.execute('BEGIN')
            snapshot = c.execute('SELECT * FROM parent_report_snapshot WHERE snapshot_id=? AND child_id=?', (snapshot_id, child_id)).fetchone()
            if not snapshot:
                raise UnknownReportSnapshotError(snapshot_id)
            source_current = self.snapshot_source_current(c, snapshot)
            def identity(row):
                return row['source_kind'], row['report_id'] or row['source_id'].rpartition('@')[0] or row['source_id']
            owned = {row['snapshot_id']:row for row in c.execute(
                'SELECT rowid AS creation_sequence,* FROM parent_report_snapshot WHERE child_id=? ORDER BY rowid',(child_id,))}
            family = {key:row for key,row in owned.items() if identity(row)==identity(snapshot)}
            events = c.execute("""SELECT audit_id,summary_json,occurred_at FROM audit_event WHERE child_id=?
                AND action IN ('parent_daily_snapshot_superseded','parent_period_snapshot_superseded') ORDER BY rowid""", (child_id,)).fetchall()
            candidates = []
            for event in events:
                try:
                    payload = json.loads(event['summary_json'])
                except (ValueError,TypeError):
                    continue
                if not isinstance(payload,dict):
                    continue
                parent, replacement = payload.get('snapshot_id'),payload.get('replacement_snapshot_id')
                if (not isinstance(parent,str) or not isinstance(replacement,str) or parent==replacement
                        or any(key in owned and key not in family for key in (parent,replacement))):
                    continue
                if any(key not in owned and c.execute('SELECT 1 FROM parent_report_snapshot WHERE snapshot_id=?',(key,)).fetchone()
                       for key in (parent,replacement)):
                    continue
                candidates.append({'snapshot_id':parent,'replacement_snapshot_id':replacement,
                    'event_id':event['audit_id'],'occurred_at':event['occurred_at']})
            # Traverse both directions: the latest detail carries the same old
            # recipient obligations. Missing intermediates are not new evidence.
            reachable = set(family)
            while True:
                expanded = reachable | {key for link in candidates
                    if link['snapshot_id'] in reachable or link['replacement_snapshot_id'] in reachable
                    for key in (link['snapshot_id'],link['replacement_snapshot_id'])}
                if expanded==reachable:
                    break
                reachable = expanded
            edges = [link for link in candidates if link['snapshot_id'] in reachable and link['replacement_snapshot_id'] in reachable]
            terminal = {link['replacement_snapshot_id'] for link in edges} - {link['snapshot_id'] for link in edges}
            latest_id = next(reversed(family)) if len(family)>1 else None
            availability = 'available' if latest_id else 'none'
            if edges:
                if not terminal:
                    latest_id,availability = None,'ambiguous'
                else:
                    last = next(link['replacement_snapshot_id'] for link in reversed(edges) if link['replacement_snapshot_id'] in terminal)
                    if last not in family:
                        latest_id,availability = None,'deleted'
                        newest_id = next(reversed(family))
                        newest = family[newest_id]
                        linked_ids = {key for link in edges for key in (link['snapshot_id'],link['replacement_snapshot_id'])}
                        # A new independently frozen current draft may follow
                        # deletion. Never substitute an older linked receipt.
                        if (newest_id not in linked_ids and newest['status']!='revoked'
                                and self.snapshot_source_current(c,newest)):
                            latest_id,availability = newest_id,'available'
                    else:
                        # Same-source snapshots remain ordered even when a
                        # predecessor was explicitly revoked before a new draft.
                        latest_id,availability = next(reversed(family)),'available'
            links = [link for link in edges if link['replacement_snapshot_id'] in family]
            latest_eligible = bool(latest_id and family[latest_id]['status']=='approved'
                and self.snapshot_source_current(c,family[latest_id]))
            deliveries, replacement_deliveries = [], []
            if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='report_delivery_outbox'").fetchone():
                for prior_id in family:
                    if prior_id != latest_id or not latest_eligible:
                        deliveries.extend(dict(row) for row in c.execute("""SELECT delivery_id,snapshot_id,status,recipient_id FROM report_delivery_outbox
                            WHERE snapshot_id=? AND status IN ('sent','processing','unknown','unknown_acknowledged','superseded') ORDER BY rowid""",(prior_id,)))
                origin_id = next(iter(family))
                for replacement_id,replacement in family.items():
                    if (replacement_id==origin_id and replacement_id!=latest_id
                            and not any(link['replacement_snapshot_id']==replacement_id for link in edges)):
                        continue
                    is_latest = replacement_id==latest_id
                    current = is_latest and latest_eligible
                    for row in c.execute('''SELECT delivery_id,snapshot_id,recipient_id,status,sent_at,provider_message_id
                            FROM report_delivery_outbox WHERE snapshot_id=? ORDER BY rowid''',(replacement_id,)):
                        replacement_deliveries.append({**dict(row), 'evidence_version':replacement['evidence_version'],
                            'source_version':replacement['source_version'], 'source_current':bool(current),
                            'is_latest_replacement':is_latest,
                            'channel_acceptance':'accepted' if row['status']=='sent' else (
                                'unknown' if row['status'] in {'processing','unknown','unknown_acknowledged','superseded'} else 'not_confirmed'),
                            'human_delivery_verified':False, 'human_read_verified':False})
            affected = {row['recipient_id'] for row in deliveries}
            accepted = {row['recipient_id'] for row in replacement_deliveries if row['status']=='sent' and row['source_current']}
            outstanding = sorted(affected - accepted)
            needs_correction = snapshot['status']=='revoked' or not source_current or bool((latest_id or edges) and deliveries)
            consent = {row['recipient_id']:row['consent_status'] for row in c.execute(
                'SELECT recipient_id,consent_status FROM report_delivery_recipient WHERE child_id=?',(child_id,))} if deliveries else {}
        # The current family's newest stored draft can itself be stale before
        # the next reconciliation. It is not its own usable replacement. Keep
        # the historical delivery/recipient calculations above unchanged.
        if not source_current and latest_id == snapshot_id:
            latest_id, availability = None, 'none'
        return {'snapshot_id': snapshot_id, 'child_id': child_id, 'status': snapshot['status'],
            'source_current': source_current,
            'latest_replacement_id':latest_id,
            'latest_replacement_availability':availability,
            'latest_replacement_status':family[latest_id]['status'] if latest_id else None,
            'replacements': links, 'prior_submissions': deliveries,
            'replacement_submissions':replacement_deliveries,
            'recipient_ids_requiring_correction':outstanding if needs_correction else [],
            'recipient_ids_blocked_by_consent':[key for key in outstanding if consent.get(key)!='consented'] if needs_correction else [],
            'correction_notification_required': bool(outstanding) and needs_correction,
            'next_action': ('verify_correction_receipt' if affected and not outstanding and needs_correction
                else ('review_replacement_and_choose_notification' if latest_id else 'recheck_source')),
            'automatic_delivery': False, 'human_delivery_verified': False,
            'local_only': True, 'cloud_transmission_allowed': False}

    @_guard_report_change
    @scoped_validation
    def get(self, snapshot_id: str) -> dict:
        # Direct historical reads must also revoke a structurally missing source.
        with report_source_connection(self.database) as c:
            source = c.execute('SELECT * FROM parent_report_snapshot WHERE snapshot_id=?', (snapshot_id,)).fetchone()
            problem = None
            if source:
                checker = self.daily_source_problem if source['source_kind'] == 'daily_report' else self.period_source_problem
                problem = checker(c, source['child_id'], self._source_root(source))
        if source and self.snapshot_problem(source):
            self.record_source_issue(source['child_id'], 'parent_report_snapshot', snapshot_id, 'REPORT_SNAPSHOT_INVALID_JSON')
            raise UnreadableReportSnapshotError(SOURCE_ISSUE_MESSAGES['REPORT_SNAPSHOT_INVALID_JSON'])
        if problem:
            self.record_source_issue(source['child_id'], source['source_kind'], self._source_root(source), problem)
        with report_source_connection(self.database) as connection:
            connection.execute('BEGIN')
            row = connection.execute("SELECT * FROM parent_report_snapshot WHERE snapshot_id = ?", (snapshot_id,)).fetchone()
            from .report_followup import ReportFollowupService
            feedback_state = ReportFollowupService.current_feedback(connection, row) if row else {}
            feedback = connection.execute("""
                SELECT helpfulness, stress_rating, notes, created_at FROM parent_report_snapshot_feedback
                WHERE snapshot_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 5
            """, (snapshot_id,)).fetchall()
            corrections = connection.execute("""
                SELECT note, created_at FROM parent_report_snapshot_correction
                WHERE snapshot_id = ? ORDER BY created_at DESC LIMIT 5
            """, (snapshot_id,)).fetchall()
            correction_state = self.correction_status(row['child_id'],snapshot_id,connection=connection) if row else None
            from .parent_action_evidence import ParentActionEvidenceService
            actions = ParentActionEvidenceService(self.database)
            parent_action = actions.current_for_review(connection, row['child_id'], snapshot_id) if row else None
            historical_parent_actions = actions.list_for_review(connection, row['child_id'], snapshot_id) if row else []
        if not row:
            raise UnknownReportSnapshotError(snapshot_id)
        result = dict(row)
        for key in ("facts_json", "inferences_json", "content_json", "confidence_json", "parent_edit_json"):
            result[key.removesuffix("_json")] = json.loads(result.pop(key))
        result["parent_edit_version"] = self.parent_edit_version(row["parent_edit_json"])
        result["parent_edit"] = {
            "delivery_intro": "", "parent_note": "", "coaching_decision": "adopt",
            "coaching_script": "", "coaching_action": "", "coaching_duration_minutes": None,
            "coaching_observation": "",
            **(result["parent_edit"] if isinstance(result["parent_edit"], dict) else {}),
        }
        result["feedback"] = [dict(item) for item in feedback]
        result.update(feedback_state)
        result["corrections"] = [dict(item) for item in corrections]
        result['correction_status'] = correction_state
        result['parent_action'] = parent_action
        result['historical_parent_actions'] = historical_parent_actions
        return result

    def list(self, child_id: str, limit: int = 30) -> list[dict]:
        bounded_limit = max(1, min(limit, 100))
        self.reconcile_daily_sources(child_id, bounded_limit)
        # The existing parent list already reconciles local daily drafts. Include
        # historical periods, which the current-week/month maintenance skips.
        self.refresh_sources(child_id, source_kind='period_report')
        with self.database.connect() as connection:
            rows = connection.execute("SELECT snapshot_id FROM parent_report_snapshot WHERE child_id = ? ORDER BY created_at DESC, rowid DESC LIMIT ?", (child_id, bounded_limit)).fetchall()
        result = []
        for row in rows:
            try:
                result.append(self.get(row['snapshot_id']))
            except UnreadableReportSnapshotError:
                continue
        return result

    @_guard_report_change
    def edit_draft(
        self, snapshot_id: str, delivery_intro: str | None = None, parent_note: str | None = None,
        *, coaching_decision: str | None = None, coaching_script: str | None = None,
        coaching_action: str | None = None, coaching_duration_minutes: int | None = None,
        coaching_observation: str | None = None, parent_edit_patch: dict | None = None,
    ) -> dict:
        patch = dict(parent_edit_patch) if parent_edit_patch is not None else {
            "delivery_intro": delivery_intro, "parent_note": parent_note,
            "coaching_decision": coaching_decision, "coaching_script": coaching_script,
            "coaching_action": coaching_action, "coaching_duration_minutes": coaching_duration_minutes,
            "coaching_observation": coaching_observation,
        }
        if parent_edit_patch is None:
            patch = {key: value for key, value in patch.items() if value is not None}
        with self.database.connect() as connection:
            row = connection.execute("SELECT child_id, status, parent_edit_json FROM parent_report_snapshot WHERE snapshot_id = ?", (snapshot_id,)).fetchone()
            if not row:
                raise UnknownReportSnapshotError(snapshot_id)
            if row["status"] != "draft":
                raise SnapshotStateError("已批准的报告内容不可编辑；请撤销后重新生成。")
            current = self._normalized_parent_edit(row["parent_edit_json"])
            values = self._merge_parent_edit(current, patch)
            if values != current:
                connection.execute("UPDATE parent_report_snapshot SET parent_edit_json = ? WHERE snapshot_id = ?", (json.dumps(values, ensure_ascii=False), snapshot_id))
                connection.execute("INSERT INTO audit_event(audit_id, actor, action, child_id, summary_json, occurred_at) VALUES (?, 'parent', 'parent_report_snapshot_edited', ?, ?, ?)", (str(uuid4()), row["child_id"], json.dumps({"snapshot_id": snapshot_id, "coaching_decision": values["coaching_decision"]}, ensure_ascii=False), utc_now()))
            connection.commit()
        return self.get(snapshot_id)

    @classmethod
    def parent_edit_version(cls, raw) -> str:
        return hashlib.sha256(json.dumps(cls._normalized_parent_edit(raw), ensure_ascii=False,
            sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()

    @_guard_report_change
    def carry_saved_edits(self, target_id: str, source_id: str, source_edit_version: str,
                          target_edit_version: str, client_action_id: str) -> dict:
        """Explicitly carry only human edits; never approve or replace evidence."""
        request = {"source_snapshot_id": source_id, "target_snapshot_id": target_id,
            "source_edit_version": source_edit_version, "target_edit_version": target_edit_version,
            "client_action_id": client_action_id}
        conflict = "报告或已保存修改已变化；两份原稿均保留，请重新核对后再沿用。"
        with self.database.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            old = c.execute("SELECT * FROM parent_report_snapshot WHERE snapshot_id=?", (source_id,)).fetchone()
            target = c.execute("SELECT * FROM parent_report_snapshot WHERE snapshot_id=?", (target_id,)).fetchone()
            if old is None or target is None:
                raise UnknownReportSnapshotError(target_id)
            # A command ID is globally owned within this audit action, not reusable
            # for another child, source, target or edit pair.
            previous = c.execute("""SELECT child_id,actor,summary_json,occurred_at FROM audit_event
                WHERE action='parent_report_saved_edits_carried' AND json_valid(summary_json)
                AND json_extract(summary_json,'$.client_action_id')=? ORDER BY rowid LIMIT 2""",
                (client_action_id,)).fetchall()
            if previous:
                saved = json.loads(previous[0]['summary_json'])
                if (len(previous) != 1 or previous[0]['actor'] != 'parent'
                        or previous[0]['child_id'] != old['child_id']
                        or any(saved.get(key) != value for key, value in request.items())
                        or target['child_id'] != old['child_id']):
                    raise SavedEditCarryConflictError(conflict)
                receipt = {**request, 'saved_edit_version': saved['saved_edit_version'],
                    'saved_at': previous[0]['occurred_at'], 'replayed': True}
            else:
                if (source_id == target_id or self.snapshot_problem(old) or self.snapshot_problem(target)
                        or old['child_id'] != target['child_id']
                        or old['source_kind'] != target['source_kind']
                        or old['report_type'] != target['report_type']
                        or self._source_root(old) != self._source_root(target)
                        or old['status'] != 'revoked' or target['status'] != 'draft'
                        or self.snapshot_source_current(c, old)
                        or not self.snapshot_source_current(c, target)
                        or self.parent_edit_version(old['parent_edit_json']) != source_edit_version
                        or self.parent_edit_version(target['parent_edit_json']) != target_edit_version):
                    raise SavedEditCarryConflictError(conflict)
                old_content, target_content = json.loads(old['content_json']), json.loads(target['content_json'])
                if old_content.get('report_date') != target_content.get('report_date'):
                    raise SavedEditCarryConflictError(conflict)
                state = self.correction_status(old['child_id'], source_id, connection=c)
                if (state['latest_replacement_id'] != target_id
                        or state['latest_replacement_availability'] != 'available'
                        or c.execute("""SELECT 1 FROM audit_event WHERE child_id=? AND actor='parent'
                            AND action='parent_report_snapshot_revoked' AND json_valid(summary_json)
                            AND json_extract(summary_json,'$.snapshot_id')=? LIMIT 1""",
                            (old['child_id'], source_id)).fetchone()):
                    raise SavedEditCarryConflictError(conflict)
                # The UI's latest pointer alone is not a supersession witness.
                # Follow only original system audit edges within this same source
                # family, with replacement creation preceding the audit event.
                reachable = {source_id}
                for _ in range(len(state['replacements']) + 1):
                    before = len(reachable)
                    for link in state['replacements']:
                        if link['snapshot_id'] not in reachable:
                            continue
                        event = c.execute("""SELECT * FROM audit_event WHERE audit_id=? AND child_id=?
                            AND actor='system' AND action=?""", (link['event_id'], old['child_id'],
                            'parent_period_snapshot_superseded' if old['source_kind'] == 'period_report'
                            else 'parent_daily_snapshot_superseded')).fetchone()
                        replacement = c.execute('SELECT * FROM parent_report_snapshot WHERE snapshot_id=?',
                            (link['replacement_snapshot_id'],)).fetchone()
                        prior = c.execute('SELECT * FROM parent_report_snapshot WHERE snapshot_id=?',
                            (link['snapshot_id'],)).fetchone()
                        try:
                            proof = json.loads(event['summary_json']) if event is not None else None
                            times = [datetime.fromisoformat(value.replace('Z', '+00:00')) for value in
                                (prior['created_at'], replacement['created_at'], event['occurred_at'])]
                            valid = (proof == {'snapshot_id': link['snapshot_id'],
                                    'replacement_snapshot_id': link['replacement_snapshot_id']}
                                and all(instant.tzinfo is not None for instant in times)
                                and times[0] <= times[1] <= times[2]
                                and all(row['child_id'] == old['child_id'] and row['source_kind'] == old['source_kind']
                                    and row['report_type'] == old['report_type']
                                    and self._source_root(row) == self._source_root(old)
                                    for row in (prior, replacement)))
                        except (ValueError, TypeError, KeyError, AttributeError):
                            valid = False
                        if valid:
                            reachable.add(link['replacement_snapshot_id'])
                    if len(reachable) == before:
                        break
                if target_id not in reachable:
                    raise SavedEditCarryConflictError(conflict)
                # Both IDs refer to one immutable period identity, including its
                # original range; a different week/month cannot be substituted.
                if old['source_kind'] == 'period_report':
                    period = c.execute('SELECT child_id,report_type,period_end FROM period_report WHERE period_report_id=?',
                        (self._source_root(old),)).fetchone()
                    if (period is None or period['child_id'] != old['child_id']
                            or period['report_type'] != old['report_type']
                            or old_content.get('report_date') != period['period_end']):
                        raise SavedEditCarryConflictError(conflict)
                source_values = self._normalized_parent_edit(old['parent_edit_json'])
                # An untouched adopt draft legitimately has no custom duration.
                # Keep that default rather than treating its stored null as input.
                if source_values.get('coaching_duration_minutes') is None:
                    source_values = {key: value for key, value in source_values.items() if key != 'coaching_duration_minutes'}
                values = self._merge_parent_edit(self._normalized_parent_edit(None), source_values)
                version = self.parent_edit_version(values)
                now = utc_now()
                c.execute('UPDATE parent_report_snapshot SET parent_edit_json=? WHERE snapshot_id=?',
                    (json.dumps(values, ensure_ascii=False), target_id))
                c.execute("""INSERT INTO audit_event(audit_id,actor,action,child_id,summary_json,occurred_at)
                    VALUES (?,'parent','parent_report_saved_edits_carried',?,?,?)""",
                    (str(uuid4()), old['child_id'], json.dumps({**request, 'saved_edit_version': version},
                        ensure_ascii=False), now))
                receipt = {**request, 'saved_edit_version': version, 'saved_at': now, 'replayed': False}
            c.commit()
        result = self.get(target_id)
        result['edit_transfer'] = receipt
        return result

    @_guard_report_change
    def approve(self, snapshot_id: str, actor: str = "parent", parent_edit_patch: dict | None = None) -> dict:
        from .parent_action_evidence import ParentActionEvidenceService
        actions = ParentActionEvidenceService(self.database)
        actions.initialize()
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM parent_report_snapshot WHERE snapshot_id = ?", (snapshot_id,)).fetchone()
            if not row:
                raise UnknownReportSnapshotError(snapshot_id)
            if row["status"] == "revoked":
                raise SnapshotStateError("已撤销的报告不能批准。")
            if row["status"] == "draft":
                if actor == "system-verifier":
                    self._assert_system_qualifiable(connection, row)
                if parent_edit_patch is not None:
                    values = self._merge_parent_edit(self._normalized_parent_edit(row["parent_edit_json"]), parent_edit_patch)
                    connection.execute("UPDATE parent_report_snapshot SET parent_edit_json = ? WHERE snapshot_id = ?", (json.dumps(values, ensure_ascii=False), snapshot_id))
                connection.execute("UPDATE parent_report_snapshot SET status = 'approved', approved_at = ?, approved_by = ? WHERE snapshot_id = ?", (now, actor, snapshot_id))
                action = 'parent_report_snapshot_system_verified' if actor == 'system-verifier' else 'parent_report_snapshot_approved'
                connection.execute("INSERT INTO audit_event(audit_id, actor, action, child_id, summary_json, occurred_at) VALUES (?, ?, ?, ?, ?, ?)", (str(uuid4()), actor, action, row["child_id"], json.dumps({"snapshot_id": snapshot_id}, ensure_ascii=False), now))
                try:
                    actions.freeze_reviewed(connection, snapshot_id)
                except ValueError as error:
                    raise SnapshotStateError("报告行动的来源或审核依据已变化；请刷新并核对最新草稿后重新批准。") from error
            connection.commit()
        return self.get(snapshot_id)

    def approve_system_verified(self, snapshot_id: str) -> dict:
        """Approve only a current, structurally evidenced default report.

        This is not a model self-approval path. The frozen source fingerprint,
        deterministic confidence contract and unedited parent action must all
        still match inside the approval transaction.
        """
        return self.approve(snapshot_id, actor="system-verifier")

    @classmethod
    def _assert_system_qualifiable(cls, connection, row) -> None:
        if cls.snapshot_problem(row) or not cls.snapshot_source_current(connection, row):
            raise SnapshotStateError("报告来源或证据指纹已变化，系统不能自动批准。")
        facts = json.loads(row["facts_json"])
        inferences = json.loads(row["inferences_json"])
        content = json.loads(row["content_json"])
        confidence = json.loads(row["confidence_json"])
        parent_edit = cls._normalized_parent_edit(row["parent_edit_json"])
        if not isinstance(facts.get("source_qualification_fingerprint"), str) or not facts["source_qualification_fingerprint"]:
            raise SnapshotStateError("报告缺少可重算的来源指纹，系统不能自动批准。")
        if confidence.get("rule") != "confirmed_learning_records_only" or not isinstance(inferences, dict):
            raise SnapshotStateError("报告置信度契约不完整，系统不能自动批准。")
        if not isinstance(content.get("summary"), dict) or not isinstance(content.get("parent_coaching"), dict):
            raise SnapshotStateError("报告正文缺少结构化依据，系统不能自动批准。")
        defaults = cls._normalized_parent_edit(None)
        if parent_edit != defaults:
            raise SnapshotStateError("报告包含家长编辑，必须由家长确认后批准。")

    @staticmethod
    def _normalized_parent_edit(raw: str | dict | None) -> dict:
        try:
            current = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except (TypeError, json.JSONDecodeError):
            current = {}
        return {
            "delivery_intro": "", "parent_note": "", "coaching_decision": "adopt",
            "coaching_script": "", "coaching_action": "", "coaching_duration_minutes": None,
            "coaching_observation": "", **(current if isinstance(current, dict) else {}),
        }

    @staticmethod
    def _merge_parent_edit(current: dict, patch: dict) -> dict:
        allowed = {"delivery_intro", "parent_note", "coaching_decision", "coaching_script", "coaching_action", "coaching_duration_minutes", "coaching_observation"}
        if not set(patch).issubset(allowed):
            raise SnapshotStateError("报告采用方式包含不支持的字段。")
        values = dict(current)
        limits = {"delivery_intro": 500, "parent_note": 1000, "coaching_script": 1000, "coaching_action": 500, "coaching_observation": 500}
        for key, limit in limits.items():
            if key in patch:
                value = patch[key]
                if value is not None and not isinstance(value, str):
                    raise SnapshotStateError("报告修改内容必须是文字。")
                values[key] = (value or "").strip()[:limit]
        if "coaching_decision" in patch:
            decision = patch["coaching_decision"]
            if decision not in {"adopt", "custom", "skip"}:
                raise SnapshotStateError("请选择采用建议、修改后采用或本次跳过。")
            values["coaching_decision"] = decision
        if "coaching_duration_minutes" in patch:
            duration = patch["coaching_duration_minutes"]
            if not isinstance(duration, int) or isinstance(duration, bool) or not 0 <= duration <= 30:
                raise SnapshotStateError("修改后的行动时长需在0到30分钟之间。")
            values["coaching_duration_minutes"] = duration
        if values["coaching_decision"] == "skip":
            values["coaching_duration_minutes"] = 0
        return values

    @_guard_report_change
    def revoke(self, snapshot_id: str, reason: str) -> dict:
        with self.database.connect() as connection:
            row = connection.execute("SELECT child_id, status FROM parent_report_snapshot WHERE snapshot_id = ?", (snapshot_id,)).fetchone()
            if not row:
                raise UnknownReportSnapshotError(snapshot_id)
            if row['status'] == 'revoked' and not connection.execute('''SELECT 1 FROM audit_event
                    WHERE child_id=? AND actor='parent' AND action='parent_report_snapshot_revoked'
                    AND json_valid(summary_json) AND json_extract(summary_json,'$.snapshot_id')=?''',
                    (row['child_id'], snapshot_id)).fetchone():
                # An automatically superseded report can still be explicitly withdrawn.
                connection.execute("INSERT INTO audit_event(audit_id,actor,action,child_id,summary_json,occurred_at) VALUES (?,'parent','parent_report_snapshot_revoked',?,?,?)",
                    (str(uuid4()), row['child_id'], json.dumps({'snapshot_id': snapshot_id}), utc_now()))
                connection.commit()
            if row["status"] != "revoked":
                connection.execute("UPDATE parent_report_snapshot SET status = 'revoked', revoked_at = ?, revoke_reason = ? WHERE snapshot_id = ?", (utc_now(), reason.strip()[:300], snapshot_id))
                # Cancel unsent work immediately. A delivery already being submitted
                # rechecks snapshot approval before handing data to its channel.
                outbox_exists = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'report_delivery_outbox'"
                ).fetchone()
                if outbox_exists:
                    connection.execute("""
                      UPDATE report_delivery_outbox
                      SET status = 'cancelled', cancelled_at = ?, cancel_reason = 'snapshot_revoked', updated_at = ?
                      WHERE snapshot_id = ? AND status = 'scheduled'
                    """, (utc_now(), utc_now(), snapshot_id))
                connection.execute("INSERT INTO audit_event(audit_id, actor, action, child_id, summary_json, occurred_at) VALUES (?, 'parent', 'parent_report_snapshot_revoked', ?, ?, ?)", (str(uuid4()), row["child_id"], json.dumps({"snapshot_id": snapshot_id}, ensure_ascii=False), utc_now()))
                connection.commit()
        return self.get(snapshot_id)

    @_guard_report_change
    def record_feedback(self, snapshot_id: str, helpfulness: int | None, stress_rating: int | None, notes: str | None, client_action_id: str) -> dict:
        action_id = client_action_id.strip()
        if not action_id:
            raise SnapshotFeedbackConflictError("缺少反馈动作标识，无法安全重试。")
        clean_notes = (notes or "").strip()[:1000] or None
        if helpfulness is None and stress_rating is None and clean_notes is None:
            raise SnapshotFeedbackValidationError("请至少填写建议帮助程度、沟通压力或一条备注。")
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            snapshot = connection.execute("SELECT child_id FROM parent_report_snapshot WHERE snapshot_id = ?", (snapshot_id,)).fetchone()
            if not snapshot:
                raise UnknownReportSnapshotError(snapshot_id)
            existing = connection.execute("""
                SELECT snapshot_id, helpfulness, stress_rating, notes FROM parent_report_snapshot_feedback
                WHERE client_action_id = ?
            """, (action_id,)).fetchone()
            if existing:
                if existing["snapshot_id"] != snapshot_id:
                    connection.rollback()
                    raise SnapshotFeedbackConflictError("该反馈动作已用于另一份报告，不能跨报告复用。")
                if (existing["helpfulness"], existing["stress_rating"], existing["notes"]) != (helpfulness, stress_rating, clean_notes):
                    connection.rollback()
                    raise SnapshotFeedbackConflictError("同一次反馈重试的内容不同；请重新保存修改后的反馈。")
                connection.rollback()
                return self.get(snapshot_id)
            now = utc_now()
            feedback_id = str(uuid4())
            connection.execute("""
                INSERT INTO parent_report_snapshot_feedback(
                  feedback_id, snapshot_id, helpfulness, stress_rating, notes, client_action_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (feedback_id, snapshot_id, helpfulness, stress_rating, clean_notes, action_id, now))
            from .report_followup import ReportFollowupService
            ReportFollowupService.append_feedback(connection, feedback_id)
            connection.execute(
                """INSERT INTO audit_event(audit_id, actor, action, child_id, summary_json, occurred_at)
                   VALUES (?, 'parent', 'parent_report_snapshot_feedback_saved', ?, ?, ?)""",
                (str(uuid4()), snapshot["child_id"], json.dumps({"snapshot_id": snapshot_id, "helpfulness": helpfulness, "stress_rating": stress_rating, "client_action_id": action_id}, ensure_ascii=False), now),
            )
            connection.commit()
        return self.get(snapshot_id)

    @_guard_report_change
    def request_correction(self, snapshot_id: str, note: str) -> dict:
        """Preserve an immutable report while stopping delivery of disputed facts."""
        clean_note = note.strip()[:1000]
        if not clean_note:
            raise SnapshotStateError("请说明需要修正的事实或证据。")
        with self.database.connect() as connection:
            now = utc_now()
            snapshot = connection.execute(
                "SELECT child_id, status FROM parent_report_snapshot WHERE snapshot_id = ?", (snapshot_id,)
            ).fetchone()
            if not snapshot:
                raise UnknownReportSnapshotError(snapshot_id)
            if snapshot["status"] != "revoked":
                connection.execute("""
                    UPDATE parent_report_snapshot
                    SET status = 'revoked', revoked_at = ?, revoke_reason = ?
                    WHERE snapshot_id = ?
                """, (now, "家长标记报告事实待修正；停止未发送任务", snapshot_id))
                outbox_exists = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'report_delivery_outbox'"
                ).fetchone()
                if outbox_exists:
                    connection.execute("""
                        UPDATE report_delivery_outbox
                        SET status = 'cancelled', cancelled_at = ?, cancel_reason = 'snapshot_correction_requested', updated_at = ?
                        WHERE snapshot_id = ? AND status = 'scheduled'
                    """, (now, now, snapshot_id))
                connection.execute("""
                    INSERT INTO audit_event(audit_id, actor, action, child_id, summary_json, occurred_at)
                    VALUES (?, 'parent', 'parent_report_snapshot_revoked', ?, ?, ?)
                """, (str(uuid4()), snapshot["child_id"], json.dumps({"snapshot_id": snapshot_id, "reason": "correction_requested"}, ensure_ascii=False), now))
            connection.execute("""
                INSERT INTO parent_report_snapshot_correction(correction_id, snapshot_id, note, created_at)
                VALUES (?, ?, ?, ?)
            """, (str(uuid4()), snapshot_id, clean_note, now))
            connection.execute("""
                INSERT INTO audit_event(audit_id, actor, action, child_id, summary_json, occurred_at)
                VALUES (?, 'parent', 'parent_report_snapshot_correction_requested', ?, ?, ?)
            """, (str(uuid4()), snapshot["child_id"], json.dumps({"snapshot_id": snapshot_id}, ensure_ascii=False), now))
            connection.commit()
        return self.get(snapshot_id)
