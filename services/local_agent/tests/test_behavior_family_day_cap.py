"""Synthetic observations through real decision/feedback persistence, no model."""
from datetime import datetime, timedelta, timezone

import pytest

from home_study_agent.behavior_model import BehaviorModelService, ObservableEvent
from home_study_agent.family_clock import family_today
from test_behavior_observation_coverage import managed

CHILD = 'child-primary-4'
SESSION = 'session'


def decide(service, stamp, key, *, accepted=True, child=CHILD, session=SESSION):
    instant = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
    service.ingest(ObservableEvent(child_id=child, session_id=session,
        occurred_at=(instant-timedelta(seconds=1)).astimezone(timezone.utc).isoformat(),
        event_id=key, event_type='work.stall', source='local.sensor', confidence=.99))
    result = service.decide_intervention(child, session, stamp)
    if result['status'] == 'suggested':
        service.record_feedback(result['intervention_id'], accepted,
            (instant+timedelta(seconds=1)).astimezone(timezone.utc).isoformat(),
            child_id=child, session_id=session)
    return result


@pytest.mark.parametrize('prior,now,expected', [
    (['2026-09-09T22:00:00Z', '2026-09-09T22:30:00Z', '2026-09-09T23:00:00Z'],
     '2026-09-10T00:05:00Z', 'capped'),
    (['2026-09-09T12:00:00Z', '2026-09-09T13:00:00Z', '2026-09-09T14:00:00Z'],
     '2026-09-09T16:30:00Z', 'new_day'),
])
def test_real_decisions_use_nanjing_day_not_utc_prefix(managed, prior, now, expected):
    database, service = managed[:2]
    for n, stamp in enumerate(prior):
        result = decide(service, stamp, 'prior-' + str(n))
        assert result['status'] == 'suggested', result
    with database.connect() as c:
        old = [tuple(r) for r in c.execute('SELECT intervention_id,decided_at,feedback FROM intervention ORDER BY rowid')]
    result = decide(BehaviorModelService(database), now, 'current')
    if expected == 'capped':
        assert len({family_today(datetime.fromisoformat(t.replace('Z', '+00:00'))) for t in prior+[now]}) == 1
        assert result == {'status': 'suppressed', 'reason': 'daily_frequency_cap'}, result
    else:
        assert family_today(datetime.fromisoformat(prior[-1].replace('Z', '+00:00'))) != family_today(datetime.fromisoformat(now.replace('Z', '+00:00')))
        assert result['status'] == 'suggested', result
    with database.connect() as c:
        rows = [tuple(r) for r in c.execute('SELECT intervention_id,decided_at,feedback FROM intervention ORDER BY rowid')]
    assert rows[:3] == old
    assert len(rows) == (3 if expected == 'capped' else 4)


@pytest.mark.parametrize('prior,now,expected', [
    (['2026-09-09T22:00:00Z', '2026-09-09T23:00:00Z'], '2026-09-10T00:05:00Z', 'capped'),
    (['2026-09-09T12:00:00Z', '2026-09-09T13:00:00Z'], '2026-09-09T16:30:00Z', 'new_day'),
])
def test_personalized_cap_keeps_real_decline_policy_but_uses_same_family_day(managed, prior, now, expected):
    database, service = managed[:2]
    for n, stamp in enumerate(['2026-09-06T01:00:00Z', '2026-09-06T02:00:00Z',
                               '2026-09-07T01:00:00Z', '2026-09-07T02:00:00Z']):
        assert decide(service, stamp, 'decline-' + str(n), accepted=False)['status'] == 'suggested'
    for n, stamp in enumerate(prior):
        result = decide(service, stamp, 'recent-' + str(n), accepted=False)
        assert result['status'] == 'suggested' and result['strategy'] == 'low_frequency_ask_first'
    result = decide(BehaviorModelService(database), now, 'now-decline', accepted=False)
    if expected == 'capped':
        assert result['status'] == 'suppressed' and result['reason'] == 'personalized_frequency_cap', result
    else:
        assert result['status'] == 'suggested' and result['strategy'] == 'low_frequency_ask_first', result


@pytest.mark.parametrize('first,second,third,now,expected', [
    ('2026-09-09T16:00:00Z', '2026-09-10T00:30:00+08:00', '2026-09-09T17:00:00+00:00',
     '2026-09-10T08:00:00+08:00', 'daily_frequency_cap'),
    ('2026-09-09T14:00:00Z', '2026-09-09T14:30:00Z', '2026-09-09T15:00:00Z',
     '2026-09-09T16:00:00Z', 'suggested'),
    ('2026-09-09T14:00:00Z', '2026-09-09T14:30:00Z', '2026-09-09T15:59:59.999999Z',
     '2026-09-10T00:30:00+08:00', 'suggested'),
])
def test_day_is_half_open_and_timestamp_offsets_do_not_change_it(managed, first, second, third, now, expected):
    database, service = managed[:2]
    for n, stamp in enumerate((first, second, third)):
        result = decide(service, stamp, 'edge-' + str(n))
        assert result['status'] == 'suggested', result
    result = decide(BehaviorModelService(database), now, 'edge-current')
    assert result.get('reason', result['status']) == expected, result
    # Re-reading the same decision time/source cannot create another reminder.
    with database.connect() as c:
        original = [tuple(r) for r in c.execute('SELECT * FROM intervention ORDER BY rowid')]
    again = BehaviorModelService(database).decide_intervention(CHILD, SESSION, now)
    assert again['status'] == 'suppressed'
    with database.connect() as c:
        assert [tuple(r) for r in c.execute('SELECT * FROM intervention ORDER BY rowid')] == original


def test_new_family_day_does_not_reset_actual_cooldown(managed):
    database, service = managed[:2]
    assert decide(service, '2026-09-09T15:55:00Z', 'cooldown-before-midnight')['status'] == 'suggested'
    result = decide(BehaviorModelService(database), '2026-09-09T16:00:00Z', 'cooldown-after-midnight')
    assert result == {'status': 'suppressed', 'reason': 'cooldown'}
    with database.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM intervention').fetchone()[0] == 1


def test_same_family_day_budget_never_borrows_other_child_history(managed):
    database, service = managed[:2]
    for n, stamp in enumerate(['2026-09-09T22:00:00Z', '2026-09-09T22:30:00Z', '2026-09-09T23:00:00Z']):
        assert decide(service, stamp, 'primary-' + str(n))['status'] == 'suggested'
    other = 'child-junior-3'
    assert service.decide_intervention(other, SESSION, '2026-09-10T00:05:00Z') == {
        'status': 'no_action', 'reason': 'session_scope_mismatch'}
    with database.connect() as c:
        # A different real session is required for the second child. Existing
        # intervention timestamps, sources, feedback and policy are untouched.
        c.execute("UPDATE daily_session SET status='completed' WHERE session_id=?", (SESSION,))
        c.execute("""INSERT INTO daily_session(session_id,child_id,status,state,started_at,
            last_checkpoint_at,client_action_id) VALUES('other-session',?,'active','study',?,?,'other-session')""",
            (other, '2026-09-10T00:00:00Z', '2026-09-10T00:00:00Z'))
        c.commit()
    result = decide(BehaviorModelService(database), '2026-09-10T00:05:00Z', 'junior-current',
        child=other, session='other-session')
    assert result['status'] == 'suggested'
    with database.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM intervention WHERE child_id=?', (CHILD,)).fetchone()[0] == 3
        assert c.execute('SELECT COUNT(*) FROM intervention WHERE child_id=?', (other,)).fetchone()[0] == 1
