from __future__ import annotations

from contextlib import contextmanager, nullcontext
from datetime import date, datetime, timedelta, timezone
from functools import wraps
import hashlib
import json
import platform
import sqlite3
import sys
from typing import Callable
from uuid import uuid4

from .database import Database, SESSION_TRANSITION_TARGETS
from .family_clock import family_time, family_today
from .curriculum_selection import current_curriculum_predicate
from .goal_program import GoalProgramService
from .habit_plan_completion import academic_completion_item
from .pet_vocabulary import PetVocabularyService
from .specialized_program import SpecializedProgramService


def _guard_daily_report_source(function):
    @wraps(function)
    def guarded(self, *args, **kwargs):
        from .parent_reports import _report_effect_guard
        with _report_effect_guard(self.database):
            return function(self, *args, **kwargs)
    return guarded


RECALL_NODE_HINTS = {
    "node-j3-math-discriminant": ("判别式", "实数根", "根的数量"),
    "node-j3-math-quadratic-formula": ("求根公式", "一元二次方程", "解二次方程"),
}


def _plan_metadata_for_display(value):
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        parsed = None
    if isinstance(parsed, dict):
        return parsed
    return {'status': 'unavailable', 'reason_code': 'PLAN_METADATA_INVALID',
            'local_only': True, 'cloud_transmission_allowed': False}


def coaching_evidence_ref(child_id: str, source_kind: str, source_id: str) -> str:
    """Stable, opaque reference for a parent-readable evidence source.

    The source id remains local to the database; reports only expose this
    deterministic reference together with a human-readable source and time.
    """
    digest = hashlib.sha256(f"{child_id}:{source_kind}:{source_id}".encode("utf-8")).hexdigest()[:20]
    return f"evidence-{digest}"

COMPLETION_CHECKS_BY_TASK_TYPE = {
    "written": ("task_finished", "missing_items_checked"),
    "memorization": ("recall_attempted", "uncertain_parts_marked"),
    "reading": ("range_completed", "main_idea_recalled"),
    "correction": ("correction_finished", "error_cause_explained"),
    "review": ("review_completed", "self_tested"),
    "preview": ("preview_completed", "question_recorded"),
    "test_prep": ("materials_reviewed", "uncertainty_marked"),
    "reasoning": ("attempt_completed", "reasoning_explained"),
    "speaking": ("spoken_response_recorded", "task_points_covered"),
    "writing": ("writing_submitted", "task_points_covered"),
}

PET_DAILY_ACTIVITIES = (
    {
        "diagnostic_item_id": "diag-pet-daily-dialogue-1",
        "title": "英语对话 · 约一次周末学习",
        "completion_standard": "听问题后用英语回应，说明时间、地点和学习内容，并反问一个问题",
        "task_type": "speaking",
    },
    {
        "diagnostic_item_id": "diag-pet-daily-story-create-1",
        "title": "故事共创 · 给开头续写下一段",
        "completion_standard": "续写35-55个英文词，包含一个行动、一个小问题和一个结尾",
        "task_type": "writing",
    },
    {
        "diagnostic_item_id": "diag-pet-daily-retell-1",
        "title": "故事复述 · 说清人物、经过和结果",
        "completion_standard": "听或读短故事后，用英语复述人物、主要经过和结果",
        "task_type": "speaking",
    },
    {
        "diagnostic_item_id": "diag-pet-reading-1",
        "title": "分级阅读 · 找主旨和关键信息",
        "completion_standard": "读完短消息，回答写作目的并指出至少两条关键信息",
        "task_type": "reading",
    },
    {
        "diagnostic_item_id": "diag-pet-writing-1",
        "title": "短写作 · 写一封真实目的邮件",
        "completion_standard": "写35-55个英文词，覆盖地点、时间和学习内容三个任务点",
        "task_type": "writing",
    },
)
PET_VOCABULARY_SOURCE_PREFIX = "pet-vocabulary-due:"
PET_VOCABULARY_MAX_DAILY_CARDS = 12

DRAFT_STATES = {
    "check_in": {"check_in"},
    "classroom_recall": {"classroom_recall"},
    "homework_capture": {"homework_capture"},
    "study": {"study", "break"},
    "reflection": {"reflection"},
    "agent_command": {"check_in", "classroom_recall", "homework_capture", "plan_review", "study", "break", "reflection"},
}
MAX_DRAFT_BYTES = 12_000
SENSOR_DEVICE_TYPES = {"camera", "document_camera", "microphone"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def windows_release_runtime_evidence() -> tuple[str, str] | None:
    if sys.platform != "win32":
        return None
    return "Windows", platform.version()[:120] or "unknown"


class StudyRepository:
    def __init__(self, database: Database):
        self.database = database

    @contextmanager
    def _write_transaction(self, connection=None):
        if connection is not None:
            if not connection.in_transaction:
                raise ValueError('borrowed write requires an active transaction')
            yield connection
            return
        with self.database.connect() as owned:
            owned.execute('BEGIN IMMEDIATE')
            try:
                yield owned
                owned.commit()
            except BaseException:
                owned.rollback()
                raise

    def seed_family(self) -> None:
        now = utc_now()
        children = (
            ("child-junior-3", "初三同学", "初三", "江苏南京", "active", now, now),
            ("child-primary-4", "四年级同学", "四年级", "江苏南京", "active", now, now),
        )
        with self.database.connect() as connection:
            connection.executemany(
                """
                INSERT OR IGNORE INTO child_profile(
                  child_id, nickname, grade, curriculum_region, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                children,
            )
            connection.executemany(
                """
                INSERT OR IGNORE INTO family_rule(
                  child_id, latest_end_time, max_learning_minutes, task_segment_minutes,
                  break_minutes, pet_min_minutes, competition_minutes, version, updated_at
                ) VALUES (?, NULL, ?, ?, 5, ?, ?, 1, ?)
                """,
                (
                    ("child-junior-3", 120, 35, 0, 0, now),
                    ("child-primary-4", 90, 25, 10, 15, now),
                ),
            )
            connection.commit()

    def recover_interrupted_model_calls(self) -> int:
        with self.database.connect() as connection:
            recovered = connection.execute(
                """
                UPDATE model_call
                SET status = 'failed', error_code = 'APP_RESTART',
                    verification_status = 'rejected', safety_status = 'degraded',
                    completed_at = ?
                WHERE status = 'started'
                """,
                (utc_now(),),
            ).rowcount
            connection.commit()
            return recovered

    def list_children(self) -> list[dict]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT child_id, nickname, grade, curriculum_region, status
                FROM child_profile WHERE status = 'active' ORDER BY created_at
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def has_existing_learning_history(self) -> bool:
        """A seeded profile alone is not evidence that a family used the app."""
        with self.database.connect() as connection:
            return connection.execute('SELECT 1 FROM daily_session LIMIT 1').fetchone() is not None

    def weekly_goals(self, child_id: str, reference_date: date | None = None) -> list[dict]:
        today = reference_date or family_today(datetime.fromisoformat(utc_now()))
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            child = connection.execute(
                """
                SELECT cp.grade, fr.pet_min_minutes, fr.competition_minutes
                FROM child_profile cp JOIN family_rule fr ON fr.child_id = cp.child_id
                WHERE cp.child_id = ? AND cp.status = 'active'
                """,
                (child_id,),
            ).fetchone()
            if not child:
                connection.rollback()
                raise UnknownChildError(child_id)
            goals = self._ensure_weekly_goals(connection, child_id, child, today)
            connection.commit()
        return goals

    @staticmethod
    def _ensure_weekly_goals(connection, child_id: str, child, today: date) -> list[dict]:
        if child["grade"] != "四年级":
            return []
        week_start = today - timedelta(days=today.weekday())
        week_end = week_start + timedelta(days=6)
        now = utc_now()
        specifications = [
            ("pet", int(child["pet_min_minutes"]) * 5, 5),
            ("competition_math", int(child["competition_minutes"]) * 3, 3),
        ]
        try:
            configured = connection.execute(
                """
                SELECT track, default_weekly_minutes, default_weekly_sessions
                FROM goal_program
                WHERE child_id = ? AND status = 'active'
                  AND track IN ('pet_path', 'competition_math_path')
                """,
                (child_id,),
            ).fetchall()
        except sqlite3.OperationalError:
            configured = []
        configured_by_track = {
            "pet" if row["track"] == "pet_path" else "competition_math": (
                int(row["default_weekly_minutes"]), int(row["default_weekly_sessions"])
            )
            for row in configured
        }
        specifications = [
            (track, *configured_by_track.get(track, (target_minutes, target_sessions)))
            for track, target_minutes, target_sessions in specifications
        ]
        for track, target_minutes, target_sessions in specifications:
            if target_minutes <= 0:
                connection.execute(
                    "DELETE FROM weekly_goal WHERE child_id = ? AND week_start = ? AND track = ?",
                    (child_id, week_start.isoformat(), track),
                )
                continue
            connection.execute(
                """
                INSERT INTO weekly_goal(
                  weekly_goal_id, child_id, week_start, week_end, track, target_minutes,
                  target_sessions, status, generator, knowledge_version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'not_started', 'weekly_goal_rules_v1',
                          'starter_v2', ?, ?)
                ON CONFLICT(child_id, week_start, track) DO UPDATE SET
                  week_end = excluded.week_end,
                  target_minutes = excluded.target_minutes,
                  target_sessions = excluded.target_sessions,
                  updated_at = excluded.updated_at
                """,
                (
                    str(uuid4()), child_id, week_start.isoformat(), week_end.isoformat(),
                    track, target_minutes, target_sessions, now, now,
                ),
            )
        rows = connection.execute(
            """
            SELECT weekly_goal_id, child_id, week_start, week_end, track, target_minutes,
                   target_sessions, status, generator, knowledge_version, created_at, updated_at
            FROM weekly_goal WHERE child_id = ? AND week_start = ?
            ORDER BY CASE track WHEN 'pet' THEN 1 ELSE 2 END
            """,
            (child_id, week_start.isoformat()),
        ).fetchall()
        results = []
        for row in rows:
            source_type = "pet_daily" if row["track"] == "pet" else "competition_math"
            progress = connection.execute(
                """
                SELECT COALESCE(SUM(COALESCE(pi.actual_minutes, pi.expected_minutes)), 0) AS completed_minutes,
                       COUNT(DISTINCT dp.session_id) AS completed_sessions
                FROM plan_item pi JOIN daily_plan dp ON dp.plan_id = pi.plan_id
                WHERE dp.child_id = ? AND dp.plan_date BETWEEN ? AND ?
                  AND pi.source_type = ? AND pi.status = 'completed'
                """,
                (child_id, row["week_start"], row["week_end"], source_type),
            ).fetchone()
            completed_minutes = int(progress["completed_minutes"] or 0)
            completed_sessions = int(progress["completed_sessions"] or 0)
            minute_ratio = completed_minutes / max(1, row["target_minutes"])
            session_ratio = completed_sessions / max(1, row["target_sessions"])
            elapsed_ratio = min(1.0, max(1, (today - week_start).days + 1) / 7)
            if completed_minutes >= row["target_minutes"] and completed_sessions >= row["target_sessions"]:
                status = "completed"
            elif completed_minutes == 0 and completed_sessions == 0:
                status = "not_started" if today == week_start else "behind"
            elif (minute_ratio + session_ratio) / 2 >= elapsed_ratio * 0.75:
                status = "on_track"
            else:
                status = "behind"
            connection.execute(
                "UPDATE weekly_goal SET status = ?, updated_at = ? WHERE weekly_goal_id = ?",
                (status, now, row["weekly_goal_id"]),
            )
            results.append({
                **dict(row),
                "status": status,
                "completed_minutes": completed_minutes,
                "completed_sessions": completed_sessions,
                "remaining_minutes": max(0, row["target_minutes"] - completed_minutes),
                "remaining_sessions": max(0, row["target_sessions"] - completed_sessions),
            })
        return results

    def parent_today(self) -> dict:
        items: list[dict] = []
        now = utc_now()
        local_now = family_time(datetime.fromisoformat(now))
        day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        start_utc = day_start.astimezone(timezone.utc).isoformat()
        end_utc = (day_start + timedelta(days=1)).astimezone(timezone.utc).isoformat()
        with self.database.connect() as connection:
            children = connection.execute(
                "SELECT child_id, nickname, grade FROM child_profile WHERE status = 'active' ORDER BY created_at"
            ).fetchall()
            for child in children:
                session = connection.execute(
                    """
                    SELECT session_id, state, status, started_at, ended_at
                    FROM daily_session
                    WHERE child_id = ? AND julianday(started_at) >= julianday(?)
                      AND julianday(started_at) < julianday(?)
                    ORDER BY started_at DESC LIMIT 1
                    """,
                    (child["child_id"], start_utc, end_utc),
                ).fetchone()
                plan = None
                task_totals = {"total": 0, "completed": 0, "deferred": 0, "planned_minutes": 0}
                current_task = None
                if session:
                    plan = connection.execute(
                        "SELECT plan_id, total_minutes FROM daily_plan WHERE session_id = ? ORDER BY created_at DESC LIMIT 1",
                        (session["session_id"],),
                    ).fetchone()
                    if plan:
                        totals = connection.execute(
                            """
                            SELECT
                              SUM(CASE WHEN source_type != 'planned_break' AND status != 'rescheduled' THEN 1 ELSE 0 END) AS total,
                              SUM(CASE WHEN source_type != 'planned_break' AND status = 'completed' THEN 1 ELSE 0 END) AS completed,
                              SUM(CASE WHEN source_type != 'planned_break' AND status = 'rescheduled' THEN 1 ELSE 0 END) AS deferred,
                              SUM(CASE WHEN source_type != 'planned_break' AND status != 'rescheduled' THEN expected_minutes ELSE 0 END) AS planned_minutes
                            FROM plan_item WHERE plan_id = ?
                            """,
                            (plan["plan_id"],),
                        ).fetchone()
                        task_totals = {key: int(totals[key] or 0) for key in task_totals}
                        current_task = connection.execute(
                            """
                            SELECT title, subject, status, expected_minutes
                            FROM plan_item
                            WHERE plan_id = ? AND source_type != 'planned_break'
                              AND status IN ('active', 'paused', 'pending')
                            ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'paused' THEN 1 ELSE 2 END, position
                            LIMIT 1
                            """,
                            (plan["plan_id"],),
                        ).fetchone()
                due_reviews = connection.execute(
                    "SELECT COUNT(*) FROM review_task WHERE child_id = ? AND status = 'pending' AND due_at <= ?",
                    (child["child_id"], utc_now()),
                ).fetchone()[0]
                report = connection.execute(
                    "SELECT report_id, report_date, summary_json FROM daily_report WHERE child_id = ? ORDER BY report_date DESC, created_at DESC LIMIT 1",
                    (child["child_id"],),
                ).fetchone()
                pending_reports = connection.execute(
                    "SELECT COUNT(*) FROM report_job WHERE child_id = ? AND status = 'pending'",
                    (child["child_id"],),
                ).fetchone()[0]
                model_failures = connection.execute(
                    "SELECT COUNT(*) FROM model_call WHERE child_id = ? AND status = 'failed' AND created_at >= datetime('now', '-7 days')",
                    (child["child_id"],),
                ).fetchone()[0]
                items.append({
                    "child": dict(child),
                    "session": dict(session) if session else None,
                    "plan": task_totals if plan else None,
                    "current_task": dict(current_task) if current_task else None,
                    "due_reviews": int(due_reviews),
                    "latest_report": {
                        "report_id": report["report_id"],
                        "report_date": report["report_date"],
                        "headline": json.loads(report["summary_json"]).get("headline", "已生成日报"),
                    } if report else None,
                    "failures": {"pending_reports": int(pending_reports), "model_failures_last_7_days": int(model_failures)},
                })
        return {"generated_at": now, "items": items}

    def update_child_profile(self, child_id: str, nickname: str, curriculum_region: str) -> dict:
        nickname = nickname.strip()
        curriculum_region = curriculum_region.strip()
        if not nickname or not curriculum_region:
            raise PlanInputError("孩子称呼和所在地区不能为空。")
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            child = connection.execute(
                "SELECT child_id FROM child_profile WHERE child_id = ? AND status = 'active'",
                (child_id,),
            ).fetchone()
            if not child:
                raise UnknownChildError(child_id)
            connection.execute(
                """
                UPDATE child_profile SET nickname = ?, curriculum_region = ?, updated_at = ?
                WHERE child_id = ?
                """,
                (nickname, curriculum_region, now, child_id),
            )
            connection.execute(
                """
                INSERT INTO audit_event(audit_id, actor, action, child_id, summary_json, occurred_at)
                VALUES (?, 'parent', 'child_profile_updated', ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    child_id,
                    json.dumps(
                        {"updated_fields": ["nickname", "curriculum_region"]},
                        ensure_ascii=False,
                    ),
                    now,
                ),
            )
            connection.commit()
            row = connection.execute(
                """
                SELECT child_id, nickname, grade, curriculum_region, status
                FROM child_profile WHERE child_id = ?
                """,
                (child_id,),
            ).fetchone()
        return dict(row)

    def get_active_session(self) -> dict | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT session_id, child_id, status, state, started_at, last_checkpoint_at,
                       observation_status, snooze_until, recall_declared_empty,
                       homework_declared_empty
                FROM daily_session WHERE status = 'active' LIMIT 1
                """
            ).fetchone()
        return dict(row) if row else None

    def get_family_rule(self, child_id: str) -> dict:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT child_id, latest_end_time, max_learning_minutes, task_segment_minutes,
                       break_minutes, pet_min_minutes, competition_minutes, observation_mode,
                       reminder_budget_per_10m, misclassification_shadow_threshold,
                       version, updated_at
                FROM family_rule WHERE child_id = ?
                """,
                (child_id,),
            ).fetchone()
        if not row:
            raise UnknownChildError(child_id)
        return dict(row)

    def update_family_rule(self, child_id: str, values: dict) -> dict:
        now = utc_now()
        with self.database.connect() as connection:
            existing = connection.execute(
                "SELECT child_id FROM family_rule WHERE child_id = ?", (child_id,)
            ).fetchone()
            if not existing:
                raise UnknownChildError(child_id)
            connection.execute(
                """
                UPDATE family_rule SET latest_end_time = ?, max_learning_minutes = ?,
                  task_segment_minutes = ?, break_minutes = ?, pet_min_minutes = ?,
                  competition_minutes = ?, observation_mode = ?, reminder_budget_per_10m = ?,
                  misclassification_shadow_threshold = ?,
                  version = version + 1, updated_at = ?
                WHERE child_id = ?
                """,
                (
                    values.get("latest_end_time"), values["max_learning_minutes"],
                    values["task_segment_minutes"], values["break_minutes"],
                    values["pet_min_minutes"], values["competition_minutes"],
                    values["observation_mode"], values["reminder_budget_per_10m"],
                    values["misclassification_shadow_threshold"], now, child_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO audit_event(audit_id, actor, action, child_id, summary_json, occurred_at)
                VALUES (?, 'parent', 'family_rule_updated', ?, ?, ?)
                """,
                (
                    str(uuid4()), child_id,
                    json.dumps({key: values.get(key) for key in (
                        "latest_end_time", "max_learning_minutes", "task_segment_minutes",
                        "break_minutes", "pet_min_minutes", "competition_minutes",
                        "observation_mode", "reminder_budget_per_10m",
                        "misclassification_shadow_threshold",
                    )}, ensure_ascii=False),
                    now,
                ),
            )
            connection.commit()
        return self.get_family_rule(child_id)

    def start_session(self, child_id: str, client_action_id: str) -> dict:
        now = utc_now()
        session_id = str(uuid4())
        transition_id = str(uuid4())
        try:
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing_action = connection.execute(
                    "SELECT session_id FROM daily_session WHERE client_action_id = ?",
                    (client_action_id,),
                ).fetchone()
                if existing_action:
                    connection.rollback()
                    return self.get_session(existing_action["session_id"])

                child = connection.execute(
                    "SELECT child_id FROM child_profile WHERE child_id = ? AND status = 'active'",
                    (child_id,),
                ).fetchone()
                if not child:
                    raise UnknownChildError(child_id)

                active = connection.execute(
                    "SELECT session_id, child_id FROM daily_session WHERE status = 'active' LIMIT 1"
                ).fetchone()
                if active:
                    raise ActiveSessionConflictError(active["session_id"], active["child_id"])

                # Repair objectives left active by older builds after their
                # owning session had already closed. A historical learning
                # goal may be resumed later, but it must not own a new day's
                # voice command until that resume is explicitly authorized.
                connection.execute(
                    """
                    UPDATE agent_objective
                    SET status = 'paused', updated_at = ?
                    WHERE child_id = ? AND status = 'active'
                      AND EXISTS (
                        SELECT 1 FROM daily_session owner
                        WHERE owner.session_id = agent_objective.session_id
                          AND owner.status != 'active'
                      )
                    """,
                    (now, child_id),
                )

                connection.execute(
                    """
                    INSERT INTO daily_session(
                      session_id, child_id, status, state, started_at, last_checkpoint_at, client_action_id
                    ) VALUES (?, ?, 'active', 'check_in', ?, ?, ?)
                    """,
                    (session_id, child_id, now, now, client_action_id),
                )
                connection.execute(
                    """
                    INSERT INTO session_transition(
                      transition_id, session_id, from_state, to_state, reason, occurred_at
                    ) VALUES (?, ?, NULL, 'check_in', 'child_started', ?)
                    """,
                    (transition_id, session_id, now),
                )
                connection.commit()
        except sqlite3.IntegrityError as error:
            raise RepositoryConflictError(str(error)) from error
        return self.get_session(session_id)

    def switch_active_session_child(
        self, session_id: str, target_child_id: str, client_action_id: str
    ) -> dict:
        now = utc_now()
        new_session_id = str(uuid4())
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay = connection.execute(
                "SELECT session_id, child_id FROM daily_session WHERE client_action_id = ?",
                (client_action_id,),
            ).fetchone()
            if replay:
                connection.rollback()
                if replay["child_id"] != target_child_id:
                    raise RepositoryConflictError("child switch action reused for another child")
                return self.get_session(replay["session_id"])
            source = connection.execute(
                "SELECT child_id, state, status FROM daily_session WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if not source:
                raise UnknownSessionError(session_id)
            if source["status"] != "active":
                raise ActiveSessionConflictError(session_id, source["child_id"])
            target = connection.execute(
                "SELECT child_id FROM child_profile WHERE child_id = ? AND status = 'active'",
                (target_child_id,),
            ).fetchone()
            if not target:
                raise UnknownChildError(target_child_id)
            if source["child_id"] == target_child_id:
                connection.rollback()
                return self.get_session(session_id)
            connection.execute(
                """
                UPDATE plan_item SET status = 'paused', active_segment_started_at = NULL
                WHERE plan_id IN (SELECT plan_id FROM daily_plan WHERE session_id = ?)
                  AND status = 'active'
                """,
                (session_id,),
            )
            connection.execute(
                """
                UPDATE document_monitor_session SET status = 'paused', updated_at = ?
                WHERE session_id = ? AND status = 'active'
                """,
                (now, session_id),
            )
            connection.execute(
                """
                UPDATE daily_session SET status = 'cancelled', state = 'cancelled', ended_at = ?,
                  last_checkpoint_at = ?, observation_status = 'paused'
                WHERE session_id = ?
                """,
                (now, now, session_id),
            )
            connection.execute(
                """
                INSERT INTO session_transition(
                  transition_id, session_id, from_state, to_state, reason, occurred_at
                ) VALUES (?, ?, ?, 'cancelled', 'voice_child_profile_switch', ?)
                """,
                (str(uuid4()), session_id, source["state"], now),
            )
            connection.execute(
                """
                INSERT INTO daily_session(
                  session_id, child_id, status, state, started_at, last_checkpoint_at, client_action_id
                ) VALUES (?, ?, 'active', 'check_in', ?, ?, ?)
                """,
                (new_session_id, target_child_id, now, now, client_action_id),
            )
            connection.execute(
                """
                INSERT INTO session_transition(
                  transition_id, session_id, from_state, to_state, reason, occurred_at
                ) VALUES (?, ?, NULL, 'check_in', 'voice_child_profile_switch', ?)
                """,
                (str(uuid4()), new_session_id, now),
            )
            connection.execute(
                """
                INSERT INTO audit_event(audit_id, actor, action, child_id, summary_json, occurred_at)
                VALUES (?, 'child', 'voice_child_profile_switched', ?, ?, ?)
                """,
                (
                    str(uuid4()), target_child_id,
                    json.dumps({"source_session_id": session_id, "target_session_id": new_session_id}, ensure_ascii=False),
                    now,
                ),
            )
            connection.commit()
        return self.get_session(new_session_id)

    def get_session(self, session_id: str, *, connection=None) -> dict:
        with nullcontext(connection) if connection is not None else self.database.connect() as connection:
            generation_sql = "(SELECT generation FROM privacy_storage_generation WHERE singleton=1)" if connection.execute("SELECT 1 FROM sqlite_master WHERE name='privacy_storage_generation'").fetchone() else "NULL"
            row = connection.execute(
                f"""
                SELECT session_id, child_id, status, state, started_at, last_checkpoint_at,
                       observation_status, observation_revision, snooze_until, recall_declared_empty,
                       homework_declared_empty, {generation_sql} AS observation_storage_generation
                FROM daily_session WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
        if not row:
            raise UnknownSessionError(session_id)
        result = dict(row)
        storage = result.pop("observation_storage_generation")
        # Domain separation keeps the parent operation generation private, while
        # the existing staged restore rotation invalidates every older intent.
        result["observation_generation"] = hashlib.sha256(("observation-intent-v1:" + session_id + ":" + storage).encode()).hexdigest() if storage else None
        return result

    def set_observation_status(self, session_id: str, status: str, *, client_action_id: str,
                               expected_revision: int | None = None, expected_generation: str | None = None) -> dict:
        """Apply only the original explicit intent; never refresh its expected revision."""
        if status not in {"enabled", "paused"}:
            raise PlanInputError("不支持这种观察状态。")
        if not isinstance(client_action_id, str) or not 8 <= len(client_action_id) <= 128:
            raise PlanInputError("观察操作缺少可核对的回执标识。")
        if expected_revision is not None and (type(expected_revision) is not int or not 0 <= expected_revision < 9007199254740991):
            raise PlanInputError("观察状态版本无效。")
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            session = self.get_session(session_id, connection=connection)
            if session["status"] != "active":
                raise StaleSessionStateError("active", session["status"])
            if status == "enabled":
                if expected_revision is not None and expected_generation is None:
                    raise ObservationControlConflictError("OBSERVATION_CONTROL_GENERATION_REQUIRED")
                if expected_generation is not None and expected_generation != session["observation_generation"]:
                    raise ObservationControlConflictError("OBSERVATION_CONTROL_GENERATION_CHANGED")
            if status == "enabled" and expected_revision is None and session["observation_status"] != "enabled":
                raise ObservationControlConflictError("OBSERVATION_CONTROL_REVISION_REQUIRED")
            previous = connection.execute("SELECT operation, expected_revision, expected_generation, effects_complete, result_json FROM observation_control_action WHERE session_id=? AND client_action_id=?",
                                          (session_id, client_action_id)).fetchone()
            if previous:
                if previous["operation"] != status or previous["expected_revision"] != expected_revision or previous["expected_generation"] != expected_generation:
                    raise ObservationControlConflictError("OBSERVATION_CONTROL_ACTION_REUSED")
                result = json.loads(previous["result_json"])
                if (result["observation_revision"] != session["observation_revision"]
                        or result["observation_status"] != session["observation_status"]
                        or result["observation_generation"] != session["observation_generation"]):
                    raise ObservationControlConflictError("OBSERVATION_CONTROL_ACTION_SUPERSEDED")
                if not previous["effects_complete"]:
                    raise ObservationControlConflictError("OBSERVATION_CONTROL_EFFECTS_UNCONFIRMED")
                result["observation_control"].update(effect="replayed", applied=False, fresh=False)
                return result
            revision = session["observation_revision"]
            effect = "applied"
            if status == "enabled" and expected_revision is None:
                if session["observation_status"] != "enabled":
                    raise ObservationControlConflictError("OBSERVATION_CONTROL_REVISION_REQUIRED")
                effect = "no_op"  # Legacy enabled is not a new permission or qualification.
            elif status == "enabled" and expected_revision != revision:
                raise ObservationControlConflictError("OBSERVATION_CONTROL_REVISION_CHANGED")
            if effect == "applied":
                limit = 9007199254740989 if status == "enabled" else 9007199254740990
                if revision >= limit:
                    raise ObservationControlConflictError("OBSERVATION_CONTROL_REVISION_EXHAUSTED")
                changed = connection.execute("UPDATE daily_session SET observation_status=?, observation_revision=observation_revision+1, last_checkpoint_at=? WHERE session_id=? AND status='active' AND observation_revision=?",
                                             ("paused", utc_now(), session_id, revision)).rowcount
                if changed != 1:
                    raise ObservationControlConflictError("OBSERVATION_CONTROL_REVISION_CHANGED")
            result = self.get_session(session_id, connection=connection)
            result["observation_control"] = {"client_action_id": client_action_id, "operation": status,
                "expected_revision": expected_revision, "expected_generation": expected_generation, "result_revision": result["observation_revision"], "effect": effect, "applied": effect == "applied", "fresh": effect == "applied"}
            connection.execute("INSERT INTO observation_control_action(session_id,client_action_id,operation,expected_revision,expected_generation,effects_complete,result_json,created_at) VALUES (?,?,?,?,?,?,?,?)",
                               (session_id, client_action_id, status, expected_revision, expected_generation, int(effect == "no_op"), json.dumps(result, ensure_ascii=False), utc_now()))
            connection.commit()
            return result

    def complete_observation_status(self, session_id: str, client_action_id: str, *, prepare_resume=None) -> dict:
        """Publish permission only after the original action's effects are confirmed."""
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self.get_session(session_id, connection=connection)
            if current["status"] != "active":
                raise StaleSessionStateError("active", current["status"])
            previous = connection.execute("SELECT operation, effects_complete, result_json FROM observation_control_action WHERE session_id=? AND client_action_id=?",
                                          (session_id, client_action_id)).fetchone()
            if not previous or previous["effects_complete"]:
                raise ObservationControlConflictError("OBSERVATION_CONTROL_EFFECTS_UNCONFIRMED")
            reserved = json.loads(previous["result_json"])
            if any(reserved[key] != current[key] for key in ("observation_generation", "observation_revision", "observation_status")):
                raise ObservationControlConflictError("OBSERVATION_CONTROL_ACTION_SUPERSEDED")
            if previous["operation"] == "enabled":
                if prepare_resume is None or current["observation_status"] != "paused":
                    raise ObservationControlConflictError("OBSERVATION_CONTROL_EFFECTS_UNCONFIRMED")
                changed = connection.execute("UPDATE daily_session SET observation_status='enabled', observation_revision=observation_revision+1, last_checkpoint_at=? WHERE session_id=? AND status='active' AND observation_status='paused' AND observation_revision=?",
                                             (utc_now(), session_id, current["observation_revision"])).rowcount
                if changed != 1:
                    raise ObservationControlConflictError("OBSERVATION_CONTROL_ACTION_SUPERSEDED")
                # This callback only performs the existing qualification SQL on
                # this borrowed transaction. Failure rolls back enabled too.
                prepare_resume(connection, session_id)
            result = self.get_session(session_id, connection=connection)
            result["observation_control"] = dict(reserved["observation_control"], result_revision=result["observation_revision"])
            connection.execute("UPDATE observation_control_action SET effects_complete=1, result_json=? WHERE session_id=? AND client_action_id=? AND effects_complete=0",
                               (json.dumps(result, ensure_ascii=False), session_id, client_action_id))
            connection.commit()
            return result

    def save_planning_context(
        self,
        session_id: str,
        energy_level: str,
        fixed_commitment_minutes: int,
        fixed_commitment_note: str | None,
        tomorrow_event_subject: str | None,
        tomorrow_event_title: str | None,
        tomorrow_event_minutes: int,
        client_action_id: str,
    ) -> dict:
        self._require_active_state(session_id, "check_in")
        if tomorrow_event_minutes and not tomorrow_event_title:
            raise PlanInputError("填写了备考时长时，请也写明天要检查或考试的内容。")
        now = utc_now()
        snapshot = {
            "energy_level": energy_level,
            "fixed_commitment_minutes": fixed_commitment_minutes,
            "fixed_commitment_note": (fixed_commitment_note or "").strip() or None,
            "tomorrow_event_subject": (tomorrow_event_subject or "").strip() or None,
            "tomorrow_event_title": (tomorrow_event_title or "").strip() or None,
            "tomorrow_event_minutes": tomorrow_event_minutes if tomorrow_event_title else 0,
        }
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT session_id FROM planning_context_event WHERE client_action_id = ?",
                (client_action_id,),
            ).fetchone()
            if existing:
                connection.rollback()
                return self._get_planning_context(session_id)
            connection.execute(
                """
                INSERT INTO session_planning_context(
                  session_id, energy_level, fixed_commitment_minutes, fixed_commitment_note,
                  tomorrow_event_subject, tomorrow_event_title, tomorrow_event_minutes, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                  energy_level = excluded.energy_level,
                  fixed_commitment_minutes = excluded.fixed_commitment_minutes,
                  fixed_commitment_note = excluded.fixed_commitment_note,
                  tomorrow_event_subject = excluded.tomorrow_event_subject,
                  tomorrow_event_title = excluded.tomorrow_event_title,
                  tomorrow_event_minutes = excluded.tomorrow_event_minutes,
                  updated_at = excluded.updated_at
                """,
                (
                    session_id, snapshot["energy_level"], snapshot["fixed_commitment_minutes"],
                    snapshot["fixed_commitment_note"], snapshot["tomorrow_event_subject"],
                    snapshot["tomorrow_event_title"], snapshot["tomorrow_event_minutes"], now,
                ),
            )
            connection.execute(
                """
                INSERT INTO planning_context_event(
                  event_id, session_id, snapshot_json, created_at, client_action_id
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (str(uuid4()), session_id, json.dumps(snapshot, ensure_ascii=False), now, client_action_id),
            )
            connection.commit()
        return self._get_planning_context(session_id)

    def _get_planning_context(self, session_id: str) -> dict:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT energy_level, fixed_commitment_minutes, fixed_commitment_note,
                       tomorrow_event_subject, tomorrow_event_title, tomorrow_event_minutes,
                       updated_at
                FROM session_planning_context WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
        if row:
            return dict(row)
        return {
            "energy_level": "medium",
            "fixed_commitment_minutes": 0,
            "fixed_commitment_note": None,
            "tomorrow_event_subject": None,
            "tomorrow_event_title": None,
            "tomorrow_event_minutes": 0,
            "updated_at": None,
        }

    def get_workspace(self, session_id: str) -> dict:
        session = self.get_session(session_id)
        with self.database.connect() as connection:
            recalls = connection.execute(
                """
                SELECT recall_id, subject, summary, confidence, confirmation_status,
                       record_kind, candidate_nodes_json, textbook_mapping_json,
                       evidence_sources_json, created_at
                FROM classroom_recall WHERE session_id = ? ORDER BY created_at
                """,
                (session_id,),
            ).fetchall()
            homework = connection.execute(
                """
                SELECT homework_id, subject, task_type, title, range_text, expected_minutes,
                       due_at, special_requirements, source_artifact_id, ocr_confidence,
                       confirmation_status, status
                FROM homework_item WHERE session_id = ? ORDER BY created_at
                """,
                (session_id,),
            ).fetchall()
            plan = connection.execute(
                """
                SELECT plan_id, plan_date, status, total_minutes, generator, version
                FROM daily_plan WHERE session_id = ? AND status = 'active' LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            plan_items = []
            constraint_decisions = []
            supply_obligations = []
            if plan:
                plan_items = connection.execute(
                    """
                    SELECT plan_item_id, homework_id, subject, title, expected_minutes,
                           actual_minutes, priority, status, position, started_at, completed_at,
                           active_seconds, active_segment_started_at, source_type, source_ref,
                           completion_standard, cognitive_load, completion_evidence_type,
                           completion_note, task_type, completion_checklist_json,
                           completion_artifact_id
                    FROM plan_item WHERE plan_id = ? ORDER BY position
                    """,
                    (plan["plan_id"],),
                ).fetchall()
                constraint_decisions = connection.execute(
                    """SELECT decision_id,obligation_type,source_ref,decision,reason,requested_minutes,
                              allocated_minutes,metadata_json,created_at
                       FROM plan_constraint_decision WHERE plan_id=?
                       ORDER BY rowid""",
                    (plan["plan_id"],),
                ).fetchall()
                from .supply_gap_presentation import project_supply_obligations
                supply_obligations = project_supply_obligations(
                    connection, session_id, session['child_id'], plan['plan_id'])
                from .supply_gap_resume import project_school_obligations
                supply_obligations += project_school_obligations(
                    connection, session_id, session['child_id'], plan['plan_id'])
                from .oral_supply_carryover import workspace as oral_supply_workspace
                supply_obligations += oral_supply_workspace(connection,session['child_id'],session_id,plan['plan_id'])
            live_plan_items = [academic_completion_item(connection, self._plan_item_result(row)) for row in plan_items]
            report_ready = connection.execute(
                "SELECT 1 FROM daily_report WHERE session_id = ?", (session_id,)
            ).fetchone() is not None
            draft_rows = connection.execute(
                "SELECT section, payload_json, updated_at FROM session_draft WHERE session_id = ?",
                (session_id,),
            ).fetchall()
            latest_tutor_row = connection.execute(
                """
                SELECT tt.turn_id, tt.created_at
                FROM tutor_turn tt
                WHERE tt.session_id = ? AND tt.status = 'completed'
                  AND NOT EXISTS (
                    SELECT 1 FROM agent_turn at
                    WHERE at.session_id = tt.session_id
                      AND at.created_at > tt.created_at
                      AND json_extract(at.action_json, '$.type') = 'request_capture'
                  )
                ORDER BY tt.created_at DESC, tt.rowid DESC LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            from .teaching_presentation_scope import replaced_by_question
            latest_tutor_turn = (
                self._get_tutor_turn(connection, latest_tutor_row["turn_id"])
                if latest_tutor_row and not replaced_by_question(connection, session_id,
                    session['child_id'], latest_tutor_row['created_at']) else None
            )
            lease_rows = connection.execute(
                """
                SELECT device_type, status, release_reason, released_at
                FROM sensor_lease WHERE session_id = ? ORDER BY opened_at
                """,
                (session_id,),
            ).fetchall()
            release_verification_row = connection.execute(
                """
                SELECT result, used_device_types_json, reopened_device_types_json,
                       device_manager_healthy, windows_privacy_indicator_clear,
                       physical_indicator_status, platform_name, platform_version, verified_at
                FROM sensor_release_verification WHERE session_id = ?
                ORDER BY verified_at DESC, rowid DESC LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            carryover_candidates = self._deferred_task_candidates(
                connection, session["child_id"], session_id
            )
            from .learning_input_carryover import LearningInputCarryoverService, CarryoverConflict
            learning_input_unplaced = {"status": "not_applicable", "items": [], "local_only": True,
                                      "cloud_transmission_allowed": False}
            if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='learning_input_carryover_event'").fetchone():
                try:
                    learning_input_unplaced = {"status": "ready", **LearningInputCarryoverService(self.database).list_unplaced(
                        session['child_id'], session_id, connection=connection)}
                except CarryoverConflict as error:
                    learning_input_unplaced.update(status="unavailable", reason=str(error))
            carryover_decisions = [
                dict(row) for row in connection.execute(
                    """
                    SELECT review_id, source_plan_item_id, source_session_id,
                           decision, reason, target_homework_id, reviewed_at
                    FROM deferred_task_review
                    WHERE target_session_id = ? ORDER BY reviewed_at
                    """,
                    (session_id,),
                ).fetchall()
            ]
        active_leases = sum(1 for row in lease_rows if row["status"] == "active")
        released_leases = len(lease_rows) - active_leases
        by_device: dict[str, dict] = {}
        for row in lease_rows:
            item = by_device.setdefault(
                row["device_type"],
                {"opened": 0, "active": 0, "released": 0, "last_release_reason": None, "last_released_at": None},
            )
            item["opened"] += 1
            if row["status"] == "active":
                item["active"] += 1
            else:
                item["released"] += 1
                if row["released_at"]:
                    item["last_release_reason"] = row["release_reason"]
                    item["last_released_at"] = row["released_at"]
        application_release_confirmed = session["status"] == "completed" and active_leases == 0
        release_verification = self._sensor_release_verification_result(release_verification_row) if release_verification_row else None
        os_release_verified = bool(application_release_confirmed and release_verification and release_verification["result"] == "passed")
        return {
            "session": session,
            "family_rule": self.get_family_rule(session["child_id"]),
            "planning_context": self._get_planning_context(session_id),
            "recalls": [self._public_recall(row) for row in recalls],
            "homework": [dict(row) for row in homework],
            "carryover_candidates": carryover_candidates,
            "learning_input_unplaced": learning_input_unplaced,
            "carryover_decisions": carryover_decisions,
            "plan": dict(plan) if plan else None,
            "plan_items": live_plan_items,
            "constraint_decisions": [
                {**dict(row), "metadata": _plan_metadata_for_display(row["metadata_json"])}
                for row in constraint_decisions
            ],
            "supply_obligations": supply_obligations,
            "daily_report_ready": report_ready,
            "latest_tutor_turn": latest_tutor_turn,
            "drafts": {
                row["section"]: {
                    "payload": json.loads(row["payload_json"]),
                    "updated_at": row["updated_at"],
                }
                for row in draft_rows
            },
            "sensor_release": {
                "opened_leases": len(lease_rows),
                "active_leases": active_leases,
                "released_leases": released_leases,
                "application_release_confirmed": application_release_confirmed,
                "evidence_level": "not_opened" if not lease_rows else "windows_session_verified" if os_release_verified else "application_record_only",
                "os_release_verified": os_release_verified,
                "release_verification": release_verification,
                "by_device": by_device,
            },
        }

    def record_sensor_release_verification(
        self,
        session_id: str,
        *,
        reopened_device_types: list[str],
        device_manager_healthy: bool,
        windows_privacy_indicator_clear: bool,
        physical_indicator_status: str,
        notes: str | None,
        client_action_id: str,
    ) -> dict:
        runtime_evidence = windows_release_runtime_evidence()
        if runtime_evidence is None:
            raise SensorReleaseVerificationNotAllowedError("系统级释放复核只能在目标 Windows 安装版中记录。")
        normalized_reopened = sorted(set(reopened_device_types))
        if any(device_type not in SENSOR_DEVICE_TYPES for device_type in normalized_reopened):
            raise InvalidSensorReleaseVerificationError("重开验证包含不支持的设备类别。")
        if physical_indicator_status not in {"clear", "not_available", "still_on"}:
            raise InvalidSensorReleaseVerificationError("设备指示灯结论无效。")
        clean_notes = notes.strip() if notes else None
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            repeated = connection.execute(
                "SELECT * FROM sensor_release_verification WHERE client_action_id = ?",
                (client_action_id,),
            ).fetchone()
            if repeated:
                connection.rollback()
                return self._sensor_release_verification_result(repeated, include_notes=True)
            session = connection.execute(
                "SELECT session_id, child_id, status FROM daily_session WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if not session:
                connection.rollback()
                raise UnknownSessionError(session_id)
            lease_rows = connection.execute(
                "SELECT device_type, status FROM sensor_lease WHERE session_id = ?",
                (session_id,),
            ).fetchall()
            if session["status"] != "completed" or not lease_rows:
                connection.rollback()
                raise SensorReleaseVerificationNotAllowedError("只能复核已完成且实际使用过传感器的学习会话。")
            if any(row["status"] == "active" for row in lease_rows):
                connection.rollback()
                raise SensorReleaseVerificationNotAllowedError("应用仍记录有活动设备租约，不能进行 Windows 释放复核。")
            used_device_types = sorted({row["device_type"] for row in lease_rows})
            result = "passed" if (
                device_manager_healthy
                and windows_privacy_indicator_clear
                and physical_indicator_status in {"clear", "not_available"}
                and normalized_reopened == used_device_types
            ) else "failed"
            verification_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO sensor_release_verification(
                  verification_id, session_id, child_id, result,
                  used_device_types_json, reopened_device_types_json,
                  device_manager_healthy, windows_privacy_indicator_clear,
                  physical_indicator_status, platform_name, platform_version,
                  notes, verified_at, client_action_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    verification_id, session_id, session["child_id"], result,
                    json.dumps(used_device_types, ensure_ascii=False),
                    json.dumps(normalized_reopened, ensure_ascii=False),
                    int(device_manager_healthy), int(windows_privacy_indicator_clear),
                    physical_indicator_status, runtime_evidence[0], runtime_evidence[1],
                    clean_notes, now, client_action_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM sensor_release_verification WHERE verification_id = ?",
                (verification_id,),
            ).fetchone()
            connection.commit()
        return self._sensor_release_verification_result(row, include_notes=True)

    def sensor_release_reviews(self, limit: int = 20) -> list[dict]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT ds.session_id, ds.child_id, cp.nickname, ds.ended_at
                FROM daily_session ds JOIN child_profile cp ON cp.child_id = ds.child_id
                WHERE ds.status = 'completed'
                  AND EXISTS(SELECT 1 FROM sensor_lease sl WHERE sl.session_id = ds.session_id)
                ORDER BY ds.ended_at DESC LIMIT ?
                """,
                (max(1, min(limit, 50)),),
            ).fetchall()
            latest_rows = {
                row["session_id"]: connection.execute(
                    """
                    SELECT * FROM sensor_release_verification WHERE session_id = ?
                    ORDER BY verified_at DESC, rowid DESC LIMIT 1
                    """,
                    (row["session_id"],),
                ).fetchone()
                for row in rows
            }
        reviews = []
        for row in rows:
            sensor_release = self.get_workspace(row["session_id"])["sensor_release"]
            if latest_rows[row["session_id"]]:
                sensor_release["release_verification"] = self._sensor_release_verification_result(
                    latest_rows[row["session_id"]], include_notes=True
                )
            reviews.append({
                "session_id": row["session_id"],
                "child_id": row["child_id"],
                "child_nickname": row["nickname"],
                "ended_at": row["ended_at"],
                "sensor_release": sensor_release,
            })
        return reviews

    @staticmethod
    def sensor_release_platform_eligible() -> bool:
        return windows_release_runtime_evidence() is not None

    @staticmethod
    def _sensor_release_verification_result(row, include_notes: bool = False) -> dict:
        result = {
            "result": row["result"],
            "used_device_types": json.loads(row["used_device_types_json"]),
            "reopened_device_types": json.loads(row["reopened_device_types_json"]),
            "device_manager_healthy": bool(row["device_manager_healthy"]),
            "windows_privacy_indicator_clear": bool(row["windows_privacy_indicator_clear"]),
            "physical_indicator_status": row["physical_indicator_status"],
            "platform_name": row["platform_name"],
            "platform_version": row["platform_version"],
            "verified_at": row["verified_at"],
        }
        if include_notes:
            result["notes"] = row["notes"]
        return result

    def save_session_draft(self, session_id: str, section: str, payload: dict) -> dict:
        allowed_states = DRAFT_STATES.get(section)
        if not allowed_states:
            raise InvalidSessionDraftError("不支持这个草稿区域。")
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(serialized.encode("utf-8")) > MAX_DRAFT_BYTES:
            raise InvalidSessionDraftError("草稿内容过长，请先提交当前内容。")
        session = self.get_session(session_id)
        if session["status"] != "active" or session["state"] not in allowed_states:
            raise StaleSessionStateError("/".join(sorted(allowed_states)), session["state"])
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO session_draft(session_id, section, payload_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(session_id, section) DO UPDATE SET
                  payload_json = excluded.payload_json,
                  updated_at = excluded.updated_at
                """,
                (session_id, section, serialized, now),
            )
            connection.commit()
        return {"section": section, "payload": payload, "updated_at": now}

    def clear_session_draft(self, session_id: str, section: str) -> dict:
        if section not in DRAFT_STATES:
            raise InvalidSessionDraftError("不支持这个草稿区域。")
        self.get_session(session_id)
        with self.database.connect() as connection:
            deleted = connection.execute(
                "DELETE FROM session_draft WHERE session_id = ? AND section = ?",
                (session_id, section),
            ).rowcount
            connection.commit()
        return {"section": section, "deleted": bool(deleted)}

    def transition(
        self,
        session_id: str,
        expected_state: str,
        target_state: str,
        reason: str,
        client_action_id: str,
        *, connection=None,
    ) -> dict:
        if target_state not in SESSION_TRANSITION_TARGETS.get(expected_state, ()):
            raise InvalidTransitionError(expected_state, target_state)

        now = utc_now()
        with self._write_transaction(connection) as connection:
            existing = connection.execute(
                "SELECT session_id,from_state,to_state,reason FROM session_transition WHERE client_action_id = ?",
                (client_action_id,),
            ).fetchone()
            if existing:
                if tuple(existing) != (session_id, expected_state, target_state, reason):
                    raise PlanInputError('会话转换操作号与原操作不匹配。')
                return self.get_session(existing["session_id"], connection=connection)

            row = connection.execute(
                "SELECT state, status FROM daily_session WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if not row:
                raise UnknownSessionError(session_id)
            if row["state"] != expected_state or row["status"] != "active":
                raise StaleSessionStateError(expected_state, row["state"])
            if expected_state == "classroom_recall" and target_state == "homework_capture":
                recall_count = connection.execute(
                    """
                    SELECT COUNT(*) FROM classroom_recall
                    WHERE session_id = ? AND confirmation_status = 'child_confirmed'
                    """,
                    (session_id,),
                ).fetchone()[0]
                declared_empty = connection.execute(
                    "SELECT recall_declared_empty FROM daily_session WHERE session_id = ?",
                    (session_id,),
                ).fetchone()[0]
                if not recall_count and not declared_empty:
                    raise PlanInputError("请先记录一条课堂回想，或明确选择今天暂时想不起来。")
            if expected_state == "study" and target_state == "reflection":
                # Long-horizon enrichment belongs in the plan, but it must not
                # trap a child in today's session after school work has been
                # settled.  Carry untouched optional work forward explicitly;
                # active or paused work still requires the child to complete or
                # reschedule it, and school work is never changed implicitly.
                optional_rows = connection.execute(
                    """
                    SELECT pi.plan_item_id, pi.source_type FROM plan_item pi
                    JOIN daily_plan dp ON dp.plan_id = pi.plan_id
                    WHERE dp.session_id = ? AND pi.status = 'pending'
                      AND pi.source_type IN (
                        'planned_break', 'pet_daily', 'competition_math', 'junior_exam_goal'
                      )
                    """,
                    (session_id,),
                ).fetchall()
                for optional in optional_rows:
                    connection.execute(
                        "UPDATE plan_item SET status = 'rescheduled' WHERE plan_item_id = ?",
                        (optional["plan_item_id"],),
                    )
                    self._insert_plan_event(
                        connection,
                        optional["plan_item_id"],
                        "rescheduled",
                        now,
                        {
                            "reason": "今日校内任务已收尾，长期目标任务顺延到后续计划",
                            "automatic": True,
                            "source_type": optional["source_type"],
                        },
                        f"{client_action_id}:auto-reschedule:{optional['plan_item_id']}",
                    )
                outstanding = connection.execute(
                    """
                    SELECT COUNT(*) FROM plan_item pi
                    JOIN daily_plan dp ON dp.plan_id = pi.plan_id
                    WHERE dp.session_id = ? AND pi.status IN ('pending', 'active', 'paused')
                    """,
                    (session_id,),
                ).fetchone()[0]
                if outstanding:
                    raise PlanItemStateError("请先完成或明确延期今天剩余的任务，再进入复盘。")

            completed = target_state == "completed"
            connection.execute(
                "DELETE FROM session_draft WHERE session_id = ? AND section = ?",
                (session_id, expected_state),
            )
            connection.execute(
                """
                UPDATE daily_session
                SET state = ?, status = ?, last_checkpoint_at = ?, ended_at = ?
                WHERE session_id = ?
                """,
                (
                    target_state,
                    "completed" if completed else "active",
                    now,
                    now if completed else None,
                    session_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO session_transition(
                  transition_id, session_id, from_state, to_state, reason, occurred_at,
                  client_action_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    session_id,
                    expected_state,
                    target_state,
                    reason,
                    now,
                    client_action_id,
                ),
            )
            result = self.get_session(session_id, connection=connection)
        return result

    def declare_empty_stage(
        self,
        session_id: str,
        declaration_type: str,
        client_action_id: str,
    ) -> dict:
        configuration = {
            "no_recall": ("classroom_recall", "recall_declared_empty", "classroom_recall"),
            "no_homework": ("homework_capture", "homework_declared_empty", "homework_item"),
        }.get(declaration_type)
        if not configuration:
            raise PlanInputError("不支持这种空内容声明。")
        expected_state, session_column, evidence_table = configuration
        session = self._require_active_state(session_id, expected_state)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT declaration_id FROM session_declaration WHERE client_action_id = ?",
                (client_action_id,),
            ).fetchone()
            if existing:
                connection.rollback()
                return self.get_session(session_id)
            if evidence_table == "classroom_recall":
                count = connection.execute(
                    """
                    SELECT COUNT(*) FROM classroom_recall
                    WHERE session_id = ? AND confirmation_status = 'child_confirmed'
                    """,
                    (session_id,),
                ).fetchone()[0]
            else:
                count = connection.execute(
                    "SELECT COUNT(*) FROM homework_item WHERE session_id = ? AND status = 'pending'",
                    (session_id,),
                ).fetchone()[0]
                if self._deferred_task_candidates(connection, session["child_id"], session_id):
                    connection.rollback()
                    raise PlanInputError("请先逐项处理上次延期的校内任务，再确认今天是否没有作业。")
            if count:
                connection.rollback()
                raise PlanInputError("已经记录了内容，不能再标记为没有；可以继续补充或修改现有记录。")
            now = utc_now()
            connection.execute(
                f"UPDATE daily_session SET {session_column} = 1, last_checkpoint_at = ? WHERE session_id = ?",
                (now, session_id),
            )
            connection.execute(
                """
                INSERT INTO session_declaration(
                  declaration_id, session_id, child_id, declaration_type, created_at,
                  client_action_id
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (str(uuid4()), session_id, session["child_id"], declaration_type, now, client_action_id),
            )
            connection.commit()
        return self.get_session(session_id)

    def add_recall(
        self, session_id: str, subject: str, summary: str, client_action_id: str
    ) -> dict:
        session = self._require_intake_state(session_id)
        recall_id = str(uuid4())
        now = utc_now()
        with self.database.connect() as connection:
            existing = connection.execute(
                "SELECT recall_id FROM classroom_recall WHERE client_action_id = ?",
                (client_action_id,),
            ).fetchone()
            if existing:
                return self._get_recall(connection, existing["recall_id"])
            connection.execute(
                """
                INSERT INTO classroom_recall(
                  recall_id, session_id, subject, summary, confidence, confirmation_status,
                  source_refs_json, record_kind, candidate_nodes_json, textbook_mapping_json,
                  evidence_sources_json, created_at, client_action_id
                ) VALUES (?, ?, ?, ?, 1.0, 'child_confirmed', ?, 'child_statement', '[]', NULL, ?, ?, ?)
                """,
                (recall_id, session_id, subject, summary, json.dumps([]), json.dumps(["孩子口述"], ensure_ascii=False), now, client_action_id),
            )
            connection.execute(
                "UPDATE daily_session SET recall_declared_empty = 0 WHERE session_id = ?",
                (session_id,),
            )
            candidate = connection.execute(
                """
                SELECT kn.knowledge_node_id, kn.title, kn.source_ref
                FROM knowledge_node kn JOIN child_profile cp ON cp.child_id = ?
                WHERE kn.track = 'school' AND kn.subject = ? AND kn.grade_band = cp.grade
                  AND kn.status = 'active'
                ORDER BY kn.code
                """,
                (session["child_id"], subject),
            ).fetchall()
            matched = next((node for node in candidate if any(
                hint in summary for hint in RECALL_NODE_HINTS.get(node["knowledge_node_id"], (node["title"],))
            )), None)
            if not matched:
                excerpts = connection.execute(
                    """
                    SELECT 'source:' || ks.source_id AS knowledge_node_id,
                           ke.content_text AS title, ks.source_ref
                    FROM knowledge_source ks
                    JOIN knowledge_excerpt ke ON ke.source_id = ks.source_id
                    JOIN child_profile cp ON cp.child_id = ?
                    WHERE ks.child_id = ? AND ks.subject = ? AND ks.grade_band = cp.grade
                      AND ks.review_status = 'approved'
                    ORDER BY ks.imported_at DESC, ke.sequence LIMIT 500
                    """,
                    (session["child_id"], session["child_id"], subject),
                ).fetchall()
                grams = self._recall_ngrams(summary)
                scored = sorted(
                    ((sum(1 for gram in grams if gram in row["title"]), row) for row in excerpts),
                    key=lambda item: item[0],
                    reverse=True,
                )
                if scored and scored[0][0] >= 2:
                    matched = scored[0][1]
            if not matched:
                current_scope, scope_args = current_curriculum_predicate(connection, session["child_id"], "cr")
                course_rows = connection.execute(
                    f"""
                    SELECT 'curriculum:' || cr.corpus_id || ':' || cr.external_resource_id AS knowledge_node_id,
                           cr.title, cc.source_ref,
                           cr.description || ' ' || cr.unit_path || ' ' || substr(cr.transcript_text, 1, 1800) AS searchable_text
                    FROM curriculum_resource cr
                    JOIN curriculum_corpus cc ON cc.corpus_id = cr.corpus_id
                    JOIN child_profile cp ON cp.child_id = ?
                    WHERE cc.status = 'ready' AND cr.grade_title = cp.grade
                      AND cr.subject_title LIKE ?
                      AND ({current_scope})
                    ORDER BY cr.title LIMIT 600
                    """,
                    (session["child_id"], f"%{subject}%", *scope_args),
                ).fetchall()
                grams = self._recall_ngrams(summary)
                scored_courses = sorted(
                    (
                        (
                            sum(
                                (6 if gram in row["title"] else 2) + len(gram)
                                for gram in grams
                                if len(gram) >= 3 and gram in f"{row['title']} {row['searchable_text']}"
                            ),
                            row,
                        )
                        for row in course_rows
                        if any(
                            len(gram) == 4 and gram in f"{row['title']} {row['searchable_text']}"
                            for gram in grams
                        )
                    ),
                    key=lambda item: item[0],
                    reverse=True,
                )
                if scored_courses and scored_courses[0][0] >= 7:
                    matched = scored_courses[0][1]
            candidate_id = str(uuid4())
            if matched:
                candidate_summary = f"可能相关的知识点：{matched['title']}。这是根据口述与本地课程索引生成的候选，需要你确认。"
                candidate_status = "inferred"
                confidence = 0.55
                source_refs = [recall_id, matched["knowledge_node_id"], matched["source_ref"]]
                candidate_nodes = [{"knowledge_node_id": matched["knowledge_node_id"], "title": matched["title"]}]
                family_source = str(matched["knowledge_node_id"]).startswith("source:")
                curriculum_source = str(matched["knowledge_node_id"]).startswith("curriculum:")
                textbook_mapping = {
                    "status": "family_approved_source" if family_source else "public_course_baseline" if curriculum_source else "course_index_only",
                    "label": "家庭已审核资料" if family_source else "苏e优课公开课程基线，仍需孩子确认" if curriculum_source else "本地课程索引，未绑定教材页码",
                }
                evidence_sources = ["孩子口述", "家庭已审核资料" if family_source else "苏e优课公开课程" if curriculum_source else "本地课程索引"]
            else:
                candidate_summary = f"{subject}的具体知识点还不确定；可以结合教材目录、作业页或老师要求再确认。"
                candidate_status = "unknown"
                confidence = 0.1
                source_refs = [recall_id]
                candidate_nodes = []
                textbook_mapping = {"status": "unmapped", "label": "尚未映射教材或课程索引"}
                evidence_sources = ["孩子口述"]
            connection.execute(
                """
                INSERT INTO classroom_recall(
                  recall_id, session_id, subject, summary, confidence, confirmation_status,
                  source_refs_json, record_kind, candidate_nodes_json, textbook_mapping_json,
                  evidence_sources_json, created_at, client_action_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'system_candidate', ?, ?, ?, ?, ?)
                """,
                (
                    candidate_id, session_id, subject, candidate_summary, confidence,
                    candidate_status, json.dumps(source_refs, ensure_ascii=False),
                    json.dumps(candidate_nodes, ensure_ascii=False),
                    json.dumps(textbook_mapping, ensure_ascii=False),
                    json.dumps(evidence_sources, ensure_ascii=False),
                    now, f"{client_action_id}:candidate",
                ),
            )
            connection.commit()
            return self._get_recall(connection, recall_id)

    @staticmethod
    def _recall_ngrams(text: str) -> set[str]:
        compact = "".join(character for character in text if character.isalnum())
        for phrase in ("为什么", "怎么样", "怎么", "怎样", "如何", "请问", "是不是", "能不能", "是否", "这道题"):
            compact = compact.replace(phrase, "")
        compact = "".join(character for character in compact if character not in "的了呢吗吧呀啊把要时")
        ignored = {"今天", "老师", "学习", "讲了", "内容", "新的"}
        return {
            compact[index:index + size]
            for size in (2, 3, 4)
            for index in range(max(0, len(compact) - size + 1))
            if compact[index:index + size] not in ignored
        }

    def review_recall(
        self,
        session_id: str,
        recall_id: str,
        decision: str,
        client_action_id: str,
    ) -> dict:
        self._require_active_state(session_id, "classroom_recall")
        if decision not in {"confirm", "reject"}:
            raise PlanInputError("不支持这种课堂候选处理。")
        with self.database.connect() as connection:
            existing = connection.execute(
                "SELECT recall_id FROM recall_review WHERE client_action_id = ?",
                (client_action_id,),
            ).fetchone()
            if existing:
                return self._get_recall(connection, existing["recall_id"])
            recall = connection.execute(
                """
                SELECT recall_id, confirmation_status FROM classroom_recall
                WHERE recall_id = ? AND session_id = ?
                """,
                (recall_id, session_id),
            ).fetchone()
            if not recall:
                raise UnknownRecallError(recall_id)
            if recall["confirmation_status"] not in {"inferred", "unknown"}:
                raise PlanInputError("只能处理尚未确认的课堂候选。")
            if decision == "confirm" and recall["confirmation_status"] == "unknown":
                raise PlanInputError("证据不足的未知项不能直接确认，请先补充教材或作业证据。")
            resulting_status = "child_confirmed" if decision == "confirm" else "rejected"
            connection.execute(
                "UPDATE classroom_recall SET confirmation_status = ? WHERE recall_id = ?",
                (resulting_status, recall_id),
            )
            connection.execute(
                """
                INSERT INTO recall_review(
                  review_id, recall_id, session_id, decision, previous_status,
                  resulting_status, occurred_at, client_action_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()), recall_id, session_id, decision,
                    recall["confirmation_status"], resulting_status, utc_now(), client_action_id,
                ),
            )
            connection.commit()
            return self._get_recall(connection, recall_id)

    @staticmethod
    def _require_homework_ocr_source(connection, artifact_id: str | None, child_id: str, session_id: str) -> None:
        if not artifact_id:
            return
        artifact = connection.execute(
            'SELECT child_id,session_id,artifact_type,status FROM capture_artifact WHERE artifact_id=?', (artifact_id,)
        ).fetchone()
        if (not artifact or artifact['child_id'] != child_id or artifact['session_id'] != session_id
                or artifact['artifact_type'] != 'document_ocr' or artifact['status'] not in {'completed', 'quality_warning'}):
            raise PlanInputError('作业图片来源未确认或不属于当前孩子与会话，请重新采集或移除图片来源。')

    def add_homework(
        self,
        session_id: str,
        subject: str,
        title: str,
        expected_minutes: int,
        task_type: str,
        range_text: str | None,
        due_at: str | None,
        special_requirements: str | None,
        source_artifact_id: str | None,
        ocr_confidence: float | None,
        client_action_id: str,
    ) -> dict:
        session = self._require_intake_state(session_id)
        homework_id = str(uuid4())
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            existing = connection.execute(
                "SELECT homework_id,child_id,session_id,source_artifact_id FROM homework_item WHERE client_action_id = ?",
                (client_action_id,),
            ).fetchone()
            if existing:
                if existing['session_id'] != session_id:
                    raise PlanInputError('作业操作与当前学习会话不匹配。')
                self._require_homework_ocr_source(connection, existing['source_artifact_id'], existing['child_id'], session_id)
                return self._get_homework(connection, existing["homework_id"])
            self._require_homework_ocr_source(connection, source_artifact_id, session['child_id'], session_id)
            connection.execute(
                """
                INSERT INTO homework_item(
                  homework_id, child_id, session_id, subject, task_type, title, range_text,
                  expected_minutes, due_at, special_requirements, source_artifact_id,
                  ocr_confidence, confirmation_status, status, created_at, client_action_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          'child_confirmed', 'pending', ?, ?)
                """,
                (
                    homework_id,
                    session["child_id"],
                    session_id,
                    subject,
                    task_type,
                    title,
                    (range_text or "").strip() or None,
                    expected_minutes,
                    due_at or None,
                    (special_requirements or "").strip() or None,
                    source_artifact_id,
                    ocr_confidence,
                    now,
                    client_action_id,
                ),
            )
            connection.execute(
                "UPDATE daily_session SET homework_declared_empty = 0 WHERE session_id = ?",
                (session_id,),
            )
            connection.commit()
            return self._get_homework(connection, homework_id)

    def review_deferred_task(
        self,
        session_id: str,
        source_plan_item_id: str,
        decision: str,
        reason: str,
        client_action_id: str,
        *, source_version: str | None = None,
    ) -> dict:
        if decision not in {"continue_today", "completed_elsewhere", "cancelled"}:
            raise PlanInputError("不支持这种延期任务处理方式。")
        session = self.get_session(session_id)
        clean_reason = reason.strip()
        if len(clean_reason) < 2:
            raise PlanInputError("请记录这项延期任务今天如何处理。")
        review_id = str(uuid4())
        target_homework_id: str | None = None
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            repeated = connection.execute(
                "SELECT * FROM deferred_task_review WHERE client_action_id = ?",
                (client_action_id,),
            ).fetchone()
            if repeated:
                if (repeated['target_session_id'], repeated['child_id'], repeated['source_plan_item_id'],
                    repeated['decision'], repeated['reason']) != (
                    session_id, session['child_id'], source_plan_item_id, decision, clean_reason):
                    raise PlanInputError('延期任务请求编号已属于另一项处理，未改变原记录。')
                connection.rollback()
                return self._get_deferred_task_review(repeated["review_id"])
            current = connection.execute('SELECT status,state FROM daily_session WHERE session_id=?', (session_id,)).fetchone()
            if not current or current['status'] != 'active' or current['state'] != 'homework_capture':
                raise StaleSessionStateError('homework_capture', current['state'] if current else 'missing')
            already_reviewed = connection.execute(
                "SELECT review_id FROM deferred_task_review WHERE source_plan_item_id = ?",
                (source_plan_item_id,),
            ).fetchone()
            if already_reviewed:
                connection.rollback()
                raise PlanInputError("这项延期任务已经处理，不能重复改变昨天的决定。")
            candidate = next(
                (
                    item for item in self._deferred_task_candidates(
                        connection, session["child_id"], session_id
                    )
                    if item["source_plan_item_id"] == source_plan_item_id
                ),
                None,
            )
            if not candidate:
                connection.rollback()
                raise PlanInputError("找不到可处理的延期任务，可能属于另一个孩子或已经处理。")
            if source_version is not None and candidate['source_version'] != source_version:
                raise DeferredTaskChangedError(session_id, session['child_id'], client_action_id, candidate)
            if decision == "continue_today":
                target_homework_id = str(uuid4())
                connection.execute(
                    """
                    INSERT INTO homework_item(
                      homework_id, child_id, session_id, subject, task_type, title, range_text,
                      expected_minutes, due_at, special_requirements, source_artifact_id,
                      ocr_confidence, confirmation_status, status, created_at, client_action_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL,
                              'child_confirmed', 'pending', ?, ?)
                    """,
                    (
                        target_homework_id,
                        session["child_id"],
                        session_id,
                        candidate["subject"],
                        candidate["task_type"],
                        candidate["title"],
                        candidate["range_text"],
                        candidate["expected_minutes"],
                        candidate["due_at"],
                        candidate["special_requirements"],
                        now,
                        f"carryover:{review_id}",
                    ),
                )
                connection.execute(
                    "UPDATE daily_session SET homework_declared_empty = 0 WHERE session_id = ?",
                    (session_id,),
                )
            connection.execute(
                """
                INSERT INTO deferred_task_review(
                  review_id, source_plan_item_id, source_session_id, child_id,
                  target_session_id, decision, reason, target_homework_id,
                  reviewed_at, client_action_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    review_id,
                    source_plan_item_id,
                    candidate["source_session_id"],
                    session["child_id"],
                    session_id,
                    decision,
                    clean_reason,
                    target_homework_id,
                    now,
                    client_action_id,
                ),
            )
            connection.commit()
        return self._get_deferred_task_review(review_id)

    def update_homework(
        self,
        session_id: str,
        homework_id: str,
        subject: str,
        title: str,
        expected_minutes: int,
        task_type: str,
        range_text: str | None,
        due_at: str | None,
        special_requirements: str | None,
        source_artifact_id: str | None,
        ocr_confidence: float | None,
        client_action_id: str,
    ) -> dict:
        self._require_active_state(session_id, "homework_capture")
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT h.homework_id,h.child_id,h.session_id,h.source_artifact_id FROM homework_revision r JOIN homework_item h USING(homework_id) WHERE r.client_action_id = ?",
                (client_action_id,),
            ).fetchone()
            if existing:
                connection.rollback()
                if existing['session_id'] != session_id:
                    raise PlanInputError('作业操作与当前学习会话不匹配。')
                self._require_homework_ocr_source(connection, existing['source_artifact_id'], existing['child_id'], session_id)
                return self._get_homework(connection, existing["homework_id"])
            row = connection.execute(
                "SELECT * FROM homework_item WHERE homework_id = ? AND session_id = ?",
                (homework_id, session_id),
            ).fetchone()
            if not row:
                raise PlanInputError("找不到这项作业，可能已经切换了学习会话。")
            self._require_homework_ocr_source(connection, source_artifact_id, row['child_id'], session_id)
            before = {
                key: row[key] for key in (
                    "subject", "task_type", "title", "range_text", "expected_minutes",
                    "due_at", "special_requirements", "source_artifact_id", "ocr_confidence",
                )
            }
            after = {
                "subject": subject, "task_type": task_type, "title": title.strip(),
                "range_text": (range_text or "").strip() or None,
                "expected_minutes": expected_minutes, "due_at": due_at or None,
                "special_requirements": (special_requirements or "").strip() or None,
                "source_artifact_id": source_artifact_id, "ocr_confidence": ocr_confidence,
            }
            connection.execute(
                """
                UPDATE homework_item SET subject = ?, task_type = ?, title = ?, range_text = ?,
                  expected_minutes = ?, due_at = ?, special_requirements = ?,
                  source_artifact_id = ?, ocr_confidence = ?, confirmation_status = 'child_confirmed'
                WHERE homework_id = ?
                """,
                (
                    after["subject"], after["task_type"], after["title"], after["range_text"],
                    after["expected_minutes"], after["due_at"], after["special_requirements"],
                    after["source_artifact_id"], after["ocr_confidence"], homework_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO homework_revision(
                  revision_id, homework_id, session_id, before_json, after_json,
                  created_at, client_action_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()), homework_id, session_id,
                    json.dumps(before, ensure_ascii=False), json.dumps(after, ensure_ascii=False),
                    utc_now(), client_action_id,
                ),
            )
            connection.commit()
        with self.database.connect() as connection:
            return self._get_homework(connection, homework_id)

    def generate_plan(self, session_id: str, client_action_id: str) -> dict:
        with self.database.connect() as connection:
            prior = connection.execute(
                'SELECT plan_id,session_id,child_id FROM daily_plan WHERE client_action_id=?', (client_action_id,)
            ).fetchone()
        if prior:
            if prior['session_id'] != session_id:
                raise PlanInputError('计划操作标识与原学习会话不匹配。')
            # A successful request already moved the session to plan_review.
            # Recover that receipt before checking the state for a new plan.
            from .child_state_consumption import ChildStateConsumptionService
            ChildStateConsumptionService(self.database).bind_current(
                child_id=prior["child_id"], consumer_type="daily_plan",
                source_type="daily_plan", source_id=prior["plan_id"],
                capture_phase="legacy_recovery",
                purpose="recover_existing_daily_plan_state_binding",
            )
            return self.get_workspace(session_id)
        session = self._require_active_state(session_id, "homework_capture")
        from .child_state_consumption import ChildStateConsumptionService
        child_state_consumption = ChildStateConsumptionService(self.database)
        from .report_followup import ReportFollowupService
        report_followup = ReportFollowupService(self.database)
        report_followup.initialize()
        from .learning_input_carryover import LearningInputCarryoverService
        carryover_service = LearningInputCarryoverService(self.database)
        carryover_service.initialize()
        pet_vocabulary_summary: dict | None = None
        from .agent_runtime import AgentRuntimeStore
        learning_runtime = AgentRuntimeStore(self.database)
        from .rubric_calibration_transaction import calibrated_write
        from .paper_scoring import _run_reconciliation_withdrawal_fence
        def prepare_learning():
            with calibrated_write(self.database, session['child_id']) as connection:
                from .learning_obligations import withdraw_excluded_reviews
                withdraw_excluded_reviews(connection, session["child_id"], utc_now())
                from .paper_correct_verification import resume_pending_supply
                resume_pending_supply(connection, session["child_id"])
                from .school_open_reviews import reconcile_school_reviews
                school_reviews = reconcile_school_reviews(connection, session['child_id'], as_of=utc_now())
                child_grade = connection.execute(
                    "SELECT grade FROM child_profile WHERE child_id=?", (session["child_id"],)
                ).fetchone()
                if child_grade and child_grade['grade'] == '四年级':
                    from .writing_stage_reviews import reconcile_writing_stage_reviews
                    reconcile_writing_stage_reviews(connection, session['child_id'], as_of=utc_now())
                learning_runtime.reconcile_learning_state(session["child_id"], connection=connection)
                connection.commit()
                return school_reviews, child_grade
        school_reviews, child_grade = _run_reconciliation_withdrawal_fence(self.database, prepare_learning)
        if child_grade and child_grade["grade"] == "四年级":
            pet_vocabulary = PetVocabularyService(self.database)
            pet_vocabulary.sync_confirmed_textbook_lexemes(session["child_id"])
            pet_vocabulary_summary = pet_vocabulary.summary(session["child_id"])
        goal_action = GoalProgramService(self.database).prepare_daily_action(session["child_id"])
        specialized_service = SpecializedProgramService(self.database)
        competition_agent_action = (
            specialized_service.next_agent_action(session["child_id"], "competition_math")
            if "competition_math" in specialized_service.tracks_for_child(session["child_id"])
            else None
        )
        # Reconciliation above is part of plan input preparation. Freeze the
        # resulting state immediately before the plan transaction; the plan's
        # own rows would otherwise make a later snapshot a different state.
        planning_source_snapshot = child_state_consumption.capture(session["child_id"])
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT plan_id,session_id,child_id FROM daily_plan WHERE client_action_id = ?",
                (client_action_id,),
            ).fetchone()
            if existing:
                if existing['session_id'] != session_id:
                    raise PlanInputError('计划操作标识与原学习会话不匹配。')
                connection.rollback()
                child_state_consumption.bind_current(
                    child_id=existing["child_id"], consumer_type="daily_plan",
                    source_type="daily_plan", source_id=existing["plan_id"],
                    capture_phase="legacy_recovery",
                    purpose="recover_existing_daily_plan_state_binding",
                )
                return self.get_workspace(session_id)
            planning_now = family_time(datetime.fromisoformat(utc_now()))
            planning_date = planning_now.date().isoformat()
            report_inputs = report_followup.project_for_plan(session['child_id'], planning_date, connection=connection)
            from .supply_obligation_history import pending_history, skill as carried_skill, append_to_plan
            supply_carryovers = pending_history(connection, session['child_id'], session_id,
                as_of=datetime.fromisoformat(utc_now()))
            carried_pet_skills = {carried_skill(entry) for entry in supply_carryovers}
            from .learning_feedback import build_learning_feedback, PRIORITY as FEEDBACK_PRIORITY
            feedback = build_learning_feedback(connection, session["child_id"], datetime.fromisoformat(utc_now()))
            feedback_by_node = {row["knowledge_node_id"]: row for row in feedback["nodes"]}
            if self._deferred_task_candidates(connection, session["child_id"], session_id):
                raise PlanInputError("请先逐项处理上次延期的校内任务，再制定今天的计划。")
            homework = connection.execute(
                """
                SELECT homework_id, subject, title, expected_minutes, task_type,
                       due_at, special_requirements
                FROM homework_item WHERE session_id = ? AND status = 'pending'
                ORDER BY CASE WHEN due_at IS NULL OR due_at = '' THEN 1 ELSE 0 END,
                         due_at, created_at
                """,
                (session_id,),
            ).fetchall()
            if not homework and not session["homework_declared_empty"]:
                raise PlanInputError("至少确认一项作业后才能制定计划。")

            child = connection.execute(
                """
                SELECT cp.grade, fr.latest_end_time, fr.max_learning_minutes,
                       fr.task_segment_minutes, fr.break_minutes, fr.pet_min_minutes,
                       fr.competition_minutes
                FROM child_profile cp JOIN family_rule fr ON fr.child_id = cp.child_id
                WHERE cp.child_id = ?
                """,
                (session["child_id"],)
            ).fetchone()
            planning_context = connection.execute(
                """
                SELECT energy_level, fixed_commitment_minutes, fixed_commitment_note,
                       tomorrow_event_subject, tomorrow_event_title, tomorrow_event_minutes
                FROM session_planning_context WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
            grade = child["grade"] if child else "四年级"
            weekly_goals = self._ensure_weekly_goals(
                connection, session["child_id"], child, planning_now.date()
            ) if child else []
            weekly_by_track = {goal["track"]: goal for goal in weekly_goals}
            context = dict(planning_context) if planning_context else {
                "energy_level": "medium", "fixed_commitment_minutes": 0,
                "tomorrow_event_subject": None, "tomorrow_event_title": None,
                "tomorrow_event_minutes": 0,
            }
            total_cap = min([child['max_learning_minutes']] + [row['value_minutes'] for row in report_inputs['constraints']
                if row['constraint_type'] == 'daily_total_minutes_cap'])
            session_limit = max(0, total_cap - context["fixed_commitment_minutes"])
            if child["latest_end_time"]:
                local_now = planning_now
                hour, minute = (int(part) for part in child["latest_end_time"].split(":"))
                local_end = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                minutes_until_end = max(0, int((local_end - local_now).total_seconds() // 60))
                if minutes_until_end <= 0:
                    raise PlanInputError("已到家庭设定的最晚结束时间，请将未完成任务明确延期。")
                session_limit = min(session_limit, minutes_until_end)
            if session_limit <= 0:
                raise PlanInputError("扣除今晚固定安排后已没有可用时间，请将未完成任务明确延期。")
            learning_limit = max(1, int(session_limit * 0.9))
            segment_limit = child["task_segment_minutes"]
            from .child_model import ChildModelService
            parent_corrections = ChildModelService(self.database)._active_corrections(
                connection, session["child_id"], datetime.fromisoformat(utc_now()))
            segment_correction = next((row for row in parent_corrections if row["target_key"] == "task_segment_minutes"), None)
            if segment_correction:
                segment_limit = int(segment_correction["corrected_value"])
            if context["energy_level"] == "low":
                segment_limit = min(segment_limit, 15)
            segment_limit = min([segment_limit] + [row['value_minutes'] for row in report_inputs['constraints']
                if row['constraint_type'] == 'task_segment_minutes_cap'])
            pet_goal = weekly_by_track.get("pet")
            pet_reserve = (
                min(child["pet_min_minutes"], pet_goal["remaining_minutes"])
                if grade == "四年级" and pet_goal and pet_goal["remaining_sessions"] > 0
                else 0
            )
            if pet_reserve >= learning_limit:
                pet_reserve = 0
            history_rows = connection.execute(
                """
                SELECT pi.subject,
                       AVG(CAST(pi.actual_minutes AS REAL) / pi.expected_minutes) AS ratio,
                       COUNT(*) AS sample_count
                FROM plan_item pi JOIN daily_plan dp ON dp.plan_id = pi.plan_id
                WHERE dp.child_id = ? AND pi.source_type = 'school_homework'
                  AND pi.status = 'completed' AND pi.actual_minutes IS NOT NULL
                  AND pi.expected_minutes > 0
                GROUP BY pi.subject HAVING COUNT(*) >= 2
                """,
                (session["child_id"],),
            ).fetchall()
            historical_ratios = {
                row["subject"]: min(1.5, max(0.75, float(row["ratio"])))
                for row in history_rows
            }
            school_candidates: list[dict] = []
            load_by_task_type = {
                "written": "high",
                "memorization": "medium",
                "reading": "low",
                "correction": "high",
                "review": "medium",
                "preview": "medium",
                "test_prep": "high",
            }
            completion_by_task_type = {
                "written": "完成指定书面作业并核对漏题",
                "memorization": "完成背诵并进行一次脱稿自测",
                "reading": "完成指定阅读并说出主要内容",
                "correction": "完成订正并写明原错因",
                "review": "完成复习并用一道题或口述自测",
                "preview": "完成预习并记下一个不理解的问题",
                "test_prep": "完成测验前必要准备并标记仍不确定处",
            }
            for item in homework:
                ratio = historical_ratios.get(item["subject"], 1.0)
                task_remaining = max(5, round(item["expected_minutes"] * ratio))
                segment_number = 1
                while task_remaining > 0:
                    allocated_minutes = min(task_remaining, segment_limit)
                    suffix = f"（续{segment_number}）" if segment_number > 1 else ""
                    completion_standard = completion_by_task_type.get(
                        item["task_type"], "完成对应作业并由孩子确认"
                    )
                    if item["special_requirements"]:
                        completion_standard += f"；老师要求：{item['special_requirements']}"
                    school_candidates.append({
                        "homework_id": item["homework_id"], "subject": item["subject"],
                        "title": f"{item['title']}{suffix}", "minutes": allocated_minutes,
                        "source_type": "school_homework", "source_ref": item["homework_id"],
                        "completion_standard": completion_standard,
                        "cognitive_load": load_by_task_type.get(item["task_type"], "medium"),
                        "task_type": item["task_type"],
                        "due_at": item["due_at"],
                        "status": "pending",
                    })
                    task_remaining -= allocated_minutes
                    segment_number += 1

            selected: list[dict] = []
            deferred: list[dict] = []
            unplaced_obligations: list[dict] = []
            for missing in school_reviews['blocked']:
                node = connection.execute('''SELECT kn.subject,kn.title FROM diagnostic_item di
                    JOIN knowledge_node kn USING(knowledge_node_id) WHERE di.diagnostic_item_id=?''',
                    (missing['diagnostic_item_id'],)).fetchone()
                unplaced_obligations.append({'source_type': 'due_review',
                    'source_ref': 'school-review-source:' + missing['source_evidence_id'],
                    'subject': node['subject'], 'title': node['title'] + ' · 独立复习待补题',
                    'minutes': 12, 'reason': missing['reason'],
                    'planning_evidence': {'school_review_deficit': missing}})
            total = 0

            def append_with_break(item: dict, limit: int) -> bool:
                nonlocal total
                if item['minutes'] > segment_limit:
                    item['deferral_reason'] = '这项任务需要的完整时段超过当前单段上限；保留待安排，不压缩题目或自动延长学习。'
                    return False
                previous = next((row for row in reversed(selected) if row["source_type"] != "planned_break"), None)
                carried = carryover_service.owns_homework(connection, session['child_id'], item.get('homework_id'))
                needs_break = bool(previous and (previous["cognitive_load"] == "high" or carried))
                required = item["minutes"] + (child["break_minutes"] if needs_break else 0)
                if total + required > limit:
                    return False
                if needs_break:
                    selected.append({
                        "homework_id": None, "subject": "休息", "title": "站起来活动一下",
                        "minutes": child["break_minutes"], "source_type": "planned_break",
                        "source_ref": previous["source_ref"], "completion_standard": "喝水、远眺并活动肩颈",
                        "cognitive_load": "low", "task_type": "break", "status": "pending",
                    })
                    total += child["break_minutes"]
                selected.append(item)
                total += item["minutes"]
                return True

            reserved_transition_break = child["break_minutes"] if pet_reserve > 0 and school_candidates else 0
            if school_candidates and learning_limit - pet_reserve - reserved_transition_break < school_candidates[0]['minutes']:
                # A tiny evening budget must still permit the first required
                # school segment; enrichment remains an explicit owed task.
                if pet_reserve:
                    unplaced_obligations.append({'source_type': 'pet_daily', 'source_ref': session['child_id'],
                        'title': 'PET待安排练习', 'minutes': pet_reserve,
                        'reason': '今日短时安排先保留校内必做；PET练习仍欠练，不视为已完成。'})
                pet_reserve = 0
                reserved_transition_break = 0
            school_budget = max(0, learning_limit - pet_reserve - reserved_transition_break)
            tomorrow_remaining = context["tomorrow_event_minutes"]
            tomorrow_segment = 1
            while context["tomorrow_event_title"] and tomorrow_remaining > 0:
                allocated_minutes = min(tomorrow_remaining, segment_limit)
                suffix = f"（续{tomorrow_segment}）" if tomorrow_segment > 1 else ""
                candidate = {
                    "homework_id": None,
                    "subject": context["tomorrow_event_subject"] or "备考",
                    "title": f"明日重点 · {context['tomorrow_event_title']}{suffix}",
                    "minutes": allocated_minutes, "source_type": "tomorrow_event",
                    "source_ref": session_id, "completion_standard": "完成明日检查或考试前的必要准备",
                    "cognitive_load": "high", "task_type": "test_prep", "status": "pending",
                }
                if not append_with_break(candidate, school_budget):
                    deferred.append({**candidate, "status": "rescheduled"})
                tomorrow_remaining -= allocated_minutes
                tomorrow_segment += 1
            for candidate in school_candidates:
                if not append_with_break(candidate, school_budget):
                    deferred.append({**candidate, "status": "rescheduled"})

            # Stored mastery is history. Reopen withdrawn gates before ranking
            # or limiting candidates, using the mistake service's own projection.
            mistake_priority = {"relapsed": 100, "remediating": 90, "diagnosing": 80,
                                "mixed_transfer": 70, "near_transfer": 60}
            mistake_candidates = []
            for mistake in self._current_mistake_projection(connection, session["child_id"]):
                if mistake["status"] in mistake_priority:
                    mistake_candidates.append(mistake)
                elif mistake["status"] in {"fragile", "delayed_verification"}:
                    gate = mistake["mastery_gate"] or {}
                    missing = gate.get("missing") or []
                    due = gate.get(f"{missing[0]}_due_at") if missing else None
                    if due and datetime.fromisoformat(due) <= datetime.fromisoformat(utc_now()):
                        mistake_candidates.append(mistake)
            mistake_candidates.sort(key=lambda item: (-mistake_priority.get(item["status"], 50), item["updated_at"]))
            mistake_candidates = mistake_candidates[:2]
            selected_mistake_node_ids: set[str] = set()
            for mistake in mistake_candidates:
                mistake_item = {
                    "homework_id": None, "subject": mistake["subject"],
                    "title": f"错题强化 · {mistake['title']}", "minutes": 10,
                    "source_type": "mistake_review", "source_ref": mistake["mistake_id"],
                    "completion_standard": "完成本次错因、补救或变式步骤后停止，不追加刷题",
                    "cognitive_load": "medium", "task_type": "review", "status": "pending",
                }
                if append_with_break(mistake_item, school_budget):
                    selected_mistake_node_ids.add(mistake["knowledge_node_id"])
                else:
                    unplaced_obligations.append({**mistake_item, "reason": "今日校内任务与时间上限已占满；错题强化保留为下一次优先义务。"})

            due_reviews = connection.execute(
                """
                SELECT rt.review_task_id, rt.knowledge_node_id, rt.interval_label, rt.due_at, kn.subject, kn.title,
                       di.answer_type
                FROM review_task rt JOIN knowledge_node kn ON kn.knowledge_node_id = rt.knowledge_node_id
                JOIN diagnostic_item di ON di.diagnostic_item_id=rt.diagnostic_item_id
                WHERE rt.child_id = ? AND rt.status = 'pending' AND rt.due_at <= ?
                ORDER BY (rt.interval_label='next_day') DESC,rt.due_at,rt.review_task_id
                """,
                (session["child_id"], utc_now()),
            ).fetchall()
            # Evidence priority chooses among due obligations, never cancels them
            # or rewrites a task already being performed by the child.
            due_reviews = sorted(due_reviews, key=lambda row: (
                FEEDBACK_PRIORITY.get(feedback_by_node.get(row["knowledge_node_id"], {}).get("status"), 2),
                row["interval_label"] != "next_day", row["due_at"], row["review_task_id"],
            ))
            from .learning_obligations import project_child_mastery_evidence
            review_projections = project_child_mastery_evidence(connection, session["child_id"], as_of=utc_now()) if due_reviews else {}
            qualified_review_nodes = set()
            represented_writing_reviews = set()
            writing_review_actions = {}
            if grade == '四年级' and due_reviews:
                from .pet_writing_stage_plan import project_writing_stage_plan
                writing_review_actions = {item['planning_evidence']['review_task_id']: item
                    for item in project_writing_stage_plan(connection, session['child_id'], as_of=utc_now())['items']
                    if item['planning_evidence']['review_task_id']}
            for review in due_reviews:
                if review["knowledge_node_id"] in selected_mistake_node_ids or review["knowledge_node_id"] in qualified_review_nodes:
                    continue
                writing_review = review['answer_type'] == 'writing' and review['interval_label'] in {'7d', 'writing_stage_7d'}
                from .school_open_reviews import INTERVALS
                school_review = review['interval_label'] in INTERVALS
                review_minutes = 12 if writing_review or school_review else 10
                if writing_review:
                    represented_writing_reviews.add(review['review_task_id'])
                try:
                    self._validate_review_source(connection, review["review_task_id"], session["child_id"],
                        allowed_statuses=("pending",), evidence_projections=review_projections)
                    if writing_review and review['review_task_id'] not in writing_review_actions:
                        raise PlanInputError('写作复查的阶段依据已变化，保留原任务等待核对，不替换原题。')
                except PlanInputError as error:
                    unplaced_obligations.append({"source_type": "due_review", "source_ref": review["review_task_id"],
                        "subject": review["subject"], "title": review["title"], "minutes": review_minutes,
                        "reason": f"{error} 暂不让孩子执行，保留待处理记录。"})
                    continue
                qualified_review_nodes.add(review["knowledge_node_id"])
                if sum(item["source_type"] == "due_review" for item in selected) >= 2:
                    unplaced_obligations.append({"source_type": "due_review", "source_ref": review["review_task_id"],
                        "subject": review["subject"], "title": review["title"], "minutes": review_minutes,
                        "reason": "今天先处理证据优先级较高的两项复测；其余保留，不以刷题填满计划。"})
                    continue
                node_feedback = feedback_by_node.get(review["knowledge_node_id"])
                review_item = {
                    "homework_id": None, "subject": review["subject"],
                    "title": f"{review['title']} · {review['interval_label']}复测", "minutes": review_minutes,
                    "source_type": "due_review", "source_ref": review["review_task_id"],
                    "completion_standard": "先回忆并独立提交复测答案；需要提示时先核对步骤，不以听过讲解代替作答", "cognitive_load": "medium", "task_type": "review", "status": "pending",
                }
                if node_feedback:
                    review_item["completion_standard"] += "；" + node_feedback["reason"]
                if school_review:
                    from .school_review_execution import freeze_review
                    review_item['planning_evidence'] = {'school_review_binding': freeze_review(
                        connection, session['child_id'], review['review_task_id'], as_of=utc_now())}
                elif writing_review:
                    review_item['planning_evidence'] = writing_review_actions[review['review_task_id']]['planning_evidence']
                    review_item['task_type'] = 'writing'
                else:
                    from .pet_task_execution import is_listening_review_task, freeze_listening_review
                    if is_listening_review_task(connection, review_item):
                        review_item['planning_evidence'] = {'pet_question_binding': freeze_listening_review(
                            connection, session['child_id'], review['review_task_id'])}
                if not append_with_break(review_item, school_budget):
                    unplaced_obligations.append({**review_item, "reason": "今日时间预算不足；到期复测未取消，将在后续计划继续优先。"})

            retention_commitments = connection.execute(
                """
                SELECT ac.commitment_id, ac.payload_json, ac.commitment_type, ao.summary
                FROM agent_commitment ac
                JOIN agent_objective ao ON ao.objective_id = ac.objective_id
                WHERE ac.child_id = ? AND ao.child_id=ac.child_id
                  AND (ac.commitment_type = 'review' OR (ac.commitment_type='follow_up'
                    AND json_extract(ac.payload_json,'$.origin')='daily_learning_closure'))
                  AND ac.status IN ('pending', 'due') AND ac.due_at <= ?
                  AND ao.objective_type = 'learning' AND ao.status IN ('active','paused')
                  AND (ao.phase = 'retention_due' OR (ac.commitment_type='follow_up' AND ao.phase IN ('explain','check','remediate','transfer_check')))
                  AND NOT EXISTS (
                    SELECT 1 FROM plan_item pi
                    WHERE pi.source_type IN ('agent_retention','oral_daily_check')
                      AND pi.source_ref = ac.commitment_id
                      AND (pi.status IN ('pending', 'active', 'paused', 'completed')
                        OR (pi.source_type='oral_daily_check' AND pi.status='rescheduled' AND EXISTS (
                          SELECT 1 FROM agent_plan ap WHERE json_extract(ap.context_manifest_json,'$.goal_prompt_binding.plan_item_id')=pi.plan_item_id
                        )))
                  )
                ORDER BY ac.due_at LIMIT 2
                """,
                (session["child_id"], utc_now()),
            ).fetchall()
            for commitment in retention_commitments:
                try:
                    payload = json.loads(commitment["payload_json"])
                except (TypeError, json.JSONDecodeError):
                    payload = {}
                summary = str(payload.get("summary") or commitment["summary"] or "上次学习目标")
                from .writing_daily_followup import commitment_task_type
                retention_item = {
                    "homework_id": None,
                    "subject": str(payload.get("subject") or "复习"),
                    "title": ("重点再检查 · " if commitment["commitment_type"] == "follow_up" else "延迟复测 · ") + summary,
                    "minutes": 8,
                    "source_type": "agent_retention",
                    "source_ref": commitment["commitment_id"],
                    "completion_standard": "实际回答检查问题并保存评价证据；讲解或口头表示懂了不算完成" if commitment["commitment_type"] == "follow_up" else "独立回答延迟复测题并形成保留证据",
                    "cognitive_load": "medium",
                    "task_type": commitment_task_type(connection, session["child_id"], payload),
                    "status": "pending",
                }
                from .oral_daily_check import planning as plan_oral_check
                try:
                    oral_check=plan_oral_check(connection,session['child_id'],session_id,commitment['commitment_id'])
                except (ValueError,KeyError,TypeError):
                    oral_check={'status':'source_changed'}
                if oral_check is not None:
                    retention_item['source_type']='oral_daily_check'
                    retention_item['task_type']='oral'
                    if oral_check['status']!='ready':
                        unplaced_obligations.append({**retention_item,'reason':'原口述回答的来源或同能力新题还需核对；保留复查，不以模型提示或原题重复冒充新题。'})
                        continue
                    retention_item['planning_evidence']={'oral_daily_binding':oral_check['binding']}
                    retention_item['completion_standard']='回答已核对的同能力不同题并保存反馈；这是复查，不以模型评价或单次回答宣称掌握。'
                if not append_with_break(retention_item, school_budget):
                    unplaced_obligations.append({**retention_item, "reason": "今日时间预算不足；Agent 的延迟复测承诺继续保留。"})

            # A review for another skill does not discharge this goal's work.
            # Unmapped Agent commitments also cannot stand in for a known node.
            from .goal_foundation_plan import selected_review_for_node
            has_junior_goal_evidence = bool(goal_action and selected_review_for_node(
                connection, session['child_id'], goal_action.get('knowledge_node_id'), selected))
            has_declared_learning_input = bool(homework) or not bool(session["recall_declared_empty"])
            if (
                grade == "初三"
                and goal_action
                and has_declared_learning_input
                and not has_junior_goal_evidence
            ):
                junior_item = {
                    "homework_id": None,
                    "subject": goal_action["subject"],
                    "title": goal_action["title"],
                    "minutes": goal_action["minutes"],
                    "source_type": goal_action["source_type"],
                    "source_ref": goal_action["cycle_id"],
                    "completion_standard": goal_action["completion_standard"],
                    "cognitive_load": goal_action["cognitive_load"],
                    "task_type": goal_action["task_type"],
                    "planning_evidence": {key: goal_action[key] for key in (
                        "knowledge_node_id", "action", "roadmap_source_version", "catalog_binding", "assessment_binding", "requested_assessment_id", "daily_decomposition_source_version", "mastery_claim_allowed"
                    ) if key in goal_action},
                    "status": "pending",
                }
                if not goal_action['dispatchable']:
                    program = connection.execute('SELECT target_horizon_json FROM goal_program WHERE program_id=? AND child_id=?',
                        (goal_action['program_id'], session['child_id'])).fetchone()
                    if not program or json.loads(program['target_horizon_json']) != goal_action['target_horizon']:
                        raise PlanInputError('原学习目标在规划期间已变化，请核对后重新制定计划。')
                    junior_item['planning_evidence']['school_supply_gap'] = {
                        'version':'school_supply_gap_v1', 'program_id':goal_action['program_id'],
                        'cycle_id':goal_action['cycle_id'], 'target_horizon':goal_action['target_horizon']}
                    unplaced_obligations.append({**junior_item, 'reason':'原知识点缺少合格题源；保留这项义务，补题后需孩子重新确认，不自动启动或换题。'})
                elif not append_with_break(junior_item, learning_limit):
                    unplaced_obligations.append({**junior_item, "reason": "完成校内必做与到期复测后没有剩余时间；中考专项顺延。"})

            if grade == "四年级" and pet_reserve > 0:
                from .pet_training_plan import LABELS as PET_SKILL_LABELS, allocate_pet_practice
                due_vocabulary = int((pet_vocabulary_summary or {}).get("due_count", 0))
                pet_allocation = allocate_pet_practice(connection, session["child_id"], pet_reserve, due_vocabulary, as_of=utc_now())
                for pet_item in sorted(pet_allocation["items"], key=lambda item: item["task_type"] == "memorization"):
                    carried_kind = ('writing' if pet_item['task_type'] == 'writing' else
                        pet_item.get('planning_evidence', {}).get('pet_question_binding', {}).get('skill'))
                    if carried_kind in carried_pet_skills and not pet_item.get('planning_evidence', {}).get('review_task_id'):
                        continue
                    if pet_item.get('planning_evidence', {}).get('review_task_id') in represented_writing_reviews:
                        continue
                    stage_evidence = {key: pet_item.get('planning_evidence', {})[key] for key in (
                        'domain_id', 'knowledge_node_id', 'diagnostic_item_id', 'source_version',
                        'review_task_id', 'not_before', 'execution_status', 'stage_constraint', 'mastery_claim_allowed', 'pet_question_binding'
                    ) if key in pet_item.get('planning_evidence', {})}
                    pet_item["planning_evidence"] = {**stage_evidence,
                        **{key: pet_allocation[key] for key in ("version", "opportunities", "missing_skills", "mastery_claim_allowed")}}
                    if pet_item['task_type'] == 'writing':
                        from .specialized_stages import revalidate_stage_action
                        checked = revalidate_stage_action(connection, session['child_id'], 'pet',
                            binding={**stage_evidence, 'child_id': session['child_id'], 'track': 'pet'}, as_of=utc_now())
                        if checked['status'] != 'ready':
                            unplaced_obligations.append({**pet_item, 'reason': '写作阶段或原题依据已变化，核对后再安排，不改用另一题。'})
                            continue
                    elif not str(pet_item['source_ref'] or '').startswith(PET_VOCABULARY_SOURCE_PREFIX):
                        from .pet_task_execution import revalidate_pet_question, PetTaskExecutionError
                        try:
                            revalidate_pet_question(connection, session['child_id'], stage_evidence.get('pet_question_binding'))
                        except PetTaskExecutionError:
                            unplaced_obligations.append({**pet_item, 'reason': 'PET原题依据已变化，保留未安排项，不自动换题。'})
                            continue
                    if not append_with_break(pet_item, learning_limit):
                        unplaced_obligations.append({**pet_item, "reason": "今日校内必做超过可用时间；PET 训练顺延且不视为已完成。"})
                unplaced_obligations.extend(item for item in pet_allocation.get('unplaced_obligations', [])
                    if item.get('planning_evidence', {}).get('review_task_id') not in represented_writing_reviews
                    and not (str(item.get('source_ref', '')).startswith('pet-writing-gap:') and 'writing' in carried_pet_skills))
                for missing_skill in pet_allocation["missing_skills"]:
                    if missing_skill in carried_pet_skills:
                        continue
                    unplaced_obligations.append({"source_type": "pet_daily", "source_ref": f"pet-skill-gap:{missing_skill}",
                        "title": f"PET{PET_SKILL_LABELS[missing_skill]}题源待补齐", "subject": "英语", "minutes": 0,
                        "reason": "没有该技能当前可用的题目材料；保留供给缺口，不用单词练习冒充该技能训练。"})
            if grade == "四年级":
                competition_goal = weekly_by_track.get("competition_math")
                competition_minutes = (
                    min(child["competition_minutes"], competition_goal["remaining_minutes"])
                    if competition_goal and competition_goal["remaining_sessions"] > 0
                    else 0
                )
                if competition_minutes > 0:
                    from .goal_foundation_plan import foundation_candidate
                    foundation = foundation_candidate(connection, session['child_id'],
                        (competition_agent_action or {}).get('knowledge_node_id'), selected, as_of=utc_now())
                    if foundation:
                        if foundation['status'] == 'represented':
                            foundation['item'].setdefault('planning_evidence', {})['goal_foundation_support'] = foundation['support']
                        else:
                            foundation_item = foundation['item']
                            if foundation['status'] != 'ready':
                                unplaced_obligations.append({**foundation_item, 'reason': '前置知识缺少合格题源，保留具体补基础任务，不换题或自动记为完成。'})
                            elif foundation_item['minutes'] > competition_minutes or not append_with_break(foundation_item, learning_limit):
                                unplaced_obligations.append({**foundation_item, 'reason': '补基础任务超出当前完整时段或目标预算，保留待安排，不额外加时。'})
                        competition_minutes = 0
                if competition_minutes > 0:
                    competition_action = competition_agent_action
                    if competition_action and competition_action.get('execution_status') == 'ready':
                        from .specialized_stages import revalidate_stage_action
                        checked_stage = revalidate_stage_action(connection, session['child_id'], 'competition_math',
                            binding={**competition_action, 'child_id': session['child_id'], 'track': 'competition_math'})
                        if checked_stage['status'] != 'ready':
                            competition_action = {**competition_action, 'execution_status': checked_stage['status']}
                    action_minutes = int((competition_action or {}).get('minutes') or child['competition_minutes'])
                    stage_evidence = {key: competition_action[key] for key in (
                        'domain_id', 'diagnostic_item_id', 'knowledge_node_id', 'source_version', 'review_task_id',
                        'not_before', 'execution_status', 'stage_constraint', 'mastery_claim_allowed'
                    ) if key in competition_action} if competition_action else {}
                    if competition_action and competition_action.get('execution_status') == 'ready':
                        from .goal_roadmap import stage_goal_prerequisites
                        prerequisites = stage_goal_prerequisites(connection, session['child_id'],
                            competition_action['knowledge_node_id'], as_of=utc_now())
                        stage_evidence['goal_prerequisites'] = prerequisites
                        if prerequisites['status'] != 'ready':
                            competition_action = {**competition_action, 'execution_status': 'prerequisite_blocked'}
                    competition_item = {
                        "homework_id": None, "subject": "数学",
                        "title": (competition_action or {}).get("title") or "数学思维挑战",
                        "minutes": action_minutes,
                        "source_type": "competition_math",
                        "source_ref": (competition_action or {}).get("domain_id") or competition_goal["weekly_goal_id"],
                        "completion_standard": (competition_action or {}).get("completion_standard") or "完成一道题并说清主要思路",
                        "cognitive_load": "high", "task_type": "reasoning", "status": "pending",
                        "planning_evidence": stage_evidence,
                    }
                    if not competition_action or competition_action.get('execution_status') != 'ready':
                        reason = ('尚未到原定阶段复测日期，不能提前检查。' if competition_action and competition_action.get('execution_status') == 'waiting_due'
                                  else '先核对当前目标的前置知识；保留数学思维目标，不跳过基础直接安排挑战。' if competition_action and competition_action.get('execution_status') == 'prerequisite_blocked'
                                  else '当前阶段缺少合格题目或评分依据，保留供给缺口，不用泛化任务替代。')
                        unplaced_obligations.append({**competition_item, 'reason': reason})
                    elif action_minutes > competition_minutes:
                        unplaced_obligations.append({**competition_item, 'reason': '剩余专项预算不足以完成原阶段任务，整项保留待安排，不缩短题目冒充完成。'})
                    elif not append_with_break(competition_item, learning_limit):
                        unplaced_obligations.append({**competition_item, "reason": "今日可用时间不足；竞赛思维训练顺延且保留周目标欠额。"})

            active_habit = None
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='habit_experiment'"
            ).fetchone():
                active_habit = connection.execute(
                    """SELECT experiment_id,title,child_action,success_signal
                       FROM habit_experiment WHERE child_id=? AND status='active'
                       ORDER BY started_at DESC LIMIT 1""",
                    (session["child_id"],),
                ).fetchone()

            planned = selected + deferred

            plan_id = str(uuid4())
            now = utc_now()
            connection.execute(
                """
                INSERT INTO daily_plan(
                  plan_id, child_id, session_id, plan_date, status, total_minutes,
                  generator, version, created_at, client_action_id
                ) VALUES (?, ?, ?, ?, 'active', ?, 'constraint_rules_v4', 4, ?, ?)
                """,
                (plan_id, session["child_id"], session_id, planning_date, total, now, client_action_id),
            )
            for position, item in enumerate(planned, start=1):
                item['plan_item_id'] = str(uuid4())
                connection.execute(
                    """
                    INSERT INTO plan_item(
                      plan_item_id, plan_id, homework_id, subject, title, expected_minutes,
                      actual_minutes, priority, status, position, source_type, source_ref,
                      completion_standard, cognitive_load, task_type
                    ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item['plan_item_id'],
                        plan_id,
                        item["homework_id"],
                        item["subject"],
                        item["title"],
                        item["minutes"],
                        position,
                        item["status"],
                        position,
                        item["source_type"],
                        item["source_ref"],
                        item["completion_standard"],
                        item["cognitive_load"],
                        item["task_type"],
                    ),
                )
            decision_rows: list[dict] = []
            for item in planned:
                decision_rows.append({
                    "obligation_type": item["source_type"],
                    "source_ref": item.get("source_ref"),
                    "decision": "deferred" if item["status"] == "rescheduled" else "scheduled",
                    "reason": item.get('deferral_reason', "超过今日可用时间，已明确顺延。") if item["status"] == "rescheduled" else "已纳入今日时间表。",
                    "requested_minutes": int(item["minutes"]),
                    "allocated_minutes": 0 if item["status"] == "rescheduled" else int(item["minutes"]),
                    "metadata": {"title": item["title"], "subject": item["subject"], "plan_item_id": item['plan_item_id'],
                                 **({"planning_evidence": item["planning_evidence"]} if item.get("planning_evidence") else {})},
                })
            for item in unplaced_obligations:
                decision_rows.append({
                    "obligation_type": item["source_type"],
                    "source_ref": item.get("source_ref"),
                    "decision": "deferred" if item["source_type"] != "habit_experiment" else "suppressed",
                    "reason": item.get('deferral_reason', item["reason"]),
                    "requested_minutes": int(item.get("minutes") or 0),
                    "allocated_minutes": 0,
                    "metadata": {"title": item.get("title"), "subject": item.get("subject"),
                                 **({"planning_evidence": item["planning_evidence"]} if item.get("planning_evidence") else {})},
                })
            if active_habit:
                decision_rows.append({
                    "obligation_type": "habit_experiment",
                    "source_ref": active_habit["experiment_id"],
                    "decision": "applied",
                    "reason": "保留为按当前情境提供的可选支持，不作为作业完成条件，不额外增加孩子负担。",
                    "requested_minutes": 0,
                    "allocated_minutes": 0,
                    "metadata": {"title": active_habit["title"], "success_signal": active_habit["success_signal"],
                                 "support_role": "contextual_optional", "completion_condition": False},
                })
            if int(context.get("fixed_commitment_minutes") or 0) > 0:
                decision_rows.append({
                    "obligation_type": "fixed_commitment",
                    "source_ref": session_id,
                    "decision": "applied",
                    "reason": "先从家庭最大学习时长中扣除固定安排。",
                    "requested_minutes": int(context["fixed_commitment_minutes"]),
                    "allocated_minutes": int(context["fixed_commitment_minutes"]),
                    "metadata": {"note": context.get("fixed_commitment_note")},
                })
            decision_rows.append({
                "obligation_type": "learning_feedback", "source_ref": feedback["evidence_fingerprint"],
                "decision": "applied", "reason": "根据有效作答选择复测顺序；自述、观察与未核实评分不作为掌握结论。",
                "requested_minutes": 0, "allocated_minutes": 0,
                "metadata": {"version": feedback["version"], "status": feedback["status"],
                    "nodes": [{key: node[key] for key in ("knowledge_node_id", "status", "next_action", "evidence_refs")}
                              for node in feedback["nodes"]], "excluded": feedback["excluded"]},
            })
            if segment_correction:
                decision_rows.append({"obligation_type": "parent_model_correction",
                    "source_ref": segment_correction["correction_id"], "decision": "applied",
                    "reason": "采用家长有效分段修正；低精力与总时间上限仍优先，不增加学习预算。",
                    "requested_minutes": 0, "allocated_minutes": 0,
                    "metadata": {"task_segment_minutes": segment_limit, "reason": segment_correction["reason"]}})
            for decision in decision_rows:
                connection.execute(
                    """INSERT INTO plan_constraint_decision(
                         decision_id,plan_id,obligation_type,source_ref,decision,reason,
                         requested_minutes,allocated_minutes,metadata_json,created_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        str(uuid4()), plan_id, decision["obligation_type"], decision["source_ref"],
                        decision["decision"], decision["reason"], decision["requested_minutes"],
                        decision["allocated_minutes"], json.dumps(decision["metadata"], ensure_ascii=False), now,
                    ),
                )
            if connection.execute("""SELECT 1 FROM learning_input_carryover_event e WHERE child_id=? AND target_session_id=?
                AND revision=(SELECT MAX(revision) FROM learning_input_carryover_event WHERE obligation_id=e.obligation_id)
                AND json_extract(result_json,'$.status')='accepted_for_planning' LIMIT 1""", (session['child_id'], session_id)).fetchone():
                carryover_service.bind_generated_plan(session['child_id'], session_id, plan_id,
                                                     f'carryover-plan:{plan_id}', connection=connection)
            append_to_plan(connection, session['child_id'], session_id, plan_id, supply_carryovers, now=now)
            from .oral_supply_carryover import append_to_plan as append_oral_supply
            append_oral_supply(connection,session['child_id'],session_id,plan_id,now=now)
            report_followup.record_plan_read(session['child_id'], plan_id, report_inputs['fingerprint'],
                                            f'report-input:{plan_id}', connection=connection)
            child_state_consumption.persist_snapshot(
                child_id=session["child_id"],
                consumer_type="daily_plan",
                source_type="daily_plan",
                source_id=plan_id,
                snapshot=planning_source_snapshot,
                capture_phase="decision_input",
                purpose="generate_daily_plan_from_longitudinal_child_state",
                connection=connection,
            )
            connection.commit()
        self.transition(
            session_id,
            "homework_capture",
            "plan_review",
            "constraint_plan_generated",
            f"{client_action_id}:transition",
        )
        return self.get_workspace(session_id)

    def reorder_plan(
        self,
        session_id: str,
        strategy: str,
        reason: str,
        client_action_id: str,
    ) -> dict:
        if strategy not in {"school_first", "short_school_first"}:
            raise PlanInputError("不支持这种计划顺序。")
        self._require_active_state(session_id, "plan_review")
        with self.database.connect() as connection:
            existing = connection.execute(
                "SELECT plan_id FROM plan_revision WHERE client_action_id = ?",
                (client_action_id,),
            ).fetchone()
            if existing:
                return self.get_workspace(session_id)
            plan = connection.execute(
                "SELECT plan_id FROM daily_plan WHERE session_id = ? AND status = 'active'",
                (session_id,),
            ).fetchone()
            if not plan:
                raise PlanInputError("当前没有可调整的计划。")
            items = [dict(row) for row in connection.execute(
                """
                SELECT plan_item_id, source_type, source_ref, expected_minutes, status,
                       position, cognitive_load
                FROM plan_item WHERE plan_id = ? ORDER BY position
                """,
                (plan["plan_id"],),
            ).fetchall()]
            previous_order = [item["plan_item_id"] for item in items]
            break_items = [item for item in items if item["source_type"] == "planned_break"]
            items = [item for item in items if item["source_type"] != "planned_break"]
            source_order = {
                "tomorrow_event": 0,
                "school_homework": 1,
                "due_review": 2,
                "mistake_review": 2,
                "agent_retention": 2,
                "pet_daily": 3,
                "competition_math": 4,
            }
            if strategy == "short_school_first":
                homework_totals: dict[str, int] = {}
                homework_first_positions: dict[str, int] = {}
                for item in items:
                    if item["source_type"] != "school_homework":
                        continue
                    group_id = item["source_ref"] or item["plan_item_id"]
                    item["group_id"] = group_id
                    homework_totals[group_id] = homework_totals.get(group_id, 0) + item["expected_minutes"]
                    homework_first_positions[group_id] = min(
                        homework_first_positions.get(group_id, item["position"]), item["position"]
                    )
                items.sort(key=lambda item: (
                    item["status"] == "rescheduled",
                    source_order.get(item["source_type"], 9),
                    homework_totals.get(item.get("group_id", ""), item["position"]),
                    homework_first_positions.get(item.get("group_id", ""), item["position"]),
                    item["position"],
                ))
            else:
                items.sort(key=lambda item: (
                    item["status"] == "rescheduled",
                    source_order.get(item["source_type"], 9),
                    item["position"],
                ))
            active_items = [item for item in items if item["status"] != "rescheduled"]
            deferred_items = [item for item in items if item["status"] == "rescheduled"]
            ordered: list[dict] = []
            available_breaks = iter(break_items)
            for index, item in enumerate(active_items):
                ordered.append(item)
                if item["cognitive_load"] == "high" and index < len(active_items) - 1:
                    planned_break = next(available_breaks, None)
                    if planned_break:
                        ordered.append(planned_break)
            ordered.extend(available_breaks)
            ordered.extend(deferred_items)
            for position, item in enumerate(ordered, start=1):
                connection.execute(
                    "UPDATE plan_item SET position = ?, priority = ? WHERE plan_item_id = ?",
                    (position, position, item["plan_item_id"]),
                )
            connection.execute(
                """
                INSERT INTO plan_revision(
                  revision_id, plan_id, strategy, reason, previous_ordered_item_ids_json,
                  ordered_item_ids_json, created_at, client_action_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()), plan["plan_id"], strategy, reason,
                    json.dumps(previous_order),
                    json.dumps([item["plan_item_id"] for item in ordered]), utc_now(), client_action_id,
                ),
            )
            connection.commit()
        return self.get_workspace(session_id)

    def _current_mistake_projection(self, connection, child_id: str, mistake_ids=None) -> list[dict]:
        from .learning_obligations import project_child_mastery_evidence
        from .mistake_book import MistakeBookService
        rows = connection.execute("SELECT mistake_id FROM mistake_case WHERE child_id=? ORDER BY updated_at,mistake_id", (child_id,)).fetchall()
        selected = [row[0] for row in rows if mistake_ids is None or row[0] in mistake_ids]
        if not selected:
            return []
        projections = project_child_mastery_evidence(connection, child_id, as_of=utc_now())
        book = MistakeBookService(self.database)
        items = []
        for mistake_id in selected:
            # Reuse the read-only detail projection on this transaction snapshot.
            detail = book._detail(connection, mistake_id, _projections=projections)
            items.append({**{key: detail[key] for key in (
                "mistake_id", "subject", "title", "status", "recorded_status", "confidence",
                "updated_at", "source_evidence_id", "mastery_gate")},
                "knowledge_node_id": detail["knowledge_node"]["knowledge_node_id"]})
        return items

    def _report_mistake_projection(self, connection, session_id: str, reliable_report: dict) -> dict:
        session = connection.execute("SELECT child_id FROM daily_session WHERE session_id=?", (session_id,)).fetchone()
        rows = connection.execute("""SELECT mc.mistake_id,le.evidence_type FROM mistake_case mc
            JOIN learning_evidence le ON le.evidence_id=mc.source_evidence_id AND le.child_id=mc.child_id
            WHERE le.session_id=? AND mc.child_id=?""", (session_id, session["child_id"])).fetchall()
        types = {row["mistake_id"]: row["evidence_type"] for row in rows}
        # A later review belongs to its answer session even though the original
        # mistake belongs to an earlier day. Only actual currently qualified
        # answer receipts may associate that older case with this report.
        reliable_ids = {row["evidence_id"] for row in reliable_report["items"]}
        attempts = connection.execute("""SELECT mc.mistake_id, COALESCE(source.evidence_type,le.evidence_type) AS evidence_type, ma.evidence_id
            FROM mistake_attempt ma
            JOIN mistake_case mc ON mc.mistake_id=ma.mistake_id AND mc.child_id=ma.child_id
            JOIN learning_evidence le ON le.evidence_id=ma.evidence_id AND le.child_id=ma.child_id
              AND le.knowledge_node_id=mc.knowledge_node_id
            LEFT JOIN learning_evidence source ON source.evidence_id=mc.source_evidence_id AND source.child_id=mc.child_id
              AND source.knowledge_node_id=mc.knowledge_node_id
            JOIN daily_session ds ON ds.session_id=le.session_id AND ds.child_id=le.child_id
            WHERE le.session_id=? AND mc.child_id=?
              AND (mc.source_evidence_id IS NULL OR source.evidence_id IS NOT NULL)
              AND julianday(ma.created_at) BETWEEN julianday(ds.started_at) AND julianday(ds.ended_at)
              AND julianday(le.occurred_at) BETWEEN julianday(ds.started_at) AND julianday(ds.ended_at)""",
            (session_id, session["child_id"])).fetchall()
        for attempt in attempts:
            if attempt["evidence_id"] in reliable_ids:
                types.setdefault(attempt["mistake_id"], attempt["evidence_type"])
        invalid = {row["evidence_id"] for row in reliable_report["excluded"] if row["reason"] != "same_item_within_24h"}
        items = []
        for item in self._current_mistake_projection(connection, session["child_id"], types):
            evidence_type = types[item["mistake_id"]]
            if item["status"] == "excluded" or (item["source_evidence_id"] in invalid
                    and (evidence_type == "diagnostic_answer" or evidence_type.startswith("mistake_"))):
                continue
            items.append({**item, "evidence_type": evidence_type})
        fingerprint = hashlib.sha256(json.dumps({"session_id": session_id, "items": items},
            sort_keys=True, ensure_ascii=True).encode()).hexdigest()
        return {"items": items, "fingerprint": fingerprint}

    def report_evidence(self, session_id: str, behavior_context: dict | None = None) -> dict:
        session = self.get_session(session_id)
        if session["status"] != "completed":
            raise ReportNotReadyError("学习会话结束后才能生成日报。")
        from .daily_behavior_source import daily_behavior_fingerprint, refreshed_daily_behavior
        with self.database.connect() as connection:
            connection.execute('BEGIN')
            behavior_fingerprint = daily_behavior_fingerprint(connection, session['child_id'], session_id)
            from .pet_audio_memory import report_memory
            oral_memory = report_memory(connection, session['child_id'], session_id)
            from .document_revisit_memory import report_memory as revisit_memory
            revisits = revisit_memory(connection, session['child_id'], session_id)
        if behavior_context is None or (isinstance(behavior_context, dict)
                and behavior_context.get('behavior_source_fingerprint') is not None
                and behavior_context['behavior_source_fingerprint'] != behavior_fingerprint):
            behavior_context = refreshed_daily_behavior(self.database, session['child_id'], session_id)
            behavior_fingerprint = behavior_context['behavior_source_fingerprint']
        with self.database.connect() as connection:
            # Source qualification and task projection must use the same snapshot.
            connection.execute('BEGIN')
            from .parent_supply_followup import report_supply_facts
            from .report_calendar import study_day
            report_day = study_day(connection, session_id)
            # get_session is a public projection and omits ended_at; use the
            # stored cutoff for as-of weekly and cycle evidence.
            report_end = connection.execute('SELECT ended_at FROM daily_session WHERE session_id=? AND child_id=?',
                                            (session_id, session['child_id'])).fetchone()[0]
            try:
                supply_facts = report_supply_facts(connection, session['child_id'], session_id)
            except (ValueError, KeyError, TypeError) as error:
                raise ReportNotReadyError('原练习安排的历史来源不完整，已保留报告，请先核对原记录。') from error
            child = connection.execute(
                "SELECT child_id, nickname, grade, curriculum_region FROM child_profile WHERE child_id = ?",
                (session["child_id"],),
            ).fetchone()
            reflection = connection.execute(
                """
                SELECT learned_text, difficult_text, feeling, next_action, skipped, created_at
                FROM daily_reflection WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
            recalls = connection.execute(
                """
                SELECT subject, summary, confidence FROM classroom_recall
                WHERE session_id = ? AND confirmation_status = 'child_confirmed'
                ORDER BY created_at
                """,
                (session_id,),
            ).fetchall()
            from .report_task_provenance import report_task_source_fingerprint
            try:
                task_source_fingerprint = report_task_source_fingerprint(connection,session['child_id'],session_id)
            except (ValueError, sqlite3.Error) as error:
                raise ReportNotReadyError('任务或孩子身份与未确认来源不匹配，请先核对原记录。') from error
            tasks = connection.execute(
                """
                SELECT pi.subject, pi.title, pi.expected_minutes, pi.actual_minutes, pi.status,
                       pi.completion_evidence_type, pi.completion_note, pi.task_type,
                       pi.completion_checklist_json,
                       (
                         SELECT pie.payload_json FROM plan_item_event pie
                         WHERE pie.plan_item_id = pi.plan_item_id
                           AND pie.event_type = 'rescheduled'
                         ORDER BY pie.occurred_at DESC LIMIT 1
                       ) AS reschedule_payload_json
                FROM plan_item pi JOIN daily_plan dp ON dp.plan_id = pi.plan_id
                WHERE dp.session_id = ? AND dp.child_id = ? AND pi.source_type != 'planned_break'
                ORDER BY pi.position
                """,
                (session_id, session['child_id']),
            ).fetchall()
            coaching_feedback = connection.execute(
                """
                SELECT prf.feedback_id, prf.report_id, prf.child_id, prf.executed,
                       prf.effectiveness, prf.stress_rating, prf.notes_summary,
                       prf.updated_at
                FROM parent_report_feedback prf
                JOIN daily_report dr ON dr.report_id = prf.report_id AND dr.child_id=prf.child_id
                WHERE prf.child_id = ?
                ORDER BY prf.updated_at DESC, prf.feedback_id
                """,
                (session["child_id"],),
            ).fetchall()
            from .parent_action_evidence import ParentActionEvidenceService
            previous_feedback = ParentActionEvidenceService(self.database).feedback_for_child(connection, session["child_id"])
            behavior_rows = connection.execute(
                """
                SELECT intervention_id, trigger_state, child_response, confidence,
                       COALESCE(responded_at, created_at) AS occurred_at
                FROM intervention_record
                WHERE child_id = ? AND child_response IS NOT NULL
                ORDER BY COALESCE(responded_at, created_at) DESC LIMIT 3
                """,
                (session["child_id"],),
            ).fetchall()
            # Report usefulness is not evidence that its suggested action was
            # executed. Keep snapshot ratings separate from action outcomes.
            snapshot_feedback = []
            coaching_decisions = []
            snapshots_exist = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'parent_report_snapshot'"
            ).fetchone()
            if snapshots_exist:
                from .report_followup import ReportFollowupService
                for snapshot in connection.execute('SELECT * FROM parent_report_snapshot WHERE child_id=?',
                                                   (session['child_id'],)).fetchall():
                    current = ReportFollowupService.current_feedback(connection, snapshot)['feedback_current']
                    if current and (current['helpfulness'] is not None or current['stress_rating'] is not None):
                        snapshot_feedback.append({**current, 'snapshot_id': snapshot['snapshot_id'],
                            'report_id': snapshot['report_id'], 'notes_summary': current['notes'],
                            'updated_at': current['created_at']})
                snapshot_feedback.sort(key=lambda item: (item['updated_at'], item['revision']), reverse=True)
                snapshot_feedback = snapshot_feedback[:5]
                approved_decisions = connection.execute(
                    """
                    SELECT parent_edit_json, approved_at
                    FROM parent_report_snapshot
                    WHERE child_id = ? AND status = 'approved' AND approved_at IS NOT NULL
                    ORDER BY approved_at DESC LIMIT 5
                    """,
                    (session["child_id"],),
                ).fetchall()
                for item in approved_decisions:
                    try:
                        edit = json.loads(item["parent_edit_json"] or "{}")
                    except (TypeError, json.JSONDecodeError):
                        edit = {}
                    decision = edit.get("coaching_decision") if isinstance(edit, dict) else None
                    if decision in {"adopt", "custom", "skip"}:
                        coaching_decisions.append({"decision": decision, "approved_at": item["approved_at"]})
            recent_rows = connection.execute(
                """
                SELECT dr.evidence_json,dr.session_id,dr.report_date FROM daily_report dr
                JOIN daily_session ds ON ds.session_id=dr.session_id AND ds.child_id=dr.child_id
                WHERE dr.child_id = ? AND dr.session_id != ?
                  AND julianday(COALESCE(ds.ended_at,ds.started_at))<=julianday(
                    (SELECT COALESCE(ended_at,started_at) FROM daily_session WHERE session_id=?))
                ORDER BY dr.created_at DESC LIMIT 3
                """,
                (session["child_id"], session_id, session_id),
            ).fetchall()
            from .learning_feedback import session_report_feedback
            verified_recent = []
            for previous in recent_rows:
                checked = session_report_feedback(connection, previous["session_id"])
                if checked["items"] and json.loads(previous["evidence_json"]).get("reliable_learning_fingerprint") == checked["evidence_fingerprint"]:
                    verified_recent.append({"session_id": previous["session_id"], "evidence_fingerprint": checked["evidence_fingerprint"],
                        "fact": f"{previous['report_date']}的学习会话有{len(checked['items'])}条已核验作答，不等同已经掌握。"})
            family_rule = connection.execute(
                """
                SELECT max_learning_minutes, pet_min_minutes, competition_minutes
                FROM family_rule WHERE child_id = ?
                """,
                (session["child_id"],),
            ).fetchone()
            weekly_goals = connection.execute(
                """
                SELECT track, target_minutes, completed_minutes, status FROM (
                  SELECT wg.track, wg.target_minutes, wg.status,
                         COALESCE(SUM(CASE WHEN pi.status = 'completed'
                           THEN COALESCE(pi.actual_minutes, pi.expected_minutes) ELSE 0 END), 0) AS completed_minutes
                  FROM weekly_goal wg
                  LEFT JOIN daily_plan dp ON dp.child_id = wg.child_id
                    AND dp.plan_date BETWEEN wg.week_start AND wg.week_end
                    AND dp.plan_date <= ?
                  LEFT JOIN plan_item pi ON pi.plan_id = dp.plan_id
                    AND pi.source_type = CASE wg.track WHEN 'pet' THEN 'pet_daily' ELSE 'competition_math' END
                    AND julianday(COALESCE(pi.completed_at, (SELECT ended_at FROM daily_session
                      WHERE session_id=dp.session_id AND child_id=wg.child_id))) <= julianday(?)
                  WHERE wg.child_id = ?
                    AND ? BETWEEN wg.week_start AND wg.week_end
                  GROUP BY wg.weekly_goal_id
                ) ORDER BY CASE track WHEN 'pet' THEN 1 ELSE 2 END
                """,
                (report_day, report_end, session["child_id"], report_day),
            ).fetchall()
            goal_cycles = []
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'goal_cycle'"
            ).fetchone():
                goal_cycles = connection.execute(
                    """
                    SELECT gp.track, gp.title, gp.target_statement,
                           gc.target_minutes, gc.target_sessions,
                           COALESCE(SUM(CASE WHEN pi.status = 'completed'
                             THEN COALESCE(pi.actual_minutes, pi.expected_minutes) ELSE 0 END), 0) AS completed_minutes,
                           COUNT(DISTINCT CASE WHEN pi.status = 'completed' THEN dp.session_id END) AS completed_sessions,
                           gc.week_start, gc.week_end
                    FROM goal_cycle gc JOIN goal_program gp ON gp.program_id = gc.program_id
                    LEFT JOIN daily_plan dp ON dp.child_id = gc.child_id
                      AND dp.plan_date BETWEEN gc.week_start AND gc.week_end
                      AND dp.plan_date <= ?
                    LEFT JOIN plan_item pi ON pi.plan_id = dp.plan_id AND (
                      (gp.track = 'pet_path' AND pi.source_type = 'pet_daily') OR
                      (gp.track = 'competition_math_path' AND pi.source_type = 'competition_math') OR
                      (gp.track = 'junior_exam_readiness' AND pi.source_type IN ('junior_exam_goal', 'mistake_review', 'due_review'))
                    )
                      AND julianday(COALESCE(pi.completed_at, (SELECT ended_at FROM daily_session
                        WHERE session_id=dp.session_id AND child_id=gc.child_id))) <= julianday(?)
                    WHERE gc.child_id = ? AND gp.status = 'active'
                      AND ? BETWEEN gc.week_start AND gc.week_end
                    GROUP BY gc.cycle_id ORDER BY gp.priority DESC
                    """,
                    (report_day, report_end, session["child_id"], report_day),
                ).fetchall()
            planning_context = connection.execute(
                """
                SELECT energy_level, fixed_commitment_minutes, fixed_commitment_note,
                       tomorrow_event_subject, tomorrow_event_title, tomorrow_event_minutes
                FROM session_planning_context WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
            from .learning_feedback import session_report_feedback
            reliable_report = session_report_feedback(connection, session_id)
            knowledge_evidence = reliable_report["items"]
            mistake_projection = self._report_mistake_projection(connection, session_id, reliable_report)
            mistakes = mistake_projection["items"]
            pet_vocabulary_attempts = []
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pet_vocabulary_attempt'"
            ).fetchone():
                pet_vocabulary_attempts = connection.execute(
                    """
                    SELECT pva.attempt_id, pvc.surface, pvc.focus_kind,
                           pva.review_mode, pva.correct, pva.interval_days,
                           pva.next_due_at, pva.occurred_at
                    FROM pet_vocabulary_attempt pva
                    JOIN pet_vocabulary_card pvc ON pvc.card_id = pva.card_id
                    WHERE pva.session_id = ? ORDER BY pva.occurred_at
                    """,
                    (session_id,),
                ).fetchall()
        if not child or not reflection:
            raise ReportNotReadyError("日报证据不完整。")
        from .specialized_program import report_reference_fingerprint
        for _ in range(2):
            with self.database.connect() as connection:
                before_reference = report_reference_fingerprint(connection, session["child_id"])
            specialized_roadmaps = SpecializedProgramService(self.database).roadmaps(session["child_id"])
            with self.database.connect() as connection:
                specialized_reference = report_reference_fingerprint(connection, session["child_id"])
            if before_reference == specialized_reference:
                break
        else:
            raise ReportNotReadyError("专项学习依据正在更新，请稍后重试生成报告。")
        # Report-only legacy feedback cannot identify the advice actually seen.
        legacy_coaching_feedback = [{**dict(row), "binding_status": "unknown",
                                    "reason": "action_snapshot_missing"} for row in coaching_feedback]
        # Rebuild referenced facts from their receipts, never from previously generated prose.
        recent_confirmed_facts = [row["fact"] for row in verified_recent]
        goal_cycles_payload = [dict(row) for row in goal_cycles]
        goal_context = self._coaching_goal_context(
            dict(child), dict(family_rule) if family_rule else {},
            [dict(row) for row in weekly_goals], goal_cycles_payload,
        )
        legacy_behavior_intervention_feedback = []
        for row in behavior_rows:
            response = str(row["child_response"] or "").strip()
            if response == "accepted":
                summary = "孩子接受过一次温和学习提醒"
                kind = "intervention_accepted"
            elif response:
                display = {"later": "稍后", "less_like_this": "这类提醒少一点", "declined": "暂不接受"}.get(response, "暂不接受")
                summary = f"孩子对一次温和学习提醒选择“{display}”"
                kind = "intervention_declined"
            else:
                continue
            legacy_behavior_intervention_feedback.append({
                "kind": kind,
                "summary": summary,
                "confidence": float(row["confidence"]),
                "occurred_at": row["occurred_at"],
                "evidence_ref": coaching_evidence_ref(session["child_id"], "intervention", row["intervention_id"]),
                "source": "旧版提醒记录（兼容来源）",
                "legacy_source": True,
            })
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
        selected_behavior_feedback = behavior_intervention_feedback if behavior_context_authoritative else legacy_behavior_intervention_feedback
        coaching_evidence_sources = [{
            "evidence_ref": coaching_evidence_ref(session["child_id"], "session", session_id),
            "source": "本次已确认学习会话",
            "occurred_at": report_end or session.get("updated_at") or session.get("created_at"),
        }, *[{key: item[key] for key in ("evidence_ref", "source", "occurred_at") if item.get(key)} for item in selected_behavior_feedback if isinstance(item, dict)]]
        return {
            "session": session,
            "child": dict(child),
            "reflection": dict(reflection),
            "recalls": [dict(row) for row in recalls],
            "tasks": [self._plan_item_result(row) for row in tasks],
            "supply_obligations_as_of": supply_facts,
            "report_task_source_fingerprint": task_source_fingerprint,
            "supply_obligations_as_of_status": 'available' if report_end else 'unavailable',
            "coaching_feedback": previous_feedback,
            "legacy_coaching_feedback": legacy_coaching_feedback,
            "report_feedback": [dict(row) for row in snapshot_feedback],
            "coaching_decisions": coaching_decisions,
            "coaching_evidence_sources": coaching_evidence_sources,
            "behavior_intervention_feedback": behavior_intervention_feedback,
            "habit_experiments": habit_experiments,
            "behavior_model_summary": behavior_model_summary,
            "behavior_context_authoritative": behavior_context_authoritative,
            "behavior_source_fingerprint": behavior_fingerprint,
            'pet_audio_practice': oral_memory['items'],
            'document_revisit_practice': revisits['items'],
            'document_revisit_practice_fingerprint': revisits['fingerprint'],
            'pet_audio_practice_fingerprint': oral_memory['fingerprint'],
            "legacy_behavior_intervention_feedback": legacy_behavior_intervention_feedback,
            "recent_confirmed_facts": recent_confirmed_facts,
            "goal_context": goal_context,
            "goal_cycles": goal_cycles_payload,
            "specialized_roadmaps": specialized_roadmaps,
            "knowledge_evidence": [dict(row) for row in knowledge_evidence],
            "reliable_learning": reliable_report,
            "reliable_learning_dependencies": [{key: row[key] for key in ("session_id", "evidence_fingerprint")} for row in verified_recent],
            "specialized_reference_fingerprint": specialized_reference,
            "mistakes": [dict(row) for row in mistakes],
            "mistake_mastery_fingerprint": mistake_projection["fingerprint"],
            "pet_vocabulary_attempts": [dict(row) for row in pet_vocabulary_attempts],
            "planning_context": dict(planning_context) if planning_context else {
                "energy_level": "medium", "fixed_commitment_minutes": 0,
                "fixed_commitment_note": None, "tomorrow_event_subject": None,
                "tomorrow_event_title": None, "tomorrow_event_minutes": 0,
            },
        }

    @staticmethod
    def _coaching_goal_context(
        child: dict, family_rule: dict, weekly_goals: list[dict], goal_cycles: list[dict] | None = None
    ) -> str:
        """Return a parent-readable planning context, never a diagnosis or label."""
        grade = child.get("grade", "当前年级")
        program_parts = [
            f"{item['title']}本周{item['target_minutes']}分钟/{item['target_sessions']}次，已记录{item['completed_minutes']}分钟/{item['completed_sessions']}次"
            for item in (goal_cycles or [])
        ]
        if program_parts:
            return f"{grade}阶段目标：" + "；".join(program_parts)
        if grade == "四年级":
            goals = {item["track"]: item for item in weekly_goals}
            pet = goals.get("pet")
            competition = goals.get("competition_math")
            parts = []
            if pet:
                parts.append(f"PET周目标{pet['target_minutes']}分钟，已记录{pet['completed_minutes']}分钟")
            elif family_rule.get("pet_min_minutes"):
                parts.append(f"PET日常目标每次约{family_rule['pet_min_minutes']}分钟")
            if competition:
                parts.append(f"数学思维周目标{competition['target_minutes']}分钟，已记录{competition['completed_minutes']}分钟")
            elif family_rule.get("competition_minutes"):
                parts.append(f"数学思维每次约{family_rule['competition_minutes']}分钟")
            return "四年级阶段目标：" + "；".join(parts) if parts else "四年级阶段目标以当日确认的校内任务为先。"
        if grade == "初三":
            return "初三中考复习阶段：只围绕当晚已确认的校内或复习任务安排，不以临时加量回应压力。"
        cap = family_rule.get("max_learning_minutes")
        return f"{grade}阶段：以已确认任务为准" + (f"，本机计划上限{cap}分钟。" if cap else "。")

    @_guard_daily_report_source
    def save_daily_report(
        self, session_id: str, summary: dict, coaching: dict, source: str = "local_rules_v2",
        report_safe_context: dict | None = None,
        *, pre_persist: Callable | None = None,
    ) -> dict:
        from .parent_action_evidence import ParentActionEvidenceService
        action_evidence = ParentActionEvidenceService(self.database)
        action_evidence.initialize()
        evidence = self.report_evidence(session_id, behavior_context=report_safe_context)
        if summary.get('behavior_source_fingerprint') != evidence['behavior_source_fingerprint']:
            evidence = self.report_evidence(session_id)
        if (summary.get('report_task_source_fingerprint') != evidence['report_task_source_fingerprint']
                or summary.get("reliable_learning_fingerprint") != evidence["reliable_learning"]["evidence_fingerprint"]
                or summary.get('document_revisit_practice_fingerprint') != evidence['document_revisit_practice_fingerprint']
                or summary.get('pet_audio_practice_fingerprint') != evidence['pet_audio_practice_fingerprint']
                or summary.get('behavior_source_fingerprint') != evidence['behavior_source_fingerprint']
                or summary.get("mistake_mastery_fingerprint") != evidence["mistake_mastery_fingerprint"]
                or summary.get("reliable_learning_dependencies") != evidence["reliable_learning_dependencies"]
                or summary.get("specialized_reference_fingerprint") != evidence["specialized_reference_fingerprint"]):
            from .reports import build_daily_report
            summary, coaching = build_daily_report(evidence)
            source = "local_rules_revalidated_v3"
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            from .report_task_provenance import report_task_source_fingerprint
            try:
                initial_tasks = report_task_source_fingerprint(connection,evidence['child']['child_id'],session_id)
            except (ValueError, sqlite3.Error) as error:
                raise ReportNotReadyError('任务或孩子身份与未确认来源不匹配，请先核对原记录。') from error
            if initial_tasks != evidence['report_task_source_fingerprint']:
                raise ReportNotReadyError('报告提交前任务依据已变化，已保留学习记录，请重试生成报告。')
            existing = connection.execute(
                "SELECT report_id FROM daily_report WHERE session_id = ?", (session_id,)
            ).fetchone()
            if existing:
                connection.execute(
                    "UPDATE report_job SET status = 'completed', updated_at = ? WHERE session_id = ? AND report_type = 'daily'",
                    (now, session_id),
                )
                connection.commit()
                return self.get_daily_report(existing["report_id"])
            if pre_persist:
                pre_persist(connection)
            from .report_task_provenance import report_task_source_fingerprint
            try:
                current_tasks = report_task_source_fingerprint(connection,evidence['child']['child_id'],session_id)
            except (ValueError, sqlite3.Error) as error:
                raise ReportNotReadyError('任务或孩子身份与未确认来源不匹配，请先核对原记录。') from error
            if current_tasks != evidence['report_task_source_fingerprint']:
                raise ReportNotReadyError('报告提交前任务依据已变化，已保留学习记录，请重试生成报告。')
            from .learning_feedback import report_dependencies_match, session_report_feedback
            from .specialized_program import report_reference_fingerprint
            from .daily_behavior_source import daily_behavior_fingerprint
            from .pet_audio_memory import report_memory
            from .document_revisit_memory import report_memory as revisit_memory
            if (session_report_feedback(connection, session_id)["evidence_fingerprint"] != evidence["reliable_learning"]["evidence_fingerprint"]
                    or revisit_memory(connection, evidence['child']['child_id'], session_id)['fingerprint'] != evidence['document_revisit_practice_fingerprint']
                    or report_memory(connection, evidence['child']['child_id'], session_id)['fingerprint'] != evidence['pet_audio_practice_fingerprint']
                    or daily_behavior_fingerprint(connection, evidence['child']['child_id'], session_id) != evidence['behavior_source_fingerprint']
                    or self._report_mistake_projection(connection, session_id, evidence["reliable_learning"])["fingerprint"] != evidence["mistake_mastery_fingerprint"]
                    or not report_dependencies_match(connection, evidence["child"]["child_id"], evidence["reliable_learning_dependencies"])
                    or report_reference_fingerprint(connection, evidence["child"]["child_id"]) != evidence["specialized_reference_fingerprint"]):
                raise ReportNotReadyError("报告提交前学习依据已变化，已保留学习记录，请重试生成报告。")
            report_id = str(uuid4())
            evidence_manifest = {
                "session_id": session_id,
                "report_task_source_fingerprint": evidence['report_task_source_fingerprint'],
                "recall_count": len(evidence["recalls"]),
                "task_count": len(evidence["tasks"]),
                "knowledge_evidence_count": len(evidence.get("knowledge_evidence") or []),
                "reliable_learning_fingerprint": evidence["reliable_learning"]["evidence_fingerprint"],
                "reliable_learning_dependencies": evidence["reliable_learning_dependencies"],
                "specialized_reference_fingerprint": evidence["specialized_reference_fingerprint"],
                "mistake_count": len(evidence.get("mistakes") or []),
                "mistake_mastery_fingerprint": evidence["mistake_mastery_fingerprint"],
                "behavior_source_fingerprint": evidence['behavior_source_fingerprint'],
                'pet_audio_practice_fingerprint': evidence['pet_audio_practice_fingerprint'],
                'document_revisit_practice_fingerprint': evidence['document_revisit_practice_fingerprint'],
                "pet_vocabulary_attempt_count": len(evidence.get("pet_vocabulary_attempts") or []),
                "no_recall_declared": bool(evidence["session"]["recall_declared_empty"]),
                "no_homework_declared": bool(evidence["session"]["homework_declared_empty"]),
                "reflection_present": not bool(evidence["reflection"]["skipped"]),
                "reflection_skipped": bool(evidence["reflection"]["skipped"]),
                "contains_raw_audio_or_video": False,
            }
            # Only the parent's already-sanitized behavior contract is allowed
            # into a persisted report manifest. Keep authoritative emptiness so
            # a later report reader never falls back to old behavior records.
            if evidence.get("behavior_context_authoritative") is True:
                evidence_manifest.update({
                    "behavior_context_authoritative": True,
                    "behavior_intervention_feedback": evidence.get("behavior_intervention_feedback") or [],
                    "habit_experiments": evidence.get("habit_experiments") or [],
                    "behavior_model_summary": evidence.get("behavior_model_summary") or {},
                })
            from .report_calendar import study_day
            # A delayed report worker must not move a study session into a new
            # day/week. Sessions without a plan retain their Nanjing start day.
            report_date = study_day(connection, session_id)
            connection.execute(
                """
                INSERT INTO daily_report(
                  report_id, session_id, child_id, report_date, source, evidence_json,
                  summary_json, parent_coaching_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report_id,
                    session_id,
                    evidence["child"]["child_id"],
                    report_date,
                    source,
                    json.dumps(evidence_manifest, ensure_ascii=False),
                    json.dumps(summary, ensure_ascii=False),
                    json.dumps(coaching, ensure_ascii=False),
                    now,
                ),
            )
            action_evidence.freeze(connection, report_id)
            connection.execute(
                "UPDATE report_job SET status = 'completed', attempts = attempts + 1, updated_at = ? WHERE session_id = ? AND report_type = 'daily'",
                (now, session_id),
            )
            connection.commit()
        return self.get_daily_report(report_id)

    def prepare_report_retry(self, child_id: str, client_action_id: str) -> dict:
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT rr.retry_id, rr.job_id, rr.child_id, rr.status, rr.result_report_id,
                       rr.error_code, rj.session_id
                FROM report_retry_request rr JOIN report_job rj ON rj.job_id = rr.job_id
                WHERE rr.client_action_id = ?
                """,
                (client_action_id,),
            ).fetchone()
            if existing:
                if existing["child_id"] != child_id or not connection.execute(
                    """
                    SELECT 1 FROM report_job rj JOIN daily_session ds ON ds.session_id = rj.session_id
                    WHERE rj.job_id = ? AND rj.child_id = ? AND ds.child_id = ?
                    """,
                    (existing["job_id"], child_id, child_id),
                ).fetchone():
                    raise ReportNotReadyError("这次日报重试与当前孩子不匹配，请重新发起重试。")
                if existing["result_report_id"] and not connection.execute(
                    "SELECT 1 FROM daily_report WHERE report_id = ? AND child_id = ? AND session_id = ?",
                    (existing["result_report_id"], child_id, existing["session_id"]),
                ).fetchone():
                    raise ReportNotReadyError("这次日报重试与当前孩子不匹配，请重新发起重试。")
                connection.rollback()
                return dict(existing)
            job = connection.execute(
                """
                SELECT job_id, session_id FROM report_job
                WHERE child_id = ? AND report_type = 'daily' AND status = 'pending'
                ORDER BY updated_at, created_at LIMIT 1
                """,
                (child_id,),
            ).fetchone()
            if not job:
                raise ReportNotReadyError("当前没有待重试的日报。")
            if not connection.execute(
                "SELECT 1 FROM daily_session WHERE session_id = ? AND child_id = ?",
                (job["session_id"], child_id),
            ).fetchone():
                raise ReportNotReadyError("这次日报重试与当前孩子不匹配，请重新发起重试。")
            retry_id = str(uuid4())
            now = utc_now()
            connection.execute(
                """
                INSERT INTO report_retry_request(
                  retry_id, job_id, child_id, status, created_at, client_action_id
                ) VALUES (?, ?, ?, 'started', ?, ?)
                """,
                (retry_id, job["job_id"], child_id, now, client_action_id),
            )
            connection.execute(
                "UPDATE report_job SET status = 'processing', updated_at = ? WHERE job_id = ?",
                (now, job["job_id"]),
            )
            connection.commit()
        return {
            "retry_id": retry_id, "job_id": job["job_id"], "child_id": child_id,
            "status": "started", "result_report_id": None, "error_code": None,
            "session_id": job["session_id"],
        }

    def finish_report_retry(self, retry_id: str, report_id: str) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE report_retry_request
                SET status = 'completed', result_report_id = ?, completed_at = ?
                WHERE retry_id = ?
                """,
                (report_id, utc_now(), retry_id),
            )
            connection.commit()

    def fail_report_retry(self, retry_id: str, job_id: str, error_code: str) -> None:
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE report_retry_request
                SET status = 'failed', error_code = ?, completed_at = ? WHERE retry_id = ?
                """,
                (error_code, now, retry_id),
            )
            connection.execute(
                """
                UPDATE report_job SET status = 'pending', attempts = attempts + 1,
                  last_error = ?, updated_at = ? WHERE job_id = ?
                """,
                (error_code, now, job_id),
            )
            connection.commit()

    @_guard_daily_report_source
    def get_daily_report(self, report_id: str, *, _refresh_attempts: int = 2) -> dict:
        with self.database.connect() as connection:
            connection.execute('BEGIN')
            row = connection.execute(
                """
                SELECT report_id, session_id, child_id, report_date, source, evidence_json,
                       summary_json, parent_coaching_json, created_at
                FROM daily_report WHERE report_id = ?
                """,
                (report_id,),
            ).fetchone()
            if not row:
                raise UnknownReportError(report_id)
            manifest_for_tasks = json.loads(row['evidence_json'])
            saved_tasks = manifest_for_tasks.get('report_task_source_fingerprint')
            if not isinstance(saved_tasks,str) or not saved_tasks:
                raise LegacyReportUnverifiedError('旧版日报缺少任务来源校验信息，历史内容无法核验；原报告已保留，请联系开发者核对。')
            from .report_task_provenance import report_task_source_fingerprint
            try:
                current_tasks = report_task_source_fingerprint(connection,row['child_id'],row['session_id'])
            except (ValueError, sqlite3.Error) as error:
                raise ReportNotReadyError('任务或孩子身份与未确认来源不匹配，请先核对原记录。') from error
            if saved_tasks != current_tasks:
                raise ReportNotReadyError('原报告的任务依据已变化，已保留原报告，请先核对。')
            recalls = connection.execute(
                """
                SELECT subject, summary, confirmation_status, confidence, created_at
                FROM classroom_recall WHERE session_id = ? AND confirmation_status != 'rejected'
                ORDER BY created_at
                """,
                (row["session_id"],),
            ).fetchall()
            tasks = connection.execute(
                """
                SELECT pi.subject, pi.title, pi.status, pi.expected_minutes, pi.actual_minutes,
                       pi.completion_evidence_type, pi.completion_note, pi.completed_at,
                       pi.task_type, pi.completion_checklist_json, pi.completion_artifact_id,
                       ca.status AS completion_artifact_status,
                       ca.confidence AS completion_artifact_confidence,
                       CASE WHEN ca.local_path IS NOT NULL THEN 1 ELSE 0 END
                         AS completion_artifact_raw_stored,
                       (
                         SELECT pie.payload_json FROM plan_item_event pie
                         WHERE pie.plan_item_id = pi.plan_item_id
                           AND pie.event_type = 'rescheduled'
                         ORDER BY pie.occurred_at DESC LIMIT 1
                       ) AS reschedule_payload_json
                FROM plan_item pi JOIN daily_plan dp ON dp.plan_id = pi.plan_id
                LEFT JOIN capture_artifact ca ON ca.artifact_id = pi.completion_artifact_id
                WHERE dp.session_id = ? AND dp.child_id = ? AND pi.source_type != 'planned_break'
                ORDER BY pi.position
                """,
                (row["session_id"],row["child_id"]),
            ).fetchall()
            reflection = connection.execute(
                """
                SELECT learned_text, difficult_text, feeling, next_action, skipped
                FROM daily_reflection WHERE session_id = ?
                """,
                (row["session_id"],),
            ).fetchone()
            declarations = connection.execute(
                """
                SELECT recall_declared_empty, homework_declared_empty
                FROM daily_session WHERE session_id = ?
                """,
                (row["session_id"],),
            ).fetchone()
            from .learning_feedback import session_report_feedback
            reliable_report = session_report_feedback(connection, row["session_id"])
            from .parent_supply_followup import report_supply_matches
            supply_current = report_supply_matches(connection, row['child_id'], row['session_id'], json.loads(row['summary_json']))
            from .learning_feedback import report_dependencies_match
            dependencies_current = report_dependencies_match(connection, row["child_id"],
                json.loads(row["evidence_json"]).get("reliable_learning_dependencies"))
            from .daily_behavior_source import daily_behavior_fingerprint
            behavior_fingerprint = daily_behavior_fingerprint(connection, row['child_id'], row['session_id'])
            from .pet_audio_memory import report_memory, EMPTY_FINGERPRINT
            oral_memory = report_memory(connection, row['child_id'], row['session_id'])
            from .document_revisit_memory import report_memory as revisit_memory
            revisits = revisit_memory(connection, row['child_id'], row['session_id'])
            from .specialized_program import report_reference_fingerprint
            specialized_reference = report_reference_fingerprint(connection, row["child_id"])
            knowledge_evidence = reliable_report["items"]
            mistake_projection = self._report_mistake_projection(connection, row["session_id"], reliable_report)
            mistakes = mistake_projection["items"]
            pet_vocabulary_attempts = []
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pet_vocabulary_attempt'"
            ).fetchone():
                pet_vocabulary_attempts = connection.execute(
                    """
                    SELECT pva.attempt_id, pvc.surface, pvc.focus_kind,
                           pva.review_mode, pva.correct, pva.interval_days,
                           pva.next_due_at, pva.occurred_at
                    FROM pet_vocabulary_attempt pva
                    JOIN pet_vocabulary_card pvc ON pvc.card_id = pva.card_id
                    WHERE pva.session_id = ? ORDER BY pva.occurred_at
                    """,
                    (row["session_id"],),
                ).fetchall()
            feedback = connection.execute(
                """
                SELECT feedback_id, report_id, child_id, executed, effectiveness,
                       stress_rating, notes_summary, created_at, updated_at
                FROM parent_report_feedback WHERE report_id = ?
                """,
                (report_id,),
            ).fetchone()
            from .parent_action_evidence import ParentActionEvidenceService
            action_evidence = ParentActionEvidenceService(self.database)
            parent_action = action_evidence.current(connection, report_id)
        result = dict(row)
        for key in ("evidence_json", "summary_json", "parent_coaching_json"):
            result[key.removesuffix("_json")] = json.loads(result.pop(key))
        if (result["evidence"].get("reliable_learning_fingerprint") != reliable_report["evidence_fingerprint"]
                or result['evidence'].get('document_revisit_practice_fingerprint', EMPTY_FINGERPRINT) != revisits['fingerprint']
                or result['evidence'].get('pet_audio_practice_fingerprint', EMPTY_FINGERPRINT) != oral_memory['fingerprint']
                or result['evidence'].get('behavior_source_fingerprint') != behavior_fingerprint
                or result["evidence"].get("mistake_mastery_fingerprint") != mistake_projection["fingerprint"]
                or not dependencies_current or not supply_current
                or result["evidence"].get("specialized_reference_fingerprint") != specialized_reference):
            if _refresh_attempts <= 0:
                raise ReportNotReadyError("学习证据正在更新，已保留原报告，请稍后重试。")
            from .reports import build_daily_report
            context = result['evidence'] if result['evidence'].get('behavior_source_fingerprint') == behavior_fingerprint else None
            current = self.report_evidence(row["session_id"], behavior_context=context)
            summary, coaching = build_daily_report(current)
            fingerprint = current["reliable_learning"]["evidence_fingerprint"]
            manifest = {**result["evidence"], "reliable_learning_fingerprint": fingerprint,
                        "reliable_learning_dependencies": current["reliable_learning_dependencies"],
                        "specialized_reference_fingerprint": current["specialized_reference_fingerprint"],
                        "mistake_mastery_fingerprint": current["mistake_mastery_fingerprint"],
                        "behavior_source_fingerprint": current['behavior_source_fingerprint'],
                        'pet_audio_practice_fingerprint': current['pet_audio_practice_fingerprint'],
                        'document_revisit_practice_fingerprint': current['document_revisit_practice_fingerprint'],
                        **{key: current[key] for key in ('behavior_context_authoritative',
                            'behavior_intervention_feedback', 'habit_experiments', 'behavior_model_summary')},
                        "mistake_count": len(current["mistakes"]),
                        "knowledge_evidence_count": len(current["knowledge_evidence"])}
            action_evidence.initialize()
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                checked = session_report_feedback(connection, row["session_id"])
                try:
                    refreshed_tasks = report_task_source_fingerprint(connection,row['child_id'],row['session_id'])
                except (ValueError, sqlite3.Error) as error:
                    raise ReportNotReadyError('任务或孩子身份与未确认来源不匹配，请先核对原记录。') from error
                if refreshed_tasks != current['report_task_source_fingerprint']:
                    raise ReportNotReadyError('原报告的任务依据已变化，已保留原报告，请先核对。')
                if (checked["evidence_fingerprint"] == fingerprint and report_dependencies_match(
                        connection, row["child_id"], current["reliable_learning_dependencies"])
                        and daily_behavior_fingerprint(connection, row['child_id'], row['session_id']) == current['behavior_source_fingerprint']
                        and report_memory(connection, row['child_id'], row['session_id'])['fingerprint'] == current['pet_audio_practice_fingerprint']
                        and revisit_memory(connection, row['child_id'], row['session_id'])['fingerprint'] == current['document_revisit_practice_fingerprint']
                        and self._report_mistake_projection(connection, row["session_id"], checked)["fingerprint"] == current["mistake_mastery_fingerprint"]
                        and report_reference_fingerprint(connection, row["child_id"]) == current["specialized_reference_fingerprint"]
                        and report_supply_matches(connection, row['child_id'], row['session_id'], summary)):
                    changed = connection.execute("""UPDATE daily_report SET evidence_json=?,summary_json=?,
                        parent_coaching_json=?,source='local_rules_revalidated_v3'
                        WHERE report_id=? AND evidence_json=?""",
                        (json.dumps(manifest, ensure_ascii=False), json.dumps(summary, ensure_ascii=False),
                         json.dumps(coaching, ensure_ascii=False), report_id, row["evidence_json"])).rowcount
                    if changed:
                        action_evidence.freeze(connection, report_id)
                        connection.execute("""INSERT INTO audit_event
                            (audit_id,actor,action,child_id,summary_json,occurred_at)
                            VALUES (?,'system','daily_report_evidence_revalidated',?,?,?)""",
                            (str(uuid4()), row["child_id"], json.dumps({"report_id": report_id,
                             "previous_summary": result["summary"], "previous_coaching": result["parent_coaching"],
                             "previous_fingerprint": result["evidence"].get("reliable_learning_fingerprint"),
                             "evidence_fingerprint": fingerprint}, ensure_ascii=False), utc_now()))
                connection.commit()
            # Approved delivery snapshots and parent edits are separate immutable records.
            return self.get_daily_report(report_id, _refresh_attempts=_refresh_attempts - 1)
        result["evidence_detail"] = {
            "supply_obligations_as_of": result['summary'].get('supply_obligations_as_of', []),
            "recalls": [dict(item) for item in recalls],
            "tasks": [self._plan_item_result(item) for item in tasks],
            "reflection": dict(reflection) if reflection else None,
            "knowledge_evidence": [dict(item) for item in knowledge_evidence],
            "reliable_learning": reliable_report,
            "mistakes": [dict(item) for item in mistakes],
            "pet_vocabulary_attempts": [dict(item) for item in pet_vocabulary_attempts],
            "declarations": {
                "no_recall": bool(declarations["recall_declared_empty"]) if declarations else False,
                "no_homework": bool(declarations["homework_declared_empty"]) if declarations else False,
            },
        }
        result["parent_action"] = parent_action
        result["feedback"] = parent_action.get("feedback") or ({**dict(feedback),
            "binding_status": "unknown", "reason": "action_snapshot_missing"} if feedback else None)
        if result["feedback"] and result["feedback"]["executed"] is not None:
            result["feedback"]["executed"] = bool(result["feedback"]["executed"])
        return result

    def save_parent_report_feedback(
        self,
        report_id: str,
        executed: bool | None,
        effectiveness: int | None,
        stress_rating: int | None,
        notes_summary: str | None,
        *, action_snapshot_id: str | None = None, client_action_id: str | None = None,
        expected_revision: int | None = None, executed_at: str | None = None,
        operation: str = "record", reason: str | None = None,
    ) -> dict:
        if action_snapshot_id is not None:
            from .parent_action_evidence import ParentActionEvidenceService
            with self.database.connect() as connection:
                owner = connection.execute("SELECT child_id FROM daily_report WHERE report_id=?", (report_id,)).fetchone()
            if not owner:
                raise UnknownReportError(report_id)
            return ParentActionEvidenceService(self.database).record_feedback(
                child_id=owner["child_id"], report_id=report_id, snapshot_id=action_snapshot_id,
                client_action_id=client_action_id, expected_revision=expected_revision,
                executed=executed, executed_at=executed_at, effectiveness=effectiveness,
                stress_rating=stress_rating, notes_summary=notes_summary, operation=operation, reason=reason)
        if (executed is None or operation != "record" or any(value is not None for value in
                (client_action_id, expected_revision, executed_at, reason))):
            raise ValueError("PARENT_ACTION_SNAPSHOT_REQUIRED")
        now = utc_now()
        notes = (notes_summary or "").strip() or None
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            report = connection.execute(
                "SELECT child_id FROM daily_report WHERE report_id = ?", (report_id,)
            ).fetchone()
            if not report:
                raise UnknownReportError(report_id)
            existing = connection.execute(
                "SELECT feedback_id, created_at FROM parent_report_feedback WHERE report_id = ?",
                (report_id,),
            ).fetchone()
            feedback_id = existing["feedback_id"] if existing else str(uuid4())
            created_at = existing["created_at"] if existing else now
            connection.execute(
                """
                INSERT INTO parent_report_feedback(
                  feedback_id, report_id, child_id, executed, effectiveness,
                  stress_rating, notes_summary, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(report_id) DO UPDATE SET
                  executed = excluded.executed,
                  effectiveness = excluded.effectiveness,
                  stress_rating = excluded.stress_rating,
                  notes_summary = excluded.notes_summary,
                  updated_at = excluded.updated_at
                """,
                (
                    feedback_id, report_id, report["child_id"], int(executed), effectiveness,
                    stress_rating, notes, created_at, now,
                ),
            )
            connection.execute(
                """
                INSERT INTO audit_event(audit_id, actor, action, child_id, summary_json, occurred_at)
                VALUES (?, 'parent', 'parent_coaching_feedback_saved', ?, ?, ?)
                """,
                (
                    str(uuid4()), report["child_id"],
                    json.dumps({"report_id": report_id, "executed": executed}, ensure_ascii=False), now,
                ),
            )
            connection.commit()
            receipt = dict(connection.execute("SELECT * FROM parent_report_feedback WHERE report_id=?", (report_id,)).fetchone())
        return {**receipt, "executed": bool(receipt["executed"]), "binding_status": "unknown",
                "reason": "action_snapshot_missing"}

    def get_report_for_session(self, session_id: str) -> dict:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT report_id FROM daily_report WHERE session_id = ?", (session_id,)
            ).fetchone()
        if not row:
            raise UnknownReportError(session_id)
        return self.get_daily_report(row["report_id"])

    def list_daily_reports(self, child_id: str, limit: int | None = 30, *, source_issues: list | None = None) -> list[dict]:
        from .parent_reports import ParentReportService
        recovery = ParentReportService(self.database)
        open_ids = {item['source_id'] for item in recovery.source_issues(child_id, include_resolved=True)
                    if item['source_kind'] == 'daily_report' and item['status'] == 'open'}
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT report_id FROM daily_report WHERE child_id = ?
                ORDER BY report_date DESC, created_at DESC LIMIT ?
                """,
                (child_id, -1 if limit is None else limit),
            ).fetchall()
        reports = []
        for row in rows:
            report_id = row['report_id']
            with self.database.connect() as connection:
                problem = recovery.daily_source_problem(connection, child_id, report_id)
            if problem:
                recovery.record_source_issue(child_id, 'daily_report', report_id, problem)
                continue
            try:
                report = self.get_daily_report(report_id)
            except LegacyReportUnverifiedError:
                recovery.record_source_issue(child_id, 'daily_report', report_id, 'REPORT_TASK_SOURCE_LEGACY_UNVERIFIED')
            except (ReportNotReadyError, UnknownReportError, UnknownSessionError):
                recovery.record_source_issue(child_id, 'daily_report', report_id, 'REPORT_SOURCE_NOT_READY')
            except json.JSONDecodeError:
                recovery.record_source_issue(child_id, 'daily_report', report_id, 'REPORT_SOURCE_INVALID_JSON')
            else:
                recovered = recovery.record_source_issue(child_id, 'daily_report', report_id, None) if report_id in open_ids else None
                if not recovered or recovered['status'] != 'open':
                    reports.append(report)
        if source_issues is not None:
            source_issues.extend(recovery.source_issues(child_id))
        return reports

    def tutoring_context(self, session_id: str) -> dict:
        session = self.get_session(session_id)
        if session["status"] != "active":
            raise StaleSessionStateError("active_session", session["status"])
        with self.database.connect() as connection:
            child = connection.execute(
                "SELECT nickname, grade, curriculum_region FROM child_profile WHERE child_id = ?",
                (session["child_id"],),
            ).fetchone()
            recalls = connection.execute(
                """
                SELECT recall_id, subject, summary FROM classroom_recall
                WHERE session_id = ? AND confirmation_status = 'child_confirmed'
                ORDER BY created_at DESC LIMIT 6
                """,
                (session_id,),
            ).fetchall()
            task = connection.execute(
                """
                SELECT pi.plan_item_id, pi.subject, pi.title
                FROM plan_item pi JOIN daily_plan dp ON dp.plan_id = pi.plan_id
                WHERE dp.session_id = ? AND pi.status IN ('active', 'pending')
                  AND pi.source_type != 'planned_break'
                ORDER BY CASE pi.status WHEN 'active' THEN 0 ELSE 1 END, pi.position LIMIT 1
                """,
                (session_id,),
            ).fetchone()
        return {
            "session": session,
            "child": dict(child) if child else {},
            "recalls": [dict(row) for row in recalls],
            "task": dict(task) if task else None,
        }

    def start_model_call(
        self,
        session_id: str,
        child_id: str,
        purpose: str,
        provider: str,
        model: str,
        input_chars: int,
        prompt_version: str,
        context_manifest: dict,
        risk_level: str,
        provider_preference: str,
        policy_version: str,
        knowledge_version: str | None,
    ) -> str:
        from .agent_request_ownership import authorize_external_dispatch

        authorize_external_dispatch()
        call_id = str(uuid4())
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO model_call(
                  call_id, request_id, session_id, child_id, purpose, provider, model, status,
                  input_chars, prompt_version, context_manifest_json, risk_level,
                  provider_preference, policy_version, knowledge_version, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'started', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    call_id, call_id,
                    session_id,
                    child_id,
                    purpose,
                    provider,
                    model,
                    input_chars,
                    prompt_version,
                    json.dumps(context_manifest, ensure_ascii=False),
                    risk_level,
                    provider_preference,
                    policy_version,
                    knowledge_version,
                    utc_now(),
                ),
            )
            connection.commit()
        return call_id

    def finish_model_call(
        self,
        call_id: str,
        status: str,
        latency_ms: int | None,
        output_chars: int | None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        error_code: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        verification_status: str | None = None,
        safety_status: str | None = None,
        fallback_used: bool = False,
    ) -> None:
        with self.database.connect() as connection:
            existing = connection.execute(
                "SELECT provider, model FROM model_call WHERE call_id = ?", (call_id,)
            ).fetchone()
            actual_provider = provider or (existing["provider"] if existing else None)
            actual_model = model or (existing["model"] if existing else None)
            pricing = connection.execute(
                """
                SELECT input_price_cny_per_million, output_price_cny_per_million
                FROM model_pricing WHERE provider = ? AND model = ?
                """,
                (actual_provider, actual_model),
            ).fetchone() if actual_provider and actual_model else None
            estimated_cost = None
            if pricing and prompt_tokens is not None and completion_tokens is not None:
                estimated_cost = round(
                    (
                        prompt_tokens * pricing["input_price_cny_per_million"]
                        + completion_tokens * pricing["output_price_cny_per_million"]
                    ) / 1_000_000,
                    8,
                )
            connection.execute(
                """
                UPDATE model_call SET status = ?, latency_ms = ?, output_chars = ?,
                  prompt_tokens = ?, completion_tokens = ?, error_code = ?,
                  provider = COALESCE(?, provider), model = COALESCE(?, model),
                  verification_status = ?, safety_status = ?, fallback_used = ?, completed_at = ?
                  , input_price_cny_per_million = ?, output_price_cny_per_million = ?,
                  estimated_cost_cny = ?
                WHERE call_id = ?
                """,
                (
                    status,
                    latency_ms,
                    output_chars,
                    prompt_tokens,
                    completion_tokens,
                    error_code,
                    provider,
                    model,
                    verification_status,
                    safety_status,
                    int(fallback_used),
                    utc_now(),
                    pricing["input_price_cny_per_million"] if pricing else None,
                    pricing["output_price_cny_per_million"] if pricing else None,
                    estimated_cost,
                    call_id,
                ),
            )
            connection.commit()

    def list_model_calls(self, child_id: str, limit: int = 20) -> list[dict]:
        bounded_limit = max(1, min(int(limit), 100))
        with self.database.connect() as connection:
            if not connection.execute(
                "SELECT 1 FROM child_profile WHERE child_id = ? AND status = 'active'",
                (child_id,),
            ).fetchone():
                raise UnknownChildError(child_id)
            rows = connection.execute(
                """
                SELECT purpose, provider, model, status, latency_ms, prompt_tokens,
                       completion_tokens, input_price_cny_per_million,
                       output_price_cny_per_million, estimated_cost_cny,
                       verification_status, safety_status, fallback_used, error_code,
                       context_manifest_json, created_at, completed_at
                FROM model_call WHERE child_id = ?
                ORDER BY created_at DESC LIMIT ?
                """,
                (child_id, bounded_limit),
            ).fetchall()
            results = []
            for row in rows:
                item = dict(row)
                manifest = json.loads(item.pop("context_manifest_json") or "{}")
                privacy = manifest.get("privacy_transform") or {}
                item["privacy_summary"] = {
                    "policy_version": privacy.get("policy_version"),
                    "transmitted": privacy.get("transmitted"),
                    "redaction_count": int(privacy.get("redaction_count") or 0),
                    "redaction_types": list(privacy.get("redaction_types") or []),
                    "transmitted_recall_count": privacy.get("transmitted_recall_count"),
                    "omitted_sensitive_context_count": int(privacy.get("omitted_sensitive_context_count") or 0),
                    "blocked_categories": list(privacy.get("blocked_categories") or []),
                }
                results.append(item)
            return results

    def list_model_pricing(self) -> list[dict]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT provider, model, input_price_cny_per_million,
                       output_price_cny_per_million, currency, source, updated_at
                FROM model_pricing ORDER BY provider, model
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def save_model_pricing(
        self,
        provider: str,
        model: str,
        input_price_cny_per_million: float,
        output_price_cny_per_million: float,
    ) -> dict:
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO model_pricing(
                  provider, model, input_price_cny_per_million,
                  output_price_cny_per_million, currency, source, updated_at
                ) VALUES (?, ?, ?, ?, 'CNY', 'parent_configured', ?)
                ON CONFLICT(provider, model) DO UPDATE SET
                  input_price_cny_per_million = excluded.input_price_cny_per_million,
                  output_price_cny_per_million = excluded.output_price_cny_per_million,
                  updated_at = excluded.updated_at
                """,
                (provider, model, input_price_cny_per_million, output_price_cny_per_million, now),
            )
            connection.commit()
            row = connection.execute(
                """
                SELECT provider, model, input_price_cny_per_million,
                       output_price_cny_per_million, currency, source, updated_at
                FROM model_pricing WHERE provider = ? AND model = ?
                """,
                (provider, model),
            ).fetchone()
            return dict(row)

    def save_tutor_turn(
        self,
        session_id: str,
        plan_item_id: str | None,
        subject: str,
        question: str,
        answer: str,
        provider: str,
        model: str,
        client_action_id: str,
        hint_level: int = 1,
        parent_turn_id: str | None = None,
        verification_status: str = "not_applicable_hint",
        safety_status: str = "passed",
        output_envelope: dict | None = None,
        tutor_request_hash: str | None = None,
        ocr_confirmation_id: str | None = None,
        *,
        pre_persist: Callable[[], None] | None = None,
        reservation_attempt: int | None = None,
        on_persist: Callable[[sqlite3.Connection, str], None] | None = None,
    ) -> dict:
        session = self.get_session(session_id)
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT turn_id FROM tutor_turn WHERE client_action_id = ?", (client_action_id,)
            ).fetchone()
            if existing:
                if on_persist is not None:
                    on_persist(connection, existing['turn_id'])
                    connection.commit()
                return self._get_tutor_turn(connection, existing["turn_id"])
            # The read-only guard runs while competing source writes are locked.
            if pre_persist is not None:
                pre_persist()
            turn_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO tutor_turn(
                  turn_id, session_id, child_id, plan_item_id, subject, question, answer,
                  provider, model, status, hint_level, parent_turn_id, response_kind,
                  verification_status, safety_status, output_envelope_json, created_at,
                  client_action_id, tutor_request_hash, ocr_confirmation_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'completed', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    turn_id,
                    session_id,
                    session["child_id"],
                    plan_item_id,
                    subject,
                    question,
                    answer,
                    provider,
                    model,
                    hint_level,
                    parent_turn_id,
                    "full_explanation" if hint_level == 6 else "hint",
                    verification_status,
                    safety_status,
                    json.dumps(output_envelope, ensure_ascii=False) if output_envelope else None,
                    now,
                    client_action_id,
                    tutor_request_hash,
                    ocr_confirmation_id,
                ),
            )
            if on_persist is not None:
                on_persist(connection, turn_id)
            if reservation_attempt is not None:
                from .tutor_dispatch_authorization import TutorDispatchRevokedError
                changed = connection.execute("""UPDATE tutor_request_reservation
                    SET status='completed',tutor_turn_id=?,last_error_code=NULL,updated_at=?
                    WHERE action_id=? AND attempts=? AND (status='pending' OR
                        (status='interrupted' AND last_error_code='INTERRUPTED_UNKNOWN_OUTCOME'))""",
                    (turn_id, now, client_action_id, reservation_attempt)).rowcount
                if changed != 1:
                    raise TutorDispatchRevokedError()
                connection.execute("""UPDATE tutor_request_attempt
                    SET status='completed',error_code=NULL,finished_at=?
                    WHERE action_id=? AND attempt=? AND status IN ('pending','interrupted')""",
                    (now, client_action_id, reservation_attempt))
            connection.commit()
            return self._get_tutor_turn(connection, turn_id)

    def get_tutor_turn_for_action(self, client_action_id: str) -> dict | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT turn_id FROM tutor_turn WHERE client_action_id = ?", (client_action_id,)
            ).fetchone()
            return self._get_tutor_turn(connection, row["turn_id"]) if row else None

    def save_tutor_feedback(self, turn_id: str, response_text: str, outcome: str, client_action_id: str, *, pre_persist=None) -> dict:
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM tutor_feedback WHERE client_action_id = ?", (client_action_id,)
            ).fetchone()
            turn = connection.execute(
                "SELECT child_id, session_id FROM tutor_turn WHERE turn_id = ?", (turn_id,)
            ).fetchone()
            if not turn:
                raise UnknownTutorTurnError(turn_id)
            if existing:
                if (existing["turn_id"], existing["child_id"], existing["session_id"], existing["response_text"], existing["outcome"]) != (
                        turn_id, turn["child_id"], turn["session_id"], response_text, outcome):
                    raise TutorFeedbackConflictError("反馈动作已用于不同内容。")
                connection.commit()
                return self._get_tutor_feedback(connection, existing["feedback_id"])
            if pre_persist is not None:
                pre_persist(connection)
            hypothesis, confidence = self._error_hypothesis(response_text, outcome)
            feedback_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO tutor_feedback(
                  feedback_id, turn_id, child_id, session_id, response_text, outcome,
                  error_hypothesis, confidence, evidence_basis, created_at, client_action_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'child_self_report_single_turn', ?, ?)
                """,
                (feedback_id, turn_id, turn["child_id"], turn["session_id"], response_text, outcome, hypothesis, confidence, utc_now(), client_action_id),
            )
            connection.commit()
            return self._get_tutor_feedback(connection, feedback_id)

    @staticmethod
    def _error_hypothesis(text: str, outcome: str) -> tuple[str, float]:
        compact = text.replace(" ", "")
        candidates = (
            (("题目看不懂", "看不懂题目", "不知道问什么", "读不懂"), "题意理解候选"),
            (("公式不会", "知识点不会", "没学会", "不记得"), "知识缺失候选"),
            (("算错", "计算错", "抄错", "符号错"), "计算或操作候选"),
            (("没思路", "不会选方法", "不知道用什么"), "方法选择候选"),
            (("写不清", "不会表达", "过程不会写"), "表达书写候选"),
        )
        for phrases, label in candidates:
            if any(phrase in compact for phrase in phrases):
                return label, 0.65
        if outcome == "understood":
            return "本轮已理解（待变式验证）", 0.45
        return "原因未知，等待草稿或变式证据", 0.2

    @staticmethod
    def _get_tutor_feedback(connection, feedback_id: str) -> dict:
        row = connection.execute(
            """
            SELECT feedback_id, turn_id, response_text, outcome, error_hypothesis,
                   confidence, evidence_basis, created_at
            FROM tutor_feedback WHERE feedback_id = ?
            """,
            (feedback_id,),
        ).fetchone()
        return dict(row)

    @staticmethod
    def _get_tutor_turn(connection: sqlite3.Connection, turn_id: str) -> dict:
        row = connection.execute(
            """
            SELECT turn_id, session_id, child_id, plan_item_id, subject, question, answer,
                   provider, model, status, hint_level, parent_turn_id, response_kind,
                   verification_status, safety_status, strategy_experiment_id,
                   strategy_version, output_envelope_json, tutor_request_hash,
                   ocr_confirmation_id, created_at, client_action_id
            FROM tutor_turn WHERE turn_id = ?
            """,
            (turn_id,),
        ).fetchone()
        result = dict(row)
        raw_envelope = result.pop("output_envelope_json", None)
        result["output_envelope"] = json.loads(raw_envelope) if raw_envelope else None
        return result

    @staticmethod
    def _validate_review_source(
        connection, review_task_id: str, child_id: str,
        *, allowed_statuses=("pending", "completed"), evidence_projections=None,
    ) -> dict:
        review = connection.execute(
            """SELECT rt.* FROM review_task rt
               JOIN diagnostic_item di ON di.diagnostic_item_id=rt.diagnostic_item_id
                 AND di.knowledge_node_id=rt.knowledge_node_id AND di.status='active'
               JOIN knowledge_node kn ON kn.knowledge_node_id=rt.knowledge_node_id AND kn.status='active'
               JOIN child_profile cp ON cp.child_id=rt.child_id AND cp.status='active'
               WHERE rt.review_task_id=? AND rt.child_id=?""",
            (review_task_id, child_id),
        ).fetchone()
        if not review or review["status"] not in allowed_statuses:
            raise PlanInputError("复测不存在、已失效或不属于当前孩子。")
        from .learning_state import LearningStateService
        if not LearningStateService._eligible_diagnostic(connection, child_id, review["diagnostic_item_id"]):
            raise PlanInputError("复测题目来源已撤回、内容已改变或不属于当前孩子。")
        source_item = connection.execute('SELECT answer_type FROM diagnostic_item WHERE diagnostic_item_id=?',
                                        (review['diagnostic_item_id'],)).fetchone()
        from .school_open_reviews import INTERVALS, validate_review_source
        if review['interval_label'] in INTERVALS:
            try:
                if datetime.fromisoformat(review['due_at']) > datetime.fromisoformat(utc_now()):
                    raise ValueError('review not due')
                return validate_review_source(connection, child_id, review, as_of=utc_now())
            except (ValueError, TypeError, KeyError) as error:
                raise PlanInputError('学校复习的原始作答、独立题源、审核或到期依据不可用，请核对原任务。') from error
        from .paper_correct_verification import INTERVAL_LABEL as PAPER_CHECK_INTERVAL, validate_review_source as validate_paper_check
        if review['interval_label'] == PAPER_CHECK_INTERVAL:
            try:
                return validate_paper_check(connection, review_task_id, child_id)
            except (ValueError, TypeError, KeyError) as error:
                raise PlanInputError(
                    '纸面正确题的原评分、不同题面或独立复测依据已变化，保留记录并等待重新供题。'
                ) from error
        if source_item['answer_type'] == 'writing' and review['interval_label'] in {'7d', 'writing_stage_7d'}:
            from .writing_stage_reviews import validate_writing_review_source
            try:
                return validate_writing_review_source(connection, child_id, review, as_of=utc_now())
            except ValueError as error:
                raise PlanInputError(f'REVIEW_SOURCE_UNQUALIFIED: 写作复查的原稿、评分或到期依据尚未核实（{error}）。') from error
        if not LearningStateService._qualified_review_item(connection, child_id, review["diagnostic_item_id"]):
            raise PlanInputError("REVIEW_ITEM_UNQUALIFIED: 复测供题缺口，当前题目无法可靠精确评分，需要重新核实或补充合格题目。")
        try:
            due = datetime.fromisoformat(review["due_at"])
            now = datetime.fromisoformat(utc_now())
            if due.tzinfo is None or due > now:
                raise ValueError("review is not due")
        except (TypeError, ValueError):
            raise PlanInputError("复测尚未到期或到期时间无效。") from None
        # Follow repeat-review ancestry so excluding an original error also invalidates its descendants.
        sources = connection.execute(
            """WITH RECURSIVE sources(evidence_id) AS (
                 SELECT source_evidence_id FROM review_task WHERE review_task_id=?
                 UNION
                 SELECT parent.source_evidence_id FROM sources s
                 JOIN diagnostic_attempt da ON da.evidence_id=s.evidence_id
                 JOIN review_task parent ON parent.review_task_id=da.review_task_id
               )
               SELECT s.evidence_id, le.child_id, le.knowledge_node_id,
                 EXISTS(SELECT 1 FROM mistake_case mc
                   WHERE mc.child_id=? AND mc.status='excluded' AND (
                     mc.source_evidence_id=s.evidence_id OR EXISTS(
                       SELECT 1 FROM mistake_occurrence mo WHERE mo.mistake_id=mc.mistake_id
                         AND mo.evidence_id=s.evidence_id))) AS excluded
               FROM sources s LEFT JOIN learning_evidence le ON le.evidence_id=s.evidence_id""",
            (review_task_id, child_id),
        ).fetchall()
        if not sources or any(
            source["child_id"] != child_id
            or source["knowledge_node_id"] != review["knowledge_node_id"]
            or source["excluded"] for source in sources
        ):
            raise PlanInputError("复测来源已删除、被排除或与当前孩子及知识点不匹配。")
        from .learning_obligations import project_mastery_evidence
        projection = (project_mastery_evidence(connection, child_id, review["knowledge_node_id"], as_of=utc_now())
            if evidence_projections is None else evidence_projections.get(review["knowledge_node_id"], {}))
        effective_ids = {row["evidence_id"] for row in projection.get("effective_evidence", [])}
        if any(source["evidence_id"] not in effective_ids for source in sources):
            raise PlanInputError("REVIEW_SOURCE_UNQUALIFIED: 复测来源作答未通过有效证据核验，需先核实评分、重复作答或撤回状态。")
        return dict(review)

    def _validate_plan_item_completion_evidence(self, connection, item) -> dict | None:
        """Validate a due-review execution receipt, not a claim of mastery."""
        if item["source_type"] != "due_review":
            return None
        review = self._validate_review_source(
            connection, item["source_ref"], item["child_id"], allowed_statuses=("completed",)
        )
        from .school_open_reviews import is_school_review
        if is_school_review(connection, item):
            from .school_review_execution import validate_completion
            try:
                return validate_completion(connection, item)
            except (ValueError, TypeError, KeyError) as error:
                raise PlanInputError('本次学校复习需要原计划绑定的新作答和可靠审核，只说完成了或使用旧题不能代替。') from error
        from .pet_task_execution import is_listening_review_task, validate_pet_task_completion, PetTaskExecutionError
        if is_listening_review_task(connection, item):
            try:
                result = validate_pet_task_completion(connection, item)
                if result['evidence_id'] != review['result_evidence_id']:
                    raise PetTaskExecutionError('pet_review_result_changed')
                return result
            except PetTaskExecutionError as error:
                raise PlanInputError('听力复测必须保留本次原题、播放和原回答回执，不能用另一题代替。') from error
        receipt = connection.execute(
            """SELECT da.attempt_id, da.evidence_id, da.diagnostic_item_id, da.child_id,
                      da.session_id, da.submitted_answer, da.correctness, da.feedback,
                      da.review_task_id, da.created_at
               FROM diagnostic_attempt da JOIN learning_evidence le ON le.evidence_id=da.evidence_id
               JOIN daily_session ds ON ds.session_id=da.session_id AND ds.child_id=da.child_id
               WHERE da.review_task_id=? AND da.evidence_id=? AND da.diagnostic_item_id=?
                 AND da.child_id=? AND da.session_id=? AND le.child_id=da.child_id
                 AND le.session_id=da.session_id AND le.knowledge_node_id=?
                 AND le.evidence_type='diagnostic_answer' AND da.correctness=le.correctness""",
            (item["source_ref"], review["result_evidence_id"], review["diagnostic_item_id"],
             item["child_id"], item["session_id"], review["knowledge_node_id"]),
        ).fetchone()
        if not receipt or not receipt["submitted_answer"].strip() or not item["started_at"]:
            raise PlanInputError("请先回答当前绑定复测并保存作答，口头确认或勾选不能完成复测。")
        if self._is_pet_writing_task(connection, item):
            from .writing_stage_reviews import validate_writing_review_result
            prompt = self._stage_prompt_row(connection, item)
            if not prompt or prompt['attempt_id'] != receipt['attempt_id']:
                raise PlanInputError('写作复查没有本次任务绑定的原稿回执，不能用其他作文代替。')
            try:
                validate_writing_review_result(connection, child_id=item['child_id'], session_id=item['session_id'],
                    review_task_id=review['review_task_id'], prompt_id=prompt['prompt_id'],
                    evidence_id=receipt['evidence_id'], as_of=utc_now())
            except ValueError as error:
                raise PlanInputError(f'写作复查的独立原稿或可靠评分尚未核实（{error}）。') from error
        try:
            started = datetime.fromisoformat(item["started_at"])
            answered = datetime.fromisoformat(receipt["created_at"])
            completed = datetime.fromisoformat(review["completed_at"])
            now = datetime.fromisoformat(utc_now())
            valid_time = started <= answered <= completed <= now
        except (TypeError, ValueError):
            valid_time = False
        if not valid_time:
            raise PlanInputError("复测作答回执不属于本次任务执行。")
        return dict(receipt)

    def plan_review_question(self, plan_item_id: str) -> dict:
        """Read the exact bound question or a durable receipt without starting or scoring anything."""
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            item = connection.execute(
                """SELECT pi.*, dp.session_id, dp.child_id FROM plan_item pi
                   JOIN daily_plan dp ON dp.plan_id=pi.plan_id WHERE pi.plan_item_id=?""",
                (plan_item_id,),
            ).fetchone()
            if not item:
                raise UnknownPlanItemError(plan_item_id)
            if item["source_type"] != "due_review" or item["status"] not in {"active", "paused", "completed"}:
                raise PlanItemStateError("请先开始当前复测任务。")
            session = connection.execute(
                "SELECT child_id, state, status FROM daily_session WHERE session_id=?", (item["session_id"],)
            ).fetchone()
            if not session or session["child_id"] != item["child_id"]:
                raise PlanInputError("复测计划与学习会话所属孩子不匹配。")
            if session["state"] != "study" or session["status"] != "active":
                raise StaleSessionStateError("study", session["state"])
            review = self._validate_review_source(connection, item["source_ref"], item["child_id"])
            attempt = None
            if review["status"] == "completed" or item["status"] == "completed":
                attempt = self._validate_plan_item_completion_evidence(connection, item)
            question = connection.execute(
                """SELECT di.diagnostic_item_id, di.prompt, di.answer_type, di.difficulty,
                          di.stimulus_text, di.stimulus_asset, di.assessment_mode,
                          kn.knowledge_node_id, kn.track, kn.subject, kn.title
                   FROM diagnostic_item di JOIN knowledge_node kn USING(knowledge_node_id)
                   WHERE di.diagnostic_item_id=?""", (review["diagnostic_item_id"],)
            ).fetchone()
            from .pet_skill_gate import pet_skill_gate
            public_question = dict(question)
            if question['track'] == 'school' and pet_skill_gate(connection, item['child_id'],
                    question['diagnostic_item_id'])['modality'] == 'listening':
                public_question['stimulus_text'] = None
                public_question['pet_audio_modality'] = 'listening'
            return {
                **public_question, "plan_item_id": plan_item_id, "child_id": item["child_id"],
                "session_id": item["session_id"], "review_task_id": review["review_task_id"],
                "review_interval": review["interval_label"], "due_at": review["due_at"],
                "review_status": review["status"], "attempt": attempt,
            }

    def start_plan_item(
        self, plan_item_id: str, client_action_id: str, allow_planned_break: bool = False,
        *, authorized_turn_id: str | None = None, connection=None,
    ) -> dict:
        now = utc_now()
        with self._write_transaction(connection) as connection:
            if authorized_turn_id:
                from .task_start_binding import TaskStartBinding
                TaskStartBinding.authorize(connection, authorized_turn_id, plan_item_id, "start")
            existing_event = connection.execute(
                "SELECT plan_item_id, event_type FROM plan_item_event WHERE client_action_id = ?",
                (client_action_id,),
            ).fetchone()
            if existing_event:
                if existing_event["plan_item_id"] != plan_item_id or existing_event["event_type"] != "started":
                    raise PlanInputError("任务操作标识与原操作不匹配。")
                return self._get_plan_item_by_id(existing_event["plan_item_id"], connection=connection)

            item = connection.execute(
                """
                SELECT pi.plan_item_id, pi.plan_id, pi.status, pi.source_type, pi.source_ref, pi.task_type,
                       dp.session_id, dp.child_id
                FROM plan_item pi JOIN daily_plan dp ON dp.plan_id = pi.plan_id
                WHERE pi.plan_item_id = ?
                """,
                (plan_item_id,),
            ).fetchone()
            if not item:
                raise UnknownPlanItemError(plan_item_id)
            self._validate_replanned_item(connection, plan_item_id)
            self._validate_stage_task_start(connection, item, authorized_turn_id)
            self._validate_pet_task_start(connection, item, authorized_turn_id)
            self._validate_goal_catalog_source(connection, item)
            if item["source_type"] == "planned_break" and not allow_planned_break:
                raise PlanItemStateError("计划休息必须通过休息流程开始。")
            session = connection.execute(
                "SELECT child_id, state, status FROM daily_session WHERE session_id = ?",
                (item["session_id"],),
            ).fetchone()
            if item["source_type"] == "due_review" and session and session["child_id"] != item["child_id"]:
                raise PlanInputError("复测计划与学习会话所属孩子不匹配。")
            if not session or session["state"] != "study" or session["status"] != "active":
                raise StaleSessionStateError("study", session["state"] if session else "missing")
            if item["status"] != "pending":
                raise PlanItemStateError("只有等待中的任务可以开始。")
            if item["source_type"] == "due_review":
                self._validate_review_source(
                    connection, item["source_ref"], item["child_id"], allowed_statuses=("pending",)
                )
            active = connection.execute(
                "SELECT plan_item_id FROM plan_item WHERE plan_id = ? AND status = 'active'",
                (item["plan_id"],),
            ).fetchone()
            if active and active["plan_item_id"] != plan_item_id:
                raise PlanItemStateError("请先完成当前任务，再开始下一项。")
            connection.execute(
                """
                UPDATE plan_item SET status = 'active', started_at = COALESCE(started_at, ?),
                       active_segment_started_at = ? WHERE plan_item_id = ?
                """,
                (now, now, plan_item_id),
            )
            connection.execute(
                """
                INSERT INTO plan_item_event(event_id, plan_item_id, event_type, occurred_at, payload_json, client_action_id)
                VALUES (?, ?, 'started', ?, '{}', ?)
                """,
                (str(uuid4()), plan_item_id, now, client_action_id),
            )
            result = self._get_plan_item_by_id(plan_item_id, connection=connection)
        return result

    def pause_plan_item(self, plan_item_id: str, client_action_id: str, reason: str = "child_requested",
                        *, authorized_turn_id: str | None = None, connection=None) -> dict:
        now = utc_now()
        with self._write_transaction(connection) as connection:
            existing = connection.execute(
                "SELECT plan_item_id,event_type,payload_json FROM plan_item_event WHERE client_action_id = ?", (client_action_id,)
            ).fetchone()
            if existing:
                if (existing['plan_item_id'] != plan_item_id or existing['event_type'] != 'paused'
                        or json.loads(existing['payload_json']).get('reason') != reason):
                    raise PlanItemStateError('暂停操作号与原任务或原操作不匹配。')
                return self._get_plan_item_by_id(existing["plan_item_id"], connection=connection)
            if authorized_turn_id:
                from .agent_runtime import AgentRuntimeStore, InvalidAgentPlanError
                control = AgentRuntimeStore.inspect_voice_control_execution(connection, authorized_turn_id)
                if control and (control['control'] != 'task.pause'
                        or control['targets'].get('task', {}).get('plan_item_id') != plan_item_id):
                    raise InvalidAgentPlanError('ANSWER_CONTROL_SCOPE_CHANGED')
            item = self._plan_item_for_update(connection, plan_item_id)
            if item["status"] != "active":
                raise PlanItemStateError("只有进行中的任务可以暂停。")
            elapsed = item["active_seconds"] + self._segment_seconds(item["active_segment_started_at"], now)
            connection.execute(
                "UPDATE plan_item SET status = 'paused', active_seconds = ?, active_segment_started_at = NULL WHERE plan_item_id = ?",
                (elapsed, plan_item_id),
            )
            self._insert_plan_event(connection, plan_item_id, "paused", now, {"reason": reason}, client_action_id)
            result = self._get_plan_item_by_id(plan_item_id, connection=connection)
        return result

    def checkpoint_plan_item(self, plan_item_id: str) -> dict:
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            item = self._plan_item_for_update(connection, plan_item_id)
            if item["status"] != "active":
                connection.rollback()
                return self._get_plan_item_by_id(plan_item_id)
            elapsed = item["active_seconds"] + self._segment_seconds(
                item["active_segment_started_at"], now
            )
            connection.execute(
                """
                UPDATE plan_item SET active_seconds = ?, active_segment_started_at = ?
                WHERE plan_item_id = ? AND status = 'active'
                """,
                (elapsed, now, plan_item_id),
            )
            connection.commit()
        return self._get_plan_item_by_id(plan_item_id)

    def resume_plan_item(self, plan_item_id: str, client_action_id: str, *, authorized_turn_id: str | None = None, connection=None) -> dict:
        now = utc_now()
        with self._write_transaction(connection) as connection:
            control = None
            if authorized_turn_id:
                from .task_start_binding import TaskStartBinding
                from .agent_runtime import AgentRuntimeStore, InvalidAgentPlanError
                control = AgentRuntimeStore.inspect_voice_control_execution(connection, authorized_turn_id)
                if control and control['control'] == 'task.resume':
                    if control['targets']['task_resume']['item']['plan_item_id'] != plan_item_id:
                        raise InvalidAgentPlanError('ANSWER_CONTROL_SCOPE_CHANGED')
                else:
                    TaskStartBinding.authorize(connection, authorized_turn_id, plan_item_id, "resume")
            existing = connection.execute(
                "SELECT plan_item_id,event_type FROM plan_item_event WHERE client_action_id = ?", (client_action_id,)
            ).fetchone()
            if existing:
                if existing["plan_item_id"] != plan_item_id or existing["event_type"] != "resumed":
                    raise PlanInputError("任务操作标识与原操作不匹配。")
                return self._get_plan_item_by_id(existing["plan_item_id"], connection=connection)
            item = self._plan_item_for_update(connection, plan_item_id)
            if item["status"] != "paused":
                raise PlanItemStateError("只有已暂停的任务可以继续。")
            self._validate_replanned_item(connection, plan_item_id)
            self._validate_stage_task_start(connection, item, authorized_turn_id)
            self._validate_pet_task_start(connection, item,
                control['targets']['task_resume']['item'].get('origin_turn_id')
                if control and control['control'] == 'task.resume' else authorized_turn_id)
            self._validate_goal_catalog_source(connection, item)
            session = connection.execute(
                "SELECT state, status FROM daily_session WHERE session_id = ?", (item["session_id"],)
            ).fetchone()
            if not session or session["state"] != "study" or session["status"] != "active":
                raise StaleSessionStateError("study", session["state"] if session else "missing")
            if connection.execute("SELECT 1 FROM plan_item WHERE plan_id=? AND status='active' AND plan_item_id!=?",
                                  (item["plan_id"], plan_item_id)).fetchone():
                raise PlanItemStateError("请先暂停或完成当前任务，再继续另一项。")
            prompt = (TaskStartBinding.resume_prompt(connection, item['session_id'], control['targets']['task_resume']['item'])
                      if control and control['control'] == 'task.resume' else None)
            connection.execute(
                "UPDATE plan_item SET status = 'active', active_segment_started_at = ? WHERE plan_item_id = ?",
                (now, plan_item_id),
            )
            self._insert_plan_event(connection, plan_item_id, "resumed", now, {}, client_action_id)
            result = self._get_plan_item_by_id(plan_item_id, connection=connection)
            if control and control['control'] == 'task.resume':
                result.update(local_only=True, cloud_transmission_allowed=False)
                if prompt:
                    result['diagnostic_prompt'] = prompt
        return result

    def reschedule_plan_item(self, plan_item_id: str, reason: str, client_action_id: str, *, authorized_turn_id: str | None = None) -> dict:
        from .learning_input_carryover import LearningInputCarryoverService, CarryoverConflict
        carryover = LearningInputCarryoverService(self.database)
        carryover.initialize()
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT plan_item_id,event_type,payload_json FROM plan_item_event WHERE client_action_id = ?", (client_action_id,)
            ).fetchone()
            if existing:
                if (existing["plan_item_id"] != plan_item_id or existing["event_type"] != "rescheduled"
                        or json.loads(existing["payload_json"]).get("reason") != reason):
                    raise PlanItemStateError("延期请求与原记录不一致，未改变其他任务。")
                connection.rollback()
                return self._get_plan_item_by_id(existing["plan_item_id"])
            item = self._plan_item_for_update(connection, plan_item_id)
            if authorized_turn_id:
                from .agent_runtime import AgentRuntimeStore, InvalidAgentPlanError
                gate = AgentRuntimeStore.inspect_voice_control_execution(connection, authorized_turn_id)
                if (not gate or gate['control'] != 'task.reschedule'
                        or (gate['targets'].get('task_deferral') or {}).get('plan_item_id') != plan_item_id):
                    raise InvalidAgentPlanError('ANSWER_CONTROL_SCOPE_CHANGED')
            if item["status"] not in {"pending", "paused"}:
                raise PlanItemStateError("只能延期尚未开始或已暂停的任务。")
            try:
                frozen = carryover.prepare_reschedule(item["child_id"], plan_item_id, connection=connection)
            except CarryoverConflict as error:
                raise PlanItemStateError(f"延期来源需重新核对：{error}") from error
            connection.execute(
                "UPDATE plan_item SET status = 'rescheduled', active_segment_started_at = NULL WHERE plan_item_id = ?",
                (plan_item_id,),
            )
            self._insert_plan_event(connection, plan_item_id, "rescheduled", now,
                {"reason": reason, "carryover_reclaim_binding": frozen}, client_action_id)
            if frozen["status"] == "prepared":
                event = connection.execute("SELECT event_id FROM plan_item_event WHERE client_action_id=?", (client_action_id,)).fetchone()
                try:
                    carryover.reclaim_rescheduled(item["child_id"], plan_item_id, event["event_id"],
                        f"{client_action_id}:carryover-reclaim", connection=connection)
                except CarryoverConflict as error:
                    raise PlanItemStateError(f"剩余任务回收未确认，延期未保存：{error}") from error
            connection.commit()
        return self._get_plan_item_by_id(plan_item_id)

    def begin_break(self, session_id: str, client_action_id: str, *, authorized_turn_id: str | None = None) -> dict:
        from .break_control import BreakControl
        from .agent_runtime import AgentRuntimeStore, InvalidAgentPlanError
        service = BreakControl(self.database)
        service.initialize()
        with self._write_transaction() as connection:
            receipt = service.recorded(connection, session_id, 'begin', client_action_id)
            replayed = receipt is not None
            if not receipt:
                binding = service.snapshot(connection, session_id)
                if binding['state'] != 'study' or binding['session_status'] != 'active':
                    raise StaleSessionStateError('study', binding['state'])
                if authorized_turn_id:
                    gate = AgentRuntimeStore.inspect_voice_control_execution(connection, authorized_turn_id)
                    if not gate or gate['control'] != 'break.begin' or gate['targets'].get('break') != binding:
                        raise InvalidAgentPlanError('ANSWER_CONTROL_SCOPE_CHANGED')
                active, planned = binding['active_item'], binding['planned_break_item']
                if active and active['source_type'] != 'planned_break':
                    self.pause_plan_item(active['plan_item_id'], f'{client_action_id}:pause', 'session_break', connection=connection)
                elif planned and planned['status'] == 'pending':
                    self.start_plan_item(planned['plan_item_id'], f'{client_action_id}:planned-break',
                                         allow_planned_break=True, connection=connection)
                self.transition(session_id, 'study', 'break', 'child_requested_break', client_action_id, connection=connection)
                receipt = service.save(connection, binding, 'begin', client_action_id, utc_now())
        return {**self.get_session(session_id), 'break_receipt': receipt, 'replayed': replayed}

    def resume_after_break(self, session_id: str, client_action_id: str, *, authorized_turn_id: str | None = None) -> dict:
        from .break_control import BreakControl
        from .agent_runtime import AgentRuntimeStore, InvalidAgentPlanError
        service = BreakControl(self.database)
        service.initialize()
        with self._write_transaction() as connection:
            receipt = service.recorded(connection, session_id, 'resume', client_action_id)
            replayed = receipt is not None
            if not receipt:
                binding = service.snapshot(connection, session_id)
                if binding['state'] != 'break' or binding['session_status'] != 'active':
                    raise StaleSessionStateError('break', binding['state'])
                if binding['status'] != 'ready':
                    raise PlanInputError('休息前的任务或计划已变化，请先核对原任务，未自动恢复计时。')
                if authorized_turn_id:
                    gate = AgentRuntimeStore.inspect_voice_control_execution(connection, authorized_turn_id)
                    if not gate or gate['control'] != 'break.resume' or gate['targets'].get('break') != binding:
                        raise InvalidAgentPlanError('ANSWER_CONTROL_SCOPE_CHANGED')
                self.transition(session_id, 'break', 'study', 'child_resumed', client_action_id, connection=connection)
                planned = binding['planned_break_item']
                if planned:
                    self.complete_plan_item(planned['plan_item_id'], planned['expected_minutes'],
                        f'{client_action_id}:complete-planned-break', 'planned_break', '计划内休息已结束', connection=connection)
                elif binding['resume_item_id']:
                    self.resume_plan_item(binding['resume_item_id'], f'{client_action_id}:resume', connection=connection)
                receipt = service.save(connection, binding, 'resume', client_action_id, utc_now())
            from .learning_input_return import after_break
            input_reentry = after_break(connection, self.database, session_id, client_action_id, authorized_turn_id)
        return {**self.get_workspace(session_id), 'break_receipt': receipt, 'replayed': replayed,
                **({'input_reentry': input_reentry} if input_reentry else {})}

    def complete_plan_item(
        self,
        plan_item_id: str,
        actual_minutes: int,
        client_action_id: str,
        completion_evidence_type: str = "child_confirmed",
        completion_note: str | None = None,
        completion_checklist: list[str] | None = None,
        completion_artifact_id: str | None = None,
        *, connection=None, authorized_turn_id: str | None = None,
    ) -> dict:
        now = utc_now()
        with self._write_transaction(connection) as connection:
            from .writing_daily_followup import inspect_completion
            if authorized_turn_id:
                from .agent_runtime import AgentRuntimeStore, InvalidAgentPlanError
                from .agent_continuation import ContinuationStore
                if not ContinuationStore.execution_allowed(connection,authorized_turn_id,running=True):
                    raise InvalidAgentPlanError('ANSWER_WRITING_DAILY_COMPLETION_COMMAND_CHANGED')
                control=AgentRuntimeStore.inspect_voice_control_execution(connection,authorized_turn_id)
                if control and (control['control']!='task.complete'
                        or (control['targets'].get('task') or {}).get('plan_item_id')!=plan_item_id):
                    raise InvalidAgentPlanError('ANSWER_WRITING_DAILY_COMPLETION_TASK_CHANGED')
            daily_completion=inspect_completion(connection,authorized_turn_id) if authorized_turn_id else None
            if daily_completion and (daily_completion['item']['plan_item_id']!=plan_item_id
                    or daily_completion['turn']['idempotency_key']!=client_action_id):
                raise PlanInputError('本次完成请求与原写作练习不一致。')
            existing_event = connection.execute(
                "SELECT plan_item_id, event_type FROM plan_item_event WHERE client_action_id = ?",
                (client_action_id,),
            ).fetchone()
            if existing_event:
                if existing_event["plan_item_id"] != plan_item_id or existing_event["event_type"] != "completed":
                    raise PlanInputError("任务操作标识与原操作不匹配。")
                suspect = connection.execute('''SELECT 1 FROM plan_item p JOIN capture_artifact a
                    ON a.artifact_id=p.completion_artifact_id
                    WHERE p.plan_item_id=? AND a.artifact_type='document_ocr_candidate' ''', (plan_item_id,)).fetchone()
                if suspect:
                    raise PlanInputError('原任务引用了未确认的多帧候选，请先核对该证据。')
                return self._get_plan_item_by_id(existing_event["plan_item_id"], connection=connection)
            item = connection.execute(
                """
                SELECT pi.plan_item_id, pi.homework_id, pi.subject, pi.status, pi.active_seconds, pi.task_type,
                       pi.active_segment_started_at, pi.started_at, pi.source_type,
                       pi.source_ref, dp.session_id, dp.child_id
                FROM plan_item pi JOIN daily_plan dp ON dp.plan_id = pi.plan_id
                WHERE pi.plan_item_id = ?
                """,
                (plan_item_id,),
            ).fetchone()
            if not item:
                raise UnknownPlanItemError(plan_item_id)
            session = connection.execute(
                "SELECT child_id, state, status FROM daily_session WHERE session_id = ?",
                (item["session_id"],),
            ).fetchone()
            if item["source_type"] == "due_review" and session and session["child_id"] != item["child_id"]:
                raise PlanInputError("复测计划与学习会话所属孩子不匹配。")
            if not session or session["state"] != "study" or session["status"] != "active":
                raise StaleSessionStateError("study", session["state"] if session else "missing")
            if item["source_type"] == "due_review" and item["status"] == "completed":
                self._validate_plan_item_completion_evidence(connection, item)
                return self._get_plan_item_by_id(plan_item_id, connection=connection)
            if item["status"] not in {"active", "paused"}:
                raise PlanItemStateError("任务开始后才能标记完成。")
            review_receipt = self._validate_plan_item_completion_evidence(connection, item)
            if item["source_type"] == "agent_retention":
                daily_source=connection.execute('''SELECT 1 FROM agent_plan WHERE child_id=? AND session_id=?
                    AND json_extract(context_manifest_json,'$.daily_writing_binding.task.plan_item_id')=?''',
                    (item['child_id'],item['session_id'],plan_item_id)).fetchone()
                if daily_source and not daily_completion:
                    raise PlanInputError('请用本次明确完成请求核对原写作任务、第二稿和反馈。')
                checked = connection.execute("""SELECT 1 FROM agent_commitment ac JOIN agent_outcome o ON o.objective_id=ac.objective_id
                    JOIN json_each(o.evidence_refs_json) ref ON json_extract(ref.value,'$.type')='child_response'
                    JOIN agent_step s ON s.step_id=json_extract(ref.value,'$.agent_step_id')
                    JOIN agent_plan p ON p.plan_id=s.plan_id AND p.objective_id=o.objective_id
                    WHERE ac.commitment_id=? AND ac.child_id=? AND o.child_id=ac.child_id AND p.child_id=ac.child_id
                      AND p.session_id=? AND o.created_at>=? AND s.capability_name='learning.evaluate_response'
                      AND length(trim(COALESCE(json_extract(s.arguments_json,'$.response'),'')))>0
                      AND (ac.commitment_type='follow_up' OR o.outcome_level='retained') LIMIT 1""",
                    (item["source_ref"], item["child_id"], item["session_id"], item["started_at"])).fetchone()
                if not checked and not daily_completion:
                    raise PlanInputError("请先实际回答本次检查问题，讲解或自述学会不能完成检查任务。")
            if item["source_type"] == "pet_daily" and not str(item["source_ref"] or "").startswith(PET_VOCABULARY_SOURCE_PREFIX):
                from .learning_state import LearningStateService
                bound_item = connection.execute('''SELECT di.answer_type FROM diagnostic_item di
                    JOIN knowledge_node kn USING(knowledge_node_id)
                    WHERE di.diagnostic_item_id=? AND kn.track='pet' ''', (item['source_ref'],)).fetchone()
                if not bound_item or not LearningStateService._eligible_diagnostic(connection, item['child_id'], item['source_ref']):
                    raise PlanInputError('本次PET任务的原题不可用，请先核对题源，不能仅凭口述完成。')
                if bound_item['answer_type'] == 'writing':
                    self._validate_stage_task_completion(connection, item)
                else:
                    from .pet_task_execution import validate_pet_task_completion, PetTaskExecutionError
                    try:
                        validate_pet_task_completion(connection, item)
                    except PetTaskExecutionError as error:
                        raise PlanInputError('请先恢复本次PET任务的原题并提交回答；原绑定或来源失效时需核对计划，不能用同题其他作答替代。') from error
                activity_attempt = connection.execute(
                    """
                    SELECT 1 FROM diagnostic_attempt
                    WHERE child_id = ? AND session_id = ? AND diagnostic_item_id = ?
                      AND created_at >= ?
                    LIMIT 1
                    """,
                    (item["child_id"], item["session_id"], item["source_ref"], item["started_at"]),
                ).fetchone()
                if not activity_attempt:
                    raise PlanInputError("请先完成今天绑定的PET活动并提交活动证据，再记录任务完成。")
            if item["source_type"] == "pet_daily" and str(item["source_ref"] or "").startswith(
                PET_VOCABULARY_SOURCE_PREFIX
            ):
                try:
                    target_cards = max(
                        1, int(str(item["source_ref"])[len(PET_VOCABULARY_SOURCE_PREFIX):])
                    )
                except ValueError:
                    target_cards = 1
                attempt_count = connection.execute(
                    """
                    SELECT COUNT(*) FROM pet_vocabulary_attempt
                    WHERE child_id=? AND session_id=? AND occurred_at>=?
                    """,
                    (item["child_id"], item["session_id"], item["started_at"]),
                ).fetchone()[0]
                if int(attempt_count) < target_cards:
                    raise PlanInputError(
                        f"请先完成今天的{target_cards}张词汇卡，"
                        f"当前已保存{int(attempt_count)}张作答证据。"
                    )
            if item['source_type'] == 'competition_math' and item['task_type'] == 'goal_prerequisite':
                self._validate_goal_catalog_source(connection, item)
                self._validate_stage_task_completion(connection, item, stage=False)
            elif item["source_type"] == "competition_math":
                self._validate_stage_task_completion(connection, item)
                active_run = connection.execute(
                    "SELECT completed_at FROM specialized_assessment_run WHERE plan_item_id=? AND status='active'",
                    (plan_item_id,),
                ).fetchone() if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='specialized_assessment_run'").fetchone() else None
                if active_run:
                    raise PlanInputError("请先完成当前数学思维连续测评；系统会逐题语音推进并保存每一题证据。")
                activity_attempt = connection.execute(
                    """
                    SELECT 1 FROM diagnostic_attempt da
                    JOIN diagnostic_item di ON di.diagnostic_item_id=da.diagnostic_item_id
                    JOIN knowledge_node kn ON kn.knowledge_node_id=di.knowledge_node_id
                    WHERE da.child_id=? AND da.session_id=? AND da.created_at>=?
                      AND kn.track='competition_math'
                    LIMIT 1
                    """,
                    (item["child_id"], item["session_id"], item["started_at"]),
                ).fetchone()
                if not activity_attempt:
                    raise PlanInputError("请先完成今天的数学思维诊断或迁移题并保存作答证据，再记录任务完成。")
            if item['source_type'] in {'oral_question_supply','oral_delivered_continuation','oral_daily_check'}:
                self._validate_stage_task_completion(connection,item,stage=False)
            if item["source_type"] == "junior_exam_goal":
                self._validate_goal_catalog_source(connection, item)
                self._validate_stage_task_completion(connection, item, stage=False)
                active_run = connection.execute(
                    "SELECT completed_at FROM specialized_assessment_run WHERE plan_item_id=? AND status='active'",
                    (plan_item_id,),
                ).fetchone() if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='specialized_assessment_run'").fetchone() else None
                if active_run:
                    raise PlanInputError("请先完成当前学科的连续测评；系统会逐题语音推进并在最后汇总薄弱能力域。")
                activity_attempt = connection.execute(
                    """
                    SELECT 1 FROM diagnostic_attempt da
                    JOIN diagnostic_item di ON di.diagnostic_item_id=da.diagnostic_item_id
                    JOIN knowledge_node kn ON kn.knowledge_node_id=di.knowledge_node_id
                    WHERE da.child_id=? AND da.session_id=? AND da.created_at>=?
                      AND kn.track='school' AND kn.subject=?
                    LIMIT 1
                    """,
                    (item["child_id"], item["session_id"], item["started_at"], item["subject"]),
                ).fetchone()
                if not activity_attempt:
                    raise PlanInputError("请先完成当前学科的中考基线或迁移题并保存作答证据，再记录任务完成。")
            if item["source_type"] == "mistake_review" and item["source_ref"]:
                mistake_progress = connection.execute(
                    """
                    SELECT 1 FROM (
                      SELECT created_at FROM mistake_status_event
                      WHERE mistake_id = ? AND child_id = ? AND created_at >= ?
                      UNION ALL
                      SELECT created_at FROM remediation_step
                      WHERE mistake_id = ? AND child_id = ? AND created_at >= ?
                      UNION ALL
                      SELECT created_at FROM mistake_attempt
                      WHERE mistake_id = ? AND child_id = ? AND created_at >= ?
                    ) LIMIT 1
                    """,
                    (
                        item["source_ref"], item["child_id"], item["started_at"],
                        item["source_ref"], item["child_id"], item["started_at"],
                        item["source_ref"], item["child_id"], item["started_at"],
                    ),
                ).fetchone()
                if not mistake_progress:
                    raise PlanInputError("请先保存本次错题诊断、补救或变式证据，再记录任务完成。")
            checks = list(dict.fromkeys(completion_checklist or []))
            allowed_checks = set(COMPLETION_CHECKS_BY_TASK_TYPE.get(item["task_type"], ()))
            if any(check not in allowed_checks for check in checks):
                raise PlanInputError("完成核对项与当前任务类型不匹配。")
            if completion_evidence_type == "child_checked" and not review_receipt:
                missing_checks = [check for check in allowed_checks if check not in checks]
                if missing_checks:
                    raise PlanInputError("请完成当前任务的全部核对项，或改为“已完成”记录。")
            if completion_artifact_id:
                artifact = connection.execute(
                    "SELECT child_id, session_id, status, artifact_type FROM capture_artifact WHERE artifact_id = ?",
                    (completion_artifact_id,),
                ).fetchone()
                if (
                    not artifact
                    or artifact["child_id"] != item["child_id"]
                    or artifact["session_id"] != item["session_id"]
                    or artifact["artifact_type"] == 'document_ocr_candidate'
                    or artifact["status"] not in {"completed", "quality_warning"}
                ):
                    raise PlanInputError("完成页证据与当前孩子或学习会话不匹配，请重新采集。")
            active_seconds = item["active_seconds"]
            if item["status"] == "active":
                active_seconds += self._segment_seconds(item["active_segment_started_at"], now)
            connection.execute(
                """
                UPDATE plan_item SET status = 'completed', actual_minutes = ?, completed_at = ?,
                       active_seconds = ?, active_segment_started_at = NULL,
                       completion_evidence_type = ?, completion_note = ?, completion_checklist_json = ?,
                       completion_artifact_id = ?
                WHERE plan_item_id = ?
                """,
                (
                    actual_minutes,
                    now,
                    active_seconds,
                    completion_evidence_type,
                    (completion_note or "").strip() or None,
                    json.dumps(checks, ensure_ascii=False),
                    completion_artifact_id,
                    plan_item_id,
                ),
            )
            if item['source_type'] in ('oral_daily_check','oral_delivered_continuation'):
                from .oral_daily_check import settle
                settle(connection,item,now)
            if item["source_type"] == "school_homework" and item["homework_id"]:
                from .learning_input_replan import completed_homework_source
                if completed_homework_source(connection, item['child_id'], item['session_id'], item['homework_id']):
                    connection.execute(
                        "UPDATE homework_item SET status = 'completed' WHERE homework_id = ? AND child_id=? AND session_id=? AND status='pending'",
                        (item["homework_id"], item['child_id'], item['session_id']),
                    )
            daily_receipt=None
            if daily_completion:
                from .local_supply import digest
                promise=connection.execute('SELECT * FROM agent_commitment WHERE commitment_id=? AND child_id=?',
                    (item['source_ref'],item['child_id'])).fetchone()
                daily_receipt={'basis':daily_completion['basis'],'previous_status':promise['status'],
                    'commitment_version':digest(dict(promise))}
                connection.execute("UPDATE agent_commitment SET status='completed',completed_at=? WHERE commitment_id=?",
                    (now,promise['commitment_id']))
            connection.execute(
                """
                INSERT INTO plan_item_event(event_id, plan_item_id, event_type, occurred_at, payload_json, client_action_id)
                VALUES (?, ?, 'completed', ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    plan_item_id,
                    now,
                    json.dumps(
                        {
                            "actual_minutes": actual_minutes,
                            "completion_evidence_type": completion_evidence_type,
                            "completion_note": (completion_note or "").strip() or None,
                            "completion_checklist": checks,
                            "completion_artifact_id": completion_artifact_id,
                            **({'daily_writing_completion':daily_receipt} if daily_receipt else {}),
                            "review_receipt": {
                                key: review_receipt[key]
                                for key in ("attempt_id", "evidence_id", "review_task_id")
                            } if review_receipt else None,
                        },
                        ensure_ascii=False,
                    ),
                    client_action_id,
                ),
            )
            result = self._get_plan_item_by_id(plan_item_id, connection=connection)
        return result

    def complete_with_reflection(
        self,
        session_id: str,
        learned_text: str,
        difficult_text: str,
        feeling: str,
        next_action: str,
        skipped: bool,
        client_action_id: str,
    ) -> dict:
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM daily_reflection WHERE client_action_id = ?",
                (client_action_id,),
            ).fetchone()
            if existing:
                expected = (session_id, learned_text.strip(), difficult_text.strip(), feeling.strip(), next_action.strip(), int(skipped))
                if tuple(existing[key] for key in ("session_id", "learned_text", "difficult_text", "feeling", "next_action", "skipped")) != expected:
                    raise ValueError("reflection action ownership or payload conflict")
                # Idempotent replay also repairs objectives written by builds
                # that completed the session without relinquishing ownership.
                connection.execute(
                    """UPDATE agent_objective SET status='paused', updated_at=?
                       WHERE session_id=? AND status='active'""",
                    (now, session_id),
                )
                connection.commit()
                return self.get_workspace(session_id)
            session = connection.execute("SELECT child_id,status,state FROM daily_session WHERE session_id=?", (session_id,)).fetchone()
            if not session:
                raise UnknownSessionError(session_id)
            if session["status"] != "active" or session["state"] != "reflection":
                raise StaleSessionStateError("reflection", session["state"])
            completed_reviews = connection.execute("""SELECT pi.plan_item_id,pi.source_type,pi.source_ref,pi.started_at,
                dp.child_id,dp.session_id FROM plan_item pi JOIN daily_plan dp USING(plan_id)
                WHERE dp.session_id=? AND dp.child_id=? AND pi.source_type='due_review' AND pi.status='completed'""",
                (session_id, session["child_id"])).fetchall()
            for item in completed_reviews:
                self._validate_plan_item_completion_evidence(connection, item)
            connection.execute(
                """
                INSERT INTO daily_reflection(
                  reflection_id, session_id, child_id, learned_text, difficult_text,
                  feeling, next_action, skipped, created_at, client_action_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    session_id,
                    session["child_id"],
                    learned_text.strip(),
                    difficult_text.strip(),
                    feeling.strip(),
                    next_action.strip(),
                    int(skipped),
                    now,
                    client_action_id,
                ),
            )
            for from_state, to_state, reason in (
                ("reflection", "evolving", "reflection_saved"),
                ("evolving", "completed", "local_updates_queued"),
            ):
                connection.execute(
                    """
                    INSERT INTO session_transition(
                      transition_id, session_id, from_state, to_state, reason, occurred_at,
                      client_action_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid4()),
                        session_id,
                        from_state,
                        to_state,
                        reason,
                        now,
                        f"{client_action_id}:{to_state}",
                    ),
                )
            connection.execute(
                """
                UPDATE daily_session
                SET state = 'completed', status = 'completed', ended_at = ?, last_checkpoint_at = ?
                WHERE session_id = ?
                """,
                (now, now, session_id),
            )
            connection.execute(
                """
                UPDATE agent_objective
                SET status = 'paused', updated_at = ?
                WHERE session_id = ? AND child_id = ? AND status = 'active'
                """,
                (now, session_id, session["child_id"]),
            )
            connection.execute("DELETE FROM session_draft WHERE session_id = ?", (session_id,))
            connection.execute(
                """
                INSERT INTO report_job(
                  job_id, session_id, child_id, report_type, status, attempts,
                  last_error, created_at, updated_at
                ) VALUES (?, ?, ?, 'daily', 'pending', 0, NULL, ?, ?)
                """,
                (str(uuid4()), session_id, session["child_id"], now, now),
            )
            connection.commit()
        return self.get_workspace(session_id)

    def _get_plan_item_by_id(self, plan_item_id: str, *, connection=None) -> dict:
        with nullcontext(connection) if connection is not None else self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT plan_item_id, homework_id, subject, title, expected_minutes,
                       actual_minutes, priority, status, position, started_at, completed_at,
                       active_seconds, active_segment_started_at, source_type, source_ref,
                       completion_standard, cognitive_load, completion_evidence_type,
                       completion_note, task_type, completion_checklist_json,
                       completion_artifact_id
                FROM plan_item WHERE plan_item_id = ?
                """,
                (plan_item_id,),
            ).fetchone()
            if not row:
                raise UnknownPlanItemError(plan_item_id)
            return academic_completion_item(connection, self._plan_item_result(row))

    @staticmethod
    def _deferred_task_candidates(
        connection: sqlite3.Connection,
        child_id: str,
        target_session_id: str,
    ) -> list[dict]:
        rows = connection.execute(
            """
            SELECT pi.plan_item_id AS source_plan_item_id,
                   dp.session_id AS source_session_id,
                   date(ds.ended_at, 'localtime') AS source_date,
                   pi.subject, pi.title, pi.expected_minutes, pi.task_type,
                   hi.range_text, hi.due_at, hi.special_requirements,
                   (
                     SELECT pie.payload_json FROM plan_item_event pie
                     WHERE pie.plan_item_id = pi.plan_item_id
                       AND pie.event_type = 'rescheduled'
                     ORDER BY pie.occurred_at DESC LIMIT 1
                   ) AS reschedule_payload_json
            FROM plan_item pi
            JOIN daily_plan dp ON dp.plan_id = pi.plan_id
            JOIN daily_session ds ON ds.session_id = dp.session_id
            LEFT JOIN homework_item hi ON hi.homework_id = pi.homework_id
            WHERE dp.child_id = ?
              AND dp.session_id != ?
              AND ds.status = 'completed'
              AND pi.status = 'rescheduled'
              AND pi.source_type = 'school_homework'
              AND NOT EXISTS (
                SELECT 1 FROM deferred_task_review dtr
                WHERE dtr.source_plan_item_id = pi.plan_item_id
              )
            ORDER BY ds.ended_at, pi.position
            """,
            (child_id, target_session_id),
        ).fetchall()
        result: list[dict] = []
        for row in rows:
            # Revision-owned remainders have one authoritative obligation;
            # erased bindings must not revive them through the legacy queue.
            marker = connection.execute("SELECT payload_json FROM plan_item_event WHERE plan_item_id=? AND event_type='revision_replanned'",
                                        (row['source_plan_item_id'],)).fetchone()
            if marker:
                from .learning_input_carryover import LearningInputCarryoverService
                table = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='learning_input_replan_item'").fetchone()
                binding = connection.execute('SELECT 1 FROM learning_input_replan_item WHERE plan_item_id=?', (row['source_plan_item_id'],)).fetchone() if table else None
                if not binding or LearningInputCarryoverService.owns_unplaced_item(connection, child_id, row['source_plan_item_id']):
                    continue
            item = dict(row)
            payload_json = item.pop("reschedule_payload_json", None)
            reason = None
            if payload_json:
                try:
                    reason = json.loads(payload_json).get("reason")
                except (TypeError, json.JSONDecodeError, AttributeError):
                    reason = None
            item["reschedule_reason"] = reason or "上次计划因时间约束建议延期"
            item['source_version'] = hashlib.sha256(json.dumps(
                item, sort_keys=True, ensure_ascii=False, separators=(',', ':')
            ).encode('utf-8')).hexdigest()
            result.append(item)
        return result

    def _get_deferred_task_review(self, review_id: str) -> dict:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT review_id, source_plan_item_id, source_session_id, child_id,
                       target_session_id, decision, reason, target_homework_id,
                       reviewed_at
                FROM deferred_task_review WHERE review_id = ?
                """,
                (review_id,),
            ).fetchone()
        if not row:
            raise PlanInputError("找不到延期任务处理记录。")
        return dict(row)

    @staticmethod
    def _plan_item_result(row: sqlite3.Row) -> dict:
        result = dict(row)
        if "completion_artifact_raw_stored" in result:
            result["completion_artifact_raw_stored"] = bool(result["completion_artifact_raw_stored"])
        raw_checklist = result.pop("completion_checklist_json", "[]") or "[]"
        try:
            result["completion_checklist"] = json.loads(raw_checklist)
        except (TypeError, json.JSONDecodeError):
            result["completion_checklist"] = []
        if "reschedule_payload_json" in result:
            payload_json = result.pop("reschedule_payload_json")
            reason = None
            if payload_json:
                try:
                    reason = json.loads(payload_json).get("reason")
                except (TypeError, json.JSONDecodeError, AttributeError):
                    reason = None
            result["reschedule_reason"] = reason or (
                "计划因时间约束建议延期" if result.get("status") == "rescheduled" else None
            )
        return result

    @staticmethod
    def _is_pet_writing_task(connection, item):
        if item['source_type'] == 'due_review':
            return bool(connection.execute('''SELECT 1 FROM review_task r
                JOIN diagnostic_item d USING(diagnostic_item_id) JOIN knowledge_node n ON n.knowledge_node_id=d.knowledge_node_id
                WHERE r.review_task_id=? AND r.interval_label IN ('7d','writing_stage_7d')
                AND d.answer_type='writing' AND n.track='pet' ''', (item['source_ref'],)).fetchone())
        if item['source_type'] != 'pet_daily':
            return False
        row = connection.execute('SELECT answer_type FROM diagnostic_item WHERE diagnostic_item_id=?',
            (item['source_ref'],)).fetchone()
        return bool(row and row['answer_type'] == 'writing') or (
            'task_type' in item.keys() and item['task_type'] == 'writing')

    @staticmethod
    def _stage_prompt_row(connection, item, turn_id=None, *, stage=True):
        tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'agent_plan', 'diagnostic_prompt'} <= tables:
            return None
        rows = connection.execute("""SELECT q.* FROM agent_plan p JOIN diagnostic_prompt q
            ON q.prompt_id=json_extract(p.context_manifest_json,'$.goal_prompt_binding.prompt_id')
            WHERE p.child_id=? AND p.session_id=? AND q.child_id=p.child_id AND q.origin_session_id=p.session_id
              AND p.status!='cancelled'
              AND json_extract(p.context_manifest_json,'$.goal_prompt_binding.stage')=?
              AND json_extract(p.context_manifest_json,'$.goal_prompt_binding.plan_item_id')=?
              AND json_extract(p.context_manifest_json,'$.goal_prompt_binding.item_version')=q.item_version
              AND (? IS NULL OR p.agent_turn_id=?)
            ORDER BY q.created_at DESC""",
            (item['child_id'], item['session_id'], int(stage), item['plan_item_id'], turn_id, turn_id)).fetchall()
        if not rows or (not stage and len({(row['prompt_id'], row['item_version']) for row in rows}) != 1):
            return None
        return rows[0]

    @staticmethod
    def _validate_pet_task_start(connection, item, turn_id=None):
        from .pet_task_execution import validate_pet_task_start, PetTaskExecutionError
        try:
            validate_pet_task_start(connection, item, turn_id)
        except PetTaskExecutionError as error:
            raise PlanInputError('PET原题或绑定已变化，请通过原计划核对题目，不能仅开始计时。') from error

    @staticmethod
    def _validate_goal_catalog_source(connection, item):
        if item['source_type'] in {'junior_exam_goal', 'pet_daily', 'competition_math'}:
            from .goal_roadmap import goal_catalog_source_reason
            reason = goal_catalog_source_reason(connection, item['session_id'], item['plan_item_id'])
            if reason:
                raise PlanInputError(reason)

    def _validate_stage_task_start(self, connection, item, turn_id=None):
        if item['source_type'] in {'oral_question_supply','oral_delivered_continuation','oral_daily_check'}:
            from .oral_supply_execution import resolve_execution
            from .supply_gap_resume import budget
            prompt=self._stage_prompt_row(connection,item,turn_id,stage=False)
            try:
                if not prompt or prompt['status'] not in ('waiting_answer','answer_saved','answered'):
                    raise ValueError('ORAL_SUPPLY_PROMPT_REQUIRED')
                if not turn_id:
                    # Break recovery keeps the previously delivered question.
                    # It must prove a completed start, never admit a new task.
                    if item['status']!='paused':
                        raise ValueError('ORAL_SUPPLY_START_AUTHORIZATION_REQUIRED')
                    from .scheduled_writing_goal import validate_origin,_start_receipt
                    origins=connection.execute('''SELECT * FROM agent_plan WHERE child_id=? AND session_id=?
                        AND json_extract(context_manifest_json,'$.scheduled_writing_goal.plan_item_id')=?
                        AND json_extract(context_manifest_json,'$.scheduled_writing_goal.prompt_id')=?''',
                        (item['child_id'],item['session_id'],item['plan_item_id'],prompt['prompt_id'])).fetchall()
                    if len(origins)!=1:
                        raise ValueError('ORAL_SUPPLY_START_RECEIPT_CHANGED')
                    validate_origin(connection,origins[0],prompt)
                    _start_receipt(connection,origins[0],prompt,json.loads(origins[0]['context_manifest_json'])['goal_prompt_binding'])
                resolved=resolve_execution(connection,item['session_id'],item['plan_item_id'])
                if (resolved['diagnostic_item_id']!=prompt['diagnostic_item_id']
                        or resolved['item_version']!=prompt['item_version']):
                    raise ValueError('ORAL_SUPPLY_PROMPT_CHANGED')
                current=connection.execute('''SELECT pi.*,dp.child_id,dp.session_id FROM plan_item pi
                    JOIN daily_plan dp USING(plan_id) WHERE pi.plan_item_id=?''',(item['plan_item_id'],)).fetchone()
                budget(connection,dict(current))
            except ValueError as error:
                raise PlanInputError('原口述新题、授权或今天时间已变化，请核对后继续。') from error
            return
        if item['source_type'] == 'competition_math' and item['task_type'] == 'goal_prerequisite':
            prompt = self._stage_prompt_row(connection, item, turn_id, stage=False)
            if prompt and prompt['status'] == 'answered':
                self._validate_stage_task_completion(connection, item, stage=False)
                return
            from .goal_roadmap import GoalRoadmapService
            resolved = GoalRoadmapService._resolve_execution(connection, item['session_id'], item['plan_item_id'],
                prompt_id=prompt['prompt_id'] if prompt and item['status'] in {'active', 'paused'} else None)
            if (not prompt or prompt['status'] not in {'waiting_answer', 'answer_saved'} or resolved['status'] != 'ready'
                    or resolved['diagnostic_item_id'] != prompt['diagnostic_item_id'] or resolved['item_version'] != prompt['item_version']):
                raise PlanInputError('请先核对计划中绑定的基础题与当前目标，不能只启动计时。')
            return
        from .school_open_reviews import is_school_review
        if is_school_review(connection, item):
            from .school_review_execution import validate_start
            try:
                validate_start(connection, item, turn_id)
            except (ValueError, TypeError, KeyError) as error:
                from .school_open_reviews import SchoolReviewError
                code = str(error) if isinstance(error, SchoolReviewError) else 'SCHOOL_REVIEW_BINDING_CHANGED'
                raise PlanInputError('请核对本次学校复习的原题、审核和当前时间安排；未开始计时。' + code) from error
            return
        if item['source_type'] != 'competition_math' and not self._is_pet_writing_task(connection, item):
            return
        prompt = self._stage_prompt_row(connection, item, turn_id)
        if not prompt or prompt['status'] not in {'waiting_answer', 'answer_saved', 'answered'}:
            raise PlanInputError('请通过语音开始计划中绑定的原题，不能只启动计时或另选题目。')
        if prompt['status'] == 'answered':
            self._validate_stage_task_completion(connection, item)
            return
        from .stage_execution import resolve_stage_execution
        resolved = resolve_stage_execution(connection, item['session_id'], item['plan_item_id'])
        if (resolved['status'] != 'ready' or resolved['diagnostic_item_id'] != prompt['diagnostic_item_id']
                or resolved['item_version'] != prompt['item_version']):
            raise PlanInputError('专项任务的原题或阶段依据已变化，请重新核对计划，不能启动旧题。')

    def _validate_stage_task_completion(self, connection, item, *, stage=True):
        prompt = self._stage_prompt_row(connection, item, stage=stage)
        if not prompt or prompt['status'] != 'answered' or not prompt['attempt_id']:
            raise PlanInputError('请先回答这项计划绑定的原题并保存作答，其他同类题不能代替。')
        from .diagnostic_prompt import DiagnosticPromptService, _version
        from .local_supply import validate_item, LocalSupplyError
        original = DiagnosticPromptService._item(connection, item['child_id'], prompt['diagnostic_item_id'])
        if not original or _version(original) != prompt['item_version']:
            raise PlanInputError('原题已撤回或修订，请核对后再完成专项任务。')
        try:
            validate_item(connection, item['child_id'], prompt['diagnostic_item_id'])
        except (LocalSupplyError, ValueError, TypeError) as error:
            raise PlanInputError('原题审核依据已失效，请核对后再完成专项任务。') from error
        from contextlib import nullcontext
        class TransactionPrompt(DiagnosticPromptService):
            def __init__(self):
                self.database = None
            def _transaction(self):
                return nullcontext(connection)
        checked = TransactionPrompt().read(item['session_id'], prompt_id=prompt['prompt_id'])
        if checked['status'] != 'answered':
            raise PlanInputError('原题作答证据已失效，请核对后再完成专项任务。')
        attempt = connection.execute("""SELECT * FROM diagnostic_attempt
            WHERE attempt_id=? AND child_id=? AND session_id=? AND diagnostic_item_id=?""",
            (prompt['attempt_id'], item['child_id'], item['session_id'], prompt['diagnostic_item_id'])).fetchone()
        started = connection.execute('SELECT started_at FROM plan_item WHERE plan_item_id=?', (item['plan_item_id'],)).fetchone()
        if not attempt or not started or not started[0] or datetime.fromisoformat(attempt['created_at']) < datetime.fromisoformat(started[0]):
            raise PlanInputError('原题作答回执不属于这次专项任务。')
        if original.get('answer_type') == 'writing':
            from .writing_response_origin import original_written_response
            try:
                original_written_response(connection, prompt, attempt)
            except (ValueError, KeyError, TypeError) as error:
                raise PlanInputError('写作原稿与这次作答尚未核对，不能仅凭口述或旧稿完成。') from error

    def _validate_replanned_item(self, connection, plan_item_id: str) -> None:
        marker = connection.execute("SELECT 1 FROM plan_item_event WHERE plan_item_id=? AND event_type='revision_replanned'", (plan_item_id,)).fetchone()
        if not marker:
            return
        from .learning_input_replan import LearningInputReplanService
        owner = connection.execute('SELECT dp.child_id FROM daily_plan dp JOIN plan_item pi USING(plan_id) WHERE pi.plan_item_id=?', (plan_item_id,)).fetchone()
        inspector = LearningInputReplanService(self.database)
        exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='learning_input_replan_item'").fetchone()
        binding = connection.execute('SELECT source_binding_json FROM learning_input_replan_item WHERE plan_item_id=?', (plan_item_id,)).fetchone() if exists else None
        if binding and json.loads(binding[0]).get('version') == 'learning_input_carryover_source_v1':
            from .learning_input_carryover import LearningInputCarryoverService
            inspector = LearningInputCarryoverService(self.database)
        if not owner or not binding or not inspector.inspect_item(owner[0], plan_item_id, connection=connection)['eligible']:
            raise PlanItemStateError('修订后的作业来源或计划已变化，请重新核对当前作业后再开始。')

    @staticmethod
    def _segment_seconds(started_at: str | None, ended_at: str) -> int:
        if not started_at:
            return 0
        start = datetime.fromisoformat(started_at)
        end = datetime.fromisoformat(ended_at)
        return max(0, round((end - start).total_seconds()))

    @staticmethod
    def _insert_plan_event(connection, plan_item_id: str, event_type: str, occurred_at: str, payload: dict, client_action_id: str) -> None:
        connection.execute(
            """
            INSERT INTO plan_item_event(event_id, plan_item_id, event_type, occurred_at, payload_json, client_action_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (str(uuid4()), plan_item_id, event_type, occurred_at, json.dumps(payload, ensure_ascii=False), client_action_id),
        )

    @staticmethod
    def _plan_item_for_update(connection, plan_item_id: str):
        item = connection.execute(
            """
            SELECT pi.plan_item_id, pi.plan_id, pi.status, pi.active_seconds,
                   pi.active_segment_started_at, pi.source_type, pi.source_ref, pi.task_type, dp.session_id, dp.child_id
            FROM plan_item pi JOIN daily_plan dp ON dp.plan_id = pi.plan_id
            WHERE pi.plan_item_id = ?
            """,
            (plan_item_id,),
        ).fetchone()
        if not item:
            raise UnknownPlanItemError(plan_item_id)
        return item

    def _require_active_state(self, session_id: str, state: str) -> dict:
        session = self.get_session(session_id)
        if session["status"] != "active" or session["state"] != state:
            raise StaleSessionStateError(state, session["state"])
        return session

    def _require_intake_state(self, session_id: str) -> dict:
        session = self.get_session(session_id)
        if session["status"] != "active" or session["state"] not in {"check_in", "classroom_recall", "homework_capture"}:
            raise StaleSessionStateError("homework_capture", session["state"])
        return session

    @staticmethod
    def _get_recall(connection: sqlite3.Connection, recall_id: str) -> dict:
        row = connection.execute(
            """
            SELECT recall_id, subject, summary, confidence, confirmation_status,
                   record_kind, candidate_nodes_json, textbook_mapping_json,
                   evidence_sources_json, created_at
            FROM classroom_recall WHERE recall_id = ?
            """,
            (recall_id,),
        ).fetchone()
        return StudyRepository._public_recall(row)

    @staticmethod
    def _public_recall(row: sqlite3.Row) -> dict:
        item = dict(row)
        candidate_nodes = json.loads(item.pop("candidate_nodes_json") or "[]")
        item["candidate_nodes"] = [
            {"title": node.get("title", "候选知识点")}
            for node in candidate_nodes
            if isinstance(node, dict)
        ]
        item["textbook_mapping"] = json.loads(item.pop("textbook_mapping_json") or "null")
        item["evidence_sources"] = json.loads(item.pop("evidence_sources_json") or "[]")
        return item

    @staticmethod
    def _get_homework(connection: sqlite3.Connection, homework_id: str) -> dict:
        row = connection.execute(
            """
            SELECT homework_id, subject, task_type, title, range_text, expected_minutes,
                   due_at, special_requirements, source_artifact_id, ocr_confidence,
                   confirmation_status, status
            FROM homework_item WHERE homework_id = ?
            """,
            (homework_id,),
        ).fetchone()
        return dict(row)


class ObservationControlConflictError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__("原观察操作的收尾尚未核对，请保持暂停并重新说明暂停观察。" if code == "OBSERVATION_CONTROL_EFFECTS_UNCONFIRMED"
                         else "观察状态已变化，原请求没有重试；请核对当前状态后再说明操作。")


class UnknownChildError(Exception):
    pass


class UnknownRecallError(Exception):
    pass


class UnknownSessionError(Exception):
    pass


class InvalidSessionDraftError(Exception):
    pass


class InvalidSensorReleaseVerificationError(Exception):
    pass


class SensorReleaseVerificationNotAllowedError(Exception):
    pass


class ActiveSessionConflictError(Exception):
    def __init__(self, session_id: str, child_id: str):
        super().__init__(session_id)
        self.session_id = session_id
        self.child_id = child_id


class RepositoryConflictError(Exception):
    pass


class InvalidTransitionError(Exception):
    def __init__(self, from_state: str, to_state: str):
        super().__init__(f"{from_state}->{to_state}")
        self.from_state = from_state
        self.to_state = to_state


class StaleSessionStateError(Exception):
    def __init__(self, expected: str, actual: str):
        super().__init__(f"expected {expected}, actual {actual}")
        self.expected = expected
        self.actual = actual


class PlanInputError(Exception):
    def __init__(self, reason):
        self.reason_code = reason
        super().__init__({
            'catalog_source_changed': '这项任务的资料已经变更。请让家长核对当前资料，并按新路线重新安排；不会自动换成另一道题。',
            'catalog_source_unbound': '这项旧任务的资料绑定无法核实。请让家长核对资料并重新安排，暂不开始或继续旧题。',
            'goal_rule_reconfirmation_required': '家长已核对新的规则，这项旧安排没有自动换版。请按新路线重新安排，原来的学习记录仍会保留。',
        }.get(reason, reason))


class DeferredTaskChangedError(PlanInputError):
    def __init__(self, session_id, child_id, request_id, candidate):
        super().__init__('延期任务原内容已经变化，原请求没有执行。请重新核对后确认。')
        self.details = {'not_applied': True, 'session_id': session_id, 'child_id': child_id,
                        'client_action_id': request_id, 'candidate': candidate}


class UnknownPlanItemError(Exception):
    pass


class PlanItemStateError(Exception):
    pass


class ReportNotReadyError(Exception):
    pass


class LegacyReportUnverifiedError(ReportNotReadyError):
    pass


class UnknownReportError(Exception):
    pass


class UnknownTutorTurnError(Exception):
    pass


class TutorFeedbackConflictError(ValueError):
    pass
