"""Model-selected public intent; real local plan, original-paper and restart path."""
import json
import os
import subprocess
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.voice_control_intent import VoiceControlIntent
from home_study_agent.model_gateway import ModelGateway
from test_mistake_review_task_entry import paper_task
from test_primary_mixed_voice_journey import MixedGateway
from test_task_resume_voice_http import hear
from test_mistake_voice_journey import ok, focus
from test_task_start_choices_http import choices, capture_choice
from test_voice_runtime_binding import interpret


def selector(quote, subject='english', kind='review', source='mistake_review'):
    return {'subject': subject, 'task_kind': kind, 'source_type': source, 'target_quote': quote}


class StartGateway(MixedGateway):
    def __init__(self, selected):
        super().__init__('')
        self.selected = selected

    async def complete(self, request):
        result = await super().complete(request)
        if request.request_type != 'agent_plan':
            value = json.loads(result.content)
            value['start_selector'] = self.selected
            result = replace(result, content=json.dumps(value, ensure_ascii=False))
        return result


class LiveStartGateway(MixedGateway):
    """Opt-in synthetic-data provider check; no application secrets in artifacts."""
    def __init__(self, config, live_plan=False):
        super().__init__('')
        self.base_config = config
        self.receipts = []
        self.live_plan = live_plan

    async def complete(self, request):
        if request.request_type == 'agent_plan' and not self.live_plan:
            return await super().complete(request)
        body = json.loads(request.messages[-1]['content'])
        if request.request_type == 'voice_interaction':
            assert set(body) == {'transcript', 'state'}
            assert all(type(value) is bool for value in body['state'].values())
        else:
            assert 'child_utterance' in body
        serialized = json.dumps(request.messages, ensure_ascii=False)
        assert 'paper-review' not in serialized and '复习原错题' not in serialized
        self.requests.append(request)
        secret = subprocess.run(['security', 'find-generic-password', '-s', 'hsc-deepseek-api-key', '-w'],
                                capture_output=True, text=True, check=True).stdout.strip()
        gateway = ModelGateway(replace(self.base_config, model_provider='deepseek',
            model_api_key=secret, model_base_url='https://api.deepseek.com', model_name='deepseek-flash',
            voice_intent_model_name='deepseek-flash', fallback_model_api_key=None, model_timeout_seconds=8))
        try:
            result = await gateway.complete(request)
            self.receipts.append({'request_type': request.request_type, 'model': result.model, 'latency_ms': result.latency_ms,
                'prompt_tokens': result.prompt_tokens, 'completion_tokens': result.completion_tokens,
                'decision': json.loads(result.content)})
            return result
        finally:
            await gateway.close()


@pytest.mark.skipif(os.environ.get('HSC_LIVE_START_SELECTOR') != '1', reason='explicit synthetic-data live API opt-in')
@pytest.mark.parametrize('quote', ['数学作业先不做，我想先做英语错题',
    '不要做数学作业，咱们先处理英语错题吧', '能陪我先把英语错题过一遍吗？'])
def test_live_model_slots_drive_original_paper(paper_task, monkeypatch, quote):
    config, db, sid, mistake, _ = paper_task
    gateway = LiveStartGateway(config)
    with db.connect() as c:
        c.execute("UPDATE plan_item SET position=0 WHERE plan_item_id='later-school'")
        c.commit()
    with TestClient(create_app(config, model_gateway=gateway)) as client:
        turn = hear(client, config, monkeypatch, gateway, sid, None, quote, 'live-semantic', 'task.start')
        print('SYNTHETIC_LIVE_RESULT', json.dumps(gateway.receipts, ensure_ascii=False))
        assert turn['runtime_capability'] == 'task.start', turn.get('response_text')
        result = ok(client.post(f'/v1/agent-turns/{turn["agent_turn_id"]}/execute'))['execution']['result']
        assert result['plan_item_id'] == 'paper-review'
        assert result['mistake_dialogue']['mistake_id'] == mistake


@pytest.mark.skipif(os.environ.get('HSC_LIVE_START_SELECTOR') != '1', reason='explicit synthetic-data live API opt-in')
@pytest.mark.parametrize('quote,starts', [('能不能先做英语错题？', True),
    ('能不能不要做英语错题', False), ('英语错题怎么解', False)])
def test_live_polite_request_and_nonexecuting_question(paper_task, monkeypatch, quote, starts):
    config, db, sid, mistake, _ = paper_task
    gateway = LiveStartGateway(config)
    with TestClient(create_app(config, model_gateway=gateway)) as client:
        turn = hear(client, config, monkeypatch, gateway, sid, None, quote, 'live-boundary', 'task.start')
        print('SYNTHETIC_LIVE_RESULT', json.dumps(gateway.receipts, ensure_ascii=False))
        assert bool(gateway.receipts)
        result = ok(client.post(f'/v1/agent-turns/{turn["agent_turn_id"]}/execute'))['execution']['result']
        if starts:
            assert result['plan_item_id'] == 'paper-review'
            assert result['mistake_dialogue']['mistake_id'] == mistake
        else:
            with db.connect() as c:
                assert not c.execute("SELECT 1 FROM plan_item WHERE status='active'").fetchone()


@pytest.mark.skipif(os.environ.get('HSC_LIVE_START_SELECTOR') != '1', reason='explicit synthetic-data live API opt-in')
def test_live_planner_and_selector_execute_original_task(paper_task, monkeypatch):
    config, db, sid, mistake, _ = paper_task
    gateway = LiveStartGateway(config, live_plan=True)
    with db.connect() as c:
        c.execute("UPDATE plan_item SET position=0 WHERE plan_item_id='later-school'")
        c.commit()
    with TestClient(create_app(config, model_gateway=gateway)) as client:
        turn = hear(client, config, monkeypatch, gateway, sid, None,
            '数学作业先不做，我想先做英语错题', 'live-entire-start', 'task.start')
        print('SYNTHETIC_LIVE_RESULT', json.dumps(gateway.receipts, ensure_ascii=False))
        assert gateway.receipts[0]['request_type'] == 'agent_plan'
        # Legacy planners need a separate selector; typed planners carry it in
        # their first response and must not be forced through another call.
        typed = gateway.receipts[0]['decision']['next_step']['arguments'].get('start_selector')
        assert [entry['request_type'] for entry in gateway.receipts] == (
            ['agent_plan'] if typed else ['agent_plan', 'voice_interaction'])
        assert turn['runtime_capability'] == 'task.start', turn.get('response_text')
        result = ok(client.post(f'/v1/agent-turns/{turn["agent_turn_id"]}/execute'))['execution']['result']
        assert result['plan_item_id'] == 'paper-review'
        assert result['mistake_dialogue']['mistake_id'] == mistake


@pytest.mark.parametrize('quote,span', [
    ('数学作业先不做，我想先做英语错题', '我想先做英语错题'),
    ('数学等会再做，咱们先处理英语错题吧', '咱们先处理英语错题吧'),
    ('能陪我先把英语错题过一遍吗？', '能陪我先把英语错题过一遍吗？'),
    ('能不能先做英语错题？', '能不能先做英语错题？'),
    ('先做英语错题，再做数学作业', '先做英语错题'),
])
def test_semantic_destination_enters_original_paper_not_queue_first(paper_task, monkeypatch, quote, span):
    config, db, sid, mistake, _ = paper_task
    gateway = StartGateway(selector(span))
    with db.connect() as c:
        c.execute("UPDATE plan_item SET position=0 WHERE plan_item_id='later-school'")
        c.commit()
    with TestClient(create_app(config, model_gateway=gateway)) as client:
        turn = hear(client, config, monkeypatch, gateway, sid, None, quote, 'semantic-start', 'task.start')
        assert turn['runtime_capability'] == 'task.start', turn
        url = f'/v1/agent-turns/{turn["agent_turn_id"]}/execute'
        result = ok(client.post(url))['execution']['result']
        assert result['plan_item_id'] == 'paper-review'
        assert result['mistake_dialogue']['mistake_id'] == mistake
        assert focus(client, sid)['state']['submitted_answer'] == 'B'
        with db.connect() as c:
            assert c.execute("SELECT status FROM plan_item WHERE plan_item_id='later-school'").fetchone()[0] == 'pending'
            manifest = json.loads(c.execute('SELECT context_manifest_json FROM agent_plan WHERE agent_turn_id=?', (turn['agent_turn_id'],)).fetchone()[0])
            semantic = manifest['task_start_arrival']['semantic_start']
            assert semantic['selector'] == gateway.selected
            assert semantic['origin']['items'][0]['plan_item_id'] == 'later-school'
        for request in gateway.requests:
            body = request.messages[-1]['content']
            assert 'paper-review' not in body and '复习原错题' not in body and mistake not in body
    with TestClient(create_app(config, model_gateway=gateway)) as client:
        ok(client.post(url))
        with db.connect() as c:
            assert c.execute("SELECT count(*) FROM plan_item_event WHERE event_type='started'").fetchone()[0] == 1
            assert c.execute("SELECT status FROM plan_item WHERE plan_item_id='paper-review'").fetchone()[0] == 'paused'


@pytest.mark.parametrize('quote,selected', [
    ('不要做英语错题', selector('做英语错题')),
    ('英语先不做，开始数学作业', selector('开始数学作业')),
    ('开始数学作业，也开始英语错题', selector('也开始英语错题')),
    ('老师说开始英语错题', selector('开始英语错题')),
    ('如果开始英语错题会怎么样', selector('开始英语错题')),
    ('开始英语错题', selector('开始数学错题')),
    ('先做那份', selector('先做那份', None, None, None)),
    ('先别开始，我想做英语错题', selector('我想做英语错题')),
    ('英语先不做，我想做英语错题', selector('我想做英语错题')),
    ('开始英语阅读', selector('开始英语阅读', 'english', 'listening', None)),
    ('能不能不要做英语错题', selector('能不能不要做英语错题')),
    ('先做数学作业，再做英语错题', selector('再做英语错题')),
])
def test_public_model_slots_cannot_strip_negation_or_invent_destination(quote, selected):
    payload = {'role': 'control', 'control': 'task.start', 'quote': quote,
               'confidence': .99, 'start_selector': selected}
    try:
        result = VoiceControlIntent.parse(json.dumps(payload), quote)
    except ValueError:
        return
    assert result['status'] != 'control'


@pytest.mark.parametrize('change', ['source', 'add_match', 'cancel', 'new_turn'])
def test_semantic_start_revalidates_original_authority_at_commit(paper_task, monkeypatch, change):
    config, db, sid, _, _ = paper_task
    quote = '数学先不做，我想先做英语错题'
    gateway = StartGateway(selector('我想先做英语错题'))
    with TestClient(create_app(config, model_gateway=gateway)) as client:
        turn = hear(client, config, monkeypatch, gateway, sid, None, quote, 'semantic-race', 'task.start')
        assert turn['runtime_capability'] == 'task.start', turn
        if change == 'new_turn':
            hear(client, config, monkeypatch, gateway, sid, None, '先别开始', 'new-hold', 'task.start')
        else:
            with db.connect() as c:
                if change == 'source':
                    c.execute("UPDATE plan_item SET source_ref='different' WHERE plan_item_id='paper-review'")
                elif change == 'add_match':
                    c.execute("UPDATE plan_item SET subject='英语',source_type='mistake_review',task_type='review' WHERE plan_item_id='later-school'")
                else:
                    c.execute("UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id=?", (turn['agent_turn_id'],))
                c.commit()
        assert client.post(f'/v1/agent-turns/{turn["agent_turn_id"]}/execute').status_code == 409
        with db.connect() as c:
            assert not c.execute("SELECT 1 FROM plan_item WHERE status='active'").fetchone()


def test_semantic_ambiguity_preserves_local_list_and_capture_choice(choices, monkeypatch):
    config, db, sid, _ = choices
    gateway = StartGateway(selector('先做英语作业', 'english', 'written', 'school_homework'))
    with db.connect() as c:
        c.execute("UPDATE plan_item SET subject='英语'")
        c.commit()
    with TestClient(create_app(config, model_gateway=gateway)) as client:
        quote = '数学晚点再做，先做英语作业'
        turn = hear(client, config, monkeypatch, gateway, sid, None, quote, 'semantic-offer', 'task.start')
        assert turn['action']['source'] == 'task_navigation_clarification'
        assert '第2项' in turn['response_text']
        ok(client.post(f'/v1/agent-turns/{turn["agent_turn_id"]}/execute'))
        reference = ok(client.get(f'/v1/sessions/{sid}/task-start-choice'))['reference']
        assert reference
    gateway.configured = False
    with TestClient(create_app(config, model_gateway=gateway)) as client:
        assert ok(client.get(f'/v1/sessions/{sid}/task-start-choice'))['reference'] == reference
        voice = ok(capture_choice(client, choices, monkeypatch, reference))['voice_input_id']
        turn = interpret(client, sid, '第二项', 'semantic-offer-choice', voice)
        result = ok(client.post(f'/v1/agent-turns/{turn["agent_turn_id"]}/execute'))
        assert result['execution']['result']['plan_item_id'] == 'later-school'


@pytest.mark.parametrize('failure', ['exception', 'malformed', 'different_control'])
def test_secondary_intent_failure_never_falls_back_to_queue_start(paper_task, monkeypatch, failure):
    config, db, sid, _, _ = paper_task
    gateway = StartGateway(selector('先做英语错题'))
    original = gateway.complete
    async def broken(request):
        result = await original(request)
        if request.request_type == 'agent_plan':
            return result
        if failure == 'exception':
            raise OSError('synthetic provider interruption')
        if failure == 'malformed':
            return replace(result, content='not json')
        return replace(result, content=json.dumps({'role': 'control', 'control': 'task.pause',
            'quote': '先做英语错题', 'confidence': .99}))
    monkeypatch.setattr(gateway, 'complete', broken)
    with TestClient(create_app(config, model_gateway=gateway)) as client:
        turn = hear(client, config, monkeypatch, gateway, sid, None, '先做英语错题', 'semantic-failure', 'task.start')
        assert turn['runtime_capability'] == 'conversation.respond'
        if failure in {'exception', 'malformed'}:
            assert turn['action']['understanding_failure_code'] == 'CONTROL_MODEL_FAILED'
            assert '服务暂时不可用' in turn['response_text']
        ok(client.post(f'/v1/agent-turns/{turn["agent_turn_id"]}/execute'))
        with db.connect() as c:
            assert not c.execute("SELECT 1 FROM plan_item WHERE status='active'").fetchone()
