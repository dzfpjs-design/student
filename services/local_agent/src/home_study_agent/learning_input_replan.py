"""Append bounded school-work replacements; never rewrite execution history."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from uuid import uuid4
from .family_clock import FAMILY_ZONE as ZONE, family_time

from .learning_input_revision import LearningInputRevisionService, LOCAL, _json, _version


RULE_FIELDS = "latest_end_time,max_learning_minutes,task_segment_minutes,break_minutes,pet_min_minutes,version"


class ReplanConflict(ValueError):
    pass


def utc_now():
    return datetime.now(timezone.utc)


def _instant(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return result if result.tzinfo else result.replace(tzinfo=ZONE)


def _spent(item, now):
    seconds = int(item["active_seconds"] or 0)
    if seconds < 0:
        raise ReplanConflict("REPLAN_TIMING_INVALID")
    segment = item["active_segment_started_at"]
    if segment:
        elapsed = (now - _instant(segment)).total_seconds()
        if item["status"] != "active" or elapsed < 0:
            raise ReplanConflict("REPLAN_TIMING_INVALID")
        seconds += math.ceil(elapsed)
    if item["actual_minutes"] is not None:
        if item["actual_minutes"] < 0:
            raise ReplanConflict("REPLAN_TIMING_INVALID")
        seconds = max(seconds, math.ceil(item["actual_minutes"] * 60))
    return seconds


def completed_homework_source(connection, child_id, session_id, homework_id):
    """Completed current school segments, not cancelled history or unplaced scope."""
    source = connection.execute("""SELECT * FROM homework_item WHERE homework_id=?
        AND child_id=? AND session_id=? AND status IN ('pending','completed') AND confirmation_status='child_confirmed'""",
        (homework_id, child_id, session_id)).fetchone()
    if not source:
        return False
    items = connection.execute("""SELECT pi.status FROM plan_item pi JOIN daily_plan dp USING(plan_id)
        WHERE pi.homework_id=? AND dp.child_id=? AND dp.session_id=?
        AND pi.source_type='school_homework' AND pi.status!='cancelled'""",
        (homework_id, child_id, session_id)).fetchall()
    if not items or any(item['status'] != 'completed' for item in items):
        return False
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='learning_input_revision_event'").fetchone():
        return True
    revision = connection.execute("""SELECT * FROM learning_input_revision_event WHERE child_id=?
        AND session_id=? AND target_kind='homework' AND target_id=? ORDER BY rowid DESC LIMIT 1""",
        (child_id, session_id, homework_id)).fetchone()
    if not revision:
        return True
    try:
        after, impact = json.loads(revision['after_json']), json.loads(revision['impact_json'])
        current = {**dict(source), 'status': 'pending'}
        if after != current or not isinstance(impact, dict):
            return False
        if not impact.get('requires_replan'):
            return True
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='learning_input_replan_receipt'").fetchone():
            return False
        receipt = connection.execute("""SELECT result_json,source_record_json FROM learning_input_replan_receipt
            WHERE event_id=? AND child_id=? AND session_id=?""",
            (revision['event_id'], child_id, session_id)).fetchone()
        if not receipt or json.loads(receipt['source_record_json']) != current:
            return False
        result = json.loads(receipt['result_json'])
        # A partial allocation or an unresolved executed scope is still work,
        # even when every materialized segment has been completed.
        return (isinstance(result, dict) and result.get('unplaced') == []
                and isinstance(result.get('receipt'), dict)
                and result['receipt'].get('status') == 'scheduled')
    except (ValueError, TypeError):
        return False


class LearningInputReplanService:
    def __init__(self, database):
        self.database = database
        self.revisions = LearningInputRevisionService(database)

    def initialize(self):
        with self.database.connect() as c:
            if c.in_transaction:
                raise ReplanConflict("REPLAN_INITIALIZE_OUTSIDE_TRANSACTION")
            c.execute("""CREATE TABLE IF NOT EXISTS learning_input_replan_receipt(
                receipt_id TEXT PRIMARY KEY,child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
                event_id TEXT NOT NULL UNIQUE REFERENCES learning_input_revision_event(event_id) ON DELETE CASCADE,
                plan_id TEXT NOT NULL REFERENCES daily_plan(plan_id) ON DELETE CASCADE,
                result_json TEXT NOT NULL,source_record_json TEXT NOT NULL,created_at TEXT NOT NULL)""")
            c.execute("""CREATE TABLE IF NOT EXISTS learning_input_replan_command(
                child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,client_action_id TEXT NOT NULL,
                event_id TEXT NOT NULL REFERENCES learning_input_revision_event(event_id) ON DELETE CASCADE,
                receipt_id TEXT NOT NULL REFERENCES learning_input_replan_receipt(receipt_id) ON DELETE CASCADE,
                PRIMARY KEY(child_id,client_action_id))""")
            c.execute("""CREATE TABLE IF NOT EXISTS learning_input_replan_unplaced(
                obligation_id TEXT PRIMARY KEY,receipt_id TEXT NOT NULL REFERENCES learning_input_replan_receipt(receipt_id) ON DELETE CASCADE,
                child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
                minutes INTEGER NOT NULL,reason TEXT NOT NULL,payload_json TEXT NOT NULL)""")
            c.execute("""CREATE TABLE IF NOT EXISTS learning_input_replan_item(
                plan_item_id TEXT PRIMARY KEY REFERENCES plan_item(plan_item_id) ON DELETE CASCADE,
                receipt_id TEXT NOT NULL REFERENCES learning_input_replan_receipt(receipt_id) ON DELETE CASCADE,
                child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
                source_binding_json TEXT NOT NULL)""")
            c.commit()

    def execute_replan(self, child_id, event_id, client_action_id, connection=None):
        if any(not isinstance(value, str) or not value.strip() or len(value) > 200
               for value in (child_id, event_id, client_action_id)):
            raise ReplanConflict("REPLAN_ACTION_INVALID")
        if connection is None:
            with self.database.connect() as c:
                c.execute("BEGIN IMMEDIATE")
                result = self.execute_replan(child_id, event_id, client_action_id, c)
                c.commit()
                return result
        if not connection.in_transaction:
            raise ReplanConflict("REPLAN_WRITE_TRANSACTION_REQUIRED")
        savepoint = "replan_" + uuid4().hex
        connection.execute(f"SAVEPOINT {savepoint}")
        try:
            result = self._execute(connection, child_id, event_id, client_action_id)
        except Exception:
            connection.execute(f"ROLLBACK TO {savepoint}")
            connection.execute(f"RELEASE {savepoint}")
            raise
        connection.execute(f"RELEASE {savepoint}")
        return result

    def _execute(self, c, child_id, event_id, key):
        event = c.execute("SELECT rowid AS sequence,* FROM learning_input_revision_event WHERE event_id=? AND child_id=?",
                          (event_id, child_id)).fetchone()
        if not event:
            raise ReplanConflict("REPLAN_EVENT_NOT_OWNED")
        source_snapshot = json.loads(event["after_json"])
        obligation = json.loads(event["impact_json"]).get("replan_obligation") or {}
        if source_snapshot.get("source_artifact_id"):
            artifact = c.execute("SELECT * FROM capture_artifact WHERE artifact_id=? AND child_id=? AND session_id=?",
                                 (source_snapshot["source_artifact_id"], child_id, event["session_id"])).fetchone()
            if not artifact or _version(dict(artifact)) != obligation.get("artifact_fingerprint"):
                raise ReplanConflict("REPLAN_ARTIFACT_SOURCE_CHANGED")
        existing_key = c.execute("SELECT event_id FROM learning_input_replan_command WHERE child_id=? AND client_action_id=?", (child_id, key)).fetchone()
        if existing_key and existing_key[0] != event_id:
            raise ReplanConflict("REPLAN_IDEMPOTENCY_CONFLICT")
        replay = c.execute("SELECT * FROM learning_input_replan_receipt WHERE event_id=? AND child_id=?", (event_id, child_id)).fetchone()
        if replay:
            source = self.revisions._item(c, child_id, event["session_id"], event["target_kind"], event["target_id"])
            if not source or source["last_event_id"] != event_id or _json(source["record"]) != replay["source_record_json"]:
                raise ReplanConflict("REPLAN_SOURCE_CHANGED")
            c.execute("INSERT OR IGNORE INTO learning_input_replan_command VALUES (?,?,?,?)", (child_id, key, event_id, replay["receipt_id"]))
            return json.loads(replay["result_json"])
        view = self.revisions.inspect_replan_obligation(child_id, event_id, connection=c)
        if view["status"] != "pending" or not view["source_current"]:
            raise ReplanConflict("REPLAN_SOURCE_CHANGED_OR_NO_OBLIGATION")
        obligation = view["obligation"]
        session = c.execute("""SELECT ds.*,cp.grade,cp.status AS child_status FROM daily_session ds
            JOIN child_profile cp USING(child_id) WHERE ds.session_id=? AND ds.child_id=?""", (event["session_id"], child_id)).fetchone()
        if not session or session["status"] != "active" or session["child_status"] != "active" or session["state"] not in {"study", "plan_review", "break"}:
            raise ReplanConflict("REPLAN_SESSION_INACTIVE")
        command = c.execute("""SELECT t.child_id,t.session_id,p.status AS plan_status,s.status AS step_status
            FROM agent_turn t JOIN agent_plan p ON p.agent_turn_id=t.agent_turn_id JOIN agent_step s USING(plan_id)
            WHERE t.agent_turn_id=? AND s.step_index=0""", (event["client_action_id"],)).fetchone()
        if (not command or command["child_id"] != child_id or command["session_id"] != event["session_id"]
                or command["plan_status"] == "cancelled" or command["step_status"] == "cancelled"):
            raise ReplanConflict("REPLAN_AUTHORIZATION_REVOKED")
        plan = c.execute("SELECT * FROM daily_plan WHERE session_id=? AND child_id=? AND status='active'", (event["session_id"], child_id)).fetchone()
        if not plan:
            raise ReplanConflict("REPLAN_ORIGINAL_PLAN_UNAVAILABLE")
        originals = [item for item in obligation["original_plan_items"] if item["plan_id"] == plan["plan_id"]]
        addition = event['operation'] == 'add' and obligation.get('operation') == 'add'
        if addition:
            items_now = [dict(row) for row in c.execute('SELECT * FROM plan_item WHERE plan_id=? ORDER BY plan_item_id', (plan['plan_id'],))]
            if dict(plan) != obligation.get('addition_plan') or items_now != obligation.get('addition_items'):
                raise ReplanConflict('REPLAN_ADDITION_PLAN_CHANGED')
        elif not originals or any(item.get("daily_plan_version") != plan["version"] or item.get("plan_date") != plan["plan_date"] for item in originals):
            raise ReplanConflict("REPLAN_PLAN_VERSION_CHANGED")
        rule = c.execute(f"SELECT {RULE_FIELDS} FROM family_rule WHERE child_id=?", (child_id,)).fetchone()
        context = c.execute("SELECT * FROM session_planning_context WHERE session_id=?", (event["session_id"],)).fetchone()
        if (not rule or not context or dict(rule) != obligation.get("budget_snapshot")
                or dict(context) != obligation.get("planning_context_snapshot")):
            raise ReplanConflict("REPLAN_BUDGET_CHANGED_OR_UNKNOWN")
        instant = utc_now()
        now = family_time(instant)
        # Match the original family's plan day, never the host OS calendar.
        if plan["plan_date"] != now.date().isoformat():
            raise ReplanConflict("REPLAN_DAY_CHANGED")
        source = obligation["revised_source"]
        if event["target_kind"] != "homework":
            raise ReplanConflict("REPLAN_SCHOOL_HOMEWORK_ONLY")
        if source["status"] != "cancelled" and (source["status"] != "pending" or source["confirmation_status"] != "child_confirmed"):
            raise ReplanConflict("REPLAN_SOURCE_NOT_CONFIRMED_PENDING")
        items = [dict(row) for row in c.execute("SELECT * FROM plan_item WHERE plan_id=? ORDER BY position,plan_item_id", (plan["plan_id"],))]
        prior_ids = set()
        for row in c.execute("SELECT impact_json FROM learning_input_revision_event WHERE child_id=? AND session_id=? AND target_kind='homework' AND target_id=? AND rowid<=?",
                             (child_id, event["session_id"], event["target_id"], event["sequence"])):
            prior_ids.update(json.loads(row[0]).get("cancelled_unstarted_plan_ids", []))
        cancelled = [item for item in items if item["plan_item_id"] in prior_ids and item["status"] == "cancelled"
                     and not item["started_at"] and not item["completed_at"] and not item["active_seconds"] and item["actual_minutes"] in (None, 0)
                     and not item["active_segment_started_at"] and item["source_type"] == "school_homework"
                     and item["homework_id"] == event["target_id"] and item["source_ref"] == event["target_id"]]
        preserved = [item for item in items if item["homework_id"] == event["target_id"] and item["source_type"] == "school_homework"
                     and item["status"] != "cancelled"]
        requested = max(0, int(source["expected_minutes"]) - sum(int(item["expected_minutes"]) for item in preserved))
        before = json.loads(event["before_json"])
        scope_changed = any(before.get(field) != source.get(field) for field in ("subject", "title", "range_text", "task_type", "special_requirements"))
        executed = [item for item in items if item["homework_id"] == event["target_id"] and item["source_type"] == "school_homework"
                    and (item["started_at"] or item["completed_at"] or item["active_seconds"] or item["actual_minutes"] or item["active_segment_started_at"])]
        disputed = bool(executed and (scope_changed or any(item["subject"] != source["subject"] for item in executed)))
        budget, available, reason = self._budget(c, child_id, session, plan, items, rule, context, source, now)
        if disputed:
            reason = "executed_scope_requires_review"
        elif cancelled and requested == 0 and source["status"] != "cancelled":
            reason = "remaining_scope_requires_review"
        elif not cancelled and source["status"] != "cancelled" and not addition:
            reason = "no_cancelled_unstarted_school_obligation"
        if reason:
            available = 0
        if source["status"] == "cancelled":
            requested, reason = 0, None
        receipt_id = str(uuid4())
        newitems = []
        remaining = requested
        position = max((item["position"] for item in items), default=0)
        needs_break = budget["has_prior_learning"]
        segment = budget["effective_task_segment_minutes_cap"]
        if segment < 1 or rule["break_minutes"] < 0:
            raise ReplanConflict("REPLAN_RULE_INVALID")
        source_binding = {"version": "learning_input_replan_source_v1", "event_id": event_id, "child_id": child_id,
            "session_id": event["session_id"], "homework_id": event["target_id"], "source": source,
            "artifact_fingerprint": obligation.get("artifact_fingerprint"), "plan_date": plan["plan_date"],
            "plan_id": plan["plan_id"], "from_plan_version": plan["version"], "to_plan_version": plan["version"] + 1}
        while remaining > 0 and available > 0:
            rest = int(rule["break_minutes"]) if needs_break else 0
            minutes = min(segment, remaining, max(0, available // 60 - rest))
            if minutes < 1:
                break
            for kind, duration in (("planned_break", rest), ("school_homework", minutes)):
                if not duration:
                    continue
                position += 1
                item_id = str(uuid4())
                c.execute("""INSERT INTO plan_item(plan_item_id,plan_id,homework_id,subject,title,expected_minutes,priority,status,
                    position,source_type,source_ref,completion_standard,task_type)
                    VALUES (?,?,?,?,?,?,1,'pending',?,?,?,?,?)""",
                    (item_id, plan["plan_id"], event["target_id"], source["subject"] if kind == "school_homework" else "休息",
                     source["title"] if kind == "school_homework" else "分段休息", duration, position, kind,
                     event["target_id"] if kind == "school_homework" else obligation["obligation_id"],
                     "完成修订后的作业内容并自行检查" if kind == "school_homework" else "完成本次分段休息",
                     source["task_type"] if kind == "school_homework" else "break"))
                newitems.append(dict(c.execute("SELECT * FROM plan_item WHERE plan_item_id=?", (item_id,)).fetchone()))
            available -= (minutes + rest) * 60
            remaining -= minutes
            needs_break = True
        if newitems:
            total = c.execute("SELECT COALESCE(SUM(expected_minutes),0) FROM plan_item WHERE plan_id=? AND status!='cancelled'", (plan["plan_id"],)).fetchone()[0]
            changed = c.execute("UPDATE daily_plan SET total_minutes=?,version=version+1 WHERE plan_id=? AND status='active' AND version=?",
                                (total, plan["plan_id"], plan["version"]))
            if changed.rowcount != 1:
                raise ReplanConflict("REPLAN_PLAN_VERSION_CHANGED")
        source_binding["to_plan_version"] = plan["version"] + bool(newitems)
        unplaced = []
        if remaining or reason:
            unplaced.append({"obligation_id": str(uuid4()), "minutes": remaining,
                "reason": reason or budget["limiting_reason"], "source_binding": source_binding,
                "original_plan_item_ids": [item["plan_item_id"] for item in cancelled], "status": "unplaced"})
        status = "deleted" if source["status"] == "cancelled" else "partial" if newitems and unplaced else "deferred" if unplaced else "scheduled"
        receipt = {"receipt_id": receipt_id, "status": status, "obligation_id": obligation["obligation_id"],
            "kind": "historical_write_receipt", "newitems_state": "at_commit",
            "event_id": event_id, "child_id": child_id, "session_id": event["session_id"], "plan_id": plan["plan_id"],
            "from_plan_version": plan["version"], "to_plan_version": plan["version"] + bool(newitems),
            "requested_minutes": requested, "allocated_minutes": requested - remaining, "budget": budget,
            "replacement_source_item_ids": [item["plan_item_id"] for item in cancelled],
            "preserved_plan_item_ids": [item["plan_item_id"] for item in items if item not in cancelled],
            "replacement_map": [{"source_plan_item_id": item["plan_item_id"],
                "new_plan_item_ids": [new["plan_item_id"] for new in newitems if new["source_type"] == "school_homework"],
                "unplaced_obligation_ids": [pending["obligation_id"] for pending in unplaced],
                "disposition": "deleted_by_revision" if status == "deleted" else status} for item in cancelled],
            "created_at": now.isoformat(), "learning_evidence_changed": False}
        result = {"receipt": receipt, "newitems": newitems, "unplaced": unplaced, **LOCAL}
        c.execute("INSERT INTO learning_input_replan_receipt VALUES (?,?,?,?,?,?,?,?)",
                  (receipt_id, child_id, event["session_id"], event_id, plan["plan_id"], _json(result), _json(source), now.isoformat()))
        c.execute("INSERT INTO learning_input_replan_command VALUES (?,?,?,?)", (child_id, key, event_id, receipt_id))
        for item in newitems:
            c.execute("INSERT INTO learning_input_replan_item VALUES (?,?,?,?,?)",
                      (item["plan_item_id"], receipt_id, child_id, event["session_id"], _json({**source_binding,
                        "item_source_type": item["source_type"], "item_source_ref": item["source_ref"],
                        "expected_minutes": item["expected_minutes"], "subject": item["subject"], "title": item["title"]})))
            c.execute("INSERT INTO plan_item_event VALUES (?,?, 'revision_replanned',?,?,?)",
                      (str(uuid4()), item["plan_item_id"], now.isoformat(), _json(source_binding), f"replan:{receipt_id}:{item['plan_item_id']}"))
        for pending in unplaced:
            c.execute("INSERT INTO learning_input_replan_unplaced VALUES (?,?,?,?,?,?,?)",
                      (pending["obligation_id"], receipt_id, child_id, event["session_id"], pending["minutes"], pending["reason"], _json(pending)))
        return result

    def inspect_item(self, child_id, plan_item_id, connection=None):
        if connection is None:
            with self.database.connect() as c:
                c.execute("BEGIN")
                return self.inspect_item(child_id, plan_item_id, c)
        row = connection.execute("""SELECT b.source_binding_json,pi.*,dp.status AS plan_status,dp.version AS plan_version,
            dp.child_id,dp.session_id,dp.plan_date,ds.status AS session_status,cp.status AS child_status
            FROM learning_input_replan_item b JOIN plan_item pi USING(plan_item_id)
            JOIN daily_plan dp USING(plan_id) JOIN daily_session ds ON ds.session_id=dp.session_id AND ds.child_id=dp.child_id
            JOIN child_profile cp ON cp.child_id=dp.child_id WHERE b.child_id=? AND pi.plan_item_id=?""", (child_id, plan_item_id)).fetchone()
        if not row:
            return {"status": "unknown", "eligible": False, **LOCAL}
        binding = json.loads(row["source_binding_json"])
        if binding.get("version") == "learning_input_carryover_source_v1":
            from .learning_input_carryover import LearningInputCarryoverService
            return LearningInputCarryoverService(self.database).inspect_item(child_id, plan_item_id, connection=connection)
        from .learning_input_carryover import LearningInputCarryoverService
        if LearningInputCarryoverService.owns_unplaced_item(connection, child_id, plan_item_id):
            return {"status": "reclaimed", "eligible": False, **LOCAL}
        source = self.revisions._item(connection, child_id, binding["session_id"], "homework", binding["homework_id"])
        artifact_valid = True
        if binding["source"].get("source_artifact_id"):
            artifact = connection.execute("SELECT * FROM capture_artifact WHERE artifact_id=? AND child_id=? AND session_id=?",
                (binding["source"]["source_artifact_id"], child_id, binding["session_id"])).fetchone()
            artifact_valid = bool(artifact and _version(dict(artifact)) == binding.get("artifact_fingerprint"))
        valid = (row["child_id"] == child_id and row["session_id"] == binding["session_id"]
                 and row["session_status"] == "active" and row["child_status"] == "active" and artifact_valid
                 and row["plan_date"] == binding.get("plan_date")
                 and row["plan_status"] == "active" and row["plan_version"] >= binding["to_plan_version"]
                 and row["status"] != "cancelled" and source and source["last_event_id"] == binding["event_id"]
                 and source["record"] == binding["source"] and row["homework_id"] == binding["homework_id"]
                 and row["source_type"] == binding["item_source_type"] and row["source_ref"] == binding["item_source_ref"]
                 and all(row[field] == binding[field] for field in ("expected_minutes", "subject", "title")))
        return {"status": "current" if valid else "stale", "eligible": bool(valid), "source_binding": binding, **LOCAL}

    def _budget(self, c, child_id, session, plan, items, rule, context, source, now, *, consumes_pet_reserve=False):
        from .report_followup import ReportFollowupService
        parent_projection = ReportFollowupService(self.database).project_for_plan(child_id, plan["plan_date"], connection=c)
        constraints = parent_projection["constraints"]
        total_cap = int(rule["max_learning_minutes"])
        segment_cap = int(rule["task_segment_minutes"])
        for constraint in constraints:
            value = constraint["value_minutes"]
            if type(value) is not int or value < 1:
                raise ReplanConflict("REPLAN_PARENT_CONSTRAINT_INVALID")
            if constraint["constraint_type"] == "daily_total_minutes_cap":
                total_cap = min(total_cap, value)
            elif constraint["constraint_type"] == "task_segment_minutes_cap":
                segment_cap = min(segment_cap, value)
            else:
                raise ReplanConflict("REPLAN_PARENT_CONSTRAINT_UNSUPPORTED")
        if context["energy_level"] == "low":
            segment_cap = min(segment_cap, 15)
        spent = 0
        pet_accounted = 0
        timing_unknown = False
        for row in c.execute("SELECT pi.* FROM plan_item pi JOIN daily_plan dp USING(plan_id) WHERE dp.child_id=? AND dp.plan_date=?", (child_id, plan["plan_date"])):
            elapsed = _spent(row, now)
            spent += elapsed
            if row["source_type"] == "pet_daily":
                pet_accounted += elapsed
            if row["status"] == "completed" and row["actual_minutes"] is None and not row["active_seconds"]:
                timing_unknown = True
        protected = 0
        prior_learning = spent > 0
        for item in items:
            # Deferred work keeps its spent time above, but no longer reserves
            # today's unspent time. Its future obligation remains in carryover.
            if item["status"] not in {"pending", "paused", "active"}:
                continue
            reserve = max(0, item["expected_minutes"] * 60 - _spent(item, now))
            protected += reserve
            if item["source_type"] != "planned_break":
                prior_learning = True
            if item["source_type"] == "pet_daily":
                pet_accounted += reserve
        fixed = int(context["fixed_commitment_minutes"]) * 60
        pet = max(0, int(rule["pet_min_minutes"]) * 60 - pet_accounted) if session["grade"] == "四年级" and not consumes_pet_reserve else 0
        maximum = max(0, total_cap * 60 - spent - protected - fixed - pet)
        reason = "actual_time_unknown" if timing_unknown else None
        try:
            hour, minute = (int(part) for part in rule["latest_end_time"].split(":"))
            # Never roll yesterday's obligation forward to another evening.
            end = datetime.fromisoformat(plan["plan_date"]).replace(tzinfo=ZONE, hour=hour, minute=minute)
        except (AttributeError, ValueError, TypeError):
            end, reason = now, "latest_end_time_unknown"
        school_end = None
        if source.get("due_at"):
            try:
                if len(source["due_at"]) <= 10:
                    raise ValueError("deadline time unknown")
                school_end = _instant(source["due_at"])
                end = min(end, school_end)
            except (ValueError, TypeError):
                reason = "school_deadline_unknown"
        wall = max(0, math.floor((end - now).total_seconds()) - protected - fixed - pet)
        limiting = "total_budget_exhausted" if maximum <= wall else "school_deadline_capacity" if school_end and end == school_end else "latest_end_capacity"
        return {"timezone": "Asia/Shanghai", "planning_date": plan["plan_date"], "plan_date_basis": "host_local",
                "parent_constraints": constraints, "parent_projection_fingerprint": parent_projection["fingerprint"],
                "effective_daily_total_minutes_cap": total_cap, "effective_task_segment_minutes_cap": segment_cap,
                "as_of": now.isoformat(), "actual_spent_seconds": spent,
                "protected_remaining_seconds": protected, "fixed_reserved_seconds": fixed,
                "fixed_progress_unknown": bool(fixed), "pet_reserved_seconds": pet,
                "total_budget_remaining_seconds": maximum, "wall_remaining_seconds": wall,
                "cutoff_at": end.isoformat(), "has_prior_learning": prior_learning, "limiting_reason": limiting,
                "actual_time_unknown": timing_unknown}, min(maximum, wall), reason
