"""Synthetic ASR -> ordinary agent HTTP -> real control dispatch, no cloud IO."""
import json

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.model_gateway import ModelResult
from home_study_agent.agent_runtime import InvalidAgentPlanError, AgentRuntimeStore
from home_study_agent.exam_simulation import ExamSimulationService
from home_study_agent.repository import StudyRepository
from test_diagnostic_prompt_http import environment
from test_answer_same_turn_correction import open_exam
from test_voice_runtime_binding import capture, interpret


class ControlGateway:
    configured = True

    def __init__(self, quote, control='exam.pause', hook=None, role='control'):
        self.quote, self.control, self.hook, self.role = quote, control, hook, role
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        assert request.request_type == 'voice_interaction'
        if self.hook:
            self.hook()
        selector = ({'selector': {'subject': None, 'task_kind': None, 'relative_order': None}}
                    if self.role == 'control' and self.control == 'task.resume' else {})
        return ModelResult(content=json.dumps({'role': self.role, 'control': self.control,
            'quote': self.quote, 'confidence': .95, **selector}), provider='test', model='controlled-intent', latency_ms=1)


def test_natural_exam_pause_during_bound_question_uses_only_current_words(environment, monkeypatch):
    settings, db = environment
    quote = '这张卷子先搁一搁，回来咱们再往下做'
    gateway = ControlGateway(quote)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid, run = open_exam(client, db)
        binding = {'kind': 'exam', 'binding': run['answer_binding']}
        voice = capture(client, settings, monkeypatch, sid, quote, binding)
        heard = interpret(client, sid, quote, 'natural-pause-exam', voice)
        assert heard['action']['type'] == 'pause_exam_simulation'
        assert heard['action']['exam_run_id'] == run['run_id']
        assert heard['action']['voice_input_binding']['question_reference'] == binding
        result = client.post(f"/v1/agent-turns/{heard['agent_turn_id']}/execute")
        assert result.status_code == 200, result.text
        with db.connect() as c:
            assert c.execute('SELECT status FROM specialized_exam_run WHERE run_id=?', (run['run_id'],)).fetchone()[0] == 'paused'
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 0
        assert len(gateway.requests) == 1
        request = gateway.requests[0]
        payload = json.loads(request.messages[-1]['content'])
        assert set(payload) == {'transcript', 'state'}
        assert payload['transcript'] == quote
        assert all(type(value) is bool for value in payload['state'].values())
        serialized = json.dumps(request.messages, ensure_ascii=False)
        for forbidden in [sid, voice, run['run_id'], *[str(v) for v in run['answer_binding'].values()], run['current_item']['prompt']]:
            if len(forbidden) > 3:
                assert forbidden not in serialized
        assert request.evidence_refs == ()
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        replay = interpret(client, sid, quote, 'natural-pause-exam', voice)
        assert replay['agent_turn_id'] == heard['agent_turn_id']
        assert client.post(f"/v1/agent-turns/{replay['agent_turn_id']}/execute").status_code == 200
    assert len(gateway.requests) == 1


@pytest.mark.parametrize('quote,control,role,action', [
    ('先让看我的那个镜头休息一下，等会儿再看我', 'observation.pause', 'control', 'pause_observation'),
    ('先让台灯休息一下，等会儿再亮', None, 'unknown', 'none'),
])
def test_bound_general_intent_is_not_captured_by_local_break_keyword(environment, monkeypatch, quote, control, role, action):
    settings, db = environment
    gateway = ControlGateway(quote, control, role=role)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid, run = open_exam(client, db)
        voice = capture(client, settings, monkeypatch, sid, quote, {'kind': 'exam', 'binding': run['answer_binding']})
        heard = interpret(client, sid, quote, 'semantic-before-keyword', voice)
        assert heard['action']['type'] == action
        assert len(gateway.requests) == 1
        assert heard['requires_control_admission'] is (action == 'pause_observation')
        if action == 'none':
            result = client.post(f"/v1/agent-turns/{heard['agent_turn_id']}/execute")
            assert result.status_code == 200, result.text
            assert result.json()['execution']['capability'] == 'conversation.respond'
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 0
            assert c.execute('SELECT state FROM daily_session WHERE session_id=?', (sid,)).fetchone()[0] == 'study'


@pytest.mark.parametrize('command', ['pause', 'resume', 'cancel'])
def test_exam_control_rejects_unadmitted_turn_in_write_transaction(environment, monkeypatch, command):
    settings, db = environment
    quote = '这张卷子先搁一搁，回来咱们再往下做'
    with TestClient(create_app(settings, model_gateway=ControlGateway(quote))) as client:
        sid, run = open_exam(client, db)
        voice = capture(client, settings, monkeypatch, sid, quote, {'kind': 'exam', 'binding': run['answer_binding']})
        heard = interpret(client, sid, quote, 'unadmitted-control', voice)
        with pytest.raises(InvalidAgentPlanError, match='ANSWER_CONTROL_SCOPE_CHANGED'):
            getattr(ExamSimulationService(db), command)(run['run_id'], authorized_turn_id=heard['agent_turn_id'])
        with db.connect() as c:
            assert c.execute('SELECT status FROM specialized_exam_run WHERE run_id=?', (run['run_id'],)).fetchone()[0] == 'active'


def test_client_control_exposes_admission_flag_not_manifest(environment, monkeypatch):
    settings, db = environment
    quote = '先让桌上的扫描歇一歇，我想安静想一会儿'
    with TestClient(create_app(settings, model_gateway=ControlGateway(quote, 'document.pause'))) as client:
        sid, run = open_exam(client, db)
        voice = capture(client, settings, monkeypatch, sid, quote, {'kind': 'exam', 'binding': run['answer_binding']})
        heard = interpret(client, sid, quote, 'natural-document-pause', voice)
        assert heard['action']['type'] == 'control_document_monitor'
        assert heard['requires_control_admission'] is True
        assert 'context_manifest' not in heard and 'context_manifest_json' not in heard


@pytest.mark.parametrize('quote', [
    '嗯', '这张卷子先搁一搁，不对，先别执行',
    '题目是“请暂停然后继续”，我是在读题',
])
def test_acknowledgement_and_revocation_never_score_or_control(environment, monkeypatch, quote):
    settings, db = environment
    gateway = ControlGateway(quote)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid, run = open_exam(client, db)
        voice = capture(client, settings, monkeypatch, sid, quote, {'kind': 'exam', 'binding': run['answer_binding']})
        heard = interpret(client, sid, quote, 'non-command', voice)
        assert heard['action']['type'] == 'none'
        executed = client.post(f"/v1/agent-turns/{heard['agent_turn_id']}/execute")
        assert executed.status_code == 200, executed.text
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 0
            assert c.execute('SELECT status FROM specialized_exam_run WHERE run_id=?', (run['run_id'],)).fetchone()[0] == 'active'
        if '不对' in quote:
            assert len(gateway.requests) == 1
        else:
            assert not gateway.requests


def task_fixture(db, sid):
    """Only seed a plan; actual start/pause/dispatch use product transactions."""
    with db.connect() as c:
        c.execute('''INSERT INTO daily_plan(plan_id,child_id,session_id,plan_date,status,total_minutes,generator,version,created_at,client_action_id)
            SELECT 'control-plan',child_id,session_id,'2026-09-07','active',20,'synthetic-control',1,datetime('now'),'control-plan'
            FROM daily_session WHERE session_id=?''', (sid,))
        for position in (1, 2):
            c.execute('''INSERT INTO plan_item(plan_item_id,plan_id,subject,title,expected_minutes,priority,status,position,source_type)
                VALUES (?,'control-plan','数学','Synthetic task',10,1,'pending',?,'school_homework')''',
                (f'control-task-{position}', position))
        c.commit()
    repo = StudyRepository(db)
    repo.start_plan_item('control-task-1', 'initial-task-start')
    return repo


@pytest.mark.parametrize('change', ['replace_item', 'revoke_before_repository_write'])
def test_task_pause_never_retargets_and_rechecks_real_write(environment, monkeypatch, change):
    settings, db = environment
    quote = '手头这件先搁着，我得腾出手来'
    with TestClient(create_app(settings, model_gateway=ControlGateway(quote, 'task.pause'))) as client:
        sid, run = open_exam(client, db)
        repo = task_fixture(db, sid)
        voice = capture(client, settings, monkeypatch, sid, quote, {'kind': 'exam', 'binding': run['answer_binding']})
        heard = interpret(client, sid, quote, 'natural-task-pause', voice)
        assert heard['action']['type'] == 'pause_active_task'
        if change == 'replace_item':
            repo.pause_plan_item('control-task-1', 'separate-task-pause')
            repo.start_plan_item('control-task-2', 'separate-task-start')
        else:
            original = StudyRepository.pause_plan_item
            checked = []
            def revoke_then_pause(self, item_id, key, *args, **kwargs):
                assert kwargs['authorized_turn_id'] == heard['agent_turn_id']
                with db.connect() as c:
                    assert AgentRuntimeStore.inspect_voice_control_execution(c, heard['agent_turn_id'])['targets']['task']['plan_item_id'] == item_id
                    c.execute("UPDATE voice_control_intent_request SET status='cancelled'")
                    c.commit()
                checked.append(item_id)
                return original(self, item_id, key, *args, **kwargs)
            monkeypatch.setattr(StudyRepository, 'pause_plan_item', revoke_then_pause)
        response = client.post(f"/v1/agent-turns/{heard['agent_turn_id']}/execute")
        assert response.status_code == 409, response.text
        with db.connect() as c:
            states = dict(c.execute("SELECT plan_item_id,status FROM plan_item WHERE plan_id='control-plan'"))
            assert states == ({'control-task-1': 'paused', 'control-task-2': 'active'} if change == 'replace_item'
                              else {'control-task-1': 'active', 'control-task-2': 'pending'})
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 0
        if change != 'replace_item':
            assert checked == ['control-task-1']


@pytest.mark.parametrize('command,quote', [
    ('pause', '这张卷子先搁一搁，回来咱们再往下做'),
    ('resume', '回到这张卷子吧，我想接着往下做'),
    ('cancel', '这张卷子到这里就交了，剩下的空着吧'),
])
def test_exam_postwrite_failure_replays_without_repeating_effect(environment, monkeypatch, command, quote):
    settings, db = environment
    gateway = ControlGateway(quote, f'exam.{command}')
    original = getattr(ExamSimulationService, command)
    failed = []
    def fail_after_write(self, run_id, *, authorized_turn_id=None):
        result = original(self, run_id, authorized_turn_id=authorized_turn_id)
        if authorized_turn_id and not failed:
            failed.append(result['status'])
            raise OSError('synthetic lost execution response')
        return result
    with TestClient(create_app(settings, model_gateway=gateway), raise_server_exceptions=False) as client:
        sid, run = open_exam(client, db)
        reference = {'kind': 'exam', 'binding': run['answer_binding']}
        if command == 'resume':
            ExamSimulationService(db).pause(run['run_id'])
        voice = capture(client, settings, monkeypatch, sid, quote, reference)
        heard = interpret(client, sid, quote, 'exam-postwrite-retry', voice)
        assert heard['action']['type'] == f'{command}_exam_simulation'
        monkeypatch.setattr(ExamSimulationService, command, fail_after_write)
        url = f"/v1/agent-turns/{heard['agent_turn_id']}/execute"
        first = client.post(url)
        assert first.status_code == 500, first.text
        with db.connect() as c:
            receipt = c.execute('SELECT execution_receipt_json FROM voice_control_intent_request').fetchone()[0]
            assert json.loads(receipt)['control'] == f'exam.{command}'
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        result = client.post(url)
        assert result.status_code == 200, result.text
        with db.connect() as c:
            assert c.execute('SELECT execution_receipt_json FROM voice_control_intent_request').fetchone()[0] == receipt
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 0
            # Restart deliberately pauses active exams. A replayed resume must
            # not start a second segment after that safety pause.
            assert c.execute('SELECT status FROM specialized_exam_run WHERE run_id=?', (run['run_id'],)).fetchone()[0] == ('cancelled' if command == 'cancel' else 'paused')
    assert len(gateway.requests) == 1


@pytest.mark.parametrize('control', ['observation.pause', 'exam.pause'])
def test_stale_capture_allows_device_control_but_never_retargets_new_exam(environment, monkeypatch, control):
    settings, db = environment
    quote = '先让看我的那个镜头休息一下，等会儿再看我' if control == 'observation.pause' else '这张卷子先搁一搁，回来咱们再往下做'
    gateway = ControlGateway(quote, control)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid, old = open_exam(client, db)
        reference = {'kind': 'exam', 'binding': old['answer_binding']}
        service = ExamSimulationService(db)
        service.cancel(old['run_id'])
        current = service.start(sid, 'competition_math', '数学', 'practice', 2, 30, 'new-exam-before-asr')
        voice = capture(client, settings, monkeypatch, sid, quote, reference)
        response = client.post(f'/v1/sessions/{sid}/agent-turn', json={
            'transcript': quote, 'client_action_id': 'stale-exam-control', 'voice_input_id': voice})
        if control == 'observation.pause':
            assert response.status_code == 200, response.text
            heard = response.json()
            assert heard['action']['type'] == 'pause_observation'
            assert heard['action']['voice_input_binding']['question_reference'] == reference
            admission = client.post(f"/v1/sessions/{sid}/agent-turns/{heard['agent_turn_id']}/control-admission")
            assert admission.status_code == 200 and admission.json()['status'] == 'admitted'
        else:
            assert response.status_code == 409, response.text
        with db.connect() as c:
            assert c.execute('SELECT status FROM specialized_exam_run WHERE run_id=?', (current['run_id'],)).fetchone()[0] == 'active'
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 0
