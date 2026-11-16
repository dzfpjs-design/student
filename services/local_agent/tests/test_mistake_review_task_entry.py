"""Real task/dialogue writes; isolated schedules and voice inference are synthetic."""
import json

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.mistake_dialogue import MistakeDialogueService
from test_mistake_voice_journey import settings, fixture, begin, say, focus, ok
from test_primary_mixed_voice_journey import MixedGateway
from test_task_resume_voice_http import hear


@pytest.fixture
def paper_task(tmp_path):
    config = settings(tmp_path)
    with TestClient(create_app(config)) as client:
        old, db = fixture(client)
        begin(client, old)
        say(client, old, '对', 'paper-stem-confirm')
        say(client, old, '我选了B', 'paper-original-answer')
        say(client, old, '是的', 'paper-original-save')
        mistake = focus(client, old)['mistake_id']
        with db.connect() as c:
            c.execute("UPDATE daily_session SET status='completed',state='completed' WHERE session_id=?", (old,))
            c.commit()
        sid = ok(client.post('/v1/sessions', json=dict(child_id='child-primary-4',
                     client_action_id='review-next-session')))['session_id']
        with db.connect() as c:
            c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
            c.execute("""INSERT INTO daily_plan(plan_id,child_id,session_id,plan_date,status,total_minutes,
                generator,version,created_at,client_action_id) VALUES
                ('review-day','child-primary-4',?,date('now'),'active',20,'synthetic schedule',1,datetime('now'),'review-day')""", (sid,))
            c.execute("""INSERT INTO plan_item(plan_item_id,plan_id,subject,title,expected_minutes,priority,status,
                position,source_type,source_ref,task_type) VALUES
                ('paper-review','review-day','英语','复习原错题',10,1,'pending',1,'mistake_review',?,'review')""", (mistake,))
            c.execute("""INSERT INTO plan_item(plan_item_id,plan_id,subject,title,expected_minutes,priority,status,
                position,source_type,task_type) VALUES
                ('later-school','review-day','数学','后续作业',10,2,'pending',2,'school_homework','written')""")
            c.commit()
    return config, db, sid, mistake, MixedGateway('')


def request(client, case, monkeypatch, quote, key, control):
    config, _, sid, _, gateway = case
    heard = hear(client, config, monkeypatch, gateway, sid, None, quote, key, control)
    assert heard.get('runtime_capability') == control, {k: heard.get(k) for k in ('action', 'response_text')}
    return f"/v1/agent-turns/{heard['agent_turn_id']}/execute"


@pytest.mark.parametrize('fail_after_focus', [False, True])
def test_start_and_original_focus_roll_back_together_then_new_voice_request(paper_task, monkeypatch, fail_after_focus):
    config, db, sid, mistake, gateway = paper_task
    with TestClient(create_app(config, model_gateway=gateway), raise_server_exceptions=False) as client:
        url = request(client, paper_task, monkeypatch, '开始今天的错题复习', 'paper-start-atomic', 'task.start')
        original = MistakeDialogueService.open_review
        def interrupted(self, *args, **kwargs):
            if fail_after_focus:
                original(self, *args, **kwargs)
            raise OSError('synthetic failure while opening original correction')
        with monkeypatch.context() as patch:
            patch.setattr(MistakeDialogueService, 'open_review', interrupted)
            assert client.post(url).status_code == 500
        with db.connect() as c:
            assert tuple(c.execute("SELECT status,started_at FROM plan_item WHERE plan_item_id='paper-review'").fetchone()) == ('pending', None)
            assert not c.execute('SELECT 1 FROM mistake_dialogue WHERE session_id=?', (sid,)).fetchone()
            assert not c.execute("SELECT 1 FROM plan_item_event WHERE plan_item_id='paper-review'").fetchone()
        assert client.post(url).status_code == 409
        url = request(client, paper_task, monkeypatch, '开始今天的错题复习', 'paper-start-renewed', 'task.start')
        result = ok(client.post(url))['execution']
        assert result['requires_child_reply'] is True
        assert result['result']['mistake_dialogue']['mistake_id'] == mistake
        assert result['result']['mistake_dialogue']['state']['submitted_answer'] == 'B'
        assert '为什么' in result['response_text']
        ok(client.post(url))
        with db.connect() as c:
            assert c.execute("SELECT count(*) FROM plan_item_event WHERE plan_item_id='paper-review' AND event_type='started'").fetchone()[0] == 1
            assert c.execute("SELECT status FROM plan_item WHERE plan_item_id='later-school'").fetchone()[0] == 'pending'


@pytest.mark.parametrize('offline', [False, True])
def test_restart_resumes_same_paper_and_dialogue_without_clicks(paper_task, monkeypatch, offline):
    config, db, sid, mistake, gateway = paper_task
    with TestClient(create_app(config, model_gateway=gateway)) as client:
        url = request(client, paper_task, monkeypatch, '开始复习', 'paper-start-restart', 'task.start')
        ok(client.post(url))
    with TestClient(create_app(config, model_gateway=gateway)) as client:
        with db.connect() as c:
            assert c.execute("SELECT status FROM plan_item WHERE plan_item_id='paper-review'").fetchone()[0] == 'paused'
        if offline:
            gateway.configured = False
        url = request(client, paper_task, monkeypatch,
                      '那我们继续吧' if offline else '刚才那项错题复习我接着做，计时也继续',
                      'paper-resume-restart', 'task.resume')
        resumed = ok(client.post(url))['execution']
        assert resumed['result']['mistake_dialogue']['mistake_id'] == mistake
        assert resumed['requires_child_reply'] is True
        assert '为什么' in resumed['response_text']
        assert focus(client, sid)['state']['submitted_answer'] == 'B'
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 0
            assert c.execute("SELECT status FROM plan_item WHERE plan_item_id='later-school'").fetchone()[0] == 'pending'
    for sent in gateway.requests:
        if sent.request_type == 'voice_interaction':
            body = json.loads(sent.messages[-1]['content'])
            assert set(body) == {'transcript', 'state'}
            assert all(type(v) is bool for v in body['state'].values())
            assert not sent.evidence_refs
            assert mistake not in json.dumps(sent.messages)


@pytest.mark.parametrize('change', ['excluded', 'mastered', 'other_child', 'cancelled'])
def test_changed_source_cannot_start_timer_or_open_original(paper_task, monkeypatch, change):
    config, db, sid, mistake, gateway = paper_task
    with TestClient(create_app(config, model_gateway=gateway)) as client:
        url = request(client, paper_task, monkeypatch, '开始复习', 'paper-source-start', 'task.start')
        with db.connect() as c:
            if change in {'excluded', 'mastered'}:
                c.execute('UPDATE mistake_case SET status=? WHERE mistake_id=?', (change, mistake))
            elif change == 'other_child':
                c.execute("UPDATE mistake_case SET child_id='child-junior-3' WHERE mistake_id=?", (mistake,))
            else:
                turn_id = url.split('/')[-2]
                c.execute("UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id=?", (turn_id,))
            c.commit()
        assert client.post(url).status_code == 409
        with db.connect() as c:
            assert tuple(c.execute("SELECT status,started_at FROM plan_item WHERE plan_item_id='paper-review'").fetchone()) == ('pending', None)
            assert not c.execute('SELECT 1 FROM mistake_dialogue WHERE session_id=?', (sid,)).fetchone()


def test_focus_change_between_hearing_and_resume_cannot_overwrite_new_work(paper_task, monkeypatch):
    config, db, sid, mistake, gateway = paper_task
    with TestClient(create_app(config, model_gateway=gateway)) as client:
        url = request(client, paper_task, monkeypatch, '开始复习', 'paper-focus-start', 'task.start')
        ok(client.post(url))
    with TestClient(create_app(config, model_gateway=gateway)) as client:
        url = request(client, paper_task, monkeypatch, '继续刚才的复习任务', 'paper-focus-resume', 'task.resume')
        with db.connect() as c:
            c.execute("UPDATE mistake_dialogue SET phase='candidate',mistake_id=NULL WHERE session_id=?", (sid,))
            c.commit()
        assert client.post(url).status_code == 409
        with db.connect() as c:
            assert c.execute("SELECT status FROM plan_item WHERE plan_item_id='paper-review'").fetchone()[0] == 'paused'
            assert c.execute('SELECT phase FROM mistake_dialogue WHERE session_id=?', (sid,)).fetchone()[0] == 'candidate'
            assert c.execute("SELECT count(*) FROM plan_item_event WHERE event_type='resumed'").fetchone()[0] == 0


@pytest.mark.parametrize('phase', ['scheduled', 'closed'])
def test_unready_review_rolls_back_timer_and_focus(paper_task, monkeypatch, phase):
    config, db, sid, _, gateway = paper_task
    with TestClient(create_app(config, model_gateway=gateway)) as client:
        url = request(client, paper_task, monkeypatch, '开始复习', 'paper-unready-start', 'task.start')
        original = MistakeDialogueService.open_review
        def no_current_question(self, *args, **kwargs):
            value = original(self, *args, **kwargs)
            return {**value, 'phase': phase}
        monkeypatch.setattr(MistakeDialogueService, 'open_review', no_current_question)
        assert client.post(url).status_code == 409
        with db.connect() as c:
            assert tuple(c.execute("SELECT status,started_at FROM plan_item WHERE plan_item_id='paper-review'").fetchone()) == ('pending', None)
            assert not c.execute('SELECT 1 FROM mistake_dialogue WHERE session_id=?', (sid,)).fetchone()


def test_committed_start_with_lost_reply_does_not_reopen_or_restart(paper_task, monkeypatch):
    from home_study_agent.agent_capability_executor import AgentCapabilityExecutor
    config, db, sid, mistake, gateway = paper_task
    with TestClient(create_app(config, model_gateway=gateway), raise_server_exceptions=False) as client:
        url = request(client, paper_task, monkeypatch, '开始复习', 'paper-lost-reply', 'task.start')
        original = AgentCapabilityExecutor._start_mistake_review
        def lose_reply(self, *args, **kwargs):
            original(self, *args, **kwargs)
            raise OSError('synthetic reply loss after both commits')
        with monkeypatch.context() as patch:
            patch.setattr(AgentCapabilityExecutor, '_start_mistake_review', lose_reply)
            assert client.post(url).status_code == 500
        before = focus(client, sid)
        replay = ok(client.post(url))['execution']
        assert replay['replayed'] is True
        assert replay['replay_audio_allowed'] is False
        assert focus(client, sid) == before
        with db.connect() as c:
            assert c.execute("SELECT count(*) FROM plan_item_event WHERE event_type='started'").fetchone()[0] == 1
            assert c.execute('SELECT count(*) FROM mistake_dialogue WHERE session_id=?', (sid,)).fetchone()[0] == 1


def test_scheduled_paper_start_to_voice_teaching_board_and_independent_answer(paper_task, monkeypatch):
    from test_continuous_marked_mistake_journey import PhonicsGateway

    class TeachingGateway(MixedGateway):
        def __init__(self):
            super().__init__('')
            self.tutor = PhonicsGateway()

        async def complete(self, request):
            if request.request_type in {'agent_plan', 'voice_interaction'}:
                return await super().complete(request)
            return await self.tutor.complete(request)

    config, db, sid, mistake, _ = paper_task
    gateway = TeachingGateway()
    case = (config, db, sid, mistake, gateway)
    with TestClient(create_app(config, model_gateway=gateway)) as client:
        token = ok(client.post('/v1/parent/setup', json={'pin': '8264'}))['parent_token']
        ok(client.put('/v1/parent/privacy', headers={'X-HSC-Parent': token},
                      json={'cloud_model_text_allowed': True}))
        url = request(client, case, monkeypatch, '开始今天的复习', 'paper-teach-start', 'task.start')
        ok(client.post(url))
        assert not gateway.tutor.requests

        def speak(text, key):
            current = focus(client, sid)
            reference = {'kind': 'mistake', 'binding': current['answer_binding']} if current.get('answer_binding') else None
            heard = hear(client, config, monkeypatch, gateway, sid, reference, text, key, 'mistake.dialogue')
            assert heard['action']['type'] == 'continue_mistake_dialogue', heard['action']
            return ok(client.post(f"/v1/agent-turns/{heard['agent_turn_id']}/execute"))

        taught = speak('我是看拼写选的，不知道比较读音的方法', 'paper-teach-reason')
        assert not taught['execution'].get('teaching_error'), taught['execution']
        tutor = taught['execution']['tutor_turn']
        board = tutor['output_envelope']['result']['blackboard']
        assert board['source'] == 'model_structured' and len(board['segments']) == 2
        assert 'grape' in tutor['question'] and 'like' in tutor['question']
        assert '一元二次' not in tutor['answer']
        assert len(gateway.tutor.requests) == 1
        speak('明白了', 'paper-teach-ready')
        activity = focus(client, sid)['state']['next_activity']
        assert activity['kind'] == 'attempt'
        question = activity['item']['prompt']
        assert question != tutor['question']
        answer = ('bag /æ/ 不同，cake name /eɪ/' if 'cake' in question else
                  'sit /ɪ/ 不同，bike five /aɪ/' if 'bike' in question else
                  'hot /ɒ/ 不同，home nose /əʊ/')
        checked = speak('答案是' + answer, 'paper-independent-answer')
        assert checked['execution']['result']['mistake_id'] == mistake
        current = client.app.state.mistake_book.get(mistake, 'child-primary-4')
        assert current['status'] != 'mastered'
        assert current['mastery_gate']['satisfied'] == {}
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM mistake_attempt WHERE mistake_id=?', (mistake,)).fetchone()[0] == 1
            assert c.execute('SELECT count(*) FROM tutor_turn WHERE session_id=?', (sid,)).fetchone()[0] == 1
            assert c.execute("SELECT status FROM plan_item WHERE plan_item_id='later-school'").fetchone()[0] == 'pending'
    with TestClient(create_app(config, model_gateway=gateway)) as client:
        assert focus(client, sid)['mistake_id'] == mistake
        assert client.app.state.mistake_book.get(mistake, 'child-primary-4')['status'] != 'mastered'
        with db.connect() as c:
            row = c.execute('SELECT submitted_answer FROM mistake_attempt WHERE mistake_id=?', (mistake,)).fetchone()
            assert row[0].endswith(answer)
