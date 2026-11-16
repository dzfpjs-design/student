"""Stopping a habit must not race a report that still recommends it."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta,timezone
import time

from test_behavior_goal_report_guard import _report
from test_behavior_goal_confirmation import CHILD,history
from test_behavior_goal_http import host
from test_behavior_observation_coverage import managed


def _active_goal(managed):
    db,service=history(managed)
    now=datetime.now(timezone.utc)
    with db.connect() as c:
        c.execute("UPDATE habit_experiment SET status='active',started_at=?,ends_at=?,closed_at=NULL WHERE experiment_id='goal'",
                  ((now-timedelta(days=1)).isoformat(),(now+timedelta(days=6)).isoformat()))
        c.commit()
    return db,service


def test_parent_stop_waits_for_sender_but_child_and_replay_do_not(managed,tmp_path):
    db,_=_active_goal(managed)
    with host(db,tmp_path) as client:
        parent=client.post('/v1/parent/setup',json={'pin':'8362'});client.headers['X-HSC-Parent']=parent.json()['parent_token']
        repo,source_current=_report(db)
        assert source_current()
        with db.connect() as c:
            assert c.execute("SELECT status FROM habit_experiment WHERE experiment_id='goal'").fetchone()[0]=='active'
        child_session=repo.start_session(CHILD,'stop-next-session')['session_id']
        repo.transition(child_session,'check_in','classroom_recall','继续学习','stop-next-checkin')
        url=f'/v1/children/{CHILD}/behavior/habit-experiments/goal/stop'
        command={'client_action_id':'stop-habit-guard'}
        with ThreadPoolExecutor(max_workers=2) as pool:
            with db.external_effect_guard():
                busy=client.post(url,json=command)
                assert busy.status_code==503 and busy.json()['detail']['code']=='BEHAVIOR_GOAL_BUSY',busy.text
                assert source_current()
                pending=pool.submit(client.post,url,json=command)
                time.sleep(.15)
                assert not pending.done() and source_current()
                child=pool.submit(client.post,f'/v1/sessions/{child_session}/recalls',json={
                    'subject':'数学','summary':'继续记录。','client_action_id':'child-during-stop'}).result(timeout=1.2)
                assert child.status_code==201,child.text
                with db.connect() as c:
                    assert c.execute("SELECT status FROM habit_experiment WHERE experiment_id='goal'").fetchone()[0]=='active'
                    assert not c.execute("SELECT 1 FROM habit_experiment_event WHERE client_action_id='stop-habit-guard'").fetchone()
            stopped=pending.result(timeout=8)
        assert stopped.status_code==200 and stopped.json()['status']=='stopped',stopped.text
        assert not source_current()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with db.external_effect_guard():
                replay=pool.submit(client.post,url,json=command).result(timeout=3)
                assert replay.status_code==200 and replay.json()==stopped.json()
        with db.connect() as c:
            assert c.execute("SELECT COUNT(*) FROM habit_experiment_event WHERE client_action_id='stop-habit-guard'").fetchone()[0]==1


def test_failed_stop_rolls_back_and_retries_once_after_restart(managed,tmp_path):
    db,_=_active_goal(managed)
    url=f'/v1/children/{CHILD}/behavior/habit-experiments/goal/stop'
    command={'client_action_id':'stop-habit-retry'}
    with host(db,tmp_path) as client:
        parent=client.post('/v1/parent/setup',json={'pin':'8362'});client.headers['X-HSC-Parent']=parent.json()['parent_token']
        with db.connect() as c:
            c.execute("CREATE TRIGGER synthetic_stop_failure BEFORE INSERT ON habit_experiment_event WHEN NEW.client_action_id='stop-habit-retry' BEGIN SELECT RAISE(ABORT,'synthetic'); END");c.commit()
        assert client.post(url,json=command).status_code==500
        with db.connect() as c:
            assert c.execute("SELECT status FROM habit_experiment WHERE experiment_id='goal'").fetchone()[0]=='active'
            assert not c.execute("SELECT 1 FROM habit_experiment_event WHERE client_action_id='stop-habit-retry'").fetchone()
            c.execute('DROP TRIGGER synthetic_stop_failure');c.commit()
        accepted=client.post(url,json=command)
        assert accepted.status_code==200 and accepted.json()['status']=='stopped',accepted.text
    with host(db,tmp_path) as client:
        parent=client.post('/v1/parent/unlock',json={'pin':'8362'});client.headers['X-HSC-Parent']=parent.json()['parent_token']
        replay=client.post(url,json=command)
        assert replay.status_code==200 and replay.json()==accepted.json()
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM habit_experiment_event WHERE client_action_id='stop-habit-retry'").fetchone()[0]==1
