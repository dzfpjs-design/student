"""New role-notice policy through actual HTTP/SQLite, synthetic ASR, no outside model."""
import hashlib
import json

from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.agent_orchestrator import AgentOrchestrator
from home_study_agent.agent_runtime import AgentRuntimeStore
from home_study_agent.diagnostic_prompt import DiagnosticPromptService
from test_agent_continuation import Gateway
from test_diagnostic_prompt_http import environment, start, CHILD, OTHER
from test_voice_runtime_binding import prepare, capture, interpret
from test_content_free_voice_control_http import ControlGateway

ROLE = '刚才这句我先不算作答案。你是在回答当前这道题、问题目里的意思，还是要处理另一件事？原题和这句话都留着。'
RESTATE = '当前没有可确认的答语，我不会猜选刚才哪一句。请完整重述，可以说“我的答案是……”。'
PRIVATE = '_orchestrator_answer_role_notice_v1'
A = '九十是我的计算结果'
B = '八十是另一段原话'


def gateway():
    g = Gateway({}); g.configured = False; return g


def heard(client, settings, monkeypatch, sid, prompt, text, key, child=CHILD):
    voice = capture(client, settings, monkeypatch, sid, text, prompt, 'voice-' + key, child=child)
    return interpret(client, sid, text, "role-" + key, voice), voice


def execute(client, turn):
    r = client.post('/v1/agent-turns/' + turn['agent_turn_id'] + '/execute')
    assert r.status_code == 200, r.text
    return r.json()


def unchanged(db, current):
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 0
        assert c.execute('SELECT status FROM diagnostic_prompt WHERE prompt_id=?', (current['prompt_id'],)).fetchone()[0] == 'waiting_answer'


def private_action(db, turn):
    with db.connect() as c:
        return json.loads(c.execute('SELECT action_json FROM agent_turn WHERE agent_turn_id=?', (turn['agent_turn_id'],)).fetchone()[0])


def public_only(value, private_hash):
    encoded = json.dumps(value, ensure_ascii=False, default=str)
    assert PRIVATE not in encoded and private_hash not in encoded


def test_first_A_then_different_B_is_silent_and_cannot_confirm_B_or_fall_back_A(environment, monkeypatch):
    settings, db = environment; g = gateway()
    with TestClient(create_app(settings, model_gateway=g)) as client:
        sid = start(client); prompt = prepare(client, sid)
        a, _ = heard(client, settings, monkeypatch, sid, prompt, A, 'first-A')
        assert a['response_text'] == ROLE and a['action'].get('suppress_speech') is not True
        assert a['action']['pending_answer_candidate']['answer'] == A
        execute(client, a)
        b, _ = heard(client, settings, monkeypatch, sid, prompt, B, 'different-B')
        assert b['response_text'] == ROLE and b['action'].get('suppress_speech') is True
        assert 'pending_answer_candidate' not in b['action']
        assert b['action']['type'] == 'none' and b['action']['mastery_update_allowed'] is False
        execute(client, b)
        confirmed, _ = heard(client, settings, monkeypatch, sid, prompt, '刚才那句就是我的答案', 'cannot-choose-B')
        assert confirmed['response_text'] == RESTATE
        assert confirmed['action']['type'] == 'none' and 'pending_answer_candidate' not in confirmed['action']
        assert 'answer' not in confirmed['action'] and 'answer_source_turn_id' not in confirmed['action']
        assert confirmed['action'].get('suppress_speech') is not True
        with db.connect() as c:
            rows = c.execute('SELECT transcript FROM agent_turn WHERE session_id=? ORDER BY rowid', (sid,)).fetchall()
            assert [r['transcript'] for r in rows] == [A, B, '刚才那句就是我的答案']
        ah = private_action(db, a)[PRIVATE]['first_candidate_sha256']
        assert ah == hashlib.sha256(A.encode()).hexdigest()
        assert private_action(db, b)[PRIVATE]['first_candidate_sha256'] == ah
        for turn in (a, b, confirmed): public_only(turn, ah)
        unchanged(db, prompt)
    assert not g.requests


def test_exact_same_A_keeps_latest_original_voice_confirmability_without_scoring(environment, monkeypatch):
    settings, db = environment; g = gateway()
    with TestClient(create_app(settings, model_gateway=g)) as client:
        sid = start(client); prompt = prepare(client, sid)
        a1, _ = heard(client, settings, monkeypatch, sid, prompt, A, 'same-A1'); execute(client, a1)
        a2, voice2 = heard(client, settings, monkeypatch, sid, prompt, A, 'same-A2')
        assert a2['action'].get('suppress_speech') is True
        assert a2['action']['pending_answer_candidate']['answer'] == A
        execute(client, a2)
        confirm, confirmation_voice = heard(client, settings, monkeypatch, sid, prompt, '刚才那句就是我的答案', 'same-A-confirm')
        assert confirm['action']['type'] == 'submit_diagnostic_answer'
        assert confirm['action']['answer'] == A
        assert confirm['action']['answer_source_turn_id'] == a2['agent_turn_id']
        assert confirm['action']['source_voice_input_id'] == voice2
        assert confirm['action']['voice_input_id'] == confirmation_voice
        assert confirm['action'].get('suppress_speech') is not True
        unchanged(db, prompt)
    assert not g.requests


def test_new_question_and_other_child_session_get_a_new_first_notice(environment, monkeypatch):
    settings, db = environment; g = gateway()
    with TestClient(create_app(settings, model_gateway=g)) as client:
        sid = start(client); current = prepare(client, sid)
        a, _ = heard(client, settings, monkeypatch, sid, current, A, 'scope-A')
        b, _ = heard(client, settings, monkeypatch, sid, current, B, 'scope-B')
        assert b['action'].get('suppress_speech') is True
        DiagnosticPromptService(db).cancel(sid, prompt_id=current['prompt_id'], item_version=current['item_version'])
        new = prepare(client, sid, 'new-source', 'diag-p4-arithmetic-closed-2')
        fresh, _ = heard(client, settings, monkeypatch, sid, new, B, 'fresh-source-B')
        assert fresh['response_text'] == ROLE and fresh['action'].get('suppress_speech') is not True
        assert fresh['action']['pending_answer_candidate']['answer'] == B
        assert private_action(db, fresh)[PRIVATE]['scope_sha256'] != private_action(db, a)[PRIVATE]['scope_sha256']
        switched = client.post(f'/v1/sessions/{sid}/switch-child', json={'target_child_id': OTHER, 'client_action_id': 'role-other-child'})
        assert switched.status_code == 200, switched.text
        other = switched.json()['session_id']
        p = client.post(f'/v1/sessions/{other}/diagnostic-prompt/prepare', json={'track':'school','client_action_id':'other-role-question'})
        assert p.status_code == 200, p.text
        other_prompt = p.json()['prompt']
        other_turn, _ = heard(client, settings, monkeypatch, other, other_prompt, A, 'other-role-first', OTHER)
        assert other_turn['action'].get('suppress_speech') is not True
        assert other_turn['action']['pending_answer_candidate']['answer'] == A
        unchanged(db, other_prompt)
    assert not g.requests


def test_nonce_replay_only_projects_silence_and_never_exports_private_notice(environment, monkeypatch):
    settings, db = environment; g = gateway()
    with TestClient(create_app(settings, model_gateway=g)) as client:
        sid = start(client); prompt = prepare(client, sid)
        a, voice = heard(client, settings, monkeypatch, sid, prompt, A, 'nonce-A')
        stored_before = private_action(db, a)
        replay = interpret(client, sid, A, 'role-nonce-A', voice)
        assert a['agent_turn_id'] == replay['agent_turn_id']
        assert a['action'].get('suppress_speech') is not True and replay['action'].get('suppress_speech') is True
        assert private_action(db, a) == stored_before
        assert 'suppress_speech' not in stored_before
        assert replay['action']['pending_answer_candidate']['answer'] == A
        public_only(replay, stored_before[PRIVATE]['first_candidate_sha256'])
        with db.connect() as c: assert c.execute('SELECT count(*) FROM agent_turn WHERE session_id=?', (sid,)).fetchone()[0] == 1
        unchanged(db, prompt)
    assert not g.requests


def test_repeat_stop_and_explicit_full_restatement_keep_original_paths_private_model_and_logs(environment, monkeypatch, caplog, capsys):
    settings, db = environment
    g = ControlGateway('', control='speech.repeat'); g.configured = False
    with TestClient(create_app(settings, model_gateway=g)) as client:
        sid = start(client); prompt = prepare(client, sid)
        a, _ = heard(client, settings, monkeypatch, sid, prompt, A, 'privacy-A'); public_first = execute(client, a)
        b, _ = heard(client, settings, monkeypatch, sid, prompt, B, 'privacy-B'); public_later = execute(client, b)
        private = private_action(db, a); h = private[PRIVATE]['first_candidate_sha256']
        assert AgentOrchestrator._allows_cloud_history(json.dumps(private)) is False
        context = AgentRuntimeStore(db).assemble_context(sid, ())
        public_only(context, h); public_only(public_first, h); public_only(public_later, h)
        g.configured = True; g.quote = '再说一遍'
        repeat, _ = heard(client, settings, monkeypatch, sid, prompt, g.quote, 'role-repeat')
        assert repeat['action']['type'] == 'repeat_response' and repeat['action'].get('suppress_speech') is not True
        result = execute(client, repeat)['execution']
        assert result['response_text'] == ROLE and result['result']['replayed_turn_id'] == a['agent_turn_id']
        g.configured = False
        restated, _ = heard(client, settings, monkeypatch, sid, prompt, '我的答案是90', 'full-restatement')
        assert restated['action']['type'] == 'submit_diagnostic_answer' and restated['action']['answer'] == '90'
        assert restated['action'].get('suppress_speech') is not True
        stop, _ = heard(client, settings, monkeypatch, sid, prompt, '别说了', 'role-stop')
        assert stop['action']['type'] == 'stop_speech' and stop['action'].get('suppress_speech') is not True
        for value in (repeat, result, restated, stop): public_only(value, h)
        assert len(g.requests) == 1
        public_only(g.requests[0].messages, h)
        unchanged(db, prompt)
    outputs = capsys.readouterr(); public_only(outputs.out + outputs.err + caplog.text, h)
