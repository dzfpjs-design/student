"""Read-only dependencies of the habits actually exposed to parent reports."""
from datetime import datetime, timedelta, timezone

from .outcome_evidence import coverage_projection, json_value, parse_time


REPORT_DEPENDENCY_VERSION = 'scoped_habit_sources_owner_v1'
REPORT_HABIT_ORDER = 'proposed_at DESC,experiment_id'


def rows(c, sql, args=()):
    from ..report_source_reads import report_dict_rows
    return report_dict_rows(c, sql, args)


def _instant(value):
    try:
        return parse_time(value)
    except (ValueError, TypeError, AttributeError):
        return None


def ownership_issues(c, child_id, session_id=None, start=None, end=None):
    """Count excluded legacy links without revealing the other owner's identity."""
    from ..family_clock import evidence_calendar_date

    counts = {}
    for table, ref, time in (('probabilistic_state', 'event_id', 'occurred_at'),
                             ('intervention', 'source_event_id', 'feedback_at')):
        if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            continue
        selected = rows(c, f'''SELECT v.child_id,v.session_id,v.{time} AS at,
            oe.child_id AS source_child,oe.session_id AS source_session,oe.occurred_at AS source_at
            FROM {table} v LEFT JOIN observable_event oe ON oe.event_id=v.{ref}
            WHERE (v.child_id=? OR oe.child_id=?) AND v.{ref} IS NOT NULL
            AND (oe.event_id IS NULL OR v.child_id!=oe.child_id OR v.session_id IS NOT oe.session_id)''', (child_id, child_id))
        count = 0
        for row in selected:
            if session_id is not None and session_id not in (row['session_id'], row['source_session']):
                continue
            times = [_instant(row['at']), _instant(row['source_at'])]
            if (start or end) and not any(
                (not start or evidence_calendar_date(t.isoformat()) >= evidence_calendar_date(str(start)))
                and (not end or evidence_calendar_date(t.isoformat()) <= evidence_calendar_date(str(end)))
                for t in times if t is not None):
                continue
            count += 1
        if count:
            counts[table + '_source_owner_mismatch'] = count
    return counts


def _coverage_sources(records, left, right):
    # Replay complete stream runs: an earlier open/sequence or later stop can
    # invalidate an interval inside the window. Unrelated closed runs do not bind.
    runs, active = [], {}
    for row in records:
        sid = row['session_id']
        payload = json_value(row['payload_json'], dict, {})
        at = _instant(row['occurred_at'])
        if row['event_type'] == 'observation.stream_started':
            if sid in active:
                active[sid]['end'] = at
            run = dict(start=at, end=None, epoch=payload.get('stream_epoch'), records=[])
            active[sid] = run
            runs.append(run)
        run = active.get(sid)
        if run is None:
            run = dict(start=at, end=at, epoch=None, records=[])
            runs.append(run)
        run['records'].append(dict(row))
        if row['event_type'] == 'observation.stream_stopped' and payload.get('stream_epoch') == run['epoch']:
            run['end'] = at
    selected = []
    for run in runs:
        overlaps = run['start'] is not None and run['start'] < right and (run['end'] is None or run['end'] >= left)
        if overlaps or any(left <= at <= right for r in run['records']
                           for at in [_instant(json_value(r['payload_json'], dict, {}).get('captured_at'))]
                           if at is not None):
            selected.extend(run['records'])
    return selected


def habit_dependencies(c, child_id, tables, now=None):
    """No writes, refreshes, outcome caches, or report/snapshot dependencies."""
    from .goal_confirmation import _opportunities
    from .service import BehaviorModelService, DAILY_EVENT_FEATURES

    if 'habit_experiment' not in tables:
        return []
    now = now or datetime.now(timezone.utc)
    habits = rows(c, f'SELECT * FROM habit_experiment WHERE child_id=? ORDER BY {REPORT_HABIT_ORDER} LIMIT 3', (child_id,))
    result = []
    coverage = None
    for habit in habits:
        item = {k: v for k, v in habit.items() if k != 'outcome_json'}
        from .habit_formation import auto_stop_pending
        if auto_stop_pending(c, child_id, habit['experiment_id']):
            item['auto_stop_pending'] = True
        if 'habit_formation_state' in tables:
            formation = c.execute(
                """SELECT stage,cue_level,revision,transition_reason,policy_version,updated_at
                   FROM habit_formation_state
                   WHERE experiment_id=? AND child_id=?""",
                (habit['experiment_id'], child_id),
            ).fetchone()
            item['formation'] = ({**dict(formation), 'habit_success': False}
                                 if formation else None)
        result.append(item)
        started, ends = _instant(habit['started_at']), _instant(habit['ends_at'])
        item['expired'] = bool(habit['status'] == 'active' and ends and ends <= now)
        feature = json_value(habit['baseline_json'], dict, {}).get('feature')
        if habit['status'] not in {'active', 'completed', 'stopped'} or started is None or feature not in DAILY_EVENT_FEATURES:
            continue
        end = min(now, ends) if ends else now
        if habit['status'] == 'stopped' and habit['closed_at']:
            end = min(end, parse_time(habit['closed_at']))
        left = started - timedelta(days=14)
        originals = rows(c, '''SELECT oe.* FROM observable_event oe
            LEFT JOIN behavior_retraction br ON br.event_id=oe.event_id
            WHERE oe.child_id=? AND oe.source!='observation_coverage' AND br.event_id IS NULL
            AND julianday(oe.occurred_at)>=julianday(?) AND julianday(oe.occurred_at)<=julianday(?)
            ORDER BY oe.event_id''', (child_id, left.isoformat(), end.isoformat()))
        item['observations'] = [r for r in originals if (at := _instant(r['occurred_at'])) is not None and left <= at < end]
        if coverage is None:
            coverage = BehaviorModelService._coverage_records(c, child_id)
        item['coverage_sources'] = _coverage_sources(coverage, left, end)
        # Invalid controls may be outside the sampled interval but still break
        # continuity. Bind their actual effect, not irrelevant historical rows.
        issues = []
        view = _opportunities(c, child_id, habit['experiment_id'], now,
            coverage_observer=lambda projection: issues.append(projection['unknown_reasons']))
        item['coverage_issues'] = (issues if len(issues) == 2 else
            [coverage_projection(coverage, child_id, a, b)['unknown_reasons']
             for a, b in ((left, started), (started, end))])
        item['goal_sources'] = dict(goal_version=view['goal_version'], policy=view['policy'],
                                   opportunities=view['opportunities'])
    return result
