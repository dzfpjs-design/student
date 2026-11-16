"""Local speech-role conflicts cannot be authorized by a model's answer label."""
import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.agent_orchestrator import AgentOrchestrator
from home_study_agent.agent_runtime import AgentRuntimeStore, InvalidAgentPlanError
from home_study_agent.exam_simulation import ExamSimulationService
from test_agent_continuation import Gateway
from test_diagnostic_prompt_http import environment, start
from test_pending_answer_arbitration_http import turn


def model_answer(gateway, text):
    gateway.output = {
        'objective': {'mode': 'none', 'phase': 'check'},
        'next_step': {'capability': 'exam.submit', 'arguments': {'answer': AgentOrchestrator._spoken_answer(text)}},
        'spoken_transition': '我核对这次回答。', 'requires_child_reply': False, 'confidence': .99,
        'utterance_binding': {'role': 'answer', 'quote': text},
    }


def open_exam(client, db):
    sid = start(client)
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
        c.commit()
    run = ExamSimulationService(db).start(sid, 'competition_math', '数学', 'practice', 2, 30, 'conflict-exam')
    assert run['status'] == 'active'
    return sid, run


def test_real_http_same_turn_withdrawals_never_score_and_new_final_answer_recovers(environment):
    settings, db = environment
    gateway = Gateway({})
    texts = [
        '我的答案是A，等一下，这不是我的答案，请不要提交。',
        '我选B，先别提交。',
        '答案是A，刚才只是在读题，不是我的答案。',
        '我的答案是A，不对，等我想一下。',
        '我选A，改成',
        '我的答案是A，取消这次提交。',
        '我选A，撤回刚才的答案。',
        '我的答案是A，我还没说完，先不要评分。',
    ]
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid, run = open_exam(client, db)
        binding = ExamSimulationService(db).arrival_binding(sid)
        for index, text in enumerate(texts):
            model_answer(gateway, text)
            heard = turn(client, sid, text, f'conflict-{index}')
            assert heard['intent'] == 'pending_answer_clarification', text
            assert heard['action']['pending_answer_candidate']['answer'] == text
            result = client.post(f"/v1/agent-turns/{heard['agent_turn_id']}/execute")
            assert result.status_code == 200, result.text
            assert result.json()['execution']['capability'] == 'conversation.respond'
            assert ExamSimulationService(db).arrival_binding(sid) == binding
            with db.connect() as c:
                assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 0
        original_turn_id = heard['agent_turn_id']
        assert not gateway.requests
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        assert client.post(f"/v1/exam-simulations/{run['run_id']}/control", json={'command': 'resume'}).status_code == 200
        replay = turn(client, sid, texts[-1], f'conflict-{len(texts)-1}')
        assert replay['agent_turn_id'] == original_turn_id
        confirmed = turn(client, sid, '刚才那句就是我的答案', 'ambiguous-confirmation', False)
        assert confirmed['action']['type'] != 'submit_exam_answer'
        model_answer(gateway, '我的答案是B')
        final = turn(client, sid, '我的答案是B', 'new-final-answer')
        assert final['action']['answer'] == 'B'
        executed = client.post(f"/v1/agent-turns/{final['agent_turn_id']}/execute")
        assert executed.status_code == 200, executed.text
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 1
            assert c.execute('SELECT transcript FROM agent_turn WHERE agent_turn_id=?', (original_turn_id,)).fetchone()[0] == texts[-1]


@pytest.mark.parametrize('text,answer', [
    ('我的答案是A', 'A'),
    ('我选A，不对，最终答案是B', 'B'),
    ('不是负数，是正数', '不是负数，是正数'),
    ('题干写着“不要提交”，我的答案是A', 'A'),
    ('题干说“不是我的答案”，我选B', 'B'),
    ('题目要求不要提交中间步骤，我选A', 'A'),
])
def test_normal_negation_quoted_source_and_finished_correction_remain_answers(environment, text, answer):
    settings, db = environment
    gateway = Gateway({})
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid, run = open_exam(client, db)
        model_answer(gateway, text)
        heard = turn(client, sid, text, 'real-final-answer')
        assert heard['action']['type'] == 'submit_exam_answer', heard['intent']
        assert heard['action']['answer'] == answer
        executed = client.post(f"/v1/agent-turns/{heard['agent_turn_id']}/execute")
        assert executed.status_code == 200, executed.text
        with db.connect() as c:
            row = c.execute('SELECT answer_text,authorized_turn_id FROM specialized_exam_item WHERE run_id=? AND position=1', (run['run_id'],)).fetchone()
            assert row['answer_text'] == answer
            assert row['authorized_turn_id'] == heard['agent_turn_id']


@pytest.mark.parametrize('text,answer', [
    ('我的答案是“不要提交”', '“不要提交”'),
    ('题目写着“假设成立，我的答案是A”', '题目写着“假设成立，我的答案是A”'),
    ('最终答案是12', '12'),
])
def test_normalization_never_extracts_a_final_answer_marker_inside_a_quote(text, answer):
    assert not AgentRuntimeStore.local_answer_requires_clarification(text)
    assert AgentOrchestrator._spoken_answer(text) == answer


@pytest.mark.parametrize('capability', ['diagnostic.submit', 'pet_vocabulary.submit', 'exam.submit', 'mistake.dialogue'])
@pytest.mark.parametrize('status', ['direct', 'confirmed'])
def test_all_write_gates_reject_conflicting_original_even_with_matching_arguments(environment, monkeypatch, capability, status):
    _, db = environment
    db.initialize()
    original = '我的答案是A，先别提交。'
    monkeypatch.setattr(AgentRuntimeStore, 'resolve_local_answer_submission', staticmethod(
        lambda *args: {'status': status, 'answer': original, 'child_id': 'child', 'session_id': 'session', 'binding': None}))
    with db.connect() as connection, pytest.raises(InvalidAgentPlanError, match='ANSWER_INTENT_CLARIFICATION_REQUIRED'):
        AgentRuntimeStore.validate_local_answer_write(connection, 'turn', capability,
            child_id='child', session_id='session', binding=None, answer=original)
