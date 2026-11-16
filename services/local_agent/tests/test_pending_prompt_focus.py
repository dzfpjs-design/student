from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.agent_runtime import AgentRuntimeStore
from home_study_agent.database import Database
from home_study_agent.diagnostic_prompt import DiagnosticPromptService
from test_pet_vocabulary_prompt_integration import environment, flow, Planner, next_prompt, turn


def test_selected_real_prompt_survives_restart_without_cancelling_other_question(environment):
    settings, db, sid = environment
    planner = Planner()
    with TestClient(create_app(settings, model_gateway=planner)) as client:
        vocabulary = next_prompt(client, sid)
        diagnostic = DiagnosticPromptService(db).prepare(sid, preferred_item_id='diag-p4-arithmetic-closed-1',
                                                        client_action_id='focus-diagnostic')['prompt']
        assert AgentRuntimeStore(db).pending_child_reply(sid)['capability_name'] == 'input.choose_prompt'
        planner.select('pet_vocabulary.start', '先回答词汇卡', arguments={})
        selected = turn(client, sid, '先回答词汇卡', 'focus-choose-vocabulary')
        receipt = {'capability': 'pet_vocabulary.start', 'execution_location': 'client_device_bridge',
                   'action_type': 'start_pet_vocabulary_review', 'status': 'completed',
                   'completed_at': '2026-09-07T00:00:00Z', 'output': {'card': vocabulary}}
        response = client.put(f"/v1/agent-turns/{selected['agent_turn_id']}/outcome", json={
            'final_response': '请回答这张词卡。', 'tool_status': 'completed',
            'result': {'execution_receipt': receipt, 'client_bridge_receipt': receipt}})
        assert response.status_code == 200, response.text
    pending = AgentRuntimeStore(Database(db.path)).pending_child_reply(sid)
    assert pending['capability_name'] == 'pet_vocabulary.start'
    assert pending['result']['prompt_session_id'] == vocabulary['prompt_session_id']
    assert DiagnosticPromptService(db).read(sid)['prompt'] == diagnostic
    planner.select('pet_vocabulary.submit', 'unrelated', role='answer')
    with TestClient(create_app(settings, model_gateway=planner)) as client:
        answering = turn(client, sid, 'unrelated', 'focus-answer-vocabulary')
        assert answering['action']['type'] == 'submit_pet_vocabulary_answer'
        assert answering['action']['vocabulary_prompt_id'] == vocabulary['prompt_session_id']


def test_forged_receipt_does_not_select_a_different_prompt(environment):
    settings, db, sid = environment
    planner = Planner()
    with TestClient(create_app(settings, model_gateway=planner)) as client:
        vocabulary = next_prompt(client, sid)
        DiagnosticPromptService(db).prepare(sid, preferred_item_id='diag-p4-arithmetic-closed-1',
                                           client_action_id='other-prompt')
        planner.select('pet_vocabulary.start', '先回答词汇卡', arguments={})
        selected = turn(client, sid, '先回答词汇卡', 'forged-focus-selection')
        with db.connect() as connection:
            AgentRuntimeStore(db).complete_turn_execution(connection, selected['agent_turn_id'], True, None,
                {'execution_receipt': {'capability': 'pet_vocabulary.start', 'execution_location': 'client_device_bridge',
                 'action_type': 'start_pet_vocabulary_review', 'status': 'completed',
                 'completed_at': '2026-09-07T00:00:00Z', 'output': {'card': {**vocabulary, 'card_version': 'forged'}}}})
            connection.commit()
        assert AgentRuntimeStore(db).pending_child_reply(sid)['capability_name'] == 'input.choose_prompt'
