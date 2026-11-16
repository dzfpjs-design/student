"""Real local FastAPI/SQLite; no cloud or household data."""
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.execution_trace import ExecutionTraceService
from home_study_agent.agent_capability_executor import AgentCapabilityExecutor
from test_agent_executor_evidence_boundary import setup, turn
from test_companion_activity_host import NoCloud
from test_cloud_first_agent import Planner


def settings(setup, tmp_path):
    return Settings(data_dir=tmp_path,database_path=setup[0].path,maintenance_enabled=False)


def parent(client, restart=False):
    token = client.post('/v1/parent/'+('unlock' if restart else 'setup'),json={'pin':'7391'}).json()['parent_token']
    return {'X-HSC-Parent':token}


def url(turn_id, child='child-primary-4'):
    return f'/v1/parent/children/{child}/execution-traces/{turn_id}'


def test_http_request_intent_execute_replay_and_restart_with_parent_only_read(setup,tmp_path,monkeypatch):
    db,repo,sid = setup
    observed_steps = []
    original_observe = ExecutionTraceService.observe_host_boundary
    def capture_result(self, **kwargs):
        if kwargs['event_type'] == 'action_result':
            observed_steps.append(dict(kwargs['connection'].execute(
                'SELECT * FROM agent_step WHERE step_id=?', (kwargs['step_id'],)).fetchone()))
        return original_observe(self, **kwargs)
    monkeypatch.setattr(ExecutionTraceService, 'observe_host_boundary', capture_result)
    config = settings(setup,tmp_path)
    body = {'transcript':'暂停当前学习，SYNTHETIC_PRIVATE','client_action_id':'voice-pause'}
    gateway = Planner('task.pause',{},body['transcript'])
    with TestClient(create_app(config,model_gateway=gateway)) as client:
        trace_id = str(uuid4())
        response = client.post(f'/v1/sessions/{sid}/agent-turn',json=body,headers={'X-HSC-Trace':trace_id})
        assert response.status_code == 200, response.text
        key = response.json()['agent_turn_id']
        headers = parent(client)
        assert client.get(url(key)).status_code == 401
        assert client.get(url(key)).headers['cache-control'] == 'no-store'
        assert client.get(url(key,'child-junior-3'),headers=headers).status_code == 404
        executed = client.post(f'/v1/agent-turns/{key}/execute')
        assert executed.status_code == 200, executed.text
        saved = client.get(url(key),headers=headers)
        assert saved.status_code == 200 and saved.headers['cache-control'] == 'no-store'
        state = saved.json()
        with db.connect() as c:
            final_step = dict(c.execute('SELECT * FROM agent_step WHERE step_id=?',
                (observed_steps[0]['step_id'],)).fetchone())
        assert {k for k in final_step if final_step[k] != observed_steps[0][k]} == {'updated_at'}
        assert next(s for s in state['spans'] if s['event_type']=='action_result')['source_current'] is True
        assert 'action_result' not in state['missing_stages']
        assert 'action_result' not in state['changed_stages']
        assert 'action_scheduled' not in state['missing_stages']
        assert 'action_scheduled' in state['changed_stages']
        assert {'request_received','intent_selected','action_scheduled','action_result'} <= {s['event_type'] for s in state['spans']}
        assert {s['trace_id'] for s in state['spans']} == {trace_id}
        assert 'SYNTHETIC_PRIVATE' not in json.dumps(state)
        assert not state['outcome_evidence_created']
        replay = client.post(f'/v1/sessions/{sid}/agent-turn',json=body,headers={'X-HSC-Trace':str(uuid4())})
        assert replay.status_code == 200 and replay.headers['X-HSC-Trace'] == trace_id
        assert client.post(f'/v1/agent-turns/{key}/execute').status_code == 200
        assert client.get(url(key),headers=headers).json()['history'] == state['history']
        assert next(s for s in client.get(url(key),headers=headers).json()['spans']
                    if s['event_type']=='action_result')['source_current'] is True
        assert len(gateway.requests) == 1
        assert trace_id not in str(gateway.requests[0])
        with db.connect() as c:
            assert c.execute("SELECT status FROM plan_item WHERE plan_item_id='task'").fetchone()[0] == 'paused'
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id='task' AND event_type='paused'").fetchone()[0] == 1
        client.app.state.parent_auth._tokens[headers['X-HSC-Parent']] = datetime.now(timezone.utc)-timedelta(seconds=1)
        expired = client.get(url(key),headers=headers)
        assert expired.status_code == 401 and expired.headers['cache-control']=='no-store'
    with TestClient(create_app(config,model_gateway=NoCloud())) as client:
        headers = parent(client,True)
        restarted = client.get(url(key),headers=headers).json()
        assert restarted['history'] == state['history']
        assert next(s for s in restarted['spans'] if s['event_type']=='action_result')['source_current'] is True


@pytest.mark.parametrize('failure',['after_insert','transaction_abort','logger_unavailable'])
def test_trace_insert_failure_cannot_erase_committed_business_or_redispatch(setup,tmp_path,monkeypatch,failure):
    db,repo,sid = setup
    with TestClient(create_app(settings(setup,tmp_path),model_gateway=NoCloud())) as client:
        key = turn(setup,'trace-fault','task.pause',{},transcript='暂停当前学习')
        original = ExecutionTraceService.record_link
        def fail_after_insert(self,**kwargs):
            original(self,**kwargs)
            raise RuntimeError('SYNTHETIC_PRIVATE')
        if failure in {'after_insert','logger_unavailable'}:
            monkeypatch.setattr(ExecutionTraceService,'record_link',fail_after_insert)
            if failure=='logger_unavailable':
                def broken_output(*args,**kwargs):
                    raise BrokenPipeError('synthetic closed log pipe')
                monkeypatch.setattr('builtins.print',broken_output)
        else:
            with db.connect() as c:
                c.execute("""CREATE TRIGGER synthetic_trace_abort BEFORE INSERT ON execution_trace_event
                    BEGIN SELECT RAISE(ROLLBACK,'synthetic trace failure'); END""")
                c.commit()
        first = client.post(f'/v1/agent-turns/{key}/execute')
        assert first.status_code == 200, first.text
        second = client.post(f'/v1/agent-turns/{key}/execute')
        assert second.status_code == 200 and second.json()['execution'] == first.json()['execution']
        with db.connect() as c:
            assert c.execute("SELECT status FROM plan_item WHERE plan_item_id='task'").fetchone()[0] == 'paused'
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id='task' AND event_type='paused'").fetchone()[0] == 1
            assert c.execute('SELECT COUNT(*) FROM execution_trace_event').fetchone()[0] == 0
            assert c.execute('SELECT status FROM agent_step WHERE plan_id IN (SELECT plan_id FROM agent_plan WHERE agent_turn_id=?)',(key,)).fetchone()[0] == 'completed'


@pytest.mark.parametrize('state',['cancelled','failed'])
def test_cancelled_or_failed_execution_is_not_reported_as_success(setup,tmp_path,monkeypatch,state):
    db,repo,sid = setup
    with TestClient(create_app(settings(setup,tmp_path),model_gateway=NoCloud()),raise_server_exceptions=False) as client:
        key = turn(setup,'bad-boundary','task.pause',{},transcript='暂停当前学习')
        if state == 'cancelled':
            with db.connect() as c:
                c.execute("UPDATE agent_step SET status='cancelled' WHERE plan_id IN (SELECT plan_id FROM agent_plan WHERE agent_turn_id=?)",(key,))
                c.commit()
        else:
            def fail(*args):
                raise ValueError('SYNTHETIC_PRIVATE')
            monkeypatch.setattr(AgentCapabilityExecutor,'_dispatch',fail)
        response = client.post(f'/v1/agent-turns/{key}/execute')
        assert response.status_code == (409 if state=='cancelled' else 500)
        result = client.get(url(key),headers=parent(client)).json()
        assert result['existing_steps'][0]['status'] == state
        assert not any(s['source_status']=='completed' for s in result['spans'])
        assert 'SYNTHETIC_PRIVATE' not in json.dumps(result)
        if state=='failed':
            assert any(s['event_type']=='action_result' and s['error_code']=='ValueError' for s in result['spans'])


def test_parent_trace_list_includes_legacy_turns_pages_without_duplicates_and_survives_restart(setup,tmp_path):
    db,repo,sid = setup
    config = settings(setup,tmp_path)
    root = '/v1/parent/children/child-primary-4/execution-traces'
    with TestClient(create_app(config,model_gateway=NoCloud())) as client:
        for key in ('page-a','page-b','page-c'):
            turn(setup,key,'task.pause',{},transcript='SYNTHETIC_PRIVATE')
        with db.connect() as c:
            # Multiple original steps must not multiply a turn in the list.
            step = dict(c.execute("SELECT * FROM agent_step WHERE plan_id IN (SELECT plan_id FROM agent_plan WHERE agent_turn_id='page-c')").fetchone())
            step.update(step_id='extra-step',step_index=1,idempotency_key='extra-step')
            c.execute('INSERT INTO agent_step('+','.join(step)+') VALUES ('+','.join('?' for _ in step)+')',tuple(step.values()))
            legacy = dict(c.execute("SELECT * FROM agent_turn WHERE agent_turn_id='page-a'").fetchone())
            legacy.update(agent_turn_id='page-0',client_action_id='legacy-no-step',created_at='2020-01-01T00:00:00+00:00')
            c.execute('INSERT INTO agent_turn('+','.join(legacy)+') VALUES ('+','.join('?' for _ in legacy)+')',tuple(legacy.values()))
            c.commit()
        assert client.get(root).status_code == 401
        assert client.get(root).headers['cache-control'] == 'no-store'
        headers = parent(client)
        before_dump = None
        with db.connect() as c:
            before_dump = '\n'.join(c.iterdump())
        first = client.get(root+'?limit=2',headers=headers)
        assert first.status_code==200 and first.headers['cache-control']=='no-store'
        data = first.json()
        assert len(data['items']) == 2 and data['next_cursor']
        assert {row['turn_id'] for row in data['items']} == {'page-c','page-b'}
        assert all(row['trace_event_count']==0 for row in data['items'])
        assert all(row['capability_name']=='task.pause' for row in data['items'])
        assert all(set(row)=={'turn_id','created_at','status','error_code','capability_name','trace_event_count'} for row in data['items'])
        assert 'SYNTHETIC_PRIVATE' not in json.dumps(data)
        second = client.get(root,params={'limit':2,'before':data['next_cursor']},headers=headers).json()
        assert [row['turn_id'] for row in second['items']] == ['page-a','page-0']
        assert second['items'][1]['capability_name'] is None and second['items'][1]['trace_event_count']==0
        assert second['next_cursor'] is None
        with db.connect() as c:
            assert '\n'.join(c.iterdump()) == before_dump
        other = root.replace('child-primary-4','child-junior-3')
        assert client.get(other,headers=headers).json() == {'items':[],'next_cursor':None}
        assert client.get(root.replace('child-primary-4','missing'),headers=headers).status_code == 404
        assert client.get(other,params={'before':data['next_cursor']},headers=headers).status_code == 404
        for params in ({'limit':0},{'limit':101},{'before':'not-a-cursor'}):
            bad = client.get(root,params=params,headers=headers)
            assert bad.status_code == 422 and bad.headers['cache-control']=='no-store'
    with TestClient(create_app(config,model_gateway=NoCloud())) as client:
        headers = parent(client,True)
        after = client.get(root,params={'limit':2,'before':data['next_cursor']},headers=headers).json()
        assert after == second
        with db.connect() as c:
            c.execute("DELETE FROM agent_turn WHERE agent_turn_id='page-b'")
            c.commit()
        expired = client.get(root,params={'before':data['next_cursor']},headers=headers)
        assert expired.status_code==422 and expired.headers['cache-control']=='no-store'


def test_parent_trace_list_filters_unknown_capability_error_and_counts_only_own_trace(setup,tmp_path):
    db,repo,sid = setup
    with TestClient(create_app(settings(setup,tmp_path),model_gateway=NoCloud())) as client:
        key = turn(setup,'list-observed','task.pause',{},transcript='SYNTHETIC_PRIVATE')
        assert client.post(f'/v1/agent-turns/{key}/execute').status_code==200
        with db.connect() as c:
            c.execute("UPDATE agent_step SET capability_name='SYNTHETIC_PRIVATE',error_code='SYNTHETIC_PRIVATE',status='unknown' WHERE plan_id IN (SELECT plan_id FROM agent_plan WHERE agent_turn_id=?)",(key,))
            c.commit()
        root = '/v1/parent/children/child-primary-4/execution-traces'
        result = client.get(root,headers=parent(client)).json()
        assert result['items'][0]['capability_name'] is None
        assert result['items'][0]['error_code'] is None
        assert result['items'][0]['status']=='unknown'
        assert result['items'][0]['trace_event_count']==2
        assert 'SYNTHETIC_PRIVATE' not in json.dumps(result)
