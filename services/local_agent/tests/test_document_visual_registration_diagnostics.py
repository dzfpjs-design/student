"""Extraction reasons expose only bounded numerical diagnostics, never media/text."""
import base64
from copy import deepcopy
from hashlib import sha256
import json
from uuid import uuid4

import cv2
import numpy as np
import pytest

from home_study_agent.document_visual_registration import EXTRACTION_REASONS, METHOD, extract_landmarks
from test_document_visual_registration import PAGE, math_paper


SAFE_KEYS = {'method', 'status', 'reason', 'line_count', 'high_confidence_lines', 'usable_lines',
             'text_span', 'text_quadrants', 'expected_frame_size', 'decoded_frame_size', 'brightness', 'contrast',
             'raw_keypoints', 'bounded_keypoints', 'landmark_span', 'landmark_quadrants', 'error_code', 'opencv_code'}


def assert_safe(diagnostic):
    assert set(diagnostic) <= SAFE_KEYS
    assert diagnostic['reason'] in EXTRACTION_REASONS
    assert diagnostic['method'] == METHOD
    # A strict numerical/string-code tree; no arrays, polygons or opaque objects.
    def scalar_or_range(value):
        if isinstance(value, list):
            assert len(value) == 2 and all(type(item) in (int, float) for item in value)
        else:
            assert value is None or type(value) in (str, int, float)
    for value in diagnostic.values():
        scalar_or_range(value)
    serialized = json.dumps(diagnostic, allow_nan=False)
    assert 'PRIVATE' not in serialized and 'descriptors' not in serialized and 'image_base64' not in serialized


@pytest.mark.parametrize('reason', ['geometry', 'sha', 'no_text', 'text_spread', 'shape', 'dark',
                                  'keypoints', 'landmark_spread', 'opencv_error', 'ready'])
def test_closed_extraction_reason_and_safe_counts(reason, monkeypatch):
    raw, frame, _ = math_paper()
    frame = deepcopy(frame)
    if reason == 'geometry':
        frame = None
    elif reason == 'sha':
        frame['frame_sha256'] = '0' * 64
    elif reason == 'no_text':
        frame['lines'] = frame['lines'][:4]
    elif reason == 'text_spread':
        for line in frame['lines']:
            line['original_polygon'] = [[.1, .1], [.12, .1], [.12, .12], [.1, .12]]
    elif reason == 'shape':
        frame['frame_size'][0] -= 1
    elif reason == 'dark':
        raw = cv2.imencode('.png', np.zeros((1800, 1600, 3), np.uint8))[1].tobytes()
        frame['frame_sha256'] = sha256(raw).hexdigest()
    elif reason == 'keypoints':
        class NoPoints:
            def detectAndCompute(self, *_):
                return (), None
        monkeypatch.setattr(cv2, 'ORB_create', lambda **_: NoPoints())
    elif reason == 'landmark_spread':
        class ClusteredPoints:
            def detectAndCompute(self, *_):
                return ([cv2.KeyPoint(800 + n % 10, 900 + n // 10, 31) for n in range(100)],
                        np.zeros((100, 32), np.uint8))
        monkeypatch.setattr(cv2, 'ORB_create', lambda **_: ClusteredPoints())
    elif reason == 'opencv_error':
        def broken(**_):
            raise cv2.error('PRIVATE child record / secret file / raw scanner message')
        monkeypatch.setattr(cv2, 'ORB_create', broken)
    diagnostic = {'PRIVATE': 'must be discarded'}
    value = extract_landmarks(raw, frame, diagnostics=diagnostic)
    assert diagnostic['reason'] == reason
    assert (value is not None) == (reason == 'ready')
    assert diagnostic['status'] == ('ready' if reason == 'ready' else 'unavailable')
    assert_safe(diagnostic)


def test_unavailable_registration_is_durable_in_http_without_changing_success_schema(tmp_path, monkeypatch):
    from home_study_agent.local_ocr.service import OCR_PIPELINE_VERSION
    from test_app import client_for
    from test_document_ocr_preview import start_homework

    raw, _, result = math_paper()
    with client_for(tmp_path) as client:
        root, db, generation = start_homework(client)
        route = root + '/previews'
        monkeypatch.setattr(client.app.state.local_ocr, '_process', lambda *_a, **_k:
                            {**result, 'question_blocks': [], 'pipeline_version': OCR_PIPELINE_VERSION})
        original = cv2.ORB_create
        def broken(**_):
            raise cv2.error('PRIVATE text must never enter registration diagnostics')
        monkeypatch.setattr(cv2, 'ORB_create', broken)
        body = {'image_base64': base64.b64encode(raw).decode(), 'frame_signature': sha256(raw).hexdigest(),
                'client_action_id': str(uuid4()), 'expected_generation': generation, 'expected_lease_id': 'preview-lease',
                'expected_phase': 'homework_capture', 'page_token': PAGE}
        response = client.post(route, json=body)
        assert response.status_code == 200, response.text
        failed = response.json()
        assert failed['page_draft']['reason'] == 'insufficient_anchors'
        diagnostic = failed['page_draft']['registration']
        assert diagnostic['reason'] == 'opencv_error'
        assert diagnostic['usable_lines'] >= 6 and diagnostic['text_quadrants'] >= 3
        assert_safe(diagnostic)
        with db.connect() as c:
            stored = json.loads(c.execute('SELECT page_draft_json FROM document_ocr_preview').fetchone()[0])
            assert stored['registration'] == diagnostic
            assert c.execute('SELECT COUNT(*) FROM capture_artifact').fetchone()[0] == 0
            assert c.execute('SELECT COUNT(*) FROM document_page_revision').fetchone()[0] == 0
            assert c.execute('SELECT COUNT(*) FROM learning_evidence').fetchone()[0] == 0
        monkeypatch.setattr(cv2, 'ORB_create', original)
        succeeded = client.post(route, json={**body, 'client_action_id': str(uuid4())})
        assert succeeded.status_code == 200, succeeded.text
        draft = succeeded.json()['page_draft']
        assert draft['reason'] == 'anchor_started' and draft['alignment_method'] == METHOD
        assert 'registration' not in draft
