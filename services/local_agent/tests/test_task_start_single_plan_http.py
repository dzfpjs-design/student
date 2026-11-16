"""One model response to exact local work, with durable provider provenance."""
import json
import os

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.model_gateway import ModelResult
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from test_cloud_first_agent import Planner
from test_mistake_review_task_entry import paper_task
from test_mistake_voice_journey import ok
from test_primary_mixed_voice_journey import MixedGateway
from test_task_resume_voice_http import hear
from test_task_start_semantics_http import selector, LiveStartGateway


QUOTE = '数学作业先不做，我想先做英语错题'
SELECTED = selector('我想先做英语错题')


class SinglePlanner(MixedGateway):
    def __init__(self, selected=None, hook=None, confidence=.95, malformed_quote=False):
        super().__init__('')
        self.selected = SELECTED if selected is None else selected
        self.hook = hook
        self.confidence = confidence
        self.malformed_quote = malformed_quote

    async def complete(self, request):
        self.requests.append(request)
        assert request.request_type == 'agent_plan', 'typed plan must not invoke a second classifier'
        if self.hook:
            self.hook(request)
        payload = Planner('task.start', {'start_selector': self.selected}, self.quote).payload
        payload['confidence'] = self.confidence
        if self.malformed_quote:
            payload['utterance_binding']['quote'] = None
        return ModelResult(json.dumps(payload, ensure_ascii=False), 'test', 'synthetic-single-planner', 1)


def start(client, config, monkeypatch, gateway, sid, quote=QUOTE):
    turn = hear(client, config, monkeypatch, gateway, sid, None, quote, 'single-plan-start', 'task.start')
    assert turn['runtime_capability'] == 'task.start', turn.get('response_text')
    return turn, f'/v1/agent-turns/{turn["agent_turn_id"]}/execute'


def test_single_planner_enters_original_paper_and_replays_after_restart(paper_task, monkeypatch):
    config, db, sid, mistake, _ = paper_task
    gateway = SinglePlanner()
    with db.connect() as c:
        c.execute("UPDATE plan_item SET position=0 WHERE plan_item_id='later-school'")
        c.commit()
    with TestClient(create_app(config, model_gateway=gateway)) as client:
        turn, url = start(client, config, monkeypatch, gateway, sid)
        assert len(gateway.requests) == 1
        result = ok(client.post(url))['execution']['result']
        assert result['plan_item_id'] == 'paper-review'
        assert result['mistake_dialogue']['mistake_id'] == mistake
        with db.connect() as c:
            record = c.execute("SELECT decision_json FROM voice_control_intent_request WHERE json_extract(decision_json,'$.model_call_id') IS NOT NULL").fetchone()
            decision = json.loads(record[0])
            call_id = decision['model_call_id']
            receipt = json.loads(c.execute('SELECT context_manifest_json FROM model_call WHERE call_id=?', (call_id,)).fetchone()[0])['task_start_selection']
            assert receipt['selector'] == SELECTED and receipt['arrival_id']
            assert c.execute("SELECT status FROM plan_item WHERE plan_item_id='later-school'").fetchone()[0] == 'pending'
        for request in gateway.requests:
            body = json.dumps(request.messages, ensure_ascii=False)
            assert 'paper-review' not in body and '复习原错题' not in body and mistake not in body
    with TestClient(create_app(config, model_gateway=gateway)) as client:
        ok(client.post(url))
        assert len(gateway.requests) == 1
        with db.connect() as c:
            assert c.execute("SELECT count(*) FROM plan_item_event WHERE event_type='started'").fetchone()[0] == 1
            assert c.execute("SELECT status FROM plan_item WHERE plan_item_id='paper-review'").fetchone()[0] == 'paused'


@pytest.mark.parametrize('change', ['selector', 'arrival', 'call_failed', 'consent', 'source', 'missing_call'])
def test_single_plan_rechecks_model_receipt_and_live_authority(paper_task, monkeypatch, change):
    config, db, sid, _, _ = paper_task
    gateway = SinglePlanner()
    with TestClient(create_app(config, model_gateway=gateway)) as client:
        _, url = start(client, config, monkeypatch, gateway, sid)
        if change == 'consent':
            privacy = PrivacyLifecycleService(db, config.data_dir)
            privacy.update_preferences({'cloud_model_text_allowed': False})
            privacy.update_preferences({'cloud_model_text_allowed': True})
        else:
            with db.connect() as c:
                call_id = json.loads(c.execute("SELECT decision_json FROM voice_control_intent_request WHERE json_extract(decision_json,'$.model_call_id') IS NOT NULL").fetchone()[0])['model_call_id']
                if change == 'missing_call':
                    c.execute("UPDATE voice_control_intent_request SET decision_json=json_remove(decision_json,'$.model_call_id') WHERE json_extract(decision_json,'$.model_call_id')=?", (call_id,))
                elif change == 'call_failed':
                    c.execute("UPDATE model_call SET status='failed' WHERE call_id=?", (call_id,))
                elif change == 'selector':
                    c.execute("UPDATE model_call SET context_manifest_json=json_set(context_manifest_json,'$.task_start_selection.selector.subject','math') WHERE call_id=?", (call_id,))
                elif change == 'arrival':
                    c.execute("UPDATE model_call SET context_manifest_json=json_set(context_manifest_json,'$.task_start_selection.arrival_id','other-input') WHERE call_id=?", (call_id,))
                else:
                    c.execute("UPDATE plan_item SET source_ref='other-paper' WHERE plan_item_id='paper-review'")
                c.commit()
        response = client.post(url)
        assert response.status_code == 409, response.text
        with db.connect() as c:
            assert not c.execute("SELECT 1 FROM plan_item WHERE status='active'").fetchone()
        assert len(gateway.requests) == 1


@pytest.mark.parametrize('bad', [
    {**SELECTED, 'target_quote': '做数学作业'},
    {**SELECTED, 'subject': 'math'},
    {**SELECTED, 'plan_item_id': 'paper-review'},
])
def test_invalid_planner_slots_never_gain_a_second_guess_or_execution(paper_task, monkeypatch, bad):
    config, db, sid, _, _ = paper_task
    gateway = SinglePlanner(bad)
    with TestClient(create_app(config, model_gateway=gateway)) as client:
        turn = hear(client, config, monkeypatch, gateway, sid, None, QUOTE, 'invalid-single-plan', 'task.start')
        assert turn.get('runtime_capability') != 'task.start'
        assert turn['action']['type'] == 'none'
        assert len(gateway.requests) == 1
        if turn.get('runtime_capability'):
            ok(client.post(f'/v1/agent-turns/{turn["agent_turn_id"]}/execute'))
        with db.connect() as c:
            assert not c.execute("SELECT 1 FROM plan_item WHERE status='active'").fetchone()
            assert c.execute("SELECT verification_status FROM model_call WHERE purpose='agent_plan' ORDER BY rowid DESC LIMIT 1").fetchone()[0] == 'rejected'


@pytest.mark.parametrize('options,code', [
    ({'confidence': .7}, 'MODEL_AGENT_PLAN_LOW_CONFIDENCE'),
    ({'malformed_quote': True}, 'MODEL_TASK_START_PLANNER_CONTROL_START_SELECTOR_INVALID'),
])
def test_single_plan_preserves_control_confidence_and_rejects_malformed_quote(paper_task, monkeypatch, options, code):
    config, db, sid, _, _ = paper_task
    gateway = SinglePlanner(**options)
    with TestClient(create_app(config, model_gateway=gateway)) as client:
        turn = hear(client, config, monkeypatch, gateway, sid, None, QUOTE, 'invalid-plan-authority', 'task.start')
        assert turn.get('runtime_capability') != 'task.start'
        assert turn['action']['type'] == 'none'
        assert len(gateway.requests) == 1
        with db.connect() as c:
            row = c.execute("SELECT error_code,verification_status FROM model_call WHERE purpose='agent_plan' ORDER BY rowid DESC LIMIT 1").fetchone()
            assert tuple(row) == (code, 'rejected')
            assert not c.execute("SELECT 1 FROM plan_item WHERE status='active'").fetchone()


@pytest.mark.skipif(os.environ.get('HSC_LIVE_START_SELECTOR') != '1', reason='explicit synthetic-data live API opt-in')
@pytest.mark.parametrize('quote', [QUOTE, '先做英语错题，数学作业等会再做'])
def test_real_single_plan_starts_original_work_without_second_model(paper_task, monkeypatch, quote):
    config, db, sid, mistake, _ = paper_task
    gateway = LiveStartGateway(config, live_plan=True)
    with db.connect() as c:
        c.execute("UPDATE plan_item SET position=0 WHERE plan_item_id='later-school'")
        c.commit()
    with TestClient(create_app(config, model_gateway=gateway)) as client:
        _, url = start(client, config, monkeypatch, gateway, sid, quote)
        print('SYNTHETIC_LIVE_RESULT', json.dumps(gateway.receipts, ensure_ascii=False))
        assert len(gateway.receipts) == 1 and gateway.receipts[0]['request_type'] == 'agent_plan'
        result = ok(client.post(url))['execution']['result']
        assert result['plan_item_id'] == 'paper-review'
        assert result['mistake_dialogue']['mistake_id'] == mistake
