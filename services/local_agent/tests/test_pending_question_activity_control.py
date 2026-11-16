"""Content-free activity requests while an exact learning question is pending."""
import json
import base64
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from home_study_agent.voice_control_intent import VoiceControlIntent
from home_study_agent.app import create_app
from home_study_agent.local_media_ai import LocalMediaAI
from home_study_agent.model_gateway import ModelGateway
from test_companion_goal_task_arrival_http import task_case, start_goal
from test_goal_roadmap_execution import execution, goal_db, exact_goal_item
from test_voice_runtime_binding import interpret, wav_file


POLITE = '能带我活动一小会儿吗？'


def decision(quote=POLITE, kind='movement', **extra):
    return json.dumps({'role': 'control', 'control': 'companion.start_activity',
        'quote': quote, 'confidence': .99, 'activity_type': kind, **extra}, ensure_ascii=False)


@pytest.mark.parametrize('kind', ['movement', 'interactive_story', 'word_chain', 'riddle', 'tidy_challenge'])
def test_semantic_activity_enum_allows_polite_request(kind):
    result = VoiceControlIntent.parse(decision(kind=kind), POLITE)
    assert result == {'status': 'control', 'control': 'companion.start_activity', 'activity_type': kind}


@pytest.mark.parametrize('extra', [{'activity_type': 'arbitrary'}, {'topic': 'private question'},
    {'opening': 'model text'}, {'plan_item_id': 'other'}, {'quote': 'different utterance'}])
def test_activity_schema_cannot_supply_business_arguments(extra):
    payload = json.loads(decision())
    payload.update(extra)
    with pytest.raises(ValueError):
        VoiceControlIntent.parse(json.dumps(payload), POLITE)


@pytest.mark.parametrize('quote', ['不要带我活动。', '他说“带我活动一下”，是什么意思？',
    '你有带我活动的功能吗？', '假如我让你带我活动，会怎样？'])
def test_model_control_label_does_not_override_explicit_non_request(quote):
    assert VoiceControlIntent.parse(decision(quote), quote)['status'] != 'control'


@pytest.mark.parametrize('role', ['question', 'mixed', 'unknown', 'answer'])
def test_non_command_roles_never_create_activity(role):
    value = {'role': role, 'control': None, 'quote': POLITE, 'confidence': .99}
    assert VoiceControlIntent.parse(json.dumps(value), POLITE)['status'] == 'non_control'


def capture_quote(client, settings, monkeypatch, sid, child, prompt, quote, key, *, focus=None):
    monkeypatch.setattr(LocalMediaAI, '_recognize_wav', lambda *a, **k: (quote, 1000, 'fixture', False))
    path = settings.data_dir / 'synthetic-activity.wav'
    wav_file(path)
    result = client.post(f'/v1/sessions/{sid}/asr', json={
        'wav_base64': base64.b64encode(path.read_bytes()).decode(), 'purpose': 'agent_command',
        'client_action_id': key, 'capture_reference': {'child_id': child, 'question_reference': {
            'prompt_id': prompt['prompt_id'], 'item_version': prompt['item_version']},
            **({'activity_reference': focus} if focus else {})}})
    assert result.status_code == 200, result.text
    return result.json()['voice_input_id']


@pytest.mark.parametrize('following', ['我想让一位小探险家当主角。', '嗯', '好的', '对'])
def test_polite_request_keeps_original_and_creative_next_turn(task_case, monkeypatch, following):
    case, child, settings = task_case
    db, sid, pid, _, _ = case
    settings = replace(settings, model_provider='qwen', model_api_key='synthetic-03504f547a5a2fc55aec',
        model_name='isolated-model', voice_intent_model_name='isolated-model')
    requests = []
    quote = '能和我一起编个小故事吗？'
    def transport(request):
        body = json.loads(request.content)
        requests.append(body)
        if '只分类当前原话' in body['messages'][0]['content']:
            data = json.loads(body['messages'][-1]['content'])
            assert set(data) == {'transcript', 'state'}
            assert all(type(v) is bool for v in data['state'].values())
            content = (decision(quote, 'interactive_story') if data['transcript'] == quote else
                json.dumps({'role': 'activity_reply', 'control': None, 'quote': following, 'confidence': .99}))
        else:
            content = '这位小探险家走到一条分岔路前。你想让主角先去树林还是河边？'
        return httpx.Response(200, json={'choices': [{'message': {'content': content}}]})
    def app():
        return create_app(settings, model_gateway=ModelGateway(settings, transport=httpx.MockTransport(transport)))
    with TestClient(app()) as client:
        prompt = start_goal(client, case, child)
        vid = capture_quote(client, settings, monkeypatch, sid, child, prompt, quote, 'activity-request-audio')
        turn = interpret(client, sid, quote, 'activity-request', vid)
        assert turn['action']['type'] == 'start_companion_activity', turn
        url = f"/v1/agent-turns/{turn['agent_turn_id']}/execute"
        result = client.post(url)
        assert result.status_code == 200, result.text
        opening = result.json()['execution']['response_text']
        assert opening == VoiceControlIntent.activity_arguments('interactive_story')['opening']
        assert client.post(url).json()['execution'] == result.json()['execution']
        with db.connect() as c:
            focus = VoiceControlIntent.activity_focus(c, sid, child)
        assert focus and focus['last_turn_id'] == turn['agent_turn_id']
        vid2 = capture_quote(client, settings, monkeypatch, sid, child, prompt, following, 'activity-reply-audio', focus=focus)
        reply = interpret(client, sid, following, 'activity-reply', vid2)
        assert reply['action']['source'] == 'cloud_companion', reply
        assert '分岔路' in reply['response_text']
        with db.connect() as c:
            activity = c.execute('SELECT * FROM companion_activity').fetchone()
            assert activity['turn_count'] == 1 and activity['max_turns'] == 6
            assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?', (pid,)).fetchone()[0] == 'paused'
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
            assert c.execute('SELECT status FROM diagnostic_prompt WHERE prompt_id=?', (prompt['prompt_id'],)).fetchone()[0] == 'waiting_answer'
        for request in requests:
            wire = json.dumps(request['messages'], ensure_ascii=False)
            assert all(x not in wire for x in (pid, vid, prompt['prompt_id'], prompt['diagnostic_item_id'], prompt['item']['prompt']))
        assert len(requests) == 3
    with TestClient(app()) as client:
        replay = interpret(client, sid, following, 'activity-reply', vid2)
        assert replay['agent_turn_id'] == reply['agent_turn_id']
        assert len(requests) == 3


@pytest.mark.parametrize('mode', ['short', 'missing_reference', 'denied', 'mixed', 'quoted', 'return_question',
                                 'ended_late', 'changed_source', 'changed_round', 'withdraw_during_reply', 'storage_failure',
                                 'stale_restored', 'cached_consent_regranted', 'withdraw_after_cache'])
def test_activity_focus_never_grades_original_or_advances_another_round(task_case, monkeypatch, mode):
    from home_study_agent.agent_runtime import AgentRuntimeStore
    case, child, settings = task_case
    db, sid, pid, _, _ = case
    settings = replace(settings, model_provider='qwen', model_api_key='synthetic-03504f547a5a2fc55aec',
        model_name='isolated-model', voice_intent_model_name='isolated-model')
    requests = []
    opening_request = '能和我玩一小轮猜谜吗？'
    following = {'denied': '不要继续了。', 'mixed': '继续猜谜，还要回答刚才那题。',
                 'quoted': '他说“五”是什么意思？', 'return_question': '我要改回刚才的原题。'}.get(mode, '五')
    def transport(request):
        body = json.loads(request.content)
        requests.append(body)
        if '只分类当前原话' in body['messages'][0]['content']:
            data = json.loads(body['messages'][-1]['content'])
            assert set(data) == {'transcript', 'state'}
            if data['transcript'] == opening_request:
                content = decision(opening_request, 'riddle')
            else:
                content = json.dumps({'role': 'mixed' if mode == 'mixed' else 'activity_reply',
                                      'control': None, 'quote': following, 'confidence': .99})
        else:
            if mode == 'withdraw_during_reply':
                AgentRuntimeStore(db).end_companion_activity(sid)
            content = '这个猜法很有意思。再想想，它是日常会用到的小物件。'
        return httpx.Response(200, json={'choices': [{'message': {'content': content}}]})
    app = create_app(settings, model_gateway=ModelGateway(settings, transport=httpx.MockTransport(transport)))
    with TestClient(app, raise_server_exceptions=False) as client:
        prompt = start_goal(client, case, child)
        vid = capture_quote(client, settings, monkeypatch, sid, child, prompt, opening_request, 'start-audio')
        first = interpret(client, sid, opening_request, 'start-activity', vid)
        started = client.post(f"/v1/agent-turns/{first['agent_turn_id']}/execute")
        assert started.status_code == 200, started.text
        with db.connect() as c:
            focus = VoiceControlIntent.activity_focus(c, sid, child)
            original_activity = dict(c.execute('SELECT * FROM companion_activity').fetchone())
        assert focus
        if mode == 'stale_restored':
            AgentRuntimeStore(db).end_companion_activity(sid)
        vid2 = capture_quote(client, settings, monkeypatch, sid, child, prompt, following, 'reply-audio',
                             focus=None if mode == 'missing_reference' else focus)
        if mode == 'stale_restored':
            with db.connect() as c:
                assert c.execute("SELECT activity_validation FROM voice_capture_binding WHERE voice_input_id=?", (vid2,)).fetchone()[0] == 'stale'
                c.execute('UPDATE companion_activity SET ' + ','.join(f'{key}=?' for key in original_activity) + ' WHERE activity_id=?',
                          (*original_activity.values(), focus['activity_id']))
                c.commit()
        if mode == 'ended_late':
            AgentRuntimeStore(db).end_companion_activity(sid)
        elif mode in {'changed_source', 'changed_round'}:
            with db.connect() as c:
                if mode == 'changed_source':
                    c.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id=?", (prompt['diagnostic_item_id'],))
                else:
                    c.execute("UPDATE companion_activity SET updated_at=updated_at || 'changed' WHERE activity_id=?", (focus['activity_id'],))
                c.commit()
        if mode in {'storage_failure', 'cached_consent_regranted'}:
            with db.connect() as c:
                c.execute("""CREATE TRIGGER fail_activity_reply BEFORE INSERT ON agent_turn
                    WHEN NEW.client_action_id='second-activity-turn'
                    BEGIN SELECT RAISE(ABORT,'isolated reply save failure'); END""")
                c.commit()
            failed = client.post(f'/v1/sessions/{sid}/agent-turn', json={'transcript': following,
                'client_action_id': 'second-activity-turn', 'cloud_text_allowed': True, 'voice_input_id': vid2})
            assert failed.status_code == 500, failed.text
            with db.connect() as c:
                assert c.execute('SELECT turn_count FROM companion_activity WHERE activity_id=?', (focus['activity_id'],)).fetchone()[0] == 0
                assert c.execute("SELECT COUNT(*) FROM agent_turn WHERE client_action_id='second-activity-turn'").fetchone()[0] == 0
                c.execute('DROP TRIGGER fail_activity_reply')
                c.commit()
            if mode == 'cached_consent_regranted':
                from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
                service = PrivacyLifecycleService(db, settings.data_dir)
                service.update_preferences({'cloud_model_text_allowed': False})
                service.update_preferences({'cloud_model_text_allowed': True})
        if mode == 'withdraw_after_cache':
            with db.connect() as c:
                c.execute("""CREATE TRIGGER end_after_reply_cache AFTER UPDATE OF execution_receipt_json
                    ON voice_control_intent_request WHEN NEW.execution_receipt_json IS NOT NULL
                    BEGIN UPDATE companion_activity SET status='completed',end_reason='child_requested'; END""")
                c.commit()
            rejected = client.post(f'/v1/sessions/{sid}/agent-turn', json={'transcript': following,
                'client_action_id': 'second-activity-turn', 'cloud_text_allowed': True, 'voice_input_id': vid2})
            assert rejected.status_code == 409, rejected.text
            with db.connect() as c:
                assert c.execute('SELECT turn_count FROM companion_activity WHERE activity_id=?', (focus['activity_id'],)).fetchone()[0] == 0
                assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
                assert c.execute("SELECT COUNT(*) FROM agent_turn WHERE client_action_id='second-activity-turn'").fetchone()[0] == 0
            return
        heard = interpret(client, sid, following, 'second-activity-turn', vid2)
        assert heard['action']['type'] == 'none', heard
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
            assert c.execute("SELECT COUNT(*) FROM agent_turn WHERE json_extract(action_json,'$.pending_answer_candidate') IS NOT NULL").fetchone()[0] == 0
            assert c.execute('SELECT turn_count FROM companion_activity WHERE activity_id=?', (focus['activity_id'],)).fetchone()[0] == (1 if mode in {'short', 'storage_failure'} else 0)
            assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?', (pid,)).fetchone()[0] == 'paused'
        if mode in {'short', 'storage_failure'}:
            assert heard['action']['source'] == 'cloud_companion'
            assert len(requests) == 3
        elif mode not in {'withdraw_during_reply', 'cached_consent_regranted'}:
            assert len(requests) <= 2
        if mode == 'cached_consent_regranted':
            assert len(requests) == 3
        for body in requests:
            text = json.dumps(body['messages'], ensure_ascii=False)
            assert all(value not in text for value in (prompt['prompt_id'], prompt['item']['prompt'], pid, focus['activity_id']))


def test_actual_activity_turn_limit_then_natural_resume_keeps_same_prompt(task_case, monkeypatch):
    case, child, settings = task_case
    db, sid, pid, _, _ = case
    settings = replace(settings, model_provider='qwen', model_api_key='synthetic-03504f547a5a2fc55aec',
        model_name='isolated-model', voice_intent_model_name='isolated-model')
    resume_quote = '我想回到刚才暂停的学习。'
    requests = []
    def transport(request):
        body = json.loads(request.content)
        requests.append(body)
        if '只分类当前原话' in body['messages'][0]['content']:
            quote = json.loads(body['messages'][-1]['content'])['transcript']
            if quote == POLITE:
                content = decision(quote, 'movement')
            elif quote == resume_quote:
                content = json.dumps({'role': 'control', 'control': 'companion.activity_feedback', 'quote': quote,
                    'confidence': .99, 'activity_feedback': {'operation': 'request_resume'}})
            else:
                content = json.dumps({'role': 'activity_reply', 'control': None, 'quote': quote, 'confidence': .99})
        else:
            content = '轻轻放松肩膀，舒服就好，不需要勉强自己。'
        return httpx.Response(200, json={'choices': [{'message': {'content': content}}]})
    def app():
        return create_app(settings, model_gateway=ModelGateway(settings, transport=httpx.MockTransport(transport)))
    with TestClient(app()) as client:
        prompt = start_goal(client, case, child)
        vid = capture_quote(client, settings, monkeypatch, sid, child, prompt, POLITE, 'limit-start-audio')
        start = interpret(client, sid, POLITE, 'limit-start', vid)
        result = client.post(f"/v1/agent-turns/{start['agent_turn_id']}/execute")
        assert result.status_code == 200, result.text
        for index, quote in enumerate(('我准备好了。', '我轻轻转了转肩膀。')):
            focus = client.get(f'/v1/sessions/{sid}/voice/activity-focus').json()['activity_focus']
            assert focus
            vid = capture_quote(client, settings, monkeypatch, sid, child, prompt, quote, f'limit-voice-{index}', focus=focus)
            reply = interpret(client, sid, quote, f'limit-reply-{index}', vid)
            assert reply['action']['source'] == 'cloud_companion', reply
        with db.connect() as c:
            row = c.execute('SELECT * FROM companion_activity').fetchone()
            assert (row['status'], row['turn_count'], row['end_reason']) == ('completed', 2, 'turn_limit')
            assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?', (pid,)).fetchone()[0] == 'paused'
        assert client.get(f'/v1/sessions/{sid}/voice/activity-focus').json()['activity_focus'] is None
        vid = capture_quote(client, settings, monkeypatch, sid, child, prompt, resume_quote, 'return-original-voice')
        resume = interpret(client, sid, resume_quote, 'return-original', vid)
        assert resume['runtime_capability'] == 'companion.activity_feedback', resume
        url = f"/v1/agent-turns/{resume['agent_turn_id']}/execute"
        result = client.post(url)
        assert result.status_code == 200, result.text
        assert result.json()['execution']['result']['activity']['resume']['status'] == 'confirmed'
        with db.connect() as c:
            assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?', (pid,)).fetchone()[0] == 'active'
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id=? AND event_type='resumed'", (pid,)).fetchone()[0] == 1
        original = client.get(f'/v1/sessions/{sid}/diagnostic-prompt', params={'prompt_id': prompt['prompt_id']}).json()
        assert original['status'] == 'waiting_answer' and original['prompt']['item_version'] == prompt['item_version']
        for body in requests:
            wire = json.dumps(body['messages'], ensure_ascii=False)
            assert prompt['item']['prompt'] not in wire and prompt['prompt_id'] not in wire and pid not in wire
        saved_requests = len(requests)
    with TestClient(app()) as client:
        assert client.post(url).json()['execution'] == result.json()['execution']
        assert len(requests) == saved_requests
