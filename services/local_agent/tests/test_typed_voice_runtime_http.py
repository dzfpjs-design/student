"""All displayed question families survive capture, dispatch and restart."""
import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.agent_runtime import AgentRuntimeStore
from home_study_agent.exam_simulation import ExamSimulationService
from home_study_agent.pet_vocabulary import PetVocabularyService
from home_study_agent.mistake_dialogue import MistakeDialogueService
from test_agent_continuation import Gateway
from test_diagnostic_prompt_http import environment, CHILD
from test_pet_vocabulary_prompt_integration import environment as vocabulary_environment, flow, next_prompt
from test_voice_runtime_binding import capture, interpret
from test_answer_same_turn_correction import open_exam
from test_mistake_pending_answer_http import waiting
from test_mistake_voice_journey import settings as mistake_settings, focus


@pytest.mark.parametrize('change', [False, True])
def test_exam_typed_asr_real_dispatch_and_source_replacement(environment, monkeypatch, change):
    settings, db = environment
    gateway = Gateway({})
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid, run = open_exam(client, db)
        reference = {'kind': 'exam', 'binding': run['answer_binding']}
        assert reference['binding'] == ExamSimulationService(db).arrival_binding(sid)
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            before = c.total_changes
            assert AgentRuntimeStore.validate_captured_question(c, sid, CHILD, reference) == reference
            assert c.in_transaction and c.total_changes == before
        voice = capture(client, settings, monkeypatch, sid, '我的答案是A', reference)
        if change:
            service = ExamSimulationService(db)
            service.cancel(run['run_id'])
            service.start(sid, 'competition_math', '数学', 'practice', 2, 30, 'replacement-exam')
        heard = interpret(client, sid, '我的答案是A', 'typed-exam-answer', voice)
        if change:
            assert heard['action']['type'] == 'none'
            assert 'pending_answer_candidate' not in heard['action']
        else:
            assert heard['action']['type'] == 'submit_exam_answer'
            assert heard['action']['exam_input_binding'] == reference['binding']
            assert heard['action']['source_voice_input_id'] == voice
        executed = client.post(f"/v1/agent-turns/{heard['agent_turn_id']}/execute")
        assert executed.status_code == 200, executed.text
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == int(not change)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        replay = interpret(client, sid, '我的答案是A', 'typed-exam-answer', voice)
        assert replay['agent_turn_id'] == heard['agent_turn_id']
        result = client.post(f"/v1/agent-turns/{replay['agent_turn_id']}/execute")
        assert result.status_code == 200, result.text
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == int(not change)
    assert not gateway.requests


@pytest.mark.parametrize('change', [False, True])
def test_vocabulary_typed_asr_and_bound_score(vocabulary_environment, monkeypatch, change):
    settings, db, sid = vocabulary_environment
    gateway = Gateway({})
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        card = next_prompt(client, sid)
        binding = {key: card[key] for key in ('prompt_session_id', 'card_version')}
        reference = {'kind': 'vocabulary', 'binding': binding}
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            before = c.total_changes
            assert AgentRuntimeStore.validate_captured_question(c, sid, CHILD, reference) == reference
            assert c.total_changes == before
        voice = capture(client, settings, monkeypatch, sid, 'library', reference)
        if change:
            PetVocabularyService(db).cancel_prompt(sid, **binding)
            next_prompt(client, sid)
        heard = interpret(client, sid, 'library', 'typed-vocabulary-answer', voice)
        if change:
            assert heard['action']['type'] != 'submit_pet_vocabulary_answer'
        else:
            assert heard['action']['type'] == 'submit_pet_vocabulary_answer'
            assert heard['action']['source_voice_input_id'] == voice
            result = client.post(f'/v1/sessions/{sid}/pet-vocabulary/prompt/answer', json={
                **binding, 'answer': 'library', 'client_action_id': 'typed-vocabulary-score',
                'authorized_turn_id': heard['agent_turn_id']})
            assert result.status_code == 200, result.text
            assert result.json()['status'] == 'answered'
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM pet_vocabulary_attempt').fetchone()[0] == int(not change)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        assert interpret(client, sid, 'library', 'typed-vocabulary-answer', voice)['agent_turn_id'] == heard['agent_turn_id']
        if not change:
            replay = client.post(f'/v1/sessions/{sid}/pet-vocabulary/prompt/answer', json={
                **binding, 'answer': 'library', 'client_action_id': 'typed-vocabulary-score',
                'authorized_turn_id': heard['agent_turn_id']})
            assert replay.status_code == 200, replay.text
            assert replay.json()['attempt'] == result.json()['attempt']
    assert not gateway.requests


@pytest.mark.parametrize('change', [False, True])
def test_mistake_displayed_binding_real_asr_dispatch_preserves_hint_version(tmp_path, monkeypatch, change):
    settings = mistake_settings(tmp_path)
    gateway = Gateway({}); gateway.configured = False
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid, db = waiting(client)
        current = focus(client, sid)
        reference = {'kind': 'mistake', 'binding': current['answer_binding']}
        assert reference['binding']['item_id'] == current['state']['next_activity']['item']['diagnostic_item_id']
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            before = c.total_changes
            assert AgentRuntimeStore.validate_captured_question(c, sid, CHILD, reference) == reference
            assert c.total_changes == before
        voice = capture(client, settings, monkeypatch, sid, '我选A', reference)
        if change:
            MistakeDialogueService(db).respond(sid, '给我一点提示', 'hint-after-capture')
            assert focus(client, sid)['answer_binding'] != reference['binding']
        heard = interpret(client, sid, '我选A', 'typed-mistake-answer', voice)
        if change:
            assert heard['action']['type'] == 'none'
            assert 'pending_answer_candidate' not in heard['action']
        else:
            assert heard['action']['type'] == 'continue_mistake_dialogue'
            assert heard['action']['source_voice_input_id'] == voice
        executed = client.post(f"/v1/agent-turns/{heard['agent_turn_id']}/execute")
        assert executed.status_code == 200, executed.text
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM mistake_attempt').fetchone()[0] == int(not change)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        replay = interpret(client, sid, '我选A', 'typed-mistake-answer', voice)
        assert replay['agent_turn_id'] == heard['agent_turn_id']
        assert client.post(f"/v1/agent-turns/{replay['agent_turn_id']}/execute").status_code == 200
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM mistake_attempt').fetchone()[0] == int(not change)
    assert not gateway.requests
