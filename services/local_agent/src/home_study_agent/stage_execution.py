"""Resolve one persisted specialized plan slot on the caller's local transaction."""
from __future__ import annotations

import json

from .diagnostic_prompt import DiagnosticPromptService, _ITEM_FIELDS, _version
from .specialized_stages import VERSION, revalidate_stage_action


PLAN_BINDING_FIELDS = ("domain_id", "knowledge_node_id", "diagnostic_item_id", "source_version",
                       "review_task_id", "not_before")
CONSTRAINT_FIELDS = ("version", "current_stage", "prerequisite_version")
SOURCE_TRACKS = {"competition_math": "competition_math", "pet_daily": "pet", "junior_exam_goal": "junior_exam", "due_review": "pet"}


def _binding(value, child_id, track):
    if not isinstance(value, dict) or any(key not in value for key in PLAN_BINDING_FIELDS):
        raise ValueError("incomplete binding")
    if value.get("track", track) != track or value.get("child_id", child_id) != child_id:
        raise ValueError("wrong scope")
    if value.get("mastery_claim_allowed", False) is not False:
        raise ValueError("no mastery grant")
    constraint = value.get("stage_constraint")
    if (not isinstance(constraint, dict) or constraint.get("version") != VERSION
            or any(not isinstance(constraint.get(key), str) or not constraint[key] for key in CONSTRAINT_FIELDS)):
        raise ValueError("invalid stage constraint")
    if any(not isinstance(value[key], str) or not value[key] for key in PLAN_BINDING_FIELDS[:4]):
        raise ValueError("missing original identity")
    for key in ("review_task_id", "not_before"):
        if value[key] is not None and (not isinstance(value[key], str) or not value[key]):
            raise ValueError("invalid optional binding")
    for version in (value["source_version"], constraint["prerequisite_version"]):
        if len(version) != 64 or any(char not in "0123456789abcdef" for char in version):
            raise ValueError("invalid version")
    result = {**{key: value[key] for key in PLAN_BINDING_FIELDS}, "child_id": child_id, "track": track,
            "stage_constraint": {key: constraint[key] for key in CONSTRAINT_FIELDS},
            "execution_status": value.get("execution_status", "ready")}
    if 'goal_prerequisites' in value:
        result['goal_prerequisites'] = value['goal_prerequisites']
    return result


def resolve_stage_execution(connection, session_id: str, plan_item_id: str, *, as_of: str | None = None) -> dict:
    """Return the exact original item/version or blocked; never reserve or score.

    Read metadata_json.planning_evidence only. Child/session/track are derived
    from owned FK state. Unknown legacy data is blocked, not silently migrated.
    Use inside the actual prompt-preparation transaction to avoid a check/use gap.
    """
    c = connection
    if not c.in_transaction:
        c.execute("BEGIN")
        try:
            return resolve_stage_execution(c, session_id, plan_item_id, as_of=as_of)
        finally:
            c.rollback()
    result = {"version": "specialized_stage_execution_v1", "session_id": session_id, "plan_item_id": plan_item_id,
        "diagnostic_item_id": None, "item_version": None, "diagnostic_item": None, "source_version": None,
        "review_task_id": None, "binding": None, "read_only": True, "requires_prompt_binding": True,
        "reservation_created": False, "mastery_claim_allowed": False,
        "local_only": True, "cloud_transmission_allowed": False}

    def blocked(reason):
        return {**result, "status": "blocked", "reason_code": reason}

    if not all(isinstance(v, str) and v.strip() for v in (session_id, plan_item_id)):
        return blocked("stage_request_invalid")
    required = {"daily_session", "child_profile", "daily_plan", "plan_item", "plan_constraint_decision"}
    if not required <= {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
        return blocked("stage_plan_binding_unavailable")
    session = c.execute("""SELECT ds.*,cp.status AS child_status FROM daily_session ds
        JOIN child_profile cp USING(child_id) WHERE session_id=?""", (session_id,)).fetchone()
    if (not session or session["status"] != "active" or session["child_status"] != "active"
            or session["state"] in {"evolving", "completed"}):
        return blocked("stage_session_unavailable")
    plan = c.execute("""SELECT pi.*,dp.child_id AS plan_child_id,dp.session_id AS plan_session_id,dp.status AS plan_status
        FROM plan_item pi JOIN daily_plan dp USING(plan_id) WHERE plan_item_id=?""", (plan_item_id,)).fetchone()
    if not plan or (plan["plan_child_id"], plan["plan_session_id"]) != (session["child_id"], session_id):
        return blocked("stage_plan_scope_mismatch")
    if plan["plan_status"] != "active" or plan["status"] not in {"pending", "active", "paused"}:
        return blocked("stage_plan_inactive")
    track = SOURCE_TRACKS.get(plan["source_type"])
    if not track:
        return blocked("stage_plan_source_unsupported")
    decisions = c.execute("""SELECT metadata_json,allocated_minutes FROM plan_constraint_decision
        WHERE plan_id=? AND obligation_type=? AND source_ref IS ? AND decision='scheduled' ORDER BY decision_id""",
        (plan["plan_id"], plan["source_type"], plan["source_ref"])).fetchall()
    same_source_count = c.execute("SELECT count(*) FROM plan_item WHERE plan_id=? AND source_type=? AND source_ref IS ?",
        (plan["plan_id"], plan["source_type"], plan["source_ref"])).fetchone()[0]
    bindings = []
    for decision in decisions:
        try:
            metadata = json.loads(decision["metadata_json"])
            if not isinstance(metadata, dict):
                raise ValueError("invalid metadata")
            marked = metadata.get("plan_item_id")
            if marked is not None and marked != plan_item_id:
                continue
            if marked is None and same_source_count != 1:
                return blocked("stage_plan_binding_ambiguous")
            if decision["allocated_minutes"] <= 0:
                return blocked("stage_plan_not_scheduled")
            bindings.append(_binding(metadata["planning_evidence"], session["child_id"], track))
        except (ValueError, TypeError, KeyError):
            return blocked("stage_plan_binding_invalid")
    if not bindings:
        return blocked("stage_plan_binding_unavailable")
    binding = bindings[0]
    if any(b != binding for b in bindings[1:]):
        return blocked("stage_plan_binding_ambiguous")
    if track == 'competition_math':
        from .goal_roadmap import stage_goal_prerequisites
        prerequisites = stage_goal_prerequisites(c, session['child_id'], binding['knowledge_node_id'], as_of=as_of)
        if prerequisites['status'] != 'ready':
            return blocked(prerequisites['blockers'][0])
        saved = binding.get('goal_prerequisites')
        if saved is not None and saved != prerequisites:
            return blocked('knowledge_prerequisite_binding_changed')
        if saved is None and prerequisites['dependencies']:
            return blocked('knowledge_prerequisite_binding_missing')
    if plan['source_type'] == 'due_review':
        from .writing_stage_reviews import validate_writing_stage_review
        try:
            review = validate_writing_stage_review(c, session['child_id'], plan['source_ref'],
                                                   allowed_statuses=('pending',), as_of=as_of)
        except ValueError:
            return blocked('stage_writing_review_source_changed')
        if (binding['review_task_id'] != review['review_task_id']
                or binding['diagnostic_item_id'] != review['diagnostic_item_id']
                or binding['knowledge_node_id'] != review['knowledge_node_id']):
            return blocked('stage_writing_review_binding_mismatch')
    elif track == 'pet' and plan['source_ref'] != binding['diagnostic_item_id']:
        return blocked('stage_pet_source_binding_mismatch')
    checked = revalidate_stage_action(c, session["child_id"], track, binding=binding, as_of=as_of)
    if checked["status"] != "ready":
        return blocked(checked["reason_code"])
    item = DiagnosticPromptService._item(c, session["child_id"], binding["diagnostic_item_id"])
    if not item or item["subject"] != plan["subject"] or item["knowledge_node_id"] != binding["knowledge_node_id"]:
        return blocked("stage_original_item_unavailable")
    return {**result, "status": "ready", "reason_code": "stage_original_binding_current",
        "child_id": session["child_id"], "track": track, "knowledge_node_id": item["knowledge_node_id"],
        "diagnostic_item_id": item["diagnostic_item_id"], "item_version": _version(item),
        "diagnostic_item": {key: item[key] for key in _ITEM_FIELDS}, "source_version": binding["source_version"],
        "review_task_id": binding["review_task_id"], "not_before": checked["action"]["not_before"], "binding": binding}
