"""Synthetic corrections must not race a report's final external submission."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import datetime, timezone
from threading import Event

import pytest

from home_study_agent.behavior_model import BehaviorModelService, ObservableEvent
from home_study_agent.daily_report_frozen_dependencies import _hash
from home_study_agent.database import Database
from home_study_agent.observation_engine import ObservationEngine
from home_study_agent.repository import StudyRepository


@pytest.mark.parametrize('kind', ['behavior', 'observation'])
def test_camera_correction_waits_for_submission_without_blocking_new_events(tmp_path, monkeypatch, kind):
    db = Database(tmp_path / 'guard.db')
    db.initialize()
    repo = StudyRepository(db)
    repo.seed_family()
    child = 'child-primary-4'
    session = repo.start_session(child, 'synthetic-start')['session_id']
    at = datetime.now(timezone.utc).isoformat()
    with db.connect() as c:
        c.execute("""INSERT INTO child_state_event(state_event_id,child_id,session_id,state,confidence,reason,duration_ms,model_version,shadow,observed_at)
                     VALUES ('sample',?,?, 'paused_like', .9, 'synthetic', 0, 'test', 0, ?)""", (child, session, at))
        c.commit()
    behavior = BehaviorModelService(db)
    behavior.initialize()
    behavior.ingest(ObservableEvent(event_id='observation:sample', child_id=child, session_id=session,
                                    occurred_at=at, event_type='work.stall', source='local_vision', confidence=.9))
    observation = ObservationEngine(db)
    monkeypatch.setattr(observation, '_active_session', lambda *_args, **_kwargs: {'child_id': child})
    with db.connect() as c:
        before = _hash(c, child, 'camera_event', 'sample')
    started = Event()

    def correction():
        started.set()
        if kind == 'behavior':
            return behavior.retract_event('observation:sample', child, 'synthetic-correction', 'Synthetic correction')
        return observation.correct(session, 'Synthetic correction', 'synthetic-correction')

    with ThreadPoolExecutor(max_workers=2) as pool:
        with db.external_effect_guard():
            future = pool.submit(correction)
            assert started.wait(2)
            with pytest.raises(TimeoutError):
                future.result(timeout=.3)
            # A normal new observation is independent of withdrawing a source.
            ordinary = pool.submit(behavior.ingest, ObservableEvent(event_id='ordinary-event', child_id=child,
                session_id=session, occurred_at=at, event_type='child.check_in', source='child_self_report', confidence=1))
            ordinary.result(timeout=3)
            with db.connect() as c:
                assert _hash(c, child, 'camera_event', 'sample') == before
        future.result(timeout=3)
    with db.connect() as c, pytest.raises(ValueError, match='FROZEN_SOURCE_WITHDRAWN'):
        _hash(c, child, 'camera_event', 'sample')


def test_blocked_withdrawal_times_out_without_mutation_and_same_action_retries(tmp_path):
    from home_study_agent.database import ExternalEffectBusyError

    db = Database(tmp_path / 'timeout.db')
    db.initialize()
    repo = StudyRepository(db)
    repo.seed_family()
    child = 'child-primary-4'
    session = repo.start_session(child, 'timeout-start')['session_id']
    behavior = BehaviorModelService(db)
    behavior.initialize()
    at = datetime.now(timezone.utc).isoformat()
    behavior.ingest(ObservableEvent(event_id='observation:timeout', child_id=child, session_id=session,
                                    occurred_at=at, event_type='work.stall', source='local_vision', confidence=.9))
    with ThreadPoolExecutor(max_workers=1) as pool, db.external_effect_guard():
        future = pool.submit(behavior.retract_event, 'observation:timeout', child, 'timeout-action', 'Synthetic correction')
        with pytest.raises(ExternalEffectBusyError):
            future.result(timeout=4)
        with db.connect() as c:
            assert not c.execute("SELECT 1 FROM behavior_retraction WHERE event_id='observation:timeout'").fetchone()
    result = behavior.retract_event('observation:timeout', child, 'timeout-action', 'Synthetic correction')
    assert result['status'] == 'retracted'
    assert behavior.retract_event('observation:timeout', child, 'timeout-action', 'Synthetic correction')['idempotent']


def test_non_restore_busy_http_is_truthful_and_retryable(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    from home_study_agent.database import ExternalEffectBusyError

    settings = Settings(data_dir=tmp_path, database_path=tmp_path / 'app.db',
                        data_encryption_key=b'0' * 32, maintenance_enabled=False)
    with TestClient(create_app(settings)) as client:
        token = client.post('/v1/parent/setup', json={'pin': '7391'}).json()['parent_token']
        def busy(*args, **kwargs):
            raise ExternalEffectBusyError('synthetic contention')
        monkeypatch.setattr(client.app.state.answer_attempt_ledger, 'list_for_child', busy)
        response = client.get('/v1/parent/children/child-primary-4/answer-attempt-ledger',
                              headers={'X-HSC-Parent': token})
        assert response.status_code == 409
        assert response.json()['retryable'] is True
        assert '恢复' not in response.json()['message']
        assert '未确认完成' in response.json()['message']
