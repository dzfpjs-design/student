"""Exact source-mismatch speech policy through real HTTP and temporary SQLite.
Only ASR recognition is a public synthetic fixture; no model or device executes.
"""
import json
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.diagnostic_prompt import DiagnosticPromptService
from home_study_agent.agent_orchestrator import AgentOrchestrator
from test_agent_continuation import Gateway
from test_diagnostic_prompt_http import environment, start, CHILD, OTHER
from test_voice_runtime_binding import prepare, capture, interpret
from test_content_free_voice_control_http import ControlGateway

NOTICE = '这段语音采集时的题目和当前待答题无法核对，我先不计作答案。请核对原题后重新回答。'

def setup(client, db, sid):
    old = prepare(client, sid)
    DiagnosticPromptService(db).cancel(sid, prompt_id=old['prompt_id'], item_version=old['item_version'])
    current = prepare(client, sid, 'second-current', 'diag-p4-arithmetic-closed-2')
    return old, current

def heard(client, settings, monkeypatch, sid, old, text, key, child=CHILD):
    voice = capture(client, settings, monkeypatch, sid, text, old, 'audio-' + key, child=child)
    turn = interpret(client, sid, text, key, voice)
    return turn, voice

def unchanged(db, current):
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 0
        assert c.execute('SELECT status FROM diagnostic_prompt WHERE prompt_id=?', (current['prompt_id'],)).fetchone()[0] == 'waiting_answer'

def gateway():
    result = Gateway({}); result.configured = False; return result

def test_first_notice_then_distinct_ASR_inputs_only_keep_text_and_original_receipts(environment, monkeypatch):
    settings, db = environment; g = gateway()
    with TestClient(create_app(settings, model_gateway=g)) as client:
        sid = start(client); old, current = setup(client, db, sid)
        turns = []
        for i, text in enumerate(('90', '91', '92')):
            turn, _ = heard(client, settings, monkeypatch, sid, old, text, 'different-' + str(i))
            turns.append(turn)
            # Consume while this original child-model snapshot is current.
            result = client.post('/v1/agent-turns/' + turn['agent_turn_id'] + '/execute')
            assert result.status_code == 200, result.text
        assert turns[0]['action'].get('suppress_speech') is not True
        assert all(t['action'].get('suppress_speech') is True for t in turns[1:])
        assert len({t['agent_turn_id'] for t in turns}) == 3
        assert all(t['intent'] == 'pending_answer_clarification' and t['response_text'] == NOTICE for t in turns)
        assert all(t['action']['type'] == 'none' and t['action']['local_only'] is True
                   and t['action']['cloud_transmission_allowed'] is False and 'pending_answer_candidate' not in t['action'] for t in turns)
        assert all(t.get('after_execution', 'stop') == 'stop' for t in turns)
        with db.connect() as c:
            rows = c.execute('SELECT transcript,response_text FROM agent_turn WHERE session_id=? ORDER BY rowid', (sid,)).fetchall()
            assert [r['transcript'] for r in rows] == ['90', '91', '92']
            assert all(r['response_text'] == NOTICE for r in rows)
        unchanged(db, current)
    assert not g.requests

def test_current_question_change_can_issue_a_new_notice(environment, monkeypatch):
    settings, db = environment; g = gateway()
    with TestClient(create_app(settings, model_gateway=g)) as client:
        sid = start(client); old, current = setup(client, db, sid)
        first, _ = heard(client, settings, monkeypatch, sid, old, '90', 'before-change')
        DiagnosticPromptService(db).cancel(sid, prompt_id=current['prompt_id'], item_version=current['item_version'])
        changed = prepare(client, sid, 'third-current', 'diag-p4-arithmetic-closed-1')
        fresh, _ = heard(client, settings, monkeypatch, sid, old, '91', 'after-change')
        again, _ = heard(client, settings, monkeypatch, sid, old, '92', 'after-change-again')
        assert first['action'].get('suppress_speech') is not True
        assert fresh['action'].get('suppress_speech') is not True
        assert again['action'].get('suppress_speech') is True
        unchanged(db, changed)
    assert not g.requests

def test_child_and_session_isolation_do_not_inherit_automatic_silence(environment, monkeypatch):
    settings, db = environment; g = gateway()
    with TestClient(create_app(settings, model_gateway=g)) as client:
        sid = start(client); old, current = setup(client, db, sid)
        heard(client, settings, monkeypatch, sid, old, '90', 'primary-first')
        second, _ = heard(client, settings, monkeypatch, sid, old, '91', 'primary-second')
        assert second['action'].get('suppress_speech') is True
        switched = client.post(f'/v1/sessions/{sid}/switch-child', json={
            'target_child_id': OTHER, 'client_action_id': 'switch-for-isolation'})
        assert switched.status_code == 200, switched.text
        other = switched.json()['session_id']
        def question(key):
            result = client.post(f'/v1/sessions/{other}/diagnostic-prompt/prepare', json={'track':'school','client_action_id':key})
            assert result.status_code == 200, result.text
            return result.json()['prompt']
        old_other = question('other-old')
        DiagnosticPromptService(db).cancel(other, prompt_id=old_other['prompt_id'], item_version=old_other['item_version'])
        current_other = question('other-new')
        fresh, _ = heard(client, settings, monkeypatch, other, old_other, '12', 'other-first', OTHER)
        assert fresh['response_text'] == NOTICE and fresh['action'].get('suppress_speech') is not True
        unchanged(db, current_other)
    assert not g.requests

def test_idempotent_replay_is_silent_and_does_not_append_a_turn(environment, monkeypatch):
    settings, db = environment; g = gateway()
    with TestClient(create_app(settings, model_gateway=g)) as client:
        sid = start(client); old, current = setup(client, db, sid)
        first, voice = heard(client, settings, monkeypatch, sid, old, '90', 'same-action')
        replay = interpret(client, sid, '90', 'same-action', voice)
        assert first['action'].get('suppress_speech') is not True
        assert replay['agent_turn_id'] == first['agent_turn_id'] and replay['action'].get('suppress_speech') is True
        assert replay['transcript'] == first['transcript'] and replay['response_text'] == NOTICE
        with db.connect() as c: assert c.execute('SELECT count(*) FROM agent_turn WHERE session_id=?', (sid,)).fetchone()[0] == 1
        unchanged(db, current)
    assert not g.requests

def test_existing_explicit_restatements_and_safety_control_are_not_silenced(environment, monkeypatch):
    settings, db = environment
    # Existing controlled semantic parser fixture, not a provider/model call.
    g = ControlGateway('', control='speech.repeat'); g.configured = False
    with TestClient(create_app(settings, model_gateway=g)) as client:
        sid = start(client); old, current = setup(client, db, sid)
        original, _ = heard(client, settings, monkeypatch, sid, old, '90', 'first-warning')
        executed = client.post('/v1/agent-turns/' + original['agent_turn_id'] + '/execute')
        assert executed.status_code == 200, executed.text
        g.configured = True
        requests = ('再说一遍', '重复一遍', '没听清', '你刚才说什么', '方才那段我没听真切，劳驾再讲一次')
        for i, text in enumerate(requests):
            g.quote = text
            repeat, _ = heard(client, settings, monkeypatch, sid, old, text, 'repeat-' + str(i))
            assert repeat['action']['type'] == 'repeat_response', (repeat['intent'], repeat['action'].get('source'))
            assert repeat['action'].get('suppress_speech') is not True
            assert repeat['action']['source'] == 'content_free_voice_control'
            assert repeat['runtime_capability'] == 'speech.repeat'
            assert repeat['requires_control_admission'] is False
            replayed = client.post('/v1/agent-turns/' + repeat['agent_turn_id'] + '/execute')
            assert replayed.status_code == 200, replayed.text
            result = replayed.json()['execution']
            assert result['response_text'] == NOTICE
            assert result['result']['status'] == 'replayed'
            assert result['result']['replayed_turn_id'] == original['agent_turn_id']
            assert 'capture_mismatch_speech_notice' not in repeat['action']
        g.configured = False
        stop, _ = heard(client, settings, monkeypatch, sid, old, '别说了', 'explicit-stop')
        assert stop['action']['type'] == 'stop_speech' and stop['action'].get('suppress_speech') is not True
        unchanged(db, current)
    assert len(g.requests) == len(requests)
    assert all(r.request_type == 'voice_interaction' for r in g.requests)

def test_matching_capture_and_ordinary_clarification_keep_their_original_guard(environment, monkeypatch):
    settings, db = environment; g = gateway()
    with TestClient(create_app(settings, model_gateway=g)) as client:
        sid = start(client); old, current = setup(client, db, sid)
        heard(client, settings, monkeypatch, sid, old, '90', 'stale-first')
        for text, key in (('90', 'valid-answer'), ('等我一下', 'ordinary-clarification')):
            fresh, _ = heard(client, settings, monkeypatch, sid, current, text, key)
            assert fresh['response_text'] != NOTICE
            assert 'capture_mismatch_speech_notice' not in fresh['action']
            assert fresh['action'].get('suppress_speech') is not True
            if key == 'valid-answer': assert fresh['action']['type'] == 'submit_diagnostic_answer'
            else: assert fresh['intent'] == 'pending_answer_clarification'
        unchanged(db, current)
    assert not g.requests

def test_unknown_or_malformed_target_cannot_create_a_shared_silence_scope():
    make = AgentOrchestrator._capture_mismatch_notice_scope
    for target in (None, {}, {'action':'foreign','binding':{}},
                   {'action':'submit_diagnostic_answer','binding':{'prompt_id':'p'}},
                   {'action':'submit_diagnostic_answer','binding':{'prompt_id':'p','item_version':None}},
                   {'action':'submit_exam_answer','binding':{'run_id':'r','position':True,'item_id':'i','source_version':'v'}},
                   {'action':'continue_mistake_dialogue','binding':{'session_id':'foreign','child_id':'c','mistake_id':'m','phase':'attempt','item_id':'i','source_version':'v'}}):
        assert make('s','c',target) is None


def test_closed_generic_six_field_scope_retains_source_turn_and_rejects_foreign_extensions():
    make = AgentOrchestrator._capture_mismatch_notice_scope
    base = {'objective_id':'objective','plan_id':'plan','capability_name':'learning.explain',
            'phase':'check','source_version':'original-server-source-version'}
    target = {'action':'evaluate_child_response','binding':{**base,'source_agent_turn_id':'source-turn-1'}}
    first = make('session','child',target)
    assert first is not None
    assert make('session','child',target) == first
    for cap in ('learning.explain','learning.evaluate_response'):
        assert make('session','child',{'action':'evaluate_child_response','binding':{**target['binding'],'capability_name':cap}}) is not None
    moved = {'action':'evaluate_child_response','binding':{**target['binding'],'source_agent_turn_id':'source-turn-2'}}
    assert make('session','child',moved) != first and make('session','child',moved) is not None
    assert make('session','child',{'action':'evaluate_child_response','binding':base}) is not None
    for binding in ({**target['binding'],'extra':'foreign'},
                    {**target['binding'],'capability_name':'foreign.capability'},
                    {**target['binding'],'capability_name':[]},
                    {**target['binding'],'capability_name':None},
                    {**target['binding'],'source_agent_turn_id':''},
                    {**target['binding'],'source_agent_turn_id':None}):
        assert make('session','child',{'action':'evaluate_child_response','binding':binding}) is None
