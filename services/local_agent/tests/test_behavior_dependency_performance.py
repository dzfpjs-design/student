"""Bounded read profiling; synthetic qualified observations, no model calls."""
import json
import os
from pathlib import Path
import time

from home_study_agent.daily_behavior_source import daily_behavior_fingerprint
from home_study_agent.period_behavior_source import period_behavior_fingerprint
from test_behavior_goal_confirmation import history, automatic_records, CHILD
from test_behavior_observation_coverage import managed


def test_qualified_habit_read_profile_is_readonly_and_stable(managed):
    db, behavior = history(managed, 'distraction_candidate')
    automatic_records(db, behavior)
    behavior.report_safe_context(CHILD, 'session')
    results = {}
    with db.connect() as c:
        c.execute('PRAGMA query_only=ON')
        events = c.execute('SELECT COUNT(*) FROM observable_event').fetchone()[0]
        for kind, call in [('daily', lambda: daily_behavior_fingerprint(c, CHILD, 'session')),
                           ('period', lambda: period_behavior_fingerprint(c, CHILD, '2026-08-01', '2026-08-31'))]:
            samples, queries, tokens = [], [], []
            for _ in range(3):
                sql = []
                c.set_trace_callback(sql.append)
                c.execute('BEGIN')
                start = time.perf_counter()
                tokens.append(call())
                samples.append(round((time.perf_counter() - start) * 1000, 3))
                c.rollback()
                c.set_trace_callback(None)
                queries.append(len(sql))
            assert len(set(tokens)) == 1
            assert c.total_changes == 0
            results[kind] = dict(ms=samples, statements=queries)
    if os.environ.get('HSC_DEPENDENCY_PROFILE'):
        Path(os.environ['HSC_DEPENDENCY_PROFILE']).write_text(json.dumps(dict(
            results=results, synthetic_events=events, task_opportunities=12,
            model_calls=0, readonly=True, connections_closed=True), indent=2) + '\n')
