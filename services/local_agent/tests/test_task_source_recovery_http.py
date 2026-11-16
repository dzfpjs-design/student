"""A missing original paper remains an owned task, not a timer or substitute."""
import json
import os

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from test_mistake_review_task_entry import paper_task
from test_mistake_voice_journey import ok
from test_task_resume_voice_http import hear as unbound_hear
from test_primary_mixed_voice_journey import MixedGateway
from test_cloud_first_agent import Planner
from test_task_start_semantics_http import LiveStartGateway
from test_task_start_choices_http import capture_choice
from test_voice_runtime_binding import interpret


def hear(client, config, monkeypatch, gateway, sid, prompt, quote, key, control):
    reference = ok(client.get(f'/v1/sessions/{sid}/task-start-choice'))['reference']
    if not reference:
        return unbound_hear(client, config, monkeypatch, gateway, sid, prompt, quote, key, control)
    gateway.quote, gateway.control = quote, control
    voice = ok(capture_choice(client, (config, None, sid, gateway), monkeypatch,
        reference, quote, key+'-voice'))['voice_input_id']
    return interpret(client, sid, quote, key, voice)


def gap(client, case, monkeypatch, key='source-gap', execute=True):
    config, db, sid, _, gateway = case
    turn = hear(client, config, monkeypatch, gateway, sid, None,
                '开始英语错题复习', key, 'task.start')
    assert turn.get('runtime_capability') == 'conversation.respond'
    assert turn['action']['type'] == 'none'
    assert turn['action']['task_source_gap']['snapshot']['item']['plan_item_id'] == 'paper-review'
    assert '没有开始计时' in turn['response_text'] and '延期' in turn['response_text']
    if execute:
        ok(client.post(f'/v1/agent-turns/{turn["agent_turn_id"]}/execute'))
    with db.connect() as c:
        assert not c.execute("SELECT 1 FROM plan_item WHERE status='active'").fetchone()
    return turn


@pytest.mark.parametrize('recovery', ['retry', 'defer'])
def test_source_gap_survives_restart_and_child_can_continue(paper_task, monkeypatch, recovery):
    config, db, sid, mid, gateway = paper_task
    with db.connect() as c:
        original = c.execute('SELECT prompt_summary FROM mistake_case WHERE mistake_id=?', (mid,)).fetchone()[0]
        c.execute("UPDATE mistake_case SET prompt_summary='' WHERE mistake_id=?", (mid,))
        c.commit()
    with TestClient(create_app(config, model_gateway=gateway)) as client:
        first = gap(client, paper_task, monkeypatch)
    with TestClient(create_app(config, model_gateway=gateway)) as client:
        if recovery == 'retry':
            with db.connect() as c:
                c.execute('UPDATE mistake_case SET prompt_summary=? WHERE mistake_id=?', (original, mid))
                c.commit()
        quote, control = (('重新开始英语错题复习', 'task.start') if recovery == 'retry'
                          else ('这项错题先延期，保留进度', 'task.reschedule'))
        turn = hear(client, config, monkeypatch, gateway, sid, None, quote, 'source-recover', control)
        assert turn.get('runtime_capability') == control, turn.get('response_text')
        url = f'/v1/agent-turns/{turn["agent_turn_id"]}/execute'
        value = ok(client.post(url))['execution']['result']
        assert value['plan_item_id'] == 'paper-review'
        ok(client.post(url))
        with db.connect() as c:
            expected = 'active' if recovery == 'retry' else 'rescheduled'
            assert c.execute("SELECT status FROM plan_item WHERE plan_item_id='paper-review'").fetchone()[0] == expected
            assert c.execute("SELECT status FROM plan_item WHERE plan_item_id='later-school'").fetchone()[0] == 'pending'
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 0
            assert json.loads(c.execute('SELECT action_json FROM agent_turn WHERE agent_turn_id=?',
                (first['agent_turn_id'],)).fetchone()[0])['task_source_gap']
        if recovery == 'defer':
            next_turn = hear(client, config, monkeypatch, gateway, sid, None,
                '开始数学作业', 'after-source-deferral', 'task.start')
            result = ok(client.post(f'/v1/agent-turns/{next_turn["agent_turn_id"]}/execute'))['execution']['result']
            assert result['plan_item_id'] == 'later-school'


@pytest.mark.parametrize('change', ['restored', 'source', 'order', 'other_child', 'cancelled_offer'])
def test_defer_rechecks_original_gap_at_execution(paper_task, monkeypatch, change):
    config, db, sid, mid, gateway = paper_task
    with db.connect() as c:
        c.execute("UPDATE mistake_case SET prompt_summary='' WHERE mistake_id=?", (mid,))
        c.commit()
    with TestClient(create_app(config, model_gateway=gateway)) as client:
        first = gap(client, paper_task, monkeypatch)
        turn = hear(client, config, monkeypatch, gateway, sid, None,
            '把这项错题延期', 'late-gap-change', 'task.reschedule')
        assert turn.get('runtime_capability') == 'task.reschedule'
        with db.connect() as c:
            if change == 'restored':
                c.execute("UPDATE mistake_case SET prompt_summary='Restored synthetic question' WHERE mistake_id=?", (mid,))
            elif change == 'source':
                c.execute("UPDATE plan_item SET source_ref='other-source' WHERE plan_item_id='paper-review'")
            elif change == 'order':
                c.execute("UPDATE plan_item SET position=10 WHERE plan_item_id='paper-review'")
            elif change == 'other_child':
                c.execute("UPDATE mistake_case SET child_id='child-junior-3' WHERE mistake_id=?", (mid,))
            else:
                c.execute("UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id=?", (first['agent_turn_id'],))
            c.commit()
        assert client.post(f'/v1/agent-turns/{turn["agent_turn_id"]}/execute').status_code == 409
        with db.connect() as c:
            assert c.execute("SELECT status FROM plan_item WHERE plan_item_id='paper-review'").fetchone()[0] == 'pending'
            assert not c.execute("SELECT 1 FROM plan_item_event WHERE event_type IN ('started','rescheduled')").fetchone()
    for request in gateway.requests:
        assert mid not in json.dumps(request.messages)
        assert 'source_version' not in json.dumps(request.messages)


class FullDeferralPlanner(MixedGateway):
    async def complete(self, request):
        if request.request_type == 'agent_plan' and self.control == 'task.reschedule':
            self.requests.append(request)
            return await Planner(self.control, {'reason': self.quote}, self.quote).complete(request)
        return await super().complete(request)


def test_unexecuted_gap_does_not_authorize_default_pending_task(paper_task, monkeypatch):
    config, db, sid, mid, _ = paper_task
    gateway = FullDeferralPlanner('')
    case = (config, db, sid, mid, gateway)
    with db.connect() as c:
        c.execute("UPDATE mistake_case SET prompt_summary='' WHERE mistake_id=?", (mid,))
        c.commit()
    with TestClient(create_app(config, model_gateway=gateway)) as client:
        gap(client, case, monkeypatch, execute=False)
        turn = hear(client, config, monkeypatch, gateway, sid, None,
            '把这项延期', 'unheard-gap', 'task.reschedule')
        if turn.get('runtime_capability') == 'task.reschedule':
            assert client.post(f'/v1/agent-turns/{turn["agent_turn_id"]}/execute').status_code == 409
        with db.connect() as c:
            assert c.execute("SELECT status FROM plan_item WHERE plan_item_id='paper-review'").fetchone()[0] == 'pending'


@pytest.mark.parametrize('change', ['excluded', 'wrong_owner'])
def test_original_unavailable_before_request_gives_recoverable_offer(paper_task, monkeypatch, change):
    config, db, sid, mid, gateway = paper_task
    with db.connect() as c:
        if change == 'excluded':
            c.execute("UPDATE mistake_case SET status='excluded' WHERE mistake_id=?", (mid,))
        else:
            c.execute("UPDATE mistake_case SET child_id='child-junior-3' WHERE mistake_id=?", (mid,))
        c.commit()
    with TestClient(create_app(config, model_gateway=gateway)) as client:
        gap(client, paper_task, monkeypatch)


def test_original_text_changed_after_request_cannot_start_timer(paper_task, monkeypatch):
    config, db, sid, mid, gateway = paper_task
    with TestClient(create_app(config, model_gateway=gateway)) as client:
        turn = hear(client, config, monkeypatch, gateway, sid, None,
            '开始英语错题复习', 'late-original-change', 'task.start')
        assert turn['runtime_capability'] == 'task.start'
        with db.connect() as c:
            c.execute("UPDATE mistake_case SET prompt_summary='A different synthetic question' WHERE mistake_id=?", (mid,))
            c.commit()
        assert client.post(f'/v1/agent-turns/{turn["agent_turn_id"]}/execute').status_code == 409
        with db.connect() as c:
            assert not c.execute("SELECT 1 FROM plan_item WHERE status='active'").fetchone()


def test_formula_review_version_changed_without_changing_case_text(paper_task, monkeypatch):
    config, db, sid, mid, gateway = paper_task
    with TestClient(create_app(config, model_gateway=gateway)) as client:
        turn = hear(client, config, monkeypatch, gateway, sid, None,
            '开始英语错题复习', 'late-formula-review', 'task.start')
        assert turn['runtime_capability'] == 'task.start'
        with db.connect() as c:
            source = c.execute('SELECT source_ref FROM mistake_case WHERE mistake_id=?', (mid,)).fetchone()[0].partition('#')[0]
            row = c.execute('SELECT details_json FROM capture_artifact WHERE artifact_id=?', (source,)).fetchone()
            details = json.loads(row[0])
            details.setdefault('quality', {})['issues'] = ['formula_review_required']
            c.execute('UPDATE capture_artifact SET details_json=? WHERE artifact_id=?', (json.dumps(details), source))
            c.commit()
        assert client.post(f'/v1/agent-turns/{turn["agent_turn_id"]}/execute').status_code == 409
        with db.connect() as c:
            assert not c.execute("SELECT 1 FROM plan_item WHERE status='active'").fetchone()


@pytest.mark.parametrize('old_reference', [False, True])
def test_missing_or_delayed_recording_cannot_acquire_latest_gap(paper_task, monkeypatch, old_reference):
    config, db, sid, mid, gateway = paper_task
    with db.connect() as c:
        c.execute("UPDATE mistake_case SET prompt_summary='' WHERE mistake_id=?", (mid,))
        c.commit()
    with TestClient(create_app(config, model_gateway=gateway)) as client:
        gap(client, paper_task, monkeypatch)
        reference = ok(client.get(f'/v1/sessions/{sid}/task-start-choice'))['reference']
        assert reference
        quote = '把这项错题延期'
        if old_reference:
            voice = ok(capture_choice(client, (config, db, sid, gateway), monkeypatch,
                reference, quote, 'old-gap-recording'))['voice_input_id']
            gap(client, paper_task, monkeypatch, key='new-gap-prompt')
            gateway.quote, gateway.control = quote, 'task.reschedule'
            turn = interpret(client, sid, quote, 'late-gap-recording', voice)
        else:
            turn = unbound_hear(client, config, monkeypatch, gateway, sid, None,
                quote, 'unbound-gap-recording', 'task.reschedule')
        assert turn.get('runtime_capability') != 'task.reschedule'
        with db.connect() as c:
            assert c.execute("SELECT status FROM plan_item WHERE plan_item_id='paper-review'").fetchone()[0] == 'pending'


@pytest.mark.skipif(os.environ.get('HSC_LIVE_SOURCE_RECOVERY') != '1', reason='synthetic official API opt-in')
def test_real_model_can_defer_exact_unavailable_original(paper_task, monkeypatch):
    config, db, sid, mid, gateway = paper_task
    with db.connect() as c:
        c.execute("UPDATE mistake_case SET prompt_summary='' WHERE mistake_id=?", (mid,))
        c.commit()
    with TestClient(create_app(config, model_gateway=gateway)) as client:
        gap(client, paper_task, monkeypatch)
    live = LiveStartGateway(config)
    with TestClient(create_app(config, model_gateway=live)) as client:
        turn = hear(client, config, monkeypatch, live, sid, None,
            '那这项错题先留到以后，别算完成', 'live-source-deferral', 'task.reschedule')
        print('SYNTHETIC_SOURCE_RECOVERY', json.dumps(live.receipts, ensure_ascii=False))
        assert len(live.receipts) == 1 and live.receipts[0]['request_type'] == 'voice_interaction'
        assert turn.get('runtime_capability') == 'task.reschedule', turn.get('response_text')
        value = ok(client.post(f'/v1/agent-turns/{turn["agent_turn_id"]}/execute'))['execution']['result']
        assert value['plan_item_id'] == 'paper-review' and value['status'] == 'rescheduled'
        with db.connect() as c:
            assert c.execute("SELECT status FROM plan_item WHERE plan_item_id='later-school'").fetchone()[0] == 'pending'
