import base64
from hashlib import sha256
from math import cos, radians, sin

import cv2
import numpy as np
import pytest

from home_study_agent.local_ocr.service import RapidOcrEngine
from home_study_agent.local_ocr import service as ocr_service
from test_app import client_for
from test_document_ocr_preview import start_homework


PAGE = '081a5aad-efa9-4607-bef2-67a00aa66071'
ANOTHER_PAGE = '0d394dde-d521-4cf6-8c67-0a62860adb0e'
ANCHORS = [
    ('Triangle perimeter calculation', .22, .18),
    ('Compare product including decimals', .72, .20),
    ('Horizontal axis of the diagram', .72, .39),
    ('Find the missing dimension', .22, .54),
    ('Rectangle with equal diagonals', .73, .68),
    ('Estimate kilograms and ounces', .23, .81),
]
HIDDEN_LEFT = ('1. Identify the equivalent fractions.', .24, .35)
HIDDEN_RIGHT = ('2. Calculate the change in volume.', .71, .55)
OLD_ANSWER = ('The intermediate value is 9.', .24, .69)
NEW_ANSWER = ('The intermediate value is 4.', .24, .69)


def raw_frame(index, entries, move):
    """Render the reported text inside its actual source boxes, then move both."""
    image = np.full((1600, 1600, 3), 248, dtype=np.uint8)
    height, width = image.shape[:2]
    for text, x, y in entries:
        (text_width, text_height), baseline = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 1.2, 2)
        tile = np.full((text_height + baseline + 8, text_width + 8, 3), 248, dtype=np.uint8)
        cv2.putText(tile, text, (4, text_height + 4), cv2.FONT_HERSHEY_SIMPLEX, 1.2,
                    (10, 10, 10), 2, cv2.LINE_AA)
        left, right = (round(value * (width - 1)) for value in (x - .086, x + .086))
        top, bottom = (round(value * (height - 1)) for value in (y - .012, y + .012))
        image[top:bottom + 1, left:right + 1] = cv2.resize(tile, (right - left + 1, bottom - top + 1))
    source = np.array([[0., 0.], [1., 0.], [0., 1.]])
    destination = np.asarray([move(*point) for point in source])
    matrix = cv2.getAffineTransform((source * (width - 1, height - 1)).astype(np.float32),
                                    (destination * (width - 1, height - 1)).astype(np.float32))
    image = cv2.warpAffine(image, matrix, (width, height), borderValue=(248, 248, 248))
    # A fresh exact source identity outside every text mask.
    image[3, 4] = index
    return cv2.imencode('.png', image)[1].tobytes()


def transform(degrees=0, shift=(0, 0)):
    angle = radians(degrees)
    matrix = np.array([[cos(angle), -sin(angle)], [sin(angle), cos(angle)]])
    return lambda x, y: (np.array([x, y]) - .5) @ matrix.T + .5 + shift


@pytest.fixture
def cpu_observations(monkeypatch):
    """Control OCR readings while retaining real ink, ORB, quality and persistence."""
    received = {'entries': [], 'move': transform()}
    prepare_page = ocr_service._prepare_page

    def prepare_without_crop(image):
        _, quality, _ = prepare_page(image)
        quality['transforms'].update(perspective_correction=False, frame_to_prepared=np.eye(3).tolist(),
                                     prepared_size=[image.shape[1], image.shape[0]])
        return image.copy(), quality, image.copy()

    def recognize(_, image, route):
        height, width = image.shape[:2]
        lines = []
        for text, x, y in received['entries']:
            corners = [(x - .086, y - .012), (x + .086, y - .012),
                       (x + .086, y + .012), (x - .086, y + .012)]
            points = [received['move'](*point) * (width - 1, height - 1) for point in corners]
            box = [[int(round(x)), int(round(y))] for x, y in points]
            lines.append({'text': text, 'confidence': .98, 'box': box, 'kind': 'printed'})
        return lines

    monkeypatch.setattr(RapidOcrEngine, 'health', lambda _: {'ready': True, 'routes': ['cpu']})
    monkeypatch.setattr(RapidOcrEngine, 'recognize', recognize)
    monkeypatch.setattr(RapidOcrEngine, 'recognize_page_orientation', recognize)
    monkeypatch.setattr(ocr_service, '_prepare_page', prepare_without_crop)
    return received


def upload(client, route, generation, received, entries, index, *, token=PAGE, move=None,
           phase='homework_capture', lease='preview-lease', action_id=None):
    received['entries'] = entries
    received['move'] = move or transform()
    raw = raw_frame(index, entries, received['move'])
    body = {'image_base64': base64.b64encode(raw).decode(), 'frame_signature': sha256(raw).hexdigest(),
            'client_action_id': action_id or f'page-observation-frame-{index}', 'expected_generation': generation,
            'expected_lease_id': lease, 'expected_phase': phase}
    if token is not None:
        body['page_token'] = token
    response = client.post(route, json=body)
    return response, body


def test_http_combines_complementary_visible_regions_preserves_conflict_and_restart(tmp_path, cpu_observations):
    with client_for(tmp_path) as client:
        root, db, generation = start_homework(client)
        route = root + '/previews'
        first, _ = upload(client, route, generation, cpu_observations, ANCHORS + [HIDDEN_LEFT, OLD_ANSWER], 1)
        assert first.status_code == 200, first.text
        one = first.json()
        assert one['page_draft']['reason'] == 'anchor_started', one['page_draft']['reason']
        second, body = upload(client, route, generation, cpu_observations, ANCHORS + [HIDDEN_RIGHT, NEW_ANSWER],
                              2, move=transform(2, (.016, -.011)))
        assert second.status_code == 200, second.text
        two = second.json()
        draft = two['page_draft']
        assert draft['merged'] and draft['frame_count'] == 2, draft['reason']
        assert draft['alignment_method'] == 'local_orb_v1'
        assert draft['registration']['matches'] >= 32 and draft['registration']['inliers'] >= 24
        assert draft['anchor_preview_id'] == one['preview_id']
        assert HIDDEN_LEFT[0] not in two['text'] and HIDDEN_RIGHT[0] not in one['text']
        lines = {line['text']: line for line in draft['lines']}
        assert lines[HIDDEN_LEFT[0]]['status'] == 'historical'
        assert lines[HIDDEN_LEFT[0]]['preview_id'] == one['preview_id']
        assert lines[HIDDEN_RIGHT[0]]['status'] == 'current'
        assert lines[HIDDEN_RIGHT[0]]['frame_sha256'] == body['frame_signature']
        assert lines[OLD_ANSWER[0]]['conflict'] and lines[NEW_ANSWER[0]]['conflict']
        line = lines[ANCHORS[0][0]]
        assert line['original_polygon'] != line['canonical_polygon']
        original = next(item['original_polygon'] for item in one['page_draft']['lines'] if item['text'] == line['text'])
        np.testing.assert_allclose(line['canonical_polygon'], original, atol=.007)
        assert client.post(route, json=body).json() == two
        changed_token = client.post(route, json={**body, 'page_token': ANOTHER_PAGE})
        assert changed_token.status_code == 409
        assert changed_token.json()['detail']['code'] == 'DOCUMENT_PREVIEW_ACTION_CONFLICT'
        assert client.post(route, json={**body, 'page_token': 'not-a-uuid'}).status_code == 422
        assert client.post(route, json={**body, 'geometry_json': 'untrusted'}).status_code == 422
        turned, _ = upload(client, route, generation, cpu_observations, ANCHORS + [HIDDEN_RIGHT], 3, token=ANOTHER_PAGE)
        assert turned.status_code == 200, turned.text
        assert turned.json()['page_draft']['frame_count'] == 1
        assert HIDDEN_LEFT[0] not in {line['text'] for line in turned.json()['page_draft']['lines']}
        with db.connect() as c:
            rows = c.execute('SELECT geometry_json,page_draft_json FROM document_ocr_preview').fetchall()
            assert len(rows) == 3 and all(row['geometry_json'] and row['page_draft_json'] for row in rows)
            assert c.execute('SELECT COUNT(*) FROM capture_artifact').fetchone()[0] == 0
            assert c.execute('SELECT COUNT(*) FROM document_page_revision').fetchone()[0] == 0
            assert c.execute('SELECT COUNT(*) FROM learning_evidence').fetchone()[0] == 0
    with client_for(tmp_path) as restarted:
        result = restarted.get(route)
        assert result.status_code == 200, result.text
        assert two in result.json()['items']
        stale = restarted.post(route, json=body)
        assert stale.status_code == 409 and stale.json()['detail']['code'] == 'DOCUMENT_PREVIEW_SOURCE_CHANGED'


def test_http_never_uses_earlier_phase_lease_or_generation_as_anchor(tmp_path, cpu_observations):
    with client_for(tmp_path) as client:
        root, db, generation = start_homework(client)
        route = root + '/previews'
        first, _ = upload(client, route, generation, cpu_observations, ANCHORS + [HIDDEN_LEFT], 10)
        assert first.status_code == 200 and first.json()['page_draft']['reason'] == 'anchor_started'

        with db.connect() as c:
            c.execute("UPDATE daily_session SET state='plan_review'")
            c.commit()
        phase, _ = upload(client, route, generation, cpu_observations, ANCHORS + [HIDDEN_RIGHT], 11,
                          phase='plan_review')
        assert phase.status_code == 200 and phase.json()['page_draft']['frame_count'] == 1
        assert HIDDEN_LEFT[0] not in {line['text'] for line in phase.json()['page_draft']['lines']}

        with db.connect() as c:
            c.execute("UPDATE sensor_lease SET status='released' WHERE lease_id='preview-lease'")
            c.execute("""INSERT INTO sensor_lease SELECT 'replacement-lease',session_id,device_id,device_type,
                'active',opened_at,last_renewed_at,expires_at,NULL,NULL FROM sensor_lease WHERE lease_id='preview-lease'""")
            c.commit()
        lease, _ = upload(client, route, generation, cpu_observations, ANCHORS + [HIDDEN_LEFT], 12,
                          phase='plan_review', lease='replacement-lease')
        assert lease.status_code == 200 and lease.json()['page_draft']['frame_count'] == 1

        with db.connect() as c:
            c.execute("UPDATE document_monitor_session SET generation=generation+1")
            c.commit()
        gen, _ = upload(client, route, generation + 1, cpu_observations, ANCHORS + [HIDDEN_RIGHT], 13,
                         phase='plan_review', lease='replacement-lease')
        assert gen.status_code == 200 and gen.json()['page_draft']['frame_count'] == 1


def test_http_rejects_same_token_wrong_alignment_template_and_chain_drift(tmp_path, cpu_observations):
    with client_for(tmp_path) as client:
        root, _, generation = start_homework(client)
        route = root + '/previews'
        first, _ = upload(client, route, generation, cpu_observations, ANCHORS + [HIDDEN_LEFT], 20)
        assert first.status_code == 200 and first.json()['page_draft']['reason'] == 'anchor_started'

        compatible, _ = upload(client, route, generation, cpu_observations, ANCHORS + [HIDDEN_RIGHT], 21,
                               move=transform(4, (.035, .030)))
        assert compatible.status_code == 200 and compatible.json()['page_draft']['reason'] == 'merged'
        drifted, _ = upload(client, route, generation, cpu_observations, ANCHORS, 22,
                            move=transform(8, (.060, .050)))
        assert drifted.status_code == 200 and not drifted.json()['page_draft']['merged']
        assert drifted.json()['page_draft']['frame_count'] == 1
        assert HIDDEN_LEFT[0] not in {line['text'] for line in drifted.json()['page_draft']['lines']}

        # Once the eight-degree view becomes the candidate, replaying the old
        # pixels must not restore that older anchor or its accumulated history.
        returned, _ = upload(client, route, generation, cpu_observations, ANCHORS + [HIDDEN_LEFT], 20,
                             action_id='return-to-old-geometry')
        assert returned.status_code == 200, returned.text
        returned_draft = returned.json()['page_draft']
        assert not returned_draft['merged'] and returned_draft['frame_count'] == 1
        assert returned_draft['anchor_preview_id'] == returned.json()['preview_id']
        assert returned_draft['anchor_preview_id'] != first.json()['preview_id']
        assert {line['preview_id'] for line in returned_draft['lines']} == {returned.json()['preview_id']}
        assert HIDDEN_RIGHT[0] not in {line['text'] for line in returned_draft['lines']}
        repeated, _ = upload(client, route, generation, cpu_observations, ANCHORS + [HIDDEN_LEFT], 20,
                             action_id='same-pixels-synthetic-retry')
        assert repeated.status_code == 200 and repeated.json()['page_draft']['reason'] == 'duplicate_frame'

        template = [('Name and identification number', x, y) for _, x, y in ANCHORS]
        duplicate_heading, _ = upload(client, route, generation, cpu_observations, template + [HIDDEN_RIGHT],
                                      23, token=ANOTHER_PAGE)
        assert duplicate_heading.status_code == 200
        assert duplicate_heading.json()['page_draft']['reason'] == 'insufficient_anchors'
        assert duplicate_heading.json()['page_draft']['anchor_preview_id'] is None


def test_answer_revisions_across_regions_keep_fixed_anchor_and_history(tmp_path, cpu_observations):
    first_page = [
        ('The measurement is exactly 917.', .25, .32),
        ('The allowed difference is twelve.', .7, .48),
        ('The second enclosure is wider.', .24, .66),
    ]
    other_page = [
        ('The measurement is exactly 463.', .25, .32),
        ('The allowed difference is seven.', .7, .48),
        ('The second enclosure is narrower.', .24, .66),
    ]
    with client_for(tmp_path) as client:
        root, _, generation = start_homework(client)
        route = root + '/previews'
        first, _ = upload(client, route, generation, cpu_observations, ANCHORS + first_page, 30)
        assert first.status_code == 200 and first.json()['page_draft']['reason'] == 'anchor_started'
        conflicting, _ = upload(client, route, generation, cpu_observations, ANCHORS + other_page, 31)
        assert conflicting.status_code == 200, conflicting.text
        draft = conflicting.json()['page_draft']
        assert draft['merged'] and draft['frame_count'] == 2
        assert draft['alignment_method'] == 'local_orb_v1'
        assert draft['anchor_preview_id'] == first.json()['preview_id']
        lines = {line['text']: line for line in draft['lines']}
        for old, new in zip(first_page, other_page):
            assert lines[old[0]]['status'] == 'historical' and lines[old[0]]['conflict']
            assert lines[new[0]]['status'] == 'current' and lines[new[0]]['conflict']
        # Corrections are competing readings on this checked visual anchor.
        # Occluding the corrected text preserves both versions and provenance.
        partial, _ = upload(client, route, generation, cpu_observations, ANCHORS + other_page[:1], 32)
        assert partial.status_code == 200, partial.text
        next_draft = partial.json()['page_draft']
        assert next_draft['merged'] and next_draft['frame_count'] == 3
        assert next_draft['anchor_preview_id'] == first.json()['preview_id']
        retained = {line['text']: line for line in next_draft['lines']}
        assert all(item[0] in retained for item in first_page + other_page)
        assert retained[other_page[1][0]]['status'] == 'historical'
        assert retained[other_page[1][0]]['preview_id'] == conflicting.json()['preview_id']


def test_changed_independent_content_starts_new_candidate_without_reviving_old_anchor(tmp_path, cpu_observations):
    replacement_text = [
        'Resolve the sequence through division',
        'List the complete set of prime factors',
        'Draw the corresponding vertical segment',
        'Explain the ratio between both lengths',
        'Arrange the objects into equal groups',
        'Measure the total capacity in litres',
    ]
    replacement = [(text, x, y) for text, (_, x, y) in zip(replacement_text, ANCHORS)]
    new_detail = ('Evaluate the newly supplied diameter', .71, .55)
    with client_for(tmp_path) as client:
        root, _, generation = start_homework(client)
        route = root + '/previews'
        first, _ = upload(client, route, generation, cpu_observations, ANCHORS + [HIDDEN_LEFT], 34)
        assert first.status_code == 200 and first.json()['page_draft']['reason'] == 'anchor_started'
        changed, _ = upload(client, route, generation, cpu_observations, replacement + [new_detail], 35)
        assert changed.status_code == 200, changed.text
        candidate = changed.json()
        draft = candidate['page_draft']
        assert not draft['merged'] and draft['frame_count'] == 1
        assert draft['anchor_preview_id'] == candidate['preview_id']
        assert {line['preview_id'] for line in draft['lines']} == {candidate['preview_id']}
        assert not {text for text, _, _ in ANCHORS + [HIDDEN_LEFT]} & {line['text'] for line in draft['lines']}
        partial, _ = upload(client, route, generation, cpu_observations, replacement, 36)
        assert partial.status_code == 200, partial.text
        resumed = partial.json()['page_draft']
        assert resumed['merged'] and resumed['frame_count'] == 2
        assert resumed['anchor_preview_id'] == candidate['preview_id']
        historical = next(line for line in resumed['lines'] if line['text'] == new_detail[0])
        assert historical['status'] == 'historical' and historical['preview_id'] == candidate['preview_id']
        returned, _ = upload(client, route, generation, cpu_observations, ANCHORS + [HIDDEN_LEFT], 34,
                             action_id='older-page-pixels-reappear')
        assert returned.status_code == 200, returned.text
        returned_draft = returned.json()['page_draft']
        assert not returned_draft['merged'] and returned_draft['frame_count'] == 1
        assert returned_draft['anchor_preview_id'] == returned.json()['preview_id']
        assert {line['preview_id'] for line in returned_draft['lines']} == {returned.json()['preview_id']}


def test_http_rejects_geometry_from_a_different_frame(tmp_path, cpu_observations, monkeypatch):
    with client_for(tmp_path) as client:
        root, db, generation = start_homework(client)
        workflow = client.app.state.ocr_workflow
        finish = workflow.finish_recognition

        def mismatched_geometry(*args):
            job = finish(*args)
            job['result']['text_geometry']['frame_sha256'] = '0' * 64
            return job

        monkeypatch.setattr(workflow, 'finish_recognition', mismatched_geometry)
        response, _ = upload(client, root + '/previews', generation, cpu_observations, ANCHORS, 41)
        assert response.status_code == 200, response.text
        result = response.json()
        assert ANCHORS[0][0] in result['text']
        assert result['page_draft']['reason'] == 'geometry_unavailable'
        assert result['page_draft']['lines'] == []
        with db.connect() as c:
            assert c.execute('SELECT geometry_json FROM document_ocr_preview').fetchone()[0] is None


def test_http_same_page_token_does_not_read_another_child_or_session(tmp_path, cpu_observations):
    with client_for(tmp_path) as client:
        root, db, generation = start_homework(client)
        route = root + '/previews'
        first, _ = upload(client, route, generation, cpu_observations, ANCHORS + [HIDDEN_LEFT], 51)
        assert first.status_code == 200 and first.json()['page_draft']['reason'] == 'anchor_started'
        sid = root.split('/')[3]
        # Simulate an archived source imported into this same database. All
        # other selector keys are kept deliberately identical to test scope.
        with db.connect() as c:
            c.execute("""INSERT INTO daily_session(session_id,child_id,status,state,started_at,ended_at,
                last_checkpoint_at,client_action_id) SELECT 'other-session',child_id,'completed',state,
                started_at,started_at,last_checkpoint_at,'other-session-fixture'
                FROM daily_session WHERE session_id=?""", (sid,))
            c.execute("UPDATE document_ocr_preview SET session_id='other-session'")
            c.commit()
        other_session, _ = upload(client, route, generation, cpu_observations, ANCHORS + [HIDDEN_RIGHT], 52)
        assert other_session.status_code == 200 and other_session.json()['page_draft']['frame_count'] == 1
        assert HIDDEN_LEFT[0] not in {line['text'] for line in other_session.json()['page_draft']['lines']}
        with db.connect() as c:
            c.execute("""UPDATE document_ocr_preview SET session_id=?,child_id='child-junior-3'""", (sid,))
            c.commit()
        other_child, _ = upload(client, route, generation, cpu_observations, ANCHORS, 53)
        assert other_child.status_code == 200 and other_child.json()['page_draft']['frame_count'] == 1
        assert len(client.get(route).json()['items']) == 1


def test_draft_caps_preserve_the_previous_history_after_insufficient_line_budget():
    from home_study_agent.document_preview_accumulator import build_page_draft, verified_frame
    from home_study_agent.local_ocr.text_geometry import build as build_geometry

    def source(entries, digest):
        lines = [{'text': text, 'confidence': .98, 'box': [[x - 20, y - 8], [x + 20, y - 8],
                 [x + 20, y + 8], [x - 20, y + 8]]}
                 for text, x, y in entries]
        geometry = build_geometry(lines, frame_sha256=digest, frame_size=(801, 801), prepared_size=(801, 801),
                                  frame_to_prepared=np.eye(3).tolist(), degrees=0)
        return verified_frame({'engine': 'rapidocr-onnx', 'lines': lines, 'text_geometry': geometry}, digest)

    landmarks = [(text, int(x * 800), int(y * 800)) for text, x, y in ANCHORS]
    anchor = source(landmarks, 'a' * 64)
    prior = build_page_draft(PAGE, 'anchor', anchor)
    current = source(landmarks + [('Visible new sentence', 160, 210)], 'b' * 64)
    past_lines = prior['lines'] + [{**prior['lines'][0], 'text': f'Historical candidate {index}'}
                                   for index in range(300 - len(prior['lines']))]
    capped = build_page_draft(PAGE, 'line-limit', current, anchor_id='anchor', anchor=anchor,
                              previous={**prior, 'lines': past_lines})
    assert capped['reason'] == 'line_limit' and capped['merged'] and len(capped['lines']) == 300
    assert all(line['status'] == 'historical' for line in capped['lines'])
    assert 'Visible new sentence' not in {line['text'] for line in capped['lines']}
    distinct_sources = prior['lines'] + [
        {**prior['lines'][0], 'text': f'Historical candidate {index}', 'preview_id': f'history-{index}'}
        for index in range(31)
    ]
    capped = build_page_draft(PAGE, 'source-limit', current, anchor_id='anchor', anchor=anchor,
                              previous={**prior, 'lines': distinct_sources, 'frame_count': 32})
    assert capped['reason'] == 'source_limit' and capped['merged'] and capped['frame_count'] == 32
    assert {line['text'] for line in capped['lines']} == {line['text'] for line in distinct_sources}
    assert all(line['status'] == 'historical' for line in capped['lines'])


def test_http_keeps_reading_past_32_captures_with_a_bounded_provenance_window(tmp_path, cpu_observations):
    with client_for(tmp_path) as client:
        root, _, generation = start_homework(client)
        route = root + '/previews'
        anchor_response, _ = upload(client, route, generation, cpu_observations, ANCHORS + [HIDDEN_LEFT], 60)
        assert anchor_response.status_code == 200
        anchor = anchor_response.json()
        assert anchor['page_draft']['reason'] == 'anchor_started'
        # A new exact image of the same visible content refreshes its source
        # without creating a new historical variant in every snapshot.
        for index in range(34):
            response, _ = upload(client, route, generation, cpu_observations,
                                 ANCHORS + [HIDDEN_RIGHT], index + 61)
            assert response.status_code == 200, response.text
        latest = response.json()
        draft = latest['page_draft']
        assert draft['merged'] and draft['frame_count'] == 35
        assert draft['alignment_method'] == 'local_orb_v1'
        assert draft['anchor_preview_id'] == anchor['preview_id']
        origins = {line['preview_id'] for line in draft['lines']}
        assert origins == {anchor['preview_id'], latest['preview_id']}
        assert HIDDEN_LEFT[0] in {line['text'] for line in draft['lines']}
        assert HIDDEN_RIGHT[0] in latest['text']
        assert len(client.get(route).json()['items']) == 10
        assert client.get(route).json()['items'][0] == latest
