"""A child's feedback and stale-reminder withdrawal both survive their race."""
from datetime import datetime,timezone
from threading import Barrier,Event,Thread
import pytest

from test_behavior_goal_confirmation import CHILD,history
from test_behavior_goal_http import host
from test_behavior_observation_coverage import managed
from home_study_agent.behavior_model.service import BehaviorModelService
from home_study_agent.behavior_model.intervention_withdrawal import pending_for_state
from home_study_agent.database import StudyCursor


def seed(db,stamp):
    with db.connect() as c:
        c.execute("INSERT INTO intervention(intervention_id,child_id,session_id,decided_at,candidate_state,message,intervention_type,status,support_basis_json) VALUES('race-reminder',?,'session',?,'blocked_candidate','synthetic','contextual_habit_ask_first','active','{}')",(CHILD,stamp));c.commit()


def feedback(service,stamp,key='feedback-race'):
    return service.record_feedback('race-reminder',True,stamp,client_action_id=key,child_id=CHILD,session_id='session')


def state(db):
    with db.connect() as c:
        row=c.execute("SELECT status,feedback,withdrawal_reason FROM intervention WHERE intervention_id='race-reminder'").fetchone()
        return tuple(row),pending_for_state(c,CHILD,'blocked_candidate')


def test_feedback_read_before_pending_preserves_feedback_and_recovers(managed,tmp_path,monkeypatch):
    db,_=history(managed); stamp=datetime.now(timezone.utc).isoformat()
    with host(db,tmp_path) as client:
        seed(db,stamp)
        blocked=Event();release=Event();original=StudyCursor.execute
        def execute(cursor,*args,**kwargs):
            if str(args[0]).startswith('UPDATE intervention SET feedback ='):
                blocked.set(); assert release.wait(10)
            return original(cursor,*args,**kwargs)
        monkeypatch.setattr(StudyCursor,'execute',execute)
        results=[]
        def save():
            try: results.append(feedback(BehaviorModelService(db),stamp))
            except Exception as error: results.append(error)
        thread=Thread(target=save);thread.start()
        assert blocked.wait(5)
        assert client.get('/v1/sessions/session/behavior/interventions/pending').json()['status']=='no_pending'
        release.set();thread.join(5);assert not thread.is_alive()
        assert results[0]['feedback']=='accepted'
        assert state(db)==(('active','accepted','habit_support_source_changed_pending'),True)
        service=BehaviorModelService(db)
        assert feedback(service,stamp)['idempotent'] is True
        assert service.process_pending_intervention_withdrawals()['completed']==1
        assert state(db)==(('withdrawn','accepted','habit_support_source_changed'),False)
        assert feedback(BehaviorModelService(db),stamp)['idempotent'] is True


@pytest.mark.parametrize('order',['feedback_first','pending_first','withdrawal_first'])
def test_feedback_and_withdrawal_orders_remain_recoverable(managed,tmp_path,order):
    db,_=history(managed);stamp=datetime.now(timezone.utc).isoformat()
    with host(db,tmp_path) as client:
        seed(db,stamp);service=BehaviorModelService(db)
        url='/v1/sessions/session/behavior/interventions/pending'
        if order=='feedback_first':
            assert feedback(service,stamp)['feedback']=='accepted'
            assert client.get(url).json()['status']=='no_pending'
            assert state(db)==(('active','accepted','habit_support_source_changed_pending'),True)
            assert service.process_pending_intervention_withdrawals()['completed']==1
            assert state(db)==(('withdrawn','accepted','habit_support_source_changed'),False)
        else:
            assert client.get(url).json()['status']=='no_pending'
            assert state(db)==(('active',None,'habit_support_source_changed_pending'),True)
            if order=='withdrawal_first':
                assert service.process_pending_intervention_withdrawals()['completed']==1
            assert feedback(BehaviorModelService(db),stamp)['feedback']=='accepted'
            assert feedback(BehaviorModelService(db),stamp)['idempotent'] is True
            assert BehaviorModelService(db).process_pending_intervention_withdrawals()['completed']==(0 if order=='withdrawal_first' else 1)
            assert state(db)==(('withdrawn','accepted','habit_support_source_changed'),False)
        with pytest.raises(KeyError):
            service.record_feedback('race-reminder',True,stamp,client_action_id='other-child-action',child_id='other-child',session_id='session')


def test_concurrent_same_feedback_action_is_saved_once(managed,monkeypatch):
    db,_=history(managed);stamp=datetime.now(timezone.utc).isoformat();seed(db,stamp)
    blocked=Event();release=Event();original=StudyCursor.execute
    def execute(cursor,*args,**kwargs):
        if str(args[0]).startswith('UPDATE intervention SET feedback ='):
            blocked.set(); assert release.wait(5)
        return original(cursor,*args,**kwargs)
    monkeypatch.setattr(StudyCursor,'execute',execute)
    outcomes=[]
    def save():
        try: outcomes.append(feedback(BehaviorModelService(db),stamp))
        except Exception as error: outcomes.append(error)
    threads=[Thread(target=save),Thread(target=save)]
    threads[0].start();assert blocked.wait(5)
    threads[1].start();release.set()
    for thread in threads:thread.join(10)
    assert all(not thread.is_alive() for thread in threads)
    assert len(outcomes)==2 and all(isinstance(result,dict) for result in outcomes),outcomes
    assert sum(bool(result.get('idempotent')) for result in outcomes)==1
    assert state(db)==(('active','accepted','habit_support_source_changed_pending'),True)
