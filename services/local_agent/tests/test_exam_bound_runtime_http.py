import json

from fastapi.testclient import TestClient
import pytest

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.exam_simulation import ExamSimulationService
from home_study_agent.agent_runtime import AgentRuntimeStore
from home_study_agent.repository import StudyRepository
from home_study_agent.diagnostic_prompt import DiagnosticPromptService
from test_exam_simulation import _database
from test_pet_vocabulary_prompt_integration import Planner, turn


@pytest.fixture
def environment(tmp_path, monkeypatch):
    db, sid = _database(tmp_path / 'exam-http.db')
    monkeypatch.setenv('HSC_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('HSC_DATABASE_PATH', str(db.path))
    settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False, agent_runtime_enabled=True)
    return settings, db, sid


def start(service, sid, key='exam-start-integration'):
    return service.start(sid, 'competition_math', '数学', 'practice', 2, 30, key)


def test_cloud_waiting_answer_keeps_arrival_slot_and_never_grades_replacement(environment):
    settings, db, sid = environment
    planner = Planner()
    service = ExamSimulationService(db)
    with TestClient(create_app(settings, model_gateway=planner)) as client:
        original = start(service, sid)
        arrived = service.arrival_binding(sid)
        assert AgentRuntimeStore(db).pending_child_reply(sid)['capability_name'] == 'exam.start'
        def replace():
            service.cancel(original['run_id'])
            start(service, sid, 'new-exam-while-thinking')
        planner.before_reply = replace
        planner.select('exam.submit', '我认为是十三', role='answer', arguments={'answer': '我认为是十三'})
        planned = turn(client, sid, '我认为是十三', 'late-exam-http-turn')
        assert planned['action']['exam_input_binding'] == arrived
        result = client.post(f"/v1/agent-turns/{planned['agent_turn_id']}/execute")
        assert result.status_code == 409, result.text
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
        assert service.arrival_binding(sid)['run_id'] != original['run_id']


def test_server_executor_saves_original_words_and_persists_next_question_focus(environment):
    settings, db, sid = environment
    planner = Planner()
    with TestClient(create_app(settings, model_gateway=planner)) as client:
        service = ExamSimulationService(db)
        start(service, sid)
        planner.select('exam.submit', '我的答案是十三', role='answer', arguments={'answer': '我的答案是十三'})
        planned = turn(client, sid, '我的答案是十三', 'real-exam-bound-turn')
        result = client.post(f"/v1/agent-turns/{planned['agent_turn_id']}/execute")
        assert result.status_code == 200, result.text
        with db.connect() as c:
            saved = c.execute('SELECT submitted_answer FROM diagnostic_attempt').fetchone()
            assert saved[0] == '我的答案是十三'
            plan = json.loads(c.execute('SELECT plan_json FROM agent_plan WHERE agent_turn_id=?',
                                        (planned['agent_turn_id'],)).fetchone()[0])
            assert plan['pending_input_binding']['result']['position'] == 2
        assert AgentRuntimeStore(db).pending_child_reply(sid)['result']['position'] == 2


def test_model_rewritten_answer_is_rejected_before_any_execution(environment):
    settings, db, sid = environment
    planner = Planner()
    with TestClient(create_app(settings, model_gateway=planner)) as client:
        service = ExamSimulationService(db)
        start(service, sid)
        binding = service.arrival_binding(sid)
        planner.select('exam.submit', '我的答案是十三', role='answer', arguments={'answer': 'fourteen'})
        planned = turn(client, sid, '我的答案是十三', 'reject-rewritten-answer')
        assert planned['action']['type'] == 'none'
        assert service.arrival_binding(sid) == binding
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 0
            assert c.execute('SELECT count(*) FROM agent_plan WHERE agent_turn_id=?', (planned['agent_turn_id'],)).fetchone()[0] == 0


def test_bound_exam_http_replay_and_cross_child_rejection(environment):
    settings, db, sid = environment
    with TestClient(create_app(settings)) as client:
        service = ExamSimulationService(db)
        start(service, sid)
        path = f'/v1/sessions/{sid}/exam-simulations'
        binding = client.get(path + '/answer-binding').json()['binding']
        payload = {**binding, 'answer': 'I am unsure', 'client_action_id': 'bound-exam-http-answer'}
        first = client.post(path + '/bound-answer', json=payload)
        assert first.status_code == 200, first.text
        second = client.post(path + '/bound-answer', json=payload)
        assert second.status_code == 200, second.text
        assert first.json()['attempt'] == second.json()['attempt']
        assert second.json()['cloud_transmission_allowed'] is False
        changed = client.post(path + '/bound-answer', json={**payload, 'answer': 'another'})
        assert changed.status_code == 409
        with db.connect() as c:
            c.execute("UPDATE daily_session SET status='completed',state='completed' WHERE session_id=?", (sid,))
            c.commit()
        other = StudyRepository(db).start_session('child-junior-3', 'other-exam-session')['session_id']
        cross = client.post(f'/v1/sessions/{other}/exam-simulations/bound-answer', json=payload)
        assert cross.status_code == 409
        assert client.post(path + '/bound-answer', json={**payload, 'position': True}).status_code == 422


def test_selecting_exam_persists_focus_without_consuming_another_pending_diagnostic(environment):
    settings, db, sid = environment
    planner = Planner()
    with TestClient(create_app(settings, model_gateway=planner)) as client:
        service = ExamSimulationService(db)
        start(service, sid)
        prompt = DiagnosticPromptService(db).prepare(sid, preferred_item_id='diag-p4-arithmetic-closed-1',
                                                     client_action_id='parallel-exam-diagnostic')['prompt']
        assert AgentRuntimeStore(db).pending_child_reply(sid)['capability_name'] == 'input.choose_prompt'
        planner.select('exam.resume', '先继续模拟卷', arguments={})
        selected = turn(client, sid, '先继续模拟卷', 'select-exam-owner')
        response = client.post(f"/v1/agent-turns/{selected['agent_turn_id']}/execute")
        assert response.status_code == 200, response.text
        assert AgentRuntimeStore(db).pending_child_reply(sid)['capability_name'] == 'exam.start'
        assert DiagnosticPromptService(db).read(sid)['prompt'] == prompt
