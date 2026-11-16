"""Pure projections of existing behavior records; never infer camera uptime."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import math
import hashlib
from collections import defaultdict

from ..family_clock import family_today


COVERAGE_VERSION = "observation_coverage_v1"
BEHAVIOR_CALENDAR_VERSION = 'nanjing_behavior_calendar_v1'
COVERAGE_EVENT_TYPES = {"observation.stream_started", "observation.sample", "observation.stream_stopped"}
MAX_SAMPLE_GAP_SECONDS = 5
MAX_CAPTURE_LATENCY_SECONDS = 5
MAX_CLOCK_DRIFT_SECONDS = .25
OBSERVATION_STATE_EVENTS = {
    "present": "presence.seated", "away": "presence.away",
    "writing_like": "work.writing", "paused_like": "work.stall",
}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def task_context_snapshot(values: dict) -> dict:
    keys = ("plan_item_id", "subject", "task_type", "cognitive_load", "source_type", "plan_version")
    result = {key: values[key] for key in keys}
    if any(not isinstance(result[key], str) or not result[key].strip() or len(result[key]) > 200 for key in keys[:-1]):
        raise ValueError("Invalid task context")
    if type(result["plan_version"]) is not int or result["plan_version"] < 1:
        raise ValueError("Invalid plan version")
    result["context_version"] = _digest(result)
    result["stratum_key"] = _digest({key: result[key] for key in ("subject", "task_type", "cognitive_load", "source_type")})
    return result


def _current_support_context(connection, child_id, session_id, occurred_at, now):
    tables = {row[0] for row in connection.execute("""SELECT name FROM sqlite_master WHERE type='table'
        AND name IN ('plan_item','daily_plan','daily_session','plan_item_event')""")}
    if tables != {'plan_item','daily_plan','daily_session','plan_item_event'}:
        return None
    tasks = connection.execute("""SELECT pi.*,dp.version AS plan_version,ds.started_at AS session_started_at FROM plan_item pi
        JOIN daily_plan dp ON dp.plan_id=pi.plan_id
        JOIN daily_session ds ON ds.session_id=dp.session_id AND ds.child_id=dp.child_id
        WHERE dp.child_id=? AND dp.session_id=? AND dp.status='active'
          AND ds.status='active' AND ds.state='study' AND pi.status='active'""", (child_id, session_id)).fetchall()
    if len(tasks) != 1:
        return None
    task = dict(tasks[0])
    if any(not isinstance(task.get(key), str) or not task[key].strip()
           for key in ('plan_item_id','subject','task_type','cognitive_load','source_type')):
        return None
    context = task_context_snapshot(task)
    opening = connection.execute("""SELECT occurred_at FROM plan_item_event
        WHERE plan_item_id=? AND event_type IN ('started','resumed') ORDER BY occurred_at DESC LIMIT 1""",
        (task['plan_item_id'],)).fetchone()
    if not opening or not parse_time(task['session_started_at']) <= parse_time(opening['occurred_at']) <= parse_time(occurred_at) <= now:
        return None
    return context


def accepted_habit_support(connection, child_id, session_id, candidate_state, occurred_at, now):
    """An optional accepted instruction, not evidence of independent success."""
    from .goal_confirmation import goal_version, opportunities

    context = _current_support_context(connection, child_id, session_id, occurred_at, now)
    if context is None:
        return None
    goal = connection.execute("""SELECT * FROM habit_experiment WHERE child_id=?
        AND target_state=? AND status='active' ORDER BY started_at DESC LIMIT 1""",
        (child_id,candidate_state)).fetchone()
    if not goal or not goal['started_at'] or not goal['ends_at']:
        return None
    from .habit_formation import auto_stop_pending
    if auto_stop_pending(connection, child_id, goal['experiment_id']):
        return None
    if not parse_time(goal['started_at']) <= now < parse_time(goal['ends_at']):
        return None
    version = goal_version(dict(goal))
    receipts = connection.execute("""SELECT event_id,payload_json,occurred_at FROM habit_experiment_event
        WHERE child_id=? AND experiment_id=? AND event_type='accepted' ORDER BY rowid DESC""",
        (child_id,goal['experiment_id'])).fetchall()
    accepted = None
    for row in receipts:
        payload = json_value(row['payload_json'],dict,{})
        if (parse_time(row['occurred_at']) <= now and payload.get('goal_version')==version
            and payload.get('accepted') is True):
            accepted = row
            break
    if not accepted:
        return None
    view = opportunities(connection,child_id,goal['experiment_id'],now)
    matching = [item for item in view['opportunities'] if item['source_current']
        and item['observed_seconds'] > 0 and parse_time(item['window_end']) <= now
        and all(item['context'].get(key)==context[key] for key in ('subject','task_type','cognitive_load','source_type'))]
    if not matching:
        return None
    return dict(version='accepted_habit_support_v1',basis='accepted_definition_with_observed_context',
        experiment_id=goal['experiment_id'],goal_version=version,acceptance_ref=accepted['event_id'],
        plan_item_id=context['plan_item_id'],context_version=context['context_version'],stratum_key=context['stratum_key'],
        evidence_version=_digest([version,sorted((item['opportunity_id'],item['source_version']) for item in matching)]),
        child_action=goal['child_action'],completion_condition=False,bounded_goal_success=False,
        habit_success=False,burden_status='unknown',local_only=True,cloud_transmission_allowed=False)


def goal_support_basis(connection, child_id, session_id, candidate_state, occurred_at, now):
    """Read current bounded evidence for less intrusive support, never habit credit.

    The caller owns the transaction. Historical decision receipts are not inputs.
    """
    from .goal_confirmation import assessment, opportunities

    context = _current_support_context(connection, child_id, session_id, occurred_at, now)
    if context is None:
        return None
    goals = connection.execute("""SELECT experiment_id,started_at,ends_at FROM habit_experiment
        WHERE child_id=? AND target_state=? AND status IN ('active','completed')
        AND started_at IS NOT NULL ORDER BY started_at DESC,experiment_id""", (child_id, candidate_state)).fetchall()
    for goal in goals:
        if parse_time(goal['started_at']) > now:
            continue
        if not goal['ends_at'] or parse_time(goal['ends_at']) < now - timedelta(days=14):
            continue
        view = opportunities(connection, child_id, goal['experiment_id'], now)
        current = [item for item in view['opportunities'] if item['phase']=='current']
        matching = [item for item in current if all(item['context'].get(key)==context[key]
            for key in ('subject','task_type','cognitive_load','source_type'))]
        if not matching:
            continue
        # A future witness must not retroactively authorize a past decision.
        if any(item['confirmation'] and parse_time(item['confirmation']['occurred_at']) > now for item in current):
            continue
        result = assessment(connection, child_id, goal['experiment_id'], now)
        if result['status'] != 'bounded_confirmed_behavior' or not result['bounded_goal_success']:
            continue
        relevant = [result['current']['strata'].get(item['stratum_key']) for item in matching]
        if any(not row or (row['success_lower_bound'] or 0) < result['policy']['minimum_success_lower_bound'] for row in relevant):
            continue
        # A stale/withdrawn opportunity cannot become a positive policy source.
        if any(not item['source_current'] for item in matching):
            continue
        last_observed = max(parse_time(item['window_end']) for item in matching)
        if now-last_observed > timedelta(days=14):
            continue
        references = sorted(item['confirmation']['event_id'] for item in matching if item['confirmation'])
        source_versions = sorted((item['opportunity_id'],item['binding_version']) for item in current)
        return dict(version='goal_support_policy_v1', experiment_id=goal['experiment_id'],
            goal_version=view['goal_version'], context_version=context['context_version'],
            plan_item_id=context['plan_item_id'], stratum_key=context['stratum_key'],
            evidence_version=_digest([view['goal_version'],source_versions,result['current']]),
            confirmation_refs=references, basis='current_bounded_independent_behavior',
            habit_success=False, burden_status='unknown', cooldown_minutes=20, daily_cap=2,
            local_only=True, cloud_transmission_allowed=False)
    return None


def validate_coverage_sample(sample: dict, *, _context_snapshot=None) -> None:
    keys = {"schema_version", "child_id", "session_id", "stream_epoch", "sequence", "captured_at",
            "capture_monotonic_ms", "received_at", "quality", "task_context", "state", "confidence", "model_version", "analysis_id", "producer_epoch"}
    if (not isinstance(sample, dict) or not keys <= set(sample)
            or set(sample) - keys - {"state_event_id"} or sample["schema_version"] != COVERAGE_VERSION):
        raise ValueError("Invalid coverage sample contract")
    if sample.get("state_event_id") is not None and (not isinstance(sample["state_event_id"], str)
            or not sample["state_event_id"].strip() or len(sample["state_event_id"]) > 200):
        raise ValueError("Invalid observation source reference")
    for key in ("child_id", "session_id", "stream_epoch", "analysis_id", "producer_epoch", "model_version", "state"):
        if not isinstance(sample[key], str) or not sample[key].strip() or len(sample[key]) > 200:
            raise ValueError(f"Invalid coverage {key}")
    for key in ("sequence", "capture_monotonic_ms"):
        if type(sample[key]) is not int or not 0 <= sample[key] <= 2**53 - 1:
            raise ValueError(f"Invalid coverage {key}")
    parse_time(sample["captured_at"])
    parse_time(sample["received_at"])
    if type(sample["confidence"]) not in (int, float) or not 0 <= sample["confidence"] <= 1:
        raise ValueError("Invalid coverage confidence")
    quality = sample["quality"]
    if not isinstance(quality, dict) or set(quality) != {"observation_ready", "scene_quality", "person_baseline", "shadow", "analysis_skipped"}:
        raise ValueError("Invalid coverage quality")
    if any(type(quality[key]) is not bool for key in ("observation_ready", "shadow", "analysis_skipped")):
        raise ValueError("Invalid coverage quality flags")
    if quality["scene_quality"] not in {"ready", "too_dark", "too_bright", "blurred", "unusable_frame", "not_evaluated"}:
        raise ValueError("Invalid scene quality")
    if quality["person_baseline"] not in {"established", "awaiting_person", "not_evaluated"}:
        raise ValueError("Invalid person baseline")
    if sample["task_context"] is not None:
        context = sample["task_context"]
        try:
            valid = isinstance(context, dict) and context == (_context_snapshot or task_context_snapshot)(context)
        except (KeyError, TypeError):
            valid = False
        if not valid:
            raise ValueError("Invalid task context snapshot")


def coverage_projection(rows, child_id: str, start: datetime, end: datetime, *, include_opportunities=False) -> dict:
    """Replay append order. Missing/retracted controls remain continuity barriers."""
    sessions, declared, intervals = {}, [], []
    reasons, sample_ids = set(), set()
    contexts, stratum_keys, opportunity_keys = {}, {}, {}

    def context_snapshot(context):
        # Pure, typed JSON content only, local to this replay. Sample timing,
        # quality, ownership, lineage and all stream controls are still checked.
        key = json.dumps(context, sort_keys=True, ensure_ascii=True, allow_nan=False)
        if key not in contexts:
            contexts[key] = task_context_snapshot(context)
        return contexts[key]
    for row in rows:
        if row["child_id"] != child_id or row["event_type"] not in COVERAGE_EVENT_TYPES:
            continue
        payload = json_value(row["payload_json"], dict, {})
        try:
            received = parse_time(row["occurred_at"])
        except (ValueError, TypeError, AttributeError):
            reasons.add("invalid_record_time")
            sessions.pop(row["session_id"], None)
            continue
        session = sessions.get(row["session_id"])
        epoch = payload.get("stream_epoch")
        retracted = bool(row["retracted"])
        if row["event_type"] == "observation.stream_started":
            if session:
                session["window"][1] = min(session["window"][1], received)
            window = [received, end]
            declared.append(window)
            sessions[row["session_id"]] = {
                "epoch": epoch, "open_id": row["event_id"], "window": window,
                "active": not retracted, "previous": None, "sequence": -1,
            }
            continue
        if row["event_type"] == "observation.stream_stopped":
            if session and session["epoch"] == epoch:
                session["active"] = False
                session["previous"] = None
                session["window"][1] = received
            continue
        try:
            validate_coverage_sample(payload, _context_snapshot=context_snapshot)
            captured = parse_time(payload["captured_at"])
        except (ValueError, TypeError, AttributeError):
            if session:
                session["previous"] = None
            reasons.add("invalid_sample")
            continue
        in_window = start <= captured <= end
        if in_window:
            sample_ids.add(row["event_id"])
        def unknown(reason):
            if in_window:
                reasons.add(reason)
        if not session or not session["active"] or session["epoch"] != epoch:
            unknown("inactive_epoch")
            continue
        previous = session["previous"]
        session["previous"] = None
        sequence = payload["sequence"]
        if sequence <= session["sequence"]:
            unknown("out_of_order")
            continue
        session["sequence"] = sequence
        refs = json_value(row["source_refs_json"], list, [])
        if retracted or session["open_id"] not in refs:
            unknown("retracted_or_missing_source")
            continue
        quality = payload["quality"]
        if not (quality["observation_ready"] and quality["scene_quality"] == "ready"
                and quality["person_baseline"] == "established" and not quality["analysis_skipped"]):
            unknown("unqualified_sample")
            continue
        if payload["task_context"] is None:
            unknown("unknown_task_context")
            continue
        latency = (received - captured).total_seconds()
        if not 0 <= latency <= MAX_CAPTURE_LATENCY_SECONDS or captured < session["window"][0]:
            unknown("stale_or_unsynchronized_capture")
            continue
        point = {"id": row["event_id"], "captured": captured, "received": received,
                 "sample": payload, "run_id": row["event_id"],
                 "behavior_source_invalid": bool(row["behavior_source_invalid"])}
        session["previous"] = point
        if previous is None:
            continue
        old = previous["sample"]
        gap = (captured - previous["captured"]).total_seconds()
        mono_gap = (payload["capture_monotonic_ms"] - old["capture_monotonic_ms"]) / 1000
        receipt_gap = (received - previous["received"]).total_seconds()
        if sequence != old["sequence"] + 1 or previous["id"] not in refs:
            unknown("missing_sequence_or_source")
            continue
        if not 0 < gap <= MAX_SAMPLE_GAP_SECONDS or not 0 < mono_gap <= MAX_SAMPLE_GAP_SECONDS:
            unknown("sampling_gap")
            continue
        if abs(gap - mono_gap) > MAX_CLOCK_DRIFT_SECONDS or not 0 <= receipt_gap <= MAX_SAMPLE_GAP_SECONDS + MAX_CAPTURE_LATENCY_SECONDS:
            unknown("clock_discontinuity")
            continue
        if payload["producer_epoch"] != old["producer_epoch"]:
            unknown("producer_restarted")
            continue
        if payload["task_context"] != old["task_context"] or payload["model_version"] != old["model_version"]:
            unknown("context_or_model_changed")
            continue
        if (payload["state"] == old["state"] and payload["quality"]["shadow"] == old["quality"]["shadow"]
                and not point["behavior_source_invalid"] and not previous["behavior_source_invalid"]):
            point["run_id"] = previous["run_id"]
        left, right = max(start, previous["captured"]), min(end, captured)
        if left < right:
            intervals.append({"start": left, "end": right, "context": payload["task_context"],
                              "points": [previous, point], "open_id": session["open_id"], "window": session["window"]})

    for interval in intervals:
        interval["start"] = max(interval["start"], interval["window"][0])
        interval["end"] = min(interval["end"], interval["window"][1])
    intervals = [item for item in intervals if item["start"] < item["end"]]
    # Conflicting streams are unknown, not extra exposure or majority-voted states.
    conflicted, active = set(), []
    for index in sorted(range(len(intervals)), key=lambda i: intervals[i]["start"]):
        interval = intervals[index]
        active = [i for i in active if intervals[i]["end"] > interval["start"]]
        if active:
            conflicted.update([index, *active])
        active.append(index)
    if conflicted:
        reasons.add("overlapping_streams")
    groups, refs, opportunities = {}, set(), {}
    source_rows = {row['event_id']: dict(row) for row in rows} if include_opportunities else {}
    for index, interval in enumerate(intervals):
        if index in conflicted:
            continue
        ctx = interval["context"]
        model = interval["points"][0]["sample"]["model_version"]
        stratum = (ctx["stratum_key"], model)
        if stratum not in stratum_keys:
            stratum_keys[stratum] = _digest([ctx["stratum_key"], model, COVERAGE_VERSION])
        stratum_key = stratum_keys[stratum]
        group = groups.setdefault(stratum_key, {
            "stratum_key": stratum_key, "model_version": model,
            "context": {key: ctx[key] for key in ("subject", "task_type", "cognitive_load", "source_type")},
            "observed_seconds": 0.0, "tasks": set(), "samples": set(), "episodes": defaultdict(set), "days": set(),
            "behavior_seconds": 0.0, "shadow_seconds": 0.0, "behavior_tasks": set(), "behavior_days": set(),
        })
        duration = (interval["end"] - interval["start"]).total_seconds()
        group["observed_seconds"] += duration
        group["tasks"].add(ctx["plan_item_id"])
        group["days"].add(family_today(interval["start"]).isoformat())
        shadow = any(point["sample"]["quality"]["shadow"] for point in interval["points"])
        behavior_eligible = not shadow and all(
            point["sample"]["confidence"] >= .6 and point["sample"]["state"] in OBSERVATION_STATE_EVENTS
            and not point["behavior_source_invalid"]
            for point in interval["points"]
        )
        if shadow:
            group["shadow_seconds"] += duration
        if behavior_eligible:
            group["behavior_seconds"] += duration
            group["behavior_tasks"].add(ctx["plan_item_id"])
            group["behavior_days"].add(family_today(interval["start"]).isoformat())
        if include_opportunities and not shadow:
            session_id = interval['points'][0]['sample']['session_id']
            day = family_today(interval['start']).isoformat()
            identity = (session_id, tuple(sorted(ctx.items())), model, day)
            if identity not in opportunity_keys:
                opportunity_keys[identity] = _digest([child_id, session_id, ctx, model, day])
            key = opportunity_keys[identity]
            opportunity = opportunities.setdefault(key, dict(
                source_key=key, session_id=session_id, day=day, context=ctx, stratum_key=stratum_key,
                observed_seconds=0.0, behavior_seconds=0.0, intervals=[], source_refs=set()))
            opportunity['observed_seconds'] += duration
            opportunity['behavior_seconds'] += duration if behavior_eligible else 0
            opportunity['intervals'].append([interval['start'].isoformat(), interval['end'].isoformat()])
            opportunity['source_refs'].update([interval['open_id'], *(p['id'] for p in interval['points'])])
        refs.add(interval["open_id"])
        for point in interval["points"]:
            refs.add(point["id"])
            if start <= point["captured"] < end:
                group["samples"].add(point["id"])
                sample = point["sample"]
                kind = OBSERVATION_STATE_EVENTS.get(sample["state"])
                if behavior_eligible:
                    group["episodes"][kind].add(point["run_id"])
    strata = []
    for group in groups.values():
        counts = {key: len(value) for key, value in group["episodes"].items()}
        strata.append({"stratum_key": group["stratum_key"], "context": group["context"],
                       "model_version": group["model_version"],
                       "observed_seconds": round(group["observed_seconds"], 6),
                       "behavior_observed_seconds": round(group["behavior_seconds"], 6),
                       "shadow_observed_seconds": round(group["shadow_seconds"], 6),
                       "behavior_task_opportunities": len(group["behavior_tasks"]),
                       "behavior_observed_days": len(group["behavior_days"]),
                       "task_opportunities": len(group["tasks"]), "sample_opportunities": len(group["samples"]),
                       "observed_days": len(group["days"]), "event_counts": counts,
                       "rates_per_hour": {key: value * 3600 / group["behavior_seconds"] for key, value in counts.items()}})
    seconds = sum(item["observed_seconds"] for item in strata)
    behavior_seconds = sum(item["behavior_observed_seconds"] for item in strata)
    union_end, declared_seconds = start, 0.0
    for left, right in sorted((max(start, left), min(end, right)) for left, right in declared):
        left = max(left, union_end)
        if left < right:
            declared_seconds += (right - left).total_seconds()
            union_end = right
    unknown_seconds = max(0.0, declared_seconds - seconds)
    if unknown_seconds > .001:
        reasons.add("unobserved_stream_time")
    if not strata:
        reasons.add("no_valid_sample_intervals")
    result = {
        "schema_version": COVERAGE_VERSION, "child_id": child_id,
        "projection_version": "qualified_exposure_and_behavior_v2",
        "status": "measured" if seconds > 0 and not reasons else "partial" if seconds > 0 else "unknown",
        "measurement_basis": "qualified_sample_intervals", "observed_seconds": round(seconds, 6) if sample_ids else None,
        "behavior_measurement_basis": "non_shadow_classified_intervals",
        "behavior_observed_seconds": round(behavior_seconds, 6) if sample_ids else None,
        "behavior_status": "measured" if behavior_seconds > 0 and behavior_seconds == seconds else "partial" if behavior_seconds > 0 else "unknown",
        "shadow_observed_seconds": sum(item["shadow_observed_seconds"] for item in strata),
        "behavior_unknown_seconds": round(seconds - behavior_seconds, 6),
        "declared_stream_seconds": declared_seconds, "unknown_seconds": round(unknown_seconds, 6),
        "coverage_fraction": seconds / declared_seconds if declared_seconds > 0 else None,
        "task_opportunities": len(set().union(*(item["tasks"] for item in groups.values()))) if strata else None,
        "observed_days": len(set().union(*(item["days"] for item in groups.values()))) if strata else 0,
        "sample_count": len(sample_ids), "strata": strata, "evidence_refs": sorted(refs),
        "unknown_reasons": sorted(reasons), "max_sample_gap_seconds": MAX_SAMPLE_GAP_SECONDS,
        "boundary": "Sample-qualified exposure is not continuous video observation, task completion, independence, or a habit outcome.",
    }
    if include_opportunities:
        result['opportunities'] = []
        for item in opportunities.values():
            item['source_refs'] = sorted(item['source_refs'])
            item['source_version'] = _digest([source_rows[ref] for ref in item['source_refs']])
            result['opportunities'].append(item)
    return result


def compare_coverage(baseline: dict, current: dict, event_type: str) -> dict:
    before = {item["stratum_key"]: item for item in baseline["strata"]}
    after = {item["stratum_key"]: item for item in current["strata"]}
    common = before.keys() & after.keys()
    result = {"direction": "not_yet_clear", "status": "insufficient_evidence", "rate_direction": "not_yet_clear",
              "baseline_rate_per_hour": None, "current_rate_per_hour": None,
              "missing_evidence": ["goal_behavior_confirmation", "representative_repeated_opportunities"],
              "context_scope": "subject/task_type/cognitive_load/source_type/model_version",
              "matched_strata": sorted(common), "unmatched_strata": sorted(before.keys() ^ after.keys())}
    if event_type not in OBSERVATION_STATE_EVENTS.values():
        result["missing_evidence"].append("target_event_not_observed")
        return result
    if not common or before.keys() != after.keys():
        result["missing_evidence"].append("comparable_task_coverage")
        return result
    if any(item["behavior_observed_seconds"] <= 0 for key in common for item in (before[key], after[key])):
        result["missing_evidence"].append("qualified_behavior_observations")
        return result
    total = sum(before[key]["behavior_observed_seconds"] for key in common)
    prior = sum(before[key]["event_counts"].get(event_type, 0) * 3600 for key in common) / total
    recent = sum(before[key]["behavior_observed_seconds"] / total * after[key]["event_counts"].get(event_type, 0)
                 * 3600 / after[key]["behavior_observed_seconds"] for key in common)
    result.update(baseline_rate_per_hour=prior, current_rate_per_hour=recent)
    if all(item["behavior_observed_days"] >= 3 and item["behavior_task_opportunities"] >= 5 and item["behavior_observed_seconds"] >= 600
           for key in common for item in (before[key], after[key])):
        result["missing_evidence"].remove("representative_repeated_opportunities")
    if (baseline["coverage_fraction"] or 0) < .75 or (current["coverage_fraction"] or 0) < .75:
        result["missing_evidence"].append("adequate_observation_coverage")
    elif any(item["behavior_observed_seconds"] / item["observed_seconds"] < .75 for key in common for item in (before[key], after[key])):
        result["missing_evidence"].append("reliable_behavior_classification")
    else:
        result["rate_direction"] = "lower" if recent < prior * .8 else "higher" if recent > prior * 1.2 else "stable"
    return result


def parse_time(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Evidence timestamps must include a timezone")
    return parsed.astimezone(timezone.utc)


def json_value(value, expected_type, default):
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return default
    return decoded if isinstance(decoded, expected_type) else default


def qualified_observation(row):
    payload = json_value(row["payload_json"], dict, None)
    missing = json_value(row["missing_sources_json"], list, None)
    confidence = row["confidence"]
    return (
        payload is not None and missing is not None
        and isinstance(confidence, (int, float)) and .6 <= confidence <= 1
        and payload.get("shadow") is not True
        and not (row["source"] in {"local_vision", "vision"} and "local_vision" in missing)
    )


def observation_summary(rows, event_type=None, start=None, end=None):
    events = []
    for row in rows:
        try:
            at = parse_time(row["occurred_at"])
        except (TypeError, ValueError, AttributeError):
            continue
        if (start is None or at >= start) and (end is None or at < end):
            events.append(row)
    valid = [row for row in events if qualified_observation(row)]
    targets = [row for row in valid if event_type is None or row["event_type"] == event_type]
    minutes = []
    for row in valid:
        if row["event_type"] == "task.finished":
            value = json_value(row["payload_json"], dict, {}).get("actual_minutes")
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
                minutes.append(value)
    # duration_ms is state age (or audio length), not non-overlapping valid
    # observation intervals. Plan starts/finishes lack stable task context here.
    return {
        "status": "insufficient_evidence", "event_count": len(targets),
        "observed_days": len({family_today(parse_time(row["occurred_at"])) for row in valid}),
        "missing_event_count": sum(bool(json_value(row["missing_sources_json"], list, ["unknown"])) for row in events),
        "excluded_event_count": len(events) - len(valid),
        "observed_seconds": None, "task_opportunities": None, "event_rate": None,
        "task_started_count": sum(row["event_type"] == "task.started" for row in valid),
        "task_finished_count": sum(row["event_type"] == "task.finished" for row in valid),
        "recorded_task_minutes": sum(minutes) if minutes else None,
        "evidence_refs": [row["event_id"] for row in targets],
        "missing_evidence": ["valid_observation_intervals", "comparable_task_opportunities", "goal_behavior_confirmation"],
    }


def effect_summary(rows, now):
    helpful_states = {"writing", "reading", "task_finished"}
    unresolved_states = {"blocked_candidate", "distraction_candidate", "fatigue_candidate", "frequent_switching"}
    eligible = []
    opportunities = set()
    for row in rows:
        if row["intervention_type"] != "default_ask_first" or row["feedback"] != "accepted":
            continue
        source = row["source_event_id"]
        if not source:
            continue
        opportunities.add(source)
        outcome = json_value(row["outcome_json"], dict, {})
        probability = outcome.get("probability")
        if (outcome.get("state") not in helpful_states | unresolved_states
                or not isinstance(probability, (int, float)) or isinstance(probability, bool)
                or not .6 <= probability <= 1):
            continue
        try:
            decided = parse_time(row["decided_at"])
            feedback = parse_time(row["feedback_at"])
            observed = parse_time(outcome.get("observed_at"))
        except (TypeError, ValueError, AttributeError):
            continue
        if not decided <= feedback <= observed <= min(now, decided + timedelta(minutes=15)):
            continue
        if not qualified_observation(row):
            continue
        if source in {item[0] for item in eligible}:
            continue
        eligible.append((source, row["intervention_id"], family_today(decided), outcome["state"] in helpful_states))
    helpful = sum(item[3] for item in eligible)
    count = len(eligible)
    days = len({item[2] for item in eligible})
    coverage = count / len(opportunities) if opportunities else None
    sufficient = count >= 4 and days >= 2 and coverage >= .75
    status = "insufficient_evidence"
    if sufficient:
        status = "limited_recovery" if helpful / count <= .25 else (
            "recovery_observed" if helpful / count >= .75 else "not_yet_clear"
        )
    return {
        "effect_samples": count, "helpful_effects": helpful,
        "unresolved_effects": count - helpful, "effect_days": days,
        "effect_opportunities": len(opportunities), "effect_coverage": coverage,
        "effect_status": status, "minimum_effect_samples": 4, "minimum_effect_days": 2,
        "minimum_effect_coverage": .75, "effect_window_minutes": 15,
        "effect_evidence_refs": [item[1] for item in eligible],
        "effect_strategy": "default_ask_first",
    }


def report_habit_outcome(raw):
    outcome = json_value(raw, dict, None)
    if outcome is None:
        return None
    if outcome.get("evaluation_version") != "coverage_required_v1":
        return {"direction": "not_yet_clear", "status": "insufficient_evidence"}
    def redact(value):
        if isinstance(value, dict):
            return {key: redact(item) for key, item in value.items() if key not in {
                "evidence_refs", "child_id", "goal_version", "source_refs", "source_version", "evidence_versions"}}
        if isinstance(value, list):
            return [redact(item) for item in value]
        return value
    return redact(outcome)
