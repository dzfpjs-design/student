"""Real SQLite source writes cannot pass an in-flight disclosure guard."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import json

import pytest

from home_study_agent.behavior_model import ObservableEvent
from home_study_agent.daily_behavior_source import daily_behavior_fingerprint
from test_behavior_observation_coverage import managed, at


@pytest.mark.parametrize('kind', ['decline_habit', 'intervention_feedback'])
def test_source_change_waits_without_taking_sqlite_writer(managed, kind):
    db, service, _, _ = managed
    child, session = 'child-primary-4', 'session'
    if kind == 'decline_habit':
        with db.connect() as c:
            c.execute('''INSERT INTO habit_experiment(experiment_id,child_id,target_state,title,child_action,
                success_signal,duration_days,baseline_json,status,proposed_at)
                VALUES('goal',?,'blocked_candidate','synthetic','synthetic','synthetic',7,?,'proposed',?)''',
                (child,json.dumps({'feature':'stall_count'}),at(0)))
            c.commit()
        def change(): return service.respond_habit_experiment('goal',child,False,at(60),'habit-response')
    else:
        service.ingest(ObservableEvent(child_id=child,session_id=session,occurred_at=at(0),
            event_type='work.stall',source='local_vision',confidence=.95))
        decision=service.decide_intervention(child,session,at(60))
        assert decision['status']=='suggested'
        def change(): return service.record_feedback(decision['intervention_id'],False,at(120),
            client_action_id='feedback-action',child_id=child,session_id=session)
    with db.connect() as c:
        before=daily_behavior_fingerprint(c,child,session)
    started,finished=Event(),Event()
    def run():
        started.set()
        try: return change()
        finally: finished.set()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with db.external_effect_guard(timeout=2):
            writer=pool.submit(run)
            assert started.wait(5) and not finished.wait(.2)
            # A waiting writer did not take SQLite's write lock first.
            with db.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                assert daily_behavior_fingerprint(c,child,session)==before
                c.rollback()
        result=writer.result(timeout=10)
    assert result['status']=='declined' if kind=='decline_habit' else result['feedback']=='declined'
    with db.connect() as c:
        assert daily_behavior_fingerprint(c,child,session)!=before
    # A repeated response is idempotent after the original durable write.
    assert change()
