"""Preserve listening modality when a diagnostic item enters the mistake loop."""
from __future__ import annotations

import hashlib
import json

from .database import local_media_scope
from .diagnostic_prompt import DiagnosticPromptService
from .mistake_book import utc_now


VERSION = "mistake_listening_delivery_v1"


def _activity(focus: dict) -> tuple[dict, str] | tuple[None, None]:
    state = focus.get("state") or {}
    activity = state.get("next_activity") or {}
    item = activity.get("item") or {}
    if item.get("answer_type") != "listening_text" or not item.get("diagnostic_item_id"):
        return None, None
    identity = {
        "mistake_id": focus.get("mistake_id"),
        "phase": focus.get("phase"),
        "stage": activity.get("stage"),
        "probe_event_id": activity.get("probe_event_id"),
        "diagnostic_item_id": item["diagnostic_item_id"],
    }
    key = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return item, key


def _marker(focus: dict, plan_item_id: str, activity_key: str) -> dict:
    return {
        "version": VERSION,
        "mistake_id": str(focus["mistake_id"]),
        "plan_item_id": str(plan_item_id),
        "activity_key": activity_key,
    }


def is_delivery_prompt(row) -> bool:
    try:
        marker = json.loads(row["request_json"]).get("mistake_listening_delivery")
    except (TypeError, ValueError, KeyError, AttributeError):
        return False
    return isinstance(marker, dict) and marker.get("version") == VERSION


def prepare(connection, focus: dict, plan_item_id: str, client_action_id: str) -> dict:
    """Prepare/reuse reviewed audio. The caller owns the transaction."""
    item, activity_key = _activity(focus)
    if not item:
        return {"status": "not_applicable", "prompt": None, "resource": None}
    marker = _marker(focus, plan_item_id, activity_key)
    service = DiagnosticPromptService.borrowed(connection)
    original = DiagnosticPromptService._item(connection, focus['child_id'], item['diagnostic_item_id'])
    if original is None:
        return {'status': 'awaiting_resources', 'reason': 'listening_prompt_unavailable', 'prompt': None, 'resource': None}
    prepared = service.prepare(
        focus["session_id"], track=original['track'], preferred_item_id=item["diagnostic_item_id"],
        client_action_id=client_action_id, _mistake_listening_delivery=marker,
    )
    prompt = prepared.get("prompt")
    if not prompt or prompt.get("diagnostic_item_id") != item["diagnostic_item_id"]:
        return {"status": "awaiting_resources", "reason": "listening_prompt_unavailable",
                "prompt": prepared, "resource": None}
    from .pet_listening_reuse import bind_reviewed_listening_resource
    bind_reviewed_listening_resource(
        connection, focus["session_id"], focus["child_id"],
        prompt["prompt_id"], prompt["item_version"],
    )
    from .pet_listening_admission import resolve_pet_listening_resource
    resource = resolve_pet_listening_resource(
        connection, focus["session_id"], focus["child_id"],
        prompt["prompt_id"], prompt["item_version"],
    )
    return {**resource, "prompt": prepared, "activity_key": activity_key}


def _bound_prompt(connection, focus: dict, operation: dict):
    item, activity_key = _activity(focus)
    if not item or operation.get("item_id") not in {None, item["diagnostic_item_id"]}:
        return None
    rows = connection.execute(
        """SELECT * FROM diagnostic_prompt
           WHERE child_id=? AND origin_session_id=? AND diagnostic_item_id=?
             AND status IN ('waiting_answer','answer_saved') ORDER BY rowid DESC LIMIT 8""",
        (focus["child_id"], focus["session_id"], item["diagnostic_item_id"]),
    ).fetchall()
    matches = []
    for row in rows:
        if not is_delivery_prompt(row):
            continue
        marker = json.loads(row["request_json"])["mistake_listening_delivery"]
        if marker.get("mistake_id") == focus["mistake_id"] and marker.get("activity_key") == activity_key:
            matches.append(row)
    if len(matches) != 1:
        return None
    return matches[0]


def assert_answer_ready(connection, focus: dict, operation: dict) -> dict | None:
    """Reject a listening answer until this exact item has fresh full playback."""
    item, _ = _activity(focus)
    if not item:
        return None
    row = _bound_prompt(connection, focus, operation)
    scope = local_media_scope(connection)
    if not row or not scope:
        from .agent_runtime import InvalidAgentPlanError
        raise InvalidAgentPlanError("ANSWER_LISTENING_DELIVERY_UNAVAILABLE")
    from .pet_listening_admission import resolve_pet_listening_resource
    resource = resolve_pet_listening_resource(
        connection, focus["session_id"], focus["child_id"], row["prompt_id"], row["item_version"])
    if resource.get("status") != "ready":
        from .agent_runtime import InvalidAgentPlanError
        raise InvalidAgentPlanError("ANSWER_LISTENING_DELIVERY_UNAVAILABLE")
    from .pet_media_evidence import project_pet_media_evidence
    media = project_pet_media_evidence(
        connection, focus["session_id"], media_id=resource["resource"]["media_id"], **scope)
    if not media["qualification"]["task_delivery_qualified"]:
        from .agent_runtime import InvalidAgentPlanError
        raise InvalidAgentPlanError("ANSWER_LISTENING_PLAYBACK_REQUIRED")
    return {"prompt_id": row["prompt_id"], "item_version": row["item_version"],
            "media_scope": scope, "media_id": media["media_id"]}


def save_answer(connection, binding: dict | None, operation: dict) -> None:
    if not binding:
        return
    payload = json.dumps({"answer": operation["answer"], "hint_level": operation["hint_level"]},
                         ensure_ascii=False, sort_keys=True)
    action_id = "mistake-listening-answer:" + operation["action_id"]
    row = connection.execute("SELECT status,answer_json,answer_action_id FROM diagnostic_prompt WHERE prompt_id=?",
                             (binding["prompt_id"],)).fetchone()
    if not row:
        from .agent_runtime import InvalidAgentPlanError
        raise InvalidAgentPlanError("ANSWER_LISTENING_DELIVERY_UNAVAILABLE")
    if row["answer_action_id"] and (row["answer_action_id"] != action_id or row["answer_json"] != payload):
        from .agent_runtime import InvalidAgentPlanError
        raise InvalidAgentPlanError("ANSWER_LISTENING_BINDING_CHANGED")
    connection.execute(
        """UPDATE diagnostic_prompt SET status='answer_saved',answer_json=?,answer_action_id=?,
           authorized_turn_id=?,updated_at=? WHERE prompt_id=?""",
        (payload, action_id, operation.get("authorized_turn_id"), utc_now(), binding["prompt_id"]),
    )


def settle(connection, binding: dict | None, *, reason: str = "mistake_dialogue_scored") -> None:
    if binding:
        connection.execute(
            "UPDATE diagnostic_prompt SET status='cancelled',reason=?,updated_at=? WHERE prompt_id=?",
            (reason, utc_now(), binding["prompt_id"]),
        )
