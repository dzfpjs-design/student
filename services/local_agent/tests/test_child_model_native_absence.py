"""Persisted native absence evidence is not a child's entertainment activity."""
import json
from uuid import uuid4

import pytest

from home_study_agent.child_model import ChildModelService
from home_study_agent.controlled_game import ControlledGameService
from home_study_agent.database import Database
from test_controlled_game import seed


@pytest.fixture
def home(tmp_path):
    db = Database(tmp_path / 'model.db')
    db.initialize()
    sid, item = seed(db, tmp_path / 'approved.exe')
    service = ControlledGameService(db)
    service.update_rule('child-game', enabled=True, max_daily_minutes=20, max_item_minutes=5)
    service.approve('child-game', 'Approved', str(tmp_path / 'approved.exe'), 5)
    # Read-side contract fixture for the native writer's optional receipt schema.
    with db.connect() as c:
        c.execute('''CREATE TABLE controlled_launch_receipt(intent_id TEXT PRIMARY KEY,
            child_id TEXT,session_id TEXT,kind TEXT,request_id TEXT,
            content_session_id TEXT,game_session_id TEXT)''')
        c.commit()
    return db, service, sid, item


def stopped(home, *, trusted=True):
    db, service, sid, item = home
    request = str(uuid4())
    started = service.start(sid, 'Approved', request, item)
    activity = started['game_session_id']
    service.end(sid, 'native_not_dispatched', game_session_id=activity)
    intent, audit = str(uuid4()), str(uuid4())
    with db.connect() as c:
        row = dict(c.execute('SELECT * FROM controlled_game_session WHERE game_session_id=?', (activity,)).fetchone())
        if trusted:
            payload = {'game_session_id': activity, 'native_intent_id': intent,
                       'reason': 'native_not_dispatched', 'proof': 'cancelled_before_dispatch',
                       'native_nonce': str(uuid4()), 'native_revision': 3,
                       'previous_status': 'active', 'previous_inferred_seconds': 0, 'actual_seconds': 0}
            c.execute('INSERT INTO controlled_launch_receipt VALUES(?,?,?,?,?,NULL,?)',
                      (intent, 'child-game', sid, 'game', request, activity))
            c.execute('INSERT INTO audit_event(audit_id,actor,action,child_id,summary_json,occurred_at) VALUES(?,?,?,?,?,?)',
                      (audit, 'native', 'controlled_game_ended', 'child-game', json.dumps(payload), row['ended_at']))
            c.commit()
    return activity, intent, audit


def balance(db):
    return ChildModelService(db).snapshot('child-game')['payload']['companion_balance']


def test_fifteen_proven_unstarted_games_do_not_trigger_parent_overuse_advice(home):
    for _ in range(15):
        stopped(home)
    result = balance(home[0])
    assert result['game_sessions'] == result['total_sessions'] == 0
    assert result['not_dispatched_launches'] == 15
    assert result['usage_boundary_status'] == 'no_records'
    assert result['boundary_status'] == 'no_evidence'
    assert '使用偏高' not in result['guidance']
    assert balance(Database(home[0].path)) == result
    with home[0].connect() as c:
        assert c.execute('SELECT COUNT(*) FROM controlled_game_session').fetchone()[0] == 15


def test_end_reason_alone_does_not_hide_an_activity(home):
    stopped(home, trusted=False)
    assert balance(home[0])['game_sessions'] == 1


@pytest.mark.parametrize('fault', ['actor', 'owner', 'session', 'request', 'kind', 'intent',
                                   'proof', 'nonce', 'revision', 'time', 'seconds', 'process', 'malformed', 'binding_type'])
def test_mismatched_absence_proof_cannot_remove_activity_from_usage(home, fault):
    activity, intent, audit = stopped(home)
    with home[0].connect() as c:
        if fault in {'owner', 'session', 'request', 'kind'}:
            field = {'owner': 'child_id', 'session': 'session_id', 'request': 'request_id', 'kind': 'kind'}[fault]
            c.execute(f'UPDATE controlled_launch_receipt SET {field}=? WHERE intent_id=?', ('different', intent))
        elif fault == 'actor':
            c.execute("UPDATE audit_event SET actor='agent' WHERE audit_id=?", (audit,))
        elif fault == 'time':
            c.execute("UPDATE audit_event SET occurred_at='2000-01-01T00:00:00+00:00' WHERE audit_id=?", (audit,))
        elif fault in {'seconds', 'process'}:
            field = 'actual_seconds' if fault == 'seconds' else 'os_process_id'
            c.execute(f'UPDATE controlled_game_session SET {field}=1 WHERE game_session_id=?', (activity,))
        else:
            payload = json.loads(c.execute('SELECT summary_json FROM audit_event WHERE audit_id=?', (audit,)).fetchone()[0])
            field = {'intent': 'native_intent_id', 'proof': 'proof', 'nonce': 'native_nonce', 'revision': 'native_revision'}.get(fault)
            if field:
                payload[field] = True if fault == 'revision' else 'different'
            if fault == 'binding_type':
                payload['native_intent_id'] = []
            c.execute('UPDATE audit_event SET summary_json=? WHERE audit_id=?',
                      ('{broken' if fault == 'malformed' else json.dumps(payload), audit))
        c.commit()
    assert balance(home[0])['game_sessions'] == 1


def test_proof_withdrawal_changes_projection_without_deleting_source_history(home):
    _, _, audit = stopped(home)
    first = balance(home[0])
    assert first['game_sessions'] == 0
    with home[0].connect() as c:
        c.execute('DELETE FROM audit_event WHERE audit_id=?', (audit,))
        c.commit()
    second = balance(Database(home[0].path))
    assert second['game_sessions'] == 1 and second['not_dispatched_launches'] == 0
    assert first['not_dispatched_evidence_fingerprint'] != second['not_dispatched_evidence_fingerprint']
