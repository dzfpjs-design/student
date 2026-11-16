"""Read-only, estimated capability paths over the existing family goal store.

Calendar windows are backward estimates, not assignments, evidence, or promises
of passing an exam. No dates are inferred from grades, terms, or event names.
The trusted feedback projection owns evidence qualification and withdrawal.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
from math import ceil

from .database import Database
from .family_clock import calendar_as_of
from .learning_feedback import build_learning_feedback


VERSION = "goal_roadmap_v1"
FRESHNESS_DAYS = 30
MAX_CALENDAR_WEEKS = 260
TRACKS = {"pet_path": "pet", "competition_math_path": "competition_math", "junior_exam_readiness": "school"}
BASELINE_PRIORITY = {"recheck_needed": 0, "supported_only": 1, "stale_recheck": 2,
                     "needs_baseline": 3, "independent_observed": 4, "retention_observed": 5}
STEPS = {
    "needs_baseline": [("establish_baseline", 10, 15), ("teach_then_check", 20, 40),
                       ("independent_check", 10, 15), ("delayed_check", 10, 15), ("transfer_check", 10, 15)],
    "stale_recheck": [("refresh_baseline", 10, 15), ("teach_then_check", 20, 40),
                      ("independent_check", 10, 15), ("delayed_check", 10, 15), ("transfer_check", 10, 15)],
    "recheck_needed": [("clarify_then_recheck", 15, 30), ("independent_check", 10, 15),
                       ("delayed_check", 10, 15), ("transfer_check", 10, 15)],
    "supported_only": [("fade_hint_then_check", 15, 25), ("independent_check", 10, 15),
                       ("delayed_check", 10, 15), ("transfer_check", 10, 15)],
    "independent_observed": [("different_item_check", 10, 15), ("delayed_check", 10, 15), ("transfer_check", 10, 15)],
    "retention_observed": [("transfer_check", 10, 15)],
}


def _fingerprint(value):
    return sha256(json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def next_ready_node(path):
    # Dependencies determine eligibility; evidence priority ranks all eligible nodes,
    # not just the first topological layer (which can otherwise starve a child node).
    return min((node for node in path if not node["blocked_by"]),
               key=lambda node: (BASELINE_PRIORITY[node["baseline_status"]], node["knowledge_node_id"]),
               default=None)


def assessment_binding(c, node_id, catalog):
    """Real graph relations annotate a skill; assessment nodes are not skills."""
    nodes, _, allowed, _, sources = catalog
    edges = [dict(row) for row in c.execute("""SELECT * FROM knowledge_edge
        WHERE source_node_id=? AND relation_type='assessed_by' AND status='active'
        ORDER BY edge_id""", (node_id,)).fetchall()]
    edges = [row for row in edges if row['target_node_id'] in nodes
        and nodes[row['target_node_id']]['node_type'] == 'assessment_task' and allowed(row['source_ref'])]
    targets = [nodes[row['target_node_id']] for row in edges]
    source_refs = sorted({row['source_ref'] for row in edges + targets})
    return {'version':'school_assessment_binding_v1', 'knowledge_node_id':node_id, 'edges':edges,
        'fingerprint':_fingerprint([edges, targets, {ref:sources(ref) for ref in source_refs}])}


def _as_of(reference_date):
    return calendar_as_of(reference_date, datetime.now(timezone.utc))


def _budget(c, program, today):
    sources = {"junior_exam_readiness": ("junior_exam_goal", "mistake_review", "due_review")}.get(
        program["track"], (program["source_type"],))
    start = today - timedelta(days=today.weekday())
    placeholders = ",".join("?" for _ in sources)
    used = c.execute(f"""SELECT COALESCE(SUM(CASE WHEN pi.status='completed'
        THEN COALESCE(pi.actual_minutes,pi.expected_minutes) ELSE pi.expected_minutes END),0)
        FROM plan_item pi JOIN daily_plan dp USING(plan_id)
        JOIN daily_session ds ON ds.session_id=dp.session_id AND ds.child_id=dp.child_id
        WHERE dp.child_id=? AND dp.plan_date BETWEEN ? AND ? AND pi.source_type IN ({placeholders})
        AND pi.status IN ('pending','active','paused','completed')""",
        (program["child_id"], start.isoformat(), (start + timedelta(days=6)).isoformat(), *sources)).fetchone()[0]
    minutes, sessions = program["default_weekly_minutes"], program["default_weekly_sessions"]
    valid = type(minutes) is int and 0 <= minutes <= 600 and type(sessions) is int and 0 <= sessions <= 7
    return {"weekly_minutes": minutes if valid else 0, "weekly_sessions": sessions if valid else 0,
            "committed_weekly_minutes": max(0, int(used)),
            "remaining_weekly_minutes": max(0, minutes - max(0, int(used))) if valid and sessions else 0,
            "auto_increase_budget": False, "configured_budget_valid": valid,
            "capacity_basis": "configured_goal_budget_not_verified_free_time"}


def _catalog(c, child, today):
    scoped_sources = defaultdict(list)
    for source in c.execute("""SELECT source_id,source_ref,child_id,review_status,effective_from,effective_to,
        content_hash,version_label,current_version,predecessor_id FROM knowledge_source ORDER BY source_id"""):
        scoped_sources[source["source_ref"]].append(dict(source))

    def applicable(source_ref):
        return [r for r in scoped_sources.get(source_ref, []) if r["child_id"] in (None, child["child_id"])]

    def current_sources(source_ref):
        return [r for r in applicable(source_ref) if r['current_version'] == 1]

    def effective(source):
        try:
            start = date.fromisoformat(source["effective_from"]) if source["effective_from"] else None
            end = date.fromisoformat(source["effective_to"]) if source["effective_to"] else None
            return (source["review_status"] == "approved" and (start is None or start <= today)
                    and (end is None or today <= end))
        except (ValueError, TypeError):
            return False

    def allowed(source_ref):
        owners = scoped_sources.get(source_ref, [])
        matches = current_sources(source_ref)
        # Historical replacements cannot veto the separately reviewed current
        # source. Multiple current aliases still all need valid authorization.
        return not owners or bool(matches) and all(effective(r) for r in matches)

    candidates = [dict(r) for r in c.execute("SELECT * FROM knowledge_node WHERE status='active'")
                  if str(r["grade_band"]).startswith(child["grade"])]
    nodes = {r["knowledge_node_id"]: r for r in candidates if allowed(r["source_ref"])}
    unavailable = [r for r in candidates if r["knowledge_node_id"] not in nodes and applicable(r["source_ref"])]
    edges = [dict(r) for r in c.execute("SELECT * FROM knowledge_edge WHERE relation_type='prerequisite_of' AND status='active' ORDER BY edge_id")]
    return nodes, edges, allowed, unavailable, current_sources


def _catalog_binding(node_id, catalog):
    nodes, edges, _, _, sources = catalog
    relevant, pending = set(), [node_id]
    while pending:
        current = pending.pop()
        if current in relevant:
            continue
        relevant.add(current)
        pending.extend(edge['source_node_id'] for edge in edges if edge['target_node_id'] == current)
    graph = [edge for edge in edges if edge['target_node_id'] in relevant]
    refs = ({nodes[key]['source_ref'] for key in relevant if key in nodes}
            | {edge['source_ref'] for edge in graph})
    source_rows = [row for ref in sorted(refs) for row in sources(ref)]
    basis = {'nodes': [nodes.get(key) for key in sorted(relevant)], 'graph': graph, 'sources': source_rows}
    return {'version': 'goal_catalog_binding_v1', 'node_id': node_id,
            'fingerprint': _fingerprint(basis), 'source_ids': sorted(row['source_id'] for row in source_rows),
            'source_refs': sorted(refs)}


def _catalog_source_reason(c, child_id, binding, today):
    child = c.execute('SELECT * FROM child_profile WHERE child_id=?', (child_id,)).fetchone()
    catalog = _catalog(c, child, today)
    node_id = binding.get('knowledge_node_id')
    if node_id not in catalog[0]:
        return 'catalog_source_changed'
    current = _catalog_binding(node_id, catalog)
    if 'catalog_binding' in binding:
        return None if binding['catalog_binding'] == current else 'catalog_source_changed'
    # A legacy plan cannot acquire replacement authority through a read.
    replaced = c.execute('''SELECT source_ref FROM knowledge_source
        WHERE (child_id IS NULL OR child_id=?) AND current_version=0''', (child_id,))
    if any(row[0] in current['source_refs'] for row in replaced):
        return 'catalog_source_unbound'
    return None


def goal_catalog_source_reason(c, session_id, plan_item_id):
    """Source-only check; no new question selection, transaction or learning writes."""
    item = c.execute('''SELECT pi.*,dp.child_id,dp.session_id FROM plan_item pi
        JOIN daily_plan dp USING(plan_id) WHERE pi.plan_item_id=? AND dp.session_id=?''',
        (plan_item_id, session_id)).fetchone()
    if not item:
        return 'catalog_source_unbound'
    reason = _rule_task_source_reason(c, item)
    if reason:
        return reason
    if item['source_type'] != 'junior_exam_goal' and item['task_type'] != 'goal_prerequisite':
        return None
    rows = c.execute('''SELECT metadata_json FROM plan_constraint_decision WHERE plan_id=?
        AND obligation_type=? AND source_ref=? AND decision='scheduled' ''',
        (item['plan_id'], item['source_type'], item['source_ref'])).fetchall()
    bindings = []
    for row in rows:
        try:
            binding = json.loads(row[0])['planning_evidence']
            if not isinstance(binding, dict) or not isinstance(binding.get('knowledge_node_id'), str):
                return 'catalog_source_unbound'
            bindings.append({key: binding[key] for key in ('knowledge_node_id', 'catalog_binding', 'assessment_binding') if key in binding})
        except (ValueError, KeyError, TypeError):
            return 'catalog_source_unbound'
    if not bindings or any(value != bindings[0] for value in bindings[1:]):
        return 'catalog_source_unbound'
    if 'assessment_binding' in bindings[0]:
        child = c.execute('SELECT * FROM child_profile WHERE child_id=?',(item['child_id'],)).fetchone()
        if bindings[0]['assessment_binding'] != assessment_binding(c,bindings[0]['knowledge_node_id'],_catalog(c,child,_as_of(None)[0])):
            return 'catalog_source_changed'
    return _catalog_source_reason(c, item['child_id'], bindings[0], _as_of(None)[0])


def _rule_task_source_reason(c, item):
    if item['source_type'] not in {'pet_daily', 'junior_exam_goal', 'competition_math'}:
        return None
    tables = {row[0] for row in c.execute("""SELECT name FROM sqlite_master
        WHERE type='table' AND name IN ('goal_program', 'goal_program_revision')""")}
    # Stage tasks also run before optional goal programs exist. Never create
    # tables in this borrowed read; a partially missing rule history fails closed.
    if not tables:
        return None
    if len(tables) != 2:
        return 'catalog_source_unbound'
    revisions = c.execute('''SELECT r.payload_json FROM goal_program_revision r
        JOIN goal_program p USING(program_id) WHERE p.child_id=? AND p.source_type=?''',
        (item['child_id'], item['source_type']))
    for row in revisions:
        try:
            payload = json.loads(row[0])
            if not isinstance(payload, dict):
                return 'catalog_source_unbound'
            if payload.get('operation') != 'confirm_rule_reference':
                continue
            receipt = payload['receipt']
            targets = receipt['superseded_plan_item_ids']
            if not isinstance(targets, list) or not all(isinstance(v, str) for v in targets):
                return 'catalog_source_unbound'
            if item['plan_item_id'] in targets:
                return 'goal_rule_reconfirmation_required'
        except (ValueError, KeyError, TypeError):
            return 'catalog_source_unbound'
    return None


def _path(nodes, edges, targets, source_allowed, priority):
    parents = defaultdict(set)
    unavailable = set()
    for edge in edges:
        if edge["target_node_id"] not in nodes:
            continue
        if edge["source_node_id"] not in nodes or not source_allowed(edge["source_ref"]):
            unavailable.add(edge["target_node_id"])
        else:
            parents[edge["target_node_id"]].add(edge["source_node_id"])
    # Iterative traversal avoids recursion limits on externally supplied catalogs.
    relevant, pending = set(targets), list(targets)
    while pending:
        node = pending.pop()
        for parent in parents[node] - relevant:
            relevant.add(parent)
            pending.append(parent)
    remaining = {node: set(parents[node]) for node in relevant}
    ordered = []
    while remaining:
        ready = sorted((node for node, dependencies in remaining.items() if not dependencies),
                       key=lambda node: (priority.get(node, 2), node))
        if not ready:
            break
        ordered.extend(ready)
        for node in ready:
            del remaining[node]
        for dependencies in remaining.values():
            dependencies.difference_update(ready)
    return ordered, parents, unavailable & relevant, set(remaining), relevant


def _baseline(node, feedback, today):
    observed = feedback.get(node["knowledge_node_id"])
    status = observed.get("status") if observed else "needs_baseline"
    if status not in STEPS:
        status = "needs_baseline"
    stale = False
    if observed:
        try:
            occurred = datetime.fromisoformat(observed["latest_evidence_at"])
            stale = (today - occurred.date()).days > FRESHNESS_DAYS
        except (KeyError, TypeError, ValueError):
            stale = True
        if stale:
            status = "stale_recheck"
    ready = bool(observed and not stale and status == "retention_observed"
                 and observed.get("independent_distinct_items", 0) >= 2
                 and observed.get("retention_success_count", 0) >= 1)
    return {"knowledge_node_id": node["knowledge_node_id"], "baseline_status": status,
            "feedback_status": observed.get("status") if observed else None,
            "evidence_refs": observed.get("evidence_refs", []) if observed else [],
            "latest_evidence_at": observed.get("latest_evidence_at") if observed else None,
            "prerequisite_observed": ready, "mastery_claim_allowed": False}


def stage_goal_prerequisites(c, child_id, node_id, *, as_of=None):
    """Use the roadmap's graph and receipt policy for scheduled stage tasks.

    This is not a gate on voluntary baseline assessment. Only incoming graph
    dependencies are checked; a task's own new answer cannot invalidate itself.
    """
    from .family_clock import family_today
    now = datetime.fromisoformat(as_of) if as_of else datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError('timezone-aware prerequisite time required')
    child = c.execute('SELECT * FROM child_profile WHERE child_id=? AND status=\'active\'', (child_id,)).fetchone()
    if not child:
        raise ValueError('active child required')
    catalog = _catalog(c, child, family_today(now))
    nodes, edges, allowed, _, _ = catalog
    _, _, missing, cyclic, relevant = _path(nodes, edges, [node_id], allowed, {})
    feedback = build_learning_feedback(c, child_id, now=now)
    observed = {item['knowledge_node_id']: item for item in feedback['nodes']}
    dependencies = [_baseline(nodes[key], observed, now.date())
                    for key in sorted(relevant - {node_id}) if key in nodes]
    blockers = []
    if node_id not in nodes or missing:
        blockers.append('knowledge_prerequisite_source_unavailable')
    if cyclic:
        blockers.append('knowledge_prerequisite_cycle')
    if any(not item['prerequisite_observed'] for item in dependencies):
        blockers.append('knowledge_prerequisite_unverified')
    result = {'version': 'stage_goal_prerequisites_v1', 'knowledge_node_id': node_id,
              'status': 'blocked' if blockers else 'ready', 'blockers': blockers,
              'dependencies': dependencies, 'catalog_binding': _catalog_binding(node_id, catalog),
              'local_only': True, 'cloud_transmission_allowed': False, 'mastery_claim_allowed': False}
    result['source_version'] = _fingerprint(result)
    return result


def _calendar(today, deadline, burden, work):
    if deadline is None or deadline < today or (deadline - today).days > MAX_CALENDAR_WEEKS * 7:
        return [], sum(w["estimated_minutes"]["max"] for w in work)
    weeks = []
    cursor = today
    while cursor <= deadline:
        end = min(deadline, cursor + timedelta(days=6 - cursor.weekday()))
        days = (end - cursor).days + 1
        capacity = burden["weekly_minutes"] * days // 7 if burden["weekly_sessions"] * days // 7 else 0
        if not weeks:
            capacity = min(capacity, burden["remaining_weekly_minutes"])
        weeks.append({"week_start": cursor.isoformat(), "week_end": end.isoformat(), "budget_minutes": capacity,
                      "planned_minutes": 0, "allocations": [], "status": "planned", "estimate_only": True})
        cursor = end + timedelta(days=1)
    index = len(weeks) - 1
    available = sum(w["budget_minutes"] for w in weeks)
    scheduled = []
    for task in work:
        if task["estimated_minutes"]["max"] > available:
            break
        scheduled.append(task)
        available -= task["estimated_minutes"]["max"]
    unallocated = sum(t["estimated_minutes"]["max"] for t in work[len(scheduled):])
    for task in reversed(scheduled):
        left = task["estimated_minutes"]["max"]
        while left and index >= 0:
            week = weeks[index]
            free = week["budget_minutes"] - week["planned_minutes"]
            amount = min(left, free)
            if amount:
                week["allocations"].insert(0, {"work_id": task["work_id"], "minutes": amount})
                week["planned_minutes"] += amount
                left -= amount
            if week["planned_minutes"] == week["budget_minutes"]:
                index -= 1
        unallocated += left
    return weeks, unallocated


def _has_qualified_question(c, child_id, node_id, as_of):
    from .goal_question_inventory import practice_item
    for item in c.execute("SELECT diagnostic_item_id FROM diagnostic_item WHERE knowledge_node_id=? AND status='active'", (node_id,)):
        if practice_item(c, child_id, item['diagnostic_item_id'], as_of):
            return True
    return False


def build_goal_roadmap(c, program, *, today, feedback_now, feedback=None, action_target_node_id=None):
    child = c.execute("SELECT * FROM child_profile WHERE child_id=? AND status='active'", (program["child_id"],)).fetchone()
    if not child:
        raise ValueError("active child required")
    feedback = feedback if feedback is not None else build_learning_feedback(c, child["child_id"], now=feedback_now)
    horizon = json.loads(program["target_horizon_json"])
    from .official_goal_rules import target_rule_context
    exam_rules = target_rule_context(program['track'], horizon, as_of_date=today,
                                     curriculum_region=child['curriculum_region'])
    clarifications, risks = [], []
    def risk(code, message):
        if not any(item["code"] == code for item in risks):
            risks.append({"code": code, "message": message})
    if exam_rules['binding_status'] == 'source_changed':
        risk('exam_rules_changed', '目标绑定的考试规则已变化，需要重新核对；旧考试准备判断不能沿用，日常学习仍可继续。')
    if exam_rules['public_rule_status'] != 'verified_reference_snapshot':
        risk('exam_rules_unverified', '该年份或赛事的官方规则尚未核实；家庭目标日期不是已确认的报名场次，不推断奖项或通过结论。')
    try:
        deadline = date.fromisoformat(horizon["target_date"]) if horizon.get("target_date") else None
    except (TypeError, ValueError):
        deadline = None
        risk("invalid_target_date", "目标日期无效，需要家长核对。")
    if deadline is None:
        clarifications.append({"field": "target_date", "reason": "请明确目标日期；年级、学期和赛事名称不能代替具体日期。"})
    if horizon.get("target_score") is not None or horizon.get("award_target"):
        risk("outcome_mapping_unverified", "目标分数或奖项尚不能由当前能力节点换算，需核对正式规则与覆盖。")
    catalog = _catalog(c, child, today)
    nodes, edges, allowed, unavailable_nodes, source_versions = catalog
    targets = sorted(key for key, node in nodes.items() if node["track"] == TRACKS.get(program["track"]))
    unavailable_targets = [node for node in unavailable_nodes if node["track"] == TRACKS.get(program["track"])]
    observed = {n["knowledge_node_id"]: n for n in feedback["nodes"]}
    all_baselines = {node: _baseline(value, observed, feedback_now.date()) for node, value in nodes.items()}
    priority = {node: BASELINE_PRIORITY[value["baseline_status"]] for node, value in all_baselines.items()}
    ordered, parents, missing, cyclic, relevant = _path(nodes, edges, targets, allowed, priority)
    if not targets:
        risk("target_catalog_missing", "当前目标缺少可核对的能力目录。")
    if unavailable_targets:
        risk("target_source_unavailable", "已知目标能力的来源已撤回、未生效或有效期不可用；不能删去这部分后当作目标变容易。")
    if missing:
        risk("prerequisite_source_unavailable", "部分前置关系的来源或适用范围不可用，不能直接跳过。")
    if cyclic:
        risk("prerequisite_cycle", "前置关系存在循环，需要先核对课程关系。")
    risk("curriculum_coverage_unverified", "路线基于当前能力目录，不代表完整考试或教材覆盖。")
    risk("capacity_unverified", "预算是已配置上限，仍需确认作业、睡眠和休息后是否有实际可用时间。")
    baseline = {node: all_baselines[node] for node in relevant}
    path, work = [], []
    for node_id in ordered:
        fact = baseline[node_id]
        if fact["baseline_status"] == "stale_recheck":
            risk("stale_evidence", "已有证据超过路线的新鲜度估算窗口，应先重新核对。")
        if node_id not in targets and fact["prerequisite_observed"]:
            continue
        tasks = [{"work_id": node_id + ":" + action, "action": action,
                  "estimated_minutes": {"min": low, "max": high}, "status": "planned",
                  "estimate_only": True, "minimum_gap_hours": 24 if action == "delayed_check" else 0,
                  "completion_basis": "qualified_distinct_independent_response_not_time"}
                 for action, low, high in STEPS[fact["baseline_status"]]]
        blocked = sorted(parent for parent in parents[node_id] if not baseline[parent]["prerequisite_observed"])
        path.append({**fact, "title": nodes[node_id]["title"], "subject": nodes[node_id]["subject"],
                     "prerequisite_node_ids": sorted(parents[node_id]), "blocked_by": blocked,
                     "source_ref": nodes[node_id]["source_ref"], "knowledge_version": nodes[node_id]["knowledge_version"],
                     "steps": tasks})
        work.extend(tasks)
    if any(b["baseline_status"] == "needs_baseline" for b in baseline.values()):
        risk("baseline_unverified", "尚缺有效基线，所需工作量只是估算，需要作答后重算。")
    burden = _budget(c, program, today)
    total = {bound: sum(task["estimated_minutes"][bound] for task in work) for bound in ("min", "max")}
    weeks, unallocated = _calendar(today, deadline, burden, work)
    capacity = sum(week["budget_minutes"] for week in weeks)
    if missing or cyclic or not targets or unavailable_targets:
        feasibility = "unknown"
    elif not deadline:
        feasibility = "unknown"
    elif deadline < today:
        feasibility = "deadline_passed"
    elif not burden["weekly_minutes"] or not burden["weekly_sessions"]:
        feasibility = "budget_unavailable"
    elif (deadline - today).days > MAX_CALENDAR_WEEKS * 7:
        feasibility = "horizon_unverified"
    elif total["min"] > capacity:
        feasibility = "estimated_over_capacity"
    elif total["max"] > capacity:
        feasibility = "estimated_at_risk"
    else:
        feasibility = "provisional_not_guaranteed"
    if feasibility not in {"unknown", "provisional_not_guaranteed"}:
        risk(feasibility, "当前日期与预算不足或尚不能验证，应协商延期、范围或先核实条件，不自动加压。")
    burden.update(estimated_minutes=total, available_minutes=capacity if deadline else None,
                  unallocated_minutes=unallocated, feasibility=feasibility,
                  estimate_complete=not (missing or cyclic or not targets or unavailable_targets),
                  estimated_weeks=ceil(total["max"] / burden["weekly_minutes"]) if burden["weekly_minutes"] else None)
    weekly = [{"period_start": w["week_start"], "target_date": w["week_end"], "status": "planned",
               "work_ids": list(dict.fromkeys(a["work_id"] for a in w["allocations"])),
               "estimated_minutes": w["planned_minutes"], "estimate_only": True,
               "basis": "latest_estimated_window_not_assignment"} for w in weeks if w["allocations"]]
    monthly = {}
    for milestone in weekly:
        month = milestone["target_date"][:7]
        item = monthly.setdefault(month, {"month": month, "target_date": milestone["target_date"], "work_ids": [],
            "estimated_minutes": 0, "status": "planned", "estimate_only": True})
        item["target_date"] = milestone["target_date"]
        item["estimated_minutes"] += milestone["estimated_minutes"]
        item["work_ids"] = list(dict.fromkeys(item["work_ids"] + milestone["work_ids"]))
    next_action = None
    # Focus only the proposed action; retain all goal work, risks and budgets.
    action_scope = relevant
    if action_target_node_id is not None:
        action_scope = (_path(nodes, edges, [action_target_node_id], allowed, priority)[-1]
                        if action_target_node_id in targets else set())
        if not action_scope:
            risk('action_target_unavailable', '本次任务的目标不在当前有效目标目录中，不选择其他目标代替。')
    # Chapters, misconceptions and assessment records remain in the roadmap,
    # but cannot be assigned a made-up diagnostic answer or mastery status.
    ready = next_ready_node([node for node in path if node['knowledge_node_id'] in action_scope])
    if program['track'] == 'junior_exam_readiness':
        executable, unresolved = [], []
        assessment_bindings = {node['knowledge_node_id']:assessment_binding(c,node['knowledge_node_id'],catalog)
            for node in path if nodes[node['knowledge_node_id']]['node_type'] == 'skill'}
        for node in path:
            node_id = node['knowledge_node_id']
            kind = nodes[node_id]['node_type']
            if kind == 'skill':
                executable.append((node, node_id, None))
            elif kind == 'assessment_task':
                linked = [skill for skill in path if nodes[skill['knowledge_node_id']]['node_type'] == 'skill'
                    and any(edge['target_node_id'] == node_id for edge in
                            assessment_bindings[skill['knowledge_node_id']]['edges'])]
                if len(linked) == 1:
                    executable.append((linked[0], node_id, node_id))
                else:
                    unresolved.append(node_id)
        eligible = [entry for entry in executable if not entry[0]['blocked_by']
                    and entry[0]['knowledge_node_id'] in action_scope]
        selected = min(eligible, key=lambda entry:(BASELINE_PRIORITY[entry[0]['baseline_status']], entry[1]), default=None)
        ready = selected[0] if selected and not unresolved else None
        if unresolved:
            risk('assessment_skill_mapping_unavailable', '复测与具体技能的图关系缺失或有歧义，先核对目录，不随意换题。')
    if (ready and not cyclic and not missing and not unavailable_targets
            and program["status"] == "active" and burden["remaining_weekly_minutes"] > 0
            and (deadline is None or deadline >= today)):
        step = ready["steps"][0]
        minutes = min(15, step["estimated_minutes"]["max"], burden["remaining_weekly_minutes"])
        if minutes >= step["estimated_minutes"]["min"]:
            available = _has_qualified_question(c, child["child_id"], ready["knowledge_node_id"], feedback_now)
            next_action = {"action": step["action"], "knowledge_node_id": ready["knowledge_node_id"],
                "child_id": child["child_id"], "program_id": program["program_id"],
                "subject": ready["subject"], "title": ready["title"], "minutes": minutes,
                "source_type": program["source_type"], "source_ref": program["program_id"],
                "evidence_refs": ready["evidence_refs"], "requires_source_selection": True,
                "catalog_binding": _catalog_binding(ready['knowledge_node_id'], catalog),
                "dispatchable": available, "status": "suggested" if available else "qualified_source_needed",
                "completion_standard": "有效独立作答与后续复测；参与分钟不作为能力达标。",
                "mastery_claim_allowed": False, "cloud_transmission_allowed": False}
            if program['track'] == 'junior_exam_readiness':
                next_action['assessment_binding'] = assessment_bindings[ready['knowledge_node_id']]
                next_action['requested_assessment_id'] = selected[2]
            if not available:
                risk("qualified_question_missing", "该能力还缺当前可用且未近期重复的可靠题源，需先补齐题源，不安排假诊断。")
        else:
            risk("remaining_budget_below_check_minimum", "本周剩余预算不足以安排这一步的保守估算，先协商或下周继续，不自动加时。")
    result = {"version": VERSION, "program_id": program["program_id"], "child_id": child["child_id"],
        "track": program["track"], "title": program["title"], "as_of_date": feedback_now.date().isoformat(),
        "reference_date": today.isoformat(), "target_date": deadline.isoformat() if deadline else None,
        "status": "paused" if program["status"] != "active" else "needs_clarification" if clarifications
                  else "blocked" if missing or cyclic or not targets or unavailable_targets
                  else "needs_negotiation" if feasibility != "provisional_not_guaranteed" else "estimated",
        "baseline": [baseline[key] for key in sorted(baseline)], "prerequisite_path": path,
        "cycles": weeks, "milestones": {"monthly": list(monthly.values()), "weekly": weekly},
        "burden": burden, "risks": risks, "clarifications": clarifications,
        "next_daily_action": next_action,
        "exam_rules": exam_rules,
        "negotiation_options": ["clarify_conditions", "extend_deadline", "negotiate_scope_with_parent"],
        "assumptions": {"effort": "unvalidated_planning_ranges_not_child_speed", "freshness_days": FRESHNESS_DAYS,
            "evidence_window_days": feedback.get("window_days", 90), "calendar": "uniform_weekly_capacity_estimate",
            "retention": "at_least_24_hours_and_distinct_independent_checks_require_actual_scheduling",
            "backward_windows": "latest_replanning_windows_not_a_reason_to_delay_baseline_checks",
            "coverage": "active_catalog_not_verified_full_curriculum"},
        "evidence_fingerprint": feedback["evidence_fingerprint"], "goal_attainment": "not_established",
        "mastery_claim_allowed": False, "local_only": True, "cloud_transmission_allowed": False}
    if action_target_node_id is not None:
        result['action_target_node_id'] = action_target_node_id
    graph = [edge for edge in edges if edge["target_node_id"] in relevant and edge["source_node_id"] in relevant]
    source_refs = ({nodes[node]["source_ref"] for node in relevant}
                   | {node["source_ref"] for node in unavailable_targets}
                   | {edge["source_ref"] for edge in edges if edge["target_node_id"] in relevant})
    result["source_version"] = VERSION + ":" + _fingerprint({"projection": result, "graph": graph,
        "source_versions": {ref: source_versions(ref) for ref in sorted(source_refs)},
        "goal": {k: program[k] for k in ("target_horizon_json", "target_statement", "version", "status")}})
    if next_action:
        next_action["roadmap_source_version"] = result["source_version"]
    return result


class GoalRoadmapService:
    def __init__(self, database: Database):
        self.database = database

    def read(self, child_id: str, track: str, reference_date: date | None = None) -> dict:
        today, now = _as_of(reference_date)
        with self.database.connect() as c:
            c.execute("BEGIN")
            if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='goal_program'").fetchone():
                raise KeyError((child_id, track))
            program = c.execute("SELECT * FROM goal_program WHERE child_id=? AND track=?", (child_id, track)).fetchone()
            if not program:
                raise KeyError((child_id, track))
            result = build_goal_roadmap(c, dict(program), today=today, feedback_now=now)
            from .goal_section_coverage import attach_section_evidence
            return attach_section_evidence(c, result, as_of=now)

    def resolve_execution(self, session_id: str, plan_item_id: str) -> dict:
        """Resolve a persisted plan's original node, without reserving or presenting.

        The caller must bind the exact returned item through diagnostic_prompt;
        this read snapshot is neither a waiting prompt nor scoring permission.
        """
        with self.database.connect() as c:
            c.execute("BEGIN")
            return self._resolve_execution(c, session_id, plan_item_id)

    @staticmethod
    def _resolve_execution(c, session_id, plan_item_id, *, prompt_id=None):
        """Internal hook for a caller-owned prompt transaction; never commits it."""
        if not c.in_transaction:
            raise RuntimeError("execution resolution requires a transaction")
        result = {"version": "goal_execution_v1", "session_id": session_id, "plan_item_id": plan_item_id,
                  "binding": None, "diagnostic_item_id": None, "item_version": None, "diagnostic_item": None,
                  "read_only": True, "requires_prompt_binding": True, "reservation_created": False,
                  "mastery_claim_allowed": False, "local_only": True, "cloud_transmission_allowed": False}

        def blocked(code, reason):
            return {**result, "status": "blocked", "reason_code": code, "reason": reason}

        if not all(isinstance(value, str) and value.strip() for value in (session_id, plan_item_id)):
            return blocked("invalid_request", "需要原会话与实际计划项。")
        required = {"daily_session", "child_profile", "daily_plan", "plan_item", "plan_constraint_decision", "goal_program", "goal_cycle"}
        tables = {row[0] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not required <= tables:
            return blocked("binding_unavailable", "尚无可读取的真实目标计划绑定。")
        session = c.execute("""SELECT ds.*,cp.status AS child_status FROM daily_session ds
            JOIN child_profile cp USING(child_id) WHERE session_id=?""", (session_id,)).fetchone()
        if (not session or session["status"] != "active" or session["child_status"] != "active"
                or session["state"] in {"evolving", "completed"}):
            return blocked("session_inactive", "原会话或孩子已不可用，不重新选题。")
        item = c.execute("""SELECT pi.*,dp.child_id AS plan_child_id,dp.session_id AS plan_session_id,
            dp.status AS plan_status,dp.plan_date FROM plan_item pi JOIN daily_plan dp USING(plan_id)
            WHERE pi.plan_item_id=?""", (plan_item_id,)).fetchone()
        if not item or (item["plan_child_id"], item["plan_session_id"]) != (session["child_id"], session_id):
            return blocked("plan_ownership_mismatch", "找不到属于原会话的这项计划。")
        result.update(child_id=session["child_id"], plan_id=item["plan_id"])
        if item["plan_status"] != "active":
            return blocked("plan_inactive", "这份计划已不在执行，不恢复其中的旧题。")
        if item["status"] not in {"pending", "active", "paused"}:
            return blocked("plan_item_inactive", "这项计划已完成、取消或顺延，不另行安排题目。")
        if item["source_type"] not in {"junior_exam_goal", "pet_daily", "competition_math"}:
            return blocked("unsupported_source", "这不是带长期路线绑定的计划项，保留原有任务入口。")
        decisions = c.execute("""SELECT metadata_json,allocated_minutes FROM plan_constraint_decision
            WHERE plan_id=? AND obligation_type=? AND source_ref=? AND decision='scheduled'
            ORDER BY decision_id""", (item["plan_id"], item["source_type"], item["source_ref"])).fetchall()
        bindings = []
        actions = {action for steps in STEPS.values() for action, _, _ in steps}
        for decision in decisions:
            try:
                binding = json.loads(decision["metadata_json"])["planning_evidence"]
                version = binding["roadmap_source_version"]
                digest = version.removeprefix(VERSION + ":") if isinstance(version, str) else ""
                if (not isinstance(binding["knowledge_node_id"], str) or not binding["knowledge_node_id"].strip()
                        or binding["action"] not in actions or binding["mastery_claim_allowed"] is not False
                        or not isinstance(version, str) or not version.startswith(VERSION + ":")
                        or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest)
                        or decision["allocated_minutes"] <= 0):
                    raise ValueError("invalid persisted binding")
                bindings.append({key: binding[key] for key in (
                    "knowledge_node_id", "action", "roadmap_source_version", "mastery_claim_allowed")})
                if 'catalog_binding' in binding:
                    bindings[-1]['catalog_binding'] = binding['catalog_binding']
                if 'goal_foundation' in binding:
                    bindings[-1]['goal_foundation'] = binding['goal_foundation']
                for key in ('assessment_binding','school_item_binding'):
                    if key in binding:
                        bindings[-1][key] = binding[key]
            except (KeyError, ValueError, TypeError):
                return blocked("binding_unavailable", "这项计划缺少有效的原知识点、动作和路线版本，不能按标题猜题。")
        if not bindings:
            return blocked("binding_unavailable", "这项计划没有持久的路线绑定，不能回退到通用选题。")
        if any(binding != bindings[0] for binding in bindings[1:]):
            return blocked("binding_ambiguous", "同一计划来源有冲突的路线绑定，需要重新核对。")
        result["binding"] = binding = bindings[0]
        in_flight = None
        if prompt_id is not None:
            if item['task_type'] != 'goal_prerequisite' or item['status'] not in {'active', 'paused'}:
                return blocked('ongoing_prompt_unavailable', '只有当前正在进行的基础题可以继续，不用于新题选取。')
            in_flight = c.execute('''SELECT * FROM diagnostic_prompt WHERE prompt_id=? AND child_id=?
                AND origin_session_id=? AND status IN ('waiting_answer','answer_saved')''',
                (prompt_id, session['child_id'], session_id)).fetchone()
            owners = c.execute('''SELECT context_manifest_json FROM agent_plan WHERE session_id=? AND child_id=?
                AND status!='cancelled' AND json_extract(context_manifest_json,'$.goal_prompt_binding.plan_item_id')=?
                AND json_extract(context_manifest_json,'$.goal_prompt_binding.prompt_id')=?''',
                (session_id, session['child_id'], plan_item_id, prompt_id)).fetchall()
            if not in_flight or not owners or any(
                    json.loads(owner[0])['goal_prompt_binding'].get('binding') != binding
                    or json.loads(owner[0])['goal_prompt_binding'].get('item_version') != in_flight['item_version']
                    for owner in owners):
                return blocked('ongoing_prompt_unbound', '当前基础题缺少原任务与题目版本绑定，不重选或借用其他题。')
        if item['task_type'] == 'goal_prerequisite':
            foundation = binding.get('goal_foundation')
            if (not isinstance(foundation, dict) or not isinstance(foundation.get('target_node_id'), str)
                    or not isinstance(foundation.get('target_catalog_binding'), dict)):
                return blocked('foundation_binding_missing', '补基础任务缺少原目标依据，需要核对原计划。')
            if _catalog_source_reason(c, session['child_id'], {'knowledge_node_id': foundation['target_node_id'],
                    'catalog_binding': foundation['target_catalog_binding']}, _as_of(None)[0]):
                return blocked('foundation_target_changed', '补基础任务的原目标或前置关系已变化，需要重新核对计划。')
        rule_reason = _rule_task_source_reason(c, {**dict(item), 'child_id': session['child_id']})
        if rule_reason:
            return blocked(rule_reason, '家长已核对新的规则；这项旧安排没有自动换版，请按新路线重新安排。')
        program = c.execute("""SELECT gp.*,gc.child_id AS cycle_child_id,gc.week_start,gc.week_end
            FROM goal_cycle gc JOIN goal_program gp USING(program_id) WHERE gc.cycle_id=?""", (item["source_ref"],)).fetchone()
        if (not program or program["child_id"] != session["child_id"] or program["cycle_child_id"] != session["child_id"]
                or program["source_type"] != item["source_type"]
                or not program["week_start"] <= item["plan_date"] <= program["week_end"]):
            return blocked("binding_scope_mismatch", "计划绑定的目标周期或孩子已不一致。")
        if program["status"] != "active":
            return blocked("goal_inactive", "原长期目标已暂停或结束，不自动继续原动作。")
        today, now = _as_of(None)
        if item["plan_date"] > today.isoformat():
            return blocked("plan_not_due", "这项计划尚未到执行日期。")
        try:
            roadmap = build_goal_roadmap(c, dict(program), today=today, feedback_now=now)
        except (TypeError, ValueError, KeyError):
            return blocked("route_unavailable", "当前目标或路线数据无法核实。")
        result.update(program_id=program["program_id"], track=program["track"],
                      current_roadmap_source_version=roadmap["source_version"])
        unavailable = {"target_catalog_missing", "target_source_unavailable", "prerequisite_source_unavailable", "prerequisite_cycle", "invalid_target_date"}
        if any(risk["code"] in unavailable for risk in roadmap["risks"]):
            return blocked("route_unavailable", "当前前置关系或目标资料不可用，不能跳过后继续旧题。")
        if roadmap["target_date"] and roadmap["target_date"] < today.isoformat():
            return blocked("target_date_passed", "目标日期已过，需要先核对安排。")
        burden = roadmap["burden"]
        if not burden["configured_budget_valid"] or not burden["weekly_minutes"] or not burden["weekly_sessions"]:
            return blocked("budget_unavailable", "当前目标没有有效预算，不能开始新的检查。")
        node = next((node for node in roadmap["prerequisite_path"] if node["knowledge_node_id"] == binding["knowledge_node_id"]), None)
        if not node:
            return blocked("node_unavailable", "原知识点已不在当前可核对路径中，不改绑其他知识点。")
        if node["blocked_by"]:
            return blocked("prerequisite_blocked", "原知识点还有前置能力需要核对，不直接执行旧动作。")
        if not node["steps"] or (node["steps"][0]["action"] != binding["action"] and not in_flight):
            return blocked("action_changed", "新证据已改变原知识点需要的动作，请重新规划，不继续旧检查。")
        source_reason = _catalog_source_reason(c, session['child_id'], binding, today)
        if source_reason:
            return blocked(source_reason, '原计划的课程或资料绑定不可用，需要重新安排，不自动改用新版。')
        if ('assessment_binding' in binding and binding['assessment_binding'] != assessment_binding(c,
                binding['knowledge_node_id'], _catalog(c, c.execute('SELECT * FROM child_profile WHERE child_id=?',
                    (session['child_id'],)).fetchone(), today))):
            return blocked('assessment_binding_changed', '原复测与技能关系已变化，保留原记录，不借其他节点继续。')

        from .diagnostic_prompt import DiagnosticPromptService, _ITEM_FIELDS, _version
        from .goal_question_inventory import practice_item

        # The plan already reserves time. Requiring remaining_minutes > 0 or exact
        # projection-version equality would reject the plan's own reservation.
        candidates = c.execute("""SELECT diagnostic_item_id FROM diagnostic_item WHERE knowledge_node_id=?
            AND status='active' ORDER BY difficulty,diagnostic_item_id""", (binding["knowledge_node_id"],)).fetchall()
        if in_flight:
            candidates = [row for row in candidates if row[0] == in_flight['diagnostic_item_id']]
        fixed = binding.get('school_item_binding')
        if fixed:
            candidates = [row for row in candidates if row[0] == fixed.get('diagnostic_item_id')]
        for candidate in candidates:
            diagnostic = DiagnosticPromptService._item(c, session["child_id"], candidate[0])
            if not diagnostic:
                continue
            if fixed and _version(diagnostic) != fixed.get('item_version'):
                continue
            if in_flight and _version(diagnostic) != in_flight['item_version']:
                continue
            # Exposure excludes new independent items, not the child's current
            # supported response. Scoring still owns help/independence evidence.
            if not practice_item(c, session['child_id'], candidate[0], now, allow_recent=bool(in_flight)):
                continue
            return {**result, "status": "ready", "reason_code": "qualified_original_node",
                    "reason": "原节点、动作、前置和题源已核对；需绑定真实诊断待答后才能读题。",
                    "diagnostic_item_id": candidate[0], "item_version": _version(diagnostic),
                    "diagnostic_item": {key: diagnostic.get(key) for key in _ITEM_FIELDS}}
        return blocked("qualified_item_unavailable", "原知识点暂无当前合格且未近期重复的题目，不猜题或换到其他知识点。")
