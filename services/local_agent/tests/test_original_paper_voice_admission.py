import json
import sqlite3
from uuid import uuid4
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor

import pytest

from home_study_agent.agent_runtime import AgentRuntimeStore, CapabilityRegistry, AgentPlanDecision, ObjectiveDraft, PlannedStep, InvalidAgentPlanError, context_manifest
from home_study_agent.database import Database
from home_study_agent.diagnostic_prompt import DiagnosticPromptService
from home_study_agent.child_model import ChildModelService
from home_study_agent.agent_orchestrator import InvalidAgentTurnOutcomeError
from home_study_agent.voice_capture_binding import VoiceCaptureBindingService
from home_study_agent.repository import StudyRepository
from test_curriculum_first_source_http import app_client
from test_paper_learning_selection import flow, choose, CHILD


def original_command(flow, *, requires_reply=True, canonical=False):
    db, _, _, sid, _, inputs = flow
    key, turn = str(uuid4()), str(uuid4())
    action={'type':'start_diagnostic','track':'school'}
    if canonical:
        captures=VoiceCaptureBindingService(db)
        captures.begin_realtime(sid,key,'agent_command',model_version='synthetic-no-cloud')
        capture=captures.complete_realtime(sid,key,'做这张卷。')
        key='realtime-'+capture['voice_input_id']
        action.update(voice_input_id=capture['voice_input_id'],voice_input_binding=AgentRuntimeStore.voice_capture_snapshot(capture))
    arrival = inputs.capture_arrival(sid, key, '做这张卷。')
    cap = CapabilityRegistry().require('diagnostic.start', 'study')
    store = AgentRuntimeStore(db)
    manifest = context_manifest(store.assemble_context(sid, ()))
    manifest['learning_input_arrival_id'] = arrival['arrival_id']
    with db.connect() as c:
        c.execute('''INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,
            action_json,tool_status,orchestrator_version,created_at,client_action_id)
            VALUES (?,?,?,?,'agent_plan','',?,'pending','test',datetime('now'),?)''',
            (turn, CHILD, sid, '做这张卷。', json.dumps(action), key))
        metadata = store.persist_plan(c, child_id=CHILD, session_id=sid, agent_turn_id=turn, client_action_id=key,
            context_manifest=manifest, decision=AgentPlanDecision(
                ObjectiveDraft('none', None, None, (), 'check'),
                PlannedStep(cap, {'track': 'school'}, '来做原题', cap.expected_evidence), '', requires_reply, 1))
        c.commit()
    return turn, metadata


def state(db, turn):
    with db.connect() as c:
        return tuple(c.execute('SELECT p.status,s.status,t.tool_status FROM agent_plan p JOIN agent_step s USING(plan_id) JOIN agent_turn t USING(agent_turn_id) WHERE t.agent_turn_id=?', (turn,)).fetchone())


def counts(db):
    with db.connect() as c:
        return tuple(c.execute('SELECT (SELECT COUNT(*) FROM diagnostic_prompt),(SELECT COUNT(*) FROM plan_item),(SELECT COUNT(*) FROM daily_plan)').fetchone())


def test_original_running_guard_remains_closed_without_admission(flow):
    choose(flow)
    turn, metadata = original_command(flow)
    assert state(flow[0], turn) == ('waiting_child', 'waiting_child', 'pending')
    result = flow[4].prepare(flow[3], paper_authorized_turn_id=turn, require_unexposed=True, client_action_id='actual-original-prepare')
    assert result['status'] == 'no_item' and result['reason'] == 'PAPER_SELECTION_VOICE_NOT_EXECUTABLE'


@pytest.mark.parametrize('requires_reply,canonical', [(True,False),(False,False),(True,True)])
def test_original_admission_prepares_once_and_reopens_same_receipt(flow, requires_reply,canonical):
    choose(flow)
    turn, _ = original_command(flow, requires_reply=requires_reply,canonical=canonical)
    db, _, _, sid, prompts, _ = flow
    before = counts(db)
    assert AgentRuntimeStore(db).admit_original_client_execution(sid, turn) == {'status': 'admitted'}
    assert state(db, turn) == ('running', 'running', 'pending')
    assert AgentRuntimeStore(Database(db.path)).admit_original_client_execution(sid, turn) == {'status': 'execution_receipt_required'}
    kwargs = dict(paper_authorized_turn_id=turn, require_unexposed=True, client_action_id='actual-original-prepare')
    result = prompts.prepare(sid, **kwargs)
    assert result['status'] == 'waiting_answer', result.get('reason')
    assert result['prompt']['diagnostic_item_id'] == flow[2]['diagnostic_item_id']
    reopened = DiagnosticPromptService(Database(db.path))
    assert reopened.prepare(sid, **kwargs)['prompt']['prompt_id'] == result['prompt']['prompt_id']
    assert reopened.read(sid)['prompt']['prompt_id'] == result['prompt']['prompt_id']
    assert counts(db) == (before[0]+1, before[1], before[2])


def test_real_application_http_original_admission_then_prepare(flow, tmp_path):
    choose(flow)
    turn, _ = original_command(flow)
    db, _, _, sid, _, _ = flow
    with app_client(tmp_path, db) as client:
        route=f'/v1/sessions/{sid}/agent-turns/{turn}/original-client-admission'
        assert client.post(route).json() == {'status':'admitted'}
        assert client.post(route).json() == {'status':'execution_receipt_required'}
        payload=dict(track='school',require_unexposed=True,paper_authorized_turn_id=turn,client_action_id='actual-http-prepare')
        result=client.post(f'/v1/sessions/{sid}/diagnostic-prompt/prepare',json=payload)
        assert result.status_code == 200, result.text
        assert result.json()['status'] == 'waiting_answer'
        assert client.post(f'/v1/sessions/{sid}/diagnostic-prompt/prepare',json=payload).json()['prompt']['prompt_id'] == result.json()['prompt']['prompt_id']
        receipt=dict(capability='diagnostic.start',execution_location='client_device_bridge',action_type='start_diagnostic',status='completed',completed_at=datetime.now(timezone.utc).isoformat(),output=result.json())
        outcome=client.put(f'/v1/agent-turns/{turn}/outcome',json=dict(final_response='请独立作答。',tool_status='completed',result={'execution_receipt':receipt,'client_bridge_receipt':receipt}))
        assert outcome.status_code==200,outcome.text
        with db.connect() as c:
            saved=json.loads(c.execute('SELECT result_json FROM agent_step WHERE plan_id=(SELECT plan_id FROM agent_plan WHERE agent_turn_id=?)',(turn,)).fetchone()[0])
        assert 'original_client_admission' not in saved
        assert saved['execution_receipt']['output']['prompt']['prompt_id']==result.json()['prompt']['prompt_id']


def test_two_real_concurrent_claims_grant_exactly_one_dispatch(flow):
    choose(flow)
    turn,_=original_command(flow)
    db,_,_,sid,_,_=flow
    before=counts(db)
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses=list(pool.map(lambda _:AgentRuntimeStore(db).admit_original_client_execution(sid,turn)['status'],range(2)))
    assert sorted(statuses)==['admitted','execution_receipt_required']
    assert state(db,turn)==('running','running','pending')
    assert counts(db)==before


def cancel_original_capture(db, turn):
    with db.connect() as c:
        key=c.execute('''SELECT b.client_action_id FROM voice_capture_binding b JOIN agent_turn t
            ON b.voice_input_id=json_extract(t.action_json,'$.voice_input_id') WHERE t.agent_turn_id=?''',(turn,)).fetchone()[0]
    VoiceCaptureBindingService(db).cancel(flow_session(db,turn),key)


def flow_session(db,turn):
    with db.connect() as c:
        return c.execute('SELECT session_id FROM agent_turn WHERE agent_turn_id=?',(turn,)).fetchone()[0]


def test_capture_revoked_after_admission_is_rechecked_inside_prepare(flow):
    choose(flow)
    turn,_=original_command(flow,canonical=True)
    db,_,_,sid,prompts,_=flow
    assert AgentRuntimeStore(db).admit_original_client_execution(sid,turn)=={'status':'admitted'}
    cancel_original_capture(db,turn)
    before=counts(db)
    with pytest.raises(InvalidAgentPlanError,match='VOICE_CAPTURE_CANCELLED'):
        prompts.prepare(sid,paper_authorized_turn_id=turn,require_unexposed=True,client_action_id='revoked-after-admit')
    assert counts(db)==before


def test_prepared_receipt_replays_after_capture_revocation_without_second_prompt(flow):
    choose(flow)
    turn,_=original_command(flow,canonical=True)
    db,_,_,sid,prompts,_=flow
    assert AgentRuntimeStore(db).admit_original_client_execution(sid,turn)=={'status':'admitted'}
    kwargs=dict(paper_authorized_turn_id=turn,require_unexposed=True,client_action_id='prepared-before-revocation')
    prepared=prompts.prepare(sid,**kwargs)
    cancel_original_capture(db,turn)
    before=counts(db)
    recovered=DiagnosticPromptService(Database(db.path)).prepare(sid,**kwargs)
    assert recovered['prompt']['prompt_id']==prepared['prompt']['prompt_id']
    assert counts(db)==before


@pytest.mark.parametrize('revocation',['stale_snapshot','tampered_stamp'])
def test_prepare_revalidates_current_decision_and_claim_binding(flow,revocation):
    choose(flow)
    turn,_=original_command(flow)
    db,_,_,sid,prompts,_=flow
    assert AgentRuntimeStore(db).admit_original_client_execution(sid,turn)=={'status':'admitted'}
    with db.connect() as c:
        if revocation=='stale_snapshot':
            c.execute('UPDATE daily_session SET state=\'study\',last_checkpoint_at=? WHERE session_id=?',((datetime.now(timezone.utc)+timedelta(minutes=1)).isoformat(),sid))
        else:
            c.execute('''UPDATE agent_step SET result_json=json_set(result_json,'$.original_client_admission.binding_sha256',?)
                WHERE plan_id=(SELECT plan_id FROM agent_plan WHERE agent_turn_id=?)''',('0'*64,turn))
        c.commit()
    if revocation=='stale_snapshot': ChildModelService(db).snapshot(CHILD)
    before=counts(db)
    with pytest.raises(InvalidAgentPlanError,match='CHILD_MODEL_SNAPSHOT_STALE|ORIGINAL_CLIENT_CLAIM_CHANGED'):
        prompts.prepare(sid,paper_authorized_turn_id=turn,require_unexposed=True,client_action_id='revoked-original')
    assert counts(db)==before


def test_second_cas_write_failure_rolls_back_first_claim_and_releases_writer(flow):
    choose(flow)
    turn,_=original_command(flow)
    db,_,_,sid,_,_=flow
    with db.connect() as c:
        c.execute("CREATE TRIGGER reject_original_claim BEFORE UPDATE ON agent_step WHEN NEW.status='running' BEGIN SELECT RAISE(ABORT,'CONTROLLED_SECOND_CAS_FAILURE'); END")
        c.commit()
    before,original_state=counts(db),state(db,turn)
    with pytest.raises(sqlite3.IntegrityError,match='CONTROLLED_SECOND_CAS_FAILURE'):
        AgentRuntimeStore(db).admit_original_client_execution(sid,turn)
    assert state(db,turn)==original_state and counts(db)==before
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        assert c.execute('SELECT result_json FROM agent_step WHERE plan_id=(SELECT plan_id FROM agent_plan WHERE agent_turn_id=?)',(turn,)).fetchone()[0] is None
        c.rollback()


@pytest.mark.parametrize('revocation', ['old_turn','cross_child_session','child_retired','session_closed','cancelled','completed','wrong_capability','wrong_arguments','source_deleted','arrival_changed','stale_snapshot','canonical_capture_cancelled','canonical_binding_changed'])
def test_rejected_or_repeat_admission_never_dispatches_or_changes_state(flow, revocation):
    choose(flow)
    turn, _ = original_command(flow,canonical=revocation.startswith('canonical_'))
    db, _, _, sid, _, inputs = flow
    expected_error = True
    with db.connect() as c:
        if revocation == 'child_retired': c.execute("UPDATE child_profile SET status='archived' WHERE child_id=?", (CHILD,))
        elif revocation == 'session_closed': c.execute("UPDATE daily_session SET status='completed' WHERE session_id=?", (sid,))
        elif revocation in {'cancelled','completed'}:
            expected_error=False
            c.execute('UPDATE agent_plan SET status=? WHERE agent_turn_id=?', (revocation,turn))
            c.execute('UPDATE agent_step SET status=? WHERE plan_id=(SELECT plan_id FROM agent_plan WHERE agent_turn_id=?)', (revocation,turn))
        elif revocation == 'wrong_capability': c.execute("UPDATE agent_step SET capability_name='diagnostic.replay' WHERE plan_id=(SELECT plan_id FROM agent_plan WHERE agent_turn_id=?)", (turn,))
        elif revocation == 'wrong_arguments': c.execute("UPDATE agent_step SET arguments_json='{}' WHERE plan_id=(SELECT plan_id FROM agent_plan WHERE agent_turn_id=?)", (turn,))
        elif revocation == 'source_deleted': c.execute("UPDATE agent_turn SET tool_error_code='SOURCE_DELETED' WHERE agent_turn_id=?", (turn,))
        elif revocation == 'arrival_changed': c.execute("UPDATE learning_input_arrival SET utterance='不同的请求' WHERE client_action_id=(SELECT client_action_id FROM agent_turn WHERE agent_turn_id=?)", (turn,))
        elif revocation == 'stale_snapshot': c.execute('UPDATE daily_session SET state=\'study\',last_checkpoint_at=? WHERE session_id=?', ((datetime.now(timezone.utc)+timedelta(minutes=1)).isoformat(),sid))
        elif revocation == 'canonical_capture_cancelled': c.execute("UPDATE voice_capture_binding SET status='cancelled'")
        elif revocation == 'canonical_binding_changed': c.execute("UPDATE agent_turn SET action_json=json_set(action_json,'$.voice_input_binding.question_validation','stale') WHERE agent_turn_id=?",(turn,))
        c.commit()
    if revocation == 'old_turn': original_command(flow)
    if revocation == 'stale_snapshot': ChildModelService(db).snapshot(CHILD)
    route_sid=sid
    if revocation=='cross_child_session':
        route_sid=str(uuid4())
        with db.connect() as c:
            c.execute('''INSERT INTO daily_session(session_id,child_id,status,state,started_at,last_checkpoint_at,client_action_id)
                VALUES(?,'child-primary-4','completed','completed',datetime('now'),datetime('now'),'historical-other-child')''',(route_sid,))
            c.commit()
    before, original_state = counts(db), state(db,turn)
    if expected_error:
        expected=InvalidAgentTurnOutcomeError if revocation=='source_deleted' else (ValueError,KeyError)
        with pytest.raises(expected):
            AgentRuntimeStore(db).admit_original_client_execution(route_sid,turn)
    else:
        assert AgentRuntimeStore(db).admit_original_client_execution(sid,turn) == {'status':'execution_receipt_required'}
    assert state(db,turn) == original_state
    assert counts(db) == before


@pytest.mark.parametrize('repair', [False, True], ids=['ordinary_plan', 'one_original_repair'])
def test_real_orchestrator_binds_original_diagnostic_arrival_before_http_admission(flow, tmp_path, monkeypatch, repair):
    """No hand-built turn/plan/manifest: public voice -> original planner/parser -> real admission/prepare."""
    import socket
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    from home_study_agent.model_gateway import ModelResult
    from test_cloud_first_agent import Planner

    def no_network(*args, **kwargs):
        raise AssertionError('REAL_NETWORK_FORBIDDEN_IN_SYNTHETIC_PLANNER_CONTROL')
    monkeypatch.setattr(socket.socket, 'connect', no_network)
    utterance = '做这张卷。'  # Existing R42 public start_paper fixture, unchanged.
    class OriginalPlanner(Planner):
        def __init__(self):
            super().__init__('diagnostic.start', {'track':'school'}, utterance, objective_type='learning')
            self.payload['requires_child_reply'] = True
            self.request_manifests = []
        async def complete(self, request):
            self.requests.append(request)
            self.request_manifests.append(json.dumps(request.context_manifest, ensure_ascii=False, sort_keys=True))
            return ModelResult(content='{' if repair and len(self.requests)==1 else json.dumps(self.payload,ensure_ascii=False),
                provider='test',model='synthetic-original-planner',latency_ms=1,prompt_tokens=10,completion_tokens=10)

    db, _, packet, sid, _, _ = flow
    choose(flow)
    gateway=OriginalPlanner()
    settings=Settings(data_dir=tmp_path,database_path=db.path,maintenance_enabled=False,
                      session_token='synthetic-original-diagnostic-arrival',agent_runtime_enabled=True)
    with TestClient(create_app(settings,model_gateway=gateway),client=('127.0.0.1',41200),
                    headers={'X-HSC-Session':settings.session_token}) as client:
        captures=VoiceCaptureBindingService(client.app.state.agent_orchestrator.database)
        captures.begin_realtime(sid,'diagnostic-real-capture','agent_command',model_version='synthetic-no-cloud')
        capture=captures.complete_realtime(sid,'diagnostic-real-capture',utterance)
        key='realtime-'+capture['voice_input_id']
        response=client.post(f'/v1/sessions/{sid}/agent-turn',json={
            'transcript':utterance,'client_action_id':key,'cloud_text_allowed':True,
            'voice_input_id':capture['voice_input_id']})
        assert response.status_code==200
        turn=response.json()
        assert turn['action']['type']=='start_diagnostic' and turn['action']['source']=='agent_runtime_planner'
        assert len(gateway.requests)==1+int(repair)
        assert all(r.request_type=='agent_plan' for r in gateway.requests)
        assert [r.prompt_version for r in gateway.requests][-1].endswith('.repair1') is repair
        with db.connect() as c:
            arrival=c.execute('SELECT * FROM learning_input_arrival WHERE client_action_id=?',(key,)).fetchone()
            saved=c.execute('SELECT context_manifest_json FROM agent_plan WHERE agent_turn_id=?',(turn['agent_turn_id'],)).fetchone()
            manifest=json.loads(saved[0])
            assert arrival and (arrival['session_id'],arrival['child_id'],arrival['utterance'])==(sid,CHILD,utterance)
        admitted=client.post(f"/v1/sessions/{sid}/agent-turns/{turn['agent_turn_id']}/original-client-admission")
        assert admitted.status_code==200, {'status':admitted.status_code,'original_arrival_in_plan':manifest.get('learning_input_arrival_id')==arrival['arrival_id']}
        assert admitted.json()=={'status':'admitted'}
        assert manifest['learning_input_arrival_id']==arrival['arrival_id']
        assert all('learning_input_arrival_id' not in r.context_manifest for r in gateway.requests)
        for index,(before,request) in enumerate(zip(gateway.request_manifests,gateway.requests)):
            original=json.loads(before)
            expected={**original,'local_learning_completion_guard':False} if index==len(gateway.requests)-1 else original
            assert request.context_manifest==expected
        request=dict(track='school',require_unexposed=True,paper_authorized_turn_id=turn['agent_turn_id'],client_action_id='real-orchestrator-prepare')
        prepared=client.post(f'/v1/sessions/{sid}/diagnostic-prompt/prepare',json=request)
        assert prepared.status_code==200 and prepared.json()['status']=='waiting_answer', {'status':prepared.status_code,'reason':prepared.json().get('reason')}
        prompt=prepared.json()['prompt']
        assert prompt['diagnostic_item_id']==packet['diagnostic_item_id']
        assert client.post(f'/v1/sessions/{sid}/diagnostic-prompt/prepare',json=request).json()['prompt']['prompt_id']==prompt['prompt_id']
        assert client.post(f"/v1/sessions/{sid}/agent-turns/{turn['agent_turn_id']}/original-client-admission").json()=={'status':'execution_receipt_required'}
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt').fetchone()[0]==1
            calls=[dict(r)for r in c.execute("SELECT purpose,status,error_code FROM model_call WHERE purpose IN ('agent_plan','agent_plan_repair') ORDER BY rowid")]
        assert [r['purpose']for r in calls]==(['agent_plan','agent_plan_repair']if repair else ['agent_plan'])
        assert [r['status']for r in calls]==(['failed','completed']if repair else ['completed'])
        if repair:assert calls[0]['error_code']=='MODEL_INVALID_AGENT_PLAN'
    reopened=DiagnosticPromptService(Database(db.path)).read(sid)
    assert reopened['prompt']['prompt_id']==prompt['prompt_id']
