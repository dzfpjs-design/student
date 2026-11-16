"""Actual local HTTP; synthetic observations, no camera or cloud model."""
from datetime import datetime, timedelta, timezone

from home_study_agent.behavior_model import BehaviorModelService, ObservableEvent
from test_behavior_goal_http import host
from test_behavior_goal_confirmation import CHILD
from test_behavior_observation_coverage import managed


def signal(service, session, key, stamp=None, child=CHILD):
    service.ingest(ObservableEvent(child_id=child, session_id=session,
        occurred_at=stamp or datetime.now(timezone.utc).isoformat(), event_id=key,
        event_type='work.stall', source='local_vision', confidence=1,
        payload={'shadow': False}))


def test_actual_http_never_borrows_previous_session_and_preserves_original(managed, tmp_path):
    db, service = managed[:2]
    with db.connect() as c:
        c.execute("""INSERT INTO daily_session(session_id,child_id,status,state,started_at,
            last_checkpoint_at,client_action_id) VALUES('previous',?,'completed','idle',?,?,'previous')""",
            (CHILD, datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat()))
        c.commit()
    path = f'/v1/children/{CHILD}/behavior/interventions/decide?session_id=session'
    with host(db, tmp_path) as client:
        assert client.post(path).status_code == 401
        client.headers['X-HSC-Parent'] = client.post('/v1/parent/setup', json={'pin':'8362'}).json()['parent_token']
        signal(service, 'previous', 'previous-stall')
        response = client.post(path)
        assert response.status_code == 200, response.text
        assert response.json()['status'] == 'no_action', response.text
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM intervention').fetchone()[0] == 0
            assert c.execute("SELECT COUNT(*) FROM probabilistic_state WHERE event_id='previous-stall'").fetchone()[0] == 1
        signal(service, 'session', 'current-stall')
        result = client.post(path)
        assert result.status_code == 200, result.text
        assert result.json()['status'] == 'suggested', result.text
        receipt = result.json()['intervention_id']
    with host(db, tmp_path) as client:
        client.headers['X-HSC-Parent'] = client.post('/v1/parent/unlock', json={'pin':'8362'}).json()['parent_token']
        assert client.post(path).json()['reason'] == 'awaiting_child_feedback'
        with db.connect() as c:
            rows = c.execute('SELECT intervention_id,session_id,source_event_id FROM intervention').fetchall()
        assert [tuple(row) for row in rows] == [(receipt, 'session', 'current-stall')]


def test_decide_rejects_wrong_child_session_future_and_unbound_sources(managed):
    db, service = managed[:2]
    now = datetime.now(timezone.utc)
    signal(service, 'session', 'other-child', child='child-junior-3')
    result = service.decide_intervention('child-junior-3', 'session', now.isoformat())
    assert result['status'] == 'no_action'
    signal(service, None, 'unbound')
    signal(service, 'session', 'future', stamp=(now + timedelta(minutes=3)).isoformat())
    result = BehaviorModelService(db).decide_intervention(CHILD, 'session', now.isoformat())
    assert result['status'] == 'no_action'
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM intervention').fetchone()[0] == 0


def test_switching_candidate_cannot_aggregate_another_session(managed):
    db, service = managed[:2]
    now = datetime.now(timezone.utc)
    for n, session in enumerate(('previous', 'previous', 'session')):
        service.ingest(ObservableEvent(child_id=CHILD, session_id=session,
            occurred_at=(now-timedelta(seconds=3-n)).isoformat(), event_id='switch-'+str(n),
            event_type='interaction.switch', source='app', confidence=1))
    result = service.decide_intervention(CHILD, 'session', now.isoformat())
    assert result['status'] == 'no_action', result
