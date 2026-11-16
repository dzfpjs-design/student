"""Game start reservation only: synthetic executables are never launched."""
import pytest

from home_study_agent.controlled_game import ControlledGameError, ControlledGameService
from home_study_agent.database import Database
from home_study_agent.repository import StudyRepository
from test_app import client_for
from test_companion_direct_start_atomic_http import task
from test_controlled_game import seed
from test_controlled_start_atomic_http import BODY, CHILD, setup, counts
from test_learning_runtime_http import NoCloud


@pytest.fixture
def game(tmp_path):
    db = Database(tmp_path / 'game.db')
    db.initialize()
    executable = tmp_path / 'Synthetic.exe'
    sid, pid = seed(db, executable)
    service = ControlledGameService(db)
    service.update_rule('child-game', enabled=True, max_daily_minutes=20, max_item_minutes=8)
    approval = service.approve('child-game', 'Synthetic', str(executable), 6)
    return db, service, sid, pid, executable, approval


def test_game_borrowed_start_does_not_commit_outcome_or_original_caller_transaction(game):
    db, service, sid, pid, _, _ = game
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        created = service.start(sid, 'Synthetic', 'borrowed-original', pid, connection=c)
        assert created['launch_allowed'] is True and c.in_transaction
        assert c.execute('SELECT COUNT(*) FROM companion_activity_outcome').fetchone()[0] == 1
        with db.connect() as reader:
            assert reader.execute('SELECT COUNT(*) FROM controlled_game_session').fetchone()[0] == 0
            assert reader.execute('SELECT COUNT(*) FROM companion_activity_outcome').fetchone()[0] == 0
        c.rollback()
        for table in ('controlled_game_session', 'companion_activity_outcome', 'companion_activity_outcome_event'):
            assert c.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM audit_event WHERE action='controlled_game_started'").fetchone()[0] == 0
    assert service.start(sid, 'Synthetic', 'borrowed-original', pid)['launch_allowed'] is True


@pytest.mark.parametrize('failure', ['session', 'active', 'disabled', 'budget', 'approval', 'file'])
def test_game_rejection_leaves_borrowed_transaction_under_caller_control(game, failure):
    db, service, sid, pid, executable, approval = game
    if failure == 'session':
        with db.connect() as c:
            c.execute("UPDATE daily_session SET status='completed' WHERE session_id=?", (sid,))
            c.commit()
    elif failure == 'active':
        service.start(sid, 'Synthetic', 'earlier-game', pid)
    elif failure == 'disabled':
        service.update_rule('child-game', enabled=False, max_daily_minutes=20, max_item_minutes=8)
    elif failure == 'budget':
        service.update_rule('child-game', enabled=True, max_daily_minutes=1, max_item_minutes=1)
        spent = service.start(sid, 'Synthetic', 'spent-real-budget', pid)
        ended = service.end(sid, 'time_limit', 60, game_session_id=spent['game_session_id'])
        assert ended['actual_seconds'] == 60 and ended['allowed_minutes'] == 1
    elif failure == 'approval':
        service.revoke('child-game', approval['approval_id'])
    else:
        executable.write_bytes(b'changed inert file after approval')
    with db.connect() as c:
        original = c.execute("SELECT nickname FROM child_profile WHERE child_id='child-game'").fetchone()[0]
        c.execute('BEGIN IMMEDIATE')
        c.execute("UPDATE child_profile SET nickname='uncommitted caller change' WHERE child_id='child-game'")
        with pytest.raises(ControlledGameError):
            service.start(sid, 'Synthetic', 'rejected-game', pid, connection=c)
        assert c.in_transaction, 'The borrowed service must not roll back the caller'
        assert c.execute("SELECT nickname FROM child_profile WHERE child_id='child-game'").fetchone()[0] == 'uncommitted caller change'
        with db.connect() as reader:
            assert reader.execute("SELECT nickname FROM child_profile WHERE child_id='child-game'").fetchone()[0] == original
        c.rollback()


def test_game_borrowing_rejects_nontransaction_and_foreign_database(game, tmp_path):
    db, service, sid, pid, _, _ = game
    with db.connect() as c:
        with pytest.raises(ControlledGameError, match='TRANSACTION_REQUIRED'):
            service.start(sid, 'Synthetic', 'no-transaction', pid, connection=c)
    foreign = Database(tmp_path / 'foreign.db')
    foreign.initialize()
    with foreign.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        with pytest.raises(ControlledGameError, match='TRANSACTION_OWNER_MISMATCH'):
            service.start(sid, 'Synthetic', 'foreign-transaction', pid, connection=c)
        assert c.in_transaction
        c.rollback()


def test_game_historical_receipt_does_not_grant_launch_after_revocation_or_file_removal(game):
    db, service, sid, pid, executable, approval = game
    first = service.start(sid, 'Synthetic', 'historical-original', pid)
    ended = service.end(sid, 'child_requested', 10, game_session_id=first['game_session_id'])
    service.revoke('child-game', approval['approval_id'])
    executable.unlink()
    replay = service.start(sid, 'Synthetic', 'historical-original', pid)
    assert replay['game_session_id'] == first['game_session_id']
    assert replay['status'] == ended['status'] == 'stopped'
    assert replay['replayed'] is True and replay['launch_allowed'] is False
    assert replay['actual_seconds'] == ended['actual_seconds']
    with pytest.raises(ControlledGameError, match='许可目录'):
        service.start(sid, 'Synthetic', 'new-unapproved-start', pid)
    with db.connect() as c:
        c.execute('DELETE FROM companion_activity_outcome_event')
        c.execute('DELETE FROM companion_activity_outcome')
        c.commit()
    with pytest.raises(ControlledGameError, match='IDEMPOTENCY_CONFLICT'):
        service.start(sid, 'Synthetic', 'historical-original', pid)
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM companion_activity_outcome').fetchone()[0] == 0


def test_game_original_http_replay_does_not_pause_a_resumed_task(tmp_path):
    db = Database(tmp_path / 'test.db')
    with client_for(tmp_path, model_gateway=NoCloud(), agent_runtime_enabled=True) as client:
        sid, pid = setup(client, db, tmp_path, 'games')
        path = f'/v1/sessions/{sid}/controlled-games'
        started = client.post(path + '/start', json=BODY)
        assert started.status_code == 200, started.text
        closed = client.post(path + '/end', json={'game_session_id': started.json()['game_session_id'],
            'reason': 'child_requested', 'actual_seconds': 1})
        assert closed.status_code == 200, closed.text
        resumed = client.post(f'/v1/plan-items/{pid}/resume', json={'client_action_id': 'real-later-task-resume'})
        assert resumed.status_code == 200, resumed.text
        before = task(db, pid)
        replay = client.post(path + '/start', json=BODY)
        assert replay.status_code == 200, replay.text
        assert replay.json()['game_session_id'] == started.json()['game_session_id']
        assert replay.json()['status'] == 'stopped' and replay.json()['launch_allowed'] is False
        assert task(db, pid) == before
        assert counts(db, pid, 'games') == (1, 1, 1)


def test_game_original_legacy_pause_cannot_acknowledge_a_new_running_segment(tmp_path):
    db = Database(tmp_path / 'test.db')
    with client_for(tmp_path, model_gateway=NoCloud(), agent_runtime_enabled=True) as client:
        sid, pid = setup(client, db, tmp_path, 'games')
        StudyRepository(db).pause_plan_item(pid, BODY['client_action_id'] + ':pause', 'controlled_game')
        resumed = client.post(f'/v1/plan-items/{pid}/resume', json={'client_action_id': 'legacy-start-compensation'})
        assert resumed.status_code == 200, resumed.text
        before = task(db, pid)
        result = client.post(f'/v1/sessions/{sid}/controlled-games/start', json=BODY)
        assert result.status_code == 409, result.text
        assert task(db, pid) == before and counts(db, pid, 'games') == (1, 0, 0)


def test_game_http_changed_executable_rejection_does_not_pause_original_task(tmp_path):
    db = Database(tmp_path / 'test.db')
    with client_for(tmp_path, model_gateway=NoCloud(), agent_runtime_enabled=True) as client:
        sid, pid = setup(client, db, tmp_path, 'games')
        before = task(db, pid)
        (tmp_path / 'SyntheticExercise.exe').write_bytes(b'changed inert file after parent approval')
        rejected = client.post(f'/v1/sessions/{sid}/controlled-games/start', json=BODY)
        assert rejected.status_code == 409, rejected.text
        assert '重新许可' in rejected.json()['message']
        assert task(db, pid) == before and counts(db, pid, 'games') == (0, 0, 0)
