"""Explicit public synthetic HTTP stream; no guard or client API is patched."""
import base64
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import cv2
import numpy as np


def synthetic_frame():
    pixels = np.tile(np.array([80, 180], dtype=np.uint8), (240, 160))
    return base64.b64encode(cv2.imencode('.png', pixels)[1]).decode('ascii')


class BoundObservationFrames:
    def __init__(self, client, session_id):
        self.client = client
        self.session_id = session_id
        self.epoch = 'public-fixture-' + str(uuid4())
        self.sequence = 0
        started = client.post(f'/v1/sessions/{session_id}/observation/stream/start', json={
            'stream_epoch': self.epoch, 'client_action_id': 'start-' + self.epoch})
        assert started.status_code == 200, started.text

    def post(self, *, json):
        self.sequence += 1
        return self.client.post(f'/v1/sessions/{self.session_id}/observation-frame', json={
            **json, 'capture_sample': {'stream_epoch': self.epoch, 'sequence': self.sequence,
                'captured_at': datetime.now(timezone.utc).isoformat(),
                'capture_monotonic_ms': self.sequence * 1000}})


def classify_public_frame(engine, state, *, age_seconds=0):
    """Only deterministic CV classification; original analysis/SQL/policy remain real."""
    def classify(gray, brightness, sharpness, motion, tracker, now):
        tracker.face_seen_at = now
        tracker.state = state
        tracker.state_started_at = now - timedelta(seconds=age_seconds)
        return state, .95, 'public synthetic local classification'
    engine._classify = classify
