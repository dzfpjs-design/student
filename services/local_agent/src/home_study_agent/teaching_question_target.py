"""Exact local question references. Resolving identity grants no cloud permission."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json

from .diagnostic_prompt import DiagnosticPromptService, _ITEM_FIELDS, _version
from .teaching_context import _exists, _hash, _source, _time


VERSION = "teaching_question_target_v1"
_IDENTITY_KEYS = ("version", "child_id", "session_id", "subject", "knowledge_node_id",
                  "task_type", "binding", "question_hash", "source_fingerprint", "expires_at")


def _question(value):
    # Do not remove punctuation, change case, compare keywords, or normalize math.
    return value.replace("\r\n", "\n").strip() if isinstance(value, str) else ""


def _digest(target):
    return _hash({key: target.get(key) for key in _IDENTITY_KEYS})


def _content_boundary(c, child_id, item, node, node_source):
    # Match the existing rubric's original-only boundary, but also verify content.
    # Source approval/identity is not consent to transmit local learning material.
    if item["source_ref"] != "hsc_original_v1":
        from .reviewed_tutor_projection import eligible_reviewed_source, POLICY_VERSION
        from .local_supply import LocalSupplyError
        try:
            if eligible_reviewed_source(c, child_id, dict(item)):
                return False, POLICY_VERSION
        except (LocalSupplyError, ValueError, TypeError, KeyError):
            pass
        return True, "diagnostic_source_local_or_unknown"
    if node["source_ref"] != "hsc_original_v1" or node_source["source_revisions"]:
        return True, "node_source_local_or_unknown"
    from .local_supply import LocalSupplyError, validate_item
    try:
        validate_item(c, child_id, item["diagnostic_item_id"])
    except (LocalSupplyError, ValueError, TypeError, KeyError):
        return True, "shipped_original_content_not_verified"
    return False, "verified_shipped_original_runtime_checks_required"


def _ocr(c, session, confirmation_id, question, now):
    from .tutor_ocr_confirmation import CONFIRMATION_TTL

    if not _exists(c, "tutor_ocr_confirmation"):
        return None, "ocr_unavailable"
    row = c.execute("SELECT * FROM tutor_ocr_confirmation WHERE confirmation_id=? AND child_id=? AND session_id=?",
                    (confirmation_id, session["child_id"], session["session_id"])).fetchone()
    if not row or row["revoked_at"]:
        return None, "ocr_unavailable"
    created, expires = _time(row["confirmed_at"]), _time(row["expires_at"])
    if not created or not expires or not created <= now < expires or expires > created + CONFIRMATION_TTL:
        return None, "ocr_expired_or_invalid_time"
    if (hashlib.sha256(row["confirmed_text"].encode()).hexdigest() != row["confirmed_text_hash"]
            or _question(row["confirmed_text"]) != question):
        return None, "ocr_question_mismatch"
    artifact = c.execute("""SELECT artifact_id,child_id,session_id,artifact_type,extracted_text,
        details_json,status,created_at FROM capture_artifact WHERE artifact_id=? AND child_id=?""",
        (row["artifact_id"], session["child_id"])).fetchone()
    if (not artifact or artifact["artifact_type"] != "document_ocr" or not artifact["extracted_text"]
            or artifact["status"] in {"deleted", "withdrawn", "revoked"}):
        return None, "ocr_artifact_unavailable"
    if artifact['session_id'] != session['session_id']:
        from .mistake_source_revision import artifact_binding
        try:
            revision = artifact_binding(c, artifact['artifact_id'], session['child_id'], question, session['session_id'])
            if not revision or revision != json.loads(row['formula_review_json'] or 'null'):
                return None, 'ocr_artifact_unavailable'
        except (ValueError, TypeError, KeyError):
            return None, 'ocr_artifact_unavailable'
    artifact_created = _time(artifact["created_at"])
    if not artifact_created or artifact_created > created:
        return None, "ocr_artifact_time_invalid"
    if row["confirmation_kind"] not in {"ocr_verified", "manual_correction"}:
        return None, "ocr_confirmation_kind_invalid"
    if row["confirmation_kind"] == "ocr_verified":
        from .tutor_ocr_confirmation import normalize_confirmation_text
        allowed = {normalize_confirmation_text(artifact["extracted_text"])}
        try:
            candidates = json.loads(artifact["details_json"] or "{}").get("local_pipeline", {}).get("wrong_question_candidates", [])
            for candidate in candidates:
                if (isinstance(candidate, dict) and candidate.get("evidence") == "red_correction_mark_near_question"
                        and candidate.get("requires_child_confirmation") is True and float(candidate.get("confidence", 0)) >= .72
                        and isinstance(candidate.get("text"), str)):
                    allowed.add(normalize_confirmation_text(candidate["text"]))
        except (ValueError, TypeError, AttributeError):
            return None, "ocr_artifact_invalid"
        if normalize_confirmation_text(row["confirmed_text"]) not in allowed:
            return None, "ocr_artifact_changed"
    return {"fingerprint": _hash({"confirmation": dict(row), "artifact": dict(artifact)}),
            "expires_at": expires.isoformat()}, None


def resolve_question_target(connection, session_id: str, *, subject: str, question: str,
        prompt_id: str | None = None, item_version: str | None = None,
        parent_turn_id: str | None = None, ocr_confirmation_id: str | None = None,
        now: datetime | None = None) -> dict:
    """Read one frozen typed prompt, or a persisted Tutor parent's original binding.

    No active-task/prompt fallback. OCR alone identifies confirmed text, not a node.
    `question` must be the actual Tutor question after OCR confirmation resolution.
    Pass the returned audit unchanged to local_teaching_control persistence only.
    Block new cloud calls when question_content_cloud_blocked is true. False means
    no additional source restriction, NOT authorization: retain all existing
    runtime consent, OCR and text-privacy checks. Never transmit this audit.
    """
    c = connection
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("timezone-aware question-target time required")
    if not c.in_transaction:
        c.execute("BEGIN")
        try:
            return resolve_question_target(c, session_id, subject=subject, question=question, prompt_id=prompt_id,
                item_version=item_version, parent_turn_id=parent_turn_id, ocr_confirmation_id=ocr_confirmation_id, now=now)
        finally:
            c.rollback()
    question = _question(question)
    result = {"version": VERSION, "status": "unknown", "reason_code": "no_explicit_question_reference",
        "child_id": None, "session_id": session_id, "subject": subject, "knowledge_node_id": None,
        "task_type": None, "binding": None, "question_hash": _hash(question), "source_fingerprint": None,
        "expires_at": None, "fingerprint": None, "parent_turn_id": parent_turn_id,
        "confidence": "unknown", "validity": "revalidate_each_request", "mastery_claim_allowed": False,
        "question_content_cloud_blocked": False,
        "question_content_cloud_reason": "unbound_requires_existing_privacy_checks",
        "local_only": True, "cloud_transmission_allowed": False, "cloud_authorization_granted": False}

    def fail(reason, status="invalid"):
        result.update(status=status, reason_code=reason, knowledge_node_id=None, task_type=None, binding=None,
                      source_fingerprint=None, fingerprint=None, confidence="unknown", expires_at=None)
        if status == "invalid":
            result.update(question_content_cloud_blocked=True,
                          question_content_cloud_reason="question_reference_invalid")
        return result

    if not question or not isinstance(subject, str) or not subject.strip():
        return fail("question_and_subject_required")
    for value in (session_id, prompt_id, item_version, parent_turn_id, ocr_confirmation_id):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            return fail("invalid_reference")
    if (prompt_id is None) != (item_version is None):
        return fail("incomplete_prompt_binding")
    session = c.execute("""SELECT ds.*,cp.grade FROM daily_session ds JOIN child_profile cp USING(child_id)
        WHERE ds.session_id=? AND cp.status='active'""", (session_id,)).fetchone()
    if not session or session["status"] != "active" or session["state"] in {"evolving", "completed"}:
        return fail("session_unavailable")
    result["child_id"] = session["child_id"]
    inherited = None
    parent_created = None
    unbound_parent = None
    if parent_turn_id:
        parent = c.execute("SELECT * FROM tutor_turn WHERE turn_id=? AND child_id=? AND session_id=?",
                           (parent_turn_id, session["child_id"], session_id)).fetchone()
        # The host persists answer_leak_replaced only after replacing both speech
        # and board with a safe hint. This permits identity continuity, not a node
        # inference, teaching-effect receipt, or permission for other safety states.
        if (not parent or parent["status"] != "completed" or parent["safety_status"] not in {"passed", "answer_leak_replaced"}
                or not _question(parent["answer"])
                or parent["subject"] != subject or _question(parent["question"]) != question):
            return fail("parent_question_mismatch")
        created = _time(parent["created_at"])
        if not created or created > now:
            return fail("parent_time_invalid")
        parent_created = created
        try:
            inherited = json.loads(parent["output_envelope_json"] or "{}").get("local_teaching_control", {}).get("question_target")
        except (ValueError, AttributeError, TypeError):
            return fail("parent_target_invalid")
        unknown = (isinstance(inherited, dict) and inherited.get("version") == VERSION
                   and inherited.get("status") == "unknown" and not inherited.get("binding")
                   and not inherited.get("knowledge_node_id") and not inherited.get("task_type"))
        if inherited is None or unknown:
            unbound_parent = "legacy_parent_without_target" if inherited is None else "parent_without_verified_target"
            if unknown and inherited.get("question_content_cloud_blocked") is True:
                result.update(question_content_cloud_blocked=True,
                              question_content_cloud_reason="parent_content_restriction")
            if ocr_confirmation_id is not None and ocr_confirmation_id != parent["ocr_confirmation_id"]:
                return fail("parent_reference_mismatch")
            prompt_id, item_version = None, None
            ocr_confirmation_id = parent["ocr_confirmation_id"]
        else:
            if (not isinstance(inherited, dict) or inherited.get("version") != VERSION or inherited.get("status") != "resolved"
                    or inherited.get("fingerprint") != _digest(inherited) or not isinstance(inherited.get("binding"), dict)):
                return fail("parent_target_invalid")
            binding = inherited["binding"]
            if (prompt_id is not None and (prompt_id, item_version) != (binding.get("prompt_id"), binding.get("item_version"))
                    or ocr_confirmation_id is not None and ocr_confirmation_id != binding.get("ocr_confirmation_id")
                    or parent["ocr_confirmation_id"] != binding.get("ocr_confirmation_id")):
                return fail("parent_reference_mismatch")
            prompt_id, item_version = binding.get("prompt_id"), binding.get("item_version")
            ocr_confirmation_id = binding.get("ocr_confirmation_id")
            if not isinstance(prompt_id, str) or not isinstance(item_version, str):
                return fail("parent_target_invalid")
        if ocr_confirmation_id is not None and not isinstance(ocr_confirmation_id, str):
            return fail("parent_target_invalid")
    ocr = None
    if ocr_confirmation_id:
        ocr, error = _ocr(c, session, ocr_confirmation_id, question, now)
        if error:
            return fail(error)
    if not prompt_id:
        return fail(unbound_parent or ("ocr_without_original_reference" if ocr else "no_explicit_question_reference"), "unknown")
    if not _exists(c, "diagnostic_prompt"):
        return fail("prompt_unavailable")
    row = c.execute("SELECT * FROM diagnostic_prompt WHERE prompt_id=? AND child_id=? AND origin_session_id=?",
                    (prompt_id, session["child_id"], session_id)).fetchone()
    if not row or row["status"] not in {"waiting_answer", "answer_saved", "answered"}:
        return fail("prompt_unavailable")
    created = _time(row["created_at"])
    if not created or created > now or (parent_created and parent_created < created) or row["item_version"] != item_version:
        return fail("prompt_version_or_time_mismatch")
    item = DiagnosticPromptService._item(c, session["child_id"], row["diagnostic_item_id"])
    if not item or _version(item) != item_version:
        return fail("item_version_or_source_changed")
    if item["subject"] != subject or _question(item["prompt"]) != question:
        return fail("question_or_subject_mismatch")
    try:
        saved = json.loads(row["item_json"])
        if saved != {key: item[key] for key in _ITEM_FIELDS}:
            return fail("prompt_snapshot_mismatch")
        requested = json.loads(row["request_json"])
        if requested.get("track") != item["track"] or requested.get("subject") not in (None, subject):
            return fail("prompt_request_mismatch")
        if row["status"] != "answered" and DiagnosticPromptService._scheduled_source_reason(c, row):
            return fail("scheduled_source_changed")
    except (ValueError, TypeError, AttributeError):
        return fail("prompt_snapshot_mismatch")
    if row["status"] == "answered" and DiagnosticPromptService._receipt(c, row) is None:
        return fail("answered_receipt_unavailable")
    node = c.execute("SELECT * FROM knowledge_node WHERE knowledge_node_id=?", (item["knowledge_node_id"],)).fetchone()
    available, node_source = _source(c, session, node, now)
    if not available:
        return fail("node_source_unavailable")
    review = None
    if row["review_task_id"]:
        review = DiagnosticPromptService._review(c, session["child_id"], row["review_task_id"], allowed_statuses=("pending", "completed"))
        if not review or _version(review) != row["review_version"] or review["diagnostic_item_id"] != item["diagnostic_item_id"]:
            return fail("review_source_changed")
    binding = {"prompt_id": prompt_id, "item_version": item_version, "diagnostic_item_id": item["diagnostic_item_id"],
               "review_task_id": row["review_task_id"], "ocr_confirmation_id": ocr_confirmation_id}
    result.update(status="resolved", reason_code="exact_persisted_question_reference", knowledge_node_id=item["knowledge_node_id"],
        task_type="review" if review else "diagnostic", binding=binding, confidence="verified_question_identity_not_ability",
        expires_at=ocr["expires_at"] if ocr else None, source_fingerprint=_hash({
            "prompt_id": prompt_id, "created_at": row["created_at"], "request": requested, "item_version": item_version,
            "node_source": node_source, "review_version": row["review_version"], "ocr": ocr}))
    blocked, reason = _content_boundary(c, session["child_id"], item, node, node_source)
    from .reviewed_tutor_projection import POLICY_VERSION, source_receipt
    if reason == POLICY_VERSION:
        result['source_fingerprint'] = _hash({
            'source_fingerprint': result['source_fingerprint'], 'projection_policy': POLICY_VERSION,
            'reviewed_source': source_receipt(c, item)})
    result["fingerprint"] = _digest(result)
    if inherited and result["fingerprint"] != inherited["fingerprint"]:
        return fail("parent_source_changed")
    result.update(question_content_cloud_blocked=blocked, question_content_cloud_reason=reason)
    return result
