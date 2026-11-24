"""A marginal OCR box must not erase a separately verified paper observation."""
import copy
import json

import numpy as np
import pytest

from home_study_agent import document_preview_accumulator as accumulator
from home_study_agent import document_visual_registration as visual
from test_app import client_for
from test_document_ocr_preview import start_homework
from test_document_preview_accumulator import (
    ANCHORS, HIDDEN_LEFT, HIDDEN_RIGHT, PAGE, cpu_observations, transform, upload,
)


EDGE_TEXT = 'Newly revealed marginal observation'


def frame(name, entries):
    return {'frame_sha256': name, 'frame_size': [1599, 1599], 'lines': [
        {'text': text, 'confidence': .99,
         'original_polygon': [[x - .04, y - .008], [x + .04, y - .008],
                              [x + .04, y + .008], [x - .04, y + .008]]}
        for text, x, y in entries]}


def unit_pair():
    fixed = frame('first-sha', ANCHORS + [HIDDEN_LEFT])
    incoming = frame('second-sha', [(text, x, y + .025) for text, x, y in ANCHORS + [HIDDEN_RIGHT]])
    # Legal source coordinates, but this new edge text is above the old frame.
    incoming['lines'].append({'text': EDGE_TEXT, 'confidence': .99,
                             'original_polygon': [[.60, .010], [.64, .010], [.64, .020], [.60, .020]]})
    matrix = np.array([[1., 0., 0.], [0., 1., -.025 * accumulator._scale(fixed)[1]]])
    return fixed, incoming, matrix


def test_partial_draft_keeps_core_and_unmapped_original_without_fake_canonical():
    fixed, incoming, matrix = unit_pair()
    before = copy.deepcopy(incoming)
    first = accumulator.build_page_draft(PAGE, 'first', fixed, visual_anchor=True)
    second = accumulator.build_page_draft(PAGE, 'second', incoming, anchor_id='first', anchor=fixed,
        previous=first, visual_matrix=matrix, registration={'method': visual.METHOD, 'status': 'verified'})
    assert second['merged'] and second['anchor_preview_id'] == 'first'
    assert second['frame_count'] == 2
    text = {line['text']: line for line in second['lines']}
    assert text[HIDDEN_LEFT[0]]['status'] == 'historical'
    assert text[HIDDEN_RIGHT[0]]['status'] == 'current'
    assert EDGE_TEXT not in text
    unmapped, = second['unmapped_lines']
    assert unmapped['text'] == EDGE_TEXT and unmapped['reason'] == 'outside_reference_frame'
    assert unmapped['original_polygon'] == before['lines'][-1]['original_polygon']
    assert unmapped['frame_sha256'] == 'second-sha' and unmapped['preview_id'] == 'second'
    assert unmapped['status'] == 'current' and 'canonical_polygon' not in unmapped
    assert incoming == before
    # A later view must not keep this old private observation looking current.
    third = accumulator.build_page_draft(PAGE, 'third', fixed, anchor_id='first', anchor=fixed,
        previous=second, visual_matrix=np.eye(2, 3), registration={'method': visual.METHOD, 'status': 'verified'})
    assert not third.get('unmapped_lines')


def test_unmapped_text_cannot_supply_missing_independent_body_agreement():
    fixed, incoming, matrix = unit_pair()
    # Leave only five reliable common body groups. Six supported groups remain
    # mandatory even if a sixth common label exists just outside the old view.
    fixed['lines'] = fixed['lines'][:5]
    incoming['lines'] = incoming['lines'][:5]
    edge = {'text': 'Common marginal heading', 'confidence': .99,
            'original_polygon': [[.60, .010], [.64, .010], [.64, .020], [.60, .020]]}
    fixed['lines'].append(copy.deepcopy(edge))
    incoming['lines'].append(copy.deepcopy(edge))
    diagnostics = {}
    consistent, _ = visual._text_evidence(fixed, incoming, matrix, diagnostics)
    assert not consistent
    assert diagnostics['mapped_line_count'] == 5 and diagnostics['unmapped_line_count'] == 1
    assert diagnostics['agreement_groups'] < 6


def test_degenerate_projection_retains_only_original_source_and_cannot_prove_identity():
    fixed, incoming, _ = unit_pair()
    before = copy.deepcopy(incoming)
    matrix = np.zeros((2, 3))
    mapped, unmapped = accumulator.partition_registered_observations('second', incoming, anchor=fixed, matrix=matrix)
    assert not mapped and len(unmapped) == len(incoming['lines'])
    assert {line['reason'] for line in unmapped} == {'degenerate_projection'}
    assert all('canonical_polygon' not in line and line['preview_id'] == 'second' for line in unmapped)
    assert [line['original_polygon'] for line in unmapped] == [line['original_polygon'] for line in before['lines']]
    assert not visual._text_evidence(fixed, incoming, matrix)[0]
    assert incoming == before


@pytest.mark.parametrize('limit', ['line_limit', 'source_limit'])
def test_limit_retains_history_without_stale_unmapped_or_claiming_current_merge(monkeypatch, limit):
    fixed, incoming, matrix = unit_pair()
    first = accumulator.build_page_draft(PAGE, 'first', fixed, visual_anchor=True)
    first['unmapped_lines'] = [{'text': 'old marginal note', 'status': 'current'}]
    monkeypatch.setattr(accumulator, '_merge', lambda *_a, **_k: (None, limit))
    result = accumulator.build_page_draft(PAGE, 'second', incoming, anchor_id='first', anchor=fixed,
        previous=first, visual_matrix=matrix, registration={'method': visual.METHOD, 'status': 'verified'})
    assert result['reason'] == limit and not result['merged']
    assert result['frame_count'] == first['frame_count'] and result['anchor_preview_id'] == 'first'
    assert not result.get('unmapped_lines')
    assert all(line['status'] == 'historical' for line in result['lines'])


def test_unmapped_current_lines_also_count_against_whole_draft_line_budget():
    fixed, incoming, matrix = unit_pair()
    first = accumulator.build_page_draft(PAGE, 'first', fixed, visual_anchor=True)
    for index in range(accumulator.MAX_LINES - len(first['lines']) - 1):
        first['lines'].append({**first['lines'][0], 'text': f'Historical fragment {index}',
            'canonical_polygon': [[.92, .94], [.98, .94], [.98, .96], [.92, .96]]})
    # The mapped part alone fits at 300, but its extra source-only line would
    # make the response exceed the aggregate bound. No history is evicted.
    result = accumulator.build_page_draft(PAGE, 'second', incoming, anchor_id='first', anchor=fixed,
        previous=first, visual_matrix=matrix, registration={'method': visual.METHOD, 'status': 'verified'})
    assert result['reason'] == 'line_limit' and not result['merged']
    assert len(result['lines']) == len(first['lines']) and result['frame_count'] == first['frame_count']
    assert not result.get('unmapped_lines')
    assert all(line['status'] == 'historical' for line in result['lines'])


@pytest.mark.parametrize('angle', [0, 1])
def test_http_partial_alignment_uses_normal_image_gates_and_survives_restart(tmp_path, cpu_observations, angle):
    with client_for(tmp_path) as client:
        root, db, generation = start_homework(client)
        route = root + '/previews'
        response, _ = upload(client, route, generation, cpu_observations, ANCHORS + [HIDDEN_LEFT], 201)
        assert response.status_code == 200, response.text
        first = response.json()
        assert first['page_draft']['reason'] == 'anchor_started', first['page_draft']['reason']
        # Render directly in the second camera view, including a new source
        # box that maps past the fixed frame's right edge. ORB still runs on
        # the unchanged body ink; this is not an injected transform/rejection.
        entries = [(text, x - .06, y) for text, x, y in ANCHORS + [HIDDEN_RIGHT]]
        entries.append((EDGE_TEXT, .905, .43))
        response, body = upload(client, route, generation, cpu_observations, entries, 202, move=transform(angle))
        assert response.status_code == 200, response.text
        second = response.json()
        draft = second['page_draft']
        assert draft['merged'] and draft['frame_count'] == 2, {
            key: draft.get(key) for key in ('reason', 'registration', 'frame_count')}
        assert draft['anchor_preview_id'] == first['preview_id']
        assert draft['registration']['status'] == 'verified'
        assert draft['registration']['inliers'] >= 24 and draft['registration']['holdout_groups'] >= 3
        assert draft['registration']['agreement_groups'] >= 6
        assert draft['registration']['unmapped_line_count'] == 1
        assert EDGE_TEXT in second['text']
        edge, = draft['unmapped_lines']
        assert edge['text'] == EDGE_TEXT and edge['reason'] == 'outside_reference_frame'
        assert edge['preview_id'] == second['preview_id'] and edge['frame_sha256'] == body['frame_signature']
        assert 'canonical_polygon' not in edge
        assert EDGE_TEXT not in {line['text'] for line in draft['lines']}
        assert client.post(route, json=body).json() == second
        with db.connect() as c:
            stored = c.execute('SELECT page_draft_json FROM document_ocr_preview WHERE preview_id=?',
                               (second['preview_id'],)).fetchone()
            assert json.loads(stored[0]) == draft
            assert c.execute('SELECT COUNT(*) FROM capture_artifact').fetchone()[0] == 0
            assert c.execute('SELECT COUNT(*) FROM document_page_revision').fetchone()[0] == 0
            assert c.execute('SELECT COUNT(*) FROM mistake_case').fetchone()[0] == 0
    with client_for(tmp_path) as restarted:
        assert second in restarted.get(route).json()['items']


def test_http_source_limit_keeps_old_anchor_and_never_replays_unmapped_as_current(tmp_path, monkeypatch, cpu_observations):
    with client_for(tmp_path) as client:
        root, _, generation = start_homework(client)
        route = root + '/previews'
        response, _ = upload(client, route, generation, cpu_observations, ANCHORS + [HIDDEN_LEFT], 211)
        first = response.json()
        monkeypatch.setattr(accumulator, 'MAX_SOURCE_FRAMES', 1)
        entries = [(text, x - .06, y) for text, x, y in ANCHORS + [HIDDEN_RIGHT]]
        response, body = upload(client, route, generation, cpu_observations,
            entries + [(EDGE_TEXT, .905, .43)], 212, move=transform(1))
        assert response.status_code == 200, response.text
        limited = response.json()
        draft = limited['page_draft']
        assert draft['registration']['status'] == 'verified'
        assert draft['reason'] == 'source_limit' and not draft['merged']
        assert draft['anchor_preview_id'] == first['preview_id'] and draft['frame_count'] == 1
        assert not draft.get('unmapped_lines')
        assert all(line['status'] == 'historical' for line in draft['lines'])
        assert EDGE_TEXT in limited['text']  # The separate raw reading survives.
        assert client.post(route, json=body).json() == limited
        monkeypatch.setattr(accumulator, 'MAX_SOURCE_FRAMES', 32)
        response, _ = upload(client, route, generation, cpu_observations, entries, 213, move=transform(1))
        assert response.status_code == 200, response.text
        after = response.json()['page_draft']
        assert after['merged'] and after['anchor_preview_id'] == first['preview_id']
        assert not after.get('unmapped_lines')
