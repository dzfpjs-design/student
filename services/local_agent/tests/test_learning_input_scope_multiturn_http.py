"""Remaining-work dialogue uses real HTTP dispatch and waiting-child recovery."""
import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from test_agent_continuation import Gateway
from test_learning_input_carryover_host import host, carry, schedule, inputs, flow, grant


@pytest.fixture
def scope_host(host, tmp_path):
    case, repo = host
    db, service, _, clock = case
    accepted = service.review_unplaced(**grant(case))
    workspace = repo.generate_plan('today', 'scope-plan')
    item = next(i for i in workspace['plan_items'] if i['homework_id'] == accepted['target_homework_id'] and i['status'] == 'pending')
    repo.transition('today', 'plan_review', 'study', 'child_confirmed_plan', 'study')
    repo.start_plan_item(item['plan_item_id'], 'start')
    clock[0] += timedelta(minutes=5)
    repo.pause_plan_item(item['plan_item_id'], 'pause')
    repo.reschedule_plan_item(item['plan_item_id'], 'unfinished', 'defer')
    return case, Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False), Gateway({})


def say(client, quote, key):
    result = client.post('/v1/sessions/today/agent-turn', json={'transcript': quote, 'client_action_id': 'scope-multi-' + key})
    assert result.status_code == 200, result.text
    return result.json()


def execute(client, turn):
    result = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
    assert result.status_code == 200, result.text
    return result.json()


def focus(client, case):
    turn = say(client, '昨天没排完的作业有哪些', 'list')
    candidates = [i for i in case[1].list_unplaced('child-primary-4', 'today')['items'] if i['allowed_actions'] and (i['remaining_minutes'] or i['scope_review_required'])]
    index = next(n for n, i in enumerate(candidates, 1) if i['scope_review_required'])
    if len(candidates) > 1:
        assert '第1项' in turn['response_text']
        turn = say(client, f'第{index}项', 'select')
    assert turn['action']['type'] == 'none'


@pytest.mark.parametrize('utterances', [
    ('后面三题没写', '大概还要五分钟'),
    ('估计得五分钟吧', '最后两道应用题还没做'),
])
@pytest.mark.parametrize('carry', [{'two': True}], indirect=True)
def test_real_multiturn_scope_confirmation_restart_and_allocation(scope_host, utterances):
    case, settings, gateway = scope_host
    db = case[0]
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        focus(client, case)
        first = say(client, utterances[0], 'slot-one')
        assert first['action']['type'] == 'none' and first['action']['local_only']
        execute(client, first)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        offer = say(client, utterances[1], 'slot-two')
        assert offer['action']['type'] == 'none' and '5' in offer['response_text']
        execute(client, offer)
        with db.connect() as c:
            assert c.execute("SELECT COUNT(*) FROM learning_input_carryover_event WHERE operation='confirm_scope'").fetchone()[0] == 0
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        turn = say(client, '对，就是这些', 'confirm')
        assert turn['action']['type'] == 'review_learning_input_reclaimed_scope'
        assert turn['action']['request_quote'] == '对，就是这些'
        execute(client, turn)
        assert execute(client, turn)
        with db.connect() as c:
            row = c.execute('SELECT p.status,s.status FROM agent_plan p JOIN agent_step s USING(plan_id) WHERE p.agent_turn_id=?', (turn['agent_turn_id'],)).fetchone()
            assert tuple(row) == ('waiting_child', 'completed')
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        continuation = say(client, '今天继续', 'allocate')
        assert continuation['action']['type'] == 'review_learning_input_unplaced'
        execute(client, continuation)
    with db.connect() as c:
        events = c.execute("SELECT result_json FROM learning_input_carryover_event WHERE operation='confirm_scope'").fetchall()
        assert len(events) == 1 and json.loads(events[0][0])['remaining_minutes'] == 5
        assert c.execute('SELECT COUNT(*) FROM learning_evidence').fetchone()[0] == 0
    assert gateway.requests == []


@pytest.mark.parametrize('change', ['source_turn', 'source_cancelled', 'new_input'])
def test_scope_correction_replaces_draft_and_dispatch_rejects_tampered_source_turn(scope_host, change):
    case, settings, gateway = scope_host
    db = case[0]
    with TestClient(create_app(settings, model_gateway=gateway), raise_server_exceptions=False) as client:
        focus(client, case)
        first = say(client, '后面三题没写', 'first')
        execute(client, first)
        execute(client, say(client, '五分钟应该够了', 'time'))
        correction = say(client, '不对，我说错了', 'correction')
        assert correction['action']['type'] == 'none'
        execute(client, correction)
        empty_confirmation = say(client, '对，就是这些', 'premature')
        assert empty_confirmation['action']['type'] == 'none'
        execute(client, empty_confirmation)
        scope = say(client, '最后两道应用题还没做', 'revised-scope')
        execute(client, scope)
        execute(client, say(client, '大概要花十分钟', 'revised-time'))
        confirmation = say(client, '没错，就这样', 'final')
        assert confirmation['action']['type'] == 'review_learning_input_reclaimed_scope'
        assert confirmation['action']['remaining_minutes'] == 10
        if change == 'new_input':
            say(client, '先不要确认', 'interrupt-confirmation')
            assert say(client, '对', 'after-cancel')['action']['type'] == 'none'
        else:
            with db.connect() as c:
                if change == 'source_turn':
                    c.execute("UPDATE agent_turn SET transcript='不是孩子确认的范围' WHERE agent_turn_id=?", (scope['agent_turn_id'],))
                else:
                    c.execute("UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id=?", (scope['agent_turn_id'],))
                c.commit()
        failed = client.post(f"/v1/agent-turns/{confirmation['agent_turn_id']}/execute")
        assert failed.status_code == 409, failed.text
        with db.connect() as c:
            assert c.execute("SELECT COUNT(*) FROM learning_input_carryover_event WHERE operation='confirm_scope'").fetchone()[0] == 0
    assert gateway.requests == []


def test_unknown_wording_asks_locally_and_does_not_invent_work_or_time(scope_host):
    case, settings, gateway = scope_host
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        focus(client, case)
        unclear = say(client, '就是后面的那些', 'unclear')
        assert unclear['action']['type'] == 'none' and unclear['action']['local_only']
        execute(client, unclear)
        scope = say(client, '后面三题没写', 'known-scope')
        execute(client, scope)
        unsure = say(client, '还要多久我也不清楚', 'uncertain-time')
        assert unsure['action']['type'] == 'none'
        execute(client, unsure)
        confirmation = say(client, '对', 'incomplete-confirm')
        assert confirmation['action']['type'] == 'none'
    assert gateway.requests == []


def test_intervening_topic_drops_scope_offer_and_cannot_confirm_old_work(scope_host):
    case, settings, gateway = scope_host
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        focus(client, case)
        execute(client, say(client, '后面三题没写', 'scope'))
        execute(client, say(client, '大概还要五分钟', 'time'))
        say(client, '我想聊聊周末的天气', 'other-topic')
        confirmation = say(client, '对，就是这些', 'old-confirm')
        assert confirmation['action']['type'] != 'review_learning_input_reclaimed_scope'
    with case[0].connect() as c:
        assert c.execute("SELECT COUNT(*) FROM learning_input_carryover_event WHERE operation='confirm_scope'").fetchone()[0] == 0


@pytest.mark.parametrize('quote,minutes,scope', [
    ('我还有三道口算没写', None, '我还有三道口算没写'),
    ('我还差作文最后一段，大约十分钟', 10, '我还差作文最后一段'),
    ('估计得五分钟吧', 5, None),
    ('时间估计五分钟，作文结尾还没写', 5, '作文结尾还没写'),
    ('我喜欢做口算', None, None),
    ('这题给我一个提示', None, None),
])
def test_local_slot_extraction_accepts_clause_order_without_inventing_content(quote, minutes, scope):
    from home_study_agent.learning_input_scope_dialogue import scope_slots
    slots = scope_slots(quote)
    assert (slots.get('estimate') or {}).get('minutes') == minutes
    assert (slots.get('scope') or {}).get('quote') == scope
