"""Full local speech, not a planner's transformed text, authorizes grading."""
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from home_study_agent.agent_orchestrator import AgentOrchestrator
from home_study_agent.agent_runtime import (
    AgentRuntimeStore, CapabilityRegistry, InvalidAgentPlanError, parse_agent_plan_output,
)
from home_study_agent.app import create_app
from home_study_agent.exam_simulation import ExamSimulationService
from test_agent_continuation import Gateway
from test_diagnostic_prompt_http import environment, start
from test_pending_answer_arbitration_http import turn


@pytest.mark.parametrize('correction', [False, True])
def test_long_answer_is_preserved_locally_without_scoring_or_export(environment, correction):
    settings, db = environment
    original = '我的答案是' + '先算括号再乘外面的数，' * 115
    if correction:
        original += '等一下，刚才只是我在读题，不是我的答案，请不要提交。'
        assert len(original) == 1296
    visible = original[:1200]
    gateway = Gateway({
        'objective': {'mode': 'none', 'phase': 'check'},
        'next_step': {'capability': 'exam.submit', 'arguments': {'answer': AgentOrchestrator._spoken_answer(visible)}},
        'spoken_transition': '我核对这次回答。', 'requires_child_reply': False, 'confidence': .99,
        'utterance_binding': {'role': 'answer', 'quote': visible},
    })
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid = start(client)
        with db.connect() as c:
            c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
            c.commit()
        service = ExamSimulationService(db)
        run = service.start(sid, 'competition_math', '数学', 'practice', 2, 30, 'long-exam')
        binding = service.arrival_binding(sid)
        heard = turn(client, sid, original, 'long-answer')
        assert heard['intent'] == 'pending_answer_clarification'
        assert heard['action']['pending_answer_candidate']['answer'] == original
        executed = client.post(f"/v1/agent-turns/{heard['agent_turn_id']}/execute")
        assert executed.status_code == 200, executed.text
        assert executed.json()['execution']['capability'] == 'conversation.respond'
        assert service.arrival_binding(sid) == binding
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 0
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        replay = turn(client, sid, original, 'long-answer')
        assert replay['action']['pending_answer_candidate']['answer'] == original
        if not correction:
            assert client.post(f"/v1/exam-simulations/{run['run_id']}/control", json={'command': 'resume'}).status_code == 200
            confirmed = turn(client, sid, '刚才那句就是我的答案', 'confirm-long-answer')
            assert confirmed['action']['answer'] == original
            executed = client.post(f"/v1/agent-turns/{confirmed['agent_turn_id']}/execute")
            assert executed.status_code == 200, executed.text
            with db.connect() as c:
                saved = c.execute('SELECT answer_text FROM specialized_exam_item WHERE run_id=? AND position=?',
                                  (run['run_id'], binding['position'])).fetchone()
                assert saved['answer_text'] == original
                assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 1
    assert not gateway.requests


@pytest.mark.parametrize('private', [False, True])
def test_actual_planner_normalization_and_private_answer_remain_distinct(environment, private):
    settings, db = environment
    gateway = Gateway({})
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid = start(client)
        with db.connect() as c:
            c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
            nickname = c.execute('SELECT nickname FROM child_profile WHERE child_id=(SELECT child_id FROM daily_session WHERE session_id=?)', (sid,)).fetchone()[0]
            c.commit()
        ExamSimulationService(db).start(sid, 'competition_math', '数学', 'practice', 2, 30, 'normal-exam')
        original = '我的答案是' + (nickname + '先算出每部分再相加' if private else 'A')
        gateway.output = {
            'objective': {'mode': 'none', 'phase': 'check'},
            'next_step': {'capability': 'exam.submit', 'arguments': {'answer': 'A'}},
            'spoken_transition': '我核对这次回答。', 'requires_child_reply': False, 'confidence': .99,
            'utterance_binding': {'role': 'answer', 'quote': original},
        }
        heard = turn(client, sid, original, 'ordinary-answer')
        if private:
            assert heard['intent'] == 'pending_answer_clarification'
            assert heard['action']['pending_answer_candidate']['answer'] == original
            assert not gateway.requests
        else:
            assert heard['action']['answer'] == 'A'
            response = client.post(f"/v1/agent-turns/{heard['agent_turn_id']}/execute")
            assert response.status_code == 200, response.text
            assert len(gateway.requests) == 1
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == (0 if private else 1)


@pytest.mark.parametrize('capability,field,owner', [
    ('diagnostic.submit', 'answer', 'diagnostic.start'),
    ('pet_vocabulary.submit', 'answer', 'pet_vocabulary.start'),
    ('exam.submit', 'answer', 'exam.start'),
    ('learning.evaluate_response', 'response', 'learning.request_response'),
    ('mistake.dialogue', 'utterance', 'mistake.dialogue'),
])
@pytest.mark.parametrize('transform', ['truncate', 'privacy'])
def test_cloud_transformed_text_cannot_authorize_any_grading(capability, field, owner, transform):
    original = '答案是' + '算出每部分再相加' * 180 if transform == 'truncate' else '答案是小禾先算出每部分再相加'
    visible = original[:1200] if transform == 'truncate' else original.replace('小禾', '[孩子]')
    arguments = {field: visible}
    if capability == 'learning.evaluate_response':
        arguments.update(subject='数学')
    decision = parse_agent_plan_output(json.dumps({
        'objective': {'mode': 'none', 'phase': 'check'},
        'next_step': {'capability': capability, 'arguments': arguments},
        'spoken_transition': '核对回答。', 'requires_child_reply': False, 'confidence': 1,
    }), 'study', CapabilityRegistry())
    with pytest.raises(InvalidAgentPlanError, match='ANSWER_FULL_LOCAL_UTTERANCE_REQUIRED'):
        AgentOrchestrator._validate_cloud_utterance_binding(
            json.dumps({'utterance_binding': {'role': 'answer', 'quote': visible}}),
            visible, decision, {'capability_name': owner}, original_transcript=original)


@pytest.mark.parametrize('capability', ['diagnostic.submit', 'pet_vocabulary.submit', 'exam.submit',
                                      'learning.evaluate_response', 'mistake.dialogue'])
def test_write_gate_does_not_trust_model_argument_as_original(monkeypatch, capability):
    row_source = sqlite3.connect(':memory:')
    row_source.row_factory = sqlite3.Row
    persisted = row_source.execute('SELECT ? AS action_json',
        (json.dumps({'answer': 'B', 'response': 'B', 'utterance': 'B'}),)).fetchone()
    row_source.close()

    class Connection:
        def execute(self, *args):
            # An old persisted model argument must not be an authorization source.
            return self

        def fetchone(self):
            return persisted

    monkeypatch.setattr(AgentRuntimeStore, 'resolve_local_answer_submission', staticmethod(
        lambda *args: {'status': 'direct', 'answer': '我的答案是A', 'child_id': 'child',
                       'session_id': 'session', 'binding': None}))
    # This case authorizes direct speech, not a separately confirmed paper original.
    monkeypatch.setattr(AgentRuntimeStore, 'confirmed_writing_submission', staticmethod(
        lambda *args, **kwargs: None))
    with pytest.raises(InvalidAgentPlanError, match='ANSWER_SOURCE_BINDING_CHANGED'):
        AgentRuntimeStore.validate_local_answer_write(Connection(), 'turn', capability,
            child_id='child', session_id='session', binding=None, answer='B')
    AgentRuntimeStore.validate_local_answer_write(Connection(), 'turn', capability,
        child_id='child', session_id='session', binding=None,
        answer='我的答案是A' if capability == 'mistake.dialogue' else 'A')
