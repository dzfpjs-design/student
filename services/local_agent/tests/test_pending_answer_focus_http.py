"""Saved answer candidates belong to the child's actual selected prompt."""
from fastapi.testclient import TestClient
import pytest

from home_study_agent.app import create_app
from home_study_agent.agent_runtime import AgentRuntimeStore
from home_study_agent.diagnostic_prompt import DiagnosticPromptService
from home_study_agent.exam_simulation import ExamSimulationService
from test_pet_vocabulary_prompt_integration import environment, flow, Planner, next_prompt, turn
from test_pending_answer_arbitration_http import assert_content_free_requests


@pytest.mark.parametrize('select_diagnostic,with_exam', [(False, False), (True, False), (True, True)])
def test_candidate_never_guesses_between_live_prompts_and_keeps_selected_original_after_restart(environment, select_diagnostic, with_exam):
    settings, db, sid = environment
    planner = Planner()
    original = '我把括号里面的数先合起来，再算外面'
    with TestClient(create_app(settings, model_gateway=planner)) as client:
        vocabulary = next_prompt(client, sid)
        diagnostic = DiagnosticPromptService(db).prepare(
            sid, preferred_item_id='diag-p4-arithmetic-closed-1', client_action_id='candidate-focus-diagnostic')['prompt']
        if with_exam:
            exam = ExamSimulationService(db).start(sid, 'competition_math', '数学', 'practice', 2, 30, 'competing-exam')
            assert exam['status'] == 'active'
        if select_diagnostic:
            selected = turn(client, sid, '先回答诊断题', 'choose-diagnostic')
            assert selected['action']['type'] == 'replay_diagnostic'
            receipt = {'capability': 'diagnostic.replay', 'execution_location': 'client_device_bridge',
                       'action_type': 'replay_diagnostic', 'status': 'completed',
                       'completed_at': '2026-09-07T00:00:00Z', 'output': {'prompt': diagnostic}}
            completed = client.put(f"/v1/agent-turns/{selected['agent_turn_id']}/outcome", json={
                'final_response': '回到这道诊断题。', 'tool_status': 'completed',
                'result': {'execution_receipt': receipt, 'client_bridge_receipt': receipt}})
            assert completed.status_code == 200, completed.text
            assert AgentRuntimeStore(db).pending_child_reply(sid)['capability_name'] == 'diagnostic.start'
        if with_exam:
            assert ExamSimulationService(db).active_for_session(sid)['status'] == 'active'
            short = turn(client, sid, '12', 'active-exam-selected-short-answer')
            assert short['action']['type'] == 'submit_diagnostic_answer'
            assert short['action']['diagnostic_prompt_id'] == diagnostic['prompt_id']
        candidate = turn(client, sid, original, 'selected-original-answer')
        assert candidate['action']['type'] == 'none'
        if select_diagnostic:
            assert candidate['action']['pending_answer_candidate']['target']['action'] == 'submit_diagnostic_answer'
            assert candidate['action']['pending_answer_candidate']['target']['binding'] == {
                key: diagnostic[key] for key in ('prompt_id', 'item_version')}
        else:
            assert candidate['action']['source'] == 'pending_question_conflict'
            assert 'pending_answer_candidate' not in candidate['action']
        executed = client.post(f"/v1/agent-turns/{candidate['agent_turn_id']}/execute")
        assert executed.status_code == 200, executed.text
    with TestClient(create_app(settings, model_gateway=planner)) as client:
        if with_exam:
            exam = ExamSimulationService(db).active_for_session(sid)
            assert exam['status'] == 'paused' and exam['answered_items'] == 0
        confirmation = turn(client, sid, '刚才那句就是我的答案', 'selected-answer-confirmation')
        if select_diagnostic:
            action = confirmation['action']
            assert action['type'] == 'submit_diagnostic_answer'
            assert action['answer'] == original
            assert action['answer_source_turn_id'] == candidate['agent_turn_id']
            assert action['diagnostic_prompt_id'] == diagnostic['prompt_id']
            with db.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                assert AgentRuntimeStore.resolve_local_answer_submission(
                    c, confirmation['agent_turn_id'], 'diagnostic.submit')['answer'] == original
                c.rollback()
            short = turn(client, sid, '12', 'selected-short-answer')
            assert short['action']['type'] == 'submit_diagnostic_answer'
            assert short['action']['diagnostic_prompt_id'] == diagnostic['prompt_id']
            assert short['action']['answer'] == '12'
        else:
            assert confirmation['action']['type'] == 'none'
        assert next_prompt(client, sid)['prompt_session_id'] == vocabulary['prompt_session_id']
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
    assert_content_free_requests(planner.requests, [original],
        [sid, diagnostic['prompt_id'], diagnostic['item']['prompt'], vocabulary['prompt_session_id']])
