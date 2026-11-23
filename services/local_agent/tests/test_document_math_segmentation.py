"""Arithmetic OCR split/join changes over real source ink and fixed landmarks."""
from hashlib import sha256

import cv2
import numpy as np
import pytest

from home_study_agent.document_preview_accumulator import _merge, build_page_draft, verified_frame
from home_study_agent.document_text_overlap import compare, text_key
from home_study_agent.document_visual_registration import _text_evidence, extract_landmarks, register
from home_study_agent.local_ocr.text_geometry import build
from test_document_visual_registration import PAGE, math_paper


def equation_paper(*, split=False, spaces=True, index=0, partial=(), changed=(), anomalous=False, digit_fragment=False):
    width, height = 1600, 1800
    image = np.full((height, width, 3), 248, np.uint8)
    image[0, 0] = index
    lines = []
    for question in range(8):
        x, baseline = 170 + question % 2 * 750, 240 + question // 2 * 400
        numerator = 246 + question * 79
        answer = str(numerator // 6 if question not in changed else 99 - question)
        prefix = f'{question + 1}.{numerator}/6='
        text = prefix + ' ' + answer
        if anomalous and question == 2:
            prefix, answer, text = '305', '13180', '30513180'
        if digit_fragment and question == 2:
            prefix, answer, text = '1', '23', '123'
        # Draw only the pixels that this sample's detector observations see.
        rendered = prefix[:5] if question in partial else text
        cv2.putText(image, rendered, (x, baseline), cv2.FONT_HERSHEY_SIMPLEX, 1.1, (10, 10, 10), 2, cv2.LINE_AA)
        def add(value, left, pixel_value):
            (w, h), bottom = cv2.getTextSize(pixel_value, cv2.FONT_HERSHEY_SIMPLEX, 1.1, 2)
            lines.append({'text': value, 'confidence': .98, 'kind': 'printed',
                          'box': [[left - 2, baseline - h - 2], [left + w + 2, baseline - h - 2],
                                  [left + w + 2, baseline + bottom + 2], [left - 2, baseline + bottom + 2]]})
        if question in partial:
            add(prefix[:5], x, prefix[:5])
        elif split:
            tight = (anomalous or digit_fragment) and question == 2
            offset = cv2.getTextSize(prefix + ('' if tight else ' '), cv2.FONT_HERSHEY_SIMPLEX, 1.1, 2)[0][0]
            add(prefix, x, prefix)
            add(answer, x + offset, answer)
        else:
            observed = ('3013180' if anomalous and question == 2 else prefix + (' ' if spaces else '') + answer)
            if digit_fragment and question == 2:
                observed = '123'
            add(observed, x, text)
    raw = cv2.imencode('.png', image)[1].tobytes()
    geometry = build(lines, frame_sha256=sha256(raw).hexdigest(), frame_size=[width, height],
                     prepared_size=[width, height], frame_to_prepared=np.eye(3).tolist(), degrees=0)
    return raw, verified_frame({'engine': 'rapidocr-onnx', 'lines': lines, 'text_geometry': geometry}, sha256(raw).hexdigest())


def merge(raw_a, a, raw_b, b):
    landmarks_a, landmarks_b = extract_landmarks(raw_a, a), extract_landmarks(raw_b, b)
    assert landmarks_a is not None and landmarks_b is not None
    matrix, conflict, evidence = register(a, b, landmarks_a, landmarks_b)
    first = build_page_draft(PAGE, 'first', a, visual_anchor=True)
    second = build_page_draft(PAGE, 'second', b, anchor_id='first', anchor=a, previous=first,
                              visual_matrix=matrix, visual_conflict=conflict, registration=evidence)
    return second, matrix, conflict


@pytest.mark.parametrize('old_split,new_split', [(False, True), (True, False)])
def test_whole_equations_and_fragment_readings_register_without_false_page_change(old_split, new_split):
    raw_a, a = equation_paper(split=old_split, index=1)
    raw_b, b = equation_paper(split=new_split, index=2)
    draft, matrix, conflict = merge(raw_a, a, raw_b, b)
    assert matrix is not None and not conflict
    assert draft['merged'] and draft['frame_count'] == 2 and draft['anchor_preview_id'] == 'first'
    assert all(line['preview_id'] == 'second' and line['status'] == 'current' and not line['conflict'] for line in draft['lines'])
    assert len(draft['lines']) == (16 if new_split else 8)


def test_operator_whitespace_does_not_change_math_but_digit_word_spaces_are_preserved():
    raw_a, a = equation_paper(spaces=True, index=1)
    raw_b, b = equation_paper(spaces=False, index=2)
    draft, matrix, conflict = merge(raw_a, a, raw_b, b)
    assert matrix is not None and draft['merged'] and not conflict
    assert len(draft['lines']) == 8 and all(not line['conflict'] for line in draft['lines'])
    assert text_key('8.630÷21= 30') == text_key('8.630÷21=30')
    assert text_key('1 23') != text_key('123')
    assert text_key('12+3') != text_key('12-3')
    assert text_key('x = 30') != text_key('x=30')
    assert text_key('two words') != text_key('twowords')


def test_half_hidden_equation_keeps_original_unknown_suffix_and_current_conflict():
    raw_a, a = equation_paper(index=1)
    raw_b, b = equation_paper(index=2, partial=(4,))
    draft, matrix, conflict = merge(raw_a, a, raw_b, b)
    assert matrix is not None and draft['merged'] and not conflict
    old = next(line for line in draft['lines'] if line['text'] == '5.562/6= 93')
    partial = next(line for line in draft['lines'] if line['text'] == '5.562')
    assert old['status'] == 'historical' and old['conflict'] and old['preview_id'] == 'first'
    assert partial['status'] == 'current' and partial['conflict'] and partial['preview_id'] == 'second'


def test_insufficient_independent_text_still_keeps_different_equations_separate():
    raw_a, a = equation_paper(index=1)
    raw_b, b = equation_paper(index=2, changed=(0, 1, 6))
    draft, matrix, conflict = merge(raw_a, a, raw_b, b)
    assert matrix is None and conflict
    assert draft['reason'] == 'conflicting_page' and draft['anchor_preview_id'] == 'second'
    assert not draft['merged'] and {line['preview_id'] for line in draft['lines']} == {'second'}


def test_same_page_three_answer_corrections_keep_fixed_anchor_and_both_readings():
    raw_a, a, _ = math_paper()
    corrections = {(0, 3): '99', (1, 3): '98', (6, 3): '999'}
    raw_b, b, _ = math_paper(changed=corrections)
    draft, matrix, conflict = merge(raw_a, a, raw_b, b)
    assert matrix is not None and not conflict
    assert draft['merged'] and draft['frame_count'] == 2 and draft['anchor_preview_id'] == 'first'
    assert draft['registration']['status'] == 'verified'
    assert 3 <= draft['registration']['content_conflict_count'] <= 300
    for (question, index), value in corrections.items():
        old_source = a['lines'][question * 8 + index]
        old = next(line for line in draft['lines'] if line['preview_id'] == 'first'
                   and line['original_polygon'] == old_source['original_polygon'])
        assert old['status'] == 'historical' and old['conflict'] and old['text'] == old_source['text']
        current = next(line for line in draft['lines'] if line['preview_id'] == 'second' and line['text'] == value)
        assert current['status'] == 'current' and current['conflict']
    # A separate page token always starts a new candidate; image similarity
    # cannot carry an earlier page's revision history across that boundary.
    other = build_page_draft('other-page-token', 'third', b, visual_anchor=True)
    assert other['frame_count'] == 1 and not other['merged']
    assert {line['preview_id'] for line in other['lines']} == {'third'}


def test_non_equivalent_fragments_are_one_physical_conflict_and_never_repair_digits():
    raw_a, a = equation_paper(index=1, anomalous=True)
    raw_b, b = equation_paper(index=2, anomalous=True, split=True)
    draft, matrix, conflict = merge(raw_a, a, raw_b, b)
    assert matrix is not None and not conflict and draft['merged']
    assert any(line['text'] == '3013180' and line['status'] == 'historical' and line['conflict'] for line in draft['lines'])
    assert any(line['text'] == '305' and line['status'] == 'current' and line['conflict'] for line in draft['lines'])
    assert any(line['text'] == '13180' and line['status'] == 'current' and line['conflict'] for line in draft['lines'])
    groups = compare([{**line, 'canonical_polygon': line['original_polygon']} for line in a['lines']],
                     [{**line, 'canonical_polygon': line['original_polygon']} for line in b['lines']])
    assert sum(group.broad_conflict for group in groups) == 1


def test_digit_fragment_boundary_never_supplies_the_absence_of_a_space():
    raw_a, a = equation_paper(index=1, digit_fragment=True)
    raw_b, b = equation_paper(index=2, digit_fragment=True, split=True)
    draft, matrix, conflict = merge(raw_a, a, raw_b, b)
    assert matrix is not None and not conflict and draft['merged']
    assert any(line['text'] == '123' and line['status'] == 'historical' and line['conflict'] for line in draft['lines'])
    assert any(line['text'] == '1' and line['status'] == 'current' and line['conflict'] for line in draft['lines'])
    assert any(line['text'] == '23' and line['status'] == 'current' and line['conflict'] for line in draft['lines'])


@pytest.mark.parametrize('old_text,new_texts', [
    ('1+1=2 2+2=4 3+3=6', ['1+1=3', '2+2=5', '3+3=7']),
    ('The original three observations', ['First changed observation', 'Second changed observation', 'Third changed observation']),
])
def test_one_wide_old_box_cannot_hide_three_independent_changed_equations_or_english_regions(old_text, new_texts):
    def line(text, polygon, source):
        return {'text': text, 'confidence': .98, 'canonical_polygon': polygon, 'original_polygon': polygon,
                'preview_id': source, 'status': 'current', 'conflict': False, 'frame_sha256': source * 64}
    previous = [line(old_text, [[.05, .05], [.95, .05], [.95, .95], [.05, .95]], 'a')]
    incoming = [line(text, [[x - .09, y - .02], [x + .09, y - .02], [x + .09, y + .02], [x - .09, y + .02]], 'b')
                for text, (x, y) in zip(new_texts, [( .2, .2), (.75, .2), (.2, .75)])]
    groups = compare(previous, incoming)
    assert len(groups) == 1 and len(groups[0].conflict_centers) == 3
    combined, reason = _merge(previous, incoming, 'a')
    assert combined is None and reason == 'conflicting_page'
    consistent, conflicting = _text_evidence({'frame_size': [1599, 1799], 'lines': previous},
                                           {'frame_size': [1599, 1799], 'lines': incoming},
                                           np.array([[1, 0, 0], [0, 1, 0]]))
    assert not consistent and conflicting
