from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
from typing import Any
from uuid import uuid4

from .database import Database
from .exam_blueprints import list_exam_blueprints, resolve_exam_blueprint
from .learning_state import LearningStateService
from .specialized_program import SpecializedProgramService

EXAM_LISTENING_BLOCKER = 'EXAM_LISTENING_PLAYBACK_SCOPE_UNSUPPORTED'


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ExamSimulationError(ValueError):
    pass


class _ExamConnection:
    def __init__(self, connection):
        self.connection = connection

    @property
    def in_transaction(self):
        return self.connection.in_transaction

    @property
    def local_media_runtime(self):
        return getattr(self.connection, 'local_media_runtime', None)

    def execute(self, sql, parameters=()):
        if sql.strip().upper() == "BEGIN IMMEDIATE":
            if not self.connection.in_transaction:
                raise RuntimeError("exam scoring transaction missing")
            return None
        return self.connection.execute(sql, parameters)

    def commit(self):
        pass


class _ExamDatabase:
    def __init__(self, connection):
        self.connection = connection

    @contextmanager
    def connect(self):
        from .original_connection_read import original_connection_view
        view=_ExamConnection(self.connection)
        with original_connection_view(view,self.connection):yield view


class ExamSimulationService:
    """Durable voice-led assessment papers assembled from approved diagnostics."""

    def __init__(self, database: Database):
        self.database = database
        self.learning = LearningStateService(database)
        self.specialized = SpecializedProgramService(database)

    def ensure_schema(self) -> None:
        from .diagnostic_prompt import DiagnosticPromptService
        DiagnosticPromptService(self.database)
        with self.database.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS specialized_exam_run (
                  run_id TEXT PRIMARY KEY,
                  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                  session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
                  track TEXT NOT NULL,
                  subject TEXT NOT NULL,
                  mode TEXT NOT NULL CHECK(mode IN ('practice','full')),
                  assessment_scope TEXT NOT NULL DEFAULT 'practice',
                  status TEXT NOT NULL CHECK(status IN ('active','paused','completed','timed_out','cancelled')),
                  blueprint_id TEXT,
                  blueprint_version TEXT,
                  blueprint_snapshot_json TEXT,
                  total_items INTEGER NOT NULL,
                  current_position INTEGER NOT NULL DEFAULT 1,
                  time_limit_seconds INTEGER NOT NULL,
                  elapsed_seconds INTEGER NOT NULL DEFAULT 0,
                  active_since TEXT,
                  started_at TEXT NOT NULL,
                  completed_at TEXT,
                  result_json TEXT,
                  client_action_id TEXT NOT NULL UNIQUE
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_specialized_exam_active_session
                  ON specialized_exam_run(session_id) WHERE status IN ('active','paused');
                CREATE TABLE IF NOT EXISTS specialized_exam_item (
                  run_id TEXT NOT NULL REFERENCES specialized_exam_run(run_id) ON DELETE CASCADE,
                  position INTEGER NOT NULL,
                  diagnostic_item_id TEXT NOT NULL REFERENCES diagnostic_item(diagnostic_item_id),
                  domain_id TEXT NOT NULL,
                  section_id TEXT NOT NULL DEFAULT 'practice',
                  attempt_id TEXT REFERENCES diagnostic_attempt(attempt_id) ON DELETE SET NULL,
                  correctness REAL,
                  answered_at TEXT,
                  PRIMARY KEY(run_id, position),
                  UNIQUE(run_id, diagnostic_item_id)
                );
                CREATE TABLE IF NOT EXISTS specialized_exam_section_state (
                  run_id TEXT NOT NULL REFERENCES specialized_exam_run(run_id) ON DELETE CASCADE,
                  section_id TEXT NOT NULL,
                  position INTEGER NOT NULL,
                  title TEXT NOT NULL,
                  total_items INTEGER NOT NULL,
                  time_limit_seconds INTEGER NOT NULL,
                  elapsed_seconds INTEGER NOT NULL DEFAULT 0,
                  active_since TEXT,
                  status TEXT NOT NULL CHECK(status IN ('pending','active','paused','completed','timed_out','cancelled')),
                  PRIMARY KEY(run_id, section_id),
                  UNIQUE(run_id, position)
                );
                CREATE INDEX IF NOT EXISTS idx_specialized_exam_child
                  ON specialized_exam_run(child_id, started_at DESC);
                """
            )
            connection.execute("BEGIN IMMEDIATE")
            run_columns = {
                row["name"] for row in connection.execute(
                    "PRAGMA table_info(specialized_exam_run)"
                ).fetchall()
            }
            for column, definition in {
                "assessment_scope": "TEXT NOT NULL DEFAULT 'practice'",
                "blueprint_id": "TEXT",
                "blueprint_version": "TEXT",
                "blueprint_snapshot_json": "TEXT",
                "delivery_phase": "TEXT NOT NULL DEFAULT 'ordinary'",
                "prepared_at": "TEXT",
                "ready_at": "TEXT",
                "control_revision": "INTEGER NOT NULL DEFAULT 1",
                "activated_at": "TEXT",
                "ready_version": "TEXT",
            }.items():
                if column not in run_columns:
                    connection.execute(
                        f"ALTER TABLE specialized_exam_run ADD COLUMN {column} {definition}"
                    )
            item_columns = {
                row["name"] for row in connection.execute(
                    "PRAGMA table_info(specialized_exam_item)"
                ).fetchall()
            }
            if "section_id" not in item_columns:
                connection.execute(
                    "ALTER TABLE specialized_exam_item ADD COLUMN section_id TEXT NOT NULL DEFAULT 'practice'"
                )
            for column in ("answer_action_id", "answer_hash", "source_version", "bound_source_version", "answer_text", "authorized_turn_id", "answer_arrived_at"):
                if column not in item_columns:
                    connection.execute(f"ALTER TABLE specialized_exam_item ADD COLUMN {column} TEXT")
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_exam_answer_action "
                "ON specialized_exam_item(answer_action_id) WHERE answer_action_id IS NOT NULL"
            )
            self._backfill_section_states(connection)
            from .exam_listening_delivery import ensure_schema as ensure_delivery_schema
            ensure_delivery_schema(connection)
            connection.commit()

    @staticmethod
    def _backfill_section_states(connection: Any) -> None:
        """Make pre-section-timer runs resumable without rewriting their answers."""
        runs = connection.execute(
            """SELECT run.* FROM specialized_exam_run run
               WHERE NOT EXISTS (
                 SELECT 1 FROM specialized_exam_section_state state
                 WHERE state.run_id=run.run_id
               )"""
        ).fetchall()
        for raw_run in runs:
            run = dict(raw_run)
            groups = connection.execute(
                """SELECT section_id,MIN(position) AS first_position,MAX(position) AS last_position,
                          COUNT(*) AS total_items
                   FROM specialized_exam_item WHERE run_id=?
                   GROUP BY section_id ORDER BY first_position""",
                (run["run_id"],),
            ).fetchall()
            if not groups:
                continue
            blueprint = (
                json.loads(run["blueprint_snapshot_json"])
                if run.get("blueprint_snapshot_json") else None
            )
            metadata = {
                item["section_id"]: item for item in (blueprint or {}).get("sections", [])
            }
            current_position = int(run["current_position"])
            for index, raw_group in enumerate(groups, 1):
                group = dict(raw_group)
                section_id = group["section_id"]
                section = metadata.get(section_id, {})
                if run["status"] in {"completed", "timed_out", "cancelled"}:
                    section_status = run["status"]
                elif current_position > int(group["last_position"]):
                    section_status = "completed"
                elif current_position < int(group["first_position"]):
                    section_status = "pending"
                else:
                    section_status = run["status"]
                duration = int(section.get("duration_minutes") or 0) * 60
                if not duration:
                    duration = int(run["time_limit_seconds"])
                is_current = section_status in {"active", "paused"}
                elapsed = min(int(run["elapsed_seconds"] or 0), duration) if is_current else 0
                if section_status in {"completed", "timed_out"}:
                    elapsed = duration if section_status == "timed_out" else 0
                connection.execute(
                    """INSERT INTO specialized_exam_section_state(
                         run_id,section_id,position,title,total_items,time_limit_seconds,
                         elapsed_seconds,active_since,status
                       ) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        run["run_id"], section_id, index,
                        section.get("title") or section_id,
                        int(group["total_items"]), duration, elapsed,
                        run["active_since"] if section_status == "active" else None,
                        section_status,
                    ),
                )

    @staticmethod
    def blueprints(track: str | None = None) -> list[dict[str, Any]]:
        return list_exam_blueprints(track)

    def start(
        self, session_id: str, track: str, subject: str, mode: str,
        requested_items: int, time_limit_minutes: int, client_action_id: str,
        blueprint_id: str | None = None,
    ) -> dict[str, Any]:
        self.ensure_schema()
        if mode not in {"practice", "coverage", "full"}:
            raise ExamSimulationError("模拟卷模式无效。")
        if not 2 <= requested_items <= 100 or not 5 <= time_limit_minutes <= 240:
            raise ExamSimulationError("题量或时长超出允许范围。")
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            session = self._active_session(connection, session_id)
            replay = connection.execute(
                "SELECT * FROM specialized_exam_run WHERE client_action_id=?", (client_action_id,)
            ).fetchone()
            if replay:
                if (replay["session_id"], replay["track"], replay["subject"], replay["assessment_scope"] or replay["mode"]) != (session_id, track, subject, mode):
                    raise ExamSimulationError("组卷动作已用于其他会话或卷型。")
                if replay["child_id"] != session["child_id"]:
                    raise ExamSimulationError("EXAM_SESSION_CONFLICT")
                connection.commit()
                return self.status(replay["run_id"])
            if track not in self.specialized.tracks_for_child(session["child_id"]):
                raise ExamSimulationError("该专项不适用于当前孩子。")
            catalog_track = "school" if track == "junior_exam" else track
            rows = connection.execute(
                """
                SELECT di.diagnostic_item_id,di.difficulty,kn.knowledge_node_id,
                       kn.subject,kn.title,COUNT(da.attempt_id) AS prior_attempts
                FROM diagnostic_item di
                JOIN knowledge_node kn ON kn.knowledge_node_id=di.knowledge_node_id
                LEFT JOIN diagnostic_attempt da ON da.diagnostic_item_id=di.diagnostic_item_id
                  AND da.child_id=?
                WHERE di.status='active' AND kn.status='active' AND kn.track=?
                  AND (?='' OR kn.subject=?)
                  AND (kn.track!='school' OR kn.grade_band=?)
                GROUP BY di.diagnostic_item_id,di.difficulty,kn.knowledge_node_id,kn.subject,kn.title
                ORDER BY prior_attempts,di.difficulty,di.diagnostic_item_id
                """,
                (session["child_id"], catalog_track, subject, subject, session["grade"]),
            ).fetchall()
            by_domain: dict[str, list[dict[str, Any]]] = {}
            unverified_listening_domains = set()
            for row in rows:
                if not LearningStateService._eligible_diagnostic(connection, session["child_id"], row["diagnostic_item_id"]):
                    continue
                domain_id = self.specialized._mastery_domain(track, row)
                if domain_id:
                    if self._listening_item(connection,row['diagnostic_item_id']):
                        from .pet_listening_requirement import listening_requirement
                        if listening_requirement(connection,session['child_id'],row['diagnostic_item_id'])['status']!='known':
                            unverified_listening_domains.add(domain_id)
                            continue
                    by_domain.setdefault(domain_id, []).append(dict(row))
            if mode in {'coverage','full'} and unverified_listening_domains - set(by_domain):
                raise ExamSimulationError('EXAM_LISTENING_ORIGINAL_INSTRUCTION_REQUIRED')
            required_domains = self.specialized._domain_ids(track, subject)
            missing_domains = sorted(required_domains - set(by_domain))
            if mode in {"coverage", "full"} and missing_domains:
                raise ExamSimulationError(
                    "整卷能力域还没覆盖：" + "、".join(missing_domains[:8])
                )
            blueprint = None
            if mode == "full":
                try:
                    blueprint = resolve_exam_blueprint(track, subject, blueprint_id)
                except KeyError:
                    if not subject:
                        raise ExamSimulationError("蓝图整卷必须先说具体学科。")
                    raise ExamSimulationError("没有找到与专项和学科匹配的考试蓝图。")
                selected = self._select_blueprint_items(
                    blueprint, by_domain, required_domains, requested_items
                )
                time_limit_minutes = sum(
                    int(section["duration_minutes"]) for section in blueprint["sections"]
                )
            else:
                target_items = (
                    max(requested_items, len(required_domains))
                    if mode == "coverage" else requested_items
                )
                selected = [
                    ("domain_coverage" if mode == "coverage" else "practice", domain_id, item)
                    for domain_id, item in self._balanced_items(by_domain, target_items)
                ]
            from .exam_listening_delivery import complete_group_selection
            selected = complete_group_selection(connection,session['child_id'],selected,by_domain,full=mode=='full')
            if len(selected) < 2:
                raise ExamSimulationError("经家长审核的诊断题不足，暂时无法组卷。")
            listening = any(self._listening_item(connection,item['diagnostic_item_id']) for _,_,item in selected)
            run_id, now = str(uuid4()), _now()
            storage_mode = "full" if mode == "full" else "practice"
            connection.execute(
                """INSERT INTO specialized_exam_run(
                     run_id,child_id,session_id,track,subject,mode,assessment_scope,status,
                     blueprint_id,blueprint_version,blueprint_snapshot_json,total_items,
                     current_position,time_limit_seconds,elapsed_seconds,active_since,
                     started_at,client_action_id
                   ) VALUES(?,?,?,?,?,?,?,'active',?,?,?,?,1,?,0,?,?,?)""",
                (
                    run_id, session["child_id"], session_id, track, subject, storage_mode, mode,
                    blueprint["blueprint_id"] if blueprint else None,
                    blueprint["version"] if blueprint else None,
                    json.dumps(blueprint, ensure_ascii=False, sort_keys=True) if blueprint else None,
                    len(selected), time_limit_minutes * 60, now, now, client_action_id,
                ),
            )
            for position, (section_id, domain_id, item) in enumerate(selected, 1):
                connection.execute(
                    """INSERT INTO specialized_exam_item(
                         run_id,position,diagnostic_item_id,domain_id,section_id
                       ) VALUES(?,?,?,?,?)""",
                    (run_id, position, item["diagnostic_item_id"], domain_id, section_id),
                )
                run_row = connection.execute("SELECT * FROM specialized_exam_run WHERE run_id=?", (run_id,)).fetchone()
                slot = connection.execute("SELECT * FROM specialized_exam_item WHERE run_id=? AND position=?",
                                          (run_id, position)).fetchone()
                connection.execute("UPDATE specialized_exam_item SET source_version=? WHERE run_id=? AND position=?",
                                   (self._source_version(connection, run_row, slot), run_id, position))
            section_specs = (
                blueprint["sections"] if blueprint else [{
                    "section_id": "domain_coverage" if mode == "coverage" else "practice",
                    "title": "能力域覆盖" if mode == "coverage" else "日常练习",
                    "duration_minutes": time_limit_minutes,
                }]
            )
            selected_counts: dict[str, int] = {}
            for section_id, _, _ in selected:
                selected_counts[section_id] = selected_counts.get(section_id, 0) + 1
            for section_position, section in enumerate(section_specs, 1):
                section_id = section["section_id"]
                if not selected_counts.get(section_id):
                    continue
                is_first = not connection.execute(
                    "SELECT 1 FROM specialized_exam_section_state WHERE run_id=?",
                    (run_id,),
                ).fetchone()
                connection.execute(
                    """INSERT INTO specialized_exam_section_state(
                         run_id,section_id,position,title,total_items,time_limit_seconds,
                         elapsed_seconds,active_since,status
                       ) VALUES(?,?,?,?,?,?,0,?,?)""",
                    (
                        run_id, section_id, section_position, section["title"],
                        selected_counts[section_id], int(section["duration_minutes"]) * 60,
                        now if is_first else None, "active" if is_first else "pending",
                    ),
                )
            if listening:
                from .exam_listening_delivery import prepare
                connection.execute("""UPDATE specialized_exam_run SET status='paused',active_since=NULL,
                    delivery_phase='preparing',prepared_at=? WHERE run_id=?""",(now,run_id))
                connection.execute("""UPDATE specialized_exam_section_state SET status='paused',active_since=NULL
                    WHERE run_id=? AND status='active'""",(run_id,))
                prepare(connection,connection.execute('SELECT * FROM specialized_exam_run WHERE run_id=?',(run_id,)).fetchone())
            connection.execute(
                """INSERT INTO audit_event(audit_id,actor,action,child_id,summary_json,occurred_at)
                   VALUES(?,'agent','specialized_exam_started',?,?,?)""",
                (str(uuid4()), session["child_id"], json.dumps({"run_id": run_id, "track": track, "subject": subject, "mode": mode, "items": len(selected), "blueprint_id": blueprint["blueprint_id"] if blueprint else None}, ensure_ascii=False), now),
            )
            connection.commit()
        return self.status(run_id)

    @staticmethod
    def _active_session(c, session_id):
        row = c.execute("""SELECT ds.*,cp.grade,cp.status AS child_status FROM daily_session ds
            JOIN child_profile cp ON cp.child_id=ds.child_id WHERE ds.session_id=?""", (session_id,)).fetchone()
        if (not row or row["status"] != "active" or row["child_status"] != "active"
                or row["state"] in {"evolving", "completed"}):
            raise ExamSimulationError("EXAM_SESSION_INACTIVE")
        return row

    @staticmethod
    def _owned_run(c, session, run_id):
        row = c.execute("SELECT * FROM specialized_exam_run WHERE run_id=?", (run_id,)).fetchone()
        if not row or (row["session_id"], row["child_id"]) != (session["session_id"], session["child_id"]):
            raise ExamSimulationError("EXAM_SESSION_CONFLICT")
        return row

    @staticmethod
    def _source_version(c, run, slot):
        if not LearningStateService._eligible_diagnostic(c, run["child_id"], slot["diagnostic_item_id"]):
            return None
        item = c.execute("""SELECT di.*,kn.track,kn.subject,kn.title,kn.grade_band,kn.knowledge_version,
            kn.source_ref AS node_source_ref FROM diagnostic_item di JOIN knowledge_node kn USING(knowledge_node_id)
            WHERE di.diagnostic_item_id=?""", (slot["diagnostic_item_id"],)).fetchone()
        if not item or not item["prompt"].strip():
            return None
        source = {"item": dict(item), "run": {k: run[k] for k in
            ("run_id", "child_id", "session_id", "track", "subject", "blueprint_id", "blueprint_version", "blueprint_snapshot_json")},
            "slot": {k: slot[k] for k in ("position", "diagnostic_item_id", "domain_id", "section_id")}}
        return "exam_source_v1:" + sha256(json.dumps(source, sort_keys=True, ensure_ascii=True,
                                                    separators=(",", ":")).encode()).hexdigest()

    @staticmethod
    def _listening_item(c, item_id):
        from .listening_modality import listening_node
        row = c.execute('''SELECT kn.track,kn.code,kn.subject,kn.grade_band,di.answer_type
            FROM diagnostic_item di JOIN knowledge_node kn USING(knowledge_node_id)
            WHERE di.diagnostic_item_id=?''',(item_id,)).fetchone()
        return bool(row and listening_node(row,row['answer_type']))

    @staticmethod
    def _open_slot(c, run, slot):
        if ExamSimulationService._listening_item(c,slot['diagnostic_item_id']):
            if run['delivery_phase'] != 'active' or not c.execute(
                    'SELECT 1 FROM exam_listening_delivery_slot WHERE run_id=? AND position=?',
                    (run['run_id'],slot['position'])).fetchone():
                raise ExamSimulationError(EXAM_LISTENING_BLOCKER)
        if run["status"] != "active" or run["current_position"] != slot["position"] or slot["attempt_id"]:
            raise ExamSimulationError("EXAM_SLOT_NOT_ACTIVE")
        section = c.execute("SELECT * FROM specialized_exam_section_state WHERE run_id=? AND section_id=?",
                            (run["run_id"], slot["section_id"])).fetchone()
        if (not section or section["status"] != "active"
                or ExamSimulationService._section_elapsed(dict(section)) >= section["time_limit_seconds"]
                or ExamSimulationService._elapsed(dict(run)) >= run["time_limit_seconds"]):
            raise ExamSimulationError("EXAM_SLOT_EXPIRED_OR_PAUSED")

    def arrival_binding(self, session_id: str, *, run_id: str | None = None) -> dict[str, Any] | None:
        """Read-only receipt-time binding; never apply timeouts, reserve, or migrate.

        Legacy slots without a saved source version return None. The caller must
        retain these exact four fields through planning and execution, never
        replace them using active_for_session after the model returns.
        """
        with self.database.connect() as c:
            c.execute("BEGIN")
            columns = {r["name"] for r in c.execute("PRAGMA table_info(specialized_exam_item)")}
            if "source_version" not in columns:
                return None
            try:
                session = self._active_session(c, session_id)
                if run_id is None:
                    current = c.execute("SELECT run_id FROM specialized_exam_run WHERE session_id=? AND status='active'",
                                        (session_id,)).fetchone()
                    if not current:
                        return None
                    run_id = current["run_id"]
                run = self._owned_run(c, session, run_id)
                slot = c.execute("SELECT * FROM specialized_exam_item WHERE run_id=? AND position=?",
                                 (run_id, run["current_position"])).fetchone()
                if not slot or not slot["source_version"]:
                    return None
                self._open_slot(c, run, slot)
                if self._source_version(c, run, slot) != slot["source_version"]:
                    return None
            except ExamSimulationError:
                return None
            return {"run_id": run_id, "position": slot["position"], "item_id": slot["diagnostic_item_id"],
                    "source_version": slot["source_version"]}

    @staticmethod
    def validate_capture_reference(connection, *, session_id, child_id, binding):
        """Validate a displayed slot without advancing clocks, runs or answers."""
        if (not isinstance(binding, dict) or set(binding) != {'run_id', 'position', 'item_id', 'source_version'}
                or type(binding['position']) is not int or binding['position'] < 1):
            raise ExamSimulationError('EXAM_CAPTURE_BINDING_INVALID')
        session = ExamSimulationService._active_session(connection, session_id)
        if session['child_id'] != child_id:
            raise ExamSimulationError('EXAM_SESSION_CONFLICT')
        run = ExamSimulationService._owned_run(connection, session, binding['run_id'])
        slot = connection.execute('SELECT * FROM specialized_exam_item WHERE run_id=? AND position=?',
                                  (binding['run_id'], binding['position'])).fetchone()
        if (not slot or slot['diagnostic_item_id'] != binding['item_id'] or not slot['source_version']
                or slot['source_version'] != binding['source_version']
                or ExamSimulationService._source_version(connection, run, slot) != slot['source_version']):
            raise ExamSimulationError('EXAM_SOURCE_VERSION_CONFLICT')
        ExamSimulationService._open_slot(connection, run, slot)
        return dict(binding)

    @staticmethod
    def _score_action(run_id, position):
        return f"exam-bound-answer:{run_id}:{position}"

    def _bound_slot(self, c, session_id, binding, answer, action):
        session = self._active_session(c, session_id)
        run = self._owned_run(c, session, binding["run_id"])
        slot = c.execute("SELECT * FROM specialized_exam_item WHERE run_id=? AND position=?",
                         (run["run_id"], binding["position"])).fetchone()
        if not slot or slot["diagnostic_item_id"] != binding["item_id"]:
            raise ExamSimulationError("EXAM_SLOT_BINDING_CONFLICT")
        if (not slot["source_version"] or binding["source_version"] != slot["source_version"]
                or self._source_version(c, run, slot) != slot["source_version"]):
            raise ExamSimulationError("EXAM_SOURCE_VERSION_CONFLICT")
        receipt = c.execute("SELECT run_id,position FROM specialized_exam_item WHERE answer_action_id=?", (action,)).fetchone()
        if receipt and (receipt["run_id"], receipt["position"]) != (run["run_id"], slot["position"]):
            raise ExamSimulationError("EXAM_ANSWER_ACTION_CONFLICT")
        if c.execute("SELECT 1 FROM diagnostic_attempt WHERE client_action_id=?", (action,)).fetchone():
            raise ExamSimulationError("EXAM_ANSWER_ACTION_CONFLICT")
        if slot["answer_action_id"]:
            if (slot["answer_action_id"] != action or slot["answer_hash"] != sha256(answer.encode()).hexdigest()
                    or slot["answer_text"] != answer or slot["bound_source_version"] != binding["source_version"]):
                raise ExamSimulationError("EXAM_RESERVATION_CONFLICT")
        elif slot["attempt_id"]:
            raise ExamSimulationError("EXAM_UNBOUND_RECEIPT")
        if not slot["attempt_id"]:
            self._open_slot(c, run, slot)
        return run, slot

    @staticmethod
    def _validate_scoring_permission(c, *, exam_slot, session_id, item_id, answer,
                                     client_action_id, review_task_id, hint_level, existing):
        """Internal learning_state admission: permission comes from saved reservation."""
        if (not isinstance(exam_slot, tuple) or len(exam_slot) != 2 or not isinstance(exam_slot[0], str)
                or type(exam_slot[1]) is not int or exam_slot[1] < 1 or review_task_id is not None or hint_level != 0):
            raise ExamSimulationError("EXAM_TRUSTED_BINDING_INVALID")
        if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='specialized_exam_item'").fetchone():
            raise ExamSimulationError("EXAM_RESERVATION_MISSING")
        session = ExamSimulationService._active_session(c, session_id)
        run = ExamSimulationService._owned_run(c, session, exam_slot[0])
        slot = c.execute("SELECT * FROM specialized_exam_item WHERE run_id=? AND position=?", exam_slot).fetchone()
        if (not slot or "bound_source_version" not in slot.keys() or not slot["bound_source_version"]
                or not slot["answer_action_id"] or slot["answer_text"] != answer
                or slot["answer_hash"] != sha256(answer.encode()).hexdigest()
                or item_id != slot["diagnostic_item_id"]
                or client_action_id != ExamSimulationService._score_action(*exam_slot)):
            raise ExamSimulationError("EXAM_RESERVATION_CONFLICT")
        if (slot["bound_source_version"] != slot["source_version"]
                or ExamSimulationService._source_version(c, run, slot) != slot["source_version"]):
            raise ExamSimulationError("EXAM_SOURCE_VERSION_CONFLICT")
        if existing:
            if (slot["attempt_id"] != existing["attempt_id"] or existing["child_id"] != run["child_id"]
                    or existing["session_id"] != session_id or existing["diagnostic_item_id"] != item_id
                    or existing["submitted_answer"] != answer):
                raise ExamSimulationError("EXAM_RECEIPT_CONFLICT")
        else:
            ExamSimulationService._open_slot(c, run, slot)
            from .agent_runtime import AgentRuntimeStore
            AgentRuntimeStore.validate_local_answer_write(c, slot['authorized_turn_id'], 'exam.submit',
                child_id=run['child_id'], session_id=session_id,
                binding={'run_id': run['run_id'], 'position': slot['position'], 'item_id': item_id,
                         'source_version': slot['source_version']}, answer=answer)

    def submit_bound(self, session_id: str, *, run_id: str, position: int, item_id: str,
                     source_version: str, answer: str, client_action_id: str,
                     authorized_turn_id: str | None = None) -> dict[str, Any]:
        """Save original words, then atomically score and attach to that exact slot.

        Replays return the original run/attempt, even if a newer run is active.
        A crash before scoring keeps the reservation; retry identical fields and
        words after explicitly resuming a paused run. No second grading store.
        """
        if (type(position) is not int or position < 1
                or any(not isinstance(v, str) or not v.strip() for v in
                       (session_id, run_id, item_id, source_version, answer, client_action_id))
                or len(answer) > 16000 or len(client_action_id) > 200
                or client_action_id.startswith(("exam-bound-answer:", "diagnostic-prompt-answer:"))):
            raise ExamSimulationError("EXAM_INVALID_BOUND_INPUT")
        self.ensure_schema()
        binding = dict(run_id=run_id, position=position, item_id=item_id, source_version=source_version)
        with self.database.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            owned_run, slot = self._bound_slot(c, session_id, binding, answer, client_action_id)
            if not slot["answer_action_id"]:
                from .agent_runtime import AgentRuntimeStore
                run = c.execute('SELECT child_id FROM specialized_exam_run WHERE run_id=?', (run_id,)).fetchone()
                AgentRuntimeStore.validate_local_answer_write(c, authorized_turn_id, 'exam.submit',
                    child_id=run['child_id'], session_id=session_id, binding=binding, answer=answer)
                if self._listening_item(c,item_id):
                    from .exam_listening_delivery import mirror_answer
                    mirror_answer(c,owned_run,slot,answer,client_action_id,authorized_turn_id)
                c.execute("""UPDATE specialized_exam_item SET answer_action_id=?,answer_hash=?,
                    answer_text=?,bound_source_version=?,authorized_turn_id=? WHERE run_id=? AND position=?""",
                    (client_action_id, sha256(answer.encode()).hexdigest(), answer, source_version, authorized_turn_id, run_id, position))
            c.commit()
        return self._submit_reserved(session_id, binding, answer, client_action_id)

    def _submit_reserved(self, session_id, binding, answer, action):
        from .diagnostic_prompt import DiagnosticPromptService
        from .learning_rubric import RubricEvidenceService
        DiagnosticPromptService(self.database)
        RubricEvidenceService(self.database).initialize()
        with self.database.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            run, slot = self._bound_slot(c, session_id, binding, answer, action)
            if not slot['attempt_id']:
                from .agent_runtime import AgentRuntimeStore
                AgentRuntimeStore.validate_local_answer_write(c, slot['authorized_turn_id'], 'exam.submit',
                    child_id=run['child_id'], session_id=session_id, binding=binding, answer=answer)
            if self._listening_item(c,slot['diagnostic_item_id']):
                from .exam_listening_delivery import scoring_scope
                scoring_scope(c,run,slot,answer=answer)
            attempt = LearningStateService(_ExamDatabase(c)).submit_diagnostic(
                session_id, slot["diagnostic_item_id"], answer, self._score_action(run["run_id"], slot["position"]),
                _trusted_exam_slot=(run["run_id"], slot["position"]))
            if self._listening_item(c,slot['diagnostic_item_id']):
                from .exam_listening_delivery import attach_attempt
                attach_attempt(c,run,slot,attempt)
                evidence = c.execute('SELECT * FROM learning_evidence WHERE evidence_id=?',(attempt['evidence_id'],)).fetchone()
                LearningStateService._update_mastery(c,run['child_id'],evidence['knowledge_node_id'],
                    evidence['evidence_id'],evidence['correctness'],evidence['hint_level'],
                    bool(evidence['independent']),evidence['confidence'],_now())
                attempt = LearningStateService._attempt(c,attempt['attempt_id'])
            if not slot["attempt_id"]:
                self._advance_bound(c, run, slot, attempt)
            self._bind_open_review(c, run, slot, attempt)
            c.commit()
        refreshed = self.status(run["run_id"])
        if refreshed["status"] == "completed":
            self._finalize(run["run_id"], "completed")
            refreshed = self.status(run["run_id"])
        return {"attempt": attempt, "exam": refreshed}

    @staticmethod
    def _review_binding(run, slot):
        return dict(run_id=run['run_id'], position=slot['position'],
                    item_id=slot['diagnostic_item_id'], source_version=slot['source_version'])

    @staticmethod
    def _review_prompt_id(run, slot):
        return f"exam-prompt:{run['run_id']}:{slot['position']}"

    def _bind_open_review(self, c, run, slot, attempt):
        """Attach the saved original to its real exam step; never create a learning turn."""
        from .diagnostic_prompt import DiagnosticPromptService, _ITEM_FIELDS, _version
        from .diagnostic_history_identity import snapshot
        from .learning_rubric import RubricEvidenceService
        item = DiagnosticPromptService._item(c, run['child_id'], slot['diagnostic_item_id'])
        if not item or item['answer_type'] != 'open_response':
            return
        binding = self._review_binding(run, slot)
        prompt_id = self._review_prompt_id(run, slot)
        existing = c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=?', (prompt_id,)).fetchone()
        if existing:
            if (existing['child_id'], existing['origin_session_id'], existing['diagnostic_item_id'], existing['attempt_id'],
                    existing['item_version'], existing['request_json']) != (run['child_id'], run['session_id'],
                    slot['diagnostic_item_id'], attempt['attempt_id'], _version(item), json.dumps({'exam_slot': binding}, sort_keys=True)):
                raise ExamSimulationError('EXAM_REVIEW_BINDING_CONFLICT')
        else:
            # This is a retrospective answered binding. Its creation time is
            # honest; exam presentation/capture chronology comes from the run.
            now = _now()
            c.execute('''INSERT INTO diagnostic_prompt(prompt_id,child_id,origin_session_id,diagnostic_item_id,
                item_version,item_json,request_json,prepare_action_id,status,answer_action_id,attempt_id,
                created_at,updated_at,identity_snapshot_json,authorized_turn_id,owner_kind)
                VALUES(?,?,?,?,?,?,?,?,'answered',?,?,?,?,?,?,'exam')''',
                (prompt_id,run['child_id'],run['session_id'],slot['diagnostic_item_id'],_version(item),
                 json.dumps({key:item[key] for key in _ITEM_FIELDS},ensure_ascii=False),
                 json.dumps({'exam_slot':binding},sort_keys=True), 'exam-review-prepare:'+attempt['attempt_id'],
                 'exam-review-answer:'+attempt['attempt_id'],attempt['attempt_id'],now,now,
                 snapshot(c,child_id=run['child_id'],session_id=run['session_id'],prompt_id=prompt_id,item=item),
                 slot['authorized_turn_id']))
        origin = c.execute('''SELECT t.*,p.objective_id,s.step_id,s.arguments_json,ao.success_criteria_json
            FROM agent_turn t JOIN agent_plan p ON p.agent_turn_id=t.agent_turn_id
                AND p.child_id=t.child_id AND p.session_id=t.session_id
            LEFT JOIN agent_objective ao ON ao.objective_id=p.objective_id AND ao.child_id=p.child_id
            JOIN agent_step s ON s.plan_id=p.plan_id AND s.step_index=0 AND s.capability_name='exam.submit'
            WHERE t.agent_turn_id=? AND t.child_id=? AND t.session_id=?''',
            (slot['authorized_turn_id'],run['child_id'],run['session_id'])).fetchone()
        if not origin:
            return  # Legacy/unbound answers are saved, never given invented provenance.
        criteria = json.dumps(json.loads(item['expected_answer_json'])['task_points'],ensure_ascii=False)
        record = dict(step_id=origin['step_id'],objective_id=origin['objective_id'],child_id=run['child_id'],
            session_id=run['session_id'],subject=item['subject'],question_asked=item['prompt'],
            child_response=attempt['submitted_answer'],source_evidence_id=attempt['evidence_id'],
            success_criteria_json=criteria,exam_slot=binding,responded_at=attempt['created_at'])
        RubricEvidenceService(self.database).freeze(c,record,{},None,datetime.now(timezone.utc),source_kind='exam_open_review')

    @staticmethod
    def _review_source(c, prompt, attempt):
        binding = json.loads(prompt['request_json']).get('exam_slot')
        if not isinstance(binding,dict) or set(binding) != {'run_id','position','item_id','source_version'}:
            raise ExamSimulationError('EXAM_REVIEW_BINDING_INVALID')
        run = c.execute('SELECT * FROM specialized_exam_run WHERE run_id=?',(binding['run_id'],)).fetchone()
        slot = c.execute('SELECT * FROM specialized_exam_item WHERE run_id=? AND position=?',
            (binding['run_id'],binding['position'])).fetchone()
        if (not run or not slot or binding != ExamSimulationService._review_binding(run,slot)
                or prompt['prompt_id'] != ExamSimulationService._review_prompt_id(run,slot)
                or prompt['status'] != 'answered' or prompt['attempt_id'] != attempt['attempt_id']
                or slot['attempt_id'] != attempt['attempt_id'] or slot['answer_text'] != attempt['submitted_answer']
                or slot['answer_hash'] != sha256(attempt['submitted_answer'].encode('utf-8')).hexdigest()
                or slot['authorized_turn_id'] != prompt['authorized_turn_id']
                or (prompt['child_id'],prompt['origin_session_id'],prompt['diagnostic_item_id']) !=
                    (run['child_id'],run['session_id'],slot['diagnostic_item_id'])
                or (attempt['child_id'],attempt['session_id'],attempt['diagnostic_item_id']) !=
                    (run['child_id'],run['session_id'],slot['diagnostic_item_id'])
                or slot['bound_source_version'] != binding['source_version']
                or ExamSimulationService._source_version(c,run,slot) != binding['source_version']):
            raise ExamSimulationError('EXAM_REVIEW_SOURCE_CHANGED')
        from .diagnostic_prompt import DiagnosticPromptService, _version
        item = DiagnosticPromptService._item(c,run['child_id'],slot['diagnostic_item_id'])
        if not item or _version(item) != prompt['item_version']:
            raise ExamSimulationError('EXAM_REVIEW_ITEM_CHANGED')
        return run,slot,binding,item

    @staticmethod
    def validate_rubric_source(c, row, payload):
        """Validate a typed candidate against the original exam.submit step."""
        from .learning_rubric import RubricEvidenceService
        from .learning_obligations import withdrawn_evidence_ids
        try:
            attempt = c.execute('SELECT * FROM diagnostic_attempt WHERE evidence_id=? AND child_id=?',
                (payload['source_evidence_id'],row['child_id'])).fetchone()
            prompt = c.execute('SELECT * FROM diagnostic_prompt WHERE attempt_id=? AND child_id=?',
                (attempt['attempt_id'],row['child_id'])).fetchone() if attempt else None
            if not prompt or attempt['evidence_id'] in withdrawn_evidence_ids(c,row['child_id']):
                return False
            run,slot,binding,item = ExamSimulationService._review_source(c,prompt,attempt)
            origin = c.execute('''SELECT t.action_json,p.objective_id,p.status,t.tool_status,s.arguments_json
                FROM agent_step s JOIN agent_plan p USING(plan_id) JOIN agent_turn t USING(agent_turn_id)
                WHERE s.step_id=? AND s.step_index=0 AND s.capability_name='exam.submit'
                  AND t.agent_turn_id=? AND p.child_id=? AND p.session_id=?
                  AND t.child_id=p.child_id AND t.session_id=p.session_id''',
                (row['step_id'],slot['authorized_turn_id'],run['child_id'],run['session_id'])).fetchone()
            return bool(origin and origin['status'] != 'cancelled' and origin['tool_status'] != 'cancelled'
                and json.loads(origin['action_json']).get('exam_input_binding') == binding
                and json.loads(origin['arguments_json']).get('answer') == attempt['submitted_answer']
                and payload.get('exam_slot') == binding and payload['objective_id'] == origin['objective_id']
                and payload['session_id'] == run['session_id'] and payload['question'] == item['prompt']
                and payload['subject'] == item['subject'] and payload['response'] == attempt['submitted_answer']
                and payload['responded_at'] == attempt['created_at']
                and payload['criteria'] == json.loads(item['expected_answer_json'])['task_points']
                and payload['source_evidence_version_hash'] == RubricEvidenceService._diagnostic_source_version(c,row['child_id'],attempt['evidence_id'])
                and payload['material'] == RubricEvidenceService._material(c,row['child_id'],item['prompt'],item['subject'],attempt['evidence_id']))
        except (ValueError,TypeError,KeyError):
            return False

    @staticmethod
    def original_answer_voice(c, prompt, attempt, *, require_audio):
        from .agent_runtime import AgentRuntimeStore
        run,slot,binding,_ = ExamSimulationService._review_source(c,prompt,attempt)
        owner = (run['child_id'],run['session_id'])
        current = c.execute('''SELECT t.*,p.status AS plan_status,s.arguments_json FROM agent_turn t
            JOIN agent_plan p ON p.agent_turn_id=t.agent_turn_id AND p.child_id=t.child_id AND p.session_id=t.session_id
            JOIN agent_step s ON s.plan_id=p.plan_id AND s.step_index=0 AND s.capability_name='exam.submit'
            WHERE t.agent_turn_id=? AND t.child_id=? AND t.session_id=?''',(slot['authorized_turn_id'],*owner)).fetchone()
        if not current or current['intent']=='runtime_continuation' or current['tool_status']=='cancelled' or current['plan_status']=='cancelled':
            raise ExamSimulationError('EXAM_ORIGINAL_VOICE_REQUIRED')
        action = json.loads(current['action_json'])
        if action.get('exam_input_binding') != binding or json.loads(current['arguments_json']).get('answer') != attempt['submitted_answer']:
            raise ExamSimulationError('EXAM_ORIGINAL_ANSWER_CHANGED')
        source = current
        source_id = action.get('answer_source_turn_id')
        if source_id:
            source = c.execute('''SELECT t.*,p.status AS plan_status FROM agent_turn t JOIN agent_plan p
                ON p.agent_turn_id=t.agent_turn_id AND p.child_id=t.child_id AND p.session_id=t.session_id
                WHERE t.agent_turn_id=? AND t.child_id=? AND t.session_id=?''',(source_id,*owner)).fetchone()
            previous = c.execute('''SELECT agent_turn_id FROM agent_turn WHERE child_id=? AND session_id=?
                AND rowid<(SELECT rowid FROM agent_turn WHERE agent_turn_id=?) ORDER BY rowid DESC LIMIT 1''',
                (*owner,current['agent_turn_id'])).fetchone()
            saved = json.loads(source['action_json']) if source else {}
            candidate = saved.get('pending_answer_candidate',{})
            if (not source or not previous or previous[0] != source_id
                    or not AgentRuntimeStore.is_answer_role_confirmation(current['transcript'])
                    or source['tool_status']=='cancelled' or source['plan_status']=='cancelled'
                    or saved.get('source') != 'pending_answer_clarification' or saved.get('local_only') is not True
                    or action.get('local_only') is not True
                    or candidate.get('target') != {'action':'submit_exam_answer','binding':binding}
                    or candidate.get('answer') != source['transcript'] or attempt['submitted_answer'] != source['transcript']):
                raise ExamSimulationError('EXAM_ORIGINAL_CONFIRMATION_CHANGED')
        elif (AgentRuntimeStore.local_answer_requires_clarification(source['transcript'])
                or attempt['submitted_answer'] not in AgentRuntimeStore.local_answer_forms(source['transcript'],'exam.submit')):
            raise ExamSimulationError('EXAM_ORIGINAL_ANSWER_CHANGED')
        times = [datetime.fromisoformat(v) for v in (run['started_at'],source['created_at'],current['created_at'],attempt['created_at'])]
        if not all(t.tzinfo for t in times) or not all(a<=b for a,b in zip(times,times[1:])):
            raise ExamSimulationError('EXAM_ORIGINAL_TIME_CHANGED')
        metadata = AgentRuntimeStore._answer_voice_metadata(c,current,source,'exam.submit',binding)
        voice = metadata.get('source_voice_input_id')
        captured = c.execute('SELECT wav_sha256 FROM voice_capture_binding WHERE voice_input_id=?',(voice,)).fetchone()
        if not captured or voice != action.get('source_voice_input_id') or (require_audio and not metadata.get('raw_audio_available')):
            raise ExamSimulationError('EXAM_ORIGINAL_VOICE_REQUIRED')
        return dict(version='exam_original_answer_voice_v1',authorized_turn_id=current['agent_turn_id'],
            source_turn_id=source['agent_turn_id'],source_voice_input_id=voice,voice_input_id=metadata.get('voice_input_id'),
            capture_audio_sha256=captured[0],exam_slot=binding)

    def _advance_bound(self, c, run, slot, attempt):
        next_position = slot["position"] + 1
        completed = next_position > run["total_items"]
        next_slot = None if completed else c.execute("SELECT section_id FROM specialized_exam_item WHERE run_id=? AND position=?",
                                                    (run["run_id"], next_position)).fetchone()
        now = _now()
        c.execute("""UPDATE specialized_exam_item SET attempt_id=?,correctness=?,answered_at=? WHERE run_id=? AND position=?""",
                  (attempt["attempt_id"], attempt["correctness"] if attempt["score_status"] == "verified_local" else None,
                   attempt["created_at"], run["run_id"], slot["position"]))
        c.execute("""UPDATE specialized_exam_run SET current_position=?,elapsed_seconds=?,active_since=?,status=?,completed_at=?
            WHERE run_id=?""", (min(next_position, run["total_items"]), self._elapsed(dict(run)), None if completed else now,
                                "completed" if completed else "active", now if completed else None, run["run_id"]))
        if run['delivery_phase'] != 'ordinary':
            previous_unit = c.execute('SELECT unit_key FROM exam_listening_delivery_slot WHERE run_id=? AND position=?',
                (run['run_id'],slot['position'])).fetchone()
            following_unit = c.execute('SELECT unit_key FROM exam_listening_delivery_slot WHERE run_id=? AND position=?',
                (run['run_id'],next_position)).fetchone() if not completed else None
            if completed or (previous_unit and (not following_unit or previous_unit[0] != following_unit[0])):
                from .exam_listening_delivery import invalidate
                invalidate(c,run['run_id'])
        if completed or next_slot["section_id"] != slot["section_id"]:
            section = c.execute("SELECT * FROM specialized_exam_section_state WHERE run_id=? AND section_id=?",
                                (run["run_id"], slot["section_id"])).fetchone()
            c.execute("""UPDATE specialized_exam_section_state SET status='completed',elapsed_seconds=?,active_since=NULL
                WHERE run_id=? AND section_id=?""", (self._section_elapsed(dict(section)), run["run_id"], slot["section_id"]))
            if next_slot:
                c.execute("UPDATE specialized_exam_section_state SET status='active',active_since=? WHERE run_id=? AND section_id=?",
                          (now, run["run_id"], next_slot["section_id"]))

    def active_for_session(self, session_id: str) -> dict[str, Any] | None:
        self.ensure_schema()
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT run_id FROM specialized_exam_run WHERE session_id=?
                   AND status IN ('active','paused') ORDER BY started_at DESC LIMIT 1""",
                (session_id,),
            ).fetchone()
        return self.status(row["run_id"]) if row else None

    def status(self, run_id: str) -> dict[str, Any]:
        self.ensure_schema()
        with self.database.connect() as ready_connection:
            ready_connection.execute('BEGIN IMMEDIATE')
            ready_run = ready_connection.execute('SELECT * FROM specialized_exam_run WHERE run_id=?',(run_id,)).fetchone()
            if ready_run and ready_run['delivery_phase'] != 'ordinary' and ready_run['status'] in {'active','paused'}:
                from .exam_listening_delivery import refresh_ready,manifest,invalidate
                if ready_run['status']=='active' and manifest(ready_connection,ready_run)['status']!='ready':
                    now = _now()
                    invalidate(ready_connection,run_id)
                    ready_connection.execute("""UPDATE specialized_exam_run SET status='paused',elapsed_seconds=?,active_since=NULL,
                        delivery_phase='preparing',ready_at=NULL,ready_version=NULL WHERE run_id=?""",(self._elapsed(dict(ready_run)),run_id))
                    for active_section in ready_connection.execute("SELECT * FROM specialized_exam_section_state WHERE run_id=? AND status='active'",(run_id,)).fetchall():
                        ready_connection.execute("UPDATE specialized_exam_section_state SET status='paused',elapsed_seconds=?,active_since=NULL WHERE run_id=? AND section_id=?",
                            (self._section_elapsed(dict(active_section)),run_id,active_section['section_id']))
                else:
                    refresh_ready(ready_connection,ready_run)
            ready_connection.commit()
        self._apply_timeout(run_id)
        with self.database.connect() as connection:
            connection.execute('BEGIN')
            run = connection.execute("SELECT * FROM specialized_exam_run WHERE run_id=?", (run_id,)).fetchone()
            if not run:
                raise KeyError(run_id)
            item = connection.execute(
                """SELECT sei.position,sei.domain_id,sei.section_id,kn.subject,di.diagnostic_item_id,di.prompt,
                          di.answer_type,di.stimulus_text,di.stimulus_asset,di.assessment_mode
                   FROM specialized_exam_item sei JOIN diagnostic_item di
                     ON di.diagnostic_item_id=sei.diagnostic_item_id
                   JOIN knowledge_node kn ON kn.knowledge_node_id=di.knowledge_node_id
                   WHERE sei.run_id=? AND sei.position=?""",
                (run_id, run["current_position"]),
            ).fetchone()
            answered = connection.execute(
                "SELECT COUNT(*) FROM specialized_exam_item WHERE run_id=? AND attempt_id IS NOT NULL",
                (run_id,),
            ).fetchone()[0]
            section_rows = connection.execute(
                """
                SELECT sei.section_id,kn.subject,COUNT(*) AS total_items,
                       SUM(CASE WHEN sei.attempt_id IS NOT NULL THEN 1 ELSE 0 END) AS answered_items,
                       COUNT(DISTINCT sei.domain_id) AS covered_domains,
                       ses.title,ses.position AS section_position,ses.status,
                       ses.time_limit_seconds,ses.elapsed_seconds,ses.active_since
                FROM specialized_exam_item sei
                JOIN diagnostic_item di ON di.diagnostic_item_id=sei.diagnostic_item_id
                JOIN knowledge_node kn ON kn.knowledge_node_id=di.knowledge_node_id
                LEFT JOIN specialized_exam_section_state ses
                  ON ses.run_id=sei.run_id AND ses.section_id=sei.section_id
                WHERE sei.run_id=? GROUP BY sei.section_id,kn.subject ORDER BY MIN(sei.position)
                """,
                (run_id,),
            ).fetchall()
            answer_binding = None
            slot = connection.execute('SELECT * FROM specialized_exam_item WHERE run_id=? AND position=?',
                                      (run_id, run['current_position'])).fetchone()
            if slot:
                proposed = {'run_id': run_id, 'position': slot['position'],
                            'item_id': slot['diagnostic_item_id'], 'source_version': slot['source_version']}
                try:
                    answer_binding = self.validate_capture_reference(connection, session_id=run['session_id'],
                        child_id=run['child_id'], binding=proposed)
                except ExamSimulationError:
                    pass
            result = self.project_current_result(connection,run) if run['result_json'] else None
            listening_blocked = run['delivery_phase'] == 'ordinary' and any(self._listening_item(connection,row[0]) for row in connection.execute(
                'SELECT diagnostic_item_id FROM specialized_exam_item WHERE run_id=?',(run_id,)))
            delivery = None
            audio_delivery = None
            if run['delivery_phase'] != 'ordinary':
                from .exam_listening_delivery import manifest,public_delivery
                delivery = manifest(connection,run)
                audio_delivery = public_delivery(connection,run,slot)
            public_item = dict(item) if item and run['status'] in {'active','paused'} else None
            if public_item and self._listening_item(connection,public_item['diagnostic_item_id']):
                from .exam_listening_delivery import public_listening_item
                public_listening_item(public_item)
                public_item.update(stimulus_text=None,stimulus_asset=None,exam_audio_modality='listening',
                    delivery_status='blocked' if listening_blocked else run['delivery_phase'],
                    delivery_blockers=[EXAM_LISTENING_BLOCKER] if listening_blocked else (delivery or {}).get('blockers',[]))
        elapsed = self._elapsed(dict(run))
        blueprint = json.loads(run["blueprint_snapshot_json"]) if run["blueprint_snapshot_json"] else None
        sections = []
        current_section = None
        for row in section_rows:
            section = dict(row)
            section_elapsed = self._section_elapsed(section)
            section["elapsed_seconds"] = section_elapsed
            section["remaining_seconds"] = max(
                0, int(section.get("time_limit_seconds") or 0) - section_elapsed
            )
            section.pop("active_since", None)
            sections.append(section)
            if section.get("status") in {"active", "paused"}:
                current_section = section
        return {
            "run_id": run["run_id"], "child_id": run["child_id"], "session_id": run["session_id"],
            "track": run["track"], "subject": run["subject"],
            "mode": run["assessment_scope"] or run["mode"],
            "blueprint_id": run["blueprint_id"], "blueprint_version": run["blueprint_version"],
            "blueprint": blueprint,
            "status": run["status"], "total_items": run["total_items"],
            "answered_items": int(answered), "current_position": run["current_position"],
            "covered_domain_count": sum(int(row["covered_domains"] or 0) for row in section_rows),
            "sections": sections, "current_section": current_section,
            "time_limit_seconds": run["time_limit_seconds"], "elapsed_seconds": elapsed,
            "remaining_seconds": max(0, int(run["time_limit_seconds"]) - elapsed),
            "started_at": run["started_at"] if run['delivery_phase']=='ordinary' or run['activated_at'] else None, "completed_at": run["completed_at"],
            "current_item": public_item,
            "delivery_status": 'blocked' if listening_blocked else run['status']
                if delivery and run['status'] in {'completed','timed_out','cancelled'}
                else run['delivery_phase'] if delivery else 'ready',
            "delivery_blockers": [EXAM_LISTENING_BLOCKER] if listening_blocked else (delivery or {}).get('blockers',[]),
            "audio_delivery": audio_delivery,
            "answer_binding": answer_binding,
            "result": result,
        }

    def submit_answer(self, run_id: str, answer: str, client_action_id: str) -> dict[str, Any]:
        if not isinstance(answer, str) or not answer.strip():
            raise ExamSimulationError("答案不能为空。")
        if not isinstance(client_action_id, str) or not client_action_id.strip():
            raise ExamSimulationError("作答动作标识不能为空。")
        current = self.status(run_id)
        # Bind before the diagnostic transaction: retries (including after a
        # crash between its commit and ours) must address the original slot.
        answer_hash = sha256(answer.encode("utf-8")).hexdigest()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            item = connection.execute(
                "SELECT * FROM specialized_exam_item WHERE answer_action_id=?", (client_action_id,)
            ).fetchone()
            prior = connection.execute(
                "SELECT * FROM diagnostic_attempt WHERE client_action_id=?", (client_action_id,)
            ).fetchone()
            if item:
                if item["run_id"] != run_id or item["answer_hash"] != answer_hash:
                    raise ExamSimulationError("作答动作已绑定其他试卷或不同答案。")
            elif prior:
                # Legacy completed receipts have no reservation columns yet.
                slots = connection.execute(
                    "SELECT * FROM specialized_exam_item WHERE attempt_id=?", (prior["attempt_id"],)
                ).fetchall()
                if (len(slots) != 1 or slots[0]["run_id"] != run_id
                        or prior["session_id"] != current["session_id"]
                        or prior["child_id"] != current["child_id"]
                        or prior["submitted_answer"] != answer):
                    raise ExamSimulationError("作答动作不属于本卷题槽或答案已改变。")
                item = slots[0]
            else:
                run = connection.execute("SELECT * FROM specialized_exam_run WHERE run_id=?", (run_id,)).fetchone()
                if (run["status"] != "active" or not current["current_item"]
                        or run["current_position"] != current["current_position"]):
                    raise ExamSimulationError("模拟卷当前题槽已改变，请核对后作答。")
                item = connection.execute(
                    "SELECT * FROM specialized_exam_item WHERE run_id=? AND position=?",
                    (run_id, run["current_position"]),
                ).fetchone()
                if item["answer_action_id"] or item["attempt_id"]:
                    raise ExamSimulationError("当前题槽已有作答动作，请重试原动作。")
            if item["bound_source_version"]:
                raise ExamSimulationError("EXAM_STRONG_BINDING_REQUIRED")
            if self._listening_item(connection,item['diagnostic_item_id']):
                raise ExamSimulationError(EXAM_LISTENING_BLOCKER)
            if not item["answer_action_id"]:
                connection.execute(
                    "UPDATE specialized_exam_item SET answer_action_id=?,answer_hash=? WHERE run_id=? AND position=?",
                    (client_action_id, answer_hash, run_id, item["position"]),
                )
            if not item["attempt_id"] and current["status"] != "active":
                raise ExamSimulationError("模拟卷当前不能提交答案。")
            connection.commit()
        attempt = self.learning.submit_diagnostic(
            current["session_id"], item["diagnostic_item_id"], answer, client_action_id
        )
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute("SELECT * FROM specialized_exam_run WHERE run_id=?", (run_id,)).fetchone()
            position = int(item["position"])
            existing = connection.execute(
                "SELECT * FROM specialized_exam_item WHERE run_id=? AND position=?",
                (run_id, position),
            ).fetchone()
            if (existing["answer_action_id"] != client_action_id
                    or existing["diagnostic_item_id"] != attempt["diagnostic_item_id"]
                    or attempt["child_id"] != run["child_id"]
                    or attempt["session_id"] != run["session_id"]
                    or existing["attempt_id"] not in (None, attempt["attempt_id"])):
                raise ExamSimulationError("作答回执与原题槽不一致。")
            if not existing["attempt_id"]:
                if run["status"] != "active" or run["current_position"] != position:
                    raise ExamSimulationError("题槽已暂停或推进，回执已保留，请核对后重试。")
                elapsed = self._elapsed(dict(run))
                next_position = position + 1
                completed = next_position > int(run["total_items"])
                current_section = connection.execute(
                    "SELECT section_id FROM specialized_exam_item WHERE run_id=? AND position=?",
                    (run_id, position),
                ).fetchone()["section_id"]
                next_section = None if completed else connection.execute(
                    "SELECT section_id FROM specialized_exam_item WHERE run_id=? AND position=?",
                    (run_id, next_position),
                ).fetchone()["section_id"]
                connection.execute(
                    """UPDATE specialized_exam_item SET attempt_id=?,correctness=?,answered_at=?
                       WHERE run_id=? AND position=?""",
                    (attempt["attempt_id"], self._current_attempt_score(connection,run,existing,attempt['attempt_id']),
                     attempt["created_at"], run_id, position),
                )
                connection.execute(
                    """UPDATE specialized_exam_run SET current_position=?,elapsed_seconds=?,active_since=?,
                         status=?,completed_at=? WHERE run_id=?""",
                    (
                        min(next_position, int(run["total_items"])), elapsed,
                        None if completed else _now(), "completed" if completed else "active",
                        _now() if completed else None, run_id,
                    ),
                )
                if completed or next_section != current_section:
                    section_row = connection.execute(
                        "SELECT * FROM specialized_exam_section_state WHERE run_id=? AND section_id=?",
                        (run_id, current_section),
                    ).fetchone()
                    connection.execute(
                        """UPDATE specialized_exam_section_state
                           SET status='completed',elapsed_seconds=?,active_since=NULL
                           WHERE run_id=? AND section_id=?""",
                        (self._section_elapsed(dict(section_row)), run_id, current_section),
                    )
                    if next_section:
                        connection.execute(
                            """UPDATE specialized_exam_section_state
                               SET status='active',active_since=? WHERE run_id=? AND section_id=?""",
                            (_now(), run_id, next_section),
                        )
                connection.commit()
        refreshed = self.status(run_id)
        if refreshed["status"] == "completed" and not refreshed["result"]:
            self._finalize(run_id, "completed")
            refreshed = self.status(run_id)
        return {"attempt": attempt, "exam": refreshed}

    def pause(self, run_id: str, *, authorized_turn_id: str | None = None) -> dict[str, Any]:
        return self._control_run(run_id, "pause", authorized_turn_id=authorized_turn_id)

    def resume(self, run_id: str, *, authorized_turn_id: str | None = None) -> dict[str, Any]:
        return self._control_run(run_id, "resume", authorized_turn_id=authorized_turn_id)

    def cancel(self, run_id: str, *, authorized_turn_id: str | None = None) -> dict[str, Any]:
        return self._control_run(run_id, "cancel", authorized_turn_id=authorized_turn_id)

    def _control_run(self, run_id, command, *, authorized_turn_id=None):
        self.ensure_schema()
        gate, recorded = None, None
        with self.database.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            if authorized_turn_id:
                from .agent_runtime import AgentRuntimeStore, InvalidAgentPlanError
                from .voice_control_intent import VoiceControlIntent
                recorded = VoiceControlIntent.recorded_execution(c, authorized_turn_id, f'exam.{command}', run_id)
                if not recorded:
                    gate = AgentRuntimeStore.inspect_voice_control_execution(c, authorized_turn_id)
                    if gate and (gate['control'] != f'exam.{command}' or gate['targets'].get('exam_run_id') != run_id):
                        raise InvalidAgentPlanError('ANSWER_CONTROL_SCOPE_CHANGED')
            raw = c.execute("SELECT * FROM specialized_exam_run WHERE run_id=?", (run_id,)).fetchone()
            if not raw:
                raise KeyError(run_id)
            if recorded:
                run = dict(raw)
            else:
                session = self._active_session(c, raw["session_id"])
                run = dict(self._owned_run(c, session, run_id))
            allowed = {"pause": {"active"}, "resume": {"paused"}, "cancel": {"active", "paused"}}
            if not recorded and run["status"] in allowed[command]:
                if run['delivery_phase'] != 'ordinary':
                    from .exam_listening_delivery import activate,invalidate
                    if command == 'resume':
                        activate(c,run,authorized_turn_id,gate)
                    invalidate(c,run_id,terminal=command=='cancel')
                    if command == 'resume':
                        c.execute("""UPDATE specialized_exam_run SET delivery_phase='active',
                            started_at=CASE WHEN activated_at IS NULL THEN ? ELSE started_at END,
                            activated_at=COALESCE(activated_at,?) WHERE run_id=?""",(_now(),_now(),run_id))
                    else:
                        c.execute('UPDATE specialized_exam_run SET delivery_phase=?,ready_at=? WHERE run_id=?',
                            ('cancelled' if command=='cancel' else 'paused',_now(),run_id))
                now = _now()
                status = {"pause": "paused", "resume": "active", "cancel": "cancelled"}[command]
                c.execute("""UPDATE specialized_exam_run SET status=?,elapsed_seconds=?,active_since=?,completed_at=?
                    WHERE run_id=?""", (status, self._elapsed(run), now if command == "resume" else None,
                                        now if command == "cancel" else None, run_id))
                for section in c.execute("SELECT * FROM specialized_exam_section_state WHERE run_id=?", (run_id,)).fetchall():
                    eligible = {"pause": {"active"}, "resume": {"paused"}, "cancel": {"active", "paused", "pending"}}
                    if section["status"] in eligible[command]:
                        c.execute("""UPDATE specialized_exam_section_state SET status=?,elapsed_seconds=?,active_since=?
                            WHERE run_id=? AND section_id=?""", (status, self._section_elapsed(dict(section)),
                            now if command == "resume" else None, run_id, section["section_id"]))
                run["status"] = status
            if gate:
                receipt = {'agent_turn_id': authorized_turn_id, 'control': f'exam.{command}',
                           'run_id': run_id, 'status': run['status'], 'recorded_at': _now()}
                c.execute('UPDATE voice_control_intent_request SET execution_receipt_json=? WHERE request_key=?',
                          (json.dumps(receipt), gate['request_key']))
            c.commit()
        if run["status"] == "cancelled":
            self._finalize(run_id, "cancelled")
        return self.status(run_id)

    def recover_after_restart(self) -> int:
        self.ensure_schema()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT run_id FROM specialized_exam_run WHERE status='active'"
            ).fetchall()
            for row in rows:
                run = dict(connection.execute(
                    "SELECT * FROM specialized_exam_run WHERE run_id=?", (row["run_id"],)
                ).fetchone())
                if run['delivery_phase'] != 'ordinary':
                    from .exam_listening_delivery import invalidate
                    invalidate(connection,run['run_id'],preserve_pending=True)
                    connection.execute("UPDATE specialized_exam_run SET delivery_phase='paused',ready_at=? WHERE run_id=?",(_now(),run['run_id']))
                connection.execute(
                    """UPDATE specialized_exam_run SET status='paused',elapsed_seconds=?,
                         active_since=NULL WHERE run_id=?""",
                    (self._elapsed(run), row["run_id"]),
                )
                section = connection.execute(
                    "SELECT * FROM specialized_exam_section_state WHERE run_id=? AND status='active'",
                    (row["run_id"],),
                ).fetchone()
                if section:
                    connection.execute(
                        """UPDATE specialized_exam_section_state SET status='paused',elapsed_seconds=?,
                           active_since=NULL WHERE run_id=? AND section_id=?""",
                        (self._section_elapsed(dict(section)), row["run_id"], section["section_id"]),
                    )
            connection.commit()
        return len(rows)

    def _apply_timeout(self, run_id: str) -> None:
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM specialized_exam_run WHERE run_id=?", (run_id,)).fetchone()
            if not row or row["status"] != "active":
                return
            run = dict(row)
            section_row = connection.execute(
                """SELECT * FROM specialized_exam_section_state
                   WHERE run_id=? AND status='active' ORDER BY position LIMIT 1""",
                (run_id,),
            ).fetchone()
            if not section_row:
                return
            section = dict(section_row)
            section_elapsed = self._section_elapsed(section)
            if section_elapsed < int(section["time_limit_seconds"]):
                return
            if run['delivery_phase'] != 'ordinary':
                from .exam_listening_delivery import invalidate
                invalidate(connection,run_id)
            next_section = connection.execute(
                """SELECT * FROM specialized_exam_section_state
                   WHERE run_id=? AND position>? AND status='pending'
                   ORDER BY position LIMIT 1""",
                (run_id, section["position"]),
            ).fetchone()
            elapsed = min(int(run["time_limit_seconds"]), self._elapsed(run))
            connection.execute(
                """UPDATE specialized_exam_section_state SET status='timed_out',
                   elapsed_seconds=time_limit_seconds,active_since=NULL
                   WHERE run_id=? AND section_id=?""",
                (run_id, section["section_id"]),
            )
            if next_section:
                next_position = connection.execute(
                    """SELECT MIN(position) FROM specialized_exam_item
                       WHERE run_id=? AND section_id=?""",
                    (run_id, next_section["section_id"]),
                ).fetchone()[0]
                now = _now()
                connection.execute(
                    """UPDATE specialized_exam_section_state SET status='active',active_since=?
                       WHERE run_id=? AND section_id=?""",
                    (now, run_id, next_section["section_id"]),
                )
                connection.execute(
                    """UPDATE specialized_exam_run SET current_position=?,elapsed_seconds=?,
                       active_since=? WHERE run_id=?""",
                    (next_position, elapsed, now, run_id),
                )
                connection.commit()
                return
            connection.execute(
                """UPDATE specialized_exam_run SET status='timed_out',elapsed_seconds=?,
                     active_since=NULL,completed_at=? WHERE run_id=?""",
                (elapsed, _now(), run_id),
            )
            connection.commit()
        self._finalize(run_id, "timed_out")

    @staticmethod
    def _current_attempt_score(connection, run, slot, attempt_id):
        from .local_supply import LocalSupplyError
        try:
            listening = ExamSimulationService._listening_item(connection,slot['diagnostic_item_id'])
            if listening:
                from .exam_listening_delivery import scoring_scope
                scoring_scope(connection,run,slot,historical=True)
            version = ExamSimulationService._source_version(connection,run,slot)
            if not version or (slot['source_version'] and slot['source_version'] != version):
                return None
            if slot['bound_source_version'] and slot['bound_source_version'] != version:
                return None
            row = connection.execute('SELECT * FROM diagnostic_attempt WHERE attempt_id=?',(attempt_id,)).fetchone()
            if (not row or (row['child_id'],row['session_id'],row['diagnostic_item_id']) !=
                    (run['child_id'],run['session_id'],slot['diagnostic_item_id'])):
                return None
            if not connection.execute('SELECT 1 WHERE julianday(?)>=julianday(?) AND julianday(?)<=julianday(?)',
                    (row['created_at'],run['started_at'],row['created_at'],run['completed_at'] or _now())).fetchone():
                return None
            item = connection.execute('SELECT answer_type FROM diagnostic_item WHERE diagnostic_item_id=?',
                (slot['diagnostic_item_id'],)).fetchone()
            if item and item['answer_type']=='open_response':
                from .school_open_reviews import _anchor
                # A current original-review authority is read separately. The
                # immutable attempt's unknown score is never rewritten.
                anchor = _anchor(connection,run['child_id'],row['evidence_id'],datetime.now(timezone.utc))
                return float(anchor['score_decision']['passed'])
            current = LearningStateService._attempt(connection,attempt_id)
        except (LocalSupplyError,ValueError,TypeError,KeyError):
            return None
        score = current['correctness']
        return score if (current['score_status']=='verified_local' and type(score) in (int,float)
                         and math.isfinite(score) and 0 <= score <= 1) else None

    @staticmethod
    def project_current_result(connection, run, terminal_status=None):
        """Read-only current qualification; result_json remains historical."""
        slots = connection.execute('SELECT * FROM specialized_exam_item WHERE run_id=?',(run['run_id'],)).fetchall()
        grouped,subject_grouped,section_grouped = {},{},{}
        scores = []
        for slot in slots:
            if not slot['attempt_id']:
                continue
            score = ExamSimulationService._current_attempt_score(connection,run,slot,slot['attempt_id'])
            if score is None:
                continue
            subject = connection.execute('SELECT kn.subject FROM diagnostic_item di JOIN knowledge_node kn USING(knowledge_node_id) WHERE di.diagnostic_item_id=?',
                                         (slot['diagnostic_item_id'],)).fetchone()[0]
            grouped.setdefault(slot['domain_id'],[]).append(score)
            subject_grouped.setdefault(subject,[]).append(score)
            section_grouped.setdefault(slot['section_id'],[]).append(score)
            scores.append(score)
        domains = {key:round(sum(values)/len(values),3) for key,values in grouped.items()}
        subjects = {key:dict(answered_items=len(values),score=round(sum(values),2),max_score=len(values),
                            percentage=round(sum(values)/len(values)*100,1)) for key,values in subject_grouped.items()}
        answered = sum(slot['attempt_id'] is not None for slot in slots)
        unbound_listening = any(
            ExamSimulationService._listening_item(connection, slot['diagnostic_item_id'])
            and not connection.execute(
                'SELECT 1 FROM exam_listening_delivery_slot WHERE run_id=? AND position=?',
                (run['run_id'], slot['position']),
            ).fetchone()
            for slot in slots
        )
        needs_review = answered > len(scores) or unbound_listening
        earned = round(sum(scores),2)
        blueprint = json.loads(run['blueprint_snapshot_json']) if run['blueprint_snapshot_json'] else None
        sections = ExamSimulationService._section_results(blueprint,section_grouped)
        result = dict(terminal_status=terminal_status or run['status'],answered_items=answered,total_items=int(run['total_items']),
            verified_items=len(scores),score_status='needs_review' if needs_review else 'reference_only',
            score=None if needs_review else earned,max_score=int(run['total_items']),
            percentage=None if needs_review else round(earned/int(run['total_items'])*100,1),
            domain_results=domains,subject_results=subjects,section_results=sections,
            blueprint_id=run['blueprint_id'],blueprint_version=run['blueprint_version'],
            score_boundary=blueprint['score_boundary'] if blueprint else '能力域练习结果，不对应官方考试分数。')
        if blueprint and not needs_review:
            weighted = sum(float(section.get('weight_percent') or 0)*float(sections.get(section['section_id'],{}).get('percentage') or 0)/100
                           for section in blueprint['sections'])
            if any(section.get('weight_percent') for section in blueprint['sections']):
                result['weighted_reference_percentage']=round(weighted,1)
            if len(blueprint['sections'])==1 and blueprint['sections'][0].get('max_score'):
                result['reference_scaled_score']=round(float(result['percentage'])*float(blueprint['sections'][0]['max_score'])/100,1)
        return result

    def _finalize(self, run_id: str, terminal_status: str) -> None:
        with self.database.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            run = connection.execute("SELECT * FROM specialized_exam_run WHERE run_id=?", (run_id,)).fetchone()
            if not run:
                return
            result = self.project_current_result(connection,run,terminal_status)
            if not run['result_json']:
                connection.execute("UPDATE specialized_exam_run SET result_json=? WHERE run_id=?",
                                   (json.dumps(result,ensure_ascii=False,sort_keys=True),run_id))
            connection.commit()
        self._record_finalized_assessment(run, result)

    def _record_finalized_assessment(self, run, result):
        # The existing assessment sink is idempotent. Retry it after a crash
        # between result_json persistence and the downstream assessment commit.
        self.specialized.ensure_schema()
        with self.database.connect() as c:
            prior = c.execute('SELECT * FROM specialized_assessment_event WHERE client_action_id=?',
                ('exam-assessment:'+run['run_id'],)).fetchone()
            if prior:
                if (prior['child_id'],prior['track'],prior['subject'],prior['source_ref'],prior['assessment_type']) != (
                    run['child_id'],run['track'],run['subject'],run['run_id'],
                    f"{run['assessment_scope'] or run['mode']}_paper_{result['terminal_status']}"):
                    raise ExamSimulationError('EXAM_ASSESSMENT_INDEX_CONFLICT')
                return  # Reviews change the live projection, never the terminal index.
        domains = result["domain_results"]
        if domains:
            score = result["score"]
            self.specialized.record_assessment(
                child_id=run["child_id"], track=run["track"], subject=run["subject"],
                assessment_type=f"{run['assessment_scope'] or run['mode']}_paper_{result['terminal_status']}",
                domain_results=domains, score=score, max_score=float(run["total_items"]) if score is not None else None,
                source_ref=run["run_id"], notes=f"语音主持整卷，作答{result['answered_items']}/{run['total_items']}题。",
                client_action_id=f"exam-assessment:{run['run_id']}",
            )
        else:
            # An unscored terminal paper still needs a durable discovery anchor
            # so later reviews can enter the existing current report projection.
            # Empty domains and NULL scores are deliberately not grading evidence.
            if run['status'] not in {'completed','timed_out'}:
                return
            with self.database.connect() as c:
                tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not {'diagnostic_prompt','rubric_assessment'} <= tables:
                    return
                original = c.execute('''SELECT 1 FROM specialized_exam_item i
                    JOIN diagnostic_item di ON di.diagnostic_item_id=i.diagnostic_item_id
                    JOIN diagnostic_prompt dp ON dp.attempt_id=i.attempt_id AND dp.child_id=?
                    JOIN rubric_assessment ra ON json_extract(ra.payload_json,'$.source_evidence_id')=(
                        SELECT evidence_id FROM diagnostic_attempt WHERE attempt_id=i.attempt_id)
                        AND ra.child_id=dp.child_id AND json_extract(ra.payload_json,'$.source_kind')='exam_open_review'
                    WHERE i.run_id=? AND i.authorized_turn_id IS NOT NULL AND di.answer_type='open_response'
                        AND json_extract(dp.request_json,'$.exam_slot.run_id')=i.run_id LIMIT 1''',
                    (run['child_id'],run['run_id'])).fetchone()
                if not original:
                    return
            self.specialized.ensure_schema()
            with self.database.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                c.execute('''INSERT OR IGNORE INTO specialized_assessment_event(
                    assessment_id,child_id,track,subject,assessment_type,occurred_at,score,max_score,
                    domain_results_json,source_ref,notes,client_action_id,created_at)
                    VALUES(?,?,?,?,?,?,NULL,NULL,'{}',?,?,?,?)''',
                    ('exam-index:'+run['run_id'],run['child_id'],run['track'],run['subject'],
                     f"{run['assessment_scope'] or run['mode']}_paper_{result['terminal_status']}",
                     run['completed_at'] or _now(),run['run_id'],'整卷原答已保存；本索引不提供评分或掌握证据。',
                     'exam-assessment:'+run['run_id'],_now()))
                c.commit()

    @staticmethod
    def _balanced_items(
        by_domain: dict[str, list[dict[str, Any]]], target_items: int
    ) -> list[tuple[str, dict[str, Any]]]:
        selected: list[tuple[str, dict[str, Any]]] = []
        depth = 0
        while len(selected) < target_items:
            added = False
            for domain_id in sorted(by_domain):
                candidates = by_domain[domain_id]
                if depth < len(candidates):
                    selected.append((domain_id, candidates[depth]))
                    added = True
                    if len(selected) >= target_items:
                        break
            if not added:
                break
            depth += 1
        return selected

    def _select_blueprint_items(
        self,
        blueprint: dict[str, Any],
        by_domain: dict[str, list[dict[str, Any]]],
        required_domains: set[str],
        requested_items: int,
    ) -> list[tuple[str, str, dict[str, Any]]]:
        selected: list[tuple[str, str, dict[str, Any]]] = []
        deficits: list[str] = []
        for section in blueprint["sections"]:
            prefixes = tuple(section["domain_prefixes"])
            section_domains = {
                domain_id: items for domain_id, items in by_domain.items()
                if domain_id.startswith(prefixes)
            }
            required_in_section = {
                domain_id for domain_id in required_domains if domain_id.startswith(prefixes)
            }
            target = section.get("item_target")
            if target is None:
                target = max(requested_items, len(required_in_section))
            items = self._balanced_items(section_domains, int(target))
            if len(items) < int(target):
                deficits.append(f"{section['title']}需{target}题、现有{len(items)}题")
                continue
            selected.extend((section["section_id"], domain_id, item) for domain_id, item in items)
        if deficits:
            raise ExamSimulationError(
                "当前审核题库不能生成蓝图整卷：" + "；".join(deficits)
                + "。可以先做能力域覆盖测评。"
            )
        return selected

    @staticmethod
    def _section_results(
        blueprint: dict[str, Any] | None,
        section_grouped: dict[str, list[float]],
    ) -> dict[str, dict[str, Any]]:
        metadata = {
            section["section_id"]: section for section in (blueprint or {}).get("sections", [])
        }
        results: dict[str, dict[str, Any]] = {}
        section_ids = list(metadata)
        section_ids.extend(section_id for section_id in section_grouped if section_id not in metadata)
        for section_id in section_ids:
            values = section_grouped.get(section_id, [])
            section = metadata.get(section_id, {})
            percentage = round(sum(values) / len(values) * 100, 1) if values else 0.0
            results[section_id] = {
                "title": section.get("title", section_id),
                "answered_items": len(values),
                "expected_items": section.get("item_target"),
                "percentage": percentage,
                "weight_percent": section.get("weight_percent"),
                "max_score": section.get("max_score"),
            }
        return results

    @staticmethod
    def _elapsed(run: dict[str, Any]) -> int:
        elapsed = int(run.get("elapsed_seconds") or 0)
        active_since = run.get("active_since")
        if run.get("status") == "active" and active_since:
            try:
                parsed = datetime.fromisoformat(active_since)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                elapsed += max(0, int((datetime.now(timezone.utc) - parsed).total_seconds()))
            except ValueError:
                pass
        return elapsed

    @staticmethod
    def _section_elapsed(section: dict[str, Any]) -> int:
        elapsed = int(section.get("elapsed_seconds") or 0)
        active_since = section.get("active_since")
        if section.get("status") == "active" and active_since:
            try:
                parsed = datetime.fromisoformat(active_since)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                elapsed += max(0, int((datetime.now(timezone.utc) - parsed).total_seconds()))
            except ValueError:
                pass
        return elapsed
