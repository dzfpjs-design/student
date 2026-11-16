"""Read-only local behavior dependencies for frozen period advice, not authority."""
from datetime import date, timedelta
import hashlib
import json

from .family_clock import evidence_calendar_date
from .behavior_model.report_dependencies import REPORT_DEPENDENCY_VERSION, habit_dependencies, ownership_issues


def period_behavior_fingerprint(c, child_id, start, end):
    return _period_behavior_fingerprint(c, child_id, start, end)


def _period_behavior_fingerprint(c, child_id, start, end, *, habits=None):
    from .behavior_model import service as behavior

    start, end = date.fromisoformat(start), date.fromisoformat(end)
    coarse = ((start - timedelta(days=1)).isoformat(), end.isoformat())
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    payload = {'version': 'period_behavior_source_v1', 'child_id': child_id,
               'projection_version': getattr(behavior, 'DAILY_PROJECTION_VERSION', 'legacy_prefix'),
               'dependencies_version': REPORT_DEPENDENCY_VERSION}

    def rows(table, sql, args):
        return [dict(r) for r in c.execute(sql, args)] if table in tables else []

    def in_period(value):
        return value is not None and start <= evidence_calendar_date(value) <= end

    payload['habits'] = habit_dependencies(c, child_id, tables) if habits is None else habits
    payload['ownership_issues'] = ownership_issues(c, child_id, start=start, end=end)
    feedback = rows('intervention', '''SELECT * FROM intervention WHERE child_id=?
        AND date(feedback_at) BETWEEN ? AND ? ORDER BY intervention_id''', (child_id, *coarse))
    payload['feedback'] = [{k: v for k, v in r.items() if k != 'outcome_json'}
                           for r in feedback if in_period(r['feedback_at'])]
    referenced = {r['source_event_id'] for r in payload['feedback']}
    legacy = rows('intervention_record', '''SELECT * FROM intervention_record WHERE child_id=?
        AND child_response IS NOT NULL AND date(COALESCE(responded_at,created_at)) BETWEEN ? AND ?
        ORDER BY intervention_id''', (child_id, *coarse))
    payload['legacy_feedback'] = [r for r in legacy if in_period(r['responded_at'] or r['created_at'])]

    scope = '(oe.child_id=? AND date(oe.occurred_at) BETWEEN ? AND ?)'
    args = (child_id, *coarse)
    if 'intervention' in tables:
        scope += ''' OR EXISTS(SELECT 1 FROM intervention i WHERE i.child_id=?
            AND i.source_event_id=oe.event_id AND date(i.feedback_at) BETWEEN ? AND ?)'''
        args += (child_id, *coarse)
    observations = rows('observable_event',
        f'SELECT oe.* FROM observable_event oe WHERE {scope} ORDER BY oe.event_id', args)
    observations = [r for r in observations
                    if r['event_id'] in referenced or (r['child_id'] == child_id and in_period(r['occurred_at']))]
    payload['observations'] = observations
    event_ids = {r['event_id'] for r in observations}
    for table, key, order in [('probabilistic_state', 'states', 'state_id'),
                              ('behavior_retraction', 'retractions', 'event_id')]:
        selected = rows(table, f'''SELECT value.* FROM {table} value
            JOIN observable_event oe ON oe.event_id=value.event_id
            WHERE {scope} ORDER BY value.{order}''', args) if 'observable_event' in tables else []
        payload[key] = [r for r in selected if r['event_id'] in event_ids]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()
