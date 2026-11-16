from copy import deepcopy

from home_study_agent.cloud_parent_reports import _report_comparison


def test_only_active_habit_calculation_clock_is_ignored_without_mutating_report():
    summary = {'facts': ['synthetic confirmed task'], 'other_window_end': 'original'}
    coaching = {'personalization': {'active_habit_experiment': {'status': 'active', 'outcome': {
        'window_start': '2026-01-01', 'window_end': '2026-01-02',
        'current': {'observed_seconds': 60}, 'direction': 'not_yet_clear'}}}}
    original = deepcopy(coaching)
    before = _report_comparison(summary, coaching)
    assert coaching == original
    coaching['personalization']['active_habit_experiment']['outcome']['window_end'] = '2026-01-02T00:00:01'
    assert _report_comparison(summary, coaching) == before
    for path, value in [
        (('status',), 'completed'),
        (('outcome', 'window_start'), '2026-01-02'),
        (('outcome', 'direction'), 'improving'),
        (('outcome', 'current', 'observed_seconds'), 120),
    ]:
        changed = deepcopy(original)
        target = changed['personalization']['active_habit_experiment']
        for part in path[:-1]:
            target = target[part]
        target[path[-1]] = value
        assert _report_comparison(summary, changed) != before
    assert _report_comparison({**summary, 'other_window_end': 'changed'}, original) != before
