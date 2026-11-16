import base64
from datetime import timedelta
import json

import cv2
import numpy as np
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from test_behavior_observation_coverage import managed, ZERO, at


def test_real_frame_http_records_only_engine_qualified_coverage(managed, tmp_path, monkeypatch):
    database, _, _, _ = managed
    settings = Settings(data_dir=tmp_path, database_path=database.path, maintenance_enabled=False)
    application = create_app(settings)
    frame = base64.b64encode(cv2.imencode('.png', np.tile(np.array([80, 180], dtype=np.uint8), (240, 160)))[1]).decode()
    monkeypatch.setattr('home_study_agent.behavior_model.app_integration._now', lambda: at(0))
    with TestClient(application) as client:
        engine = application.state.observation_engine
        def classify(gray, brightness, sharpness, motion, tracker, now):
            tracker.face_seen_at = now
            return 'present', .9, 'synthetic test classification'
        monkeypatch.setattr(engine, '_classify', classify)
        opened = client.post('/v1/sessions/session/observation/stream/start', json={
            'stream_epoch': 'test-epoch-123', 'client_action_id': 'test-stream-open',
        })
        assert opened.status_code == 200, opened.text
        for index in (1, 2):
            engine._now = lambda: ZERO + timedelta(seconds=(index - 1) * 2)
            result = client.post('/v1/sessions/session/observation-frame', json={
                'frame_base64': frame,
                'capture_sample': {'stream_epoch': 'test-epoch-123', 'sequence': index,
                                   'captured_at': at((index - 1) * 2), 'capture_monotonic_ms': (index - 1) * 2000},
            })
            assert result.status_code == 200, result.text
            assert result.json()['coverage_record']['status'] == 'recorded'
        measured = application.state.behavior_model.observation_coverage('child-primary-4', start_at=at(0), end_at=at(3))
        assert measured['observed_seconds'] == 2
        assert measured['task_opportunities'] == 1
        bad = client.post('/v1/sessions/session/observation-frame', json={'frame_base64': frame, 'capture_sample': {
            'stream_epoch': 'test-epoch-123', 'sequence': 3, 'captured_at': at(4), 'capture_monotonic_ms': 4000,
            'quality': {'observation_ready': True},
        }})
        assert bad.status_code == 422
        monkeypatch.setattr('home_study_agent.behavior_model.app_integration._now', lambda: at(3))
        stopped = client.post('/v1/sessions/session/observation/stream/stop', json={
            'stream_epoch': 'test-epoch-123', 'sequence': 2, 'reason': 'paused', 'client_action_id': 'test-stream-stop',
        })
        assert stopped.status_code == 200
        assert client.post('/v1/sessions/other/observation/stream/start', json={
            'stream_epoch': 'test-epoch-other', 'client_action_id': 'test-other',
        }).status_code == 404
    with database.connect() as c:
        rows = c.execute("SELECT payload_json FROM observable_event WHERE event_type LIKE 'observation.%'").fetchall()
        assert rows
        assert all(frame not in row[0] and 'frame_base64' not in row[0] for row in rows)
    with TestClient(create_app(settings)) as client:
        measured = client.app.state.behavior_model.observation_coverage('child-primary-4', start_at=at(0), end_at=at(3))
        assert measured['observed_seconds'] == 2


def test_missing_clock_is_explicit_and_never_creates_coverage(managed, tmp_path, monkeypatch):
    database, _, _, _ = managed
    frame = base64.b64encode(cv2.imencode('.png', np.full((240, 320), 128, dtype=np.uint8))[1]).decode()
    with TestClient(create_app(Settings(data_dir=tmp_path, database_path=database.path, maintenance_enabled=False))) as client:
        result = client.post('/v1/sessions/session/observation-frame', json={'frame_base64': frame})
        assert result.status_code == 200
        assert result.json()['coverage_record'] == {'status': 'unavailable', 'reason': 'CAPTURE_CLOCK_MISSING'}
        with database.connect() as c:
            assert c.execute("SELECT count(*) FROM observable_event WHERE event_type='observation.sample'").fetchone()[0] == 0
