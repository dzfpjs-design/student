"""Conditional semantic contracts through real Runtime HTTP and SQLite."""
import json

import pytest
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.model_gateway import ModelResult
from test_learning_input_replan import schedule, inputs, flow
from test_learning_input_actionable_clarification import MISSING
from test_paused_homework_revision_runtime_http import SemanticGateway, say, post
from test_diagnostic_prompt_http import prepare
from test_voice_runtime_binding import capture, interpret


class JourneyGateway(SemanticGateway):
    """Conditional routing witness, never real-model understanding evidence."""
    phase = 'proposal'
    control = None

    async def complete(self, request):
        payload = json.loads(request.messages[-1]['content'])
        assert request.request_type == 'voice_interaction', 'No general planner/history fallback'
        if self.phase == 'proposal' or (payload.get('pending_input_field') and payload['transcript'] == '大概十分钟'):
            return await super().complete(request)
        self.requests.append(request)
        if payload.get('pending_input_field'):
            assert set(payload) == {'transcript', 'pending_input_field'}
            result = dict(field=payload['pending_input_field'], meaning='other_request', quote=payload['transcript'],
                          confidence=.99, literal_quote=None, minutes=None)
        else:
            assert set(payload) == {'transcript', 'state'}, payload
            result = dict(role='control' if self.control else 'unknown', control=self.control,
                          quote=payload['transcript'], confidence=.99)
            if self.control == 'companion.start_activity':
                result['activity_type'] = 'movement'
        return ModelResult(content=json.dumps(result, ensure_ascii=False), provider='synthetic', model='routing-fixture', latency_ms=1)


def state(db, prompt):
    with db.connect() as c:
        row = c.execute('SELECT * FROM learning_input_proposal').fetchone()
        arrival = c.execute('SELECT pending_json FROM learning_input_arrival ORDER BY rowid DESC LIMIT 1').fetchone()
        return dict(draft_state=row['state'], changes=json.loads(row['proposal_json'])['changes'],
            draft_request_id=row['request_id'], offered_by_turn_id=row['offered_by_turn_id'],
            latest_arrival_pending=json.loads(arrival[0] or 'null'),
            question=dict(c.execute('SELECT prompt_id,item_version,status,answer_action_id FROM diagnostic_prompt WHERE prompt_id=?', (prompt['prompt_id'],)).fetchone()),
            homework=[dict(r) for r in c.execute('SELECT homework_id,status,expected_minutes FROM homework_item ORDER BY homework_id')],
            evidence_count=c.execute('SELECT count(*) FROM learning_evidence').fetchone()[0],
            revision_count=c.execute('SELECT count(*) FROM learning_input_revision_event').fetchone()[0],
            attempts=c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0],
            session_state=c.execute('SELECT state FROM daily_session WHERE session_id=?', (row['session_id'],)).fetchone()[0],
            activities=[dict(r) for r in c.execute('SELECT activity_id,activity_type,status FROM companion_activity ORDER BY rowid')])


@pytest.mark.parametrize('path', ['chat', 'rest', 'activity'])
def test_explicit_support_and_reentry_preserve_old_question(schedule, tmp_path, monkeypatch, path):
    (db, sid, _), _, _ = schedule
    settings = Settings(data_dir=tmp_path/'app', database_path=db.path, maintenance_enabled=False)
    gateway = JourneyGateway(revision=MISSING)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        post(client, '/v1/plan-items/old/start', 'start')
        post(client, '/v1/plan-items/old/pause', 'pause')
        prompt = prepare(client, sid, track='school', preferred_item_id='diag-p4-arithmetic-closed-1')['prompt']
        _, draft = say(client, sid, '还漏了语文作业，背诵第二段', 'draft')
        assert draft['execution']['result']['missing_field'] == 'expected_minutes'
        before = state(db, prompt)
        gateway.phase = 'request'

        def heard(text, key, control=None):
            key = 'reentry-'+key
            gateway.control = control
            voice = capture(client, settings, monkeypatch, sid, text, prompt, key+'-voice')
            turn = interpret(client, sid, text, key, voice)
            response = client.post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
            assert response.status_code == 200, response.text
            return turn, response.json()

        if path == 'chat':
            entered, execution = heard('我有点难过，先陪我聊会儿好吗', 'chat', 'companion.chat')
            assert entered['runtime_capability'] == 'conversation.respond'
            assert '分钟' not in execution['execution']['response_text']
        elif path == 'rest':
            entered, _ = heard('我累了，先歇会儿', 'rest', 'break.begin')
            assert entered['runtime_capability'] == 'break.begin'
            resumed, resumed_result = heard('休息好了', 'resume', 'break.resume')
            assert resumed['runtime_capability'] == 'break.resume'
            assert resumed_result['execution']['result']['input_reentry']['missing_field'] == 'expected_minutes'
            assert '几分钟' in resumed_result['execution']['response_text']
        else:
            entered, _ = heard('先陪我活动一下身体吧', 'activity', 'companion.start_activity')
            assert entered['runtime_capability'] == 'companion.start_activity'
            ended = client.post('/v1/sessions/'+sid+'/companion-activities/end', json={'reason':'child_requested'})
            assert ended.status_code == 200 and ended.json()['status'] == 'completed'

        if path != 'rest':
            # A bare duration after interruption is not the old field answer.
            heard('大概十分钟', 'not-yet-returned')
            assert state(db, prompt)['changes'] == before['changes']
            returned, result = heard('回到刚才没填完的作业，接着说吧', 'return', 'learning_inputs.resume_draft')
            assert returned['runtime_capability'] == 'learning_inputs.select_target'
            assert result['execution']['result']['missing_field'] == 'expected_minutes'
        else:
            returned = resumed
        reoffered = state(db, prompt)
        assert reoffered['offered_by_turn_id'] == returned['agent_turn_id']
        assert reoffered['changes'] == before['changes']
        _, supplied = heard('大概十分钟', 'duration')
        assert supplied['execution']['result']['status'] == 'awaiting_confirmation'
        after = state(db, prompt)
        assert after['changes']['expected_minutes'] == 10
        assert after['question'] == before['question']
        assert after['homework'] == before['homework']
        assert after['draft_request_id'] == before['draft_request_id']
        assert after['attempts'] == after['revision_count'] == after['evidence_count'] == 0


@pytest.fixture
def interrupted_input(schedule, tmp_path, monkeypatch):
    (db, sid, _), _, clock = schedule
    settings = Settings(data_dir=tmp_path/'app', database_path=db.path, maintenance_enabled=False)
    gateway = JourneyGateway(revision=MISSING)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        post(client, '/v1/plan-items/old/start', 'guard-start')
        post(client, '/v1/plan-items/old/pause', 'guard-pause')
        prompt = prepare(client, sid, track='school', preferred_item_id='diag-p4-arithmetic-closed-1')['prompt']
        say(client, sid, '还漏了语文作业，背诵第二段', 'guard-draft')
        gateway.phase = 'request'

        def heard(text, key, control=None, execute=True):
            gateway.control = control
            voice = capture(client, settings, monkeypatch, sid, text, prompt, key+'-voice')
            turn = interpret(client, sid, text, key, voice)
            if not execute:
                return turn, voice
            response = client.post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
            assert response.status_code == 200, response.text
            return turn, response.json()

        heard('我有点难过，先陪我聊会儿好吗', 'guard-chat', 'companion.chat')
        yield db, sid, settings, client, gateway, prompt, heard, clock


@pytest.mark.parametrize('text,control', [
    ('不要回到刚才的作业，先别继续', 'learning_inputs.resume_draft'),
    ('老师说“回到刚才的作业”', 'learning_inputs.resume_draft'),
    ('如果回到作业会怎么样', 'learning_inputs.resume_draft'),
    ('不想聊天了，先别陪我聊', 'companion.chat'),
    ('她说“陪我聊一会儿”', 'companion.chat'),
    ('确认', 'learning_inputs.resume_draft'),
    ('大概十分钟', 'learning_inputs.resume_draft'),
    ('大概十分钟', 'companion.chat'),
    ('这是一句没有明确用途的话', None),
])
def test_untrusted_or_unknown_request_never_reopens_field(interrupted_input, text, control):
    db, sid, settings, client, gateway, prompt, heard, clock = interrupted_input
    before = state(db, prompt)
    turn, _ = heard(text, 'guard-denied', control)
    assert turn['action'].get('source') != 'content_free_voice_control'
    after = state(db, prompt)
    assert after['offered_by_turn_id'] == before['offered_by_turn_id']
    assert after['question'] == before['question'] and after['changes'] == before['changes']
    assert after['attempts'] == after['revision_count'] == after['evidence_count'] == 0


@pytest.mark.parametrize('mutation', ['source', 'cancel', 'offer_cancel', 'consent', 'new_arrival'])
def test_late_reentry_cannot_revive_changed_authority(interrupted_input, mutation):
    db, sid, settings, client, gateway, prompt, heard, clock = interrupted_input
    before = state(db, prompt)
    turn, _ = heard('回到刚才没填完的作业，接着说吧', 'guard-return', 'learning_inputs.resume_draft', execute=False)
    assert turn['runtime_capability'] == 'learning_inputs.select_target'
    if mutation == 'new_arrival':
        heard('我还没决定', 'guard-newer')
    else:
        with db.connect() as c:
            if mutation == 'source':
                c.execute("UPDATE homework_item SET title='changed source' WHERE homework_id='math-a'")
            elif mutation == 'cancel':
                c.execute("UPDATE learning_input_proposal SET state='cancelled'")
            elif mutation == 'offer_cancel':
                c.execute("UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id=?", (before['offered_by_turn_id'],))
            else:
                c.execute('UPDATE privacy_preference SET cloud_model_text_allowed=0')
            c.commit()
    response = client.post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
    assert response.status_code == 409, response.text
    after = state(db, prompt)
    assert after['offered_by_turn_id'] == before['offered_by_turn_id']
    assert after['question'] == before['question'] and after['changes'] == before['changes']
    assert after['attempts'] == after['revision_count'] == after['evidence_count'] == 0


def test_changed_source_requires_review_not_silent_rebase(interrupted_input):
    db, sid, settings, client, gateway, prompt, heard, clock = interrupted_input
    before = state(db, prompt)
    with db.connect() as c:
        c.execute("UPDATE homework_item SET title='new teacher scope' WHERE homework_id='math-a'")
        c.commit()
    turn, result = heard('回到刚才没填完的作业，接着说吧', 'changed-return', 'learning_inputs.resume_draft')
    assert result['execution']['result']['status'] == 'conflict'
    assert '重新核对' in result['execution']['response_text']
    after = state(db, prompt)
    assert after['changes'] == before['changes'] and after['question'] == before['question']
    assert after['offered_by_turn_id'] == before['offered_by_turn_id']


def test_return_then_duration_still_requires_separate_real_confirmation(interrupted_input):
    db, sid, settings, client, gateway, prompt, heard, clock = interrupted_input
    before = state(db, prompt)
    heard('回到刚才没填完的作业，接着说吧', 'confirm-return', 'learning_inputs.resume_draft')
    heard('大概十分钟', 'confirm-duration')
    assert state(db, prompt)['homework'] == before['homework']
    confirmed, result = heard('确认', 'confirm-final')
    assert confirmed['runtime_capability'] == 'learning_inputs.confirm_revision'
    assert result['execution']['result']['status'] == 'applied'
    with db.connect() as c:
        added = c.execute("SELECT title,expected_minutes,confirmation_status FROM homework_item WHERE homework_id NOT IN ('math-a','math-b')").fetchall()
        assert len(added) == 1
        assert tuple(added[0]) == ('背诵第二段', 10, 'child_confirmed')
    after = state(db, prompt)
    assert after['question'] == before['question']
    assert after['attempts'] == after['evidence_count'] == 0
    assert after['revision_count'] == 1


def test_captured_return_cannot_cross_children(interrupted_input, monkeypatch):
    from test_diagnostic_prompt_http import OTHER
    db, sid, settings, client, gateway, prompt, heard, clock = interrupted_input
    before = state(db, prompt)
    text = '回到刚才没填完的作业，接着说吧'
    voice = capture(client, settings, monkeypatch, sid, text, prompt, 'cross-voice')
    switched = client.post('/v1/sessions/'+sid+'/switch-child', json={'target_child_id': OTHER, 'client_action_id': 'cross-switch-child'})
    assert switched.status_code == 200, switched.text
    other_sid = switched.json()['session_id']
    response = client.post('/v1/sessions/'+other_sid+'/agent-turn', json={
        'transcript': text, 'client_action_id': 'cross-return', 'cloud_text_allowed': True, 'voice_input_id': voice})
    assert response.status_code in {403, 409}, response.text
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM learning_input_proposal WHERE session_id=?', (other_sid,)).fetchone()[0] == 0
    assert state(db, prompt)['changes'] == before['changes']


@pytest.mark.parametrize('return_path', ['explicit', 'break', 'break_lost_response'])
def test_return_offer_and_original_command_survive_independent_processes(interrupted_input, tmp_path, monkeypatch, return_path):
    from test_paused_homework_revision_runtime_http import process_http
    db, sid, settings, client, gateway, prompt, heard, clock = interrupted_input
    if return_path.startswith('break'):
        heard('我累了，先歇会儿', 'process-break', 'break.begin')
        if return_path == 'break_lost_response':
            from home_study_agent.repository import StudyRepository
            turn, _ = heard('休息好了', 'process-return', 'break.resume', execute=False)
            original = StudyRepository.resume_after_break
            def lost_response(self, *args, **kwargs):
                original(self, *args, **kwargs)
                raise OSError('synthetic response loss after committed break and offer')
            with monkeypatch.context() as patch:
                patch.setattr(StudyRepository, 'resume_after_break', lost_response)
                client.__exit__(None, None, None)
                with TestClient(create_app(settings, model_gateway=gateway), raise_server_exceptions=False) as crash_client:
                    failed = crash_client.post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
                    assert failed.status_code == 500, failed.text
            with db.connect() as c:
                assert c.execute("SELECT count(*) FROM break_control_receipt WHERE operation='resume'").fetchone()[0] == 1
                assert c.execute('SELECT count(*) FROM learning_input_command WHERE turn_id=?', (turn['agent_turn_id'],)).fetchone()[0] == 1
        else:
            turn, result = heard('休息好了', 'process-return', 'break.resume')
            assert result['execution']['result']['input_reentry']['missing_field'] == 'expected_minutes'
    else:
        turn, result = heard('回到刚才没填完的作业，接着说吧', 'process-return', 'learning_inputs.resume_draft')
    before = state(db, prompt)
    first = process_http(db, tmp_path/'process', sid, clock[0], [
        {'kind': 'http', 'path': '/v1/sessions/'+sid+'/agent-request-receipts/process-return'},
        {'kind': 'http', 'method': 'POST', 'path': '/v1/agent-turns/'+turn['agent_turn_id']+'/execute'},
        {'kind': 'say', 'text': '大概十分钟', 'key': 'after-restart-duration'}])
    assert first['results'][-1]['result']['execution']['result']['status'] == 'awaiting_confirmation'
    assert first['results'][0]['status'] == ('recorded_failed' if return_path == 'break_lost_response' else 'recorded_completed')
    assert first['results'][0]['replay_allowed'] is False
    assert first['model_requests'] == 1
    after = state(db, prompt)
    assert after['changes']['expected_minutes'] == 10 and after['homework'] == before['homework']
    second = process_http(db, tmp_path/'process', sid, clock[0], [
        {'kind': 'http', 'path': '/v1/sessions/'+sid+'/agent-request-receipts/process-return'},
        {'kind': 'say', 'text': '取消', 'key': 'after-restart-cancel'}])
    assert second['results'][-1]['result']['execution']['result']['status'] == 'cancelled'
    third = process_http(db, tmp_path/'process', sid, clock[0], [
        {'kind': 'http', 'path': '/v1/sessions/'+sid+'/agent-request-receipts/process-return'},
        {'kind': 'http', 'method': 'POST', 'path': '/v1/agent-turns/'+turn['agent_turn_id']+'/execute'}])
    assert len({first['pid'], second['pid'], third['pid']}) == 3
    assert second['model_requests'] == third['model_requests'] == 0
    assert third['results'][0]['status'] == 'recorded_completed' and third['results'][0]['replay_allowed'] is False
    final = state(db, prompt)
    assert final['draft_state'] == 'cancelled' and final['question'] == before['question']
    assert final['homework'] == before['homework']
    assert final['attempts'] == final['revision_count'] == final['evidence_count'] == 0
    if return_path.startswith('break'):
        with db.connect() as c:
            assert c.execute("SELECT count(*) FROM break_control_receipt WHERE operation='resume'").fetchone()[0] == 1
