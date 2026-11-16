"""Actual parent confirmation -> actual decide -> restart/retract. No cloud/camera."""
from datetime import datetime, timedelta, timezone
import json

from test_behavior_goal_confirmation import CHILD, NOW, history
from test_behavior_goal_http import ROOT, host
from test_behavior_intervention_scope_http import signal
from test_behavior_observation_coverage import managed


def current_history(managed):
    db, service = history(managed)
    delta = datetime.now(timezone.utc) - datetime.fromisoformat(NOW)
    shift = lambda stamp: (datetime.fromisoformat(stamp)+delta).isoformat() if stamp else stamp
    # Move only synthetic, still-unreviewed source history onto the host clock.
    # No qualification, confirmation or intervention receipt exists yet.
    with db.connect() as c:
        for table, identity, fields in (
            ('observable_event','event_id',('occurred_at',)),
            ('habit_experiment','experiment_id',('proposed_at','started_at','ends_at','closed_at')),
            ('habit_experiment_event','event_id',('occurred_at',)),
            ('homework_item','homework_id',('created_at',)),
            ('daily_plan','plan_id',('created_at',)),
        ):
            for row in c.execute('SELECT * FROM '+table).fetchall():
                c.execute('UPDATE '+table+' SET '+','.join(field+'=?' for field in fields)+' WHERE '+identity+'=?',
                    (*[shift(row[field]) for field in fields],row[identity]))
        for row in c.execute("SELECT event_id,payload_json FROM observable_event WHERE event_type='observation.sample'").fetchall():
            payload = json.loads(row['payload_json'])
            for key in ('captured_at','received_at'):
                payload[key] = shift(payload[key])
            c.execute('UPDATE observable_event SET payload_json=? WHERE event_id=?', (json.dumps(payload),row['event_id']))
        c.execute("UPDATE plan_item SET status='pending',task_type='written',cognitive_load='medium',source_type='school_homework' WHERE plan_item_id='task'")
        c.commit()
    return db, service


def test_actual_parent_confirmations_change_decide_and_retract_after_host_restart(managed, tmp_path):
    db, service = current_history(managed)
    path = f'/v1/children/{CHILD}/behavior/interventions/decide?session_id=session'
    with host(db, tmp_path) as client:
        client.headers['X-HSC-Parent'] = client.post('/v1/parent/setup',json={'pin':'8362'}).json()['parent_token']
        view = client.get(ROOT+'/opportunities')
        assert view.status_code == 200, view.text
        for item in view.json()['opportunities']:
            body = dict(opportunity_id=item['opportunity_id'],expected_version=item['binding_version'],
                opportunity_present=True,criteria={key:item['phase']=='current' for key in item['criteria']},
                initiated_by='child',assistance_level='none',client_action_id='confirm-'+item['opportunity_id'])
            recorded = client.post(ROOT+'/confirmation',json=body)
            assert recorded.status_code == 200, recorded.text
        started = client.post('/v1/plan-items/task/start',json={'client_action_id':'policy-start'})
        assert started.status_code == 200, started.text
        signal(service,'session','policy-source')
        decision = client.post(path)
        assert decision.status_code == 200, decision.text
        assert decision.json()['strategy'] == 'autonomy_faded_ask_first', decision.text
        receipt = decision.json()
    with host(db,tmp_path) as client:
        client.headers['X-HSC-Parent'] = client.post('/v1/parent/unlock',json={'pin':'8362'}).json()['parent_token']
        assert client.post(path).json()['reason'] == 'awaiting_child_feedback'
        for item in client.get(ROOT+'/opportunities').json()['opportunities']:
            if item['phase']=='current':
                response = client.post(ROOT+'/confirmation', json=dict(
                    opportunity_id=item['opportunity_id'],expected_version=item['binding_version'],
                    opportunity_present=None,criteria={key:None for key in item['criteria']},
                    initiated_by='unknown',assistance_level='unknown',operation='retract',
                    client_action_id='withdraw-'+item['opportunity_id']))
                assert response.status_code == 200, response.text
        # No automatic resend or changed historical reason after withdrawal.
        assert client.post(path).json()['reason'] == 'awaiting_child_feedback'
        with db.connect() as c:
            row = c.execute('SELECT support_basis_json FROM intervention WHERE intervention_id=?',
                (receipt['intervention_id'],)).fetchone()
            assert json.loads(row[0]) == receipt['support_basis']
            assert c.execute('SELECT COUNT(*) FROM intervention').fetchone()[0] == 1
            assert c.execute('SELECT COUNT(*) FROM model_call').fetchone()[0] == 0


def test_actual_accepted_action_observes_baseline_silently_and_stop_never_replays_it(managed,tmp_path):
    db, _ = current_history(managed)
    with host(db,tmp_path) as client:
        client.headers['X-HSC-Parent'] = client.post('/v1/parent/setup',json={'pin':'8362'}).json()['parent_token']
        service = client.app.state.behavior_model
        service.habit_experiments(CHILD)
        now = datetime.now(timezone.utc)
        for index in range(3):
            signal(service,'session','propose-'+str(index),stamp=(now-timedelta(days=2-index)).isoformat())
        proposal = service.pending_habit_experiment(CHILD)['experiment']
        respond = f"/v1/sessions/session/behavior/habit-experiments/{proposal['experiment_id']}/respond"
        body={'accepted':True,'client_action_id':'accept-live-goal'}
        accepted = client.post(respond,json=body)
        assert accepted.status_code==200, accepted.text
        assert accepted.json()['status']=='active'
        started=client.post('/v1/plan-items/task/start',json={'client_action_id':'optional-start'})
        assert started.status_code==200, started.text
        signal(service,'session','optional-source')
        path=f'/v1/children/{CHILD}/behavior/interventions/decide?session_id=session'
        result=client.post(path)
        assert result.status_code==200, result.text
        decision=result.json()
        assert decision['status']=='suppressed', result.text
        assert decision['reason']=='habit_baseline_observation_no_prompt', result.text
        assert decision['formation']['stage']=='baseline'
        assert decision['formation']['cue_level']=='L0'
        pending=client.get('/v1/sessions/session/behavior/interventions/pending')
        assert pending.status_code==200, pending.text
        assert pending.json()['status']=='no_pending'
        stopped=client.post(f"/v1/children/{CHILD}/behavior/habit-experiments/{proposal['experiment_id']}/stop",
            json={'client_action_id':'stop-live-goal'})
        assert stopped.status_code==200 and stopped.json()['status']=='stopped', stopped.text
        assert client.get('/v1/sessions/session/behavior/interventions/pending').json()['status']=='no_pending'
    with host(db,tmp_path) as client:
        client.headers['X-HSC-Parent']=client.post('/v1/parent/unlock',json={'pin':'8362'}).json()['parent_token']
        assert client.get('/v1/sessions/session/behavior/interventions/pending').json()['status']=='no_pending'
        replay=client.post(respond,json=body)
        assert replay.status_code==200 and replay.json()['status']=='stopped', replay.text
        with db.connect() as c:
            assert c.execute("SELECT COUNT(*) FROM intervention WHERE intervention_type='contextual_habit_ask_first'").fetchone()[0]==0
            assert c.execute("SELECT COUNT(*) FROM habit_experiment_event WHERE client_action_id='accept-live-goal'").fetchone()[0]==1
            assert c.execute("SELECT COUNT(*) FROM habit_formation_event WHERE experiment_id=? AND to_stage='baseline'",
                (proposal['experiment_id'],)).fetchone()[0]>=1
            assert c.execute("SELECT COUNT(*) FROM habit_formation_event WHERE experiment_id=? AND to_stage='stopped'",
                (proposal['experiment_id'],)).fetchone()[0]==1
            assert c.execute("SELECT COUNT(*) FROM habit_experiment_event WHERE event_type='goal_behavior_confirmed'").fetchone()[0]==0
            assert c.execute('SELECT COUNT(*) FROM model_call').fetchone()[0]==0


def test_parent_can_audit_habit_formation_without_cross_child_or_cloud_access(managed, tmp_path):
    db, service = current_history(managed)
    service.habit_experiments(CHILD)
    path = f'/v1/children/{CHILD}/behavior/habit-experiments/goal/history'
    with host(db, tmp_path) as client:
        assert client.get(path).status_code == 401
        token = client.post('/v1/parent/setup', json={'pin': '8362'}).json()['parent_token']
        headers = {'X-HSC-Parent': token}
        history_response = client.get(path, headers=headers)
        assert history_response.status_code == 200, history_response.text
        payload = history_response.json()
        assert payload['child_id'] == CHILD
        assert payload['experiment_id'] == 'goal'
        assert payload['state']['habit_success'] is False
        assert payload['events']
        assert payload['local_only'] is True
        assert payload['cloud_transmission_allowed'] is False
        assert client.get(
            path.replace(CHILD, 'child-junior-3'), headers=headers
        ).status_code == 404
