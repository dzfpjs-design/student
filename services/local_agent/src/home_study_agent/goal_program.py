from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
from typing import Any
from uuid import uuid4

from .database import Database
from .family_clock import calendar_as_of, family_time, family_today
from .specialized_program import SpecializedProgramService


PROGRAM_VERSION = "family_goal_program_v1"


class GoalProgramError(ValueError):
    pass


class GoalRuleConflict(GoalProgramError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class GoalProgramService:
    """Turns durable family goals into evidence-backed weekly cycles."""

    DEFAULTS = {
        "child-primary-4": (
            {
                "track": "pet_path",
                "title": "PET 能力成长",
                "target_statement": "在六年级上学期结束前具备参加 PET 考试并争取通过的听说读写能力。",
                "priority": 70,
                "source_type": "pet_daily",
                "minutes": 50,
                "sessions": 5,
                "horizon": {"type": "school_stage", "grade": "六年级", "term": "上学期"},
            },
            {
                "track": "competition_math_path",
                "title": "数学竞赛与奥赛思维",
                "target_statement": "持续发展数感、建模、逻辑、计数和迁移能力，以真实竞赛表现和解题证据检验。",
                "priority": 60,
                "source_type": "competition_math",
                "minutes": 45,
                "sessions": 3,
                "horizon": {"type": "continuous", "milestone": "适龄赛事获奖并形成奥赛思维"},
            },
        ),
        "child-junior-3": (
            {
                "track": "junior_exam_readiness",
                "title": "中考学科能力与薄弱点闭环",
                "target_statement": "在不挤占校内作业和睡眠的前提下，持续修复中考核心学科薄弱点并保留独立迁移证据。",
                "priority": 90,
                "source_type": "junior_exam_goal",
                "minutes": 75,
                "sessions": 5,
                "horizon": {"type": "school_stage", "grade": "初三", "milestone": "中考"},
            },
        ),
    }

    def __init__(self, database: Database):
        self.database = database

    def ensure_schema(self) -> None:
        with self.database.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS goal_program (
                  program_id TEXT PRIMARY KEY,
                  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                  track TEXT NOT NULL,
                  title TEXT NOT NULL,
                  target_statement TEXT NOT NULL,
                  target_horizon_json TEXT NOT NULL,
                  priority INTEGER NOT NULL,
                  source_type TEXT NOT NULL,
                  default_weekly_minutes INTEGER NOT NULL,
                  default_weekly_sessions INTEGER NOT NULL,
                  status TEXT NOT NULL CHECK(status IN ('active', 'paused', 'completed')),
                  origin TEXT NOT NULL,
                  version TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  UNIQUE(child_id, track)
                );
                CREATE TABLE IF NOT EXISTS goal_cycle (
                  cycle_id TEXT PRIMARY KEY,
                  program_id TEXT NOT NULL REFERENCES goal_program(program_id) ON DELETE CASCADE,
                  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                  week_start TEXT NOT NULL,
                  week_end TEXT NOT NULL,
                  target_minutes INTEGER NOT NULL,
                  target_sessions INTEGER NOT NULL,
                  focus_json TEXT NOT NULL,
                  status TEXT NOT NULL CHECK(status IN ('not_started', 'on_track', 'behind', 'completed')),
                  completed_minutes INTEGER NOT NULL DEFAULT 0,
                  completed_sessions INTEGER NOT NULL DEFAULT 0,
                  evidence_watermark_json TEXT NOT NULL DEFAULT '{}',
                  generator TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  UNIQUE(program_id, week_start)
                );
                CREATE INDEX IF NOT EXISTS idx_goal_cycle_child_week
                ON goal_cycle(child_id, week_start, status);
                CREATE TABLE IF NOT EXISTS goal_program_revision (
                  revision_id TEXT PRIMARY KEY,
                  program_id TEXT NOT NULL REFERENCES goal_program(program_id) ON DELETE CASCADE,
                  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                  payload_json TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  client_action_id TEXT NOT NULL UNIQUE
                );
                """
            )
            connection.commit()

    def seed_defaults(self) -> None:
        self.ensure_schema()
        now = _now()
        with self.database.connect() as connection:
            for child_id, programs in self.DEFAULTS.items():
                if not connection.execute(
                    "SELECT 1 FROM child_profile WHERE child_id = ? AND status = 'active'", (child_id,)
                ).fetchone():
                    continue
                for program in programs:
                    connection.execute(
                        """
                        INSERT INTO goal_program(
                          program_id, child_id, track, title, target_statement,
                          target_horizon_json, priority, source_type,
                          default_weekly_minutes, default_weekly_sessions,
                          status, origin, version, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', 'family_default', ?, ?, ?)
                        ON CONFLICT(child_id, track) DO UPDATE SET
                          title = CASE WHEN goal_program.origin = 'family_default' THEN excluded.title ELSE goal_program.title END,
                          target_statement = CASE WHEN goal_program.origin = 'family_default' THEN excluded.target_statement ELSE goal_program.target_statement END,
                          target_horizon_json = CASE WHEN goal_program.origin = 'family_default' THEN excluded.target_horizon_json ELSE goal_program.target_horizon_json END,
                          source_type = CASE WHEN goal_program.origin = 'family_default' THEN excluded.source_type ELSE goal_program.source_type END,
                          version = excluded.version,
                          updated_at = excluded.updated_at
                        WHERE goal_program.origin = 'family_default' AND (
                          goal_program.title != excluded.title OR
                          goal_program.target_statement != excluded.target_statement OR
                          goal_program.target_horizon_json != excluded.target_horizon_json OR
                          goal_program.source_type != excluded.source_type OR
                          goal_program.version != excluded.version
                        )
                        """,
                        (
                            str(uuid4()), child_id, program["track"], program["title"],
                            program["target_statement"], json.dumps(program["horizon"], ensure_ascii=False),
                            program["priority"], program["source_type"], program["minutes"],
                            program["sessions"], PROGRAM_VERSION, now, now,
                        ),
                    )
            connection.commit()

    def cycles(
        self, child_id: str, reference_date: date | None = None, *, _prepare_reviews: bool = False
    ) -> list[dict[str, Any]]:
        self.seed_defaults()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            results = self._cycles_in_connection(connection, child_id, reference_date,
                _prepare_reviews=_prepare_reviews)
            connection.commit()
            return results

    def refresh_existing_cycles(self, connection, child_id: str, *, as_of: datetime):
        """Borrow the snapshot transaction; never seed or create a goal/cycle."""
        if not connection.in_transaction:
            raise RuntimeError("goal snapshot refresh requires caller transaction")
        if not all(connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
                   for table in ('goal_program', 'goal_cycle')):
            return []
        return self._cycles_in_connection(connection, child_id, as_of=as_of, existing_only=True)

    def _cycles_in_connection(self, connection, child_id, reference_date=None, *,
                             _prepare_reviews=False, as_of=None, existing_only=False):
        from .learning_feedback import build_learning_feedback
        now = as_of.isoformat() if as_of is not None else _now()
        instant = datetime.fromisoformat(now)
        current_day = family_today(instant)
        today = reference_date or current_day
        week_start = today - timedelta(days=today.weekday())
        week_end = week_start + timedelta(days=6)
        historical = week_end < current_day
        feedback_now = calendar_as_of(week_end, instant)[1] if historical else instant
        programs = connection.execute(
            "SELECT * FROM goal_program WHERE child_id = ? AND status = 'active' ORDER BY priority DESC",
            (child_id,),
        ).fetchall()
        if not programs and not connection.execute(
            "SELECT 1 FROM child_profile WHERE child_id = ?", (child_id,)
        ).fetchone():
            raise KeyError(child_id)
        if existing_only:
            programs = [p for p in programs if connection.execute(
                'SELECT 1 FROM goal_cycle WHERE program_id=? AND child_id=? AND week_start=?',
                (p['program_id'], child_id, week_start.isoformat())).fetchone()]
        feedback = build_learning_feedback(connection, child_id, now=feedback_now) if programs else None
        if feedback is not None and historical:
            # Rejected future receipts must not churn an already closed week's snapshot.
            basis = {key: feedback[key] for key in ("version", "window_days", "nodes")}
            basis.update({"child_id": child_id, "as_of": feedback_now.isoformat()})
            feedback = {**feedback, "as_of": basis["as_of"], "evidence_fingerprint": hashlib.sha256(
                json.dumps(basis, sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest()}
        for program in programs:
            focus = self._focus(self._learning_outcome(feedback, program["track"]))
            if existing_only:
                focus_json = json.dumps(focus, ensure_ascii=False, sort_keys=True)
                connection.execute(
                    '''UPDATE goal_cycle SET week_end=?,target_minutes=?,target_sessions=?,focus_json=?,updated_at=?
                       WHERE program_id=? AND child_id=? AND week_start=?
                       AND (week_end!=? OR target_minutes!=? OR target_sessions!=? OR focus_json!=?)''',
                    (week_end.isoformat(),program['default_weekly_minutes'],program['default_weekly_sessions'],focus_json,now,
                     program['program_id'],child_id,week_start.isoformat(),week_end.isoformat(),
                     program['default_weekly_minutes'],program['default_weekly_sessions'],focus_json))
                continue
            connection.execute(
                """
                INSERT INTO goal_cycle(
                  cycle_id, program_id, child_id, week_start, week_end,
                  target_minutes, target_sessions, focus_json, status,
                  evidence_watermark_json, generator, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'not_started', '{}', ?, ?, ?)
                ON CONFLICT(program_id, week_start) DO UPDATE SET
                  week_end = excluded.week_end,
                  target_minutes = excluded.target_minutes,
                  target_sessions = excluded.target_sessions,
                  focus_json = excluded.focus_json,
                  updated_at = excluded.updated_at
                WHERE goal_cycle.week_end != excluded.week_end
                   OR goal_cycle.target_minutes != excluded.target_minutes
                   OR goal_cycle.target_sessions != excluded.target_sessions
                   OR goal_cycle.focus_json != excluded.focus_json
                """,
                (
                    str(uuid4()), program["program_id"], child_id,
                    week_start.isoformat(), week_end.isoformat(),
                    program["default_weekly_minutes"], program["default_weekly_sessions"],
                    json.dumps(focus, ensure_ascii=False, sort_keys=True), PROGRAM_VERSION, now, now,
                ),
            )
        rows = connection.execute(
            """
            SELECT gc.*, gp.track, gp.title, gp.target_statement,
                   gp.target_horizon_json, gp.priority, gp.source_type
            FROM goal_cycle gc JOIN goal_program gp ON gp.program_id = gc.program_id
            WHERE gc.child_id = ? AND gc.week_start = ? AND gp.status = 'active'
            ORDER BY gp.priority DESC
            """,
            (child_id, week_start.isoformat()),
        ).fetchall()
        results = []
        program_by_id = {row["program_id"]: dict(row) for row in programs}
        roadmaps = self._constrained_roadmaps(connection, list(program_by_id.values()), today, feedback_now, feedback)
        for row in rows:
            roadmap = roadmaps[row['program_id']]
            args = (connection, {**dict(row), "roadmap": roadmap}, today, self._learning_outcome(feedback, row["track"]))
            result = (self._refresh_progress(*args, prepare_reviews=True)
                      if _prepare_reviews and today == current_day else self._refresh_progress(*args))
            result["roadmap"] = roadmap
            results.append(result)
        return results

    def configure_program(
        self,
        child_id: str,
        track: str,
        *,
        target_statement: str | None,
        target_date: str | None,
        target_score: float | None,
        target_score_label: str | None,
        event_name: str | None,
        event_rules_url: str | None,
        event_version: str | None,
        award_target: str | None,
        registration_deadline: str | None,
        weekly_minutes: int | None,
        weekly_sessions: int | None,
        client_action_id: str,
    ) -> dict[str, Any]:
        if (weekly_minutes is not None and (type(weekly_minutes) is not int or not 15 <= weekly_minutes <= 600)
                or weekly_sessions is not None and (type(weekly_sessions) is not int or not 1 <= weekly_sessions <= 7)):
            raise GoalProgramError("每周预算必须是15到600分钟、1到7次的整数；不自动增加负担。")
        if target_score is not None and (type(target_score) not in (int, float) or not math.isfinite(target_score)):
            raise GoalProgramError("目标分数必须是有限数值。")
        if not isinstance(client_action_id, str) or not client_action_id.strip():
            raise GoalProgramError("目标修改需要操作标识。")
        self.seed_defaults()
        parsed_target_date = None
        if target_date:
            try:
                parsed_target_date = date.fromisoformat(target_date)
            except ValueError as error:
                raise GoalProgramError("目标日期格式无效。") from error
        if target_score is not None and not 0 <= target_score <= 1000:
            raise GoalProgramError("目标分数超出允许范围。")
        event_values = (event_name, event_rules_url, event_version, award_target, registration_deadline)
        if any(value is not None for value in event_values):
            if track != "competition_math_path":
                raise GoalProgramError("赛事配置只适用于数学竞赛目标。")
            if not event_name or not event_rules_url or not event_version or not target_date:
                raise GoalProgramError("赛事配置必须同时提供赛事名称、规则链接、规则版本和比赛日期。")
            if not event_rules_url.startswith(("https://", "http://")):
                raise GoalProgramError("赛事规则必须使用可核对的网页链接。")
        if registration_deadline:
            try:
                parsed_registration = date.fromisoformat(registration_deadline)
            except ValueError as error:
                raise GoalProgramError("报名截止日期格式无效。") from error
            if target_date and parsed_registration > date.fromisoformat(target_date):
                raise GoalProgramError("报名截止日期不能晚于比赛日期。")
        payload = {
            "target_statement": target_statement,
            "target_date": target_date,
            "target_score": target_score,
            "target_score_label": target_score_label,
            "event_name": event_name,
            "event_rules_url": event_rules_url,
            "event_version": event_version,
            "award_target": award_target,
            "registration_deadline": registration_deadline,
            "weekly_minutes": weekly_minutes,
            "weekly_sessions": weekly_sessions,
        }
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        now = _now()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not connection.execute("SELECT 1 FROM child_profile WHERE child_id=? AND status='active'", (child_id,)).fetchone():
                raise GoalProgramError("当前孩子不可用。")
            existing = connection.execute(
                "SELECT program_id, child_id, payload_json FROM goal_program_revision WHERE client_action_id = ?",
                (client_action_id,),
            ).fetchone()
            if existing:
                if existing["child_id"] != child_id or existing["payload_json"] != payload_json:
                    raise GoalProgramError("同一操作标识不能用于不同目标修改。")
                row = connection.execute(
                    "SELECT * FROM goal_program WHERE program_id = ?", (existing["program_id"],)
                ).fetchone()
                if not row or row["track"] != track:
                    raise GoalProgramError("同一操作标识不能改绑另一个目标。")
                return self._program_result(dict(row))
            current_day = family_today(datetime.fromisoformat(now))
            if parsed_target_date is not None and parsed_target_date < current_day:
                raise GoalProgramError("目标日期不能早于今天。")
            if registration_deadline and parsed_registration < current_day:
                raise GoalProgramError("报名截止日期不能早于今天。")
            row = connection.execute(
                "SELECT * FROM goal_program WHERE child_id = ? AND track = ?",
                (child_id, track),
            ).fetchone()
            if not row:
                raise KeyError((child_id, track))
            horizon = json.loads(row["target_horizon_json"])
            first_target = not horizon.get('target_date') and 'rule_binding' not in horizon
            for key, value in (
                ("target_date", target_date),
                ("target_score", target_score),
                ("target_score_label", target_score_label),
                ("event_name", event_name),
                ("event_rules_url", event_rules_url),
                ("event_version", event_version),
                ("award_target", award_target),
                ("registration_deadline", registration_deadline),
            ):
                if value is not None:
                    horizon[key] = value
            if target_date is not None and first_target:
                from .official_goal_rules import rule_snapshot
                # Ordinary edits are not acknowledgement of replacement rules.
                horizon['rule_binding'] = rule_snapshot(track)
            statement = target_statement.strip() if target_statement else row["target_statement"]
            minutes = weekly_minutes if weekly_minutes is not None else row["default_weekly_minutes"]
            sessions = weekly_sessions if weekly_sessions is not None else row["default_weekly_sessions"]
            connection.execute(
                """
                UPDATE goal_program SET target_statement = ?, target_horizon_json = ?,
                  default_weekly_minutes = ?, default_weekly_sessions = ?,
                  origin = 'family_configured', updated_at = ? WHERE program_id = ?
                """,
                (statement, json.dumps(horizon, ensure_ascii=False, sort_keys=True), minutes, sessions, now, row["program_id"]),
            )
            connection.execute(
                """
                INSERT INTO goal_program_revision(
                  revision_id, program_id, child_id, payload_json, created_at, client_action_id
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (str(uuid4()), row["program_id"], child_id, payload_json, now, client_action_id),
            )
            connection.commit()
            updated = connection.execute(
                "SELECT * FROM goal_program WHERE program_id = ?", (row["program_id"],)
            ).fetchone()
        return self._program_result(dict(updated))

    @staticmethod
    def _rule_program(c, child_id, track):
        row = c.execute('''SELECT p.* FROM goal_program p JOIN child_profile ch USING(child_id)
            WHERE p.child_id=? AND p.track=? AND ch.status='active' ''', (child_id, track)).fetchone()
        if not row:
            raise KeyError((child_id, track))
        return dict(row)

    @staticmethod
    def _rule_review(c, row):
        from .official_goal_rules import target_rule_context, _hash
        try:
            horizon = json.loads(row['target_horizon_json'])
            if not isinstance(horizon, dict):
                raise ValueError('not_object')
        except (ValueError, TypeError):
            raise GoalRuleConflict('GOAL_RULE_TARGET_UNAVAILABLE', '原目标记录无法核对，请先修复目标设置。') from None
        revisions = [tuple(v) for v in c.execute('''SELECT revision_id,payload_json FROM goal_program_revision
            WHERE program_id=? ORDER BY revision_id''', (row['program_id'],))]
        child = c.execute('SELECT curriculum_region FROM child_profile WHERE child_id=?', (row['child_id'],)).fetchone()
        rules = target_rule_context(row['track'], horizon, as_of_date=family_today(), curriculum_region=child[0])
        return dict(child_id=row['child_id'], program_id=row['program_id'], track=row['track'],
            program_version=_hash({'program': row, 'revisions': revisions}), rules=rules,
            can_confirm=bool(rules['current_snapshot']) and row['status']=='active',
            local_only=True, cloud_transmission_allowed=False)

    def review_rules(self, child_id, track):
        with self.database.connect() as c:
            c.execute('BEGIN')
            return self._rule_review(c, self._rule_program(c, child_id, track))

    def confirm_rules(self, child_id, track, *, expected_program_version, expected_rule_version,
                      expected_rule_hash, client_action_id):
        from .official_goal_rules import rule_snapshot
        request = dict(operation='confirm_rule_reference', child_id=child_id, track=track,
            expected_program_version=expected_program_version, expected_rule_version=expected_rule_version,
            expected_rule_hash=expected_rule_hash)
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row = self._rule_program(c, child_id, track)
            old = c.execute('SELECT * FROM goal_program_revision WHERE client_action_id=?', (client_action_id,)).fetchone()
            if old:
                try:
                    saved = json.loads(old['payload_json'])
                except (ValueError, TypeError):
                    saved = None
                if (old['program_id'] != row['program_id'] or old['child_id'] != child_id
                        or not isinstance(saved, dict) or saved.get('request') != request):
                    raise GoalRuleConflict('GOAL_RULE_REQUEST_CONFLICT', '这次操作已用于另一份核对，请重新读取后确认。')
                return dict(receipt=saved['receipt'], review=self._rule_review(c, row), replayed=True)
            review = self._rule_review(c, row)
            snapshot = review['rules']['current_snapshot']
            if (not review['can_confirm'] or not snapshot or snapshot['version'] != expected_rule_version
                    or snapshot['snapshot_hash'] != expected_rule_hash):
                raise GoalRuleConflict('GOAL_RULE_SOURCE_CHANGED', '当前规则已变化或不可用，请重新读取规则后核对。')
            if review['program_version'] != expected_program_version:
                raise GoalRuleConflict('GOAL_RULE_PROGRAM_CHANGED', '目标设置已被修改，请重新读取目标和规则后确认。')
            horizon = json.loads(row['target_horizon_json'])
            previous = horizon.get('rule_binding')
            horizon['rule_binding'] = snapshot
            now, revision_id = _now(), str(uuid4())
            # Freeze exact outstanding tasks, including pre-binding legacy plans. No plan is rewritten.
            replaced = [] if previous == snapshot else [v[0] for v in c.execute('''
                SELECT pi.plan_item_id FROM plan_item pi JOIN daily_plan dp USING(plan_id)
                WHERE dp.child_id=? AND pi.source_type=? AND pi.status IN ('pending','active','paused')
                ORDER BY pi.plan_item_id''', (child_id, row['source_type']))]
            receipt = dict(revision_id=revision_id, program_id=row['program_id'], child_id=child_id,
                operation='confirm_rule_reference', actor_type='parent', actor_id='household_parent',
                confirmed_at=now, rule_version=snapshot['version'], rule_hash=snapshot['snapshot_hash'],
                old_rule_hash=previous.get('snapshot_hash') if isinstance(previous, dict) else None,
                public_rule_status=review['rules']['public_rule_status'],
                superseded_plan_item_ids=replaced, exam_readiness_claim_allowed=False)
            c.execute('''UPDATE goal_program SET target_horizon_json=?,updated_at=?,origin='family_configured' WHERE program_id=?''',
                (json.dumps(horizon, ensure_ascii=False, sort_keys=True), now, row['program_id']))
            c.execute('''INSERT INTO goal_program_revision(revision_id,program_id,child_id,payload_json,created_at,client_action_id)
                VALUES(?,?,?,?,?,?)''', (revision_id,row['program_id'],child_id,
                    json.dumps(dict(operation='confirm_rule_reference', request=request, receipt=receipt), ensure_ascii=False, sort_keys=True),now,client_action_id))
            # The source is code-versioned today. Re-read before commit, including injected source replacement.
            if rule_snapshot(track) != snapshot:
                raise GoalRuleConflict('GOAL_RULE_SOURCE_CHANGED', '核对期间规则已变化，本次没有保存，请重新核对。')
            current = self._rule_review(c, self._rule_program(c, child_id, track))
            c.commit()
            return dict(receipt=receipt, review=current, replayed=False)

    def roadmap(self, child_id: str, track: str, reference_date: date | None = None) -> dict[str, Any]:
        from .goal_roadmap import _as_of
        from .learning_feedback import build_learning_feedback
        today, now = _as_of(reference_date)
        with self.database.connect() as connection:
            connection.execute('BEGIN')
            programs = [dict(row) for row in connection.execute("SELECT * FROM goal_program WHERE child_id=? AND (status='active' OR track=?) ORDER BY priority DESC,program_id", (child_id, track))]
            requested = next((row for row in programs if row['track']==track), None)
            if not requested:
                raise KeyError((child_id, track))
            feedback = build_learning_feedback(connection, child_id, now=now)
            result = self._constrained_roadmaps(connection, programs, today, now, feedback)[requested['program_id']]
            from .goal_section_coverage import attach_section_evidence
            return attach_section_evidence(connection, result, as_of=now)

    def _constrained_roadmaps(self, connection, programs, today, feedback_now, feedback):
        from .goal_roadmap import MAX_CALENDAR_WEEKS, build_goal_roadmap
        from .report_followup import ReportFollowupService
        if not programs:
            return {}
        programs = sorted(programs, key=lambda item: (-item['priority'], item['program_id']))
        maps = {p['program_id']: build_goal_roadmap(connection, p, today=today, feedback_now=feedback_now, feedback=feedback) for p in programs}
        child = programs[0]['child_id']
        family = connection.execute('SELECT * FROM family_rule WHERE child_id=?', (child,)).fetchone()
        cap = max(0, int(family['max_learning_minutes'])) if family else 0
        segment = max(0, int(family['task_segment_minutes'])) if family else 0
        break_minutes = max(0, int(family['break_minutes'])) if family else 0
        horizon_limit = today + timedelta(weeks=MAX_CALENDAR_WEEKS)
        ends = {key: min(date.fromisoformat(value['target_date']), horizon_limit) if value['target_date']
                else today + timedelta(days=6-today.weekday()) for key,value in maps.items()}
        end = max(max(ends.values()), today)
        windows = ReportFollowupService(self.database).constraint_windows(child, today.isoformat(), end.isoformat(), connection=connection)
        committed = {}
        track_commitments = {}
        rows = connection.execute("""SELECT dp.plan_date,pi.source_type,pi.source_ref,pi.plan_item_id,
            CASE WHEN pi.status='completed' THEN COALESCE(pi.actual_minutes,pi.expected_minutes) ELSE pi.expected_minutes END AS minutes
            FROM plan_item pi JOIN daily_plan dp USING(plan_id)
            JOIN daily_session ds ON ds.session_id=dp.session_id AND ds.child_id=dp.child_id
            WHERE dp.child_id=? AND dp.plan_date BETWEEN ? AND ?
              AND pi.status IN ('pending','scheduled','active','paused','completed') ORDER BY dp.plan_date,pi.plan_item_id""",
            (child, (today-timedelta(days=today.weekday())).isoformat(), (end+timedelta(days=6-end.weekday())).isoformat())).fetchall()
        for row in rows:
            committed[row['plan_date']] = committed.get(row['plan_date'], 0) + max(0, row['minutes'])
            for p in programs:
                sources = ('junior_exam_goal','mistake_review','due_review') if p['track']=='junior_exam_readiness' else (p['source_type'],)
                if row['source_type'] in sources:
                    day = date.fromisoformat(row['plan_date'])
                    key = (p['program_id'], (day-timedelta(days=day.weekday())).isoformat())
                    info = track_commitments.setdefault(key, {'minutes': 0, 'dates': set()})
                    info['minutes'] += max(0, row['minutes'])
                    info['dates'].add(row['plan_date'])
        fixed = {}
        for row in connection.execute("""SELECT DISTINCT ds.session_id,dp.plan_date,ds.started_at,
            pc.fixed_commitment_minutes FROM session_planning_context pc JOIN daily_session ds USING(session_id)
            LEFT JOIN daily_plan dp ON dp.session_id=ds.session_id AND dp.child_id=ds.child_id
            WHERE ds.child_id=?""", (child,)):
            day = row['plan_date'] or family_today(datetime.fromisoformat(row['started_at'])).isoformat()
            fixed[day] = fixed.get(day,0)+max(0,row['fixed_commitment_minutes'])
        unplanned = {}
        for row in connection.execute("""SELECT ds.started_at,h.expected_minutes,
            (SELECT COALESCE(SUM(pi.expected_minutes),0) FROM plan_item pi JOIN daily_plan dp USING(plan_id)
              WHERE pi.homework_id=h.homework_id AND dp.child_id=ds.child_id AND dp.session_id=ds.session_id) AS allocated
            FROM homework_item h JOIN daily_session ds USING(session_id)
            WHERE ds.child_id=? AND ds.status='active' AND h.status='pending'""", (child,)):
            day = family_today(datetime.fromisoformat(row['started_at'])).isoformat()
            unplanned[day] = unplanned.get(day,0)+max(0,row['expected_minutes']-row['allocated'])
        days = []
        current_time = family_time(datetime.fromisoformat(_now()))
        for offset in range((end-today).days+1):
            day = (today+timedelta(days=offset)).isoformat()
            window = next(row for row in windows if row['start_date'] <= day <= row['end_date'])
            constraints = window['constraints']
            total = min([cap]+[item['value_minutes'] for item in constraints if item['constraint_type']=='daily_total_minutes_cap'])
            single = min([segment]+[item['value_minutes'] for item in constraints if item['constraint_type']=='task_segment_minutes_cap'])
            time_window = None
            if day==current_time.date().isoformat() and family and family['latest_end_time']:
                hour, minute = map(int,family['latest_end_time'].split(':'))
                time_window = min(total,max(0,int((current_time.replace(hour=hour,minute=minute,second=0,microsecond=0)-current_time).total_seconds()//60)))
            free = max(0,total-committed.get(day,0)-fixed.get(day,0)-unplanned.get(day,0))
            if time_window is not None:
                free = min(free,time_window)
            days.append({'date': day, 'total_cap_minutes': total, 'segment_cap_minutes': single,
                'committed_minutes': committed.get(day,0), 'break_minutes': break_minutes,
                'fixed_commitment_minutes': fixed.get(day,0), 'unplanned_school_minutes': unplanned.get(day,0),
                'time_window_minutes': time_window, 'available_minutes': free, 'constraints': constraints,
                'constraint_source_version': window['source_version'], 'allocated_minutes': 0})
        for p in programs:
            result = maps[p['program_id']]
            selected_days = [dict(day) for day in days if day['date'] <= ends[p['program_id']].isoformat()]
            for day in selected_days:
                day['other_goal_reserved_minutes'] = day['allocated_minutes']
            base = result['burden']
            weekly = {}
            for day in selected_days:
                instant = date.fromisoformat(day['date'])
                week = (instant-timedelta(days=instant.weekday())).isoformat()
                existing = track_commitments.get((p['program_id'],week), {'minutes': 0,'dates': set()})
                weekly.setdefault(week, {'remaining': max(0,base['weekly_minutes']-existing['minutes']),
                    'dates': set(existing['dates']), 'used': 0})
                day['week_start'] = week
            capacity = 0
            for week, info in weekly.items():
                eligible = [max(0,day['available_minutes']-day['allocated_minutes']) for day in selected_days
                    if day['week_start']==week and day['date'] in info['dates']]
                others = sorted([max(0,day['available_minutes']-day['allocated_minutes']) for day in selected_days
                    if day['week_start']==week and day['date'] not in info['dates']], reverse=True)
                info['capacity'] = min(info['remaining'],sum(eligible)+sum(others[:max(0,base['weekly_sessions']-len(info['dates']))]))
                capacity += info['capacity']
            allocations, unplaced, finished = [], [], {}
            for node in result['prerequisite_path']:
                previous = None
                blocked = any(finished.get(parent) is None for parent in node['blocked_by'])
                earliest = max([today]+[finished[parent]+timedelta(days=1) for parent in node['blocked_by'] if finished.get(parent)])
                for task in node['steps']:
                    work = {**task, 'knowledge_node_id': node['knowledge_node_id'], 'program_id': p['program_id']}
                    minutes = task['estimated_minutes']['max']
                    if previous and task['minimum_gap_hours']:
                        earliest = max(earliest, previous+timedelta(days=math.ceil(task['minimum_gap_hours']/24)+1))
                    placed = None
                    if not blocked and p['status']=='active':
                        for day in selected_days:
                            if day['date'] < earliest.isoformat():
                                continue
                            week = weekly[day['week_start']]
                            rest = break_minutes if day['committed_minutes'] or day['allocated_minutes'] else 0
                            if (minutes > day['segment_cap_minutes'] or minutes+rest > day['available_minutes']-day['allocated_minutes']
                                    or minutes > week['remaining']-week['used']
                                    or (day['date'] not in week['dates'] and len(week['dates']) >= base['weekly_sessions'])):
                                continue
                            placed = {**work, 'date': day['date'], 'planned_minutes': minutes, 'reserved_break_minutes': rest,
                                'conditional_on_qualified_predecessors': True, 'actual_assignment': False}
                            allocations.append(placed)
                            day['allocated_minutes'] += minutes+rest
                            week['used'] += minutes
                            week['dates'].add(day['date'])
                            previous = date.fromisoformat(day['date'])
                            earliest = previous
                            break
                    if not placed:
                        reason = ('predecessor_unplaced' if blocked else 'deadline_passed' if ends[p['program_id']] < today
                            else 'whole_activity_exceeds_segment_cap' if selected_days and all(minutes>d['segment_cap_minutes'] for d in selected_days)
                            else 'deadline_capacity_gap')
                        unplaced.append({**work, 'reason_code': reason, 'next_action': 'negotiate_with_parent',
                            'reason': '保留完整活动与原估时；核对日期、负担或分解依据，不自动加时、切题或删目标。'})
                        blocked = True
                finished[node['knowledge_node_id']] = None if blocked else previous
            for day in selected_days:
                day['goal_allocated_minutes'] = day['allocated_minutes']-day['other_goal_reserved_minutes']
                next(global_day for global_day in days if global_day['date']==day['date'])['allocated_minutes'] = day['allocated_minutes']
            unplaced_minutes = sum(item['estimated_minutes']['max'] for item in unplaced)
            feasibility = ('unknown' if not base['estimate_complete'] or not result['target_date'] else
                'deadline_passed' if ends[p['program_id']] < today else 'horizon_unverified' if date.fromisoformat(result['target_date']) > horizon_limit else
                'estimated_over_capacity' if base['estimated_minutes']['min'] > capacity else
                'unplaced_requires_negotiation' if unplaced else 'provisional_not_guaranteed')
            projection = {'version': 'goal_daily_constraints_v1', 'base_roadmap_source_version': result['source_version'],
                'child_id': child, 'program_id': p['program_id'], 'reference_date': today.isoformat(), 'target_date': result['target_date'],
                'days': selected_days, 'allocations': allocations, 'unplaced': unplaced,
                'feasibility': feasibility, 'capacity_gap_minutes': max(0,base['estimated_minutes']['max']-capacity),
                'unplaced_minutes': unplaced_minutes, 'requires_parent_negotiation': bool(unplaced) or feasibility!='provisional_not_guaranteed',
                'estimate_only': True, 'actual_assignments_created': False, 'mastery_claim_allowed': False,
                'local_only': True, 'cloud_transmission_allowed': False}
            projection['source_version'] = 'goal_daily_constraints_v1:'+hashlib.sha256(json.dumps(projection,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
            result['daily_decomposition'] = projection
            result['planning_source_version'] = projection['source_version']
            result['source_version_scope'] = 'baseline_graph_and_original_action'
            result['cycles'] = [{'week_start': week, 'week_end': min(date.fromisoformat(week)+timedelta(days=6),ends[p['program_id']]).isoformat(),
                'budget_minutes': info['capacity'], 'planned_minutes': info['used'],
                'allocations': [{'work_id': item['work_id'],'minutes': item['planned_minutes'],'date': item['date']}
                    for item in allocations if week <= item['date'] <= (date.fromisoformat(week)+timedelta(days=6)).isoformat()],
                'status': 'planned' if info['used'] else 'unplaced', 'estimate_only': True} for week,info in weekly.items()]
            milestones = [{'period_start': week['week_start'],'target_date': week['week_end'],
                'work_ids': [item['work_id'] for item in week['allocations']], 'estimated_minutes': week['planned_minutes'],
                'status': 'planned','estimate_only': True,'basis': 'daily_whole_activity_conditional_forecast'}
                for week in result['cycles'] if week['allocations']]
            monthly = {}
            for item in allocations:
                month = item['date'][:7]
                milestone = monthly.setdefault(month, {'month': month,'target_date': item['date'],'work_ids': [],'estimated_minutes': 0,'status': 'planned','estimate_only': True})
                milestone['target_date'] = max(milestone['target_date'],item['date'])
                milestone['work_ids'].append(item['work_id'])
                milestone['estimated_minutes'] += item['planned_minutes']
            result['milestones'] = {'weekly': milestones,'monthly': list(monthly.values())}
            result['unconstrained_burden'] = dict(base)
            result['burden'] = {**base, 'available_minutes': capacity, 'unallocated_minutes': unplaced_minutes,
                'feasibility': feasibility, 'capacity_basis': 'daily_parent_caps_family_rules_and_existing_commitments'}
            if projection['requires_parent_negotiation'] and result['status'] not in {'blocked','paused','needs_clarification'}:
                result['status'] = 'needs_negotiation'
            action = result['next_daily_action']
            if action:
                current = selected_days[0] if selected_days else None
                allowed = bool(current and action['minutes'] <= current['segment_cap_minutes']
                    and action['minutes']+(break_minutes if current['committed_minutes'] else 0) <= current['available_minutes']-current['other_goal_reserved_minutes'])
                result['next_daily_action'] = {**action, 'daily_decomposition_source_version': projection['source_version'],
                    'dispatchable': action['dispatchable'] and allowed,
                    'status': action['status'] if allowed else 'unplaced_parent_negotiation'}
        return maps

    def prepare_daily_action(self, child_id: str, reference_date: date | None = None) -> dict[str, Any] | None:
        """Explicit planning command; unlike GET suggestions it may prepare a review."""
        return self.next_daily_action(child_id, reference_date, _prepare_reviews=True, _include_source_gaps=True)

    def next_daily_action(self, child_id: str, reference_date: date | None = None, *,
                          _prepare_reviews: bool = False, _include_source_gaps: bool = False) -> dict[str, Any] | None:
        current_day = family_today(datetime.fromisoformat(_now()))
        today = reference_date or current_day
        cycles = self.cycles(child_id, today, _prepare_reviews=_prepare_reviews and today == current_day)
        for cycle in cycles:
            if cycle["scheduling_status"] != "ready":
                continue
            if cycle["source_type"] != "junior_exam_goal":
                continue
            if cycle.get("execution"):
                # The existing planner reads due_review sources after this call.
                # Returning a generic goal item would lose the exact source and duplicate it.
                continue
            action = cycle["roadmap"]["next_daily_action"]
            if not action or (not action["dispatchable"] and not
                    (_include_source_gaps and action['status'] == 'qualified_source_needed')):
                continue
            return {**action, "cycle_id": cycle["cycle_id"], "task_type": "test_prep", "cognitive_load": "medium",
                    "target_horizon": cycle['target_horizon'],
                    "participation_status": cycle["participation_status"], "learning_outcome": cycle["learning_outcome"],
                    "budget_status": cycle["budget_status"], "scheduling_status": cycle["scheduling_status"],
                    "scheduling_reason": cycle["scheduling_reason"], "roadmap_source_version": cycle["roadmap"]["source_version"]}
        return None

    def _refresh_progress(
        self, connection, cycle: dict[str, Any], today: date, learning_outcome: dict[str, Any],
        *, prepare_reviews: bool = False,
    ) -> dict[str, Any]:
        source_types = {
            "pet_path": ("pet_daily",),
            "competition_math_path": ("competition_math",),
            "junior_exam_readiness": ("junior_exam_goal", "mistake_review", "due_review"),
        }.get(cycle["track"], (cycle["source_type"],))
        placeholders = ",".join("?" for _ in source_types)
        progress = connection.execute(
            f"""
            SELECT COALESCE(SUM(COALESCE(pi.actual_minutes, pi.expected_minutes)), 0) AS minutes,
                   COUNT(DISTINCT dp.session_id) AS sessions,
                   COUNT(pi.plan_item_id) AS evidence_items,
                   MAX(pi.completed_at) AS latest
            FROM plan_item pi JOIN daily_plan dp ON dp.plan_id = pi.plan_id
            WHERE dp.child_id = ? AND dp.plan_date BETWEEN ? AND ?
              AND pi.source_type IN ({placeholders}) AND pi.status = 'completed'
            """,
            (cycle["child_id"], cycle["week_start"], cycle["week_end"], *source_types),
        ).fetchone()
        completed_minutes = int(progress["minutes"] or 0)
        completed_sessions = int(progress["sessions"] or 0)
        elapsed = min(1.0, max(1, (today - date.fromisoformat(cycle["week_start"])).days + 1) / 7)
        ratio = (
            completed_minutes / max(1, cycle["target_minutes"])
            + completed_sessions / max(1, cycle["target_sessions"])
        ) / 2
        if completed_minutes >= cycle["target_minutes"] and completed_sessions >= cycle["target_sessions"]:
            status = "completed"
        elif not completed_minutes and not completed_sessions:
            status = "not_started" if elapsed <= 1 / 7 else "behind"
        else:
            status = "on_track" if ratio >= elapsed * .75 else "behind"
        participation_status = "quota_met" if status == "completed" else status
        remaining_minutes = max(0, cycle["target_minutes"] - completed_minutes)
        remaining_sessions = max(0, cycle["target_sessions"] - completed_sessions)
        budget_status = "exhausted" if not remaining_minutes else "available"
        if status == "completed":
            scheduling_status = "quota_met"
            scheduling_reason = "本周活动分钟和次数目标已达到，不再自动加时；参与达标不代表学习目标达成。"
        elif not remaining_minutes:
            scheduling_status = "budget_exhausted"
            scheduling_reason = f"本周分钟预算已用尽，参与次数尚差{remaining_sessions}次；不自动加时，需家长调整节奏或下周继续，学习结果另看有效证据。"
        else:
            scheduling_status = "ready"
            scheduling_reason = (
                "参与次数目标已达到，但分钟预算仍有余量；可在剩余预算内安排证据检查，不因次数达标宣称学习完成。"
                if not remaining_sessions else "可在剩余分钟预算内继续安排；活动参与和学习证据分别记录。"
            )
        participation = {
            "participation_status": participation_status,
            "status_scope": "participation_only",
            "budget_status": budget_status,
            "scheduling_status": scheduling_status,
            "scheduling_reason": scheduling_reason,
        }
        execution = None
        if scheduling_status == "ready" and learning_outcome["evidence_scope"] != "as_of_week_end":
            execution = self._feedback_execution(
                connection, cycle, learning_outcome, remaining_minutes, prepare=prepare_reviews, roadmap=cycle.get("roadmap")
            )
            if execution:
                participation.update(execution_status=execution["status"], scheduling_reason=execution["reason"])
        # Legacy evidence_items counts completed activities, not qualified answers.
        watermark = {
            "evidence_items": int(progress["evidence_items"] or 0), "latest": progress["latest"],
            **participation, "learning_outcome": learning_outcome,
            "execution": execution,
            "daily_decomposition_source_version": (cycle.get('roadmap') or {}).get('planning_source_version'),
            "planning_feasibility": ((cycle.get('roadmap') or {}).get('daily_decomposition') or {}).get('feasibility'),
        }
        watermark_json = json.dumps(watermark, ensure_ascii=False, sort_keys=True)
        updated_at = _now()
        updated = connection.execute(
            """
            UPDATE goal_cycle SET completed_minutes = ?, completed_sessions = ?,
              status = ?, evidence_watermark_json = ?, updated_at = ? WHERE cycle_id = ?
              AND (
                completed_minutes != ? OR completed_sessions != ? OR status != ?
                OR evidence_watermark_json != ?
              )
            """,
            (
                completed_minutes, completed_sessions, status,
                watermark_json, updated_at, cycle["cycle_id"],
                completed_minutes, completed_sessions, status, watermark_json,
            ),
        )
        if updated.rowcount:
            cycle["updated_at"] = updated_at
        target_horizon = json.loads(cycle.pop("target_horizon_json"))
        deadline = self._deadline_status(target_horizon, today)
        cycle.update({
            "completed_minutes": completed_minutes,
            "completed_sessions": completed_sessions,
            "remaining_minutes": remaining_minutes,
            "remaining_sessions": remaining_sessions,
            "status": status,
            **participation,
            "learning_outcome": learning_outcome,
            "execution": execution,
            "focus": json.loads(cycle.pop("focus_json")),
            "target_horizon": target_horizon,
            "deadline": deadline,
            "evidence_watermark": watermark,
        })
        cycle.pop("evidence_watermark_json", None)
        return cycle

    @staticmethod
    def _feedback_execution(connection, cycle, outcome, remaining_minutes, *, prepare=False, roadmap=None):
        from .goal_question_inventory import practice_item

        if cycle["track"] != "junior_exam_readiness":
            return None
        # Read and write selection share the transaction's current prerequisite route.
        action = (roadmap or {}).get("next_daily_action")
        if not action:
            if roadmap and any(r["code"] == "remaining_budget_below_check_minimum" for r in roadmap["risks"]):
                from .goal_roadmap import next_ready_node
                ready = next_ready_node(roadmap["prerequisite_path"])
                if ready and ready["steps"][0]["action"] in {"clarify_then_recheck", "fade_hint_then_check"}:
                    return {"status": "budget_limited", "source_type": "due_review", "source_ref": None,
                            "knowledge_node_id": ready["knowledge_node_id"], "diagnostic_item_id": None,
                            "roadmap_source_version": roadmap["source_version"], "cloud_transmission_allowed": False,
                            "reason": "当前前置步骤的保守估算超出剩余周预算，不自动加时或创建复测。"}
            return None
        if action["action"] not in {
            "clarify_then_recheck", "fade_hint_then_check",
        }:
            return None
        prepare = prepare and action["dispatchable"]
        node = next((item for item in roadmap["prerequisite_path"]
                     if item["knowledge_node_id"] == action["knowledge_node_id"] and not item["blocked_by"]), None)
        if not node:
            return None
        child, node_id, now = cycle["child_id"], node["knowledge_node_id"], _now()
        route = {"status": "supply_gap", "source_type": "due_review", "source_ref": None,
                 "knowledge_node_id": node_id, "diagnostic_item_id": None,
                 "roadmap_source_version": roadmap["source_version"], "action": action["action"],
                 "cloud_transmission_allowed": False}

        def result(status, reason, **details):
            return {**route, "status": status, "reason": reason, **details}

        calendar = (roadmap or {}).get('daily_decomposition') or {}
        daily = next(iter(calendar.get('days') or []), None)
        if (action.get('status')=='unplaced_parent_negotiation' or (daily and
                (daily['segment_cap_minutes'] < 10 or daily['available_minutes']-daily['other_goal_reserved_minutes'] < 10))):
            return result('budget_limited', '当前批准的每日或单段上限放不下完整动作；保留原题和复测，不切题、加时或重复创建，先与家长协商。',
                daily_decomposition_source_version=calendar.get('source_version'), next_action='negotiate_with_parent')
        if remaining_minutes < 10:
            return result("budget_limited", "可靠薄弱点需要复测，但现有复测每项预留10分钟，剩余周预算不足；不自动加时，待调整节奏。")
        sources = connection.execute("""SELECT evidence_id FROM learning_evidence
            WHERE child_id=? AND knowledge_node_id=? ORDER BY occurred_at DESC,evidence_id""", (child, node_id)).fetchall()
        valid_refs = set(node["evidence_refs"])
        source = next((row[0] for row in sources if row[0] in valid_refs), None)
        if not source:
            return result("supply_gap", "薄弱点的有效作答来源需要重新核实，暂不安排泛化任务代替复测。")

        def qualified_fresh(item_id, *, allow_recent=False):
            qualified = practice_item(connection, child, item_id, datetime.fromisoformat(now), allow_recent=allow_recent)
            return bool(qualified and qualified['knowledge_node_id'] == node_id)

        reviews = connection.execute("""SELECT * FROM review_task WHERE child_id=? AND knowledge_node_id=?
            AND status='pending' ORDER BY (julianday(due_at)<=julianday(?)) DESC,
            (interval_label='next_day') DESC,julianday(due_at),review_task_id""", (child, node_id, now)).fetchall()
        review = reviews[0] if reviews else None
        from .school_open_reviews import INTERVALS
        school_original = connection.execute('''SELECT 1 FROM diagnostic_attempt a
            JOIN diagnostic_item i USING(diagnostic_item_id) JOIN knowledge_node n USING(knowledge_node_id)
            WHERE a.evidence_id=? AND a.child_id=? AND n.track='school'
              AND i.answer_type='open_response' AND i.assessment_mode='open_response_rubric' ''',
            (source, child)).fetchone()
        if school_original:
            review = next((r for r in reviews if r['interval_label'] in INTERVALS
                           and r['source_evidence_id'] == source), None)
        if school_original or (review and review['interval_label'] in INTERVALS):
            # Stage-bound original observations never enter generic question rebinding.
            if not review or review['interval_label'] not in INTERVALS or review['source_evidence_id'] not in valid_refs:
                return result('supply_gap', '学校原稿需要对应已审核迁移题；保留原观察，不另建通用复测。')
            from .repository import StudyRepository, PlanInputError
            try:
                StudyRepository._validate_review_source(connection, review['review_task_id'], child,
                    allowed_statuses=('pending',))
            except PlanInputError:
                return result('supply_gap', '学校复习的原稿或阶段题源已变化，请核对原任务，不自动换题。',
                    source_ref=review['review_task_id'])
            details = dict(source_ref=review['review_task_id'], source_evidence_id=review['source_evidence_id'],
                           diagnostic_item_id=review['diagnostic_item_id'])
            active = connection.execute('''SELECT 1 FROM plan_item pi JOIN daily_plan dp USING(plan_id)
                WHERE dp.child_id=? AND pi.source_type='due_review' AND pi.source_ref=?
                  AND (pi.started_at IS NOT NULL OR pi.status IN ('active','paused','completed')) LIMIT 1''',
                (child, review['review_task_id'])).fetchone()
            if active:
                return result('review_in_progress', '学校复习已进入执行，保留原题和进度，不重复安排。', **details)
            if datetime.fromisoformat(review['due_at']) > datetime.fromisoformat(now):
                return result('review_waiting', '保留学校原稿对应的复习任务，尚未到期，不提前或重复安排。',
                    due_at=review['due_at'], **details)
            if remaining_minutes < 12 or (daily and (daily['segment_cap_minutes'] < 12 or
                    daily['available_minutes'] - daily['other_goal_reserved_minutes'] < 12)):
                return result('budget_limited', '学校原稿复查需完整12分钟，保留原任务，不加时或切题。', **details)
            return result('review_scheduled', '复用学校原稿已绑定的独立复查，保留原题、时间与进度。', **details)
        if review:
            if review["source_evidence_id"] not in valid_refs:
                return result("supply_gap", "已有复测的来源不在当前有效证据中，需要先核实；不重复创建任务。", source_ref=review["review_task_id"])
            try:
                due = datetime.fromisoformat(review["due_at"])
                if due.tzinfo is None:
                    raise ValueError("review time needs a timezone")
            except (TypeError, ValueError):
                return result("supply_gap", "已有复测的到期时间无效，先核实排程；不自动创建替代任务。", source_ref=review["review_task_id"])
            locked = connection.execute("""SELECT 1 FROM plan_item pi JOIN daily_plan dp USING(plan_id)
                WHERE dp.child_id=? AND pi.source_type='due_review' AND pi.source_ref=?
                  AND (pi.started_at IS NOT NULL OR pi.status IN ('active','paused','completed')) LIMIT 1""",
                (child, review["review_task_id"])).fetchone()
            if locked:
                if not qualified_fresh(review["diagnostic_item_id"], allow_recent=True):
                    return result("supply_gap", "执行中的复测题源已无法确认资格，需核实后恢复；不覆盖原题或新增替代任务。", source_ref=review["review_task_id"])
                return result("review_in_progress", "已有同知识点复测进入执行，保留原题和进度，不重复安排或中途换题。",
                              source_ref=review["review_task_id"], diagnostic_item_id=review["diagnostic_item_id"])
            if due > datetime.fromisoformat(now):
                return result("review_waiting", "已有同知识点复测尚未到期，保留原时间，不提前或重复安排。",
                              source_ref=review["review_task_id"], due_at=review["due_at"])
            source = review["source_evidence_id"]

        candidate = review["diagnostic_item_id"] if review and qualified_fresh(review["diagnostic_item_id"]) else None
        if candidate is None:
            items = connection.execute("""SELECT di.diagnostic_item_id FROM diagnostic_item di
                JOIN knowledge_node kn USING(knowledge_node_id)
                WHERE kn.knowledge_node_id=? AND di.status='active'
                ORDER BY di.difficulty,di.diagnostic_item_id""", (node_id,)).fetchall()
            candidate = next((row[0] for row in items if qualified_fresh(row[0])), None)
        if candidate is None:
            return result("supply_gap", "可靠薄弱点暂无同知识点、已合格且24小时内未重复的复测题；需补充合格题源，不用其他学科或泛化专项替代。",
                          source_ref=review["review_task_id"] if review else None, source_evidence_id=source)

        review_id = review["review_task_id"] if review else None
        if not review:
            previous = connection.execute("SELECT status FROM review_task WHERE source_evidence_id=? AND interval_label='immediate'", (source,)).fetchone()
            if previous:
                return result("review_already_handled", "该来源的即时复测已有处理记录，不重复建立；等待新的有效作答或人工核对。")
        if prepare:
            if review and candidate != review["diagnostic_item_id"]:
                connection.execute("UPDATE review_task SET diagnostic_item_id=? WHERE review_task_id=?", (candidate, review_id))
            elif not review:
                review_id = str(uuid4())
                connection.execute("""INSERT INTO review_task(review_task_id,child_id,knowledge_node_id,
                    diagnostic_item_id,source_evidence_id,interval_label,due_at,status,created_at)
                    VALUES (?,?,?,?,?,'immediate',?,'pending',?)""", (review_id, child, node_id, candidate, source, now, now))
            if not review or candidate != review["diagnostic_item_id"]:
                connection.execute("""INSERT INTO audit_event(audit_id,actor,action,child_id,summary_json,occurred_at)
                    VALUES (?,'system','goal_review_bound',?,?,?)""", (str(uuid4()), child, json.dumps({
                        "cycle_id": cycle["cycle_id"], "review_task_id": review_id,
                        "source_evidence_id": source, "diagnostic_item_id": candidate,
                        "previous_diagnostic_item_id": review["diagnostic_item_id"] if review else None,
                        "evidence_fingerprint": outcome["evidence_fingerprint"], "cloud_transmission_allowed": False,
                        "roadmap_source_version": roadmap["source_version"], "knowledge_node_id": node_id,
                    }, sort_keys=True), now))
        return result("review_scheduled" if prepare or (review and candidate == review["diagnostic_item_id"]) else "review_ready",
                      "优先通过已有到期复测路径执行同知识点的合格题，保留真实作答来源；不再额外安排泛化专项。",
                      source_ref=review_id, source_evidence_id=source, diagnostic_item_id=candidate)

    @staticmethod
    def _program_result(program: dict[str, Any]) -> dict[str, Any]:
        result = dict(program)
        result["target_horizon"] = json.loads(result.pop("target_horizon_json"))
        return result

    @staticmethod
    def _deadline_status(horizon: dict[str, Any], today: date) -> dict[str, Any] | None:
        target_date = horizon.get("target_date")
        if not target_date:
            return None
        try:
            deadline = date.fromisoformat(str(target_date))
        except ValueError:
            return {"status": "invalid", "target_date": target_date}
        days_remaining = (deadline - today).days
        return {
            "status": "overdue" if days_remaining < 0 else "due_today" if days_remaining == 0 else "active",
            "target_date": deadline.isoformat(),
            "days_remaining": days_remaining,
            "weeks_remaining": round(max(0, days_remaining) / 7, 1),
            "target_score": horizon.get("target_score"),
            "target_score_label": horizon.get("target_score_label"),
        }

    @staticmethod
    def _learning_outcome(feedback: dict[str, Any], track: str) -> dict[str, Any]:
        evidence_track = {
            "junior_exam_readiness": "school", "pet_path": "pet",
            "competition_math_path": "competition_math",
        }.get(track, track)
        priority = {
            "recheck_needed": 0, "supported_only": 1, "insufficient_evidence": 2,
            "independent_observed": 3, "retention_observed": 4,
        }
        nodes = sorted(
            (node for node in feedback["nodes"] if node["track"] == evidence_track),
            key=lambda node: (priority[node["status"]], node.get("latest_evidence_at", ""), node["knowledge_node_id"]),
        )
        first = nodes[0] if nodes else None
        return {
            "status": first["status"] if first else "insufficient_evidence",
            "next_action": first["next_action"] if first else "needs_baseline",
            "reason": (
                first["reason"] if first else "该目标方向尚无有效作答证据，需要基线诊断；活动时长和次数不能证明学习结果。"
            ),
            "track": evidence_track,
            "evidence_version": feedback["version"],
            "evidence_fingerprint": feedback["evidence_fingerprint"],
            "window_days": feedback.get("window_days", 90),
            "evidence_scope": "as_of_week_end" if feedback.get("as_of") else "current_valid_evidence",
            "as_of": feedback.get("as_of"),
            "goal_attainment": "not_established",
            "mastery_claim_allowed": False,
            "cloud_transmission_allowed": False,
            "nodes": nodes,
        }

    @staticmethod
    def _focus(outcome: dict[str, Any]) -> dict[str, Any]:
        explanation = {
            "cloud_transmission_allowed": False,
            "evidence_version": outcome["evidence_version"],
            "evidence_fingerprint": outcome["evidence_fingerprint"],
            "window_days": outcome["window_days"],
            "evidence_scope": outcome["evidence_scope"],
            "as_of": outcome["as_of"],
        }
        if outcome["nodes"]:
            return {**outcome["nodes"][0], **explanation, "basis": "learning_feedback"}
        return {
            "subject": {"pet": "英语", "competition_math": "数学"}.get(outcome["track"], "综合"),
            "title": None, "basis": "baseline_needed", "status": "needs_baseline",
            "track": outcome["track"], "reason": outcome["reason"],
            "next_action": "needs_baseline", "evidence_refs": [], **explanation,
        }
