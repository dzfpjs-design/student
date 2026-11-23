"""Physical glyph coverage, spatial holdout and candidate-anchor boundaries."""
from hashlib import sha256

import cv2
import numpy as np

from home_study_agent import document_visual_registration as visual
from home_study_agent.document_preview_accumulator import verified_frame
from home_study_agent.local_ocr.text_geometry import build
from test_document_preview_accumulator import cpu_observations


def column_paper(*, move=(0, 0)):
    image = np.full((1800, 1600, 3), 248, np.uint8)
    lines = []
    for index in range(8):
        text = f'{index + 1}. {246 + 73 * index} / 6 = {41 + 12 * index}'
        (width, _), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 1.2, 2)
        tile = np.full((60, width + 8, 3), 248, np.uint8)
        cv2.putText(tile, text, (2, 44), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (10, 10, 10), 2, cv2.LINE_AA)
        tile = cv2.resize(tile, (400, 48))
        x, y = 520, 150 + index * 190
        image[y:y + 48, x:x + 400] = tile
        polygon = np.array([[x, y], [x + 400, y], [x + 400, y + 48], [x, y + 48]]) + move
        lines.append({'text': text, 'confidence': .98, 'box': polygon.tolist(), 'kind': 'printed'})
    image = cv2.warpAffine(image, np.array([[1., 0., move[0]], [0., 1., move[1]]]),
                           (1600, 1800), borderValue=(248, 248, 248))
    raw = cv2.imencode('.png', image)[1].tobytes()
    geometry = build(lines, frame_sha256=sha256(raw).hexdigest(), frame_size=[1600, 1800],
                     prepared_size=[1600, 1800], frame_to_prepared=np.eye(3).tolist(), degrees=0)
    frame = verified_frame({'engine': 'rapidocr-onnx', 'lines': lines, 'text_geometry': geometry}, sha256(raw).hexdigest())
    return raw, frame


def test_absolute_area_and_shape_allow_narrow_two_dimensional_support():
    points = np.array([(x, y) for x in np.linspace(.1, .3, 9) for y in np.linspace(.1, .65, 12)])
    scale = np.array([.8, .6])
    assert visual._registration_support(points * scale, scale)


def test_single_column_real_glyphs_register_even_when_centres_are_collinear():
    raw_a, a = column_paper()
    raw_b, b = column_paper(move=(3, -2))
    centers = np.array([np.mean(line['original_polygon'], axis=0) for line in a['lines']])
    assert np.ptp(centers[:, 0]) < 1e-8
    first, second = visual.extract_landmarks(raw_a, a), visual.extract_landmarks(raw_b, b)
    assert first is not None and second is not None
    matrix, conflict, evidence = visual.register(a, b, first, second)
    assert matrix is not None and not conflict
    assert evidence['holdout_groups'] >= 3 and evidence['holdout_min_training'] >= 24


def test_area_alone_does_not_accept_a_wide_thin_strip():
    points = np.array([(x, y) for x in np.linspace(.05, .95, 30) for y in np.linspace(.1, .12, 5)])
    scale = np.array([.8, .6])
    metrics = {}
    assert not visual._registration_support(points * scale, scale, metrics=metrics)
    assert metrics['hull_frame_area'] > 1 / 64 and metrics['hull_bbox_ratio'] > .99
    assert metrics['width_diameter_ratio'] < .1


def test_spatial_holdout_checks_every_occupied_group_and_rejects_inadequate_budget():
    points = np.array([(x, y) for x in np.linspace(.1, .7, 12) for y in np.linspace(.1, .7, 12)])
    target = points + (.002, -.001)
    accepted, evidence = visual._spatial_holdout(points, target)
    assert accepted and evidence['holdout_groups'] == 4
    assert evidence['holdout_min_training'] >= 24
    assert evidence['holdout_max'] < 1e-8
    sparse = np.vstack([np.repeat([[.1, .1]], 8, axis=0), np.repeat([[.7, .1]], 8, axis=0),
                        np.repeat([[.1, .7]], 8, axis=0)])
    accepted, evidence = visual._spatial_holdout(sparse, sparse)
    assert not accepted and evidence['status'] == 'holdout_budget_insufficient'


def test_local_distortion_with_small_global_residual_fails_whole_group_holdout():
    points = np.array([(x, y) for x in np.linspace(.1, .7, 16) for y in np.linspace(.1, .7, 16)])
    target = points.copy()
    corner = (points[:, 0] > .4) & (points[:, 1] > .4)
    target[corner, 0] += .004
    fitted = visual._similarity_least_squares(points, target)
    ordinary = np.linalg.norm(points @ fitted[:, :2].T + fitted[:, 2] - target, axis=1)
    assert np.quantile(ordinary, .9) <= .003 and ordinary.max() <= .005
    # No discarded heldout points or best-fold selection may hide this corner.
    accepted, evidence = visual._spatial_holdout(points, target)
    assert not accepted and evidence['status'] == 'holdout_unstable'


def test_six_tall_words_on_one_header_baseline_are_not_independent_body_evidence():
    words = ['fractions', 'multiply', 'decimal', 'integer', 'division', 'remainder']
    for mixed_fonts in (False, True):
        lines = []
        for index, word in enumerate(words):
            x = .07 + index * .145
            height = (.4 if index == 0 else .08) if mixed_fonts else .25
            lines.append({'text': word, 'confidence': .99,
                          'original_polygon': [[x, .65 - height], [x + .115, .65 - height], [x + .115, .65], [x, .65]]})
        frame = {'frame_size': [1599, 1799], 'lines': lines}
        consistent, _ = visual._text_evidence(frame, frame, np.array([[1, 0, 0], [0, 1, 0]]))
        assert not consistent


def test_api_downstream_reference_rejection_starts_current_candidate_and_preserves_old_draft(tmp_path, monkeypatch):
    import base64
    from uuid import uuid4
    from home_study_agent import document_ocr_preview as previews
    from home_study_agent.local_ocr.service import OCR_PIPELINE_VERSION
    from test_app import client_for
    from test_document_ocr_preview import start_homework
    from test_document_visual_registration import PAGE, math_paper

    with client_for(tmp_path) as client:
        root, db, generation = start_homework(client)
        route, observations = root + '/previews', {}
        monkeypatch.setattr(client.app.state.local_ocr, '_process', lambda raw, *_a, **_k: observations[sha256(raw).hexdigest()])
        def upload(hidden):
            raw, _, result = math_paper(hidden=hidden)
            observations[sha256(raw).hexdigest()] = {**result, 'question_blocks': [], 'pipeline_version': OCR_PIPELINE_VERSION}
            response = client.post(route, json={'image_base64': base64.b64encode(raw).decode(),
                'frame_signature': sha256(raw).hexdigest(), 'client_action_id': str(uuid4()), 'expected_generation': generation,
                'expected_lease_id': 'preview-lease', 'expected_phase': 'homework_capture', 'page_token': PAGE})
            assert response.status_code == 200, response.text
            return response.json()
        first = upload((7,))
        original = previews.build_page_draft
        def reference_rejects(*args, **kwargs):
            draft = original(*args, **kwargs)
            if kwargs.get('anchor') is not None and draft['merged']:
                # Inject only the downstream reference-bounds rejection. All
                # preceding image/OCR/registration gates ran on real pixels.
                return {**draft, 'merged': False, 'reason': 'geometry_outside_anchor'}
            return draft
        monkeypatch.setattr(previews, 'build_page_draft', reference_rejects)
        second = upload((6,))
        draft = second['page_draft']
        assert draft['reason'] == 'anchor_started' and not draft['merged'] and draft['frame_count'] == 1
        assert draft['anchor_preview_id'] == second['preview_id']
        assert draft['registration']['status'] == 'unconfirmed_alignment'
        assert draft['registration']['candidate_reason'] == 'geometry_outside_anchor'
        assert {line['preview_id'] for line in draft['lines']} == {second['preview_id']}
        assert first in client.get(route).json()['items']
        with db.connect() as connection:
            assert connection.execute('SELECT COUNT(*) FROM document_ocr_preview').fetchone()[0] == 2
            assert connection.execute('SELECT COUNT(*) FROM capture_artifact').fetchone()[0] == 0


def test_live_api_never_creates_a_text_only_anchor_without_qualified_landmarks(tmp_path, monkeypatch, cpu_observations):
    from home_study_agent import document_ocr_preview as previews
    from test_app import client_for
    from test_document_ocr_preview import start_homework
    from test_document_preview_accumulator import ANCHORS, upload
    with client_for(tmp_path) as client:
        root, db, generation = start_homework(client)
        monkeypatch.setattr(previews, 'extract_landmarks', lambda *_a, **_k: None)
        response, _ = upload(client, root + '/previews', generation, cpu_observations, ANCHORS, 98)
        assert response.status_code == 200, response.text
        draft = response.json()['page_draft']
        assert not draft['merged'] and draft['reason'] == 'insufficient_anchors'
        assert draft['anchor_preview_id'] is None
        with db.connect() as connection:
            assert connection.execute('SELECT COUNT(*) FROM document_ocr_preview').fetchone()[0] == 1
