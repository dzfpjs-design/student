"""Real SQLite + original synthetic Gateway; no paid or device requests."""
import json

import pytest

from home_study_agent.realtime_output_authority import RealtimeOutputConflict
from test_realtime_output_authority import case, proof, seed_projection


ACTOR = 'public_reply_rejection_diagnostic_v1'


def rows(db):
    with db.connect() as connection:
        return connection.execute('SELECT child_id,action,summary_json FROM audit_event WHERE actor=? ORDER BY rowid', (ACTOR,)).fetchall()


@pytest.mark.parametrize('mode,reason', [
    ('missing_identity', 'TRANSPORT_IDENTITY_MISSING'),
    ('protected_request', 'PROTECTED_REQUEST_CONTEXT'),
])
def test_rejection_is_durable_value_free_and_does_not_create_output_authority(case, mode, reason):
    db, scope, authority = case
    with db.connect() as connection:
        connection.execute("DELETE FROM audit_event WHERE actor='public_reply_projection_v1'")
        connection.commit()
    context = {'private': {'local_only': True, 'text': '合成公共问候'}} if mode == 'protected_request' else None
    mutate = (lambda payload: payload.pop('id')) if mode == 'missing_identity' else None
    _, reference = seed_projection(db, scope, context=context, mutate=mutate)
    assert reference is None
    saved = rows(db)
    assert len(saved) == 1
    assert saved[0]['child_id'] == scope['child_id']
    assert saved[0]['action'] == 'public_reply_rejected'
    summary = json.loads(saved[0]['summary_json'])
    assert summary == {'version': ACTOR, 'reason': reason, 'export_allowed': False}
    assert all(value not in saved[0]['summary_json'] for value in [scope['child_id'], scope['session_id'], scope['agent_turn_id'], '合成公共问候', 'synthetic-key'])
    from home_study_agent.database import Database
    reopened = Database(db.path)
    assert [dict(item) for item in rows(reopened)] == [dict(item) for item in saved]
    with pytest.raises(RealtimeOutputConflict, match='PUBLIC_SOURCE_INVALID'):
        authority.execute(proof(scope))
    with db.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM audit_event WHERE actor='public_reply_projection_v1'").fetchone()[0] == 0


def test_accepted_original_reply_retains_original_two_receipts_and_no_rejection(case):
    db, scope, authority = case
    assert rows(db) == []
    with db.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM audit_event WHERE actor='public_reply_projection_v1'").fetchone()[0] == 2
    assert authority.execute(proof(scope))['state'] == 'reserved'


def test_repeated_rejection_keeps_first_finite_reason_once_without_model_replay(case, monkeypatch):
    from home_study_agent import public_reply_projection as module
    db, scope, _ = case
    original = module.record_candidate
    called = []
    def twice(connection, **kwargs):
        called.append(kwargs['call_id'])
        assert original(connection, **kwargs) is None
        assert original(connection, **kwargs) is None
        return None
    monkeypatch.setattr(module, 'record_candidate', twice)
    _, reference = seed_projection(db, scope, mutate=lambda payload: payload.pop('id'))
    assert reference is None and len(called) == 1
    assert len(rows(db)) == 1
    assert json.loads(rows(db)[0]['summary_json'])['reason'] == 'TRANSPORT_IDENTITY_MISSING'
    with db.connect() as connection:
        model = connection.execute('SELECT status,verification_status,safety_status,error_code FROM model_call WHERE call_id=?', (called[0],)).fetchone()
        assert dict(model) == {'status': 'completed', 'verification_status': 'verified', 'safety_status': 'passed', 'error_code': None}


def test_rejection_diagnostic_uses_original_transaction_and_rolls_back(case):
    from types import SimpleNamespace
    from home_study_agent.public_reply_projection import record_candidate
    db, _, _ = case
    with pytest.raises(RuntimeError, match='synthetic-rollback'):
        with db.connect() as connection:
            call_id = connection.execute('SELECT call_id FROM model_call ORDER BY rowid DESC LIMIT 1').fetchone()[0]
            record_candidate(connection, call_id=call_id, action_id='unused',
                result=SimpleNamespace(reply_provenance=None), decision=None, messages=(),
                context={}, known_child_names=[], routes=())
            assert connection.execute('SELECT COUNT(*) FROM audit_event WHERE actor=?', (ACTOR,)).fetchone()[0] == 1
            raise RuntimeError('synthetic-rollback')
    assert rows(db) == []
