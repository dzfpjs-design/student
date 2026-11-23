"""Registration depends on spatial support, not where the frame centre falls."""
from hashlib import sha256

import cv2
import numpy as np
import pytest

from home_study_agent.document_preview_accumulator import verified_frame
from home_study_agent import document_visual_registration as visual
from home_study_agent.local_ocr.text_geometry import build
from test_document_visual_registration import math_paper


def side_paper(*, offset=0, move=(0, 0), changed=None, index=0):
    source, _, result = math_paper(changed=changed)
    image = cv2.imdecode(np.frombuffer(source, np.uint8), cv2.IMREAD_COLOR)
    canvas = np.full((1800, 2400, 3), 248, np.uint8)
    canvas[:, offset:offset + 1400] = image[:, :1400]
    matrix = np.array([[1., 0., move[0]], [0., 1., move[1]]])
    canvas = cv2.warpAffine(canvas, matrix, (2400, 1800), borderValue=(248, 248, 248))
    canvas[0, 0] = index
    lines = [{**line, 'box': (np.asarray(line['box']) + (offset + move[0], move[1])).tolist()}
             for line in result['lines']]
    raw = cv2.imencode('.png', canvas)[1].tobytes()
    geometry = build(lines, frame_sha256=sha256(raw).hexdigest(), frame_size=[2400, 1800],
                     prepared_size=[2400, 1800], frame_to_prepared=np.eye(3).tolist(), degrees=0)
    frame = verified_frame({'engine': 'rapidocr-onnx', 'lines': lines, 'text_geometry': geometry}, sha256(raw).hexdigest())
    return raw, frame


def test_same_math_content_at_left_edge_registers_with_two_global_quadrants():
    first, a = side_paper(index=1)
    second, b = side_paper(move=(5, -3), index=2)
    assert all(np.max(line['original_polygon'], axis=0)[0] < .5 for line in b['lines'])
    before, after = visual.extract_landmarks(first, a), visual.extract_landmarks(second, b)
    assert before is not None and after is not None
    matrix, conflict, _ = visual.register(a, b, before, after)
    assert matrix is not None and not conflict


def test_support_is_translation_invariant_without_normalizing_tiny_patches_into_a_page():
    base = np.array([(x, y) for x in np.linspace(.05, .4, 8) for y in np.linspace(.1, .8, 12)])
    results = []
    for shift in ((0, 0), (.2, .04), (.55, .03)):
        metrics = {}
        assert visual._registration_support(base + shift, np.ones(2), metrics=metrics)
        results.append(metrics)
    for metric in results:
        assert metric['support_cells'] == 4
        assert metric['hull_bbox_ratio'] == pytest.approx(1)
        assert metric['hull_frame_area'] == pytest.approx(.245)
    tiny = base * .1
    assert not visual._registration_support(tiny, np.ones(2))


@pytest.mark.parametrize('kind', ['line', 'thin_diagonal', 'tiny_letter', 'two_clusters'])
def test_registration_rejects_degenerate_or_low_area_support(kind):
    rng = np.random.default_rng(33)
    t = np.linspace(.1, .8, 100)
    if kind == 'line':
        points = np.c_[t, t]
    elif kind == 'thin_diagonal':
        points = np.c_[t, t + rng.uniform(-.006, .006, len(t))]
    elif kind == 'tiny_letter':
        points = rng.uniform((.1, .1), (.16, .18), (100, 2))
    else:
        points = np.vstack([rng.uniform((.1, .1), (.16, .16), (100, 2)),
                            rng.uniform((.7, .7), (.76, .76), (100, 2))])
    assert not visual._registration_support(points, np.ones(2))


def test_single_observed_character_and_different_paper_remain_inadmissible():
    raw, frame = side_paper(index=1)
    one = {**frame, 'lines': [{**frame['lines'][0], 'text': '8'}]}
    assert visual.extract_landmarks(raw, one) is None
    changed = {(q, i): str(800 + q * 13 + i * 7) for q in range(8) for i in (1, 3, 4, 5, 6)}
    other, new_frame = side_paper(changed=changed, index=2)
    matrix, _, _ = visual.register(frame, new_frame, visual.extract_landmarks(raw, frame), visual.extract_landmarks(other, new_frame))
    assert matrix is None


def test_zero_independent_text_still_rejects_even_with_valid_geometry_support():
    raw_a, a = side_paper(index=1)
    raw_b, b = side_paper(index=2)
    # Same visible ink can have an unusable OCR reading. Strong image geometry
    # alone must not waive the independent-text requirement.
    lines = [{'text': f'Unrelated independent text {n}', 'confidence': line['confidence'],
              'box': (np.asarray(line['original_polygon']) * b['frame_size']).tolist()}
             for n, line in enumerate(b['lines'])]
    geometry = build(lines, frame_sha256=sha256(raw_b).hexdigest(), frame_size=[2400, 1800],
                     prepared_size=[2400, 1800], frame_to_prepared=np.eye(3).tolist(), degrees=0)
    b = verified_frame({'engine': 'rapidocr-onnx', 'lines': lines, 'text_geometry': geometry}, sha256(raw_b).hexdigest())
    assert b is not None
    matrix, _, _ = visual.register(a, b, visual.extract_landmarks(raw_a, a), visual.extract_landmarks(raw_b, b))
    assert matrix is None
