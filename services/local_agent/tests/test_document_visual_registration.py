"""Real ORB over generated short arithmetic ink, never mocked image landmarks."""
from hashlib import sha256
import json

import cv2
import numpy as np
import pytest

from home_study_agent.document_preview_accumulator import build_page_draft, qualifies_as_anchor, verified_frame
from home_study_agent.document_visual_registration import LandmarkCache, METHOD, _text_evidence, extract_landmarks, register
from home_study_agent.local_ocr.text_geometry import build


PAGE = '26606909-049d-4478-9704-51323a88ae23'


def math_paper(*, hidden=(), changed=None, move=None, background=False):
    """Eight division exercises, 64 short blocks, with exact source ink boxes."""
    height, width = 1800, 1600
    image = np.full((height, width, 3), 248, np.uint8)
    if background:
        rng = np.random.default_rng(17)
        image[:100] = rng.integers(0, 255, image[:100].shape, dtype=np.uint8)
    changed = changed or {}
    lines = []
    for question in range(8):
        if question in hidden:
            continue
        x, y = (230 + question % 2 * 710), (180 + question // 2 * 390)
        numerator = 246 + question * 79
        values = [f'{question + 1}.', str(numerator), '/6', str(numerator // 6),
                  str(24 + question * 3), str(12 + question * 7), '-18', '00']
        for index, value in enumerate(values):
            value = changed.get((question, index), value)
            left, baseline = x + index % 2 * 135, y + index // 2 * 53
            (text_width, text_height), bottom = cv2.getTextSize(value, cv2.FONT_HERSHEY_SIMPLEX, 1.1, 2)
            cv2.putText(image, value, (left, baseline), cv2.FONT_HERSHEY_SIMPLEX, 1.1, (10, 10, 10), 2, cv2.LINE_AA)
            box = [[left - 2, baseline - text_height - 2], [left + text_width + 2, baseline - text_height - 2],
                   [left + text_width + 2, baseline + bottom + 2], [left - 2, baseline + bottom + 2]]
            lines.append({'text': value, 'confidence': .98, 'box': box, 'kind': 'printed'})
    if move is not None:
        image = cv2.warpAffine(image, move, (width, height), borderValue=(248, 248, 248))
        for line in lines:
            points = np.asarray(line['box']) @ move[:, :2].T + move[:, 2]
            line['box'] = points.tolist()
    raw = cv2.imencode('.png', image)[1].tobytes()
    geometry = build(lines, frame_sha256=sha256(raw).hexdigest(), frame_size=[width, height],
                     prepared_size=[width, height], frame_to_prepared=np.eye(3).tolist(), degrees=0)
    result = {'engine': 'rapidocr-onnx', 'lines': lines, 'text_geometry': geometry}
    return raw, verified_frame(result, sha256(raw).hexdigest()), result


def test_short_math_complementary_ink_registers_to_fixed_original_and_keeps_sources():
    raw_a, frame_a, _ = math_paper(hidden=(7,))
    move = cv2.getRotationMatrix2D((800, 900), 1.5, 1)
    move[:, 2] += (7, -5)
    raw_b, frame_b, _ = math_paper(hidden=(6,), move=move, changed={(2, 3): '99'})
    assert not qualifies_as_anchor(frame_a) and not qualifies_as_anchor(frame_b)
    features_a, features_b = extract_landmarks(raw_a, frame_a), extract_landmarks(raw_b, frame_b)
    assert features_a is not None and features_b is not None
    matrix, conflict, evidence = register(frame_a, frame_b, features_a, features_b)
    assert matrix is not None and not conflict
    assert evidence['inliers'] >= 24
    one = build_page_draft(PAGE, 'first', frame_a, visual_anchor=True)
    two = build_page_draft(PAGE, 'second', frame_b, anchor_id='first', anchor=frame_a,
                           previous=one, visual_matrix=matrix, registration=evidence)
    assert two['merged'] and two['frame_count'] == 2 and two['anchor_preview_id'] == 'first'
    assert two['alignment_method'] == METHOD
    assert any(line['preview_id'] == 'first' and line['status'] == 'historical' for line in two['lines'])
    assert any(line['preview_id'] == 'second' and line['status'] == 'current' for line in two['lines'])
    assert {line['frame_sha256'] for line in two['lines']} == {sha256(raw_a).hexdigest(), sha256(raw_b).hexdigest()}
    assert any(line['text'] == '99' and line['conflict'] for line in two['lines'])
    # Pixel coordinates and a source digest survive, no imagined full-frame image.
    current = next(line for line in two['lines'] if line['preview_id'] == 'second' and line['text'] == '325')
    original = next(line for line in one['lines'] if line['text'] == '325')
    np.testing.assert_allclose(current['canonical_polygon'], original['canonical_polygon'], atol=.004)
    assert current['original_polygon'] != current['canonical_polygon']


def test_shared_template_with_different_problem_content_has_no_independent_identity():
    raw_a, frame_a, _ = math_paper(background=True)
    # Keep only the printed question labels/division/zero scaffolding. Change
    # all distinctive operands and results, unlike corrections on one page.
    changed = {(q, i): str(800 + q * 13 + i * 7) for q in range(8) for i in (1, 3, 4, 5, 6)}
    raw_b, frame_b, _ = math_paper(changed=changed, background=True)
    matrix, conflict, _ = register(frame_a, frame_b, extract_landmarks(raw_a, frame_a), extract_landmarks(raw_b, frame_b))
    assert matrix is None
    # This substantially changed ink may fail registration before textual
    # comparison. Even if geometry were identical, it has no independent text
    # agreement; neither route may merge the different problem content.
    consistent, broad = _text_evidence(frame_a, frame_b, np.array([[1, 0, 0], [0, 1, 0]]))
    assert not consistent and broad
    old = build_page_draft(PAGE, 'old', frame_a, visual_anchor=True)
    new = build_page_draft(PAGE, 'new', frame_b, anchor_id='old', anchor=frame_a, previous=old,
                           visual_conflict=conflict)
    assert not new['merged'] and new['frame_count'] == 1
    assert {line['preview_id'] for line in new['lines']} == {'new'}


def test_different_page_or_insufficient_ink_never_registers_stationary_background():
    raw_a, frame_a, _ = math_paper(background=True)
    changed = {(q, i): f'{(q * 97 + i * 43 + 901) % 997:03d}' for q in range(8) for i in range(1, 8)}
    raw_b, frame_b, _ = math_paper(changed=changed, background=True)
    matrix, _, _ = register(frame_a, frame_b, extract_landmarks(raw_a, frame_a), extract_landmarks(raw_b, frame_b))
    assert matrix is None
    for level in (0, 248):
        raw = cv2.imencode('.png', np.full((1800, 1600, 3), level, np.uint8))[1].tobytes()
        fake_frame = {**frame_a, 'frame_sha256': sha256(raw).hexdigest()}
        assert extract_landmarks(raw, fake_frame) is None
    raw_small, frame_small, _ = math_paper(hidden=tuple(range(1, 8)))
    assert extract_landmarks(raw_small, frame_small) is None
    assert extract_landmarks(raw_a, frame_b) is None


@pytest.mark.parametrize('angle,scale,shift', [(8, 1, (0, 0)), (0, 1.2, (0, 0)), (0, 1, (220, 0))])
def test_geometric_drift_rejected_against_original_not_latest(angle, scale, shift):
    raw_a, frame_a, _ = math_paper()
    move = cv2.getRotationMatrix2D((800, 900), angle, scale)
    move[:, 2] += shift
    # All OCR polygons must still fall in the original source frame.
    if scale > 1:
        raw_b, frame_b, _ = math_paper(hidden=(0, 1, 6, 7), move=move)
    else:
        raw_b, frame_b, _ = math_paper(move=move)
    matrix, _, _ = register(frame_a, frame_b, extract_landmarks(raw_a, frame_a), extract_landmarks(raw_b, frame_b))
    assert matrix is None


def test_cache_expires_without_read_refresh_and_isolates_child_source_page():
    raw, frame, _ = math_paper()
    features = extract_landmarks(raw, frame)
    now = [0]
    cache = LandmarkCache(clock=lambda: now[0])
    scope = ('session', 'child', 'phase', 'lease', 1, PAGE)
    cache.put(scope, 'anchor', features)
    now[0] = 299
    assert cache.get(scope, 'anchor', frame['frame_sha256']) is features
    now[0] = 300
    assert cache.get(scope, 'anchor', frame['frame_sha256']) is None
    for part in range(len(scope)):
        cache.put(scope, 'anchor', features)
        different = tuple('changed' if n == part else value for n, value in enumerate(scope))
        cache.activate(different)
        assert cache.get(scope, 'anchor', frame['frame_sha256']) is None
    assert len(cache._items) == 0


def test_validated_activity_renews_idle_time_beyond_five_minutes_without_replacing_anchor():
    raw, frame, _ = math_paper()
    features = extract_landmarks(raw, frame)
    descriptor_bytes = features.descriptors.tobytes()
    now = [0]
    cache = LandmarkCache(clock=lambda: now[0])
    scope = ('session', 'child', 'phase', 'lease', 1, PAGE)
    cache.put(scope, 'anchor', features)
    for second in (240, 480, 720, 960):
        now[0] = second
        assert cache.get(scope, 'anchor', frame['frame_sha256']) is features
        assert cache.touch(scope, 'anchor', frame['frame_sha256'])
        assert cache._items[scope][2] is features
        assert features.descriptors.tobytes() == descriptor_bytes
    now[0] = 1259
    assert not cache.touch(scope, 'unknown-anchor', frame['frame_sha256'])
    assert not cache.touch(scope, 'anchor', '0' * 64)
    assert not cache.touch(('unknown', *scope[1:]), 'anchor', frame['frame_sha256'])
    now[0] = 1260
    assert cache.get(scope, 'anchor', frame['frame_sha256']) is None
    assert not cache.touch(scope, 'anchor', frame['frame_sha256'])


def test_committed_math_registration_keeps_fixed_anchor_for_long_active_page(tmp_path, monkeypatch):
    from uuid import uuid4
    from home_study_agent.document_ocr_preview import DocumentOcrPreviewService
    from home_study_agent.local_ocr.service import OCR_PIPELINE_VERSION
    from test_app import client_for
    from test_document_ocr_preview import start_homework
    with client_for(tmp_path) as client:
        root, db, generation = start_homework(client)
        service = DocumentOcrPreviewService(db, client.app.state.ocr_workflow)
        now = [0]
        service._visual_landmarks._clock = lambda: now[0]
        received = {}
        monkeypatch.setattr(client.app.state.local_ocr, '_process', lambda image, *_a, **_k: received[sha256(image).hexdigest()])
        anchor_id, fixed = None, None
        for index, at in enumerate((0, 240, 480, 720)):
            now[0] = at
            raw, _, result = math_paper(hidden=(7 if index % 2 == 0 else 6,), changed={(2, 3): str(80 + index)})
            received[sha256(raw).hexdigest()] = {**result, 'question_blocks': [], 'pipeline_version': OCR_PIPELINE_VERSION}
            current = service.create(root.split('/')[3], raw, str(uuid4()), generation, 'preview-lease',
                sha256(raw).hexdigest(), expected_phase='homework_capture', page_token=PAGE)
            draft = current['page_draft']
            anchor_id = anchor_id or current['preview_id']
            assert draft['anchor_preview_id'] == anchor_id
            assert draft['frame_count'] == index + 1
            cached = next(iter(service._visual_landmarks._items.values()))
            if fixed is None:
                fixed = cached[2]
            assert cached[2] is fixed and cached[3] == at
        assert draft['merged'] and now[0] > 300
        service._visual_landmarks.clear()


def test_visual_source_limit_preserves_old_draft_and_current_source_remains_separate():
    raw, frame, _ = math_paper()
    prior = build_page_draft(PAGE, 'anchor', frame, visual_anchor=True)
    history = [dict(prior['lines'][0], text=f'older-{n}', preview_id=f'old-{n}') for n in range(31)]
    # Place distinct historical readings away from all current OCR boxes.
    for line in history:
        line['canonical_polygon'] = [[.01, .01], [.02, .01], [.02, .02], [.01, .02]]
    previous = {**prior, 'lines': prior['lines'] + history, 'frame_count': 32}
    limited = build_page_draft(PAGE, 'next', frame, anchor_id='anchor', anchor=frame, previous=previous,
                              visual_matrix=np.array([[1, 0, 0], [0, 1, 0]]))
    assert limited['reason'] == 'source_limit' and limited['frame_count'] == 32
    assert len(limited['lines']) == len(previous['lines'])
    assert all(line['status'] == 'historical' for line in limited['lines'])
    assert not any(line['preview_id'] == 'next' for line in limited['lines'])


def test_http_short_math_saves_sources_reanchors_after_ram_loss_and_keeps_old_draft(tmp_path, monkeypatch):
    import base64
    from uuid import uuid4
    from home_study_agent.document_ocr_preview import DocumentOcrPreviewService
    from home_study_agent.local_ocr.service import OCR_PIPELINE_VERSION
    from test_app import client_for
    from test_document_ocr_preview import start_homework

    with client_for(tmp_path) as client:
        root, db, generation = start_homework(client)
        route = root + '/previews'
        received = {}
        # Detector text is controlled; all source pixels, checked geometry,
        # ORB matching, queue, HTTP and durable transactions remain real.
        monkeypatch.setattr(client.app.state.local_ocr, '_process', lambda image, *_a, **_k: received[sha256(image).hexdigest()])
        def upload(*, hidden=(), token=PAGE, phase='homework_capture', lease='preview-lease', gen=generation):
            raw, _, result = math_paper(hidden=hidden)
            received[sha256(raw).hexdigest()] = {**result, 'question_blocks': [], 'pipeline_version': OCR_PIPELINE_VERSION}
            return client.post(route, json={'image_base64': base64.b64encode(raw).decode(),
                'frame_signature': sha256(raw).hexdigest(), 'client_action_id': str(uuid4()),
                'expected_generation': gen, 'expected_lease_id': lease, 'expected_phase': phase, 'page_token': token})
        first, second = upload(hidden=(7,)), upload(hidden=(6,))
        assert first.status_code == second.status_code == 200, second.text
        one, two = first.json(), second.json()
        assert one['page_draft']['alignment_method'] == METHOD
        assert two['page_draft']['merged'] and two['page_draft']['frame_count'] == 2
        assert two['page_draft']['anchor_preview_id'] == one['preview_id']
        with db.connect() as c:
            persisted = c.execute('SELECT geometry_json,page_draft_json FROM document_ocr_preview').fetchall()
            assert len(persisted) == 2
            for row in persisted:
                value = '\n'.join(row)
                assert 'descriptors' not in value and 'image_base64' not in value and 'Landmarks' not in value
            assert c.execute('SELECT COUNT(*) FROM capture_artifact').fetchone()[0] == 0
            assert c.execute('SELECT COUNT(*) FROM document_page_revision').fetchone()[0] == 0

        changed_page = upload(hidden=(7,), token=str(uuid4()))
        assert changed_page.status_code == 200, changed_page.text
        separate = changed_page.json()
        assert separate['page_draft']['anchor_preview_id'] == separate['preview_id']
        assert separate['page_draft']['frame_count'] == 1 and not separate['page_draft']['merged']
        assert {line['preview_id'] for line in separate['page_draft']['lines']} == {separate['preview_id']}

        # A fresh service has no image-derived memory, exactly as after a
        # backend restart. The old persisted aggregate must remain readable.
        fresh = DocumentOcrPreviewService(db, client.app.state.ocr_workflow)
        assert two in fresh.latest(root.split('/')[3])['items']
        raw, _, result = math_paper(hidden=(7,))
        received[sha256(raw).hexdigest()] = {**result, 'question_blocks': [], 'pipeline_version': OCR_PIPELINE_VERSION}
        new = fresh.create(root.split('/')[3], raw, str(uuid4()), generation, 'preview-lease',
                           sha256(raw).hexdigest(), expected_phase='homework_capture', page_token=PAGE)
        assert new['page_draft']['reason'] == 'anchor_started'
        assert new['page_draft']['anchor_preview_id'] == new['preview_id']
        assert new['page_draft']['frame_count'] == 1 and not new['page_draft']['merged']
        assert two in fresh.latest(root.split('/')[3])['items']
        # New physical source/phase/page can never borrow the previous cache.
        old_scope = (root.split('/')[3], 'child-primary-4', 'homework_capture', 'preview-lease', generation, PAGE)
        assert fresh._visual_landmarks.get(old_scope, new['preview_id'], sha256(raw).hexdigest()) is not None
        with db.connect() as c:
            c.execute("UPDATE daily_session SET state='plan_review'")
            c.commit()
        changed = fresh.create(root.split('/')[3], raw, str(uuid4()), generation, 'preview-lease',
                               sha256(raw).hexdigest(), expected_phase='plan_review', page_token=PAGE)
        assert changed['page_draft']['frame_count'] == 1
        assert fresh._visual_landmarks.get(old_scope, new['preview_id'], sha256(raw).hexdigest()) is None
    with client_for(tmp_path) as restarted:
        response = restarted.get(route)
        assert response.status_code == 200
        assert two in response.json()['items']


def test_scope_changes_during_recognition_cannot_publish_visual_cache(tmp_path, monkeypatch):
    from uuid import uuid4
    from home_study_agent.document_ocr_preview import DocumentOcrPreviewService
    from home_study_agent.local_ocr.service import OCR_PIPELINE_VERSION
    from test_app import client_for
    from test_document_ocr_preview import start_homework

    with client_for(tmp_path) as client:
        root, db, generation = start_homework(client)
        service = DocumentOcrPreviewService(db, client.app.state.ocr_workflow)
        raw, _, result = math_paper()
        def recognize(*_args, **_kwargs):
            with db.connect() as c:
                c.execute("UPDATE document_monitor_session SET generation=generation+1")
                c.commit()
            return {**result, 'question_blocks': [], 'pipeline_version': OCR_PIPELINE_VERSION}
        monkeypatch.setattr(client.app.state.local_ocr, '_process', recognize)
        with pytest.raises((ValueError, RuntimeError)):
            service.create(root.split('/')[3], raw, str(uuid4()), generation, 'preview-lease',
                           sha256(raw).hexdigest(), expected_phase='homework_capture', page_token=PAGE)
        assert len(service._visual_landmarks._items) == 0
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM document_ocr_preview').fetchone()[0] == 0


def test_cache_timer_releases_landmarks_without_any_followup_request():
    import weakref
    from threading import Event
    from time import monotonic
    raw, frame, _ = math_paper()
    landmarks = extract_landmarks(raw, frame)
    reference = weakref.ref(landmarks)
    cache = LandmarkCache(ttl=.03)
    cache.put(('session', 'child', 'phase', 'lease', 1, PAGE), 'anchor', landmarks)
    del landmarks
    deadline = monotonic() + 1
    while reference() is not None and monotonic() < deadline:
        Event().wait(.01)
    assert reference() is None
    assert len(cache._items) == 0 and cache._timer is None


def test_parent_http_erasure_immediately_clears_all_child_landmark_caches(tmp_path, monkeypatch):
    from uuid import uuid4
    from home_study_agent.document_ocr_preview import DocumentOcrPreviewService
    from home_study_agent.document_visual_registration import register_cache
    from home_study_agent.local_ocr.service import OCR_PIPELINE_VERSION
    from test_app import client_for
    from test_document_ocr_preview import start_homework
    from test_visual_capture_producer import parent_headers

    with client_for(tmp_path) as client:
        root, db, generation = start_homework(client)
        service = DocumentOcrPreviewService(db, client.app.state.ocr_workflow)
        received = {}
        monkeypatch.setattr(client.app.state.local_ocr, '_process', lambda image, *_a, **_k: received[sha256(image).hexdigest()])
        for hidden in ((7,), (6,)):
            raw, frame, result = math_paper(hidden=hidden)
            received[sha256(raw).hexdigest()] = {**result, 'question_blocks': [], 'pipeline_version': OCR_PIPELINE_VERSION}
            latest = service.create(root.split('/')[3], raw, str(uuid4()), generation, 'preview-lease',
                sha256(raw).hexdigest(), expected_phase='homework_capture', page_token=PAGE)
        assert latest['page_draft']['merged'] and len(service._visual_landmarks._items) == 1
        unrelated = LandmarkCache()
        register_cache(db, unrelated)
        unrelated.put(('other-session', 'child-junior-3', 'study', 'other-lease', 2, PAGE), 'other', extract_landmarks(raw, frame))
        response = client.post('/v1/parent/children/child-primary-4/delete', headers=parent_headers(client),
                               json={'scope': 'media', 'confirmation': '确认删除', 'client_action_id': str(uuid4())})
        assert response.status_code == 200, response.text
        assert len(service._visual_landmarks._items) == 0
        assert service._visual_landmarks._timer is None
        assert len(unrelated._items) == 1
        unrelated.clear()
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM document_ocr_preview').fetchone()[0] == 0


def test_restore_preparation_discards_visual_cache_even_when_staging_fails(tmp_path):
    import sqlite3
    from home_study_agent.document_ocr_preview import DocumentOcrPreviewService
    from home_study_agent.database import Database
    from test_app import client_for
    from test_document_ocr_preview import start_homework

    with client_for(tmp_path) as client:
        _, db, _ = start_homework(client)
        service = DocumentOcrPreviewService(db, client.app.state.ocr_workflow)
        raw, frame, _ = math_paper()
        service._visual_landmarks.put(('session', 'child-primary-4', 'study', 'lease', 1, PAGE), 'anchor', extract_landmarks(raw, frame))
        # No successful replacement is claimed: the preparation boundary must
        # release old derived image memory even if validation later rejects it.
        invalid = tmp_path / 'invalid.db'
        invalid.write_bytes(b'not a SQLite source' * 100)
        with pytest.raises(sqlite3.DatabaseError):
            client.app.state.privacy_lifecycle._prepare_database_snapshot(invalid, Database(tmp_path / 'staged.db'))
        assert len(service._visual_landmarks._items) == 0
