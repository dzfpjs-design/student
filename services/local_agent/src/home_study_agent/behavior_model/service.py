from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
import hashlib
from pathlib import Path
import sqlite3
from uuid import uuid4

from ..database import Database
from ..family_clock import evidence_calendar_date, family_time
from .report_dependencies import REPORT_HABIT_ORDER, ownership_issues
from .outcome_evidence import (
    BEHAVIOR_CALENDAR_VERSION, COVERAGE_EVENT_TYPES, COVERAGE_VERSION, OBSERVATION_STATE_EVENTS, compare_coverage, coverage_projection,
    accepted_habit_support, effect_summary, goal_support_basis, observation_summary, report_habit_outcome, validate_coverage_sample,
)


OBSERVABLE_EVENT_TYPES = {
    "presence.seated", "presence.away", "work.writing", "work.reading",
    "interaction.switch", "help.request", "work.stall", "attention.diversion",
    "fatigue.candidate", "task.started", "task.finished", "plan.deviation",
    "child.check_in", "intervention.feedback", "document.ocr_completed", "audio.transcribed",
}

DAILY_EVENT_FEATURES = {
    "seated_count": "presence.seated",
    "away_count": "presence.away",
    "writing_count": "work.writing",
    "reading_count": "work.reading",
    "switch_count": "interaction.switch",
    "help_count": "help.request",
    "stall_count": "work.stall",
    "distraction_candidate_count": "attention.diversion",
    "fatigue_candidate_count": "fatigue.candidate",
    "plan_deviation_count": "plan.deviation",
    "child_check_in_count": "child.check_in",
}

DAILY_PROJECTION_VERSION = BEHAVIOR_CALENDAR_VERSION

INTERVENTION_SOURCE_OWNER = """(source_event_id IS NULL OR EXISTS(
    SELECT 1 FROM observable_event source WHERE source.event_id=intervention.source_event_id
    AND source.child_id=intervention.child_id AND source.session_id IS intervention.session_id))"""

HABIT_EXPERIMENTS = {
    "blocked_candidate": {
        "title": "圈出卡点再求助",
        "child_action": "卡住两分钟时，先圈出最不确定的一步，再用一句话说清卡在哪里。",
        "feature": "stall_count",
        "success_signal": "卡住候选次数下降，且孩子更早说出具体卡点",
    },
    "distraction_candidate": {
        "title": "五分钟单任务小段",
        "child_action": "想切换时，先把当前任务做到一个五分钟小节点，再决定是否换。",
        "feature": "distraction_candidate_count",
        "success_signal": "分心候选次数下降，五分钟小段完成率提高",
    },
    "fatigue_candidate": {
        "title": "主动短休息",
        "child_action": "感觉节奏明显变慢时，主动说需要喝水或伸展两分钟，再回来继续。",
        "feature": "fatigue_candidate_count",
        "success_signal": "疲劳候选后能主动短休息并返回任务",
    },
    "frequent_switching": {
        "title": "一次只推进一件事",
        "child_action": "一次只保留一个当前任务，完成五分钟小段后再切换。",
        "feature": "switch_count",
        "success_signal": "五分钟内频繁切换次数下降",
    },
}


@dataclass(frozen=True)
class ObservableEvent:
    """A low-sensitivity observation. `source_refs` must never contain media bytes."""

    child_id: str
    occurred_at: str
    event_type: str
    source: str
    confidence: float
    event_id: str = field(default_factory=lambda: str(uuid4()))
    session_id: str | None = None
    source_refs: tuple[str, ...] = ()
    missing_sources: tuple[str, ...] = ()
    payload: dict = field(default_factory=dict)
    alternative_explanations: tuple[str, ...] = ()

    def validate(self) -> None:
        if self.event_type not in OBSERVABLE_EVENT_TYPES:
            raise ValueError(f"Unsupported observable event type: {self.event_type}")
        if not self.child_id or not self.source:
            raise ValueError("child_id and source are required")
        if not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
        _parse_time(self.occurred_at)
        if any("raw" in ref.lower() or "video" in ref.lower() for ref in self.source_refs):
            raise ValueError("source_refs must be local derived-event references, never raw media")


class ObservationCaptureStale(ValueError):
    """The original HTTP capture no longer belongs to the current stream."""


class _GoalConfirmationNeedsEffectGuard(Exception):
    """Retry a correction only after releasing the optimistic SQLite writer."""


class _InterventionWithdrawalNeedsEffectGuard(Exception):
    """Retry a source withdrawal without holding the SQLite writer."""


class BehaviorModelService:
    """Durable event → probabilistic-state → intervention/effect chain.

    Production receives the main ``Database`` so behavior records belong to
    backup, restore, export and deletion. A standalone ``Path`` is retained for
    isolated test fixtures and legacy import tests only.
    """

    def __init__(self, target: Database | Path, legacy_path: Path | None = None):
        self.database = target if isinstance(target, Database) else None
        self.path = target.path if isinstance(target, Database) else Path(target)
        self.legacy_path = legacy_path
        if self.database is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.initialize()

    def initialize(self, *, force_daily_rebuild: bool = False) -> None:
        """Create managed tables and atomically import a former sidecar once."""
        self._initialize()
        if self.database is not None:
            self._migrate_legacy_sidecar()
        self.rebuild_daily_features(force=force_daily_rebuild)

    def rebuild_daily_features(self, *, force: bool = True) -> None:
        """Reproject derived counts after startup/restore; never relabel old buckets."""
        with self._connect() as db:
            if not force and self._daily_projection_current(db):
                return
            db.execute('BEGIN IMMEDIATE')
            if force or not self._daily_projection_current(db):
                self._rebuild_daily_features(db)
            db.commit()

    @staticmethod
    def _daily_projection_current(db):
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='behavior_daily_projection'").fetchone():
            BehaviorModelService._initialize_daily_projection(db)
        row = db.execute('SELECT version,dirty FROM behavior_daily_projection WHERE singleton=1').fetchone()
        return row is not None and row['version'] == DAILY_PROJECTION_VERSION and not row['dirty']

    def _rebuild_daily_features(self, db):
        rows = self._daily_feature_rows(db)
        db.execute('DELETE FROM daily_feature')
        self._save_daily_feature_rows(db, rows)
        self._mark_daily_projection_current(db)

    @staticmethod
    def _mark_daily_projection_current(db):
        db.execute('UPDATE behavior_daily_projection SET version=?,dirty=0 WHERE singleton=1',
                   (DAILY_PROJECTION_VERSION,))

    def begin_observation_stream(
        self, child_id: str, session_id: str, *, stream_epoch: str,
        at: str, client_action_id: str,
    ) -> dict:
        return self._record_coverage_control(child_id, session_id, stream_epoch, at, client_action_id, "observation.stream_started")

    def stop_observation_stream(
        self, child_id: str, session_id: str, *, stream_epoch: str, sequence: int,
        reason: str, at: str, client_action_id: str,
    ) -> dict:
        if type(sequence) is not int or sequence < 0:
            raise ValueError("Invalid stop sequence")
        if reason not in {"paused", "unavailable", "ended", "disconnected", "context_changed", "restarted"}:
            raise ValueError("Invalid observation stop reason")
        return self._record_coverage_control(child_id, session_id, stream_epoch, at, client_action_id,
                                             "observation.stream_stopped", sequence=sequence, reason=reason)

    def _record_coverage_control(self, child_id, session_id, stream_epoch, at, client_action_id, kind, **extra):
        _parse_time(at)
        if any(not isinstance(value, str) or not value.strip() or len(value) > 200
               for value in (child_id, session_id, stream_epoch, client_action_id)):
            raise ValueError("Coverage identifiers are required and bounded")
        payload = {"schema_version": COVERAGE_VERSION, "stream_epoch": stream_epoch,
                   "client_action_id": client_action_id, **extra}
        event_id = "coverage-action:" + hashlib.sha256(client_action_id.encode()).hexdigest()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if kind == "observation.stream_started" and self.database is not None:
                payload["server_binding"] = self._capture_session_binding(db, child_id, session_id)
            existing = db.execute("SELECT * FROM observable_event WHERE event_id=?", (event_id,)).fetchone()
            if existing:
                if (existing["child_id"] != child_id or existing["session_id"] != session_id
                        or existing["event_type"] != kind or json.loads(existing["payload_json"]) != payload):
                    raise ValueError("Coverage action conflict")
                return {"event_id": event_id, "status": "recorded", "idempotent": True}
            opening = self._coverage_source(db, child_id, session_id, stream_epoch, "observation.stream_started")
            if kind == "observation.stream_started" and opening:
                raise ValueError("A stream epoch cannot be reopened; create a new epoch")
            if kind == "observation.stream_stopped" and opening is None:
                raise ValueError("Unknown stream epoch")
            self._insert_coverage(db, event_id, child_id, session_id, kind, at, payload,
                                  [opening["event_id"]] if opening else [])
            self._refresh_habit_experiments(db, child_id, datetime.now(timezone.utc))
            db.commit()
        return {"event_id": event_id, "status": "recorded", "idempotent": False}

    @staticmethod
    def _capture_session_binding(db, child_id, session_id):
        row = db.execute("SELECT child_id,status,observation_status,observation_revision FROM daily_session WHERE session_id=?", (session_id,)).fetchone()
        if (row is None or row["child_id"] != child_id or row["status"] != "active"
                or row["observation_status"] != "enabled"):
            raise ObservationCaptureStale("Observation session is not current")
        has_generation = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='privacy_storage_generation'").fetchone()
        generation = db.execute("SELECT generation FROM privacy_storage_generation WHERE singleton=1").fetchone() if has_generation else None
        if has_generation and generation is None:
            raise ObservationCaptureStale("Observation generation is missing")
        mode = db.execute("SELECT observation_mode FROM family_rule WHERE child_id=?", (child_id,)).fetchone()
        return {"child_id": child_id, "session_id": session_id, "observation_mode": mode[0] if mode else None,
                "observation_revision": row["observation_revision"], "storage_generation": generation[0] if generation is not None else None}

    def capture_admission(self, child_id, session_id, capture, *, connection=None, expected=None):
        """Read-only preflight, repeated inside the original frame write transaction.

        A preflight result is only a comparison value; it never grants a write.
        The public client supplies clocks/epoch/sequence, never server_binding.
        """
        with self._connect(connection) as db:
            binding = self._capture_session_binding(db, child_id, session_id)
            row = db.execute("""SELECT rowid AS control_rowid,event_id,event_type,payload_json FROM observable_event
                WHERE child_id=? AND session_id=? AND source='observation_coverage'
                  AND event_type='observation.stream_started'
                ORDER BY rowid DESC LIMIT 1""", (child_id, session_id)).fetchone()
            try:
                payload = json.loads(row["payload_json"]) if row else None
                if (not row or row["event_type"] != "observation.stream_started"
                        or payload.get("stream_epoch") != capture["stream_epoch"]
                        or payload.get("server_binding") != binding):
                    raise ObservationCaptureStale("Observation stream is not current")
                stopped = db.execute("""SELECT 1 FROM observable_event WHERE child_id=? AND session_id=?
                    AND source='observation_coverage' AND event_type='observation.stream_stopped'
                    AND json_extract(payload_json,'$.stream_epoch')=? AND rowid>? LIMIT 1""",
                    (child_id, session_id, capture["stream_epoch"], row["control_rowid"])).fetchone()
                if stopped:
                    raise ObservationCaptureStale("Observation stream was stopped")
                sequence, monotonic = capture["sequence"], capture["capture_monotonic_ms"]
                _parse_time(capture["captured_at"])
                if type(sequence) is not int or sequence < 1 or type(monotonic) is not int or monotonic < 0:
                    raise ObservationCaptureStale("Invalid capture clocks")
                previous = self._coverage_source(db, child_id, session_id, capture["stream_epoch"], "observation.sample")
                if previous:
                    prior = json.loads(previous["payload_json"])
                    if sequence <= prior["sequence"] or monotonic <= prior["capture_monotonic_ms"]:
                        raise ObservationCaptureStale("Capture was already consumed or superseded")
                current = {"opening_event_id": row["event_id"], "opening_payload": row["payload_json"], "server_binding": binding}
                if expected is not None and current != expected:
                    raise ObservationCaptureStale("Capture opening changed during analysis")
            except (KeyError, TypeError, ValueError) as error:
                raise ObservationCaptureStale("Invalid capture binding") from error
            return current

    def record_observation_sample(self, child_id: str, session_id: str, sample: dict, *, connection=None) -> dict:
        """Trusted local producer entry; never expose this payload as a public write API."""
        validate_coverage_sample(sample)
        if sample["child_id"] != child_id or sample["session_id"] != session_id:
            raise ValueError("Coverage sample owner mismatch")
        identity = json.dumps([child_id, session_id, sample["stream_epoch"], sample["sequence"]])
        event_id = "coverage-sample:" + hashlib.sha256(identity.encode()).hexdigest()
        with self._connect(connection) as db:
            if connection is None:
                db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT payload_json FROM observable_event WHERE event_id=?", (event_id,)).fetchone()
            if existing:
                if json.loads(existing["payload_json"]) != sample:
                    raise ValueError("Coverage sample payload conflict")
                return {"event_id": event_id, "status": "recorded", "idempotent": True}
            reused = db.execute(
                """SELECT 1 FROM observable_event WHERE source='observation_coverage'
                   AND event_type='observation.sample' AND child_id=?
                   AND json_extract(payload_json,'$.analysis_id')=? LIMIT 1""",
                (child_id, sample["analysis_id"]),
            ).fetchone()
            if reused:
                raise ValueError("A coverage analysis cannot be reused for another sample")
            opening = self._coverage_source(db, child_id, session_id, sample["stream_epoch"], "observation.stream_started")
            if opening is None:
                raise ValueError("Unknown stream epoch; begin it before recording samples")
            previous = self._coverage_source(db, child_id, session_id, sample["stream_epoch"], "observation.sample")
            refs = [opening["event_id"]]
            if previous:
                refs.append(previous["event_id"])
            self._insert_coverage(db, event_id, child_id, session_id, "observation.sample", sample["received_at"], sample, refs)
            if connection is None:
                db.commit()
        return {"event_id": event_id, "status": "recorded", "idempotent": False}

    @staticmethod
    def _coverage_source(db, child_id, session_id, stream_epoch, kind):
        return db.execute(
            """SELECT * FROM observable_event WHERE child_id=? AND session_id=?
               AND source='observation_coverage' AND event_type=?
               AND json_extract(payload_json,'$.stream_epoch')=? ORDER BY rowid DESC LIMIT 1""",
            (child_id, session_id, kind, stream_epoch),
        ).fetchone()

    @staticmethod
    def _insert_coverage(db, event_id, child_id, session_id, kind, at, payload, refs):
        db.execute(
            """INSERT INTO observable_event(event_id,child_id,session_id,occurred_at,event_type,source,
               confidence,source_refs_json,missing_sources_json,payload_json,alternatives_json)
               VALUES(?,?,?,?,?,'observation_coverage',1,?,'[]',?,'[]')""",
            (event_id, child_id, session_id, at, kind, json.dumps(refs), json.dumps(payload, ensure_ascii=False, allow_nan=False)),
        )

    @staticmethod
    def _coverage_records(db, child_id, session_id=None):
        clause = " AND oe.session_id=?" if session_id is not None else ""
        return db.execute(
            """SELECT oe.*, br.event_id IS NOT NULL AS retracted,
               (json_extract(oe.payload_json,'$.state_event_id') IS NOT NULL
                 AND (source.event_id IS NULL OR source_br.event_id IS NOT NULL)) AS behavior_source_invalid
               FROM observable_event oe
               LEFT JOIN behavior_retraction br ON br.event_id=oe.event_id
               LEFT JOIN observable_event source ON source.event_id=('observation:' || json_extract(oe.payload_json,'$.state_event_id'))
                 AND source.child_id=oe.child_id AND source.session_id IS oe.session_id AND source.source='local_vision'
               LEFT JOIN behavior_retraction source_br ON source_br.event_id=source.event_id
               WHERE oe.child_id=? AND oe.source='observation_coverage'""" + clause + " ORDER BY oe.rowid",
            (child_id, session_id) if session_id is not None else (child_id,),
        ).fetchall()

    def observation_coverage(self, child_id: str, *, start_at: str, end_at: str) -> dict:
        start, end = _parse_time(start_at), _parse_time(end_at)
        if start >= end:
            raise ValueError("Coverage window must have positive length")
        with self._connect() as db:
            return coverage_projection(self._coverage_records(db, child_id), child_id, start, end)

    def ingest(self, event: ObservableEvent, *, connection=None) -> dict:
        event.validate()
        if event.source == "observation_coverage" or event.event_type in COVERAGE_EVENT_TYPES:
            raise ValueError("Coverage records require the trusted observation producer entry")
        with self._connect(connection) as db:
            if connection is None:
                db.execute('BEGIN IMMEDIATE')
            if not self._daily_projection_current(db):
                self._rebuild_daily_features(db)
            existing = db.execute("SELECT event_id FROM observable_event WHERE event_id = ?", (event.event_id,)).fetchone()
            if existing:
                if connection is None:
                    db.commit()
                return self.replay(event.child_id, event.session_id, connection=connection)
            db.execute(
                """INSERT INTO observable_event VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (event.event_id, event.child_id, event.session_id, event.occurred_at,
                 event.event_type, event.source, event.confidence,
                 json.dumps(event.source_refs), json.dumps(event.missing_sources),
                 json.dumps(event.payload, ensure_ascii=False), json.dumps(event.alternative_explanations, ensure_ascii=False)),
            )
            states = self._states_for(db, event)
            for state in states:
                db.execute(
                    "INSERT INTO probabilistic_state VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (str(uuid4()), event.event_id, event.child_id, event.session_id,
                     event.occurred_at, state["state"], state["probability"],
                     json.dumps(state["evidence"], ensure_ascii=False), json.dumps(state["alternatives"], ensure_ascii=False)),
                )
            self._refresh_daily_features(db, event.child_id, event.occurred_at)
            for state in states:
                self._maybe_propose_habit_experiment(
                    db, event.child_id, state["state"], event.occurred_at
                )
            from .habit_formation import synchronize_habit_formation
            for experiment in db.execute(
                """SELECT experiment_id FROM habit_experiment
                   WHERE child_id=? AND status IN ('proposed','active')""",
                (event.child_id,),
            ).fetchall():
                synchronize_habit_formation(
                    db, event.child_id, experiment["experiment_id"],
                    _parse_time(event.occurred_at),
                )
            if connection is None:
                db.commit()
        return self.replay(event.child_id, event.session_id, connection=connection)

    def pending_habit_experiment(self, child_id: str) -> dict:
        with self._connect() as db:
            row = db.execute(
                """SELECT * FROM habit_experiment
                   WHERE child_id = ? AND status = 'proposed'
                   ORDER BY proposed_at LIMIT 1""",
                (child_id,),
            ).fetchone()
            formation = None
            if row:
                from .habit_formation import synchronize_habit_formation
                formation = synchronize_habit_formation(
                    db, child_id, row["experiment_id"], datetime.now(timezone.utc)
                )
                db.commit()
        if not row:
            return {"status": "none", "experiment": None}
        item = self._habit_experiment_dict(row)
        item["formation"] = formation
        return {"status": "proposed", "experiment": item}

    def active_habit_experiment(self, child_id: str) -> dict:
        with self._connect() as db:
            now = datetime.now(timezone.utc)
            self._refresh_habit_experiments(db, child_id, now)
            row = db.execute(
                """SELECT * FROM habit_experiment
                   WHERE child_id = ? AND status = 'active'
                   ORDER BY started_at DESC LIMIT 1""",
                (child_id,),
            ).fetchone()
            formation = None
            if row:
                from .habit_formation import synchronize_habit_formation
                formation = synchronize_habit_formation(
                    db, child_id, row["experiment_id"], now
                )
            db.commit()
        experiment = self._habit_experiment_dict(row) if row else None
        if experiment:
            experiment["formation"] = formation
        return {
            "status": "active" if row else "none",
            "experiment": experiment,
        }

    def respond_habit_experiment(
        self, experiment_id: str, child_id: str, accepted: bool,
        at: str, client_action_id: str,
    ) -> dict:
        at_dt = _parse_time(at)
        from ..parent_reports import _report_effect_guard
        guard = _report_effect_guard(self.database, timeout=2) if self.database is not None else nullcontext()
        with guard, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            replay = db.execute(
                "SELECT experiment_id, child_id, event_type, payload_json FROM habit_experiment_event WHERE client_action_id = ?",
                (client_action_id,),
            ).fetchone()
            payload = {"accepted": accepted}
            if replay:
                original = json.loads(replay["payload_json"])
                if (replay["child_id"] != child_id or replay["experiment_id"] != experiment_id
                    or replay["event_type"] != ("accepted" if accepted else "declined")
                    or original.get("accepted") is not accepted):
                    raise ValueError("Habit experiment response action conflicts with saved data.")
                row = db.execute("SELECT * FROM habit_experiment WHERE experiment_id = ?", (experiment_id,)).fetchone()
                from .habit_formation import synchronize_habit_formation
                formation = synchronize_habit_formation(db, child_id, experiment_id, at_dt)
                db.commit()
                result = self._habit_experiment_dict(row)
                result["formation"] = formation
                return result
            row = db.execute(
                "SELECT * FROM habit_experiment WHERE experiment_id = ? AND child_id = ?",
                (experiment_id, child_id),
            ).fetchone()
            if not row:
                raise KeyError("Unknown habit experiment")
            if row["status"] != "proposed":
                raise ValueError("Habit experiment is no longer awaiting a response.")
            status = "active" if accepted else "declined"
            ends_at = (at_dt + timedelta(days=int(row["duration_days"]))).isoformat() if accepted else None
            db.execute(
                """UPDATE habit_experiment SET status=?,started_at=?,ends_at=?,closed_at=?
                   WHERE experiment_id=?""",
                (status, at if accepted else None, ends_at, None if accepted else at, experiment_id),
            )
            from .goal_confirmation import goal_version
            frozen = dict(db.execute("SELECT * FROM habit_experiment WHERE experiment_id=?", (experiment_id,)).fetchone())
            payload["goal_version"] = goal_version(frozen)
            db.execute(
                """INSERT INTO habit_experiment_event(
                     event_id,experiment_id,child_id,event_type,payload_json,occurred_at,client_action_id
                   ) VALUES(?,?,?,?,?,?,?)""",
                (str(uuid4()), experiment_id, child_id, "accepted" if accepted else "declined",
                 json.dumps(payload, ensure_ascii=False), at, client_action_id),
            )
            from .habit_formation import synchronize_habit_formation
            formation = synchronize_habit_formation(
                db, child_id, experiment_id, at_dt
            )
            db.commit()
            saved = db.execute("SELECT * FROM habit_experiment WHERE experiment_id = ?", (experiment_id,)).fetchone()
        result = self._habit_experiment_dict(saved)
        result["formation"] = formation
        return result

    def stop_habit_experiment(
        self, experiment_id: str, child_id: str, at: str, client_action_id: str
    ) -> dict:
        at_dt = _parse_time(at)
        if not isinstance(client_action_id,str) or not client_action_id.strip():
            raise ValueError('HABIT_STOP_ACTION_INVALID')
        def replay(db):
            row=db.execute('SELECT * FROM habit_experiment WHERE experiment_id=? AND child_id=?',
                           (experiment_id,child_id)).fetchone()
            if not row: raise KeyError('Unknown habit experiment')
            event=db.execute('SELECT child_id,experiment_id,event_type FROM habit_experiment_event WHERE client_action_id=?',
                             (client_action_id,)).fetchone()
            if not event: return None
            if (event['child_id'],event['experiment_id'],event['event_type'])!=(child_id,experiment_id,'stopped') or row['status']!='stopped':
                raise ValueError('HABIT_STOP_ACTION_CONFLICT')
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='habit_formation_state'").fetchone():
                return None
            if not db.execute('SELECT 1 FROM habit_formation_state WHERE child_id=? AND experiment_id=?',
                              (child_id,experiment_id)).fetchone():
                return None
            from .habit_formation import habit_formation_state
            result=self._habit_experiment_dict(row)
            result['formation']=habit_formation_state(db,child_id,experiment_id,assume_initialized=True)
            return result
        # Same successful action is a read-only receipt, even during mail.
        with self._connect() as db:
            db.execute('BEGIN')
            prior=replay(db)
            if prior is not None: return prior
        with (self.database.external_effect_guard(timeout=2) if self.database is not None else nullcontext()), self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                prior=replay(db)
                if prior is not None:
                    db.rollback()
                    return prior
                row=db.execute('SELECT * FROM habit_experiment WHERE experiment_id=? AND child_id=?',
                               (experiment_id,child_id)).fetchone()
                from .habit_formation import synchronize_habit_formation
                if row['status'] in {'proposed','active'}:
                    db.execute("UPDATE habit_experiment SET status='stopped',closed_at=? WHERE experiment_id=? AND child_id=?",
                               (at,experiment_id,child_id))
                    db.execute('''INSERT INTO habit_experiment_event(event_id,experiment_id,child_id,event_type,payload_json,occurred_at,client_action_id)
                        VALUES(?,?,?,?,?,?,?)''', (str(uuid4()),experiment_id,child_id,'stopped','{}',at,client_action_id))
                    db.execute('''UPDATE intervention SET status='withdrawn',withdrawn_at=?,
                        withdrawal_reason='habit_experiment_stopped' WHERE child_id=? AND status='active'
                        AND feedback IS NULL AND intervention_type='contextual_habit_ask_first'
                        AND CASE WHEN json_valid(support_basis_json)
                            THEN json_extract(support_basis_json,'$.experiment_id') END=?''', (at,child_id,experiment_id))
                    self._refresh_habit_experiments(db,child_id,at_dt)
                formation=synchronize_habit_formation(db,child_id,experiment_id,at_dt)
                saved=db.execute('SELECT * FROM habit_experiment WHERE experiment_id=? AND child_id=?',
                                 (experiment_id,child_id)).fetchone()
                db.commit()
            except BaseException:
                if db.in_transaction: db.rollback()
                raise
        result = self._habit_experiment_dict(saved)
        result["formation"] = formation
        return result

    def goal_behavior_opportunities(self, child_id: str, experiment_id: str, *, now=None, connection=None) -> dict:
        from .goal_confirmation import opportunities
        instant = _parse_time(now) if now else datetime.now(timezone.utc)
        if connection is not None:
            return opportunities(connection, child_id, experiment_id, instant)
        with self._connect() as db:
            db.execute('BEGIN')
            return opportunities(db, child_id, experiment_id, instant)

    def confirm_goal_behavior(self, child_id: str, experiment_id: str, *, opportunity_id, expected_version,
                              opportunity_present, criteria, initiated_by, assistance_level, actor_provenance,
                              client_action_id, operation='confirm', reason='', at=None, connection=None) -> dict:
        from .goal_confirmation import confirm, GoalBehaviorConflict
        stamp = at or datetime.now(timezone.utc).isoformat()
        arguments = dict(opportunity_id=opportunity_id, expected_version=expected_version,
                         opportunity_present=opportunity_present, criteria=criteria, initiated_by=initiated_by,
                         assistance_level=assistance_level, actor_provenance=actor_provenance,
                         client_action_id=client_action_id, operation=operation, reason=reason, at=stamp)
        def changes_prior_confirmation(db):
            # A repeated action is validated by confirm() and never changes
            # the old receipt. A malformed prior event is ambiguous, so take
            # the guard rather than assume this is a first confirmation.
            if db.execute('SELECT 1 FROM habit_experiment_event WHERE client_action_id=?', (client_action_id,)).fetchone():
                return False
            return bool(db.execute('''SELECT 1 FROM habit_experiment_event
                WHERE child_id=? AND experiment_id=?
                  AND event_type IN ('goal_behavior_confirmed','goal_behavior_retracted')
                  AND CASE WHEN json_valid(payload_json)
                    THEN json_extract(payload_json,'$.opportunity_id')=? ELSE 1 END
                LIMIT 1''', (child_id,experiment_id,opportunity_id)).fetchone())
        if connection is not None:
            if not isinstance(connection, sqlite3.Connection) or not connection.in_transaction:
                raise GoalBehaviorConflict('BEHAVIOR_WRITE_TRANSACTION_REQUIRED')
            if changes_prior_confirmation(connection):
                from ..parent_reports import _held_original_effect_transaction
                if self.database is None or not _held_original_effect_transaction(self.database,connection):
                    raise GoalBehaviorConflict('BEHAVIOR_EFFECT_GUARD_REQUIRED')
            return confirm(connection, child_id, experiment_id, **arguments)
        def attempt(guarded):
            with self._connect() as db:
                db.execute('BEGIN IMMEDIATE')
                try:
                    if not guarded and changes_prior_confirmation(db):
                        db.rollback()
                        raise _GoalConfirmationNeedsEffectGuard()
                    receipt = confirm(db, child_id, experiment_id, **arguments)
                    if receipt.get('replayed'):
                        db.rollback()
                        return receipt
                    self._refresh_habit_experiments(db, child_id, _parse_time(stamp))
                    from .habit_formation import synchronize_habit_formation
                    synchronize_habit_formation(db, child_id, experiment_id, _parse_time(stamp))
                    db.commit()
                    return receipt
                except BaseException:
                    if db.in_transaction:
                        db.rollback()
                    raise
        try:
            return attempt(False)
        except _GoalConfirmationNeedsEffectGuard:
            with self.database.external_effect_guard(timeout=2) if self.database is not None else nullcontext():
                return attempt(True)

    def habit_experiments(self, child_id: str, now: str | None = None) -> dict:
        now_dt = _parse_time(now) if now else datetime.now(timezone.utc)
        from ..parent_reports import _report_effect_guard
        guard = _report_effect_guard(self.database, timeout=2) if self.database is not None else nullcontext()
        with guard, self._connect() as db:
            self._refresh_habit_experiments(db, child_id, now_dt)
            from .habit_formation import synchronize_habit_formation
            for experiment in db.execute(
                "SELECT experiment_id FROM habit_experiment WHERE child_id=?",
                (child_id,),
            ).fetchall():
                synchronize_habit_formation(
                    db, child_id, experiment["experiment_id"], now_dt
                )
            rows = db.execute(
                "SELECT * FROM habit_experiment WHERE child_id = ? ORDER BY proposed_at DESC",
                (child_id,),
            ).fetchall()
            formation_rows = db.execute(
                "SELECT * FROM habit_formation_state WHERE child_id=?",
                (child_id,),
            ).fetchall()
            pending_ids = {row[0] for row in db.execute('''SELECT h.experiment_id FROM habit_experiment h
                JOIN habit_experiment_event e ON e.child_id=h.child_id AND e.experiment_id=h.experiment_id
                WHERE h.child_id=? AND h.status='active' AND e.event_type='auto_stop_pending' ''', (child_id,))}
            retry_ids = {row[0] for row in db.execute('''SELECT h.experiment_id FROM habit_experiment h
                JOIN habit_experiment_event e ON e.child_id=h.child_id AND e.experiment_id=h.experiment_id
                WHERE h.child_id=? AND h.status='active' AND e.event_type='auto_stop_attempt_failed' ''', (child_id,))}
            db.commit()
        formations = {
            row["experiment_id"]: {
                "experiment_id": row["experiment_id"], "child_id": row["child_id"],
                "target_state": row["target_state"], "stage": row["stage"],
                "cue_level": row["cue_level"], "revision": int(row["revision"]),
                "assessment_fingerprint": row["assessment_fingerprint"],
                "metrics": json.loads(row["metrics_json"]),
                "transition_reason": row["transition_reason"],
                "policy_version": row["policy_version"], "updated_at": row["updated_at"],
                "habit_success": False, "local_only": True,
                "cloud_transmission_allowed": False,
            }
            for row in formation_rows
        }
        items = []
        for row in rows:
            item = self._habit_experiment_dict(row)
            item["formation"] = formations.get(item["experiment_id"])
            item['stop_pending'] = item['experiment_id'] in pending_ids
            item['stop_retrying'] = item['stop_pending'] and item['experiment_id'] in retry_ids
            items.append(item)
        return {
            "child_id": child_id,
            "items": items,
            "boundary": "实验只比较本地可观察事件的变化，不推断人格、意志力、情绪或医学状态。",
        }

    def process_pending_habit_stops(self, limit: int = 10) -> dict:
        """Finish queued automatic stops without holding a child write during SMTP."""
        from .habit_formation import auto_stop_pending, synchronize_habit_formation
        with self._connect() as db:
            pending = db.execute('''SELECT DISTINCT h.child_id,h.experiment_id FROM habit_experiment h
                JOIN habit_experiment_event e ON e.child_id=h.child_id AND e.experiment_id=h.experiment_id
                WHERE h.status='active' AND e.event_type='auto_stop_pending'
                ORDER BY h.child_id,h.experiment_id LIMIT ?''', (max(1, min(int(limit), 100)),)).fetchall()
        from ..database import ExternalEffectBusyError
        completed = failed = waiting = 0
        for child_id, experiment_id in pending:
            try:
                with (self.database.external_effect_guard(timeout=2) if self.database is not None else nullcontext()), self._connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    try:
                        did_stop = auto_stop_pending(db, child_id, experiment_id)
                        if did_stop:
                            now = datetime.now(timezone.utc)
                            synchronize_habit_formation(db, child_id, experiment_id, now, complete_auto_stop=True)
                            # Persist the stopped-status fingerprint in this same
                            # guarded transaction, not on a later read request.
                            synchronize_habit_formation(db, child_id, experiment_id, now, complete_auto_stop=True)
                        db.commit()
                        completed += int(did_stop)
                    except BaseException:
                        if db.in_transaction: db.rollback()
                        raise
            except ExternalEffectBusyError:
                waiting += 1
            except Exception:
                failed += 1
                # One durable diagnostic is enough; the pending event remains
                # authoritative even if this best-effort write also fails.
                try:
                    with self._connect() as db:
                        action='habit-auto-stop-failed:' + hashlib.sha256((child_id+'\0'+experiment_id).encode()).hexdigest()
                        db.execute('''INSERT OR IGNORE INTO habit_experiment_event
                            (event_id,experiment_id,child_id,event_type,payload_json,occurred_at,client_action_id)
                            VALUES(?,?,?,'auto_stop_attempt_failed','{}',?,?)''',
                            (str(uuid4()),experiment_id,child_id,datetime.now(timezone.utc).isoformat(),action))
                        db.commit()
                except Exception:
                    pass
        return {'completed': completed, 'waiting': waiting, 'failed': failed}

    def habit_formation_history(self, child_id: str, experiment_id: str) -> dict:
        """Return the immutable local stage audit without exposing raw media."""
        with self._connect() as db:
            owner = db.execute(
                "SELECT 1 FROM habit_experiment WHERE experiment_id=? AND child_id=?",
                (experiment_id, child_id),
            ).fetchone()
            if not owner:
                raise KeyError("Unknown habit experiment")
            from .habit_formation import ensure_habit_formation_schema, habit_formation_state
            ensure_habit_formation_schema(db)
            state = habit_formation_state(db, child_id, experiment_id)
            rows = db.execute(
                """SELECT event_id,from_stage,to_stage,from_cue_level,to_cue_level,
                          assessment_fingerprint,metrics_json,reason,policy_version,occurred_at
                   FROM habit_formation_event
                   WHERE experiment_id=? AND child_id=? ORDER BY event_seq""",
                (experiment_id, child_id),
            ).fetchall()
        return {
            "child_id": child_id,
            "experiment_id": experiment_id,
            "state": state,
            "events": [
                {**{key: row[key] for key in row.keys() if key != "metrics_json"},
                 "metrics": json.loads(row["metrics_json"])}
                for row in rows
            ],
            "habit_success": False,
            "local_only": True,
            "cloud_transmission_allowed": False,
        }

    def decide_intervention(self, child_id: str, session_id: str | None, now: str, *, connection=None) -> dict:
        """Return a suggestion only under cooldown/daily-frequency safeguards."""
        savepoint = 'intervention_attempt_' + uuid4().hex if connection is not None else None
        if savepoint:
            connection.execute('SAVEPOINT ' + savepoint)
        try:
            result = self._decide_intervention(child_id, session_id, now, connection=connection)
            if savepoint:
                connection.execute('RELEASE SAVEPOINT ' + savepoint)
            return result
        except _InterventionWithdrawalNeedsEffectGuard:
            if savepoint:
                connection.execute('ROLLBACK TO SAVEPOINT ' + savepoint)
                connection.execute('RELEASE SAVEPOINT ' + savepoint)
            from ..database import ExternalEffectBusyError
            if connection is not None:
                return {'status':'suppressed','reason':'habit_support_withdrawal_pending'}
            try:
                with (self.database.external_effect_guard(timeout=.05) if self.database is not None else nullcontext()):
                    return self._decide_intervention(child_id, session_id, now, guarded=True)
            except ExternalEffectBusyError:
                return {'status':'suppressed','reason':'habit_support_withdrawal_pending'}
        except BaseException:
            if savepoint:
                connection.execute('ROLLBACK TO SAVEPOINT ' + savepoint)
                connection.execute('RELEASE SAVEPOINT ' + savepoint)
            raise

    def _decide_intervention(self, child_id: str, session_id: str | None, now: str, *, connection=None, guarded=False) -> dict:
        now_dt = _parse_time(now)
        with self._connect(connection) as db, (db if connection is None else nullcontext()):
            if connection is None:
                db.execute("BEGIN IMMEDIATE")
            if self.database is not None:
                session = db.execute(
                    "SELECT child_id FROM daily_session WHERE session_id=?", (session_id,)
                ).fetchone()
                if session is None or session["child_id"] != child_id:
                    return {"status": "no_action", "reason": "session_scope_mismatch"}
            from .habit_formation import synchronize_habit_formation
            for experiment in db.execute(
                """SELECT experiment_id FROM habit_experiment
                   WHERE child_id=? AND status IN ('proposed','active')""",
                (child_id,),
            ).fetchall():
                synchronize_habit_formation(
                    db, child_id, experiment["experiment_id"], now_dt
                )
            pending = db.execute(
                f"""SELECT * FROM intervention WHERE child_id = ? AND session_id IS ?
                   AND {INTERVENTION_SOURCE_OWNER}
                   AND status = 'active' AND feedback IS NULL AND decided_at >= ? ORDER BY decided_at DESC LIMIT 1""",
                (child_id, session_id, (now_dt - timedelta(minutes=15)).isoformat()),
            ).fetchone()
            from .habit_formation import auto_stop_pending_for_state
            if pending and auto_stop_pending_for_state(db, child_id, pending['candidate_state']):
                pending = None
            if pending and not self._withdraw_stale_optional_support(db, pending, now_dt, guarded=guarded):
                return {"status": "suppressed", "reason": "awaiting_child_feedback"}
            recent = db.execute(
                """SELECT ps.event_id, ps.state, ps.probability, ps.evidence_json, ps.alternatives_json, ps.occurred_at
                   FROM probabilistic_state ps
                   JOIN observable_event oe ON oe.event_id=ps.event_id
                     AND oe.child_id=ps.child_id AND oe.session_id IS ps.session_id
                   LEFT JOIN behavior_retraction br ON br.event_id = ps.event_id
                   WHERE ps.child_id = ? AND ps.session_id IS ? AND ps.occurred_at >= ?
                     AND br.event_id IS NULL ORDER BY ps.occurred_at DESC""",
                (child_id, session_id, (now_dt - timedelta(minutes=8)).isoformat()),
            ).fetchall()
            candidates = []
            for row in recent:
                item = dict(row)
                if _parse_time(item["occurred_at"]) > now_dt:
                    continue
                if item["state"] == "frequent_switching" and self._session_switch_count(
                    db, child_id, session_id, item["occurred_at"]
                ) < 3:
                    continue
                item["decayed_probability"] = _decay(item["probability"], item["occurred_at"], now_dt)
                if item["state"] in {"blocked_candidate", "distraction_candidate", "fatigue_candidate", "frequent_switching"} and item["decayed_probability"] >= .60:
                    candidates.append(item)
            if not candidates:
                # Retractions are excluded from candidates.  Still return an
                # explainable stop while the just-corrected evidence would
                # otherwise have been eligible for a reminder.
                corrected = db.execute(
                    """SELECT 1 FROM behavior_stop_condition sc
                       JOIN probabilistic_state ps ON ps.event_id = sc.source_event_id
                       WHERE sc.child_id = ? AND sc.session_id IS ? AND sc.expires_at > ?
                         AND ps.occurred_at >= ? LIMIT 1""",
                    (child_id, session_id, now_dt.isoformat(), (now_dt - timedelta(minutes=8)).isoformat()),
                ).fetchone()
                if corrected:
                    return {"status": "suppressed", "reason": "child_corrected_recent_observation"}
                return {"status": "no_action", "reason": "No sufficiently supported actionable candidate."}
            candidate = candidates[0]
            from .intervention_withdrawal import pending_for_state
            if pending_for_state(db, child_id, candidate['state']):
                return {'status':'suppressed','reason':'habit_support_withdrawal_pending'}
            stop = db.execute(
                """SELECT 1 FROM behavior_stop_condition WHERE child_id = ? AND session_id IS ?
                   AND candidate_state = ? AND expires_at > ? LIMIT 1""",
                (child_id, session_id, candidate["state"], now_dt.isoformat()),
            ).fetchone()
            if stop:
                return {"status": "suppressed", "reason": "child_corrected_recent_observation"}
            from .habit_formation import current_prompt_policy
            formation = current_prompt_policy(db, child_id, candidate["state"])
            if formation and formation["suppress_voice"]:
                return {
                    "status": "suppressed",
                    "reason": formation["suppression_reason"],
                    "formation": formation,
                }
            policy = self._policy_summary(db, child_id, candidate["state"], now_dt)
            support = goal_support_basis(db, child_id, session_id, candidate["state"], candidate["occurred_at"], now_dt)
            if support:
                policy["cooldown_minutes"] = max(policy["cooldown_minutes"], support["cooldown_minutes"])
                policy["daily_cap"] = min(policy["daily_cap"], support["daily_cap"])
                if not policy["adjusted"]:
                    policy["strategy"] = "autonomy_faded_ask_first"
                policy["explanation"].append("同情境近期已有重复的独立行为证据，减少主动打断，仍保留随时求助；不据此认定习惯养成或没有负担。")
            optional_support = None
            if support is None and not policy["adjusted"]:
                optional_support = accepted_habit_support(db, child_id, session_id, candidate["state"], candidate["occurred_at"], now_dt)
                if optional_support:
                    policy["strategy"] = "contextual_habit_ask_first"
                    policy["explanation"].append("仅在已有来源支持的同情境下提供孩子接受过的可选行动，不作为任务完成条件，也不表示习惯成功。")
            last = db.execute("SELECT decided_at FROM intervention WHERE child_id = ? ORDER BY decided_at DESC LIMIT 1", (child_id,)).fetchone()
            if last and _parse_time(last["decided_at"]) > now_dt - timedelta(minutes=10):
                return {"status": "suppressed", "reason": "cooldown"}
            candidate_last = db.execute("SELECT decided_at FROM intervention WHERE child_id = ? AND candidate_state = ? ORDER BY decided_at DESC LIMIT 1", (child_id, candidate["state"])).fetchone()
            if candidate_last and _parse_time(candidate_last["decided_at"]) > now_dt - timedelta(minutes=policy["cooldown_minutes"]):
                return {"status": "suppressed", "reason": "personalized_cooldown", "explanation": policy["explanation"]}
            day_start = family_time(now_dt).replace(hour=0, minute=0, second=0, microsecond=0)
            start_utc = day_start.astimezone(timezone.utc).isoformat()
            end_utc = (day_start + timedelta(days=1)).astimezone(timezone.utc).isoformat()
            today_count = db.execute("SELECT COUNT(*) AS count FROM intervention WHERE child_id = ? AND datetime(decided_at) >= datetime(?) AND datetime(decided_at) < datetime(?)", (child_id, start_utc, end_utc)).fetchone()["count"]
            if today_count >= 3:
                return {"status": "suppressed", "reason": "daily_frequency_cap"}
            candidate_today = db.execute("SELECT COUNT(*) AS count FROM intervention WHERE child_id = ? AND candidate_state = ? AND datetime(decided_at) >= datetime(?) AND datetime(decided_at) < datetime(?)", (child_id, candidate["state"], start_utc, end_utc)).fetchone()["count"]
            if candidate_today >= policy["daily_cap"]:
                return {"status": "suppressed", "reason": "personalized_frequency_cap", "explanation": policy["explanation"]}
            suggestion = {
                "blocked_candidate": "要不要把卡住的一步圈出来，或选择向我求助？",
                "distraction_candidate": "要不要先把当前题做到一个小节点，再决定是否切换？",
                "fatigue_candidate": "看起来任务节奏变慢了；要不要短暂喝水或伸展后继续？",
                "frequent_switching": "要不要选一个任务先完成 5 分钟的小段？",
            }[candidate["state"]] if policy["strategy"] == "default_ask_first" else "如果你愿意，可以先自己想一会儿；需要时再叫我一起看。"
            if optional_support:
                suggestion = f"你愿意试试我们约好的这一步吗？{optional_support['child_action']} 不想做也可以，也可以直接告诉我需要什么帮助。"
            if formation and formation["cue_level"] == "L2":
                suggestion = "要不要用我们约好的那一步？不需要也可以。"
            elif formation and formation["cue_level"] == "L1":
                suggestion = "需要时叫我，我先不打断你。"
            support = support or optional_support
            intervention_id = str(uuid4())
            try:
                db.execute("""INSERT INTO intervention(intervention_id, child_id, session_id, source_event_id, decided_at,
                   candidate_state, message, intervention_type, status, support_basis_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)""",
                           (intervention_id, child_id, session_id, candidate["event_id"], now, candidate["state"], suggestion, policy["strategy"],
                            json.dumps(support, ensure_ascii=False, allow_nan=False) if support else None))
            except sqlite3.IntegrityError as error:
                if "UNIQUE constraint failed: intervention.source_event_id" in str(error):
                    return {"status": "suppressed", "reason": "duplicate_source_event"}
                raise
            if connection is None:
                db.commit()
            return {"status": "suggested", "intervention_id": intervention_id, "message": suggestion,
                    "candidate_state": candidate["state"], "evidence_probability": candidate["decayed_probability"], "requires_child_feedback": True,
                    "strategy": policy["strategy"], "support_basis": support,
                    "formation": formation,
                    "explanation": [f"当前候选的本地证据支持度为 {round(candidate['decayed_probability'] * 100)}%。", *policy["explanation"]]}

    @staticmethod
    def _withdraw_stale_optional_support(db, row, now, *, guarded=False):
        from .intervention_withdrawal import PENDING_REASON
        if row['withdrawal_reason'] == PENDING_REASON:
            return True
        if row['intervention_type'] != 'contextual_habit_ask_first':
            return False
        try:
            basis = json.loads(row['support_basis_json'] or '{}')
        except json.JSONDecodeError:
            basis = None
        current = accepted_habit_support(db, row['child_id'], row['session_id'], row['candidate_state'], row['decided_at'], now)
        if isinstance(basis,dict) and current and all(current[key]==basis.get(key)
            for key in ('goal_version','acceptance_ref','context_version','evidence_version')):
            return False
        if not guarded:
            raise _InterventionWithdrawalNeedsEffectGuard()
        db.execute("""UPDATE intervention SET withdrawal_reason=? WHERE intervention_id=? AND status='active'
            AND feedback IS NULL""", (PENDING_REASON,row['intervention_id']))
        return True

    def pending_feedback(self, child_id: str, session_id: str, now: str) -> dict:
        """Return a restart-safe pending prompt, or explicitly report that it expired."""
        try:
            return self._pending_feedback(child_id, session_id, now)
        except _InterventionWithdrawalNeedsEffectGuard:
            from ..database import ExternalEffectBusyError
            try:
                with (self.database.external_effect_guard(timeout=.05) if self.database is not None else nullcontext()):
                    return self._pending_feedback(child_id, session_id, now, guarded=True)
            except ExternalEffectBusyError:
                # Suppress stale prompts while the sender holds the guard.
                return {'status':'no_pending'}

    def _pending_feedback(self, child_id: str, session_id: str, now: str, *, guarded=False) -> dict:
        now_dt = _parse_time(now)
        with self._connect() as db, db:
            db.execute("BEGIN IMMEDIATE" if guarded else "BEGIN")
            row = db.execute(
                f"""SELECT * FROM intervention WHERE child_id = ? AND session_id = ? AND status = 'active' AND feedback IS NULL
                   AND {INTERVENTION_SOURCE_OWNER}
                   ORDER BY decided_at DESC LIMIT 1""",
                (child_id, session_id),
            ).fetchone()
            from .habit_formation import auto_stop_pending_for_state
            if row and auto_stop_pending_for_state(db, child_id, row['candidate_state']):
                row = None
            if row and self._withdraw_stale_optional_support(db,row,now_dt,guarded=guarded):
                row = None
        if not row:
            return {"status": "no_pending"}
        item = self._intervention_dict(row)
        expires_at = (_parse_time(item["decided_at"]) + timedelta(minutes=15)).isoformat()
        if _parse_time(expires_at) <= now_dt:
            return {"status": "expired", "expires_at": expires_at}
        return {"status": "pending", "intervention": {
            "intervention_id": item["intervention_id"], "message": item["message"],
            "expires_at": expires_at, "requires_child_feedback": True,
            "candidate_state": item["candidate_state"],
            "trigger_source": (
                "document_monitor"
                if str(item.get("source_event_id") or "").startswith("document-revision:")
                else "local_observation"
            ),
        }}

    def process_pending_intervention_withdrawals(self, limit: int = 10) -> dict:
        """Finish stale reminders after the active mail owner releases its guard."""
        from ..database import ExternalEffectBusyError
        from .intervention_withdrawal import PENDING_REASON, FINAL_REASON
        cap = max(1,min(int(limit),100))
        completed = waiting = failed = 0
        # A prompt can be suppressed while SMTP owns the effect guard, before
        # the pending marker can be persisted. Rescan bounded candidates so a
        # restart or lack of another child request cannot lose that work.
        with self._connect() as db:
            candidates = db.execute('''SELECT child_id,intervention_id FROM intervention
                WHERE status='active' AND feedback IS NULL AND withdrawal_reason IS NULL
                  AND intervention_type='contextual_habit_ask_first'
                ORDER BY decided_at,intervention_id LIMIT ?''', (cap,)).fetchall()
        for child_id, intervention_id in candidates:
            try:
                with (self.database.external_effect_guard(timeout=.05) if self.database is not None else nullcontext()), self._connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    row=db.execute('''SELECT * FROM intervention WHERE intervention_id=? AND child_id=?
                        AND status='active' AND feedback IS NULL AND withdrawal_reason IS NULL''',
                        (intervention_id,child_id)).fetchone()
                    if row:
                        self._withdraw_stale_optional_support(db,row,datetime.now(timezone.utc),guarded=True)
                    db.commit()
            except ExternalEffectBusyError:
                waiting += 1
            except Exception:
                failed += 1
        with self._connect() as db:
            rows = db.execute('''SELECT child_id,intervention_id FROM intervention
                WHERE status='active' AND withdrawal_reason=?
                ORDER BY decided_at,intervention_id LIMIT ?''',
                (PENDING_REASON,cap)).fetchall()
        for child_id, intervention_id in rows:
            try:
                with (self.database.external_effect_guard(timeout=2) if self.database is not None else nullcontext()), self._connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    try:
                        count=db.execute('''UPDATE intervention SET status='withdrawn',withdrawn_at=?,withdrawal_reason=?
                            WHERE intervention_id=? AND child_id=? AND status='active'
                              AND withdrawal_reason=?''',
                            (datetime.now(timezone.utc).isoformat(),FINAL_REASON,intervention_id,child_id,PENDING_REASON)).rowcount
                        db.commit()
                        completed += count
                    except BaseException:
                        if db.in_transaction: db.rollback()
                        raise
            except ExternalEffectBusyError:
                waiting += 1
            except Exception:
                failed += 1
        return {'completed':completed,'waiting':waiting,'failed':failed}

    def feedback_for_action(self, feedback_action_id: str, child_id: str, session_id: str) -> dict | None:
        """Find an already committed feedback action without exposing another child's data."""
        with self._connect() as db:
            row = db.execute(
                f"SELECT * FROM intervention WHERE feedback_action_id = ? AND child_id = ? AND session_id = ? AND {INTERVENTION_SOURCE_OWNER}",
                (feedback_action_id, child_id, session_id),
            ).fetchone()
        if not row:
            return None
        item = self._intervention_dict(row)
        return {
            "intervention_id": item["intervention_id"], "feedback": item["feedback"],
            "note": item["feedback_note"], "payload_hash": item["feedback_payload_hash"],
            "candidate_state": item["candidate_state"],
            "trigger_source": (
                "document_monitor"
                if str(item.get("source_event_id") or "").startswith("document-revision:")
                else "local_observation"
            ),
        }

    def record_feedback(
        self,
        intervention_id: str,
        accepted: bool,
        at: str,
        note: str | None = None,
        client_action_id: str | None = None,
        child_id: str | None = None,
        session_id: str | None = None,
        feedback_payload_hash: str | None = None,
    ) -> dict:
        now_dt = _parse_time(at)
        from ..parent_reports import _report_effect_guard
        guard = _report_effect_guard(self.database, timeout=2) if self.database is not None else nullcontext()
        with guard, self._connect() as db:
            if client_action_id:
                action_owner = db.execute(
                    "SELECT intervention_id FROM intervention WHERE feedback_action_id = ?", (client_action_id,)
                ).fetchone()
                if action_owner and action_owner["intervention_id"] != intervention_id:
                    raise ValueError("Feedback action id belongs to a different intervention.")
            clauses = ["intervention_id = ?"]
            params: list[str] = [intervention_id]
            if child_id is not None:
                clauses.append("child_id = ?"); params.append(child_id)
            if session_id is not None:
                clauses.append("session_id = ?"); params.append(session_id)
            row = db.execute(f"SELECT * FROM intervention WHERE {' AND '.join(clauses)} AND {INTERVENTION_SOURCE_OWNER}", params).fetchone()
            if not row:
                raise KeyError("Unknown intervention")
            if row["feedback"]:
                expected_feedback = "accepted" if accepted else "declined"
                if (client_action_id and row["feedback_action_id"] == client_action_id
                        and row["feedback"] == expected_feedback and row["feedback_note"] == note
                        and row["feedback_payload_hash"] == feedback_payload_hash):
                    return {"intervention_id": intervention_id, "feedback": row["feedback"], "idempotent": True}
                raise ValueError("Intervention feedback has already been recorded.")
            from .intervention_withdrawal import FINAL_REASON
            if row['status'] != 'active' and not (row['status']=='withdrawn' and row['withdrawal_reason']==FINAL_REASON):
                raise ValueError("Intervention has been withdrawn.")
            if _parse_time(row["decided_at"]) + timedelta(minutes=15) <= now_dt:
                raise ValueError("Intervention feedback has expired.")
            # Feedback may race the child-facing stale-prompt check. Preserve
            # the answer, but also persist the necessary withdrawal while the
            # same disclosure guard is held, even if no later poll occurs.
            self._withdraw_stale_optional_support(db,row,now_dt,guarded=True)
            written=db.execute(f'''UPDATE intervention SET feedback = ?, feedback_at = ?, feedback_note = ?,
                feedback_action_id = ?, feedback_payload_hash = ? WHERE {' AND '.join(clauses)}
                AND {INTERVENTION_SOURCE_OWNER} AND feedback IS NULL
                AND (status='active' OR (status='withdrawn' AND withdrawal_reason=?))''',
                ("accepted" if accepted else "declined", at, note, client_action_id, feedback_payload_hash, *params, FINAL_REASON)).rowcount
            if written != 1:
                current=db.execute(f"SELECT * FROM intervention WHERE {' AND '.join(clauses)} AND {INTERVENTION_SOURCE_OWNER}", params).fetchone()
                expected_feedback='accepted' if accepted else 'declined'
                replay=bool(current and client_action_id and current['feedback_action_id']==client_action_id
                    and current['feedback']==expected_feedback and current['feedback_note']==note
                    and current['feedback_payload_hash']==feedback_payload_hash)
                db.rollback()
                if replay:
                    return {'intervention_id':intervention_id,'feedback':expected_feedback,'idempotent':True}
                raise ValueError('Intervention changed before feedback was saved.')
            db.commit()
        return {"intervention_id": intervention_id, "feedback": "accepted" if accepted else "declined"}

    def evaluate_effect(
        self, intervention_id: str, outcome_state: str, probability: float, at: str,
        child_id: str, session_id: str, client_action_id: str,
    ) -> dict:
        observed_at = _parse_time(at)
        if not 0 <= probability <= 1:
            raise ValueError("probability must be between 0 and 1")
        if not isinstance(client_action_id, str) or not client_action_id.strip():
            raise ValueError("client_action_id is required for an intervention effect")
        with self._connect() as db:
            payload_hash = hashlib.sha256(json.dumps(
                {"outcome_state": outcome_state, "probability": probability, "observed_at": at}, sort_keys=True
            ).encode("utf-8")).hexdigest()
            row = db.execute(
                f"SELECT status, decided_at, outcome_json, effect_action_id, effect_payload_hash FROM intervention WHERE intervention_id = ? AND child_id = ? AND session_id = ? AND {INTERVENTION_SOURCE_OWNER}",
                (intervention_id, child_id, session_id),
            ).fetchone()
            if not row:
                raise KeyError("Unknown intervention")
            if observed_at < _parse_time(row["decided_at"]):
                raise ValueError("Intervention effect cannot precede the decision.")
            if row["effect_action_id"]:
                if row["effect_action_id"] == client_action_id and row["effect_payload_hash"] == payload_hash:
                    return {"intervention_id": intervention_id, "status": "effect_recorded", "idempotent": True}
                raise ValueError("Effect action id conflicts with another intervention or payload.")
            if row["status"] != "active":
                raise ValueError("Intervention has been withdrawn.")
            owner = db.execute("SELECT intervention_id FROM intervention WHERE effect_action_id = ?", (client_action_id,)).fetchone()
            if owner:
                raise ValueError("Effect action id conflicts with another intervention or payload.")
            if row["outcome_json"]:
                raise ValueError("Intervention effect has already been recorded.")
            db.execute("UPDATE intervention SET outcome_json = ?, effect_action_id = ?, effect_payload_hash = ? WHERE intervention_id = ?",
                       (json.dumps({"state": outcome_state, "probability": probability, "observed_at": at}), client_action_id, payload_hash, intervention_id))
            db.commit()
        return {"intervention_id": intervention_id, "status": "effect_recorded", "idempotent": False}

    def personalization(self, child_id: str) -> dict:
        with self._connect() as db:
            states = [row["candidate_state"] for row in db.execute("SELECT DISTINCT candidate_state FROM intervention WHERE child_id = ? ORDER BY candidate_state", (child_id,))]
            return {"child_id": child_id, "minimum_feedback_samples": 4, "items": [self._policy_summary(db, child_id, state) for state in states],
                    "boundary": "Feedback can only lower reminder frequency or keep an ask-first wording; it never increases observation or creates diagnoses."}

    def reset_personalization(self, child_id: str, at: str) -> dict:
        _parse_time(at)
        with self._connect() as db:
            db.execute("INSERT INTO intervention_policy_reset(child_id, reset_at) VALUES (?, ?) ON CONFLICT(child_id) DO UPDATE SET reset_at = excluded.reset_at", (child_id, at))
            db.commit()
        return self.personalization(child_id)

    def _policy_summary(self, db: sqlite3.Connection, child_id: str, candidate_state: str, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        reset = db.execute("SELECT reset_at FROM intervention_policy_reset WHERE child_id = ?", (child_id,)).fetchone()
        reset_at = reset["reset_at"] if reset else ""
        rows = db.execute(
            """SELECT i.*, oe.payload_json, oe.missing_sources_json, oe.confidence, oe.source
               FROM intervention i
               LEFT JOIN observable_event oe ON oe.event_id=i.source_event_id
                 AND oe.child_id=i.child_id AND oe.session_id IS i.session_id
               LEFT JOIN behavior_retraction br ON br.event_id=i.source_event_id
               WHERE i.child_id = ? AND i.candidate_state = ?
                 AND i.status IN ('active','withdrawn')
                 AND (i.source_event_id IS NULL OR oe.event_id IS NOT NULL)
                 AND br.event_id IS NULL""",
            (child_id, candidate_state),
        ).fetchall()
        rows = [row for row in rows if (not reset_at or _parse_time(row["decided_at"]) >= _parse_time(reset_at))
                and _parse_time(row["decided_at"]) <= now]
        feedback = [row["feedback"] for row in rows if row["feedback"] in {"accepted", "declined"}]
        accepted = feedback.count("accepted"); declined = feedback.count("declined")
        effects = effect_summary(rows, now)
        feedback_samples = len(feedback); decline_rate = round(declined / feedback_samples, 2) if feedback_samples else None
        declined_often = feedback_samples >= 4 and (decline_rate or 0) >= .60
        limited_recovery = effects["effect_status"] == "limited_recovery"
        adjusted = declined_often or limited_recovery
        strategy = "low_frequency_ask_first" if adjusted else "default_ask_first"
        explanation = ([f"已有 {feedback_samples} 次孩子反馈，其中 {declined} 次拒绝；该场景会降低提醒频率，仍先询问。"] if declined_often
                       else [f"已有 {feedback_samples} 次孩子反馈；接受不等于提醒有效，也不会因此增加提醒。"])
        if limited_recovery:
            explanation.append(f"同类支持已有 {effects['effect_samples']} 次可信后续记录，多数仍未恢复任务；降低提醒频率，改为需要时再求助，不推断原因或心理状态。")
        elif effects["effect_status"] == "recovery_observed":
            explanation.append("后续记录中观察到恢复任务，保留先询问的支持；这不是习惯改善或因果效果证明，不增加频率。")
        else:
            explanation.append("效果记录数量、覆盖或一致性不足，暂不据此调整支持。")
        return {"candidate_state": candidate_state, "sample_size": feedback_samples, "accepted": accepted, "declined": declined,
                "decline_rate": decline_rate, **effects,
                "strategy": strategy, "cooldown_minutes": 20 if adjusted else 10, "daily_cap": 2 if adjusted else 3,
                "adjusted": adjusted, "explanation": explanation}

    @staticmethod
    def _maybe_propose_habit_experiment(
        db: sqlite3.Connection, child_id: str, candidate_state: str, occurred_at: str
    ) -> None:
        template = HABIT_EXPERIMENTS.get(candidate_state)
        if not template:
            return
        open_experiment = db.execute(
            """SELECT 1 FROM habit_experiment
               WHERE child_id = ? AND status IN ('proposed','active') LIMIT 1""",
            (child_id,),
        ).fetchone()
        if open_experiment:
            return
        evidence = db.execute(
            """SELECT COUNT(*) AS event_count,
                      COUNT(DISTINCT behavior_instant_day(ps.occurred_at)) AS day_count
               FROM probabilistic_state ps
               JOIN observable_event oe ON oe.event_id=ps.event_id AND oe.child_id=ps.child_id AND oe.session_id IS ps.session_id
               LEFT JOIN behavior_retraction br ON br.event_id = ps.event_id
               WHERE ps.child_id = ? AND ps.state = ? AND br.event_id IS NULL
                 AND behavior_instant_day(ps.occurred_at) IS NOT NULL
                 AND behavior_instant_day(oe.occurred_at) IS NOT NULL
                 AND ps.probability >= .60 AND ps.occurred_at >= ?""",
            (
                child_id, candidate_state,
                (_parse_time(occurred_at) - timedelta(days=14)).isoformat(),
            ),
        ).fetchone()
        if int(evidence["event_count"] or 0) < 3 or int(evidence["day_count"] or 0) < 2:
            return
        feature = str(template["feature"])
        feature_rows = db.execute(
            f"SELECT day,{feature} AS value FROM daily_feature WHERE child_id=? ORDER BY day DESC LIMIT 14",
            (child_id,),
        ).fetchall()
        values = [int(row["value"] or 0) for row in feature_rows]
        baseline = {
            "feature": feature,
            "days": len(values),
            "average": round(sum(values) / len(values), 3) if values else None,
            "event_count": int(evidence["event_count"] or 0),
            "evidence_day_count": int(evidence["day_count"] or 0),
            "coverage": observation_summary(
                BehaviorModelService._current_observations(db, child_id), DAILY_EVENT_FEATURES[feature],
                _parse_time(occurred_at) - timedelta(days=14), _parse_time(occurred_at) + timedelta(microseconds=1),
            ),
            "status": "insufficient_evidence",
        }
        experiment_id = str(uuid4())
        db.execute(
            """INSERT INTO habit_experiment(
                 experiment_id,child_id,target_state,title,child_action,success_signal,
                 duration_days,baseline_json,status,proposed_at
               ) VALUES(?,?,?,?,?,?,7,?,'proposed',?)""",
            (
                experiment_id, child_id, candidate_state, template["title"],
                template["child_action"], template["success_signal"],
                json.dumps(baseline, ensure_ascii=False), occurred_at,
            ),
        )
        from ..bounded_experiment_protocol import ensure_protocol
        ensure_protocol(
            db, child_id=child_id, domain="behavior_intervention",
            source_kind="habit_experiment", source_id=experiment_id,
            primary_variable=candidate_state, baseline_version="observed_baseline_v1",
            candidate_version="habit_formation_policy_v1", status="draft",
            metrics={
                "sample_size": int(evidence["event_count"] or 0),
                "source_metrics": baseline,
            },
            now=occurred_at,
            reason="观察证据达到候选门槛，建立单变量草案；尚未向孩子实施提示。",
        )

    @staticmethod
    def _current_observations(db: sqlite3.Connection, child_id: str) -> list:
        return db.execute(
            """SELECT oe.* FROM observable_event oe
               LEFT JOIN behavior_retraction br ON br.event_id=oe.event_id
               WHERE oe.child_id=? AND br.event_id IS NULL AND oe.source != 'observation_coverage'
               ORDER BY oe.occurred_at,oe.event_id""",
            (child_id,),
        ).fetchall()

    @staticmethod
    def _refresh_habit_experiments(
        db: sqlite3.Connection, child_id: str, now: datetime
    ) -> None:
        rows = db.execute(
            "SELECT * FROM habit_experiment WHERE child_id=? AND status IN ('active','completed','stopped')",
            (child_id,),
        ).fetchall()
        events = BehaviorModelService._current_observations(db, child_id) if rows else []
        coverage_rows = BehaviorModelService._coverage_records(db, child_id) if rows else []
        for row in rows:
            baseline = json.loads(row["baseline_json"])
            feature = str(baseline.get("feature") or "")
            if feature not in DAILY_EVENT_FEATURES or not row["started_at"]:
                continue
            started = _parse_time(row["started_at"])
            end = min(now, _parse_time(row["ends_at"])) if row["ends_at"] else now
            if row["status"] == "stopped" and row["closed_at"]:
                end = min(end, _parse_time(row["closed_at"]))
            before = observation_summary(events, DAILY_EVENT_FEATURES[feature], started - timedelta(days=14), started)
            current = observation_summary(events, DAILY_EVENT_FEATURES[feature], started, end)
            baseline_coverage = coverage_projection(coverage_rows, child_id, started - timedelta(days=14), started)
            current_coverage = coverage_projection(coverage_rows, child_id, started, end)
            comparison = compare_coverage(baseline_coverage, current_coverage, DAILY_EVENT_FEATURES[feature])
            before["coverage"] = baseline_coverage
            current["coverage"] = current_coverage
            for summary, measured in ((before, baseline_coverage), (current, current_coverage)):
                if measured["observed_seconds"] is not None:
                    summary["observed_seconds"] = measured["observed_seconds"]
                    summary["behavior_observed_seconds"] = measured["behavior_observed_seconds"]
                    summary["task_opportunities"] = measured["task_opportunities"]
                    summary["observed_days"] = measured["observed_days"]
                    summary["evidence_refs"] = measured["evidence_refs"]
                    summary["raw_event_count"] = summary["event_count"]
                    summary["event_count"] = (sum(item["event_counts"].get(DAILY_EVENT_FEATURES[feature], 0) for item in measured["strata"])
                                              if measured["behavior_observed_seconds"] and DAILY_EVENT_FEATURES[feature] in OBSERVATION_STATE_EVENTS.values() else None)
                    summary["event_rate"] = summary["event_count"] / measured["behavior_observed_seconds"] if summary["event_count"] is not None else None
                    if measured["observed_seconds"]:
                        summary["status"] = "coverage_only"
                        summary["missing_evidence"] = ["goal_behavior_confirmation"]
                        if not measured["behavior_observed_seconds"]:
                            summary["missing_evidence"].append("qualified_behavior_observations")
            outcome = {
                "feature": feature, "evaluation_version": "coverage_required_v1",
                "status": "insufficient_evidence", "direction": "not_yet_clear",
                "observed_days": current["observed_days"],
                "baseline_average": None, "current_average": None,
                "baseline": before, "current": current,
                "normalized_comparison": comparison,
                "window_start": started.isoformat(), "window_end": end.isoformat(),
                "missing_evidence": comparison["missing_evidence"],
                "explanation": ("已有可追溯的采样覆盖；行为率变化仍不等于习惯改善，需核对可比样本和目标行为确认。"
                                if current_coverage["observed_seconds"] else
                                "缺少有效观察覆盖、同情境任务机会和目标行为确认；事件变少或实验到期都不能证明习惯改善。"),
            }
            from .goal_confirmation import assessment
            goal_behavior = assessment(db, child_id, row['experiment_id'], now)
            outcome.update(goal_behavior=goal_behavior, status=goal_behavior['status'],
                           direction=goal_behavior['direction'], missing_evidence=goal_behavior['missing_evidence'])
            if goal_behavior['status'] in {'repeated_observed_behavior', 'bounded_confirmed_behavior'}:
                outcome['explanation'] = '已在限定任务机会中重复观察到目标行为；独立性、前后变化与长期习惯分别核验，不以候选率下降认证。'
            status = "completed" if row["status"] == "completed" or (row["ends_at"] and _parse_time(row["ends_at"]) <= now) else "active"
            if row["status"] == "stopped":
                status = "stopped"
            elif row['status'] == 'active':
                from .habit_formation import auto_stop_pending
                if auto_stop_pending(db, child_id, row['experiment_id']):
                    # The bounded window cannot retire a queued stop: doing so
                    # would discard its pending withdrawal and revive prompts.
                    status = 'active'
            db.execute(
                """UPDATE habit_experiment SET outcome_json=?,status=?,closed_at=?
                   WHERE experiment_id=?""",
                (
                    json.dumps(outcome, ensure_ascii=False), status,
                    (row["closed_at"] or now.isoformat()) if status in {"completed", "stopped"} else None,
                    row["experiment_id"],
                ),
            )

    @staticmethod
    def _habit_experiment_dict(row: sqlite3.Row) -> dict:
        item = dict(row)
        item["baseline"] = json.loads(item.pop("baseline_json"))
        outcome_json = item.pop("outcome_json")
        item["outcome"] = json.loads(outcome_json) if outcome_json else None
        return item

    def retract_event(self, event_id: str, child_id: str, correction_id: str, note: str) -> dict:
        """Keep the observation audit trail while removing corrected evidence from decisions/baselines."""
        with (self.database.external_effect_guard(timeout=2) if self.database is not None else nullcontext()), self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not self._daily_projection_current(db):
                self._rebuild_daily_features(db)
            row = db.execute("SELECT child_id, session_id, occurred_at FROM observable_event WHERE event_id = ?", (event_id,)).fetchone()
            if not row or row["child_id"] != child_id:
                return {"status": "not_found"}
            by_action = db.execute(
                "SELECT event_id, child_id, correction_id, note, created_at FROM behavior_retraction WHERE correction_id = ?",
                (correction_id,),
            ).fetchone()
            if by_action:
                existing = dict(by_action)
                if existing["event_id"] == event_id and existing["child_id"] == child_id and existing["note"] == note:
                    return {"status": "retracted", "idempotent": True, "correction": existing}
                raise ValueError("Correction action id conflicts with a different observation or correction.")
            existing_event = db.execute(
                "SELECT event_id, child_id, correction_id, note, created_at FROM behavior_retraction WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if existing_event:
                raise ValueError("This observation has already been retracted by a different correction.")
            created_at = datetime.now(timezone.utc).isoformat()
            db.execute("INSERT INTO behavior_retraction VALUES (?, ?, ?, ?, ?)", (event_id, child_id, correction_id, note, created_at))
            states = db.execute("SELECT DISTINCT state FROM probabilistic_state WHERE event_id = ?", (event_id,)).fetchall()
            for state in states:
                if state["state"] not in {"blocked_candidate", "distraction_candidate", "fatigue_candidate", "frequent_switching"}:
                    continue
                db.execute(
                    """INSERT INTO behavior_stop_condition(child_id, session_id, candidate_state, source_event_id, correction_id, expires_at)
                       VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(child_id, session_id, candidate_state, source_event_id)
                       DO NOTHING""",
                    (child_id, row["session_id"], state["state"], event_id, correction_id,
                     (_parse_time(created_at) + timedelta(minutes=30)).isoformat()),
                )
            db.execute(
                """UPDATE intervention SET status = 'withdrawn', withdrawn_at = ?, withdrawal_reason = 'corrected_observation'
                   WHERE child_id = ? AND source_event_id = ? AND status = 'active'""",
                (datetime.now(timezone.utc).isoformat(), child_id, event_id),
            )
            self._refresh_daily_features(db, child_id, row["occurred_at"])
            self._refresh_habit_experiments(db, child_id, datetime.now(timezone.utc))
            db.commit()
            return {"status": "retracted", "idempotent": False, "correction": {
                "event_id": event_id, "child_id": child_id, "correction_id": correction_id,
                "note": note, "created_at": created_at,
            }}

    def retract_observation_state(self, state_id: str, child_id: str, correction_id: str, note: str) -> dict:
        """Parent correction is limited to local observation evidence, never plan facts."""
        with self._connect() as db:
            row = db.execute("""SELECT ps.event_id, oe.source FROM probabilistic_state ps
                JOIN observable_event oe ON oe.event_id = ps.event_id AND oe.child_id=ps.child_id AND oe.session_id IS ps.session_id
                WHERE ps.state_id = ? AND ps.child_id = ?""", (state_id, child_id)).fetchone()
        if not row:
            raise KeyError("Unknown behavior state")
        if row["source"] != "local_vision":
            raise ValueError("Only local observation evidence can be retracted here.")
        return self.retract_event(row["event_id"], child_id, correction_id, note)

    def replay(self, child_id: str, session_id: str | None = None, *, connection=None) -> dict:
        clause, params = ("child_id = ?", [child_id]) if session_id is None else ("child_id = ? AND session_id = ?", [child_id, session_id])
        with self._connect(connection) as db:
            events = [self._event_dict(row) for row in db.execute(f"SELECT * FROM observable_event WHERE {clause} ORDER BY occurred_at, event_id", params)]
            retracted = {row["event_id"]: {"correction_id": row["correction_id"], "note": row["note"]} for row in db.execute("SELECT event_id, correction_id, note FROM behavior_retraction WHERE child_id = ?", (child_id,))}
            for event in events:
                if event["event_id"] in retracted: event["retracted"] = retracted[event["event_id"]]
            states = [self._state_dict(row) for row in db.execute(f"""SELECT * FROM probabilistic_state
                WHERE {clause} AND EXISTS(SELECT 1 FROM observable_event oe
                WHERE oe.event_id=probabilistic_state.event_id AND oe.child_id=probabilistic_state.child_id
                AND oe.session_id IS probabilistic_state.session_id) ORDER BY occurred_at, state_id""", params)]
            for state in states:
                if state["event_id"] in retracted: state["retracted"] = retracted[state["event_id"]]
                state["effective_probability"] = _decay(state["probability"], state["occurred_at"], datetime.now(timezone.utc))
            interventions = [self._intervention_dict(row) for row in db.execute(f"""SELECT * FROM intervention
                WHERE {clause} AND (source_event_id IS NULL OR EXISTS(SELECT 1 FROM observable_event oe
                WHERE oe.event_id=intervention.source_event_id AND oe.child_id=intervention.child_id
                AND oe.session_id IS intervention.session_id)) ORDER BY decided_at""", params)]
            issues = ownership_issues(db, child_id, session_id)
            now = datetime.now(timezone.utc)
            for intervention in interventions:
                from .intervention_withdrawal import PENDING_REASON
                intervention['withdrawal_pending'] = (intervention['status']=='active'
                    and intervention.get('withdrawal_reason')==PENDING_REASON)
                if not intervention['withdrawal_pending'] and intervention["status"] == "active" and not intervention["feedback"] and _parse_time(intervention["decided_at"]) + timedelta(minutes=15) <= now:
                    intervention["status"] = "expired"
                    intervention["expired_at"] = (_parse_time(intervention["decided_at"]) + timedelta(minutes=15)).isoformat()
        return {"child_id": child_id, "session_id": session_id, "events": events, "states": states, "interventions": interventions,
                **({"excluded_history_issues": issues} if issues else {})}

    def baseline(self, child_id: str) -> dict:
        with self._connect() as db:
            rows = [dict(row) for row in db.execute("SELECT * FROM daily_feature WHERE child_id = ? ORDER BY day", (child_id,))]
            coverage = observation_summary(self._current_observations(db, child_id))
            coverage_rows = self._coverage_records(db, child_id)
            if coverage_rows:
                beginning = min(_parse_time(row["occurred_at"]) for row in coverage_rows)
                coverage["measurement"] = coverage_projection(coverage_rows, child_id, beginning, datetime.now(timezone.utc))
                coverage["observed_seconds"] = coverage["measurement"]["observed_seconds"]
                coverage["task_opportunities"] = coverage["measurement"]["task_opportunities"]
                coverage["behavior_observed_seconds"] = coverage["measurement"]["behavior_observed_seconds"]
        coverage_days = coverage.get("measurement", {}).get("observed_days", 0)
        if len(rows) < 3:
            if coverage_days >= 3:
                return {"status": "coverage_only", "days_observed": coverage_days, "minimum_days": 3,
                        "coverage": coverage, "behavior_status": "insufficient_evidence",
                        "interpretation_boundary": "Qualified exposure establishes a coverage baseline, not a confirmed behavior or habit baseline."}
            return {"status": "insufficient_history", "days_observed": len(rows), "minimum_days": 3, "coverage": coverage}
        feature_baselines = {feature: _mean_std([int(row[feature]) for row in rows]) for feature in DAILY_EVENT_FEATURES}
        return {
            "status": "coverage_only" if coverage_days >= 3 else "insufficient_evidence", "days_observed": max(len(rows), coverage_days), "coverage": coverage,
            "switch_count_baseline": feature_baselines["switch_count"],
            "feature_baselines": feature_baselines,
            "habit_trends": {feature: {"direction": "not_yet_clear", "status": "insufficient_evidence"} for feature in DAILY_EVENT_FEATURES},
            "count_trends": {feature: _habit_trend([int(row[feature]) for row in rows]) for feature in DAILY_EVENT_FEATURES},
            "features": rows,
            "interpretation_boundary": "Counts summarize observable local events. They are not emotion, personality, or medical assessments.",
        }

    def behavior_model_summary(self, child_id: str, session_id: str | None, day: str) -> dict:
        """Report-safe candidates and feedback from the one behavior record."""
        with self._connect() as db:
            clause = "oe.child_id = ? AND behavior_instant_day(oe.occurred_at) = ?"
            params: list[object] = [child_id, day]
            if session_id is not None:
                clause += " AND oe.session_id = ?"; params.append(session_id)
            events = db.execute(f"SELECT oe.event_id FROM observable_event oe WHERE {clause}", params).fetchall()
            event_ids = [row["event_id"] for row in events]
            if not event_ids:
                return {"event_count": 0, "active_candidates": [], "intervention_feedback": [], "retracted_hypothesis_count": 0, "withdrawn_intervention_count": 0,
                        "boundary": "Only local observable evidence is summarized; no emotion, personality, medical, image, audio, or transcript inference."}
            placeholders = ",".join("?" for _ in event_ids)
            retracted = db.execute(f"SELECT COUNT(*) AS count FROM behavior_retraction WHERE event_id IN ({placeholders})", event_ids).fetchone()["count"]
            active = db.execute(f"""SELECT ps.event_id, ps.state, ps.probability, ps.occurred_at, oe.source, oe.payload_json
                FROM probabilistic_state ps JOIN observable_event oe ON oe.event_id = ps.event_id
                  AND ps.child_id=oe.child_id AND ps.session_id IS oe.session_id
                LEFT JOIN behavior_retraction br ON br.event_id = ps.event_id
                WHERE ps.event_id IN ({placeholders}) AND br.event_id IS NULL
                  AND ps.state IN ('blocked_candidate','distraction_candidate','fatigue_candidate','frequent_switching')
                ORDER BY ps.occurred_at""", event_ids).fetchall()
            owner = '''EXISTS(SELECT 1 FROM observable_event oe WHERE oe.event_id=intervention.source_event_id
                AND oe.child_id=intervention.child_id AND oe.session_id IS intervention.session_id)'''
            withdrawn = db.execute(f"SELECT COUNT(*) AS count FROM intervention WHERE {owner} AND source_event_id IN ({placeholders}) AND status = 'withdrawn'", event_ids).fetchone()["count"]
            feedback = db.execute(f"""SELECT intervention_id, feedback, feedback_at, source_event_id
                FROM intervention WHERE {owner} AND source_event_id IN ({placeholders}) AND status = 'active'
                  AND feedback IN ('accepted', 'declined') ORDER BY feedback_at""", event_ids).fetchall()
        candidates = []
        for row in active:
            payload = json.loads(row["payload_json"])
            candidates.append({
                "event_ref": row["event_id"], "state": row["state"], "confidence": row["probability"],
                "occurred_at": row["occurred_at"], "source": row["source"],
                "model_version": payload.get("model_version", "not_recorded"), "unconfirmed": True,
            })
        return {"event_count": len(event_ids), "active_candidates": candidates,
                "retracted_hypothesis_count": int(retracted), "withdrawn_intervention_count": int(withdrawn),
                "intervention_feedback": [{
                    "evidence_ref": f"behavior_intervention:{row['intervention_id']}",
                    "source_event_ref": row["source_event_id"], "response": row["feedback"],
                    "occurred_at": row["feedback_at"],
                } for row in feedback],
                "boundary": "Only local observable evidence is summarized; no emotion, personality, medical, image, audio, or transcript inference."}

    # Compatibility for callers shipped before the report-safe contract was named.
    def daily_report_evidence(self, child_id: str, session_id: str | None, day: str) -> dict:
        return self.behavior_model_summary(child_id, session_id, day)

    def report_safe_context(
        self, child_id: str, session_id: str | None = None,
        period_start=None, period_end=None,
    ) -> dict:
        """The sole parent-report contract; it never returns database identifiers."""
        start = str(period_start)[:10] if period_start is not None else None
        end = str(period_end)[:10] if period_end is not None else None
        with self._connect() as db:
            self._refresh_habit_experiments(db, child_id, datetime.now(timezone.utc))
            db.commit()
            clauses = ["oe.child_id = ?", "behavior_instant_day(oe.occurred_at) IS NOT NULL"]
            params: list[object] = [child_id]
            if session_id is not None:
                clauses.append("oe.session_id = ?"); params.append(session_id)
            if start:
                clauses.append("behavior_instant_day(oe.occurred_at) >= ?"); params.append(start)
            if end:
                clauses.append("behavior_instant_day(oe.occurred_at) <= ?"); params.append(end)
            where = " AND ".join(clauses)
            states = db.execute(f"""SELECT ps.event_id, ps.state, ps.probability, ps.occurred_at, oe.source, oe.payload_json
                FROM probabilistic_state ps JOIN observable_event oe ON oe.event_id = ps.event_id
                  AND ps.child_id=oe.child_id AND ps.session_id IS oe.session_id
                LEFT JOIN behavior_retraction br ON br.event_id = ps.event_id
                WHERE {where} AND br.event_id IS NULL
                  AND ps.state IN ('blocked_candidate','distraction_candidate','fatigue_candidate','frequent_switching')
                ORDER BY ps.occurred_at""", params).fetchall()
            retracted = db.execute(f"""SELECT COUNT(*) FROM behavior_retraction br
                JOIN observable_event oe ON oe.event_id = br.event_id AND br.child_id=oe.child_id WHERE {where}""", params).fetchone()[0]
            withdrawn = db.execute(f"""SELECT COUNT(*) FROM intervention i
                JOIN observable_event oe ON oe.event_id = i.source_event_id AND i.child_id=oe.child_id
                  AND i.session_id IS oe.session_id WHERE {where} AND i.status = 'withdrawn'""", params).fetchone()[0]
            feedback_clauses = ["i.child_id = ?", "i.status = 'active'", "i.feedback IN ('accepted','declined')",
                                "behavior_instant_day(i.feedback_at) IS NOT NULL"]
            feedback_params: list[object] = [child_id]
            if session_id is not None:
                feedback_clauses.append("i.session_id = ?"); feedback_params.append(session_id)
            if start:
                feedback_clauses.append("behavior_instant_day(i.feedback_at) >= ?"); feedback_params.append(start)
            if end:
                feedback_clauses.append("behavior_instant_day(i.feedback_at) <= ?"); feedback_params.append(end)
            feedback = db.execute(f"""SELECT i.intervention_id, i.feedback, i.feedback_note, i.feedback_at, oe.source
                FROM intervention i LEFT JOIN observable_event oe ON oe.event_id = i.source_event_id
                  AND i.child_id=oe.child_id AND i.session_id IS oe.session_id
                WHERE {' AND '.join(feedback_clauses)} AND (i.source_event_id IS NULL OR oe.event_id IS NOT NULL)
                ORDER BY i.feedback_at""", feedback_params).fetchall()
            issues = ownership_issues(db, child_id, session_id, start, end)
            habit_rows = db.execute(
                f"""SELECT title,child_action,success_signal,status,started_at,ends_at,outcome_json
                   FROM habit_experiment WHERE child_id=? ORDER BY {REPORT_HABIT_ORDER} LIMIT 3""",
                (child_id,),
            ).fetchall()
        labels = {"blocked_candidate": "卡住候选", "distraction_candidate": "分心候选", "fatigue_candidate": "疲劳候选", "frequent_switching": "频繁切换候选"}
        hypotheses = [{
            "state": labels.get(row["state"], "未确认候选"), "confidence": float(row["probability"]),
            "occurred_at": row["occurred_at"], "source": row["source"],
            "model_version": _model_version_from_payload(row["payload_json"]),
            "evidence_ref": "behavior-hypothesis:" + hashlib.sha256(
                f"{child_id}:{row['event_id']}".encode("utf-8")
            ).hexdigest()[:24], "unconfirmed": True,
        } for row in states]
        items = []
        for row in feedback:
            kind = "intervention_accepted" if row["feedback"] == "accepted" else "intervention_declined"
            summary = "孩子接受了一次温和提醒" if kind == "intervention_accepted" else "孩子暂不接受一次温和提醒"
            digest = hashlib.sha256(f"{child_id}:{row['intervention_id']}".encode("utf-8")).hexdigest()[:24]
            items.append({"kind": kind, "summary": summary, "evidence_ref": f"behavior-feedback:{digest}",
                          "source": row["source"] or "local_behavior", "occurred_at": row["feedback_at"]})
        return {
            "behavior_context_authoritative": True,
            "behavior_intervention_feedback": items,
            "habit_experiments": [
                {
                    "title": row["title"], "child_action": row["child_action"],
                    "success_signal": row["success_signal"], "status": row["status"],
                    "started_at": row["started_at"], "ends_at": row["ends_at"],
                    "outcome": report_habit_outcome(row["outcome_json"]),
                }
                for row in habit_rows
            ],
            "behavior_model_summary": {
                "active_hypotheses": hypotheses,
                "withdrawn_intervention_count": int(withdrawn),
                "retracted_hypothesis_count": int(retracted),
                **({"excluded_history_issues": issues} if issues else {}),
                "uncertainty": "仅汇总本地可观察候选与反馈；不作情绪、人格或能力推断。"
                    + (f"另有{sum(issues.values())}条历史依据归属不一致，已排除并保留原审计待核对。" if issues else ""),
            },
        }

    @staticmethod
    def _session_switch_count(db, child_id, session_id, at):
        end = _parse_time(at)
        rows = db.execute("""SELECT oe.occurred_at FROM observable_event oe
            LEFT JOIN behavior_retraction br ON br.event_id=oe.event_id
            WHERE oe.child_id=? AND oe.session_id IS ? AND oe.event_type='interaction.switch'
              AND oe.occurred_at>=? AND br.event_id IS NULL""",
            (child_id, session_id, (end-timedelta(minutes=5)).isoformat())).fetchall()
        return sum(end-timedelta(minutes=5) <= _parse_time(row['occurred_at']) <= end for row in rows)

    def _states_for(self, db: sqlite3.Connection, event: ObservableEvent) -> list[dict]:
        mapping = {
            "presence.seated": ("seated", .8), "presence.away": ("away_from_seat", .8),
            "work.writing": ("writing", .76), "work.reading": ("reading", .76),
            "help.request": ("help_seeking", .9), "work.stall": ("blocked_candidate", .68),
            "attention.diversion": ("distraction_candidate", .66), "fatigue.candidate": ("fatigue_candidate", .64),
            "task.started": ("task_started", .95), "task.finished": ("task_finished", .95),
            "plan.deviation": ("plan_deviation", .72),
        }
        output: list[dict] = []
        if event.event_type == "interaction.switch":
            count = self._session_switch_count(db, event.child_id, event.session_id, event.occurred_at)
            if count >= 3:
                output.append(("frequent_switching", min(.88, .48 + count * .1)))
        elif event.event_type in mapping:
            output.append(mapping[event.event_type])
        return [{"state": state, "probability": round(probability * event.confidence, 3),
                 "evidence": {"event_type": event.event_type, "source": event.source, "source_refs": event.source_refs, "missing_sources": event.missing_sources},
                 "alternatives": list(event.alternative_explanations) or ["Single-source observation; confirm with the child or another local signal."]}
                for state, probability in output]

    def _refresh_daily_features(self, db: sqlite3.Connection, child_id: str, occurred_at: str) -> None:
        day = _calendar_day(occurred_at)
        if day is None:
            return
        rows = self._daily_feature_rows(db, child_id, day)
        db.execute('DELETE FROM daily_feature WHERE child_id=? AND day=?', (child_id, day))
        self._save_daily_feature_rows(db, rows)
        self._mark_daily_projection_current(db)

    @staticmethod
    def _daily_feature_rows(db, child_id=None, day=None):
        # Coverage samples do not become observable behavior counts. Invalid
        # legacy timestamps cannot supply a calendar bucket or observed day.
        event_types = sorted(OBSERVABLE_EVENT_TYPES)
        clauses = [f"oe.event_type IN ({','.join('?' for _ in event_types)})",
                   "oe.source != 'observation_coverage'", 'br.event_id IS NULL',
                   'behavior_calendar_day(oe.occurred_at) IS NOT NULL']
        params = list(event_types)
        if child_id is not None:
            clauses.append('oe.child_id=?'); params.append(child_id)
        if day is not None:
            clauses.append('behavior_calendar_day(oe.occurred_at)=?'); params.append(day)
        counts = [f"SUM(oe.event_type='{event_type}') AS {feature}"
                  for feature, event_type in DAILY_EVENT_FEATURES.items()]
        counts.append("SUM(oe.missing_sources_json != '[]') AS missing_observation_count")
        return db.execute(f"""SELECT oe.child_id, behavior_calendar_day(oe.occurred_at) AS day,
            {', '.join(counts)} FROM observable_event oe
            LEFT JOIN behavior_retraction br ON br.event_id=oe.event_id
            WHERE {' AND '.join(clauses)} GROUP BY oe.child_id,behavior_calendar_day(oe.occurred_at)
            ORDER BY oe.child_id,day""", params).fetchall()

    @staticmethod
    def _save_daily_feature_rows(db, rows):
        columns = ['child_id', 'day', *DAILY_EVENT_FEATURES, 'missing_observation_count']
        db.executemany(f"INSERT INTO daily_feature({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                       [tuple(row[column] for column in columns) for row in rows])

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS observable_event(event_id TEXT PRIMARY KEY, child_id TEXT NOT NULL, session_id TEXT, occurred_at TEXT NOT NULL, event_type TEXT NOT NULL, source TEXT NOT NULL, confidence REAL NOT NULL, source_refs_json TEXT NOT NULL, missing_sources_json TEXT NOT NULL, payload_json TEXT NOT NULL, alternatives_json TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_behavior_event_child_time ON observable_event(child_id, occurred_at);
            CREATE TABLE IF NOT EXISTS probabilistic_state(state_id TEXT PRIMARY KEY, event_id TEXT NOT NULL REFERENCES observable_event(event_id), child_id TEXT NOT NULL, session_id TEXT, occurred_at TEXT NOT NULL, state TEXT NOT NULL, probability REAL NOT NULL, evidence_json TEXT NOT NULL, alternatives_json TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_behavior_state_child_time ON probabilistic_state(child_id, occurred_at);
            CREATE TABLE IF NOT EXISTS intervention(intervention_id TEXT PRIMARY KEY, child_id TEXT NOT NULL, session_id TEXT, source_event_id TEXT, decided_at TEXT NOT NULL, candidate_state TEXT NOT NULL, message TEXT NOT NULL, feedback TEXT, feedback_at TEXT, feedback_note TEXT, feedback_action_id TEXT, feedback_payload_hash TEXT, outcome_json TEXT, intervention_type TEXT NOT NULL DEFAULT 'default_ask_first', status TEXT NOT NULL DEFAULT 'active', withdrawn_at TEXT, withdrawal_reason TEXT);
            CREATE TABLE IF NOT EXISTS intervention_policy_reset(child_id TEXT PRIMARY KEY, reset_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS behavior_retraction(event_id TEXT PRIMARY KEY REFERENCES observable_event(event_id), child_id TEXT NOT NULL, correction_id TEXT NOT NULL, note TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS behavior_stop_condition(child_id TEXT NOT NULL, session_id TEXT, candidate_state TEXT NOT NULL, source_event_id TEXT NOT NULL, correction_id TEXT NOT NULL, expires_at TEXT NOT NULL, PRIMARY KEY(child_id, session_id, candidate_state, source_event_id));
            CREATE TABLE IF NOT EXISTS daily_feature(child_id TEXT NOT NULL, day TEXT NOT NULL, seated_count INTEGER NOT NULL DEFAULT 0, away_count INTEGER NOT NULL DEFAULT 0, writing_count INTEGER NOT NULL DEFAULT 0, reading_count INTEGER NOT NULL DEFAULT 0, switch_count INTEGER NOT NULL DEFAULT 0, help_count INTEGER NOT NULL DEFAULT 0, stall_count INTEGER NOT NULL DEFAULT 0, distraction_candidate_count INTEGER NOT NULL DEFAULT 0, fatigue_candidate_count INTEGER NOT NULL DEFAULT 0, plan_deviation_count INTEGER NOT NULL DEFAULT 0, child_check_in_count INTEGER NOT NULL DEFAULT 0, missing_observation_count INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(child_id, day));
            CREATE TABLE IF NOT EXISTS habit_experiment(
              experiment_id TEXT PRIMARY KEY,
              child_id TEXT NOT NULL,
              target_state TEXT NOT NULL,
              title TEXT NOT NULL,
              child_action TEXT NOT NULL,
              success_signal TEXT NOT NULL,
              duration_days INTEGER NOT NULL,
              baseline_json TEXT NOT NULL,
              status TEXT NOT NULL CHECK(status IN ('proposed','active','completed','declined','stopped')),
              proposed_at TEXT NOT NULL,
              started_at TEXT,
              ends_at TEXT,
              closed_at TEXT,
              outcome_json TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_habit_one_open
              ON habit_experiment(child_id) WHERE status IN ('proposed','active');
            CREATE TABLE IF NOT EXISTS habit_experiment_event(
              event_id TEXT PRIMARY KEY,
              experiment_id TEXT NOT NULL REFERENCES habit_experiment(experiment_id) ON DELETE CASCADE,
              child_id TEXT NOT NULL,
              event_type TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              occurred_at TEXT NOT NULL,
              client_action_id TEXT NOT NULL UNIQUE
            );
            """)
            from .habit_formation import ensure_habit_formation_schema
            ensure_habit_formation_schema(db)
            existing_columns = {row["name"] for row in db.execute("PRAGMA table_info(daily_feature)")}
            for column in [*DAILY_EVENT_FEATURES, "missing_observation_count"]:
                if column not in existing_columns:
                    db.execute(f"ALTER TABLE daily_feature ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0")
            intervention_columns = {row["name"] for row in db.execute("PRAGMA table_info(intervention)")}
            for column, definition in {
                "intervention_type": "TEXT NOT NULL DEFAULT 'default_ask_first'", "feedback_action_id": "TEXT",
                "feedback_payload_hash": "TEXT", "source_event_id": "TEXT", "status": "TEXT NOT NULL DEFAULT 'active'",
                "withdrawn_at": "TEXT", "withdrawal_reason": "TEXT", "effect_action_id": "TEXT", "effect_payload_hash": "TEXT",
                "support_basis_json": "TEXT",
            }.items():
                if column not in intervention_columns:
                    db.execute(f"ALTER TABLE intervention ADD COLUMN {column} {definition}")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_intervention_feedback_action ON intervention(feedback_action_id) WHERE feedback_action_id IS NOT NULL")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_intervention_source_event ON intervention(source_event_id) WHERE source_event_id IS NOT NULL")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_intervention_effect_action ON intervention(effect_action_id) WHERE effect_action_id IS NOT NULL")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_behavior_retraction_correction ON behavior_retraction(correction_id)")
            db.execute("CREATE TABLE IF NOT EXISTS behavior_legacy_migration(source_path TEXT PRIMARY KEY, completed_at TEXT NOT NULL)")
            self._initialize_daily_projection(db)
            db.commit()

    @staticmethod
    def _initialize_daily_projection(db):
        db.execute('''CREATE TABLE IF NOT EXISTS behavior_daily_projection(
            singleton INTEGER PRIMARY KEY CHECK(singleton=1), version TEXT NOT NULL,
            dirty INTEGER NOT NULL CHECK(dirty IN (0,1)))''')
        if db.execute('SELECT 1 FROM behavior_daily_projection WHERE singleton=1').fetchone() is None:
            db.execute("INSERT INTO behavior_daily_projection VALUES(1,'',1)")
        # Full-row invalidation catches historical edits without relying on
        # count/max timestamps. The marker contains no child data.
        for table in ('observable_event', 'behavior_retraction', 'daily_feature'):
            for action in ('INSERT', 'UPDATE', 'DELETE'):
                condition = ''
                if table == 'observable_event':
                    owners = ('OLD', 'NEW') if action == 'UPDATE' else ('OLD',) if action == 'DELETE' else ('NEW',)
                    condition = 'WHEN ' + ' OR '.join(f"{owner}.source != 'observation_coverage'" for owner in owners)
                elif table == 'behavior_retraction':
                    owners = ('OLD', 'NEW') if action == 'UPDATE' else ('OLD',) if action == 'DELETE' else ('NEW',)
                    condition = 'WHEN ' + ' OR '.join("EXISTS(SELECT 1 FROM observable_event WHERE source != 'observation_coverage' AND event_id=" + owner + '.event_id)' for owner in owners)
                db.execute(f'''CREATE TRIGGER IF NOT EXISTS behavior_daily_dirty_{table}_{action.lower()}
                    AFTER {action} ON {table} {condition}
                    BEGIN UPDATE behavior_daily_projection SET dirty=1 WHERE singleton=1; END''')

    def _migrate_legacy_sidecar(self) -> None:
        """Copy the former behavior sidecar in one idempotent SQLite transaction."""
        legacy = self.legacy_path
        if not legacy or legacy.resolve() == self.path.resolve() or not legacy.is_file():
            return
        source_path = str(legacy.resolve())
        tables = (
            "observable_event", "probabilistic_state", "intervention",
            "intervention_policy_reset", "behavior_retraction",
            "behavior_stop_condition", "habit_experiment",
            "habit_experiment_event",
        )
        with self._connect() as db:
            completed = db.execute(
                "SELECT 1 FROM behavior_legacy_migration WHERE source_path = ?", (source_path,)
            ).fetchone()
            if completed:
                self._archive_legacy_sidecar(legacy)
                return
            db.execute("ATTACH DATABASE ? AS behavior_legacy", (source_path,))
            try:
                available = {
                    row["name"] for row in db.execute(
                        "SELECT name FROM behavior_legacy.sqlite_master WHERE type = 'table'"
                    )
                }
                db.execute("BEGIN IMMEDIATE")
                for table in tables:
                    if table not in available:
                        continue
                    target_info = list(db.execute(f"PRAGMA table_info({table})"))
                    source_columns = {row["name"] for row in db.execute(f"PRAGMA behavior_legacy.table_info({table})")}
                    columns = [row["name"] for row in target_info if row["name"] in source_columns]
                    key_columns = [row["name"] for row in sorted(target_info, key=lambda row: row["pk"]) if row["pk"] and row["name"] in source_columns]
                    if not columns or not key_columns:
                        continue
                    for source_row in db.execute(f"SELECT {', '.join(columns)} FROM behavior_legacy.{table}"):
                        where = " AND ".join(f"{column} = ?" for column in key_columns)
                        key_values = tuple(source_row[column] for column in key_columns)
                        existing = db.execute(f"SELECT {', '.join(columns)} FROM {table} WHERE {where}", key_values).fetchone()
                        if existing:
                            if any(existing[column] != source_row[column] for column in columns):
                                raise ValueError(f"Legacy behavior migration conflict in {table}.")
                            continue
                        db.execute(
                            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                            tuple(source_row[column] for column in columns),
                        )
                # Keep the imported audit intact. Invalid owner links are excluded
                # by readers; record the quarantine boundary in the same commit.
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='audit_event'").fetchone():
                    children = db.execute('''SELECT child_id FROM observable_event UNION
                        SELECT child_id FROM probabilistic_state UNION SELECT child_id FROM intervention''').fetchall()
                    for child in children:
                        issues = ownership_issues(db, child['child_id'])
                        if issues:
                            db.execute('''INSERT INTO audit_event(audit_id,actor,action,child_id,summary_json,occurred_at)
                                VALUES(?,'system','behavior_legacy_owner_links_excluded',?,?,?)''',
                                (str(uuid4()), child['child_id'], json.dumps({'issues': issues, 'originals_preserved': True}),
                                 datetime.now(timezone.utc).isoformat()))
                db.execute(
                    "INSERT INTO behavior_legacy_migration(source_path, completed_at) VALUES (?, ?)",
                    (source_path, datetime.now(timezone.utc).isoformat()),
                )
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.execute("DETACH DATABASE behavior_legacy")
        self._archive_legacy_sidecar(legacy)

    def _archive_legacy_sidecar(self, legacy: Path) -> None:
        """Archive a consumed source; quarantine a later same-path sidecar safely."""
        if not legacy.is_file():
            return
        archive = legacy.with_name(legacy.name + ".migrated")
        if archive.exists():
            digest = _file_sha256(legacy)[:12]
            quarantine = legacy.with_name(f"{legacy.name}.quarantine-{digest}-{uuid4().hex[:8]}")
            legacy.replace(quarantine)
            if self.database is not None:
                with self.database.connect() as db:
                    db.execute(
                        "INSERT INTO audit_event(audit_id, actor, action, child_id, summary_json, occurred_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (str(uuid4()), "system", "behavior_legacy_sidecar_quarantined", None,
                         json.dumps({"quarantine_file": quarantine.name, "reason": "same_path_reappeared_after_migration"}),
                         datetime.now(timezone.utc).isoformat()),
                    )
                    db.commit()
            return
        legacy.replace(archive)

    def purge_legacy_child(self, child_id: str) -> None:
        """Deletion also removes any recoverable legacy copy, preventing revival."""
        if not self.legacy_path:
            return
        candidates = [self.legacy_path, self.legacy_path.with_name(self.legacy_path.name + ".migrated"),
                      *self.legacy_path.parent.glob(self.legacy_path.name + ".quarantine-*")]
        for legacy in candidates:
            if not legacy.is_file():
                continue
            with sqlite3.connect(legacy) as db:
                for table in (
                    "habit_formation_event", "habit_formation_state", "habit_experiment_event",
                    "habit_experiment", "behavior_stop_condition", "intervention",
                    "behavior_retraction", "probabilistic_state", "observable_event",
                    "daily_feature", "intervention_policy_reset",
                ):
                    if db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone():
                        db.execute(f"DELETE FROM {table} WHERE child_id = ?", (child_id,))
                db.commit()

    @contextmanager
    def _connect(self, connection=None) -> Iterator[sqlite3.Connection]:
        if connection is not None:
            if not connection.in_transaction:
                raise RuntimeError("Observation write transaction missing")
            from ..original_connection_read import _state
            _state(connection, self.database)
            connection.create_function('behavior_calendar_day', 1, _calendar_day, deterministic=True)
            connection.create_function('behavior_instant_day', 1, _instant_day, deterministic=True)
            yield connection
            return
        if self.database is not None:
            with self.database.connect() as db:
                db.create_function('behavior_calendar_day', 1, _calendar_day, deterministic=True)
                db.create_function('behavior_instant_day', 1, _instant_day, deterministic=True)
                yield db
            return
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        db.create_function('behavior_calendar_day', 1, _calendar_day, deterministic=True)
        db.create_function('behavior_instant_day', 1, _instant_day, deterministic=True)
        try:
            yield db
        finally:
            db.close()

    @staticmethod
    def _event_dict(row: sqlite3.Row) -> dict:
        item = dict(row)
        for key in ("source_refs_json", "missing_sources_json", "payload_json", "alternatives_json"):
            item[key.removesuffix("_json")] = json.loads(item.pop(key))
        return item

    @staticmethod
    def _state_dict(row: sqlite3.Row) -> dict:
        item = dict(row)
        item["evidence"] = json.loads(item.pop("evidence_json"))
        item["alternatives"] = json.loads(item.pop("alternatives_json"))
        return item

    @staticmethod
    def _intervention_dict(row: sqlite3.Row) -> dict:
        item = dict(row)
        item["outcome"] = json.loads(item.pop("outcome_json")) if item.get("outcome_json") else None
        item["support_basis"] = json.loads(item.pop("support_basis_json")) if item.get("support_basis_json") else None
        return item


def _calendar_day(value: str) -> str | None:
    try:
        return evidence_calendar_date(value).isoformat()
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None


def _instant_day(value: str) -> str | None:
    # A legacy wall date can label a historical count, not qualify an instant.
    try:
        return family_time(_parse_time(value)).date().isoformat()
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("occurred_at must include a timezone")
    return parsed.astimezone(timezone.utc)


def _model_version_from_payload(payload_json: str) -> str:
    """Historical payload damage must not make a parent report unavailable."""
    try:
        payload = json.loads(payload_json)
    except (TypeError, json.JSONDecodeError):
        return "not_recorded"
    return str(payload.get("model_version") or "not_recorded") if isinstance(payload, dict) else "not_recorded"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _decay(probability: float, occurred_at: str, now: datetime) -> float:
    """Evidence fades over an hour; it is never converted to a diagnosis."""
    age_minutes = max(0.0, (now - _parse_time(occurred_at)).total_seconds() / 60)
    return round(float(probability) * (.5 ** (age_minutes / 120)), 3)


def _mean_std(values: list[int]) -> dict:
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    return {"mean": round(mean, 2), "stddev": round(variance ** .5, 2)}


def _habit_trend(values: list[int]) -> dict:
    if len(values) < 6:
        return {"status": "insufficient_history", "minimum_days": 6}
    midpoint = len(values) // 2
    prior = sum(values[:midpoint]) / midpoint
    recent = sum(values[midpoint:]) / (len(values) - midpoint)
    delta = recent - prior
    return {"status": "count_summary", "prior_daily_mean": round(prior, 2), "recent_daily_mean": round(recent, 2), "delta": round(delta, 2), "direction": "increasing" if delta > .5 else "decreasing" if delta < -.5 else "stable"}
