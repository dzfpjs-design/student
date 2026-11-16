"""Real host transactions with synthetic assets and a controlled media clock."""
from datetime import date, datetime, time, timedelta, timezone

import pytest

from home_study_agent.database import Database
from test_app import client_for
from test_companion_direct_start_atomic_http import task
from test_controlled_family_budget import media_clock
from test_controlled_start_atomic_http import BODY, CHILD, setup, counts
from test_learning_runtime_http import NoCloud


def configure_one_minute(client, db, clock, kind):
    login = client.post('/v1/parent/unlock', json={'pin': '4826'})
    assert login.status_code == 200, login.text
    changed = client.put(f'/v1/parent/children/{CHILD}/controlled-{kind}/rule',
        headers={'X-HSC-Parent': login.json()['parent_token']},
        json={'enabled': True, 'max_daily_minutes': 1, 'max_item_minutes': 1})
    assert changed.status_code == 200, changed.text
    with db.connect() as c:
        today = date.fromisoformat(c.execute("SELECT date('now')").fetchone()[0])
    clock['now'] = datetime.combine(today, time(12), tzinfo=timezone.utc)


@pytest.mark.parametrize('kind', ['content', 'games'])
def test_http_remaining_second_refusal_keeps_resumed_learning_task_untouched(tmp_path, kind, media_clock):
    db = Database(tmp_path / 'test.db')
    with client_for(tmp_path, model_gateway=NoCloud(), agent_runtime_enabled=True) as client:
        sid, pid = setup(client, db, tmp_path, kind)
        configure_one_minute(client, db, media_clock, kind)
        path = f'/v1/sessions/{sid}/controlled-{kind}'
        first = client.post(path + '/start', json=BODY)
        assert first.status_code == 200, first.text
        key = 'game_session_id' if kind == 'games' else 'content_session_id'
        media_clock['now'] += timedelta(seconds=59)
        ended = client.post(path + '/end', json={key: first.json()[key], 'reason': 'child_requested', 'actual_seconds': 59})
        assert ended.status_code == 200, ended.text
        resumed = client.post(f'/v1/plan-items/{pid}/resume', json={'client_action_id': 'resume-before-budget-refusal'})
        assert resumed.status_code == 200, resumed.text
        before = task(db, pid)
        blocked = client.post(path + '/start', json={**BODY, 'client_action_id': 'no-extra-whole-minute'})
        assert blocked.status_code == 409, blocked.text
        assert '不足一分钟' in blocked.json()['message']
        assert task(db, pid) == before
        assert counts(db, pid, kind) == (1, 1, 1)
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
            assert c.execute('SELECT COUNT(*) FROM model_call').fetchone()[0] == 0


@pytest.mark.parametrize('kind', ['content', 'games'])
def test_http_restart_preserves_unconfirmed_budget_and_original_readonly_receipt(tmp_path, kind, media_clock):
    db = Database(tmp_path / 'test.db')
    with client_for(tmp_path, model_gateway=NoCloud(), agent_runtime_enabled=True) as client:
        sid, pid = setup(client, db, tmp_path, kind)
        configure_one_minute(client, db, media_clock, kind)
        path = f'/v1/sessions/{sid}/controlled-{kind}'
        first = client.post(path + '/start', json=BODY)
        assert first.status_code == 200, first.text
        key = 'game_session_id' if kind == 'games' else 'content_session_id'
        media_clock['now'] += timedelta(seconds=2)
    with client_for(tmp_path, model_gateway=NoCloud(), agent_runtime_enabled=True) as client:
        replay = client.post(path + '/start', json=BODY)
        assert replay.status_code == 200, replay.text
        assert replay.json()[key] == first.json()[key]
        assert replay.json()['status'] == 'interrupted'
        assert replay.json()['replayed'] is True and replay.json()['launch_allowed'] is False
        assert replay.json()['actual_seconds'] == 2
        blocked = client.post(path + '/start', json={**BODY, 'client_action_id': 'new-key-cannot-refund-crash'})
        assert blocked.status_code == 409, blocked.text
        assert counts(db, pid, kind) == (1, 1, 1)
        again = client.post(path + '/start', json=BODY)
        assert again.status_code == 200 and again.json() == replay.json()
        assert task(db, pid)['status'] == 'paused'
