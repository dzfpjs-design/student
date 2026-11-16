"""Exact-prompt delivery admission, independent of score/mastery projections."""

import json


def scoped_listening_gate(c, child_id, item_id, requirement, *, session_id=None, prompt_id=None,
                         item_version=None, attempt_id=None, evidence_id=None, media_root=None,
                         data_encryption_key=None, as_of=None, answer=None):
    from .pet_media_evidence import _current, _date, _now, _hash, PetMediaEvidenceError
    from .pet_listening_scoring import listening_scoring_item
    from .learning_obligations import withdrawn_evidence_ids
    from .local_supply import LocalSupplyError

    def blocked(reason):
        return {"status": "unknown", "skill_scoring_allowed": False, "blockers": [reason]}

    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='pet_media_binding'").fetchone():
        return blocked("exact_prompt_media_unavailable")
    now = _date(as_of) if as_of else _now()
    if not session_id or not prompt_id or not item_version or media_root is None:
        return blocked("exact_prompt_and_managed_media_scope_required")
    prompt = c.execute("""SELECT * FROM diagnostic_prompt WHERE prompt_id=? AND child_id=?
        AND origin_session_id=? AND diagnostic_item_id=? AND item_version=?""",
        (prompt_id, child_id, session_id, item_id, item_version)).fetchone()
    if not prompt or prompt["status"] not in {"answer_saved", "answered"}:
        return blocked("original_saved_answer_required")
    attempt = None
    if attempt_id or evidence_id or prompt["status"] == "answered":
        attempt = c.execute("""SELECT da.*,le.occurred_at,le.evidence_summary AS summary FROM diagnostic_attempt da
            JOIN learning_evidence le ON le.evidence_id=da.evidence_id AND le.child_id=da.child_id AND le.session_id=da.session_id
            WHERE da.attempt_id=? AND da.child_id=? AND da.session_id=? AND da.diagnostic_item_id=?""",
            (prompt["attempt_id"], child_id, session_id, item_id)).fetchone()
        if (not attempt or (attempt_id and attempt["attempt_id"] != attempt_id)
                or (evidence_id and attempt["evidence_id"] != evidence_id)):
            return blocked("original_attempt_binding_conflict")
        if attempt["evidence_id"] in withdrawn_evidence_ids(c, child_id):
            return blocked("answer_evidence_withdrawn")
        marker = ";prompt_answer_saved_at="
        if marker not in attempt["summary"]:
            return blocked("original_answer_arrival_time_unavailable")
        cutoff = _date(attempt["summary"].partition(marker)[2].split(";", 1)[0])
        if cutoff > _date(attempt["occurred_at"]):
            return blocked("answer_time_conflict")
        original_answer = attempt["submitted_answer"]
    else:
        if not prompt["answer_json"] or not prompt["answer_action_id"]:
            return blocked("original_saved_answer_required")
        cutoff = _date(prompt["updated_at"])
        original_answer = json.loads(prompt["answer_json"])["answer"]
    if answer is not None and answer != original_answer:
        return blocked("original_answer_payload_conflict")
    if not _date(prompt["created_at"]) <= cutoff <= now:
        return blocked("answer_time_conflict")
    try:
        scoring = listening_scoring_item(c, child_id, item_id)
    except (LocalSupplyError, ValueError, TypeError, KeyError):
        return blocked("objective_original_scoring_unavailable")
    if not scoring:
        return blocked("objective_original_scoring_unavailable")
    if not requirement.get("version"):
        return blocked("original_task_playback_requirement_unavailable")
    delivery_prompt = dict(prompt)
    from .exam_listening_delivery import exam_owner, frozen
    if exam_owner(prompt):
        try:
            run,slot,source,unit = frozen(c,prompt_id)
            if (run['child_id'],run['session_id'],slot['diagnostic_item_id']) != (child_id,session_id,item_id):
                return blocked('exam_original_owner_conflict')
            delivery_prompt = dict(c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=?',(unit['prompt_id'],)).fetchone())
            if source['requirement'] != requirement:
                return blocked('exam_original_requirement_conflict')
        except (ValueError,TypeError,KeyError):
            return blocked('exam_original_source_unavailable')
    qualified = delivered_before(c, delivery_prompt, requirement, cutoff=cutoff,
        media_root=media_root, data_encryption_key=data_encryption_key)
    if not qualified:
        return blocked("reviewed_original_full_play_count_before_answer_unverified")
    evidence = {"prompt_id": prompt_id, "session_id": session_id, "item_version": item_version,
        "attempt_id": attempt["attempt_id"] if attempt else None,
        "evidence_id": attempt["evidence_id"] if attempt else None, "answered_at": cutoff.isoformat(),
        "delivery": qualified, "scoring_version": scoring["listening_scoring_version"],
        "requirement_version": requirement["version"]}
    return {"status": "qualified_for_objective_listening_scoring", "skill_scoring_allowed": True,
        "task_delivery_qualified": True, "blockers": [], "scope_evidence": evidence,
        "evidence_version": "pet_listening_scope_v1:" + _hash(evidence),
        "scope": "original_objective_exam_listening_only_not_official_score_or_mastery" if exam_owner(prompt)
            else "original_objective_listening_practice_only_not_pet_exam_or_mastery"}


def delivered_before(c, prompt, requirement, *, cutoff, media_root, data_encryption_key=None):
    """Current original-resource delivery only; never an answer/scoring permission."""
    from .pet_media_evidence import _current, _date, PetMediaEvidenceError
    if not requirement.get('version') or media_root is None:
        return []
    rows = c.execute("""SELECT * FROM pet_media_binding WHERE child_id=? AND session_id=? AND prompt_id=?
        AND diagnostic_item_id=? AND item_version=? AND kind='listening' ORDER BY created_at,media_id""",
        (prompt['child_id'], prompt['origin_session_id'], prompt['prompt_id'],
         prompt['diagnostic_item_id'], prompt['item_version'])).fetchall()
    qualified = []
    for raw in rows:
        row = dict(raw)
        try:
            _, _, _, audio, reasons = _current(c, row, media_root, data_encryption_key)
            state = json.loads(row["state_json"])
            if reasons or state["status"] == "cancelled" or state.get("delivery_requirement") != requirement:
                continue
            from .pet_listening_reuse import listening_source_review
            review = listening_source_review(c, row)
            if not review or json.loads(review["payload_json"])["operation"] != "approve" or _date(review["created_at"]) > cutoff:
                continue
            events = c.execute("""SELECT * FROM pet_media_event WHERE media_id=? AND operation IN ('playback','playback_settled')
                ORDER BY rowid""", (row["media_id"],)).fetchall()
            plays = {}
            for event in events:
                payload = json.loads(event["payload_json"])
                if (payload.get("event") == "ended" and payload.get("receipt_state", "accepted") == "accepted"
                        and payload.get("requirement_version") == requirement["version"]
                        and payload.get("audio_sha256") == audio["sha256"] and payload.get("frame_position") == audio["frames"]
                        and _date(review["created_at"]) <= _date(event["created_at"]) <= cutoff):
                    plays.setdefault(event["generation"], event["event_id"])
            if len(plays) >= requirement["required_play_count"]:
                qualified.append({"media_id": row["media_id"], "source_version": row["source_version"],
                    "source_review_event_id": review["event_id"], "completed_event_ids": list(plays.values()),
                    "audio_sha256": audio["sha256"]})
        except (PetMediaEvidenceError, ValueError, TypeError, KeyError):
            continue
    return qualified
