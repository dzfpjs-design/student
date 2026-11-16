"""Local SQLite transport mirrors, not model authorization or child outcomes."""
import copy
import json

import pytest

from home_study_agent.agent_runtime import (
    AgentPlanDecision, CapabilityRegistry, ObjectiveDraft, PlannedStep,
)
from home_study_agent.formative_evaluation_projection import build_formative_projection
from home_study_agent.database import Database
from test_formative_evaluation_projection import projection, writing
from test_paper_writing_projection import paper_projection, ocr


@pytest.fixture
def transport(projection):
    case, scope = projection
    with case[0].connect() as c:
        row = c.execute('''SELECT p.*,s.step_id FROM agent_plan p JOIN agent_step s USING(plan_id)
            WHERE p.objective_id=?''', (scope['objective_id'],)).fetchone()
        arguments = {'local_only': True, 'cloud_transmission_allowed': False}
        capability = CapabilityRegistry().require('diagnostic.start', 'study')
        decision = AgentPlanDecision(ObjectiveDraft('continue', 'learning', None, (), 'diagnose'),
            PlannedStep(capability, arguments, 'local transport', capability.expected_evidence), '', False, 1.0)
        plan = {**decision.to_dict(), **json.loads(row['plan_json'])}
        mirror = dict(plan_id=row['plan_id'], step_id=row['step_id'], objective_id=row['objective_id'],
            objective=plan['objective'], capability=plan['next_step'],
            requires_child_reply=plan['requires_child_reply'], after_execution=plan['after_execution'])
        action = {'type': 'start_diagnostic', 'local_only': True,
            'cloud_transmission_allowed': False, 'agent_runtime': mirror}
        c.execute('UPDATE agent_plan SET plan_json=? WHERE plan_id=?', (json.dumps(plan), row['plan_id']))
        c.execute('''UPDATE agent_step SET arguments_json=?,capability_version=?,execution_location=?,
            risk_level=?,expected_evidence_json=? WHERE step_id=?''',
            (json.dumps(arguments), capability.version, capability.execution_location, capability.risk_level,
             json.dumps(list(capability.expected_evidence)), row['step_id']))
        c.execute('UPDATE agent_turn SET action_json=? WHERE agent_turn_id=?',
            (json.dumps(action), row['agent_turn_id']))
        c.commit()
    return case, scope, dict(row), action, plan


def test_exact_runtime_transport_is_not_cloud_content_and_remains_persisted(transport):
    case, scope, row, action, plan = transport
    with case[0].connect() as c:
        result = build_formative_projection(c, **scope)
        assert result['status'] == 'ready_for_runtime_checks', result
        assert set(result['payload']) == {'question', 'response', 'rubric'}
        assert 'agent_runtime' not in json.dumps(result['payload'])
        assert json.loads(c.execute('SELECT action_json FROM agent_turn WHERE agent_turn_id=?',
            (row['agent_turn_id'],)).fetchone()[0]) == action
        assert json.loads(c.execute('SELECT plan_json FROM agent_plan WHERE plan_id=?',
            (row['plan_id'],)).fetchone()[0]) == json.loads(json.dumps(plan))
    with Database(case[0].path).connect() as c:
        assert build_formative_projection(c, **scope) == result


@pytest.mark.parametrize('fault', [
    'plan_id', 'step_id', 'objective_id', 'capability', 'arguments', 'capability_version',
    'execution_location', 'risk_level', 'expected_evidence', 'objective', 'requires_child_reply',
    'after_execution', 'extra_mirror', 'extra_capability', 'missing_plan_step', 'null_mirror',
    'step_only', 'plan_and_mirror', 'nested_arguments', 'nested_action', 'nested_objective',
])
def test_runtime_mirror_is_not_a_nested_local_bypass(transport, fault):
    case, scope, row, action, plan = transport
    action, plan = copy.deepcopy(action), copy.deepcopy(plan)
    mirror = action['agent_runtime']
    arguments = copy.deepcopy(plan['next_step']['arguments'])
    if fault in {'plan_id', 'step_id', 'objective_id'}:
        mirror[fault] = 'other-owner'
    elif fault in {'capability', 'capability_version', 'execution_location', 'risk_level', 'expected_evidence'}:
        mirror['capability'][fault] = 'changed'
    elif fault == 'arguments': mirror['capability']['arguments'] = {'track': 'private'}
    elif fault == 'objective': mirror['objective']['phase'] = 'other'
    elif fault == 'requires_child_reply': mirror[fault] = True
    elif fault == 'after_execution': mirror[fault] = 'continue'
    elif fault == 'extra_mirror': mirror['material'] = {'local_only': True}
    elif fault == 'extra_capability':
        mirror['capability']['material'] = {'local_only': True}
        plan['next_step']['material'] = {'local_only': True}
    elif fault == 'missing_plan_step': del plan['next_step']
    elif fault == 'null_mirror': action['agent_runtime'] = None
    elif fault == 'step_only': arguments['track'] = 'private'
    elif fault == 'plan_and_mirror':
        plan['next_step']['arguments']['track'] = 'private'
        mirror['capability']['arguments']['track'] = 'private'
    elif fault == 'nested_arguments':
        arguments['material'] = {'cloud_transmission_allowed': False, 'text': 'private'}
        plan['next_step']['arguments'] = copy.deepcopy(arguments)
        mirror['capability']['arguments'] = copy.deepcopy(arguments)
    elif fault == 'nested_action': action['material'] = {'local_only': True, 'text': 'private'}
    elif fault == 'nested_objective':
        plan['objective']['material'] = {'local_only': True}
        mirror['objective']['material'] = {'local_only': True}
    with case[0].connect() as c:
        c.execute('UPDATE agent_turn SET action_json=? WHERE agent_turn_id=?',
            (json.dumps(action), row['agent_turn_id']))
        c.execute('UPDATE agent_plan SET plan_json=? WHERE plan_id=?', (json.dumps(plan), row['plan_id']))
        c.execute('UPDATE agent_step SET arguments_json=? WHERE step_id=?', (json.dumps(arguments), row['step_id']))
        result = build_formative_projection(c, **scope)
        assert result['status'] == 'local_required' and result['payload'] is None, result


@pytest.fixture
def confirmed_transport(paper_projection):
    case, scope, packet, _ = paper_projection
    with case[0].connect() as c:
        row = c.execute('''SELECT p.*,s.step_id,t.action_json FROM agent_plan p
            JOIN agent_step s USING(plan_id) JOIN agent_turn t USING(agent_turn_id)
            WHERE s.capability_name='writing.confirm_original' ''').fetchone()
        action = json.loads(row['action_json'])
        arguments = {key: value for key, value in action.items() if key == 'writing_ocr_binding'}
        arguments.update(local_only=True, cloud_transmission_allowed=False)
        cap = CapabilityRegistry().require('writing.confirm_original', 'study')
        plan = AgentPlanDecision(ObjectiveDraft('none', None, None, (), 'execution'),
            PlannedStep(cap, arguments, 'local transport', cap.expected_evidence), '', False, 1.0).to_dict()
        action['agent_runtime'] = dict(plan_id=row['plan_id'], step_id=row['step_id'], objective_id=scope['objective_id'],
            objective=plan['objective'], capability=plan['next_step'], requires_child_reply=False, after_execution='stop')
        c.execute('UPDATE agent_plan SET objective_id=?,plan_json=? WHERE plan_id=?',
            (scope['objective_id'], json.dumps(plan), row['plan_id']))
        c.execute('''UPDATE agent_step SET arguments_json=?,capability_version=?,execution_location=?,risk_level=?,
            expected_evidence_json=? WHERE step_id=?''', (json.dumps(arguments), cap.version, cap.execution_location,
            cap.risk_level, json.dumps(list(cap.expected_evidence)), row['step_id']))
        c.execute('UPDATE agent_turn SET action_json=? WHERE agent_turn_id=?', (json.dumps(action), row['agent_turn_id']))
        c.commit()
    return case, scope, dict(row), action, plan, packet


@pytest.mark.parametrize('fault', [None, 'arguments_binding', 'unrelated_artifact', 'extra_nested_material',
    'extra_binding_key', 'wrong_authorizing_turn', 'revoked_authorization'])
def test_only_exact_original_authorization_transport_is_recognized(confirmed_transport, fault):
    case, scope, row, action, plan, packet = confirmed_transport
    action, plan = copy.deepcopy(action), copy.deepcopy(plan)
    arguments = plan['next_step']['arguments']
    if fault == 'arguments_binding': arguments['writing_ocr_binding']['artifact_id'] = 'other-page'
    elif fault == 'unrelated_artifact': arguments['artifact_id'] = 'other-page'
    elif fault == 'extra_nested_material': arguments['material'] = {'local_only': True, 'text': 'private'}
    elif fault == 'extra_binding_key': arguments['writing_ocr_binding']['material'] = {'local_only': True}
    elif fault == 'wrong_authorizing_turn':
        # All transport copies agree, but none can replace the original receipt.
        action['writing_ocr_binding']['voice_input_id'] = 'other-voice'
        arguments['writing_ocr_binding'] = copy.deepcopy(action['writing_ocr_binding'])
    action['agent_runtime']['capability'] = copy.deepcopy(plan['next_step'])
    if fault == 'revoked_authorization':
        from home_study_agent.writing_ocr_authorization import WritingOcrAuthorizationService
        WritingOcrAuthorizationService(case[0]).retract(case[1], confirmation_id=packet['confirmation_id'],
            binding_version=packet['binding_version'], client_action_id='retract-transport')
    with case[0].connect() as c:
        c.execute('UPDATE agent_turn SET action_json=? WHERE agent_turn_id=?', (json.dumps(action), row['agent_turn_id']))
        c.execute('UPDATE agent_plan SET plan_json=? WHERE plan_id=?', (json.dumps(plan), row['plan_id']))
        c.execute('UPDATE agent_step SET arguments_json=? WHERE step_id=?', (json.dumps(arguments), row['step_id']))
        result = build_formative_projection(c, **scope)
        if fault:
            assert result['status'] == 'local_required' and result['payload'] is None, result
        else:
            assert result['status'] == 'ready_for_runtime_checks', result
            assert set(result['payload']) == {'question', 'response', 'rubric'}
            assert packet['artifact_id'] not in json.dumps(result['payload'])
