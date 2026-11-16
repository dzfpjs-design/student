"""C05: actual task/answer receipts, synthetic catalog and no model or device."""
from datetime import datetime, timedelta, timezone
import json
import os
import subprocess
import sys

import pytest

from home_study_agent.agent_capability_executor import AgentCapabilityExecutor
from home_study_agent.agent_runtime import AgentRuntimeStore
from home_study_agent.child_model import ChildModelService
from home_study_agent.companion_activity_outcome import CompanionActivityOutcomeService
from home_study_agent.database import Database
from home_study_agent.diagnostic_prompt import DiagnosticPromptService
from home_study_agent.repository import StudyRepository
from test_goal_task_prompt import command
from test_goal_roadmap_execution import execution, goal_db, exact_goal_item, CHILD, OTHER, change


@pytest.fixture
def returned(execution):
    db, sid, pid, _, exact = execution
    repo = StudyRepository(db)
    started = AgentCapabilityExecutor(db, repo).execute(command(execution))
    prompt = started['result']['diagnostic_prompt']['prompt']
    return open_resume(db, sid, pid, prompt, exact[1])


def open_resume(db, sid, pid, prompt, expected):
    repo = StudyRepository(db)
    repo.pause_plan_item(pid, 'pause-before-companion')
    runtime = AgentRuntimeStore(db)
    runtime.start_companion_activity(sid, 'movement', 'Synthetic stretching', 'open-activity', pid)
    service = CompanionActivityOutcomeService(db)
    oid = service.list(CHILD)['items'][0]['outcome_id']
    runtime.end_companion_activity(sid, 'turn_limit')
    requested = service.request_resume(CHILD, oid, note='Continue original task', actor='child',
        expected_revision=1, client_action_id='request-resume')['event']
    key = 'companion-outcome-resume:' + requested['event_id']
    repo.resume_plan_item(pid, key)
    with db.connect() as c:
        receipt = dict(c.execute('SELECT * FROM plan_item_event WHERE client_action_id=?', (key,)).fetchone())
    service.confirm_resume(CHILD, oid, request_event_id=requested['event_id'], plan_item_event_id=receipt['event_id'],
        expected_revision=requested['revision'], client_action_id='confirm-resume')
    return db, sid, pid, prompt, expected, oid, receipt


@pytest.fixture
def homework_returned(execution):
    db, sid = execution[:2]
    with db.connect() as c:
        pid = c.execute("SELECT plan_item_id FROM plan_item WHERE source_type='school_homework'").fetchone()[0]
    started = AgentCapabilityExecutor(db, StudyRepository(db)).execute(command((db, sid, pid, None, None)))
    assert started['result']['status'] == 'active'
    return open_resume(db, sid, pid, None, None)


def saved_answer(j, text=None, hint_level=0):
    db, sid, _, prompt, expected, _, _ = j
    result = DiagnosticPromptService(db).answer(sid, prompt_id=prompt['prompt_id'],
        item_version=prompt['item_version'], answer=expected if text is None else text,
        hint_level=hint_level, client_action_id='next-independent-response')
    assert result['status'] == 'answered', result
    assert result['attempt']['score_status'] == 'verified_local', result
    return result['attempt']


def snap(j, child=CHILD):
    return ChildModelService(j[0]).snapshot(child)


def balance(j, child=CHILD):
    return snap(j, child)['payload']['companion_balance']


@pytest.mark.parametrize('correct', [True, False])
def test_real_resume_then_independent_answer_verifies_return_not_success(returned, correct):
    j = returned
    assert balance(j)['verified_child_return_sessions'] == 0
    attempt = saved_answer(j, None if correct else '999999')
    view = balance(j)
    assert view['task_resume_receipt_sessions'] == view['verified_child_return_sessions'] == 1
    assert view['habit_success_sessions'] == 0
    proof = view['outcome_evidence'][0]['return_verification']
    assert proof['status'] == 'verified' and proof['evidence_refs'] == [attempt['evidence_id']]
    assert proof['basis'] == ['independent_answer']
    assert 'submitted_answer' not in json.dumps(proof)
    detail = CompanionActivityOutcomeService(j[0]).get(CHILD, j[5])['activity']
    assert detail['resume']['actual_child_return_verified'] is True
    assert detail['habit_evidence_eligible'] is False


@pytest.mark.parametrize('mutation', ['source', 'task_source', 'prompt_version', 'child', 'unrelated_binding',
                                     'hint', 'not_independent', 'score', 'withdraw', 'receipt_deleted'])
def test_current_evidence_or_exact_task_loss_downgrades_without_fabricating_history(returned, mutation):
    j = returned
    attempt = saved_answer(j)
    before = snap(j)
    assert before['payload']['companion_balance']['verified_child_return_sessions'] == 1
    db, _, pid, prompt = j[:4]
    eid = attempt['evidence_id']
    if mutation == 'source':
        change(db, "UPDATE diagnostic_item SET status='inactive' WHERE diagnostic_item_id=?", (prompt['diagnostic_item_id'],))
    elif mutation == 'task_source':
        change(db, "UPDATE plan_item SET source_ref='replacement' WHERE plan_item_id=?", (pid,))
    elif mutation == 'prompt_version':
        change(db, "UPDATE diagnostic_prompt SET item_version='replacement' WHERE prompt_id=?", (prompt['prompt_id'],))
    elif mutation == 'child':
        change(db, 'UPDATE learning_evidence SET child_id=? WHERE evidence_id=?', (OTHER, eid))
    elif mutation == 'unrelated_binding':
        change(db, "UPDATE agent_plan SET context_manifest_json=json_remove(context_manifest_json,'$.goal_prompt_binding')")
    elif mutation in {'hint', 'not_independent', 'score'}:
        column, value = {'hint': ('hint_level', 1), 'not_independent': ('independent', 0), 'score': ('confidence', .1)}[mutation]
        change(db, f'UPDATE learning_evidence SET {column}=? WHERE evidence_id=?', (value, eid))
    elif mutation == 'withdraw':
        # Exact privacy withdrawal journal uses the same reader as source deletion.
        from home_study_agent.learning_obligations import withdrawn_evidence_ids
        from home_study_agent.privacy_source_withdrawal import ensure_schema, record
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            ensure_schema(c)
            record(c, CHILD, [prompt['diagnostic_item_id']], [eid])
            assert eid in withdrawn_evidence_ids(c, CHILD)
            c.commit()
    else:
        change(db, 'DELETE FROM plan_item_event WHERE event_id=?', (j[6]['event_id'],))
    after = snap(j)
    result = after['payload']['companion_balance']
    assert result['verified_child_return_sessions'] == 0
    assert result['habit_success_sessions'] == 0
    assert result['task_resume_receipt_sessions'] == (0 if mutation in {'task_source', 'receipt_deleted'} else 1)
    assert after['evidence_fingerprint'] != before['evidence_fingerprint']
    assert balance(j, OTHER)['verified_child_return_sessions'] == 0


def test_verified_return_survives_completed_task_and_process_restart(returned):
    j = returned
    saved_answer(j)
    StudyRepository(j[0]).complete_plan_item(j[2], 1, 'task-completed', completion_note='Checked the actual answer')
    expected = balance(j)
    assert expected['verified_child_return_sessions'] == expected['task_resume_receipt_sessions'] == 1
    code = """import json,sys
from home_study_agent.database import Database
from home_study_agent.child_model import ChildModelService
print(json.dumps(ChildModelService(Database(sys.argv[1])).snapshot(sys.argv[2])['payload']['companion_balance']))
"""
    completed = subprocess.run([sys.executable, '-c', code, str(j[0].path), CHILD],
        env={**os.environ, 'PYTHONPATH': os.pathsep.join(sys.path)}, capture_output=True, text=True, check=True)
    assert json.loads(completed.stdout) == expected


@pytest.mark.parametrize('boundary', ['before_resume', 'pause', 'unrelated_resume', 'hint', 'another_activity'])
def test_answer_outside_original_resumed_segment_or_with_help_is_not_return(returned, boundary):
    j = returned
    repo = StudyRepository(j[0])
    if boundary in {'pause', 'unrelated_resume'}:
        repo.pause_plan_item(j[2], 'paused-again')
        if boundary == 'unrelated_resume':
            repo.resume_plan_item(j[2], 'later-unrelated-resume')
    if boundary == 'another_activity':
        AgentRuntimeStore(j[0]).start_companion_activity(j[1], 'movement', 'Another break', 'second-activity', j[2])
    attempt = saved_answer(j, hint_level=1 if boundary == 'hint' else 0)
    if boundary == 'before_resume':
        old = (datetime.fromisoformat(j[6]['occurred_at']) - timedelta(seconds=1)).isoformat()
        change(j[0], 'UPDATE learning_evidence SET occurred_at=? WHERE evidence_id=?', (old, attempt['evidence_id']))
        change(j[0], 'UPDATE diagnostic_attempt SET created_at=? WHERE evidence_id=?', (old, attempt['evidence_id']))
    view = balance(j)
    assert view['task_resume_receipt_sessions'] == 1
    assert view['verified_child_return_sessions'] == view['habit_success_sessions'] == 0


@pytest.mark.parametrize('answered_before_restart', [True, False])
def test_unclean_runtime_pause_closes_return_segment_without_erasing_prior_evidence(returned, answered_before_restart):
    j = returned
    db, _, pid = j[:3]
    old_run, _ = db.begin_runtime()
    if answered_before_restart:
        saved_answer(j)
    recovered = Database(db.path)
    new_run, unclean = recovered.begin_runtime()
    assert unclean and new_run != old_run
    with recovered.connect() as c:
        event = c.execute("SELECT payload_json FROM plan_item_event WHERE plan_item_id=? AND event_type='paused' ORDER BY rowid DESC", (pid,)).fetchone()
        assert json.loads(event[0])['reason'] == 'unclean_restart'
    if not answered_before_restart:
        # A valid delayed answer still belongs to its question, not to a closed
        # activity-return segment or a new claim that the child resumed work.
        saved_answer(j)
    view = balance(j)
    assert view['task_resume_receipt_sessions'] == 1
    assert view['verified_child_return_sessions'] == int(answered_before_restart)
    assert view['habit_success_sessions'] == 0
    assert balance(j, OTHER)['verified_child_return_sessions'] == 0


def observation_samples(j, defect=None):
    from home_study_agent.behavior_model import BehaviorModelService
    from home_study_agent.behavior_model.app_integration import BehaviorAppIntegration
    from home_study_agent.behavior_model.outcome_evidence import task_context_snapshot
    db, sid, pid = j[:3]
    integration = BehaviorAppIntegration(BehaviorModelService(db), db)
    service = integration.service
    start = datetime.now(timezone.utc)
    service.begin_observation_stream(CHILD, sid, stream_epoch='return-stream',
        at=start.isoformat(), client_action_id='observation-start')
    with db.connect() as c:
        task = dict(c.execute('SELECT pi.*,dp.version AS plan_version FROM plan_item pi JOIN daily_plan dp USING(plan_id) WHERE plan_item_id=?', (pid,)).fetchone())
    context = {k: task[k] for k in ('plan_item_id','subject','task_type','cognitive_load','source_type','plan_version')}
    if defect == 'other_task':
        context['plan_item_id'] = 'another-task'
    if defect == 'plan_version':
        context['plan_version'] += 1
    ids = []
    for i in range(1 if defect == 'single_sample' else 2):
        now = datetime.now(timezone.utc)
        state = 'present' if defect == 'presence_only' else 'writing_like'
        state_id = 'return-state-' + str(i)
        integration.observation(sid, dict(observation_ready=True, shadow=defect == 'shadow',
            state_event_id=state_id, state=state, confidence=.9, observed_at=now.isoformat(), model_version='synthetic_classifier'))
        sample = dict(schema_version='observation_coverage_v1', child_id=CHILD, session_id=sid,
            stream_epoch='return-stream', sequence=i, captured_at=now.isoformat(), received_at=now.isoformat(),
            capture_monotonic_ms=int((now-start).total_seconds()*1000),
            quality=dict(observation_ready=True,scene_quality='ready',person_baseline='established',
                         shadow=defect == 'shadow',analysis_skipped=False),
            task_context=task_context_snapshot(context), state=state, confidence=.9,
            model_version='synthetic_classifier', analysis_id='return-analysis-' + str(i),
            producer_epoch='producer', state_event_id=state_id)
        ids.append(service.record_observation_sample(CHILD, sid, sample)['event_id'])
    return service, ids


def test_qualified_same_task_observation_is_return_not_independence_and_retracts(returned):
    j = returned
    service, ids = observation_samples(j)
    before = snap(j)
    view = before['payload']['companion_balance']
    assert view['verified_child_return_sessions'] == 1
    proof = view['outcome_evidence'][0]['return_verification']
    assert proof['basis'] == ['qualified_writing_observation']
    assert set(proof['evidence_refs']) == set(ids)
    assert proof['independent'] is False and proof['activity_initiated_by'] == 'unknown'
    assert view['habit_success_sessions'] == 0
    service.retract_event('observation:return-state-0', CHILD, 'withdraw-state', 'Synthetic observation corrected')
    after = snap(j)
    assert after['payload']['companion_balance']['verified_child_return_sessions'] == 0
    assert after['evidence_fingerprint'] != before['evidence_fingerprint']


@pytest.mark.parametrize('defect', ['other_task', 'plan_version', 'presence_only', 'shadow', 'single_sample'])
def test_observation_requires_existing_qualified_task_intervals(returned, defect):
    observation_samples(returned, defect)
    assert balance(returned)['verified_child_return_sessions'] == 0


def test_homework_observation_uses_frozen_original_source_version(homework_returned):
    j = homework_returned
    observation_samples(j)
    before = snap(j)
    assert before['payload']['companion_balance']['verified_child_return_sessions'] == 1
    change(j[0], "UPDATE homework_item SET title='Different work' WHERE homework_id=(SELECT homework_id FROM plan_item WHERE plan_item_id=?)", (j[2],))
    after = snap(j)
    assert after['payload']['companion_balance']['verified_child_return_sessions'] == 0
    assert after['payload']['companion_balance']['task_resume_receipt_sessions'] == 1
    assert after['evidence_fingerprint'] != before['evidence_fingerprint']


def test_homework_completion_preserves_verified_prior_return(homework_returned):
    j = homework_returned
    observation_samples(j)
    assert balance(j)['verified_child_return_sessions'] == 1
    StudyRepository(j[0]).complete_plan_item(j[2], 1, 'homework-complete', completion_note='Finished the original work')
    assert balance(j)['verified_child_return_sessions'] == 1


def test_http_original_answer_parent_projection_restart_and_source_withdrawal(returned, monkeypatch):
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    from home_study_agent.model_gateway import ModelGateway
    from home_study_agent.privacy_source_withdrawal import ensure_schema, record
    async def no_model(*args, **kwargs):
        pytest.fail('Local receipt projection must not call a model')
    monkeypatch.setattr(ModelGateway, 'complete', no_model)
    j = returned
    settings = Settings(data_dir=j[0].path.parent, database_path=j[0].path,
        data_encryption_key=b'c' * 32, maintenance_enabled=False)
    route = '/v1/parent/children/' + CHILD + '/longitudinal-model'
    payload = dict(prompt_id=j[3]['prompt_id'], item_version=j[3]['item_version'], answer=j[4],
                   client_action_id='http-after-resume')
    with TestClient(create_app(settings)) as client:
        token = client.post('/v1/parent/setup', json={'pin': '7391'}).json()['parent_token']
        response = client.post(f'/v1/sessions/{j[1]}/diagnostic-prompt/answer', json=payload)
        assert response.status_code == 200, response.text[:400]
        assert response.json()['status'] == 'answered', response.json()
        eid = response.json()['attempt']['evidence_id']
        result = client.get(route, headers={'X-HSC-Parent': token}).json()
        assert result['payload']['companion_balance']['verified_child_return_sessions'] == 1
    with TestClient(create_app(settings)) as client:
        token = client.post('/v1/parent/unlock', json={'pin': '7391'}).json()['parent_token']
        replay = client.post(f'/v1/sessions/{j[1]}/diagnostic-prompt/answer', json=payload)
        assert replay.status_code == 200 and replay.json()['attempt']['evidence_id'] == eid
        assert client.get(route, headers={'X-HSC-Parent': token}).json()['payload']['companion_balance']['verified_child_return_sessions'] == 1
        with j[0].connect() as c:
            c.execute('BEGIN IMMEDIATE')
            ensure_schema(c)
            record(c, CHILD, [j[3]['diagnostic_item_id']], [eid])
            c.commit()
        after = client.get(route, headers={'X-HSC-Parent': token}).json()['payload']['companion_balance']
        assert after['verified_child_return_sessions'] == 0
        assert after['task_resume_receipt_sessions'] == 1
        assert after['habit_success_sessions'] == 0


def test_same_question_tutor_exposure_does_not_certify_independent_return(returned):
    j = returned
    with j[0].connect() as c:
        question = c.execute('SELECT prompt FROM diagnostic_item WHERE diagnostic_item_id=?', (j[3]['diagnostic_item_id'],)).fetchone()[0]
    StudyRepository(j[0]).save_tutor_turn(j[1], j[2], '数学', question, 'Check the relation first.',
        'local', 'synthetic', 'saved-hint', hint_level=1)
    saved_answer(j)
    assert balance(j)['verified_child_return_sessions'] == 0


def test_unrelated_unbound_question_same_session_does_not_count(homework_returned):
    j = homework_returned
    from home_study_agent.learning_state import LearningStateService
    with j[0].connect() as c:
        item = dict(c.execute("SELECT * FROM diagnostic_item WHERE source_ref LIKE 'local_verified_%' LIMIT 1").fetchone())
    answer = json.loads(item['expected_answer_json'])['exact_answers'][0]
    receipt = LearningStateService(j[0]).submit_diagnostic(j[1], item['diagnostic_item_id'], answer, 'unrelated-answer')
    assert receipt['score_status'] == 'verified_local'
    assert balance(j)['verified_child_return_sessions'] == 0


def test_replayed_answer_does_not_add_return_and_projection_is_read_only(returned):
    from home_study_agent.companion_activity_outcome import project_companion_activity_evidence
    j = returned
    saved_answer(j)
    before = snap(j)
    saved_answer(j)
    assert snap(j)['snapshot_id'] == before['snapshot_id']
    with j[0].connect() as c:
        c.execute('BEGIN')
        changes = c.total_changes
        projection = project_companion_activity_evidence(c, CHILD, (datetime.now(timezone.utc)-timedelta(days=14)).isoformat())
        assert c.total_changes == changes
        assert projection['verified_child_return_count'] == 1
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 1
        assert c.execute('SELECT COUNT(*) FROM companion_activity_outcome_event').fetchone()[0] == 3


def test_old_homework_outcome_does_not_backfill_current_source_snapshot(homework_returned):
    j = homework_returned
    observation_samples(j)
    change(j[0], "UPDATE companion_activity_outcome SET source_snapshot_json=json_remove(source_snapshot_json,'$.return_task_source') WHERE outcome_id=?", (j[5],))
    proof = balance(j)['outcome_evidence'][0]['return_verification']
    assert proof['status'] == 'unknown'
    assert proof['reason'] == 'exact_task_question_binding_unavailable'
