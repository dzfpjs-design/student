"""Shared start admission for real published, resource-waiting PET tasks."""
import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from home_study_agent.agent_runtime import (
    AgentRuntimeStore, AgentPlanDecision, ObjectiveDraft, PlannedStep, CapabilityRegistry,
)
from home_study_agent import repository
from test_supply_gap_resume_http import supplied, facts, execute, CHILD
from test_supply_gap_listening_wait import publish_listening
from test_pet_listening_resource_admission import bind_audio


def start_command(case, *, child_id=CHILD, action_extra=None):
    """Real Runtime command ingress, not a mocked resolver or target-plan insert."""
    key = str(uuid4())
    cap = CapabilityRegistry().require('task.start', 'study')
    action = {'type': 'start_next_task', **(action_extra or {})}
    with case['db'].connect() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute('''INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,
            action_json,tool_status,orchestrator_version,created_at,client_action_id)
            VALUES(?,?,?,'开始这项任务','start_task','',?,'pending','isolated-runtime-ingress',?,?)''',
            (key, child_id, case['sid'], json.dumps(action), repository.utc_now(), key))
        AgentRuntimeStore(case['db']).persist_plan(c, child_id=child_id, session_id=case['sid'],
            agent_turn_id=key, client_action_id=key, context_manifest={},
            decision=AgentPlanDecision(ObjectiveDraft('new', 'execution', 'Start scheduled task',
                ('Actual task started',), 'execute'), PlannedStep(cap, {}, 'Explicit start request',
                cap.expected_evidence), '', False, 1))
        c.commit()
    return {'agent_turn_id': key}


def waiting_with_audio(case):
    published = publish_listening(case)
    case['speech']['selector'] = {'subject': None, 'task_kind': 'listening', 'relative_order': None}
    original = case['say']('place-before-audio', '题目补好了，继续原来的听力练习。')
    response = execute(case, original)
    assert response.status_code == 200, response.text
    receipt = response.json()['execution']
    assert receipt['result']['status'] == 'awaiting_resources'
    prompt = receipt['result']['diagnostic_prompt']['prompt']
    pid = receipt['result']['plan_item_id']
    media, _ = bind_audio(case['client'], case['db'], case['tmp_path'], case['sid'], prompt, approved=False)
    # Simulated test-parent reference attestation; synthetic signal, no acoustic/content proof.
    case['post'](case['client'], f"/v1/sessions/{case['sid']}/pet-media/{media['media_id']}/source-review",
        {'audio_sha256': media['audio']['sha256'], 'operation': 'approve', 'full_spoken_reference': True, 'client_action_id': 'review-original-audio'})
    return original, receipt, prompt, pid, published


def lower_budget(case):
    rule = case['client'].get(f'/v1/parent/children/{CHILD}/family-rule').json()
    response = case['client'].put(f'/v1/parent/children/{CHILD}/family-rule', json={
        **{k: rule[k] for k in ('latest_end_time', 'max_learning_minutes', 'task_segment_minutes',
            'break_minutes', 'pet_min_minutes', 'competition_minutes', 'observation_mode', 'reminder_budget_per_10m')},
        'task_segment_minutes': 10})
    assert response.status_code == 200, response.text


@pytest.mark.parametrize('supplied', ['listening_wait'], indirect=True)
@pytest.mark.parametrize('capability', ['task.start', 'task.resume'])
@pytest.mark.parametrize('change', ['budget', 'none', 'timer_budget', 'late', 'withdraw', 'cancel', 'receipt_owner'])
def test_both_start_commands_obey_current_supply_budget(supplied, capability, change, monkeypatch):
    case = supplied
    original, receipt, prompt, pid, published = waiting_with_audio(case)
    if change == 'budget':
        lower_budget(case)
    before = facts(case)
    with case['db'].connect() as c:
        original_manifest = c.execute('SELECT context_manifest_json FROM agent_plan WHERE agent_turn_id=?',
            (original['agent_turn_id'],)).fetchone()[0]
    command = (start_command(case, action_extra={'control_consent_version': [], 'supply_resume_receipt': None})
        if capability == 'task.start' else case['say']('new-resume', '原音补好了，请继续原任务。', prompt=prompt))
    if change in {'timer_budget', 'cancel'}:
        repository = case['client'].app.state.repository
        start = repository.start_plan_item
        def change_before_timer(*args, **kwargs):
            if change == 'timer_budget':
                repository.update_family_rule(CHILD, {
                    **repository.get_family_rule(CHILD), 'task_segment_minutes': 10})
            else:
                from home_study_agent.diagnostic_prompt import DiagnosticPromptService
                DiagnosticPromptService(case['db']).cancel(case['sid'], prompt_id=prompt['prompt_id'],
                    item_version=prompt['item_version'])
            return start(*args, **kwargs)
        monkeypatch.setattr(repository, 'start_plan_item', change_before_timer)
    elif change == 'late':
        from home_study_agent import supply_gap_resume
        from home_study_agent.learning_input_replan import ZONE
        day = datetime.fromisoformat(before['daily_plan'][0]['plan_date'])
        at = day.replace(tzinfo=ZONE, hour=23, minute=58).astimezone(timezone.utc)
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return at.astimezone(tz)
        monkeypatch.setattr(supply_gap_resume, 'datetime', Clock)
    elif change == 'withdraw':
        case['post'](case['client'], case['base'] + f"/{published['candidate_id']}/withdraw",
            {'expected_version': 4, 'action_id': 'withdraw-before-start', 'note': 'Synthetic source withdrawal'})
    elif change == 'receipt_owner':
        # Fault injection into the stored placement, not into caller args.
        with case['db'].connect() as c:
            row = c.execute('''SELECT decision_id,metadata_json FROM plan_constraint_decision
                WHERE json_extract(metadata_json,'$.plan_item_id')=?''', (pid,)).fetchone()
            data = json.loads(row['metadata_json'])
            data['supply_resume']['child_id'] = 'child-junior-3'
            c.execute('UPDATE plan_constraint_decision SET metadata_json=? WHERE decision_id=?',
                (json.dumps(data), row['decision_id']))
            c.commit()
    response = case['client'].post(f"/v1/agent-turns/{command['agent_turn_id']}/execute")
    if change != 'none':
        if change == 'cancel' and capability == 'task.start':
            assert response.status_code == 422 and response.json()['code'] == 'PLAN_INPUT_REQUIRED', response.text
        else:
            assert response.status_code == 409, response.text
        if change in {'budget', 'timer_budget', 'late'}:
            assert 'SUPPLY_RESUME_BUDGET_INSUFFICIENT' in response.text
    else:
        assert response.status_code == 200, response.text
        result = response.json()['execution']['result']
        assert result['status'] == 'active' and result['plan_item_id'] == pid
        assert result['diagnostic_prompt']['prompt']['prompt_id'] == prompt['prompt_id']
        saved = facts(case)
        with case['db'].connect() as c:
            stored = c.execute('''SELECT result_json FROM agent_step JOIN agent_plan USING(plan_id)
                WHERE agent_turn_id=?''', (command['agent_turn_id'],)).fetchone()[0]
        replay = case['client'].post(f"/v1/agent-turns/{command['agent_turn_id']}/execute")
        assert replay.status_code == 200, replay.text
        assert replay.json()['execution']['result']['plan_item_id'] == pid
        assert replay.json()['execution']['result']['diagnostic_prompt']['prompt']['prompt_id'] == prompt['prompt_id']
        with case['db'].connect() as c:
            assert c.execute('''SELECT result_json FROM agent_step JOIN agent_plan USING(plan_id)
                WHERE agent_turn_id=?''', (command['agent_turn_id'],)).fetchone()[0] == stored
        assert facts(case) == saved
    with case['db'].connect() as c:
        assert c.execute('SELECT context_manifest_json FROM agent_plan WHERE agent_turn_id=?',
            (original['agent_turn_id'],)).fetchone()[0] == original_manifest
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id=? AND event_type='started'",
            (pid,)).fetchone()[0] == (1 if change == 'none' else 0)
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE diagnostic_item_id=?',
            (prompt['diagnostic_item_id'],)).fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM pet_media_event WHERE operation='playback'").fetchone()[0] == 0
    saved = facts(case)
    assert execute(case, original).json()['execution'] == receipt
    assert facts(case) == saved
    assert before['daily_plan'] == saved['daily_plan']
