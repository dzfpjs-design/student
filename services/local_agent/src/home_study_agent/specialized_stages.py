"""Read-only, sampled-question stage evidence. Never an exam certificate.

Scores come exclusively from the shared effective-receipt projection. This
module adds ordering and source constraints; it does not score or create work.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

from .learning_obligations import project_child_mastery_evidence, question_identity, _question_catalog, _tutor_exposure_index
from .local_supply import LocalSupplyError, validate_item, digest
from .teaching_context import _exists, _source, _time


VERSION = "specialized_stages_v2"
STAGES = ("baseline", "near_transfer", "far_transfer", "delayed_retention")
POLICY = {"version": VERSION, "evidence_window_days": 90, "retention_delay_days": 7,
          "checks_per_stage": 1, "qualification_scope": "sampled_questions_not_whole_domain",
          "threshold_basis": "conservative_engineering_policy_not_exam_calibration",
          "transfer_source": "explicit_parent_reviewed_variant_stage",
          "expired_evidence_action": "recheck_not_forget", "automatic_pressure_increase": False}


def _item_source(c, child_id, raw, now):
    try:
        item = validate_item(c, child_id, raw["diagnostic_item_id"])
        rubric = json.loads(item["expected_answer_json"])
        identity = question_identity(item, connection=c)
    except (LocalSupplyError, ValueError, TypeError, KeyError):
        return None, "source_unavailable"
    rule = (rubric.get("local_rule") == "local_exact_v1" and bool(rubric.get("exact_answers"))
            and not rubric.get("keyword_groups") and not rubric.get("task_points")
            and item.get("assessment_mode") != "pet_reference_rubric")
    from .pet_audio_rubric import is_speaking
    from .pet_skill_gate import pet_skill_gate
    modality = pet_skill_gate(c, child_id, item['diagnostic_item_id'])['modality']
    if modality == 'listening':
        from .pet_listening_scoring import listening_scoring_item
        rule = listening_scoring_item(c, child_id, item['diagnostic_item_id']) is not None
    elif is_speaking(c, item['diagnostic_item_id']):
        # The question can be reviewed; only a bound human review supplies a score.
        rule = True
    elif item['answer_type'] == 'open_response' and rubric.get('response_mode') == 'spoken_explanation':
        from .open_response_profile import profile_for_item
        node = c.execute('SELECT * FROM knowledge_node WHERE knowledge_node_id=?',
                         (item['knowledge_node_id'],)).fetchone()
        try:
            profile_for_item(item, dict(node) if node else {})
            # Source eligibility only. Original voice and current score authority
            # are still required by school_spoken_score after a real response.
            rule = True
        except (ValueError, TypeError, KeyError):
            return None, 'spoken_source_criteria_unavailable'
    else:
        from .writing_rubric import is_writing
        if is_writing(c, item['diagnostic_item_id']):
            from .writing_response_origin import writing_item
            try:
                writing_item(c, child_id, item['diagnostic_item_id'])
                rule = True
            except (ValueError, TypeError, KeyError):
                return None, 'writing_source_criteria_unavailable'
    stage, root, reviewed_at, source = "baseline", item["diagnostic_item_id"], None, item["source_ref"] or ""
    metadata = {"item": item}
    if source.startswith('parent_approved_first:'):
        from .curriculum_first_source import reviewed_baseline
        try:
            first = reviewed_baseline(c, child_id, item, now)
        except LocalSupplyError as error:
            return None, str(error)
        metadata['first_source'] = first
        reviewed_at = _time(first['reviewed_at'])
    elif source.startswith(("parent_approved_", "local_verified_")):
        variant = source.startswith(("parent_approved_variant:", "local_verified_variant:"))
        table = "variant_candidate" if variant else "specialized_supply_candidate"
        candidate = c.execute(f"SELECT * FROM {table} WHERE candidate_id=? AND diagnostic_item_id=? AND child_id=?",
                              (source.partition(":")[2], item["diagnostic_item_id"], child_id)).fetchone()
        metadata["candidate"] = dict(candidate)
        reviewed_at = _time(candidate["reviewed_at"])
        if not reviewed_at or reviewed_at > now:
            return None, "source_review_date_invalid"
        if variant:
            parent = c.execute("SELECT diagnostic_item_id FROM mistake_case WHERE mistake_id=? AND child_id=?",
                               (candidate["mistake_id"], child_id)).fetchone()
            root = parent[0] if parent else None
            stage = candidate["stage"] if (source.startswith("parent_approved_variant:")
                and candidate["review_note"] and candidate["stage"] in {"near_transfer", "far_transfer"}) else None
        else:
            root = candidate["seed_item_id"]
            stage = "baseline" if candidate["stage"] == "parallel" else None
            from .stage_source_review import reviewed_stage
            try:
                explicit = reviewed_stage(c, child_id, candidate)
            except (LocalSupplyError, ValueError, KeyError):
                return None, 'stage_review_source_changed'
            if explicit:
                stage = explicit['stage']
                metadata['stage_review'] = explicit
    return {"diagnostic_item_id": item["diagnostic_item_id"], "question_identity": identity,
            "source_version": digest(metadata), "stage": stage, "source_root_item_id": root,
            "reviewed_at": reviewed_at.isoformat() if reviewed_at else None, "score_qualified": rule,
            "assessment_modality": modality}, None


def _history(c, child_id, now):
    catalog, identities = _question_catalog(c)
    first = {}
    for table in ("diagnostic_attempt", "mistake_attempt"):
        for row in c.execute(f"SELECT diagnostic_item_id,evidence_id,created_at FROM {table} WHERE child_id=? ORDER BY rowid", (child_id,)):
            at = _time(row["created_at"])
            identity = identities.get(row["diagnostic_item_id"])
            if at and at <= now and identity and (identity not in first or at < first[identity][0]):
                first[identity] = (at, row["evidence_id"])
    exact, bound = _tutor_exposure_index(c, child_id, catalog, identities)
    for item_id, item in catalog.items():
        identity = identities.get(item_id)
        matches = list(bound.get(identity, []))
        if not item["stimulus_text"] and not item["stimulus_asset"]:
            matches += exact.get(" ".join(item["prompt"].split()), [])
        for _, at, turn in matches:
            if identity and at <= now and (identity not in first or at <= first[identity][0]):
                first[identity] = (at, "tutor:" + turn["turn_id"])
    return first


def _node(c, child, node, domain_id, projection, first, now):
    available, source_basis = _source(c, child, node, now)
    raw_items = []
    for raw in c.execute("SELECT * FROM diagnostic_item WHERE knowledge_node_id=? ORDER BY diagnostic_item_id", (node["knowledge_node_id"],)):
        source = raw["source_ref"] or ""
        if source.startswith('parent_approved_first:'):
            if not _exists(c, 'curriculum_first_source') or not c.execute(
                'SELECT 1 FROM curriculum_first_source WHERE child_id=? AND candidate_id=? AND diagnostic_item_id=?',
                (child['child_id'], source.partition(':')[2], raw['diagnostic_item_id'])).fetchone():
                continue
        elif source.startswith(("parent_approved_", "local_verified_")):
            variant = source.startswith(("parent_approved_variant:", "local_verified_variant:"))
            table = "variant_candidate" if variant else "specialized_supply_candidate"
            if not _exists(c, table) or not c.execute(f"SELECT 1 FROM {table} WHERE candidate_id=? AND child_id=?",
                (source.partition(":")[2], child["child_id"])).fetchone():
                continue
        raw_items.append(dict(raw))
    sources, source_rejections = {}, []
    for raw in raw_items:
        source, reason = _item_source(c, child["child_id"], raw, now)
        if source:
            sources[raw["diagnostic_item_id"]] = source
        else:
            source_rejections.append({"diagnostic_item_id": raw["diagnostic_item_id"], "reason": reason})
    blockers = []
    if not available or node["status"] != "active":
        blockers.append("node_source_unavailable")
    from .specialized_stage_evidence import reviewed_audio_receipts, reviewed_writing_receipts
    audio, audio_rejected = reviewed_audio_receipts(c, child['child_id'], node['knowledge_node_id'], sources, first, now)
    writing, writing_rejected = reviewed_writing_receipts(c, child['child_id'], node['knowledge_node_id'], sources, first, now)
    if domain_id.startswith('pet.writing.') and not writing:
        blockers.append('writing_original_scoring_receipt_unavailable')
    receipts = {r['evidence_id']: r for r in projection.get('effective_evidence', [])}
    receipts.update({r['evidence_id']: r for r in audio + writing})
    eligible = sorted(receipts.values(), key=lambda r: (_time(r["occurred_at"]), r["evidence_id"]))
    gates = {stage: {"status": "missing", "evidence_refs": [], "diagnostic_item_ids": [],
                     "question_identities": [], "observed_at": None, "review_refs": []} for stage in STAGES}
    reviewed_ids = {r['evidence_id'] for r in audio + writing}
    rejected = [r for r in projection.get('excluded_evidence', []) if r['evidence_id'] not in reviewed_ids] + audio_rejected + writing_rejected
    if projection.get('excluded_evidence') and not eligible and any(s['assessment_modality'] == 'listening' for s in sources.values()):
        blockers.append('listening_delivery_receipt_unavailable')
    if audio_rejected and not audio:
        blockers.append('speaking_audio_scoring_receipt_unavailable')
    used, root, anchor, reset_refs = set(), None, None, []
    cutoff = now - timedelta(days=POLICY["evidence_window_days"])
    for receipt in eligible:
        at = _time(receipt["occurred_at"])
        source = sources.get(receipt["diagnostic_item_id"])
        reason = None
        if at < cutoff:
            reason = "expired_recheck_required"
        elif not source or not source["score_qualified"] or blockers:
            reason = "source_or_modality_unqualified"
        elif source["reviewed_at"] and at < _time(source["reviewed_at"]):
            reason = "answer_precedes_source_review"
        elif receipt["correctness"] != 1 or not receipt["independent"] or receipt["hint_level"]:
            for gate in gates.values():
                gate.update(status="missing", evidence_refs=[], diagnostic_item_ids=[], question_identities=[], observed_at=None, review_refs=[])
            used, root, anchor = set(), None, None
            reset_refs.append(receipt["evidence_id"])
            reason = "counterevidence_or_support_recheck"
        else:
            stage = next((s for s in STAGES if not gates[s]["evidence_refs"]), None)
            identity = receipt["question_identity"]
            if stage is None:
                continue
            if stage == "delayed_retention":
                far_refs = gates["far_transfer"]["evidence_refs"]
                due = anchor + timedelta(days=POLICY["retention_delay_days"])
                if (not receipt.get("review_task_id") or receipt.get("review_source_evidence_id") not in far_refs
                        or not _time(receipt.get("review_due_at")) or at < max(due, _time(receipt["review_due_at"]))):
                    reason = "bound_delayed_review_not_due_or_missing"
            elif source["stage"] != stage:
                reason = "stage_source_not_explicit_or_out_of_order"
            elif stage != "baseline" and (source["source_root_item_id"] != root or identity in used
                    or first.get(identity, (None, None))[1] != receipt["evidence_id"] or at <= anchor):
                reason = "transfer_not_new_or_source_chain_mismatch"
            if not reason:
                gates[stage].update(status="observed", evidence_refs=[receipt["evidence_id"]],
                    diagnostic_item_ids=[receipt["diagnostic_item_id"]], question_identities=[identity], observed_at=receipt["occurred_at"],
                    review_refs=receipt.get('review_refs', []))
                used.add(identity)
                root = source["source_root_item_id"] if stage == "baseline" else root
                anchor = at
        if reason:
            rejected.append({"evidence_id": receipt["evidence_id"], "reason": reason})
    current = next((s for s in STAGES if not gates[s]["evidence_refs"]), "maintenance")
    qualified = next((s for s in reversed(STAGES) if gates[s]["evidence_refs"]), None)
    due_at = (_time(gates["far_transfer"]["observed_at"]) + timedelta(days=POLICY["retention_delay_days"])).isoformat() if gates["far_transfer"]["observed_at"] else None
    choices = []
    for source in sources.values():
        if not source["score_qualified"] or source["stage"] != current:
            continue
        if current != "baseline" and (source["source_root_item_id"] != root or source["question_identity"] in first):
            continue
        if current == "baseline" and source["question_identity"] in first:
            continue
        choices.append(source)
    review_choices = []
    if current == "delayed_retention":
        far_ref = gates["far_transfer"]["evidence_refs"][0]
        for r in c.execute("SELECT * FROM review_task WHERE child_id=? AND knowledge_node_id=? AND source_evidence_id=? AND status='pending'",
                           (child["child_id"], node["knowledge_node_id"], far_ref)):
            source = sources.get(r["diagnostic_item_id"])
            due = _time(r["due_at"])
            if source and source["score_qualified"] and due:
                review_choices.append({**source, "review_task_id": r["review_task_id"],
                    "not_before": max(due, _time(due_at)).isoformat()})
        choices = sorted(review_choices, key=lambda r: (r["not_before"], r["review_task_id"]))
    if not any(s["score_qualified"] for s in sources.values()):
        blockers.append("qualified_scoring_source_missing")
    if not choices and current != "maintenance":
        blockers.append("bound_retention_review_missing" if current == "delayed_retention" else "fresh_stage_source_missing")
    # A missing writing score blocks certification, not the first writing task.
    dispatch_blockers = [b for b in blockers if b != 'writing_original_scoring_receipt_unavailable']
    selected = choices[0] if choices and not dispatch_blockers else None
    not_before = selected.get("not_before") if selected else due_at if current == "delayed_retention" else None
    waiting = bool(not_before and now < _time(not_before))
    execution = "blocked" if not selected else "waiting_due" if waiting else "ready"
    action = {"knowledge_node_id": node["knowledge_node_id"], "domain_id": domain_id,
        "child_id": child["child_id"], "track": domain_id.split(".")[0],
        "stage": current, "execution_status": execution,
        "diagnostic_item_id": selected["diagnostic_item_id"] if selected else None,
        "source_version": selected["source_version"] if selected else None,
        "review_task_id": selected.get("review_task_id") if selected else None,
        "not_before": not_before, "blockers": sorted(set(dispatch_blockers)), "mastery_claim_allowed": False}
    action["stage_constraint"] = {"version": VERSION, "current_stage": current, "qualified_stage": qualified,
        "prerequisite_version": digest({"policy": POLICY, "node": node["knowledge_node_id"],
            "gates": gates, "counterevidence_refs": reset_refs}), "blockers": action["blockers"]}
    result = {"knowledge_node_id": node["knowledge_node_id"], "current_stage": current, "qualified_stage": qualified,
        "status": "retention_observed" if current == "maintenance" else "recheck_needed" if reset_refs and qualified is None
                  else "blocked" if dispatch_blockers else "waiting_due" if waiting else "ready_for_check",
        "gates": gates, "next_action": action, "blockers": sorted(set(blockers)), "retention_due_at": due_at,
        "counterevidence_refs": reset_refs, "excluded_evidence": rejected,
        "source_rejections": source_rejections, "qualification_scope": POLICY["qualification_scope"],
        "mastery_claim_allowed": False, "certification_claim_allowed": False}
    result["evidence_fingerprint"] = digest({"version": VERSION, "result": result, "node_source": source_basis,
        "learning": projection.get("evidence_fingerprint"), "sources": sources,
        "exposure_history": {s["question_identity"]: (first[s["question_identity"]][0].isoformat(), first[s["question_identity"]][1])
                             for s in sources.values() if s["question_identity"] in first},
        "source_inventory": digest(raw_items)})
    action["stage_constraint"]["evidence_fingerprint"] = result["evidence_fingerprint"]
    return result


def _aggregate(rows):
    current = min((r["current_stage"] for r in rows), key=lambda s: (*STAGES, "maintenance").index(s)) if rows else "baseline"
    qualified = next((s for s in reversed(STAGES) if rows and all(
        r["qualified_stage"] is not None and STAGES.index(r["qualified_stage"]) >= STAGES.index(s) for r in rows)), None)
    return {"current_stage": current, "qualified_stage": qualified,
            "blockers": sorted({b for r in rows for b in r["blockers"]}) if rows else ["knowledge_target_missing"],
            "mastery_claim_allowed": False, "certification_claim_allowed": False}


def project_specialized_stages(connection, child_id: str, track: str, *, as_of: str | None = None) -> dict:
    """Borrow one connection; no schema initialization, writes, scoring or supply.

    as_of is a timezone-aware ISO timestamp (defaults to current UTC). Recompute
    for each planning/report use; fingerprints contain local IDs, never cloud DTOs.
    """
    from .specialized_program import SpecializedProgramService, NODE_DOMAIN_HINTS
    c = connection
    now = _time(as_of) if as_of is not None else datetime.now(timezone.utc)
    if now is None:
        raise ValueError("timezone-aware stage time required")
    if not c.in_transaction:
        c.execute("BEGIN")
        try:
            return project_specialized_stages(c, child_id, track, as_of=now.isoformat())
        finally:
            c.rollback()
    child = c.execute("SELECT child_id,grade FROM child_profile WHERE child_id=? AND status='active'", (child_id,)).fetchone()
    if not child:
        raise KeyError(child_id)
    allowed = ("pet", "competition_math") if child["grade"] == "四年级" else ("junior_exam",)
    if track not in allowed:
        raise ValueError("specialized track does not belong to child")
    projections = project_child_mastery_evidence(c, child_id, as_of=now.isoformat())
    first = _history(c, child_id, now)
    catalog_track = "school" if track == "junior_exam" else track
    nodes = [r for r in c.execute("SELECT * FROM knowledge_node WHERE track=? ORDER BY knowledge_node_id", (catalog_track,))
             if str(r["grade_band"]).startswith(child["grade"])]
    domains = []
    for domain_id, label, subject in SpecializedProgramService._domains(track):
        children = [_node(c, child, n, domain_id, projections.get(n["knowledge_node_id"], {}), first, now)
                    for n in nodes if NODE_DOMAIN_HINTS.get(n["knowledge_node_id"]) == domain_id]
        domains.append({"domain_id": domain_id, "label": label, "subject": subject,
                        "nodes": children, **_aggregate(children)})
    skills = {skill: _aggregate([d for d in domains if d["domain_id"].startswith("pet." + skill + ".")])
              for skill in ("listening", "speaking", "reading", "writing")} if track == "pet" else {}
    actions = [n["next_action"] for d in domains for n in d["nodes"] if n["current_stage"] != "maintenance"]
    target_date = None
    if _exists(c, "goal_program"):
        goal = c.execute("SELECT target_horizon_json FROM goal_program WHERE child_id=? AND track=? AND status='active'",
            (child_id, {"pet": "pet_path", "competition_math": "competition_math_path", "junior_exam": "junior_exam_readiness"}[track])).fetchone()
        try:
            from datetime import date
            target_date = date.fromisoformat(json.loads(goal[0]).get("target_date")).isoformat() if goal else None
        except (TypeError, ValueError, AttributeError):
            pass
    result = {"version": VERSION, "child_id": child_id, "track": track, **_aggregate(domains),
        "domains": domains, "skills": skills, "next_actions": actions,
        "obligations": [{**a, "kind": "stage_check", "persisted_task_created": False} for a in actions],
        "target_date": target_date, "target_date_status": "provided_not_rule_verified" if target_date else "clarification_required",
        "policy": POLICY.copy(), "local_only": True, "cloud_transmission_allowed": False}
    result["evidence_fingerprint"] = digest(result)
    return result


def revalidate_stage_action(connection, child_id: str, track: str, *, binding: dict,
                            as_of: str | None = None) -> dict:
    """Recheck a persisted plan binding, never select a replacement question.

    The caller must derive child/track/binding from owned durable plan state and
    use this borrowed connection in its execution transaction. This grants no
    scoring/phase/cloud permission and does not recover an already scored attempt.
    """
    def blocked(reason):
        return {"status": "blocked", "reason_code": reason, "action": None,
                "local_only": True, "cloud_transmission_allowed": False}

    if not isinstance(binding, dict) or (binding.get("child_id"), binding.get("track")) != (child_id, track):
        return blocked("stage_binding_scope_mismatch")
    constraint = binding.get("stage_constraint")
    if (not isinstance(constraint, dict) or constraint.get("version") != VERSION
            or not constraint.get("prerequisite_version") or not binding.get("diagnostic_item_id")
            or not binding.get("source_version") or binding.get("execution_status") not in {"ready", "waiting_due"}):
        return blocked("stage_binding_incomplete")
    now = _time(as_of) if as_of is not None else datetime.now(timezone.utc)
    if now is None:
        return blocked("stage_time_invalid")
    if binding.get("not_before") is not None and _time(binding["not_before"]) is None:
        return blocked("stage_time_invalid")
    try:
        current = project_specialized_stages(connection, child_id, track, as_of=now.isoformat())
    except (KeyError, ValueError):
        return blocked("stage_scope_unavailable")
    domain = next((d for d in current["domains"] if d["domain_id"] == binding.get("domain_id")), None)
    node = next((n for n in domain["nodes"] if n["knowledge_node_id"] == binding.get("knowledge_node_id")), None) if domain else None
    if node is None:
        return blocked("stage_target_unavailable")
    action = node["next_action"]
    if constraint.get("current_stage") != node["current_stage"]:
        return blocked("stage_changed")
    if constraint["prerequisite_version"] != action["stage_constraint"]["prerequisite_version"]:
        return blocked("stage_prerequisites_changed")
    if (action["execution_status"] not in {"ready", "waiting_due"}
            or any(binding.get(k) != action.get(k) for k in ("diagnostic_item_id", "source_version", "review_task_id"))):
        return blocked("stage_original_question_unavailable_or_changed")
    bounds = [_time(v) for v in (binding.get("not_before"), action.get("not_before")) if v is not None]
    due = max(bounds) if bounds else None
    status = "waiting_due" if due and now < due else "ready"
    return {"status": status, "reason_code": "stage_not_due" if status == "waiting_due" else "stage_original_binding_current",
            "action": {**action, "not_before": due.isoformat() if due else None, "execution_status": status},
            "local_only": True, "cloud_transmission_allowed": False}
