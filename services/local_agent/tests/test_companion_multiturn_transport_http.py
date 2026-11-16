"""Real ModelGateway with an in-memory HTTP transport, never a paid model."""
import json
import inspect

import httpx
import pytest
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.model_gateway import ModelGateway
from test_learning_input_replan import schedule, inputs, flow
from test_learning_input_actionable_clarification import MISSING, TITLE
from test_companion_input_reentry_http import state
from test_paused_homework_revision_runtime_http import post, say, process_http
from test_diagnostic_prompt_http import prepare
from test_voice_runtime_binding import capture, interpret

WORDS = ['我有点难过，先陪我聊会儿好吗', '今天一起做作业时同伴走开了，我觉得他不愿意和我一组',
         '不是，我刚才没说清，他是去拿本子，不是讨厌我，我只是题目做不出来很烦']
REPLIES = ['可以，我们先聊聊。刚才是什么事让你心里不舒服？',
           '一起做作业时他突然走开，让你有些失落。你想先说说当时的情况吗？',
           '谢谢你纠正我，他是去拿本子。让你烦的是题目卡住了；我们可以先缓一缓，再一起看看你卡在哪一步。']
RETURN = '回到刚才没填完的作业，接着说吧'
SHORT_REPLIES = {
    '嗯': '我在听。你可以按自己的节奏说说刚才发生的事。',
    '好的': '那我们先聊聊，不急着回到作业。你最想说的是哪一段？',
    '对': '明白，是刚才那件事让你难过。你想先说自己的感受吗？',
    '哦？': '我刚才是想听听发生了什么，不是要催你做题。哪里没说清楚呢？',
}


class Transport:
    def __init__(self):
        self.payloads = []
        self.dialogues = []
        self.on_dialogue = None
        self.fail_dialogue = False
        self.revision = MISSING
        self.short_role = 'companion_reply'
        self.short_control = None

    async def __call__(self, request):
        assert request.url.host == 'synthetic.invalid'
        payload = json.loads(request.content)
        self.payloads.append(payload)
        messages = payload['messages']
        if messages[0]['content'].startswith('你是家庭学习软件中的儿童学习伙伴'):
            self.dialogues.append(messages)
            if self.on_dialogue:
                changed = self.on_dialogue()
                if inspect.isawaitable(changed):
                    await changed
            if self.fail_dialogue:
                return httpx.Response(503, json={'error': {'message': 'synthetic unavailable'}})
            text = messages[-1]['content']
            answer = SHORT_REPLIES[text] if text in SHORT_REPLIES else REPLIES[WORDS.index(text)]
        else:
            data = json.loads(messages[-1]['content'])
            text = data['transcript']
            if 'pending_input_field' in data:
                assert set(data) == {'transcript', 'pending_input_field'}
                value = dict(field=data['pending_input_field'], meaning='other_request', quote=text,
                             literal_quote=None, minutes=None, confidence=.99)
                if text == '大概十分钟':
                    value.update(meaning='estimate', literal_quote=text, minutes=10)
                elif text == '这是我要改的作业':
                    value.update(meaning='own')
            else:
                assert set(data) == {'transcript', 'state'}
                value = dict(role='unknown', control=None, quote=text, confidence=.99)
                if '还漏了语文作业' in text or '改成小练笔' in text:
                    value.update(role='control', control='learning_inputs.propose_revision', revision=self.revision)
                elif text == WORDS[0]:
                    value.update(role='control', control='companion.chat')
                elif text in WORDS[1:]:
                    value.update(role='companion_reply')
                elif text in SHORT_REPLIES:
                    value.update(role=self.short_role, control=self.short_control)
                elif text == RETURN:
                    value.update(role='control', control='learning_inputs.resume_draft')
            answer = json.dumps(value, ensure_ascii=False)
        return httpx.Response(200, json={'choices':[{'message':{'content':answer}}],
            'usage':{'prompt_tokens':30, 'completion_tokens':20}})


@pytest.fixture
def companion(schedule, tmp_path, monkeypatch):
    (db, sid, _), _, _ = schedule
    settings = Settings(data_dir=tmp_path/'app', database_path=db.path, maintenance_enabled=False,
        model_api_key='synthetic-not-a-real-key', model_base_url='https://synthetic.invalid/v1',
        model_provider='synthetic', model_name='synthetic-dialogue', voice_intent_model_name='synthetic-intent')
    transport = Transport()
    gateway = ModelGateway(settings, transport=httpx.MockTransport(transport))
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        post(client, '/v1/plan-items/old/start', 'v9-start')
        post(client, '/v1/plan-items/old/pause', 'v9-pause')
        prompt = prepare(client, sid, track='school', preferred_item_id='diag-p4-arithmetic-closed-1')['prompt']
        say(client, sid, '还漏了语文作业，背诵第二段', 'v9-draft')
        before = state(db, prompt)

        def heard(client, text, key):
            voice = capture(client, settings, monkeypatch, sid, text, prompt, key+'-voice')
            turn = interpret(client, sid, text, key, voice)
            result = client.post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
            assert result.status_code == 200, result.text
            return turn, result.json()

    return db, sid, settings, gateway, transport, prompt, before, heard


def test_three_generated_turns_correction_and_return(companion):
    db, sid, settings, gateway, transport, prompt, before, heard = companion
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        for index, text in enumerate(WORDS):
            turn, result = heard(client, text, 'v9-chat-'+str(index))
            assert turn['response_text'] == REPLIES[index]
            assert result['execution']['response_text'] == REPLIES[index]
            assert result['turn']['response_text'] == REPLIES[index]
            assert turn['action']['source'] == 'cloud_companion'
            assert turn['action']['type'] == 'none'
            assert turn['runtime_capability'] == 'conversation.respond'
            sent = transport.dialogues[-1]
            assert [x['content'] for x in sent[1:]] == [s for pair in zip(WORDS[:index], REPLIES[:index]) for s in pair]+[text]
            assert '背诵第二段' not in json.dumps(sent, ensure_ascii=False)
        unchanged = state(db, prompt)
        assert unchanged['question'] == before['question'] and unchanged['changes'] == before['changes']
        assert unchanged['homework'] == before['homework']
        assert unchanged['attempts'] == unchanged['revision_count'] == unchanged['evidence_count'] == 0
        returned, result = heard(client, RETURN, 'v9-return')
        assert result['execution']['result']['missing_field'] == 'expected_minutes'
        _, supplied = heard(client, '大概十分钟', 'v9-duration')
        assert supplied['execution']['result']['status'] == 'awaiting_confirmation'
        assert len(transport.dialogues) == 3


@pytest.mark.parametrize('text', SHORT_REPLIES)
def test_short_reply_uses_durable_companion_context_without_answer_or_control(companion, text, schedule, tmp_path):
    db, sid, settings, gateway, transport, prompt, before, heard = companion
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        heard(client, WORDS[0], 'short-reply-enter')
    start_calls = len(transport.payloads)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        turn, result = heard(client, text, 'short-reply-follow')
        assert turn['response_text'] == SHORT_REPLIES[text]
        assert turn['action']['type'] == 'none'
        assert turn['action']['source'] == 'cloud_companion'
        assert turn['runtime_capability'] == 'conversation.respond'
        assert result['execution']['response_text'] == SHORT_REPLIES[text]
        sent = transport.dialogues[-1]
        assert [m['content'] for m in sent[1:]] == [WORDS[0], REPLIES[0], text]
        serialized = json.dumps(transport.payloads[start_calls:], ensure_ascii=False)
        assert '背诵第二段' not in serialized
        assert prompt['prompt_id'] not in serialized
        calls = len(transport.payloads)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        repeated, _ = heard(client, text, 'short-reply-follow')
        assert repeated['agent_turn_id'] == turn['agent_turn_id']
        assert repeated['response_text'] == SHORT_REPLIES[text]
        assert len(transport.payloads) == calls
    recovered_process = process_http(db, tmp_path/'short-independent-recovery', sid, schedule[2][0], [
        {'kind': 'http', 'path': '/v1/sessions/'+sid+'/agent-request-receipts/short-reply-follow'},
        {'kind': 'http', 'method': 'POST', 'path': '/v1/agent-turns/'+turn['agent_turn_id']+'/execute'}])
    assert recovered_process['model_requests'] == 0
    assert recovered_process['results'][0]['status'] == 'recorded_completed'
    assert recovered_process['results'][1]['turn']['response_text'] == SHORT_REPLIES[text]
    unchanged = state(db, prompt)
    assert unchanged['question'] == before['question']
    assert unchanged['homework'] == before['homework']
    assert unchanged['changes'] == before['changes']
    assert unchanged['attempts'] == unchanged['evidence_count'] == unchanged['revision_count'] == 0


def test_short_reply_cloud_failure_is_not_local_confirmation(companion):
    db, sid, settings, gateway, transport, prompt, before, heard = companion
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        heard(client, WORDS[0], 'short-failure-enter')
        transport.fail_dialogue = True
        turn, _ = heard(client, '嗯', 'short-failure')
        assert turn['action']['source'] == 'input_companion_unavailable'
        assert '没能连上' in turn['response_text']
        assert turn['action']['type'] == 'none'
        transport.fail_dialogue = False
        recovered, _ = heard(client, '哦？', 'short-after-failure')
        assert recovered['response_text'] == SHORT_REPLIES['哦？']
    assert state(db, prompt)['attempts'] == 0


def test_short_reply_without_current_cloud_consent_does_not_dispatch(companion):
    db, sid, settings, gateway, transport, prompt, before, heard = companion
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        heard(client, WORDS[0], 'short-consent-enter')
        calls = len(transport.payloads)
        with db.connect() as c:
            c.execute('UPDATE privacy_preference SET cloud_model_text_allowed=0 WHERE singleton_id=1')
            c.commit()
        turn, _ = heard(client, '嗯', 'short-consent-off')
        assert turn['action']['type'] == 'none'
        assert turn['action']['source'] != 'cloud_companion'
        assert len(transport.payloads) == calls
    assert state(db, prompt)['attempts'] == 0


@pytest.mark.parametrize('role,control', [('control', 'task.complete'), ('answer', None),
                                        ('control', 'companion.chat'), ('unknown', None)])
def test_short_reply_cannot_be_promoted_to_control_or_learning_answer(companion, role, control):
    db, sid, settings, gateway, transport, prompt, before, heard = companion
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        heard(client, WORDS[0], 'short-denial-enter')
        transport.short_role, transport.short_control = role, control
        calls = len(transport.payloads)
        turn, _ = heard(client, '嗯', 'short-denial-follow')
        assert len(transport.payloads) == calls + 1
        assert len(transport.dialogues) == 1
        assert turn['action']['type'] == 'none'
        assert turn['action']['source'] != 'cloud_companion'
    unchanged = state(db, prompt)
    assert unchanged['question'] == before['question']
    assert unchanged['homework'] == before['homework']
    assert unchanged['changes'] == before['changes']
    assert unchanged['attempts'] == unchanged['evidence_count'] == unchanged['revision_count'] == 0


def test_restart_replay_and_next_reply_use_same_durable_conversation(companion, schedule, tmp_path):
    db, sid, settings, gateway, transport, prompt, before, heard = companion
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        first, _ = heard(client, WORDS[0], 'durable-chat')
    calls = len(transport.payloads)
    recovered_process = process_http(db, tmp_path/'independent-recovery', sid, schedule[2][0], [
        {'kind': 'http', 'path': '/v1/sessions/'+sid+'/agent-request-receipts/durable-chat'},
        {'kind': 'http', 'method': 'POST', 'path': '/v1/agent-turns/'+first['agent_turn_id']+'/execute'}])
    assert recovered_process['model_requests'] == 0
    assert recovered_process['results'][0]['status'] == 'recorded_completed'
    assert recovered_process['results'][1]['turn']['response_text'] == REPLIES[0]
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        recovered = client.get('/v1/sessions/'+sid+'/agent-request-receipts/durable-chat')
        assert recovered.status_code == 200 and recovered.json()['status'] == 'recorded_completed'
        replay = client.post('/v1/agent-turns/'+first['agent_turn_id']+'/execute')
        assert replay.status_code == 200 and replay.json()['turn']['response_text'] == REPLIES[0]
        assert len(transport.payloads) == calls
        second, _ = heard(client, WORDS[1], 'durable-chat-next')
        assert second['response_text'] == REPLIES[1]
        assert [m['content'] for m in transport.dialogues[-1][1:]] == [WORDS[0], REPLIES[0], WORDS[1]]
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM agent_turn WHERE client_action_id=?', ('durable-chat',)).fetchone()[0] == 1
    assert state(db, prompt)['attempts'] == 0


def test_reply_role_without_entered_chat_cannot_steal_retained_question(companion):
    db, sid, settings, gateway, transport, prompt, before, heard = companion
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        turn, _ = heard(client, WORDS[1], 'unbound-companion-reply')
        assert turn['action'].get('source') != 'cloud_companion'
    assert transport.dialogues == []
    final = state(db, prompt)
    assert final['question'] == before['question'] and final['changes'] == before['changes']
    assert final['attempts'] == final['evidence_count'] == final['revision_count'] == 0


@pytest.mark.parametrize('field,value', [
    ('context_manifest_json', '{broken'), ('context_manifest_json', '[]'),
    ('context_manifest_json', '{"input_companion_dialogue":42}'),
    ('execution_receipt_json', '[]'), ('decision_json', '{broken'),
    ('transcript', 'changed synthetic prior voice'),
])
def test_corrupt_history_is_not_reused_as_cloud_context(companion, field, value):
    from home_study_agent.input_companion_dialogue import context
    db, sid, settings, gateway, transport, prompt, before, heard = companion
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        turn, _ = heard(client, WORDS[0], 'history-to-corrupt')
        with db.connect() as c:
            child = c.execute('SELECT child_id FROM daily_session WHERE session_id=?', (sid,)).fetchone()[0]
            if field == 'context_manifest_json':
                c.execute('UPDATE agent_plan SET context_manifest_json=? WHERE agent_turn_id=?', (value, turn['agent_turn_id']))
            elif field == 'transcript':
                c.execute('UPDATE agent_turn SET transcript=? WHERE agent_turn_id=?', (value, turn['agent_turn_id']))
            else:
                c.execute('UPDATE voice_control_intent_request SET '+field+'=? WHERE request_key=(SELECT json_extract(context_manifest_json,\'$.input_companion_dialogue.request_key\') FROM agent_plan WHERE agent_turn_id=?)',
                          (value, turn['agent_turn_id']))
            c.commit()
            assert context(c, child, sid) is None
        # A classifier cannot restore a missing/invalid focus with a reply label.
        if field != 'context_manifest_json':
            _, result = heard(client, WORDS[1], 'corrupt-history-next')
            assert result['turn']['action']['source'] != 'cloud_companion'
            assert len(transport.dialogues) == 1


@pytest.mark.parametrize('change', ['consent', 'draft'])
@pytest.mark.parametrize('short_followup', [False, True])
def test_late_cloud_response_cannot_revive_changed_authority(companion, monkeypatch, change, short_followup):
    db, sid, settings, gateway, transport, prompt, before, heard = companion
    def revoke():
        with db.connect() as c:
            if change == 'consent':
                c.execute('UPDATE privacy_preference SET cloud_model_text_allowed=0 WHERE singleton_id=1')
            else:
                c.execute("UPDATE learning_input_proposal SET state='cancelled'")
            c.commit()
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        if short_followup:
            heard(client, WORDS[0], 'late-short-enter')
        transport.on_dialogue = revoke
        text = '嗯' if short_followup else WORDS[0]
        voice = capture(client, settings, monkeypatch, sid, text, prompt, 'late-chat-voice')
        response = client.post('/v1/sessions/'+sid+'/agent-turn', json={
            'transcript': text, 'client_action_id': 'late-chat', 'cloud_text_allowed': True, 'voice_input_id': voice})
        assert response.status_code == 409, response.text
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM agent_turn WHERE client_action_id=?', ('late-chat',)).fetchone()[0] == 0
    assert state(db, prompt)['attempts'] == 0


def test_unavailable_model_is_truthful_and_next_turn_can_recover(companion):
    db, sid, settings, gateway, transport, prompt, before, heard = companion
    transport.fail_dialogue = True
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        turn, _ = heard(client, WORDS[0], 'model-unavailable')
        assert turn['action']['source'] == 'input_companion_unavailable'
        assert '没能连上' in turn['response_text']
        transport.fail_dialogue = False
        recovered, _ = heard(client, WORDS[1], 'model-recovered')
        assert recovered['action']['source'] == 'cloud_companion' and recovered['response_text'] == REPLIES[1]
    final = state(db, prompt)
    assert final['attempts'] == final['evidence_count'] == final['revision_count'] == 0


def test_chat_returns_to_existing_title_revision_without_inventing_minutes(companion):
    db, sid, settings, gateway, transport, prompt, before, heard = companion
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        say(client, sid, '取消', 'cancel-addition')
        transport.revision = TITLE
        _, proposed = say(client, sid, '我要把口算作业的名字改成小练笔', 'title-change')
        if proposed['execution']['result']['status'] == 'clarification_required':
            _, proposed = say(client, sid, '这是我要改的作业', 'title-ownership')
        assert proposed['execution']['result']['status'] == 'awaiting_confirmation'
        heard(client, WORDS[0], 'chat-after-title')
        _, returned = heard(client, RETURN, 'return-title')
        assert returned['execution']['result']['status'] == 'awaiting_confirmation'
        assert '小练笔' in returned['execution']['response_text']
        _, confirmed = say(client, sid, '确认', 'confirm-title')
        assert confirmed['execution']['result']['status'] == 'applied'
    with db.connect() as c:
        row = c.execute("SELECT title,expected_minutes FROM homework_item WHERE homework_id='math-a'").fetchone()
        assert row['title'] == '小练笔'
        assert row['expected_minutes'] == next(r['expected_minutes'] for r in before['homework'] if r['homework_id'] == 'math-a')
        assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 0


def test_inflight_duplicate_does_not_generate_twice_or_replace_first_reply(companion, monkeypatch):
    db, sid, settings, gateway, transport, prompt, before, heard = companion
    app = create_app(settings, model_gateway=gateway)
    duplicates = []
    with TestClient(app, raise_server_exceptions=False) as client:
        voice = capture(client, settings, monkeypatch, sid, WORDS[0], prompt, 'inflight-voice')
        payload = {'transcript': WORDS[0], 'client_action_id': 'inflight-chat',
                   'cloud_text_allowed': True, 'voice_input_id': voice}
        async def duplicate():
            transport.on_dialogue = None
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as local:
                duplicates.append(await local.post('/v1/sessions/'+sid+'/agent-turn', json=payload))
        transport.on_dialogue = duplicate
        response = client.post('/v1/sessions/'+sid+'/agent-turn', json=payload)
        assert response.status_code == 200, response.text
        assert response.json()['response_text'] == REPLIES[0]
    assert len(transport.dialogues) == 1
    assert len(duplicates) == 1 and duplicates[0].status_code == 409
    assert duplicates[0].json()['code'] == 'INPUT_COMPANION_RESPONSE_PENDING'
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM agent_turn WHERE client_action_id=?', ('inflight-chat',)).fetchone()[0] == 1
