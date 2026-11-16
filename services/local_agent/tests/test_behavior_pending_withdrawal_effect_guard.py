"""A stale child prompt is suppressed without racing an active mail owner."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from test_behavior_accepted_habit_support import active, offer, CHILD, NOW
from test_behavior_observation_coverage import managed


@pytest.mark.parametrize('entry', ['pending', 'decision', 'borrowed_decision'])
def test_stale_prompt_does_not_write_during_effect_guard_and_retries(managed, entry):
    db, service = active(managed)
    result = offer(service)
    assert result['strategy']=='contextual_habit_ask_first'
    with db.connect() as c:
        c.execute("UPDATE homework_item SET confirmation_status='pending'")
        c.commit()
    def invoke():
        if entry == 'pending':
            return service.pending_feedback(CHILD,'session',NOW)
        if entry == 'decision':
            return service.decide_intervention(CHILD,'session',NOW)
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            answer=service.decide_intervention(CHILD,'session',NOW,connection=c)
            c.commit()
            return answer
    with ThreadPoolExecutor(max_workers=1) as pool:
        with db.external_effect_guard(timeout=2):
            answer=pool.submit(invoke).result(timeout=3)
            assert answer == ({'status':'no_pending'} if entry=='pending' else
                {'status':'suppressed','reason':'habit_support_withdrawal_pending'})
            with db.connect() as c:
                assert c.execute('SELECT withdrawal_reason FROM intervention WHERE intervention_id=?',
                                 (result['intervention_id'],)).fetchone()[0] is None
                c.execute('BEGIN IMMEDIATE'); c.rollback()
        # Retry under the same user state persists a withdrawal for maintenance.
        assert service.pending_feedback(CHILD,'session',NOW)=={'status':'no_pending'}
        with db.connect() as c:
            assert c.execute('SELECT withdrawal_reason FROM intervention WHERE intervention_id=?',
                             (result['intervention_id'],)).fetchone()[0]=='habit_support_source_changed_pending'
        assert service.process_pending_intervention_withdrawals()['completed']==1


def test_maintenance_recovers_suppressed_unmarked_prompt_after_restart(managed):
    from home_study_agent.behavior_model import BehaviorModelService
    db, service = active(managed)
    result=offer(service)
    with db.connect() as c:
        c.execute("UPDATE homework_item SET confirmation_status='pending'")
        c.commit()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with db.external_effect_guard(timeout=2):
            assert pool.submit(service.pending_feedback,CHILD,'session',NOW).result(timeout=3)=={'status':'no_pending'}
    # No second child request: a newly instantiated background service finds it.
    assert BehaviorModelService(db).process_pending_intervention_withdrawals()['completed']==1
    with db.connect() as c:
        assert tuple(c.execute('SELECT status,withdrawal_reason FROM intervention WHERE intervention_id=?',
                               (result['intervention_id'],)).fetchone()) == ('withdrawn','habit_support_source_changed')


def test_parent_habit_list_refresh_waits_before_updating_existing_source(managed):
    from threading import Event
    from home_study_agent.daily_behavior_source import daily_behavior_fingerprint
    db, service=active(managed)
    with db.connect() as c:
        c.execute("UPDATE habit_experiment SET ends_at='2026-08-28T00:00:00+00:00' WHERE experiment_id='goal'")
        c.commit()
        before=daily_behavior_fingerprint(c,CHILD,'session')
    started,done=Event(),Event()
    def refresh():
        started.set()
        try: return service.habit_experiments(CHILD,NOW)
        finally: done.set()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with db.external_effect_guard(timeout=2):
            pending=pool.submit(refresh)
            assert started.wait(3) and not done.wait(.2)
            with db.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                assert daily_behavior_fingerprint(c,CHILD,'session')==before
                c.rollback()
        assert pending.result(timeout=10)['items']
    with db.connect() as c:
        assert c.execute("SELECT status FROM habit_experiment WHERE experiment_id='goal'").fetchone()[0]=='completed'
        assert daily_behavior_fingerprint(c,CHILD,'session')!=before
