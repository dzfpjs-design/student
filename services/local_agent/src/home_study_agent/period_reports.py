from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from functools import wraps
import json
import sqlite3
from uuid import uuid4

from .database import Database
from .family_clock import FAMILY_ZONE, family_today, evidence_calendar_date, evidence_calendar_time
from .repository import UnknownChildError, coaching_evidence_ref, utc_now
from .report_source_reads import report_source_connection, scoped_validation


def _guard_period_report_source(function):
    @wraps(function)
    def guarded(self, *args, **kwargs):
        from .parent_reports import _report_effect_guard
        with _report_effect_guard(self.database):
            return function(self, *args, **kwargs)
    return guarded


class PeriodReportSourceError(ValueError):
    pass


class PeriodReportService:
    def __init__(self, database: Database, behavior_context_provider=None):
        self.database = database
        self.behavior_context_provider = behavior_context_provider

    def set_behavior_context_provider(self, provider) -> None:
        self.behavior_context_provider = provider

    def initialize(self) -> None:
        """Store parent-led evidence withdrawals separately from learning facts.

        A withdrawal never deletes the original learning record.  This keeps the
        audit trail intact while ensuring later live period aggregates do not use
        disputed evidence.  Frozen snapshots keep their original version.
        """
        with self.database.connect() as connection:
            connection.executescript("""
            CREATE TABLE IF NOT EXISTS period_report_evidence_reference (
              evidence_id TEXT PRIMARY KEY,
              child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
              evidence_ref TEXT NOT NULL UNIQUE,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS period_report_evidence_state (
              evidence_id TEXT PRIMARY KEY,
              child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
              is_retracted INTEGER NOT NULL CHECK(is_retracted IN (0, 1)),
              note TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS period_report_evidence_action (
              client_action_id TEXT PRIMARY KEY,
              evidence_id TEXT NOT NULL,
              child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
              action_type TEXT NOT NULL CHECK(action_type IN ('retract', 'restore')),
              note TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            """)
            connection.commit()

    @_guard_period_report_source
    @scoped_validation
    def generate(self, child_id: str, report_type: str, period_end: date | None = None) -> dict:
        self.initialize()
        instant = datetime.fromisoformat(utc_now())
        reference = period_end if period_end is not None else family_today(instant)
        start, end = self._period_bounds(report_type, reference)
        with self.database.connect() as connection:
            existing = connection.execute('SELECT * FROM period_report WHERE child_id=? AND report_type=? AND period_start=? AND period_end=?',
                (child_id, report_type, start.isoformat(), end.isoformat())).fetchone()
        if existing:
            self._check_source(existing)
        self._begin_refresh(child_id, report_type, start, end)
        try:
            behavior_context = self._behavior_context(child_id, start, end)
            cutoff = datetime.combine(min(reference, end) + timedelta(days=1), datetime.min.time(),
                tzinfo=FAMILY_ZONE) - timedelta(microseconds=1)
            supply_as_of = min(instant, cutoff).isoformat()
            content = self._build_content(child_id, report_type, start, end, reference, behavior_context,
                supply_as_of=supply_as_of)
            now = utc_now()
            with report_source_connection(self.database) as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._assert_behavior_source(connection, child_id, start, end, content)
                existing = connection.execute(
                    """
                    SELECT period_report_id, content_json FROM period_report
                    WHERE child_id = ? AND report_type = ? AND period_start = ? AND period_end = ?
                    """,
                    (child_id, report_type, start.isoformat(), end.isoformat()),
                ).fetchone()
                report_id = existing["period_report_id"] if existing else str(uuid4())
                if existing:
                    history = content.get('supply_history')
                    preserved = None
                    # This optional comparison must not make an old damaged
                    # cutoff a new prerequisite for rebuilding the aggregate.
                    try:
                        previous = json.loads(existing['content_json'])
                        prior_history = previous.get('supply_history') if isinstance(previous, dict) else None
                        if isinstance(prior_history, dict) and isinstance(history, dict):
                            old_asof = datetime.fromisoformat(prior_history.get('as_of'))
                            new_asof = datetime.fromisoformat(history.get('as_of'))
                            same_facts = {**content, 'supply_history': {**history, 'as_of': prior_history['as_of']}}
                            if (old_asof.tzinfo is not None and new_asof.tzinfo is not None
                                    and old_asof <= new_asof and same_facts == previous):
                                preserved = same_facts
                    except (ValueError, TypeError):
                        pass
                    if preserved is not None:
                        content = preserved
                    connection.execute(
                        """
                        UPDATE period_report
                        SET source = 'local_aggregate_v2', content_json = ?, updated_at = ?
                        WHERE period_report_id = ?
                        """,
                        (json.dumps(content, ensure_ascii=False), now, report_id),
                    )
                else:
                    connection.execute(
                        """
                        INSERT INTO period_report(
                          period_report_id, child_id, report_type, period_start, period_end,
                          source, content_json, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, 'local_aggregate_v2', ?, ?, ?)
                        """,
                        (
                            report_id, child_id, report_type, start.isoformat(), end.isoformat(),
                            json.dumps(content, ensure_ascii=False), now, now,
                        ),
                    )
                connection.execute(
                    """
                    UPDATE period_report_refresh_state
                    SET status = 'completed', last_error = NULL, updated_at = ?
                    WHERE child_id = ? AND report_type = ?
                    """,
                    (now, child_id, report_type),
                )
                connection.commit()
            return self.get(report_id, persist_refresh=True)
        except Exception as error:
            self._mark_failed(child_id, report_type, type(error).__name__)
            raise

    @scoped_validation
    def refresh_current(self, child_id: str, require_evidence: bool = True) -> dict:
        self.initialize()
        results: list[dict] = []
        failures: list[dict] = []
        today = family_today(datetime.fromisoformat(utc_now()))
        for report_type in ("weekly", "monthly"):
            start, end = self._period_bounds(report_type, today)
            if require_evidence and not self._has_evidence(child_id, start, end):
                continue
            try:
                results.append(self.generate(child_id, report_type, today))
            except (sqlite3.Error, UnknownChildError):
                raise
            except Exception as error:
                # Each period has its own durable failure state. A generation
                # failure must not skip the other period or reopen the daily job.
                failures.append({"report_type": report_type, "error_code": type(error).__name__})
        return {"reports": results, "failures": failures}

    @scoped_validation
    def refresh_all_current(self) -> dict:
        with self.database.connect() as connection:
            children = [
                row["child_id"]
                for row in connection.execute(
                    "SELECT child_id FROM child_profile WHERE status = 'active' ORDER BY child_id"
                ).fetchall()
            ]
        reports: list[dict] = []
        failures: list[dict] = []
        for child_id in children:
            result = self.refresh_current(child_id, require_evidence=True)
            reports.extend(result["reports"])
            failures.extend({"child_id": child_id, **item} for item in result["failures"])
        return {"reports": reports, "failures": failures}

    def list(self, child_id: str) -> list[dict]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT period_report_id FROM period_report
                WHERE child_id = ? ORDER BY period_end DESC, updated_at DESC
                """,
                (child_id,),
            ).fetchall()
        reports = []
        for row in rows:
            try:
                reports.append(self.get(row['period_report_id']))
            except PeriodReportSourceError:
                continue
        return reports

    def _check_source(self, row):
        from .parent_reports import ParentReportService
        issue = ParentReportService.period_row_problem(row)
        if issue:
            ParentReportService(self.database).record_source_issue(row['child_id'], 'period_report', row['period_report_id'], issue)
            raise PeriodReportSourceError('阶段报告来源损坏，旧稿已保留，请核对完整备份。')

    def _behavior_context(self, child_id: str, start: date, end: date) -> dict:
        from .period_behavior_source import period_behavior_fingerprint
        def fingerprint():
            with self.database.connect() as c:
                c.execute('BEGIN')
                return period_behavior_fingerprint(c, child_id, start.isoformat(), end.isoformat())
        # A local provider may finalize a habit on its first read. Retry once,
        # but never stamp a stale provider result with fresh source authority.
        for _ in range(2):
            before = fingerprint()
            try:
                context = (self.behavior_context_provider(child_id=child_id, period_start=start, period_end=end)
                           if self.behavior_context_provider else {})
            except sqlite3.Error:
                raise
            except Exception:
                context = None
            if not isinstance(context, dict):
                context = {"behavior_context_authoritative": True, "behavior_intervention_feedback": [], "behavior_model_summary": {"uncertainty": "本次行为上下文不可用，不作推断。"}}
            if before == fingerprint():
                return {**context, '_local_source_fingerprint': before}
        raise PeriodReportSourceError('PERIOD_BEHAVIOR_SOURCE_CHANGED')

    @_guard_period_report_source
    @scoped_validation
    def get(self, report_id: str, *, persist_refresh: bool = False) -> dict:
        self.initialize()
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT period_report_id, child_id, report_type, period_start, period_end,
                       source, content_json, created_at, updated_at
                FROM period_report WHERE period_report_id = ?
                """,
                (report_id,),
            ).fetchone()
        if not row:
            raise UnknownPeriodReportError(report_id)
        self._check_source(row)
        result = dict(row)
        stored = json.loads(result.pop("content_json"))
        start, end = date.fromisoformat(row['period_start']), date.fromisoformat(row['period_end'])
        as_of = date.fromisoformat(stored.get('as_of_date') or row['period_end'])
        # Live period reports revalidate facts; approved parent snapshots remain immutable elsewhere.
        result["content"] = self._build_content(row['child_id'], row['report_type'], start, end, as_of,
            self._behavior_context(row['child_id'], start, end),
            supply_as_of=(stored.get('supply_history') or {}).get('as_of'))
        if persist_refresh:
            # Snapshot freezing must hash the same persisted content that it
            # presents for approval, not yesterday's aggregate cache.
            now = utc_now()
            with report_source_connection(self.database) as connection:
                connection.execute('BEGIN IMMEDIATE')
                self._assert_behavior_source(connection, row['child_id'], start, end, result['content'])
                if result['content'] != stored:
                    updated = connection.execute('''UPDATE period_report SET content_json=?,updated_at=?
                        WHERE period_report_id=? AND child_id=? AND content_json=? AND updated_at=?''',
                        (json.dumps(result['content'],ensure_ascii=False),now,report_id,row['child_id'],row['content_json'],row['updated_at']))
                    if updated.rowcount != 1:
                        from .parent_reports import SnapshotStateError
                        raise SnapshotStateError('阶段报告在冻结期间已变化，请重新读取。')
                connection.commit()
            if result['content'] != stored:
                result['updated_at'] = now
        return result

    @staticmethod
    def _assert_behavior_source(connection, child_id, start, end, content):
        from .parent_reports import ParentReportService
        behavior, learning = ParentReportService._period_source_fingerprints(connection, child_id, start, end)
        if content.get('behavior_source_fingerprint') != behavior:
            raise PeriodReportSourceError('PERIOD_BEHAVIOR_SOURCE_CHANGED')
        if content.get('learning_source_fingerprint') != learning:
            raise PeriodReportSourceError('PERIOD_LEARNING_SOURCE_CHANGED')

    def _build_content(
        self, child_id: str, report_type: str, start: date, end: date, as_of: date,
        behavior_context: dict | None = None,
        *, supply_as_of: str | None = None,
    ) -> dict:
        from .parent_reports import DAILY_STRUCTURE_SELECT, ParentReportService, SOURCE_ISSUE_MESSAGES
        source_issues = []
        # UTC calendar dates only bound the scan. Final inclusion uses the exact
        # family date, including subsecond instants and offset-bearing records.
        query_start = (start - timedelta(days=1)).isoformat()
        with report_source_connection(self.database) as connection:
            self._ensure_evidence_refs(connection, child_id)
            connection.commit()
            connection.execute('BEGIN')
            from .period_behavior_source import period_behavior_fingerprint
            behavior_fingerprint = period_behavior_fingerprint(connection, child_id, start.isoformat(), end.isoformat())
            if (behavior_context.get('_local_source_fingerprint') is not None
                    and behavior_context['_local_source_fingerprint'] != behavior_fingerprint):
                raise PeriodReportSourceError('PERIOD_BEHAVIOR_SOURCE_CHANGED')
            supply_history = None
            if supply_as_of:
                from .parent_supply_followup import period_supply_facts
                try:
                    supply_history = period_supply_facts(connection, child_id,
                        start.isoformat(), end.isoformat(), supply_as_of)
                except (ValueError, KeyError, TypeError) as error:
                    raise PeriodReportSourceError('PERIOD_SUPPLY_SOURCE_UNAVAILABLE') from error
            effective = self._effective_evidence(connection, child_id)
            rows = connection.execute(
                DAILY_STRUCTURE_SELECT + ' WHERE r.child_id=? AND r.report_date BETWEEN ? AND ? ORDER BY r.report_date,r.created_at',
                (child_id, start.isoformat(), end.isoformat()),
            ).fetchall()
            valid_rows = []
            for row in rows:
                problem = ParentReportService._daily_row_problem(row)
                if problem:
                    source_issues.append((row['report_id'], problem))
                else:
                    valid_rows.append(row)
            rows = valid_rows
            evidence_rows = connection.execute(
                """
                SELECT le.evidence_id, le.knowledge_node_id, kn.subject, kn.title,
                       le.evidence_type, le.correctness, le.hint_level, le.independent,
                       le.confidence, le.evidence_summary, le.occurred_at, prref.evidence_ref
                FROM learning_evidence le
                JOIN knowledge_node kn ON kn.knowledge_node_id = le.knowledge_node_id
                JOIN period_report_evidence_reference prref ON prref.evidence_id = le.evidence_id AND prref.child_id = le.child_id
                LEFT JOIN period_report_evidence_state pres
                  ON pres.evidence_id = le.evidence_id AND pres.child_id = le.child_id
                WHERE le.child_id = ? AND date(le.occurred_at) BETWEEN ? AND ?
                  AND COALESCE(pres.is_retracted, 0) = 0
                ORDER BY le.occurred_at, le.evidence_id
                """, (child_id, query_start, end.isoformat())
            ).fetchall()
            new_mistakes = connection.execute(
                """
                SELECT mc.mistake_id, mc.subject, mc.title, mc.status, mc.confidence, mc.knowledge_node_id,
                       mc.source_evidence_id,
                       mc.created_at, le.evidence_summary, le.occurred_at, prref.evidence_ref
                FROM mistake_case mc
                JOIN learning_evidence le ON le.evidence_id = mc.source_evidence_id
                JOIN period_report_evidence_reference prref ON prref.evidence_id = le.evidence_id AND prref.child_id = le.child_id
                LEFT JOIN period_report_evidence_state pres
                  ON pres.evidence_id = le.evidence_id AND pres.child_id = le.child_id
                WHERE mc.child_id = ? AND le.child_id = ? AND COALESCE(pres.is_retracted, 0) = 0 AND mc.status != 'excluded'
                  AND (date(mc.created_at) BETWEEN ? AND ? OR EXISTS (
                    SELECT 1 FROM mistake_attempt period_attempt
                    WHERE period_attempt.mistake_id = mc.mistake_id AND period_attempt.child_id = mc.child_id
                      AND date(period_attempt.created_at) BETWEEN ? AND ?))
                ORDER BY mc.created_at, mc.mistake_id
                """, (child_id, child_id, query_start, end.isoformat(), query_start, end.isoformat())
            ).fetchall()
            retests = connection.execute(
                """
                SELECT ma.mistake_id, mc.subject, mc.title, ma.stage, ma.created_at, ma.evidence_id,
                       mc.source_evidence_id, ma.diagnostic_item_id,
                       le.evidence_summary, prref.evidence_ref
                FROM mistake_attempt ma
                JOIN mistake_case mc ON mc.mistake_id = ma.mistake_id AND mc.child_id = ma.child_id
                JOIN learning_evidence le ON le.evidence_id = ma.evidence_id AND le.child_id = ma.child_id
                JOIN period_report_evidence_reference prref ON prref.evidence_id = le.evidence_id AND prref.child_id = le.child_id
                LEFT JOIN period_report_evidence_state pres
                  ON pres.evidence_id = le.evidence_id AND pres.child_id = le.child_id
                WHERE ma.child_id = ? AND date(ma.created_at) BETWEEN ? AND ?
                  AND ma.correctness >= 1 AND ma.independent = 1
                  AND ma.stage IN ('near_transfer', 'representation_transfer', 'discrimination', 'far_transfer')
                  AND COALESCE(pres.is_retracted, 0) = 0
                ORDER BY ma.created_at, ma.attempt_id
                """, (child_id, query_start, end.isoformat())
            ).fetchall()
            from .period_written_observations import observations_for_period
            written_observations = observations_for_period(connection, child_id, start, end, effective,
                now=datetime.fromisoformat(utc_now()))
            evidence_rows = [{**dict(row), **{key: effective[row['evidence_id']][key]
                              for key in ('correctness', 'independent', 'hint_level', 'confidence')}}
                             for row in evidence_rows if row['evidence_id'] in effective
                             and start <= self._evidence_date(row['occurred_at']) <= end]
            period_mistakes = {row.get('mistake_id') for row in effective.values()
                              if start <= self._evidence_date(row['occurred_at']) <= end}
            new_mistakes = [row for row in new_mistakes if row['source_evidence_id'] in effective
                           and effective[row['source_evidence_id']]['correctness'] == 0
                           and effective[row['source_evidence_id']]['knowledge_node_id'] == row['knowledge_node_id']
                           and (start <= self._evidence_date(row['created_at']) <= end
                                or row['mistake_id'] in period_mistakes)]
            retests = [row for row in retests if row['evidence_id'] in effective
                       and start <= self._evidence_date(row['created_at']) <= end
                       and row['source_evidence_id'] in effective
                       and effective[row['evidence_id']]['correctness'] == 1
                       and effective[row['evidence_id']]['independent']
                       and effective[row['evidence_id']]['hint_level'] == 0
                       and effective[row['evidence_id']].get('question_identity')
                       and effective[row['evidence_id']]['question_identity'] != effective[row['source_evidence_id']].get('question_identity')
                       and row['diagnostic_item_id'] != effective[row['source_evidence_id']]['diagnostic_item_id']]
            child = connection.execute("SELECT grade FROM child_profile WHERE child_id = ?", (child_id,)).fetchone()
            from .report_followup import ReportFollowupService
            feedback = ReportFollowupService.coaching_experiences(connection, child_id)
            behavior_rows = connection.execute(
                """
                SELECT intervention_id, trigger_state, child_response, confidence,
                       COALESCE(responded_at, created_at) AS occurred_at
                FROM intervention_record
                WHERE child_id = ? AND child_response IS NOT NULL
                  AND date(COALESCE(responded_at, created_at)) BETWEEN ? AND ?
                """, (child_id, query_start, end.isoformat())
            ).fetchall()
            behavior_rows = sorted(
                (row for row in behavior_rows if start <= self._evidence_date(row['occurred_at']) <= end),
                key=lambda row: (evidence_calendar_time(row['occurred_at']).replace(tzinfo=None),
                                 row['intervention_id']), reverse=True)[:5]
            goal_progress = self._goal_progress(connection, child_id, start, end)
            learning_fingerprint = ParentReportService.period_learning_sources(connection, child_id, start, end,
                effective=effective, observations=written_observations,
                coaching_experiences=feedback, goal_progress=goal_progress)
        for report_id, problem in source_issues:
            ParentReportService(self.database).record_source_issue(child_id, 'daily_report', report_id, problem)
        daily = [
            {
                "report_date": row["report_date"],
                "summary": json.loads(row["summary_json"]),
                "coaching": json.loads(row["parent_coaching_json"]),
            }
            for row in rows
        ]
        by_date: dict[str, list[dict]] = {}
        for item in daily:
            by_date.setdefault(item["report_date"], []).append(item)
        recorded_days = len(by_date)
        recorded_sessions = len(daily)
        expected_days = (end - start).days + 1
        evidence_total = sum(item["summary"]["evidence_count"] for item in daily)
        daily_signals = []
        for report_date, items in by_date.items():
            latest_headline = items[-1]["summary"]["headline"]
            headline = (
                latest_headline
                if len(items) == 1
                else f"{len(items)}次学习会话，最近一次：{latest_headline}"
            )
            daily_signals.append({"date": report_date, "headline": headline, "session_count": len(items)})
        trend_threshold = 3 if report_type == "weekly" else 8
        learning_summary = self._learning_summary(evidence_rows, new_mistakes, retests)
        learning_summary['qualified_observations'] = written_observations
        learning_summary['coverage']['qualified_observation_count'] = len(written_observations)
        if written_observations and not evidence_rows:
            learning_summary['uncertainty'] = '本期有可靠的原作答核对记录，但没有可用于判断稳定掌握的独立迁移或保持证据；两者分开记录。'
        legacy_behavior_intervention_feedback = []
        for row in behavior_rows:
            response = str(row["child_response"] or "")
            if response == "accepted":
                legacy_behavior_intervention_feedback.append({"kind": "intervention_accepted", "summary": "孩子接受过一次温和学习提醒", "confidence": float(row["confidence"]), "evidence_ref": coaching_evidence_ref(child_id, "intervention", row["intervention_id"]), "source": "旧版提醒记录（兼容来源）", "occurred_at": row["occurred_at"], "legacy_source": True})
            elif response:
                display = {"later": "稍后", "less_like_this": "这类提醒少一点", "declined": "暂不接受"}.get(response, "暂不接受")
                legacy_behavior_intervention_feedback.append({"kind": "intervention_declined", "summary": f"孩子对一次温和学习提醒选择“{display}”", "confidence": float(row["confidence"]), "evidence_ref": coaching_evidence_ref(child_id, "intervention", row["intervention_id"]), "source": "旧版提醒记录（兼容来源）", "occurred_at": row["occurred_at"], "legacy_source": True})
        behavior_context = behavior_context if isinstance(behavior_context, dict) else {}
        behavior_context_authoritative = bool(behavior_context.get("behavior_context_authoritative")) or "behavior_intervention_feedback" in behavior_context
        behavior_intervention_feedback = behavior_context.get("behavior_intervention_feedback")
        if not isinstance(behavior_intervention_feedback, list):
            behavior_intervention_feedback = []
        behavior_model_summary = behavior_context.get("behavior_model_summary")
        if not isinstance(behavior_model_summary, dict):
            behavior_model_summary = {}
        habit_experiments = behavior_context.get("habit_experiments")
        if not isinstance(habit_experiments, list):
            habit_experiments = []
        goal_progress_payload = [dict(item) for item in goal_progress]
        coaching = self._period_coaching(child_id, dict(child) if child else {}, learning_summary, [dict(item) for item in feedback], behavior_intervention_feedback if behavior_context_authoritative else legacy_behavior_intervention_feedback, behavior_model_summary, goal_progress_payload, habit_experiments)
        all_evidence = evidence_total + learning_summary["coverage"]["eligible_evidence_count"]
        enough_period_evidence = bool(learning_summary["coverage"]["eligible_evidence_count"])
        return {
            'behavior_source_fingerprint': behavior_fingerprint,
            'learning_source_fingerprint': learning_fingerprint,
            **({'supply_history': supply_history} if supply_history is not None else {}),
            "headline": (f"{recorded_days}天有学习记录；已汇总{learning_summary['coverage']['eligible_evidence_count']}条可追溯知识证据"
                         if enough_period_evidence else
                         f"{recorded_days}天有学习记录；{len(written_observations)}份原作答已可靠核对，稳定掌握仍待复查"
                         if written_observations else f"{recorded_days}天有学习记录；知识掌握证据不足"),
            "as_of_date": min(as_of, end).isoformat(),
            "coverage": {
                "recorded_days": recorded_days,
                "recorded_sessions": recorded_sessions,
                "excluded_daily_reports": len(source_issues),
                "expected_calendar_days": expected_days,
                "evidence_count": all_evidence,
                "sufficient_for_trend": recorded_days >= trend_threshold and enough_period_evidence,
            },
            "daily_signals": daily_signals,
            "source_warnings": list(dict.fromkeys(SOURCE_ISSUE_MESSAGES[problem] for _, problem in source_issues)),
            "learning_summary": learning_summary,
            "goal_progress": goal_progress_payload,
            "habit_experiments": habit_experiments,
            "interpretation": (
                "本期有可追溯的知识与复测记录；独立变式通过只说明该次复测通过，不等同于已经稳定掌握。"
                if recorded_days >= trend_threshold and enough_period_evidence
                else "本期记录不足以判断稳定趋势或掌握程度；未作答、未记录与被撤回的证据都不作负面推断。"
            ),
            "next_action": None,
            "parent_script": coaching["script"],
            "parent_coaching": coaching,
        }

    @_guard_period_report_source
    def retract_evidence(self, child_id: str, evidence_ref: str, note: str, client_action_id: str) -> bool:
        self.initialize()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if self._replay_action(connection, child_id, evidence_ref, "retract", note, client_action_id):
                connection.rollback(); return True
            evidence = connection.execute("SELECT evidence_id FROM period_report_evidence_reference WHERE evidence_ref = ? AND child_id = ?", (evidence_ref, child_id)).fetchone()
            if not evidence:
                connection.rollback()
                return False
            replay = self._reserve_evidence_action(connection, evidence["evidence_id"], child_id, "retract", note, client_action_id)
            if replay:
                connection.rollback(); return True
            state = connection.execute("SELECT is_retracted FROM period_report_evidence_state WHERE evidence_id = ? AND child_id = ?", (evidence["evidence_id"], child_id)).fetchone()
            if state and state["is_retracted"]:
                connection.rollback()
                raise PeriodEvidenceActionConflictError("这条记录已撤回；请勿重复提交不同的撤回操作。")
            now = utc_now()
            connection.execute("""INSERT INTO period_report_evidence_state(evidence_id, child_id, is_retracted, note, updated_at)
                VALUES (?, ?, 1, ?, ?) ON CONFLICT(evidence_id) DO UPDATE SET is_retracted = 1, note = excluded.note, updated_at = excluded.updated_at""", (evidence["evidence_id"], child_id, note.strip()[:500], now))
            connection.execute("INSERT INTO audit_event(audit_id, actor, action, child_id, summary_json, occurred_at) VALUES (?, 'parent', 'period_report_evidence_retracted', ?, ?, ?)", (str(uuid4()), child_id, json.dumps({"evidence_ref": evidence_ref}, ensure_ascii=False), now))
            connection.commit()
        return True

    @_guard_period_report_source
    def restore_evidence(self, child_id: str, evidence_ref: str, note: str, client_action_id: str) -> bool:
        self.initialize()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if self._replay_action(connection, child_id, evidence_ref, "restore", note, client_action_id):
                connection.rollback(); return True
            evidence = connection.execute("SELECT evidence_id FROM period_report_evidence_reference WHERE evidence_ref = ? AND child_id = ?", (evidence_ref, child_id)).fetchone()
            if not evidence:
                connection.rollback(); return False
            replay = self._reserve_evidence_action(connection, evidence["evidence_id"], child_id, "restore", note, client_action_id)
            if replay:
                connection.rollback(); return True
            state = connection.execute("SELECT is_retracted FROM period_report_evidence_state WHERE evidence_id = ? AND child_id = ?", (evidence["evidence_id"], child_id)).fetchone()
            if not state or not state["is_retracted"]:
                connection.rollback(); raise PeriodEvidenceActionConflictError("这条记录当前没有撤回状态，不能恢复。")
            now = utc_now()
            connection.execute("UPDATE period_report_evidence_state SET is_retracted = 0, note = ?, updated_at = ? WHERE evidence_id = ? AND child_id = ?", (note.strip()[:500], now, evidence["evidence_id"], child_id))
            connection.execute("INSERT INTO audit_event(audit_id, actor, action, child_id, summary_json, occurred_at) VALUES (?, 'parent', 'period_report_evidence_restored', ?, ?, ?)", (str(uuid4()), child_id, json.dumps({"evidence_ref": evidence_ref}, ensure_ascii=False), now))
            connection.commit()
        return True

    def retracted_evidence(self, child_id: str) -> list[dict]:
        self.initialize()
        with self.database.connect() as connection:
            rows = connection.execute("""SELECT ref.evidence_ref, kn.subject, kn.title AS knowledge_title,
                le.evidence_summary, le.occurred_at, state.note, state.updated_at
                FROM period_report_evidence_state state
                JOIN period_report_evidence_reference ref ON ref.evidence_id = state.evidence_id AND ref.child_id = state.child_id
                JOIN learning_evidence le ON le.evidence_id = state.evidence_id AND le.child_id = state.child_id
                JOIN knowledge_node kn ON kn.knowledge_node_id = le.knowledge_node_id
                WHERE state.child_id = ? AND state.is_retracted = 1
                ORDER BY state.updated_at DESC""", (child_id,)).fetchall()
        return [{"evidence_ref": row["evidence_ref"], "subject": row["subject"], "knowledge_title": row["knowledge_title"], "summary": row["evidence_summary"], "date": self._evidence_date(row["occurred_at"]).isoformat(), "note": row["note"], "retracted_at": row["updated_at"]} for row in rows]

    @staticmethod
    def _replay_action(connection, child_id: str, evidence_ref: str, action_type: str, note: str, client_action_id: str) -> bool:
        existing = connection.execute("""SELECT action.child_id, action.action_type, action.note, ref.evidence_ref
            FROM period_report_evidence_action action
            JOIN period_report_evidence_reference ref ON ref.evidence_id = action.evidence_id
            WHERE action.client_action_id = ?""", (client_action_id,)).fetchone()
        if not existing:
            return False
        if (existing["child_id"], existing["evidence_ref"], existing["action_type"], existing["note"]) != (child_id, evidence_ref, action_type, note.strip()[:500]):
            raise PeriodEvidenceActionConflictError("该操作标识已用于不同证据或内容，不能跨操作复用。")
        return True

    @staticmethod
    def _reserve_evidence_action(connection, evidence_id: str, child_id: str, action_type: str, note: str, client_action_id: str) -> bool:
        existing = connection.execute("SELECT evidence_id, child_id, action_type, note FROM period_report_evidence_action WHERE client_action_id = ?", (client_action_id,)).fetchone()
        clean_note = note.strip()[:500]
        if existing:
            if (existing["evidence_id"], existing["child_id"], existing["action_type"], existing["note"]) != (evidence_id, child_id, action_type, clean_note):
                raise PeriodEvidenceActionConflictError("该操作标识已用于不同证据或内容，不能跨操作复用。")
            return True
        connection.execute("INSERT INTO period_report_evidence_action(client_action_id, evidence_id, child_id, action_type, note, created_at) VALUES (?, ?, ?, ?, ?, ?)", (client_action_id, evidence_id, child_id, action_type, clean_note, utc_now()))
        return False

    @staticmethod
    def _ensure_evidence_refs(connection, child_id: str) -> None:
        rows = connection.execute("SELECT evidence_id FROM learning_evidence WHERE child_id = ?", (child_id,)).fetchall()
        now = utc_now()
        for row in rows:
            connection.execute("INSERT OR IGNORE INTO period_report_evidence_reference(evidence_id, child_id, evidence_ref, created_at) VALUES (?, ?, ?, ?)", (row["evidence_id"], child_id, f"record-{uuid4().hex[:18]}", now))

    @staticmethod
    def _evidence_date(value: str) -> date:
        return evidence_calendar_date(value)

    @staticmethod
    def _goal_progress(connection, child_id, start, end):
        """One live goal/plan projection shared by content and source guards."""
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='goal_cycle'").fetchone():
            return []
        return [dict(row) for row in connection.execute('''
            WITH cycle_progress AS (
              SELECT gc.cycle_id,gp.track,gp.title,gp.target_statement,gc.target_minutes,gc.target_sessions,
                COALESCE(SUM(CASE WHEN pi.status='completed'
                  THEN COALESCE(pi.actual_minutes,pi.expected_minutes) ELSE 0 END),0) AS completed_minutes,
                COUNT(DISTINCT CASE WHEN pi.status='completed' THEN dp.session_id END) AS completed_sessions,
                COUNT(CASE WHEN pi.status='completed' THEN 1 END) AS evidence_items
              FROM goal_cycle gc JOIN goal_program gp ON gp.program_id=gc.program_id AND gp.child_id=gc.child_id
              LEFT JOIN daily_plan dp ON dp.child_id=gc.child_id
                AND dp.plan_date BETWEEN gc.week_start AND gc.week_end AND dp.plan_date BETWEEN ? AND ?
              LEFT JOIN plan_item pi ON pi.plan_id=dp.plan_id AND (
                (gp.track='pet_path' AND pi.source_type='pet_daily') OR
                (gp.track='competition_math_path' AND pi.source_type='competition_math') OR
                (gp.track='junior_exam_readiness' AND pi.source_type IN ('junior_exam_goal','mistake_review','due_review')))
              WHERE gc.child_id=? AND gp.status='active' AND gc.week_end>=? AND gc.week_start<=?
              GROUP BY gc.cycle_id)
            SELECT track,title,target_statement,SUM(target_minutes) AS target_minutes,
              SUM(target_sessions) AS target_sessions,SUM(completed_minutes) AS completed_minutes,
              SUM(completed_sessions) AS completed_sessions,SUM(evidence_items) AS evidence_items
            FROM cycle_progress GROUP BY track,title,target_statement ORDER BY title,track,target_statement
            ''', (str(start),str(end),child_id,str(start),str(end)))]

    @staticmethod
    def _evidence_action_versions(connection, child_id: str, period_start: str, period_end: str) -> list[dict]:
        roots = {row['evidence_id'] for row in connection.execute(
            'SELECT evidence_id,occurred_at FROM learning_evidence WHERE child_id=?', (child_id,))
            if period_start <= PeriodReportService._evidence_date(row['occurred_at']).isoformat() <= period_end}
        edges = list(connection.execute('SELECT result_evidence_id,source_evidence_id FROM review_task WHERE child_id=?', (child_id,)))
        edges.extend(connection.execute('''SELECT ma.evidence_id,mc.source_evidence_id FROM mistake_attempt ma
            JOIN mistake_case mc USING(mistake_id) WHERE ma.child_id=? AND mc.child_id=?''', (child_id, child_id)))
        # Keep withdrawn ancestors in version history even when the effective
        # projection excludes them and their dependent receipts.
        while True:
            ancestors = {source for result, source in edges if result in roots and source is not None}
            if ancestors <= roots:
                break
            roots.update(ancestors)
        return [dict(row) for row in connection.execute('''SELECT client_action_id,evidence_id,action_type
            FROM period_report_evidence_action WHERE child_id=? ORDER BY client_action_id''', (child_id,))
            if row['evidence_id'] in roots]

    @staticmethod
    def _effective_evidence(connection, child_id: str) -> dict:
        from .learning_obligations import project_child_mastery_evidence

        effective = {}
        history = connection.execute('SELECT rowid,evidence_id,knowledge_node_id FROM learning_evidence WHERE child_id=?', (child_id,)).fetchall()
        positions = {r['evidence_id']: r['rowid'] for r in history}
        projections = project_child_mastery_evidence(connection, child_id, as_of=utc_now())
        for node_id, projection in projections.items():
            effective.update({r['evidence_id']: {**r, 'knowledge_node_id': node_id} for r in projection['effective_evidence']})
        retracted = {r[0] for r in connection.execute(
            'SELECT evidence_id FROM period_report_evidence_state WHERE child_id=? AND is_retracted=1', (child_id,))}
        cases = {r['mistake_id']: dict(r) for r in connection.execute(
            'SELECT mistake_id,source_evidence_id,knowledge_node_id,status FROM mistake_case WHERE child_id=?', (child_id,))}
        for evidence_id in retracted:
            effective.pop(evidence_id, None)
        # Resolve ancestry over all history before applying the report window.
        # A report-only withdrawal must also invalidate its derived review and mistake receipts.
        changed = True
        while changed:
            changed = False
            for evidence_id, row in list(effective.items()):
                source_id = row.get('review_source_evidence_id')
                case = cases.get(row.get('mistake_id')) if row.get('mistake_id') else None
                if row.get('mistake_id'):
                    source_id = case['source_evidence_id'] if case else None
                    if (not case or case['status'] == 'excluded' or source_id not in effective
                            or effective[source_id]['correctness'] != 0
                            or case['knowledge_node_id'] != row['knowledge_node_id']):
                        effective.pop(evidence_id)
                        changed = True
                        continue
                if source_id and (source_id not in effective
                                  or effective[source_id]['knowledge_node_id'] != row['knowledge_node_id']
                                  or positions[source_id] >= positions[evidence_id]
                                  or datetime.fromisoformat(effective[source_id]['occurred_at']) > datetime.fromisoformat(row['occurred_at'])):
                    effective.pop(evidence_id)
                    changed = True
        return effective

    @staticmethod
    def _learning_summary(evidence_rows: list, new_mistakes: list, retests: list) -> dict:
        evidence = [dict(row) for row in evidence_rows]
        by_node: dict[str, list[dict]] = {}
        for item in evidence:
            by_node.setdefault(item["knowledge_node_id"], []).append(item)
        weak: list[dict] = []
        pending: list[dict] = []
        for item in new_mistakes:
            row = dict(item)
            readable = {"subject": row["subject"], "title": row["title"], "date": evidence_calendar_date(row["created_at"]).isoformat(), "summary": row["evidence_summary"], "evidence_ref": row["evidence_ref"]}
            # A successful transfer attempt is encouraging evidence, not a reason
            # to hide a case that is currently fragile, relapsed, or still pending.
            if row["status"] != "mastered":
                weak.append(readable)
            if row["status"] in {"captured", "awaiting_confirmation", "diagnosing", "remediating", "near_transfer", "mixed_transfer", "delayed_verification", "fragile", "relapsed"}:
                pending.append(readable)
        retest_items = [{"subject": row["subject"], "title": row["title"], "date": evidence_calendar_date(row["created_at"]).isoformat(), "stage": row["stage"], "summary": row["evidence_summary"], "evidence_ref": row["evidence_ref"]} for row in retests]
        readable_evidence = [{"subject": row["subject"], "knowledge_title": row["title"], "date": evidence_calendar_date(row["occurred_at"]).isoformat(), "summary": row["evidence_summary"], "independent": bool(row["independent"] and row["hint_level"] == 0), "correctness": row["correctness"], "confidence": row["confidence"], "evidence_ref": row["evidence_ref"]} for row in evidence]
        independent = sum(1 for row in evidence if row["independent"] and row["hint_level"] == 0 and row["correctness"] is not None)
        answered = sum(1 for row in evidence if row["correctness"] is not None)
        return {
            "new_weaknesses": weak,
            "independent_variant_retests": retest_items,
            "pending_confirmation": pending,
            "evidence": readable_evidence,
            "knowledge_support": [PeriodReportService._node_support(items) for items in by_node.values()],
            "coverage": {"eligible_evidence_count": len(evidence), "knowledge_nodes_covered": len(by_node), "answered_evidence_count": answered, "independent_answered_count": independent, "sufficient_for_mastery_claim": False},
            "uncertainty": ("本期没有可追溯且未撤回的知识或错题证据；不把未作答当作不会。" if not evidence else "单次正确、单次错题或一次独立变式通过，都不足以单独宣称已经掌握；未作答不作负面推断。"),
        }

    @staticmethod
    def _node_support(items: list[dict]) -> dict:
        independent_correct = sum(1 for item in items if item["independent"] and item["hint_level"] == 0 and item["correctness"] == 1)
        independent_wrong = sum(1 for item in items if item["independent"] and item["hint_level"] == 0 and item["correctness"] is not None and item["correctness"] < 1)
        undecided = sum(1 for item in items if item["correctness"] is None)
        if independent_wrong:
            support = "本期存在独立作答未完全正确的记录，需先补救并在不同题型中复核。"
        elif independent_correct >= 2 and not undecided:
            support = "本期有多次独立正确记录支持本次表现，仍需后续变式复核，不能据此宣称稳定掌握。"
        elif independent_correct:
            support = "本期有一次独立正确记录；证据仍少，建议后续在不同题型中复核。"
        else:
            support = "本期正误或独立性证据不足，建议先核对记录，不作掌握判断。"
        return {"subject": items[0]["subject"], "knowledge_title": items[0]["title"], "evidence_count": len(items), "independent_correct_count": independent_correct, "independent_incorrect_count": independent_wrong, "unjudged_count": undecided, "support": support}

    @staticmethod
    def _period_coaching(child_id: str, child: dict, summary: dict, feedback: list[dict], behavior_intervention_feedback: list[dict] | None = None, behavior_model_summary: dict | None = None, goal_progress: list[dict] | None = None, habit_experiments: list[dict] | None = None) -> dict:
        grade = child.get("grade") or "当前阶段"
        goal = "四年级的PET/数学思维阶段目标" if grade == "四年级" else "初三中考复习阶段" if grade == "初三" else f"{grade}的已确认阶段目标"
        pressure = [item.get("stress_rating") for item in feedback if item.get("stress_rating") is not None]
        gentle = bool(pressure and sum(pressure) / len(pressure) >= 4)
        behavior_intervention_feedback = [item for item in (behavior_intervention_feedback or []) if isinstance(item, dict)]
        behavior_model_summary = behavior_model_summary or {}
        declined = any(item.get("kind") == "intervention_declined" for item in behavior_intervention_feedback)
        observations = summary.get('qualified_observations') or []
        has_exact_evidence = summary["coverage"]["eligible_evidence_count"] > 0
        has_evidence = has_exact_evidence or bool(observations)
        goal_progress = goal_progress or []
        habit_experiments = [item for item in (habit_experiments or []) if isinstance(item, dict)]
        active_habit = next((item for item in habit_experiments if item.get("status") == "active"), None)
        completed_habit = next((item for item in habit_experiments if item.get("status") == "completed"), None)
        recorded_goal_progress = [item for item in goal_progress if int(item.get("evidence_items") or 0) > 0]
        progress_text = "；".join(
            f"{item['title']}已记录{item['completed_minutes']}/{item['target_minutes']}分钟、{item['completed_sessions']}/{item['target_sessions']}次"
            for item in recorded_goal_progress
        )
        reason = f"依据本期{summary['coverage']['eligible_evidence_count']}条可追溯证据，并结合{goal}" if has_exact_evidence else f"本期缺少可追溯知识证据；仅结合{goal}说明如何不施压地核对记录"
        if observations:
            observation_reason = f"本期另有{len(observations)}份已可靠核对的原作答，不等同迁移或稳定掌握"
            reason = reason + '；' + observation_reason if has_exact_evidence else observation_reason + f'；结合{goal}核对一个具体步骤'
        if progress_text:
            reason += f"；长期目标执行记录：{progress_text}"
        if gentle:
            reason += "；最近反馈提示沟通压力偏高，本次先听孩子的感受，不追加任务。"
        if declined:
            reason += "；本期有一次孩子选择暂不接受提醒，家长只邀请孩子选择是否核对，不追问原因。"
        if active_habit:
            reason += f"；孩子已同意进行“{active_habit.get('title', '七天小实验')}”，本期只支持这一项，不叠加新要求。"
        elif completed_habit:
            direction = (completed_habit.get("outcome") or {}).get("direction")
            reason += f"；“{completed_habit.get('title', '七天小实验')}”已结束，结果为{'出现改善信号' if direction == 'improving' else '暂未形成清晰变化'}，不作因果判断。"
        basis = [f"本期可追溯知识证据：{summary['coverage']['eligible_evidence_count']}条", f"新发现需继续核对的错题：{len(summary['pending_confirmation'])}项", f"独立变式复测通过：{len(summary['independent_variant_retests'])}次（不等同稳定掌握）", *([f"长期目标执行：{progress_text}"] if progress_text else []), *[str(item["summary"]) for item in behavior_intervention_feedback if item.get("summary")]]
        if observations:
            basis[0] = f"其他知识学习记录：{summary['coverage']['eligible_evidence_count']}条"
            basis.append(f'已可靠核对的原作答：{len(observations)}份（单独记录，不等同掌握）')
        pending = summary.get("pending_confirmation") or []
        evidence = summary.get("evidence") or []
        focus_observation = next((item for item in observations if not item.get('passed')),
                                 observations[0] if observations else None)
        focus_title = (
            str(pending[0].get("title") or pending[0].get("knowledge_title") or "一个待核对点")
            if pending else
            str(evidence[0].get("knowledge_title") or "本期学习记录") if evidence else
            str(focus_observation.get('knowledge_title') or '这份原作答') if focus_observation else
            "本期学习记录"
        )
        if has_evidence:
            evidence_line = (
                "这次有独立变式通过，这是有价值的一次证据；我们先不把它说成以后都会。"
                if summary["independent_variant_retests"] else
                "这份记录只说明这次发生了什么，我们先不把一次表现说成能力结论。"
            )
            if not has_exact_evidence and focus_observation:
                result = '达到本题量表要求' if focus_observation.get('passed') else '还有一个步骤需要核对'
                evidence_line = f'这份原作答已可靠核对，{result}；一次核对不等同掌握，我们先听听你当时怎么想。'
            script = [
                f"我想和你用几分钟看看{focus_title}，先听你说当时哪一步最需要帮助。",
                "你可以只说一件具体发生的事；现在不想说也可以，我们就停下。",
                evidence_line,
                "你来选：下次先核对这个点，还是换一个你更想处理的点？",
                "今天只定下一次的一小步，不因为看了报告临时加题。",
            ]
        else:
            script = [
                "这期记录还不够，我不想凭空判断你学得怎么样。",
                "你愿意告诉我有没有哪一项其实做过、只是没有记录吗？不想说也可以。",
                "没有记录不等于不会，也不等于没有努力。",
                "我们只选下次要不要补一条记录，不补题、不追问原因。",
                "五分钟到了就结束，今天不追加学习任务。",
            ]
        duration = 5 if gentle or not has_evidence else 8
        action = {
            "what": f"和孩子核对{focus_title}的一条事实，并共同选定下一次是否复核。",
            "when": "饭后或休息充分、没有赶作业时",
            "duration_minutes": duration,
            "steps": [
                "先完整说出开场和倾听话术，不拿报告数据质问孩子。",
                "只核对一条具体记录，让孩子先说；家长不补充推测。",
                "给出‘下次复核’或‘本期先不处理’两个都可接受的选择。",
                "把孩子的选择作为下次观察点，时间到立即结束。",
            ],
            "stop_condition": "孩子说不想谈、明显不耐烦，或达到约定时长时立即停止，不追问。",
            "next_observation": f"下一次学习后只观察孩子是否愿意再次提到{focus_title}，以及是否能说出一个具体步骤；不据沉默作负面判断。",
        }
        if active_habit:
            script.insert(-1, f"你同意的小实验这周只看这一件事：{active_habit.get('child_action', '')}")
            action["steps"].insert(2, f"只问一次小实验是否用上：{active_habit.get('success_signal', '记录孩子愿意说的一条事实')}；不追问原因。")
        sources = [
            {"evidence_ref": item["evidence_ref"], "source": "本期知识学习记录", "occurred_at": item["date"]}
            for item in summary.get("evidence", []) if item.get("evidence_ref")
        ] + [
            {"evidence_ref": item['evidence_ref'], 'source': '本期可靠原作答核对（不等同掌握）', 'occurred_at': item['date']}
            for item in observations if item.get('evidence_ref')
        ] + [
            {key: item[key] for key in ("evidence_ref", "source", "occurred_at") if item.get(key)}
            for item in behavior_intervention_feedback if item.get("evidence_ref")
        ]
        sources = list({item["evidence_ref"]: item for item in sources}.values())
        uncertainty = "本期行为反馈只说明一次已记录的选择；不用于判断情绪、态度、人格或因果。" if behavior_intervention_feedback else "未记录的信息不作推断；阶段报告不把未作答当作不会。"
        if behavior_model_summary.get("uncertainty"):
            uncertainty += " " + str(behavior_model_summary["uncertainty"]).strip()[:500]
        if feedback:
            uncertainty += " 沟通压力仅来自家长对已审核报告的主观体验，不作为任务执行或学习效果的证明。"
        return {"focus": "period_review" if has_evidence else "insufficient_evidence", "scenario": "一起看本期已经发生的记录，只确认下一次需要核对的地方，不对孩子下结论。", "why_this_fits": reason + "。", "evidence_basis": basis, "evidence_refs": [item["evidence_ref"] for item in sources], "evidence_sources": sources, "uncertainty": uncertainty, "script": script, "script_phases": [{"phase": phase, "text": text} for phase, text in zip(["开场", "倾听", "共情", "共同选择", "收尾", "实验支持"], script)], "action": action, "avoid": [{"do_not_say": "这次都对了，以后肯定不会错。", "say_instead": "这次通过说明这个变式做出来了，我们以后再看不同情境。"}, {"do_not_say": "没做就是不会，快补完。", "say_instead": "没记录不能说明不会；先确认有没有机会和需要支持。"}, {"do_not_say": "你看别人早就掌握了。", "say_instead": "我们只看你这次留下的证据和下一步。"}, {"do_not_say": "你到底为什么总在这里出错？", "say_instead": "你愿意说说当时从哪一步开始不确定吗？不想说也可以。"}, {"do_not_say": "再这样下去考试就完了。", "say_instead": "我们今天只决定下一次核对哪一个小步骤。"}], "personalization": {"child_stage": grade, "goal_context": goal, "focus_title": focus_title, "recent_confirmed_facts": [], "feedback_considered": len(feedback), "active_habit_experiment": active_habit, "latest_completed_habit_experiment": completed_habit}, "adaptation": {"basis": "parent_feedback" if feedback else "confirmed_facts", "feedback_count": len(feedback), "note": "最近反馈压力偏高，已缩短沟通时间并设置孩子可随时停止的条件。" if gentle else "阶段报告不安排当晚任务，只帮助家长进行低压力的证据核对。"}}

    def _begin_refresh(self, child_id: str, report_type: str, start: date, end: date) -> None:
        if report_type not in {"weekly", "monthly"}:
            raise InvalidPeriodReportTypeError(report_type)
        now = utc_now()
        with self.database.connect() as connection:
            child = connection.execute(
                "SELECT 1 FROM child_profile WHERE child_id = ?", (child_id,)
            ).fetchone()
            if not child:
                raise UnknownChildError(child_id)
            connection.execute(
                """
                INSERT INTO period_report_refresh_state(
                  child_id, report_type, period_start, period_end, status,
                  attempts, last_error, updated_at
                ) VALUES (?, ?, ?, ?, 'processing', 1, NULL, ?)
                ON CONFLICT(child_id, report_type) DO UPDATE SET
                  period_start = excluded.period_start,
                  period_end = excluded.period_end,
                  status = 'processing',
                  attempts = CASE
                    WHEN period_report_refresh_state.period_start = excluded.period_start
                     AND period_report_refresh_state.period_end = excluded.period_end
                    THEN period_report_refresh_state.attempts + 1
                    ELSE 1
                  END,
                  last_error = NULL,
                  updated_at = excluded.updated_at
                """,
                (child_id, report_type, start.isoformat(), end.isoformat(), now),
            )
            connection.commit()

    def _mark_failed(self, child_id: str, report_type: str, error_code: str) -> None:
        try:
            with self.database.connect() as connection:
                connection.execute(
                    """
                    UPDATE period_report_refresh_state
                    SET status = 'failed', last_error = ?, updated_at = ?
                    WHERE child_id = ? AND report_type = ?
                    """,
                    (error_code[:120], utc_now(), child_id, report_type),
                )
                connection.commit()
        except Exception:
            pass

    def _has_evidence(self, child_id: str, start: date, end: date) -> bool:
        with report_source_connection(self.database) as connection:
            connection.execute('BEGIN')
            daily = connection.execute('SELECT 1 FROM daily_report WHERE child_id=? AND report_date BETWEEN ? AND ? LIMIT 1',
                                       (child_id, start.isoformat(), end.isoformat())).fetchone()
            if daily:
                return True
            effective = self._effective_evidence(connection, child_id)
            if any(start <= self._evidence_date(row['occurred_at']) <= end for row in effective.values()):
                return True
            from .period_written_observations import observations_for_period
            return bool(observations_for_period(connection, child_id, start, end, effective,
                now=datetime.fromisoformat(utc_now())))

    @staticmethod
    def _period_bounds(report_type: str, reference: date) -> tuple[date, date]:
        if report_type == "weekly":
            start = reference - timedelta(days=reference.weekday())
            return start, start + timedelta(days=6)
        if report_type == "monthly":
            start = reference.replace(day=1)
            return start, reference.replace(day=monthrange(reference.year, reference.month)[1])
        raise InvalidPeriodReportTypeError(report_type)


class InvalidPeriodReportTypeError(Exception):
    pass


class UnknownPeriodReportError(Exception):
    pass


class PeriodEvidenceActionConflictError(ValueError):
    pass
