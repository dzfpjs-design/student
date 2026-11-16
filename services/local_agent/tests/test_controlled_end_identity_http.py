"""Actual local HTTP; inert assets only, no playback, executable or cloud call."""
import pytest
from fastapi.testclient import TestClient

from home_study_agent.database import Database
from test_app import client_for
from test_controlled_start_atomic_http import BODY, setup, table, DropFirstSuccessfulReply
from test_companion_direct_start_atomic_http import task
from test_learning_runtime_http import NoCloud


def identity_key(kind):
    return 'game_session_id' if kind == 'games' else 'content_session_id'


def stored(db, kind, identity):
    with db.connect() as c:
        return dict(c.execute(f'SELECT * FROM {table(kind)} WHERE {identity_key(kind)}=?',
                              (identity,)).fetchone())


@pytest.mark.parametrize('kind', ['content', 'games'])
def test_late_old_closed_http_cannot_settle_new_controlled_activity(tmp_path, kind):
    db = Database(tmp_path / 'test.db')
    with client_for(tmp_path, model_gateway=NoCloud(), agent_runtime_enabled=True) as client:
        sid, _ = setup(client, db, tmp_path, kind)
        path = f'/v1/sessions/{sid}/controlled-{kind}'
        first = client.post(path + '/start', json=BODY)
        assert first.status_code == 200, first.text
        key, old_id = identity_key(kind), first.json()[identity_key(kind)]
        closed = client.post(path + '/end', json={key: old_id, 'reason': 'child_requested', 'actual_seconds': 1})
        assert closed.status_code == 200, closed.text
        second = client.post(path + '/start', json={**BODY, 'client_action_id': 'second-controlled-start'})
        assert second.status_code == 200, second.text
        new_id = second.json()[key]
        old_before, new_before = stored(db, kind, old_id), stored(db, kind, new_id)
        late = client.post(path + '/end', json={key: old_id, 'reason': 'late_old_window_closed', 'actual_seconds': 300})
        assert late.status_code == 200, late.text
        assert late.json()[key] == old_id, 'Old-window completion must never select current active media'
        assert late.json()['replayed'] is True
        assert stored(db, kind, old_id) == old_before
        assert stored(db, kind, new_id) == new_before
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0


@pytest.mark.parametrize('kind', ['content', 'games'])
def test_close_without_original_identity_does_not_choose_latest(tmp_path, kind):
    db = Database(tmp_path / 'test.db')
    with client_for(tmp_path, model_gateway=NoCloud(), agent_runtime_enabled=True) as client:
        sid, _ = setup(client, db, tmp_path, kind)
        path = f'/v1/sessions/{sid}/controlled-{kind}'
        first = client.post(path + '/start', json=BODY)
        assert first.status_code == 200, first.text
        identity = first.json()[identity_key(kind)]
        before = stored(db, kind, identity)
        result = client.post(path + '/end', json={'reason': 'legacy_window_closed', 'actual_seconds': 200})
        assert result.status_code == 409, result.text
        assert stored(db, kind, identity) == before
        for bad_id in (None, 'missing-controlled-identity'):
            invalid = client.post(path + '/end', json={identity_key(kind): bad_id,
                'reason': 'legacy_window_closed', 'actual_seconds': 200})
            assert invalid.status_code == 409, invalid.text
            assert stored(db, kind, identity) == before


@pytest.mark.parametrize('kind', ['content', 'games'])
def test_close_identity_from_other_child_is_rejected_not_retargeted(tmp_path, kind):
    db = Database(tmp_path / 'test.db')
    with client_for(tmp_path, model_gateway=NoCloud(), agent_runtime_enabled=True) as client:
        sid, _ = setup(client, db, tmp_path, kind)
        first = client.post(f'/v1/sessions/{sid}/controlled-{kind}/start', json=BODY)
        assert first.status_code == 200, first.text
        identity = first.json()[identity_key(kind)]
        switched = client.post(f'/v1/sessions/{sid}/switch-child', json={
            'target_child_id': 'child-primary-4', 'client_action_id': 'close-identity-other-child'})
        assert switched.status_code == 200, switched.text
        other_sid = switched.json()['session_id']
        before = stored(db, kind, identity)
        result = client.post(f'/v1/sessions/{other_sid}/controlled-{kind}/end', json={
            identity_key(kind): identity, 'reason': 'old_other_child_window_closed', 'actual_seconds': 300})
        assert result.status_code == 409, result.text
        assert stored(db, kind, identity) == before


@pytest.mark.parametrize('kind', ['content', 'games'])
@pytest.mark.parametrize('closed_before_restart', [False, True])
def test_restart_reuses_existing_identity_without_rewriting_end_or_new_activity(tmp_path, kind, closed_before_restart):
    db = Database(tmp_path / 'test.db')
    app = client_for(tmp_path, model_gateway=NoCloud(), agent_runtime_enabled=True).app
    wrapper = DropFirstSuccessfulReply(app, '')
    with TestClient(wrapper, raise_server_exceptions=False) as client:
        sid, pid = setup(client, db, tmp_path, kind)
        path = f'/v1/sessions/{sid}/controlled-{kind}'
        first = client.post(path + '/start', json=BODY)
        assert first.status_code == 200, first.text
        key, old_id = identity_key(kind), first.json()[identity_key(kind)]
        if closed_before_restart:
            wrapper.path = path + '/end'
            lost = client.post(path + '/end', json={key: old_id,
                'reason': 'child_requested', 'actual_seconds': 25})
            assert wrapper.dropped and lost.status_code == 500
            assert stored(db, kind, old_id)['status'] == 'stopped'
        with db.connect() as c:
            old_schema = [tuple(row) for row in c.execute("SELECT name,sql FROM sqlite_master WHERE name LIKE 'controlled_%' ORDER BY name")]
    with client_for(tmp_path, model_gateway=NoCloud(), agent_runtime_enabled=True) as client:
        # No new columns or inferred identity are needed for the existing media row.
        old_before = stored(db, kind, old_id)
        assert old_before['status'] == ('stopped' if closed_before_restart else 'interrupted')
        second = client.post(path + '/start', json={**BODY, 'client_action_id': 'new-media-after-restart'})
        assert second.status_code == 200, second.text
        new_id = second.json()[key]
        new_before, task_before = stored(db, kind, new_id), task(db, pid)
        late = client.post(path + '/end', json={key: old_id, 'reason': 'late_after_restart', 'actual_seconds': 300})
        assert late.status_code == 200, late.text
        assert late.json()[key] == old_id and late.json()['replayed'] is True
        assert late.json()['task_resumed'] is False and late.json()['launch_allowed'] is False
        assert stored(db, kind, old_id) == old_before
        assert stored(db, kind, new_id) == new_before
        assert task(db, pid) == task_before
        with db.connect() as c:
            assert [tuple(row) for row in c.execute("SELECT name,sql FROM sqlite_master WHERE name LIKE 'controlled_%' ORDER BY name")] == old_schema
            action = 'controlled_game_ended' if kind == 'games' else 'controlled_content_ended'
            assert c.execute('SELECT COUNT(*) FROM audit_event WHERE action=?', (action,)).fetchone()[0] == int(closed_before_restart)
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0


@pytest.mark.parametrize('kind', ['content', 'games'])
def test_end_audit_failure_rolls_back_then_exact_retry_records_once(tmp_path, kind):
    db = Database(tmp_path / 'test.db')
    app = client_for(tmp_path, model_gateway=NoCloud(), agent_runtime_enabled=True).app
    with TestClient(app, raise_server_exceptions=False) as client:
        sid, pid = setup(client, db, tmp_path, kind)
        path = f'/v1/sessions/{sid}/controlled-{kind}'
        first = client.post(path + '/start', json=BODY)
        assert first.status_code == 200, first.text
        key, identity = identity_key(kind), first.json()[identity_key(kind)]
        before, task_before = stored(db, kind, identity), task(db, pid)
        action = 'controlled_game_ended' if kind == 'games' else 'controlled_content_ended'
        with db.connect() as c:
            c.execute(f"CREATE TRIGGER fail_end_audit BEFORE INSERT ON audit_event WHEN NEW.action='{action}' BEGIN SELECT RAISE(ABORT,'isolated end failure'); END")
            c.commit()
        payload = {key: identity, 'reason': 'child_requested', 'actual_seconds': 14_400}
        failed = client.post(path + '/end', json=payload)
        assert failed.status_code == 500, failed.text
        assert stored(db, kind, identity) == before and task(db, pid) == task_before
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM audit_event WHERE action=?', (action,)).fetchone()[0] == 0
            c.execute('DROP TRIGGER fail_end_audit')
            c.commit()
        ended = client.post(path + '/end', json=payload)
        assert ended.status_code == 200, ended.text
        assert ended.json()['actual_seconds'] == before['allowed_minutes'] * 60
        assert ended.json()['replayed'] is False
        saved = stored(db, kind, identity)
        replay = client.post(path + '/end', json=payload)
        assert replay.status_code == 200 and replay.json()['replayed'] is True
        assert stored(db, kind, identity) == saved and task(db, pid) == task_before
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM audit_event WHERE action=?', (action,)).fetchone()[0] == 1
