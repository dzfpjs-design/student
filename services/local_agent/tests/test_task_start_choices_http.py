"""Spoken task-list selection with real receipts and capture-time identity."""
import base64
import json

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.local_media_ai import LocalMediaAI
from test_mistake_review_task_entry import paper_task
from test_mistake_voice_journey import ok
from test_task_resume_voice_http import hear
from test_voice_runtime_binding import interpret
from test_pet_media_evidence import wav_file


@pytest.fixture
def choices(paper_task):
    config, db, sid, _, gateway = paper_task
    with db.connect() as c:
        c.execute("UPDATE plan_item SET subject='数学',source_type='school_homework',task_type='written',source_ref=NULL")
        c.execute("UPDATE plan_item SET title='合成练习甲' WHERE plan_item_id='paper-review'")
        c.execute("UPDATE plan_item SET title='合成练习乙' WHERE plan_item_id='later-school'")
        c.commit()
    return config, db, sid, gateway


def offer(client, case, monkeypatch, key='first-offer', execute=True):
    config, db, sid, gateway = case
    turn = hear(client, config, monkeypatch, gateway, sid, None, '开始数学作业', key, 'task.start')
    assert turn['action']['source'] == 'task_navigation_clarification'
    assert '第1项，数学，合成练习甲' in turn['response_text']
    assert '第2项，数学，合成练习乙' in turn['response_text']
    with db.connect() as c:
        plan = json.loads(c.execute('SELECT plan_json FROM agent_plan WHERE agent_turn_id=?', (turn['agent_turn_id'],)).fetchone()[0])
        assert plan['requires_child_reply'] is True
    if execute:
        ok(client.post(f'/v1/agent-turns/{turn["agent_turn_id"]}/execute'))
    view = ok(client.get(f'/v1/sessions/{sid}/task-start-choice'))
    return turn, view['reference']


def capture_choice(client, case, monkeypatch, reference, text='第二项', key='choice-capture'):
    config, _, sid, _ = case
    monkeypatch.setattr(LocalMediaAI, '_recognize_wav', lambda *a, **k: (text, 1000, 'synthetic', False))
    path = config.data_dir / 'synthetic-choice.wav'
    wav_file(path)
    return client.post(f'/v1/sessions/{sid}/asr', json={
        'wav_base64': base64.b64encode(path.read_bytes()).decode(), 'purpose': 'agent_command',
        'client_action_id': key, 'capture_reference': {'child_id': 'child-primary-4', 'task_choice_reference': reference}})


@pytest.mark.parametrize('restart', [False, True])
def test_choose_recorded_second_task_without_cloud_and_keep_receipt(choices, monkeypatch, restart):
    config, db, sid, gateway = choices
    quote = '第二个' if restart else '第二项'
    with TestClient(create_app(config, model_gateway=gateway)) as client:
        _, reference = offer(client, choices, monkeypatch)
        assert reference
        if not restart:
            recorded = ok(capture_choice(client, choices, monkeypatch, reference, quote))
            assert recorded['capture_binding']['task_choice_reference'] == reference
            voice = recorded['voice_input_id']
    gateway.configured = False
    with TestClient(create_app(config, model_gateway=gateway)) as client:
        assert ok(client.get(f'/v1/sessions/{sid}/task-start-choice'))['reference'] == reference
        if restart:
            voice = ok(capture_choice(client, choices, monkeypatch, reference, quote))['voice_input_id']
        calls = len(gateway.requests)
        turn = interpret(client, sid, quote, 'choose-second', voice)
        assert turn['runtime_capability'] == 'task.start', turn
        url = f'/v1/agent-turns/{turn["agent_turn_id"]}/execute'
        result = ok(client.post(url))
        assert result['execution']['result']['plan_item_id'] == 'later-school'
        assert len(gateway.requests) == calls
        assert ok(client.get(f'/v1/sessions/{sid}/task-start-choice'))['reference'] is None
        with db.connect() as c:
            saved = c.execute('SELECT task_choice_reference_json FROM voice_capture_binding WHERE voice_input_id=?', (voice,)).fetchone()
            assert json.loads(saved[0]) == reference
            assert c.execute("SELECT status FROM plan_item WHERE plan_item_id='paper-review'").fetchone()[0] == 'pending'
    with TestClient(create_app(config, model_gateway=gateway)) as client:
        ok(client.post(url))
        with db.connect() as c:
            assert c.execute("SELECT status FROM plan_item WHERE plan_item_id='later-school'").fetchone()[0] == 'paused'
            assert c.execute("SELECT count(*) FROM plan_item_event WHERE event_type='started'").fetchone()[0] == 1


@pytest.mark.parametrize('change', ['order', 'title', 'new_offer', 'unbound', 'out_of_range'])
def test_old_recording_or_ambiguous_reply_cannot_acquire_new_target(choices, monkeypatch, change):
    config, db, sid, gateway = choices
    with TestClient(create_app(config, model_gateway=gateway)) as client:
        _, reference = offer(client, choices, monkeypatch)
        text = '第九项' if change == 'out_of_range' else '第二项'
        voice = ok(capture_choice(client, choices, monkeypatch, None if change == 'unbound' else reference, text))['voice_input_id']
        if change == 'new_offer':
            _, newer = offer(client, choices, monkeypatch, key='new-offer')
            assert newer != reference
        elif change in {'order', 'title'}:
            with db.connect() as c:
                if change == 'order':
                    c.execute("UPDATE plan_item SET position=0 WHERE plan_item_id='later-school'")
                else:
                    c.execute("UPDATE plan_item SET title='新的练习' WHERE plan_item_id='later-school'")
                c.commit()
        gateway.configured = False
        turn = interpret(client, sid, text, 'choose-stale', voice)
        assert turn['action']['source'] == 'task_navigation_clarification', turn
        ok(client.post(f'/v1/agent-turns/{turn["agent_turn_id"]}/execute'))
        with db.connect() as c:
            assert not c.execute("SELECT 1 FROM plan_item WHERE status='active'").fetchone()


@pytest.mark.parametrize('change', ['plan', 'offer_cancelled', 'new_turn'])
def test_selection_is_rechecked_in_actual_start_transaction(choices, monkeypatch, change):
    config, db, sid, gateway = choices
    with TestClient(create_app(config, model_gateway=gateway)) as client:
        original, reference = offer(client, choices, monkeypatch)
        voice = ok(capture_choice(client, choices, monkeypatch, reference))['voice_input_id']
        gateway.configured = False
        turn = interpret(client, sid, '第二项', 'choose-race', voice)
        assert turn['runtime_capability'] == 'task.start', turn
        if change == 'new_turn':
            hear(client, config, monkeypatch, gateway, sid, None, '别开始', 'newer-hold', 'task.start')
        else:
            with db.connect() as c:
                if change == 'plan':
                    c.execute("UPDATE plan_item SET source_ref='changed' WHERE plan_item_id='later-school'")
                else:
                    c.execute("UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id=?", (original['agent_turn_id'],))
                c.commit()
        assert client.post(f'/v1/agent-turns/{turn["agent_turn_id"]}/execute').status_code == 409
        with db.connect() as c:
            assert not c.execute("SELECT 1 FROM plan_item WHERE status='active'").fetchone()


def test_offer_requires_execution_and_asr_key_cannot_change_list(choices, monkeypatch):
    config, _, sid, gateway = choices
    with TestClient(create_app(config, model_gateway=gateway)) as client:
        turn, reference = offer(client, choices, monkeypatch, execute=False)
        assert reference is None
        ok(client.post(f'/v1/agent-turns/{turn["agent_turn_id"]}/execute'))
        reference = ok(client.get(f'/v1/sessions/{sid}/task-start-choice'))['reference']
        ok(capture_choice(client, choices, monkeypatch, reference))
        altered = {**reference, 'source_version': '0' * 64}
        assert capture_choice(client, choices, monkeypatch, altered).status_code == 409


def test_an_unbound_number_without_any_offer_cannot_start_first_task(choices, monkeypatch):
    config, db, sid, gateway = choices
    with TestClient(create_app(config, model_gateway=gateway)) as client:
        voice = ok(capture_choice(client, choices, monkeypatch, None))['voice_input_id']
        before = len(gateway.requests)
        turn = interpret(client, sid, '第二项', 'no-offer-number', voice)
        assert turn['action']['source'] == 'task_navigation_clarification'
        ok(client.post(f'/v1/agent-turns/{turn["agent_turn_id"]}/execute'))
        assert len(gateway.requests) == before
        with db.connect() as c:
            assert not c.execute("SELECT 1 FROM plan_item WHERE status='active'").fetchone()


def test_other_child_cannot_bind_this_list_to_their_recording(choices, monkeypatch):
    config, db, sid, gateway = choices
    with TestClient(create_app(config, model_gateway=gateway)) as client:
        _, reference = offer(client, choices, monkeypatch)
        for item_id in ('paper-review', 'later-school'):
            ok(client.post(f'/v1/plan-items/{item_id}/reschedule', json={
                'reason': '本次结束，保留原作业', 'client_action_id': 'switch-child:' + item_id}))
        ok(client.post(f'/v1/sessions/{sid}/transition', json={'expected_state': 'study', 'target_state': 'reflection',
            'reason': '结束后再换孩子', 'client_action_id': 'other-child-reflection'}))
        ok(client.post(f'/v1/sessions/{sid}/reflection/complete', json={
            'skipped': True, 'client_action_id': 'other-child-end-first'}))
        other = ok(client.post('/v1/sessions', json={'child_id': 'child-junior-3', 'client_action_id': 'other-child-session'}))['session_id']
        # Reuse the synthetic WAV, not a child's recording.
        path = config.data_dir / 'other-choice.wav'
        wav_file(path)
        rejected = client.post(f'/v1/sessions/{other}/asr', json={
            'wav_base64': base64.b64encode(path.read_bytes()).decode(), 'purpose': 'agent_command',
            'client_action_id': 'other-choice-capture',
            'capture_reference': {'child_id': 'child-junior-3', 'task_choice_reference': reference}})
        assert rejected.status_code == 409
        with db.connect() as c:
            assert not c.execute("SELECT 1 FROM voice_capture_binding WHERE client_action_id='other-choice-capture'").fetchone()


@pytest.mark.parametrize('quote,index', [('第二项', 1), ('我选第二个', 1), ('第十一项', 10),
    ('第二十一项', 20), ('第99个任务', 98), ('不是第二项', None), ('第十十项', None), ('第二项的答案是什么', None)])
def test_ordinal_is_an_explicit_selection_not_a_question_or_negation(quote, index):
    from home_study_agent.task_start_choices import choice_index
    assert choice_index(quote) == index
