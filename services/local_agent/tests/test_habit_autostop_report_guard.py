"""Automatic stop is durable, silent and does not race a frozen report."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import pytest
from test_behavior_goal_report_guard import _report
from test_behavior_goal_confirmation import CHILD
from test_behavior_goal_http import host
from test_behavior_observation_coverage import managed
from test_habit_stop_report_guard import _active_goal


def _make_burden(db):
    now = datetime.now(timezone.utc).isoformat()
    with db.connect() as c:
        for n in range(2):
            c.execute('''INSERT INTO intervention(intervention_id,child_id,session_id,decided_at,candidate_state,message,feedback,status)
                         VALUES(?,?,NULL,?,'blocked_candidate','synthetic','declined','active')''', (f'burden-{n}', CHILD, now))
        c.commit()


def _login(client):
    response=client.post('/v1/parent/setup', json={'pin': '8362'})
    if response.status_code != 200:
        response=client.post('/v1/parent/unlock', json={'pin': '8362'})
    client.headers['X-HSC-Parent'] = response.json()['parent_token']


def test_auto_stop_queues_while_report_sends_and_recovers_on_restart(managed, tmp_path):
    db, svc = _active_goal(managed)
    with host(db, tmp_path) as client:
        _login(client)
        repo, source_current = _report(db)
        assert source_current()
        _make_burden(db)
        assert source_current()
        session=repo.start_session(CHILD,'after-freeze')['session_id']
        repo.transition(session,'check_in','classroom_recall','继续学习','after-freeze-check')
        with ThreadPoolExecutor(max_workers=2) as pool:
            with db.external_effect_guard():
                response=pool.submit(client.get,f'/v1/children/{CHILD}/behavior/habit-experiments').result(timeout=3)
                assert response.status_code == 200
                item=next(i for i in response.json()['items'] if i['experiment_id']=='goal')
                assert item['status']=='active' and item['stop_pending'] is True
                assert not source_current()  # new pending event invalidates later sends
                with db.connect() as c:
                    assert c.execute("SELECT status FROM habit_experiment WHERE experiment_id='goal'").fetchone()[0]=='active'
                    assert c.execute("SELECT COUNT(*) FROM habit_experiment_event WHERE experiment_id='goal' AND event_type='auto_stop_pending'").fetchone()[0]==1
                    from home_study_agent.behavior_model.habit_formation import current_prompt_policy
                    assert current_prompt_policy(c,CHILD,'blocked_candidate')['suppress_voice']
                child=pool.submit(client.post,f'/v1/sessions/{session}/recalls',json={
                    'subject':'数学','summary':'继续记录。','client_action_id':'child-while-stop-pending'}).result(timeout=2)
                assert child.status_code==201,child.text
                future=(datetime.now(timezone.utc)+timedelta(days=8)).isoformat()
                pending=pool.submit(svc.habit_experiments,CHILD,future).result(timeout=3)['items'][0]
                assert pending['status']=='active' and pending['stop_pending']
                assert pool.submit(svc.process_pending_habit_stops).result(timeout=4)['waiting']==1
        from home_study_agent.behavior_model.service import BehaviorModelService
        recovered_service=BehaviorModelService(db)
        assert recovered_service.process_pending_habit_stops()['completed']==1
        assert recovered_service.process_pending_habit_stops()['completed']==0
    with host(db, tmp_path) as client:
        _login(client)
        item=next(i for i in client.get(f'/v1/children/{CHILD}/behavior/habit-experiments').json()['items'] if i['experiment_id']=='goal')
        assert item['status']=='stopped' and not item['stop_pending']


@pytest.mark.parametrize('failure_trigger', [
    "CREATE TRIGGER synthetic_auto_stop_failure BEFORE INSERT ON habit_formation_event WHEN NEW.to_stage='stopped' BEGIN SELECT RAISE(ABORT,'synthetic'); END",
    "CREATE TRIGGER synthetic_auto_stop_failure BEFORE UPDATE ON bounded_experiment_protocol WHEN NEW.status='rolled_back' BEGIN SELECT RAISE(ABORT,'synthetic'); END",
])
def test_auto_stop_failure_stays_silent_and_retries(managed, tmp_path, failure_trigger):
    db, svc = _active_goal(managed)
    with host(db, tmp_path) as client:
        _login(client)
        _make_burden(db)
        response=client.get(f'/v1/children/{CHILD}/behavior/habit-experiments')
        assert response.status_code==200
        with db.connect() as c:
            c.execute(failure_trigger)
            c.commit()
        assert svc.process_pending_habit_stops()['failed']==1
        item=next(i for i in client.get(f'/v1/children/{CHILD}/behavior/habit-experiments').json()['items'] if i['experiment_id']=='goal')
        assert item['status']=='active' and item['stop_pending'] and item['stop_retrying']
        with db.connect() as c:
            c.execute('DROP TRIGGER synthetic_auto_stop_failure');c.commit()
        assert svc.process_pending_habit_stops()['completed']==1
        assert svc.process_pending_habit_stops()['completed']==0


def test_borrowed_observation_commits_pending_without_wait_or_duplicate(managed):
    db, svc = _active_goal(managed)
    _make_burden(db)
    from home_study_agent.behavior_model.service import ObservableEvent
    event=ObservableEvent(child_id=CHILD,session_id='session',event_id='synthetic-observation-during-send',
        occurred_at=datetime.now(timezone.utc).isoformat(),event_type='work.writing',
        source='synthetic',confidence=.9)
    def save():
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            svc.ingest(event,connection=c)
            c.commit()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with db.external_effect_guard():
            pool.submit(save).result(timeout=3)
            pool.submit(save).result(timeout=3)
            with db.connect() as c:
                assert c.execute("SELECT status FROM habit_experiment WHERE experiment_id='goal'").fetchone()[0]=='active'
                assert c.execute("SELECT COUNT(*) FROM observable_event WHERE event_id=?",(event.event_id,)).fetchone()[0]==1
                assert c.execute("SELECT COUNT(*) FROM habit_experiment_event WHERE experiment_id='goal' AND event_type='auto_stop_pending'").fetchone()[0]==1
    assert svc.process_pending_habit_stops()['completed']==1


def test_pending_queue_failure_rolls_back_observation_and_retry(managed):
    import sqlite3
    from home_study_agent.behavior_model.service import ObservableEvent
    db, svc = _active_goal(managed)
    _make_burden(db)
    event=ObservableEvent(child_id=CHILD,session_id='session',event_id='synthetic-retry-observation',
        occurred_at=datetime.now(timezone.utc).isoformat(),event_type='work.writing',source='synthetic',confidence=.9)
    with db.connect() as c:
        c.execute("CREATE TRIGGER synthetic_queue_failure BEFORE INSERT ON habit_experiment_event WHEN NEW.event_type='auto_stop_pending' BEGIN SELECT RAISE(ABORT,'synthetic'); END")
        c.commit()
    with pytest.raises(sqlite3.IntegrityError):
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            svc.ingest(event,connection=c)
            c.commit()
    with db.connect() as c:
        assert not c.execute('SELECT 1 FROM observable_event WHERE event_id=?',(event.event_id,)).fetchone()
        assert not c.execute("SELECT 1 FROM habit_experiment_event WHERE event_type='auto_stop_pending'").fetchone()
        c.execute('DROP TRIGGER synthetic_queue_failure');c.commit()
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        svc.ingest(event,connection=c)
        c.commit()
        assert c.execute("SELECT COUNT(*) FROM habit_experiment_event WHERE event_type='auto_stop_pending'").fetchone()[0]==1
