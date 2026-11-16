"""Read-only contextual teaching support; never a mastery certification."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import json

from .learning_obligations import project_child_mastery_evidence


VERSION = "teaching_context_v2"
EVIDENCE_TTL = timedelta(days=30)
RECENT_TTL = timedelta(hours=2)


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _time(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (ValueError, TypeError, AttributeError):
        return None


def _exists(c, table):
    return bool(c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def _unknown(reason):
    return {"status": "unknown", "reason": reason, "confidence": "unknown",
            "evidence_refs": [], "observed_at": None, "expires_at": None,
            "mastery_claim_allowed": False}


def _source(c, child, node, now):
    sources = [dict(r) for r in c.execute("""SELECT source_id,child_id,review_status,effective_from,
        effective_to,content_hash,version_label FROM knowledge_source WHERE source_ref=?
        ORDER BY source_id""", (node["source_ref"],))]
    applicable = [r for r in sources if r["child_id"] in (None, child["child_id"])]
    allowed = (not sources and node["source_ref"] == "hsc_original_v1") or bool(applicable)
    for row in applicable:
        try:
            start = date.fromisoformat(row["effective_from"]) if row["effective_from"] else None
            end = date.fromisoformat(row["effective_to"]) if row["effective_to"] else None
            allowed = allowed and row["review_status"] == "approved" and (
                start is None or start <= now.date()) and (end is None or now.date() <= end)
        except (ValueError, TypeError):
            allowed = False
    return allowed, {"knowledge_version": node["knowledge_version"], "node_status": node["status"],
                     "node_revision": _hash(dict(node)),
                     "source_revisions": applicable}


def _plan_node(c, plan):
    bindings = set()
    if plan["source_type"] == "due_review":
        row = c.execute("""SELECT knowledge_node_id FROM review_task WHERE review_task_id=? AND child_id=?
            AND status IN ('pending','completed')""",
                        (plan["source_ref"], plan["child_id"])).fetchone()
        return row[0] if row else False
    if plan["source_type"] == "mistake_review":
        row = c.execute("""SELECT knowledge_node_id FROM mistake_case WHERE mistake_id=? AND child_id=?
            AND status!='excluded' AND confirmed_at IS NOT NULL""", (plan["source_ref"], plan["child_id"])).fetchone()
        return row[0] if row else False
    if plan["source_type"] in {"pet_daily", "competition_math"}:
        item = c.execute("SELECT diagnostic_item_id FROM diagnostic_item WHERE diagnostic_item_id=?",
                         (plan["source_ref"],)).fetchone()
        if item:
            from .local_supply import LocalSupplyError, validate_item
            try:
                bindings.add(validate_item(c, plan["child_id"], item[0])["knowledge_node_id"])
            except LocalSupplyError:
                return False
    if not _exists(c, "plan_constraint_decision"):
        return next(iter(bindings)) if bindings else None
    for row in c.execute("""SELECT metadata_json FROM plan_constraint_decision
        WHERE plan_id=? AND obligation_type=? AND source_ref=? AND decision='scheduled'""",
        (plan["plan_id"], plan["source_type"], plan["source_ref"])):
        try:
            value = json.loads(row[0]).get("planning_evidence", {}).get("knowledge_node_id")
            if isinstance(value, str) and value:
                bindings.add(value)
        except (ValueError, AttributeError, TypeError):
            return False
    return next(iter(bindings)) if len(bindings) == 1 else False if bindings else None


def _recent(c, session_id, now):
    if not session_id:
        return _unknown("missing_session")
    row = c.execute("SELECT * FROM session_planning_context WHERE session_id=?", (session_id,)).fetchone()
    event = c.execute("""SELECT event_id,snapshot_json,created_at FROM planning_context_event
        WHERE session_id=? ORDER BY rowid DESC LIMIT 1""", (session_id,)).fetchone()
    if not row or not event:
        return _unknown("missing_recent_context")
    observed = _time(row["updated_at"])
    try:
        saved = json.loads(event["snapshot_json"])
        matches = saved.get("energy_level") == row["energy_level"] and event["created_at"] == row["updated_at"]
    except (ValueError, AttributeError, TypeError):
        matches = False
    if not matches or not observed or not observed <= now < observed + RECENT_TTL:
        return _unknown("expired_or_unverified_recent_context")
    return {"status": "reported", "energy_level": row["energy_level"],
            "reason": "current_session_self_report_not_ability", "confidence": "self_report_only",
            "evidence_refs": [event["event_id"]], "observed_at": observed.isoformat(),
            "expires_at": (observed + RECENT_TTL).isoformat(), "mastery_claim_allowed": False}


def build_teaching_context(connection, child_id: str, *, session_id: str | None = None,
        plan_item_id: str | None = None, subject: str | None = None,
        knowledge_node_id: str | None = None, task_type: str | None = None,
        now: datetime | None = None) -> dict:
    """Read existing records only; callers may share a read transaction.

    Caller-supplied node/task identify the current request, never a diagnosis.
    Persisted plan scope wins; mismatches fail closed. Unbound written/oral tasks
    cannot inherit diagnostic support. TTLs are conservative policy estimates.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("timezone-aware teaching context time required")
    now = now.astimezone(timezone.utc)
    c = connection
    if not c.in_transaction:
        c.execute("BEGIN")
        try:
            return build_teaching_context(c, child_id, session_id=session_id, plan_item_id=plan_item_id,
                subject=subject, knowledge_node_id=knowledge_node_id, task_type=task_type, now=now)
        finally:
            c.rollback()
    result = {"version": VERSION, "status": "unknown", "scope": {
        "child_id": child_id, "session_id": session_id, "plan_item_id": plan_item_id,
        "subject": subject, "knowledge_node_id": knowledge_node_id, "task_type": task_type},
        "learning": _unknown("missing_context"), "recent": _unknown("missing_context"),
        "corrections": [], "correction_history": [], "source_versions": {}, "excluded": [],
        "mastery_claim_allowed": False, "cloud_transmission_allowed": False,
        "freshness_policy": {"version": "support_freshness_v1", "estimated": True,
                             "learning_days": 30, "recent_hours": 2}}

    def finish(reason=None):
        if reason:
            result["status"] = "invalid_context"
            result["learning"] = _unknown(reason)
        result["source_version"] = VERSION + ":" + _hash(result)
        return result

    child = c.execute("SELECT * FROM child_profile WHERE child_id=? AND status='active'", (child_id,)).fetchone()
    if not child:
        return finish("child_unavailable")
    if session_id:
        session = c.execute("SELECT * FROM daily_session WHERE session_id=? AND child_id=?",
                            (session_id, child_id)).fetchone()
        if not session or session["status"] != "active" or session["state"] in ("evolving", "completed"):
            return finish("session_unavailable")
    elif plan_item_id:
        return finish("plan_requires_session")
    family = task_type if task_type in {"diagnostic", "review", "mistake_review"} else None
    if plan_item_id:
        plan = c.execute("""SELECT pi.*,dp.child_id,dp.session_id,dp.status AS plan_status
            FROM plan_item pi JOIN daily_plan dp USING(plan_id) WHERE pi.plan_item_id=?""", (plan_item_id,)).fetchone()
        if (not plan or plan["child_id"] != child_id or plan["session_id"] != session_id
                or plan["plan_status"] != "active" or plan["status"] not in ("pending", "active", "paused")):
            return finish("plan_unavailable")
        if (subject and subject != plan["subject"]) or (task_type and task_type != plan["task_type"]):
            return finish("plan_scope_mismatch")
        subject, task_type = plan["subject"], plan["task_type"]
        bound_node = _plan_node(c, plan)
        if bound_node is False or (bound_node and knowledge_node_id and bound_node != knowledge_node_id):
            return finish("plan_node_mismatch")
        knowledge_node_id = bound_node or knowledge_node_id
        family = {"due_review": "review", "mistake_review": "mistake_review",
                  "junior_exam_goal": "diagnostic", "pet_daily": "diagnostic",
                  "competition_math": "diagnostic"}.get(plan["source_type"]) if bound_node else None
        result["source_versions"]["plan"] = _hash(dict(plan))
    result["scope"].update(subject=subject, knowledge_node_id=knowledge_node_id, task_type=task_type,
                            evidence_task_family=family)
    node = c.execute("SELECT * FROM knowledge_node WHERE knowledge_node_id=?", (knowledge_node_id,)).fetchone() if knowledge_node_id else None
    from .learning_grade_scope import allows_learning_grade
    if knowledge_node_id and (not node or node["subject"] != subject
            or not allows_learning_grade(child['grade'], node['grade_band'], node['track'])):
        return finish("node_scope_mismatch")
    available = False
    if node:
        available, revision = _source(c, child, node, now)
        available = available and node["status"] == "active"
        result["source_versions"]["node"] = revision
    if node and family:
        projection = project_child_mastery_evidence(c, child_id, as_of=now.isoformat()).get(knowledge_node_id)
        from .teaching_modality_evidence import project_reviewed_teaching_evidence
        reviewed = project_reviewed_teaching_evidence(c, child_id, knowledge_node_id, now)
        reviewed_ids = {row['evidence_id'] for row in reviewed['effective_evidence']}
        result['source_versions']['reviewed_modality'] = reviewed['evidence_fingerprint']
        if projection:
            result["source_versions"]["qualification"] = projection["evidence_fingerprint"]
            result["excluded"] = [row for row in projection["excluded_evidence"] if row['evidence_id'] not in reviewed_ids]
        result['excluded'].extend(reviewed['excluded_evidence'])
        effective = {row['evidence_id']: row for row in (projection or {}).get('effective_evidence', [])}
        effective.update({row['evidence_id']: row for row in reviewed['effective_evidence']})
        rows = []
        for row in effective.values():
            row_family = "mistake_review" if row["receipt_kind"] == "mistake" else "review" if row["review_task_id"] else "diagnostic"
            if row_family == family:
                rows.append(row)
        rows.sort(key=lambda row: _time(row["occurred_at"]))
        latest = rows[-1] if rows else None
        if not available:
            result["learning"] = _unknown("source_unavailable")
        elif not latest:
            result["learning"] = _unknown("no_qualified_context_evidence")
        elif now >= _time(latest["occurred_at"]) + EVIDENCE_TTL:
            result["learning"] = _unknown("evidence_expired")
        else:
            status = "recheck_needed" if latest["correctness"] < 1 else "supported_only" if latest["hint_level"] or not latest["independent"] else "independent_observed"
            expires = _time(latest["occurred_at"]) + EVIDENCE_TTL
            for source in result["source_versions"]["node"]["source_revisions"]:
                if source["effective_to"]:
                    end = datetime.combine(date.fromisoformat(source["effective_to"]), datetime.min.time(), timezone.utc)
                    expires = min(expires, end + timedelta(days=1))
            result["learning"] = {"status": status, "reason": "latest_qualified_context_response_not_mastery",
                "confidence": "qualified_single_response", "evidence_refs": [latest["evidence_id"]],
                "observed_at": latest["occurred_at"], "expires_at": expires.isoformat(),
                "mastery_claim_allowed": False}
            if latest.get('review_refs'):
                result['learning'].update(review_refs=latest['review_refs'], score_authority=latest['score_authority'])
            result["status"] = "evidence_available"
    result["recent"] = _recent(c, session_id, now)
    if _exists(c, "child_model_correction"):
        for raw in c.execute("""SELECT correction_id,target_key,corrected_value_json,status,created_at,
            expires_at FROM child_model_correction WHERE child_id=? AND target_key IN
            ('explanation_pacing','interaction_tone') ORDER BY rowid""", (child_id,)):
            row = dict(raw)
            created, expires = _time(row["created_at"]), _time(row["expires_at"])
            active = row["status"] == "active" and created and created <= now and (
                row["expires_at"] is None or expires and now < expires)
            row["effective"] = bool(active)
            row["corrected_value"] = json.loads(row.pop("corrected_value_json"))
            result["correction_history"].append(row)
            if active:
                result["corrections"].append({**row, "confidence": "explicit_parent_preference_not_ability"})
    return finish()
