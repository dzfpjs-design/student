"""Playback-current GET through the real app, not a substituted Runtime."""
from contextlib import ExitStack
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from test_behavior_observation_coverage import seed_managed_observation
from test_behavior_proactive_source_current import queued


class NoModel:
    async def complete(self, request):
        raise AssertionError('A current-source GET must not call a model')


@pytest.fixture
def live_app(tmp_path, monkeypatch):
    monkeypatch.setenv('HSC_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('HSC_DATABASE_PATH', str(tmp_path / 'agent.db'))
    settings = Settings(data_dir=tmp_path, database_path=tmp_path / 'agent.db', maintenance_enabled=False)
    with ExitStack() as stack:
        app = create_app(settings, model_gateway=NoModel())
        client = stack.enter_context(TestClient(app))
        yield SimpleNamespace(app=app, client=client, settings=settings, stack=stack)


@pytest.fixture
def managed(live_app, tmp_path):
    # Build the existing source fixture after app startup. Starting an app after
    # it has an active task legitimately pauses that task; never fake its resume.
    return seed_managed_observation(live_app.app.state.repository.database)


def saved(db):
    with db.connect() as c:
        return tuple(c.iterdump())


def current(client, db, prompt_id, session_id='session', *, status=200, value=None):
    before = saved(db)
    response = client.get(f'/v1/sessions/{session_id}/agent/proactive-prompts/{prompt_id}/current')
    assert response.status_code == status, response.text
    assert response.headers['cache-control'] == 'no-store'
    if status == 200:
        assert response.json() == {'prompt_id': prompt_id, 'current': value}
    assert saved(db) == before, 'Read must not acknowledge, expire, regenerate, or otherwise mutate the DB.'
    return response


def test_actual_get_current_is_read_only_and_does_not_acknowledge(live_app, queued):
    db, service, store, prompt, clock = queued
    assert live_app.app.state.agent_runtime is not store
    current(live_app.client, db, prompt['prompt_id'], value=True)
    current(live_app.client, db, prompt['prompt_id'], value=True)
    with db.connect() as c:
        row = c.execute('SELECT * FROM agent_proactive_prompt WHERE prompt_id=?', (prompt['prompt_id'],)).fetchone()
        assert row['delivery_attempts'] == 1 and row['acknowledged_at'] is None
        assert row['message_text'] == prompt['message']


def test_stopped_source_stays_not_current_after_real_app_restart(live_app, queued):
    db, service, store, prompt, clock = queued
    pid = prompt['prompt_id']
    current(live_app.client, db, pid, value=True)
    service.stop_habit_experiment('goal', 'child-primary-4', clock[0].isoformat(), 'stop-before-current-get')
    current(live_app.client, db, pid, value=False)
    with db.connect() as c:
        original = dict(c.execute('SELECT * FROM agent_proactive_prompt WHERE prompt_id=?', (pid,)).fetchone())
    live_app.stack.close()
    with TestClient(create_app(live_app.settings, model_gateway=NoModel())) as client:
        current(client, db, pid, value=False)
        with db.connect() as c:
            assert dict(c.execute('SELECT * FROM agent_proactive_prompt WHERE prompt_id=?', (pid,)).fetchone()) == original


def test_pending_automatic_stop_cannot_revive_a_saved_prompt_after_restart(live_app, queued):
    db, _, _, prompt, clock = queued
    pid = prompt['prompt_id']
    current(live_app.client, db, pid, value=True)
    with db.connect() as c:
        c.execute('''INSERT INTO habit_experiment_event(event_id,experiment_id,child_id,event_type,payload_json,occurred_at,client_action_id)
                     VALUES('http-synthetic-pending','goal','child-primary-4','auto_stop_pending','{}',?,'http-synthetic-pending-action')''',
                  (clock[0].isoformat(),))
        c.commit()
    current(live_app.client, db, pid, value=False)
    live_app.stack.close()
    with TestClient(create_app(live_app.settings, model_gateway=NoModel())) as client:
        current(client, db, pid, value=False)


def test_other_session_and_missing_prompt_are_404_and_no_store(live_app, queued):
    db, _, _, prompt, _ = queued
    created = live_app.client.post('/v1/sessions/session/switch-child', json={
        'target_child_id': 'child-junior-3', 'client_action_id': 'other-child-proactive-current'})
    assert created.status_code == 200, created.text
    other = created.json()['session_id']
    rejected = current(live_app.client, db, prompt['prompt_id'], other, status=404)
    assert prompt['message'] not in rejected.text
    current(live_app.client, db, 'missing-prompt', status=404)
    current(live_app.client, db, prompt['prompt_id'], 'missing-session', status=404)
