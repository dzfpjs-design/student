"""Adversarial clarification chains on a private SQLite app; no cloud calls."""
import json
import socket
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.learning_state import LearningStateService
from home_study_agent.repository import utc_now
from test_diagnostic_prompt_http import environment, start, endpoint, binding, OTHER
from test_numeric_clarification_http import unclear
from test_numeric_tcp_audit30 import TCPClient


@pytest.fixture(autouse=True)
def loopback_only(monkeypatch):
    import home_study_agent.numeric_clarification as numeric
    assert Path(__file__).resolve().parents[1] / 'src' in Path(numeric.__file__).resolve().parents
    connect = socket.socket.connect
    def guarded(sock, address):
        if isinstance(address, tuple):
            assert address[0] in {'127.0.0.1', '::1'}, 'External socket forbidden'
        return connect(sock, address)
    monkeypatch.setattr(socket.socket, 'connect', guarded)


def prepare(client, sid, request):
    response = client.post(endpoint(sid, 'prepare'), json=request)
    assert response.status_code == 200, response.text
    return response.json()['prompt']


def answer(client, sid, prompt, text, key):
    turn = client.post(f'/v1/sessions/{sid}/agent-turn', json={
        'transcript': text, 'client_action_id': key + '-turn', 'cloud_text_allowed': True})
    assert turn.status_code == 200, turn.text
    turn = turn.json()
    assert turn['action']['type'] == 'submit_diagnostic_answer', turn
    response = client.post(endpoint(sid, 'answer'), json={**binding(prompt),
        'answer': turn['action']['answer'], 'authorized_turn_id': turn['agent_turn_id'],
        'client_action_id': key + '-answer'})
    assert response.status_code == 200, response.text
    return response.json()


def next_request(result):
    return {'track': result['prompt']['item']['track'],
            'preferred_item_id': result['prompt']['diagnostic_item_id'],
            'client_action_id': 'numeric-clarification:' + result['attempt']['attempt_id'],
            'numeric_clarification': result['numeric_clarification']}


def finish(client, sid):
    stages = ['check_in', 'classroom_recall', 'homework_capture', 'plan_review',
              'study', 'reflection', 'evolving', 'completed']
    for before, after in zip(stages, stages[1:]):
        if before == 'classroom_recall':
            response = client.post(f'/v1/sessions/{sid}/recalls/none', json={'client_action_id':'no-recall33'})
            assert response.status_code == 200, response.text
        response = client.post(f'/v1/sessions/{sid}/transition', json={'expected_state': before,
            'target_state': after, 'reason':'audit-history33', 'client_action_id':'end33-' + after})
        assert response.status_code == 200, response.text


def complete(client, db, result):
    with db.connect() as c:
        tid = c.execute('SELECT authorized_turn_id FROM diagnostic_prompt WHERE prompt_id=?',
            (result['prompt']['prompt_id'],)).fetchone()[0]
    response = client.put(f'/v1/agent-turns/{tid}/outcome', json={
        'final_response':'Original answer saved', 'tool_status':'completed',
        'result': {'execution_receipt': {'capability':'diagnostic.submit',
            'execution_location':'client_device_bridge', 'action_type':'submit_diagnostic_answer',
            'status':'completed', 'completed_at':utc_now(), 'output':{'attempt':result['attempt']}}}})
    assert response.status_code == 200, response.text
    assert response.json()['tool_status'] == 'completed'
    return tid


@pytest.mark.parametrize('fault', ['invalidated', 'source_deleted', 'evidence_withdrawn'])
@pytest.mark.parametrize('prepared', [False, True])
def test_grandparent_withdrawal_blocks_new_clarification(environment, monkeypatch, fault, prepared):
    _, db = environment
    with unclear(environment, monkeypatch) as (client, sid, root, request, _):
        second = answer(client, sid, prepare(client, sid, request), '答案是四五', 'second-unclear')
        third_request = next_request(second)
        third = prepare(client, sid, third_request) if prepared else None
        with db.connect() as c:
            if fault == 'invalidated':
                c.execute("UPDATE diagnostic_prompt SET status='invalidated' WHERE prompt_id=?", (root['prompt']['prompt_id'],))
            elif fault == 'source_deleted':
                c.execute("UPDATE agent_turn SET tool_error_code='SOURCE_DELETED' WHERE agent_turn_id=(SELECT authorized_turn_id FROM diagnostic_prompt WHERE prompt_id=?)", (root['prompt']['prompt_id'],))
            else:
                from home_study_agent.privacy_source_withdrawal import record
                c.execute('BEGIN')
                record(c, root['prompt']['child_id'], {'synthetic-parent-original'}, {root['attempt']['evidence_id']})
            c.commit()
            before = c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]
        if prepared:
            response = client.get(endpoint(sid), params={'prompt_id': third['prompt_id']})
            assert response.status_code == 200, response.text
            assert response.json()['status'] == 'invalidated', response.json()
        else:
            response = client.post(endpoint(sid, 'prepare'), json=third_request)
            assert response.status_code == 409, response.text
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == before


@pytest.mark.parametrize('other_child', [False, True])
def test_packet_and_replay_never_cross_session(environment, monkeypatch, other_child):
    _, db = environment
    with unclear(environment, monkeypatch) as (client, sid, root, request, _):
        successor = prepare(client, sid, request)
        with db.connect() as c:
            c.execute("UPDATE daily_session SET status='completed',state='completed' WHERE session_id=?", (sid,))
            c.commit()
        new_sid = start(client, OTHER if other_child else root['prompt']['child_id'], 'other-session')
        for key in [request['client_action_id'], 'cross-session-new-key']:
            response = client.post(endpoint(new_sid, 'prepare'), json={**request, 'client_action_id': key})
            assert response.status_code == 409, response.text
        response = client.get(endpoint(new_sid), params={'prompt_id': successor['prompt_id']})
        assert response.status_code == 409, response.text


def test_retry_after_new_focus_does_not_restore_old_waiting_prompt(environment, monkeypatch):
    _, db = environment
    with unclear(environment, monkeypatch) as (client, sid, root, request, _):
        successor = prepare(client, sid, request)
        cancelled = client.post(endpoint(sid, 'cancel'), json=binding(successor))
        assert cancelled.status_code == 200, cancelled.text
        fresh = prepare(client, sid, {'track':'school', 'preferred_item_id':'diag-p4-arithmetic-closed-1',
            'client_action_id':'fresh-focus'})
        replay = client.post(endpoint(sid, 'prepare'), json=request)
        assert replay.status_code == 200, replay.text
        assert replay.json()['status'] == 'cancelled'
        assert client.get(endpoint(sid)).json()['prompt']['prompt_id'] == fresh['prompt_id']
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 1


def test_completed_chain_history_is_read_only_after_session_end_and_restart(environment, monkeypatch):
    settings, db = environment
    with unclear(environment, monkeypatch, TCPClient) as (client, sid, root, request, gateway):
        successor = prepare(client, sid, request)
        completed = answer(client, sid, successor, '答案是六', 'known-original')
        assert completed['attempt']['score_status'] == 'practice_only'
        with db.connect() as c:
            original_rows = [dict(row) for row in c.execute('SELECT * FROM diagnostic_attempt ORDER BY rowid')]
        finish(client, sid)
    with TCPClient(create_app(settings, model_gateway=gateway)) as client:
        for prompt in [root['prompt'], successor]:
            receipt = client.get(endpoint(sid), params={'prompt_id': prompt['prompt_id']})
            assert receipt.status_code == 200, receipt.text
            assert receipt.json()['status'] == 'answered'
            assert not receipt.json().get('numeric_clarification')
        with db.connect() as c:
            c.execute('PRAGMA query_only=ON')
            before = c.total_changes
            result = LearningStateService._attempt(c, completed['attempt']['attempt_id'])
            assert result['correctness'] == 1 and result['score_status'] == 'practice_only'
            assert not result['mastery_update_allowed']
            assert c.total_changes == before
            assert [dict(row) for row in c.execute('SELECT * FROM diagnostic_attempt ORDER BY rowid')] == original_rows
    assert len(gateway.requests) == 1


def test_same_clarification_is_not_independent_when_answer_arrives_next_day(environment, monkeypatch):
    _, db = environment
    with unclear(environment, monkeypatch) as (client, sid, root, request, _):
        successor = prepare(client, sid, request)
        later = datetime.now(timezone.utc) + timedelta(hours=25)
        class Later(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls.fromtimestamp(later.timestamp(), tz)
            @classmethod
            def utcnow(cls):
                return cls.fromtimestamp(later.timestamp(), timezone.utc).replace(tzinfo=None)
        # Advance the application clock, never rewrite original receipt dates.
        for name, module in list(sys.modules.items()):
            if name.startswith('home_study_agent.') and getattr(module, 'datetime', None) is datetime:
                monkeypatch.setattr(module, 'datetime', Later)
        result = answer(client, sid, successor, '答案是六', 'delayed-clarification')
        assert result['attempt']['correctness'] == 1
        assert not result['attempt']['mastery_update_allowed'], result['attempt']
        with db.connect() as c:
            evidence = c.execute('SELECT independent FROM learning_evidence WHERE evidence_id=?',
                (result['attempt']['evidence_id'],)).fetchone()
            assert evidence[0] == 0
            # Legacy buggy writers may already have certified independence.
            c.execute('UPDATE learning_evidence SET independent=1 WHERE evidence_id=?',
                (result['attempt']['evidence_id'],))
            c.commit()
            c.execute('PRAGMA query_only=ON')
            before = c.total_changes
            legacy = LearningStateService._attempt(c, result['attempt']['attempt_id'])
            assert legacy['score_status'] == 'practice_only' and not legacy['mastery_update_allowed']
            assert c.total_changes == before


@pytest.mark.parametrize('withdraw', [False, True])
def test_tcp_lost_reply_chain_restart_and_ancestor_live_checks(environment, monkeypatch, withdraw):
    settings, db = environment
    with unclear(environment, monkeypatch, TCPClient) as (client, sid, root, request, gateway):
        tids = [complete(client, db, root)]
        second = answer(client, sid, prepare(client, sid, request), '答案是四五', 'middle33')
        tids.append(complete(client, db, second))
        third_request = next_request(second)
        prepared = prepare(client, sid, third_request)
        # Preparation committed; discard its reply at the client, then restart.
        with db.connect() as c:
            originals = [dict(row) for row in c.execute('SELECT * FROM diagnostic_attempt ORDER BY rowid')]
    with TCPClient(create_app(settings, model_gateway=gateway)) as client:
        replay = prepare(client, sid, third_request)
        assert replay['prompt_id'] == prepared['prompt_id']
        if withdraw:
            with db.connect() as c:
                c.execute("UPDATE agent_turn SET tool_error_code='SOURCE_DELETED' WHERE agent_turn_id=?", (tids[0],))
                c.commit()
            turn = client.post(f'/v1/sessions/{sid}/agent-turn', json={'transcript':'答案是六',
                'client_action_id':'withdrawn-final33', 'cloud_text_allowed':True})
            assert turn.status_code == 200, turn.text
            submitted = client.post(endpoint(sid, 'answer'), json={**binding(prepared),
                'answer':'六', 'authorized_turn_id':turn.json()['agent_turn_id'], 'client_action_id':'withdrawn-answer33'})
            assert submitted.status_code == 200, submitted.text
            assert submitted.json()['status'] == 'invalidated' and submitted.json()['attempt'] is None
        else:
            result = answer(client, sid, replay, '答案是六', 'final33')
            tids.append(complete(client, db, result))
            assert not result['attempt']['mastery_update_allowed']
            with db.connect() as c:
                originals.append(dict(c.execute('SELECT * FROM diagnostic_attempt WHERE attempt_id=?',
                    (result['attempt']['attempt_id'],)).fetchone()))
        with db.connect() as c:
            assert [dict(row) for row in c.execute('SELECT * FROM diagnostic_attempt ORDER BY rowid')] == originals
    with TCPClient(create_app(settings, model_gateway=gateway)) as client:
        restored = client.get(endpoint(sid), params={'prompt_id':prepared['prompt_id']})
        assert restored.status_code == 200, restored.text
        assert restored.json()['status'] == ('invalidated' if withdraw else 'answered')
        if not withdraw:
            assert restored.json()['attempt']['attempt_id'] == result['attempt']['attempt_id']
            with db.connect() as c:
                assert c.execute("SELECT COUNT(*) FROM agent_turn WHERE tool_status='completed'").fetchone()[0] == 3
                c.execute("UPDATE agent_turn SET tool_error_code='SOURCE_DELETED' WHERE agent_turn_id=?", (tids[0],))
                c.commit()
                c.execute('PRAGMA query_only=ON')
                before = c.total_changes
                changed = LearningStateService._attempt(c, result['attempt']['attempt_id'])
                assert changed['score_reason'] == 'numeric_clarification_source_changed'
                assert changed['score_status'] == 'needs_review' and not changed['mastery_update_allowed']
                assert c.total_changes == before
                assert [dict(row) for row in c.execute('SELECT * FROM diagnostic_attempt ORDER BY rowid')] == originals
    assert len(gateway.requests) == 1
    print({'db':str(db.path), 'withdraw':withdraw, 'attempts':len(originals),
           'completed_original_turns':len(tids), 'synthetic_generation_requests':len(gateway.requests), 'owned_workers':0})


def test_unrelated_original_still_keeps_independent_authority(environment, monkeypatch):
    _, db = environment
    with unclear(environment, monkeypatch) as (client, sid, root, request, _):
        fresh = prepare(client, sid, {'track':'school', 'preferred_item_id':'diag-p4-arithmetic-closed-1',
            'client_action_id':'independent-focus33'})
        result = answer(client, sid, fresh, '答案是12', 'independent-original33')
        assert result['attempt']['correctness'] == 0
        assert result['attempt']['mastery_update_allowed']


@pytest.mark.parametrize('fault', ['child', 'session', 'cycle', 'malformed',
    'null_request', 'false_request', 'number_request', 'string_request'])
def test_ancestor_binding_corruption_is_rejected_without_erasing_answers(environment, monkeypatch, fault):
    _, db = environment
    with unclear(environment, monkeypatch) as (client, sid, root, request, _):
        second = answer(client, sid, prepare(client, sid, request), '答案是四五', 'corrupt-middle33')
        third = prepare(client, sid, next_request(second))
        with db.connect() as c:
            originals = [dict(row) for row in c.execute('SELECT * FROM diagnostic_attempt ORDER BY rowid')]
            original_evidence = [dict(row) for row in c.execute('SELECT * FROM learning_evidence ORDER BY rowid')]
            if fault in {'child', 'session'}:
                column, value = ('child_id', OTHER) if fault == 'child' else ('origin_session_id', 'missing-session')
                # Corrupt only the binding to a separate, valid completed session.
                if fault == 'session':
                    value = 'corrupt-other-session33'
                    c.execute('''INSERT INTO daily_session(session_id,child_id,status,state,started_at,
                        ended_at,last_checkpoint_at,client_action_id) VALUES(?,?,'completed','completed',?,?,?,?)''',
                        (value, OTHER, utc_now(), utc_now(), utc_now(), value))
                c.execute(f'UPDATE diagnostic_prompt SET {column}=? WHERE prompt_id=?', (value, root['prompt']['prompt_id']))
            else:
                invalid_requests = {'malformed': [], 'null_request': None, 'false_request': False,
                    'number_request': 7, 'string_request': 'legacy request'}
                payload = invalid_requests[fault] if fault in invalid_requests else {**request,
                    'numeric_clarification': {**request['numeric_clarification'], 'parent_prompt_id':third['prompt_id']}}
                c.execute('UPDATE diagnostic_prompt SET request_json=? WHERE prompt_id=?',
                    (json.dumps(payload), second['prompt']['prompt_id']))
            old_prompt = dict(c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=?',
                (second['prompt']['prompt_id'],)).fetchone())
            c.commit()
        result = client.get(endpoint(sid), params={'prompt_id':third['prompt_id']})
        assert result.status_code == 200 and result.json()['status'] == 'invalidated', result.text
        with db.connect() as c:
            assert [dict(row) for row in c.execute('SELECT * FROM diagnostic_attempt ORDER BY rowid')] == originals
            assert [dict(row) for row in c.execute('SELECT * FROM learning_evidence ORDER BY rowid')] == original_evidence
            assert dict(c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=?',
                (second['prompt']['prompt_id'],)).fetchone()) == old_prompt
            assert old_prompt['owner_kind'] == 'diagnostic'


def test_read_only_source_validation_propagates_database_errors(environment, monkeypatch):
    from home_study_agent.numeric_clarification import source_reason
    _, db = environment
    with unclear(environment, monkeypatch) as (client, sid, root, request, _):
        successor = prepare(client, sid, request)
        with db.connect() as c:
            row = c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=?', (successor['prompt_id'],)).fetchone()
            c.execute('PRAGMA query_only=ON')
            assert source_reason(c, row) is None
            class Broken:
                def execute(self, *args):
                    raise sqlite3.OperationalError('synthetic read failure')
            with pytest.raises(sqlite3.OperationalError, match='synthetic read failure'):
                source_reason(Broken(), row)
