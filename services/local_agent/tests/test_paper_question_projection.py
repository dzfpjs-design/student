"""Exact local OCR line provenance, not OCR accuracy or content permission."""
from copy import deepcopy
import json

import pytest

from home_study_agent.local_ocr.service import _segment_questions


def source():
    lines = [{'text': text, 'confidence': .99, 'box': [[100, y], [800, y], [800, y + 20], [100, y + 20]]}
             for text, y in [('1. 计算37加13。', 100), ('只回答整数。', 150), ('2. 邻题不得发送。', 250)]]
    lines.reverse()
    blocks = _segment_questions(lines, 1000, 800)
    block = blocks[0]
    details = {'lines': lines, 'quality': {'passed': True, 'issues': []},
        'local_pipeline': {'quality': {'passed': True, 'issues': []}, 'question_blocks': blocks}}
    artifact = {'extracted_text': '\n'.join(line['text'] for line in lines), 'details_json': json.dumps(details)}
    candidate = {'question_text': block['text'],
        'region_json': json.dumps(dict(zip(('x', 'y', 'width', 'height'), block['bbox_xywh'])))}
    return artifact, candidate, details


def resolve(artifact, candidate):
    from home_study_agent.paper_question_projection import project_question_lines
    return project_question_lines(artifact, candidate, candidate['question_text'])


def test_exact_reordered_mapping_preserves_original_offsets_and_does_not_contain_text():
    artifact, candidate, _ = source()
    before = deepcopy((artifact, candidate))
    result = resolve(artifact, candidate)
    assert result['version'] == 'paper_question_lines_v2'
    assert [line['index'] for line in result['line_refs']] == [2, 1]
    reconstructed = '\n'.join(artifact['extracted_text'][line['start']:line['end']] for line in result['line_refs'])
    assert reconstructed == candidate['question_text']
    assert result == resolve(artifact, candidate)
    assert (artifact, candidate) == before
    assert candidate['question_text'] not in json.dumps(result, ensure_ascii=False)
    assert '邻题不得发送' not in json.dumps(result, ensure_ascii=False)


@pytest.mark.parametrize('mutation', ['missing_lines', 'page_order', 'line_text', 'geometry', 'region',
    'fragment', 'boundary', 'block_count', 'block_geometry', 'block_order', 'duplicate_block',
    'duplicate_line', 'line_confidence', 'quality', 'unknown_width'])
def test_ambiguous_changed_or_incomplete_mapping_is_not_authorizable(mutation):
    artifact, candidate, details = source()
    block = details['local_pipeline']['question_blocks'][0]
    if mutation == 'missing_lines':
        del details['lines']
    elif mutation == 'page_order':
        artifact['extracted_text'] = candidate['question_text']
    elif mutation == 'line_text':
        details['lines'][1]['text'] = 'changed'
    elif mutation == 'geometry':
        for point in details['lines'][1]['box']:
            point[0] += 300
    elif mutation == 'region':
        candidate['region_json'] = '{"x":0,"y":0,"width":10,"height":10}'
    elif mutation == 'fragment':
        block['block_kind'] = 'fragment'
    elif mutation == 'boundary':
        block['needs_manual_boundary_check'] = True
    elif mutation == 'block_count':
        block['line_count'] += 1
    elif mutation == 'block_geometry':
        block['bbox_xywh'][0] += 1
    elif mutation == 'block_order':
        block['text'] = '\n'.join(reversed(block['text'].splitlines()))
        candidate['question_text'] = block['text']
    elif mutation == 'duplicate_block':
        details['local_pipeline']['question_blocks'].append(deepcopy(block))
    elif mutation == 'duplicate_line':
        details['lines'].append(deepcopy(details['lines'][1]))
        artifact['extracted_text'] = '\n'.join(line['text'] for line in details['lines'])
    elif mutation == 'line_confidence':
        details['lines'][1]['confidence'] = .2
    elif mutation == 'quality':
        details['quality']['passed'] = False
    elif mutation == 'unknown_width':
        for b in details['local_pipeline']['question_blocks']:
            b.pop('page_x_range')
    artifact['details_json'] = json.dumps(details)
    with pytest.raises(ValueError, match='PAPER_VARIANT_'):
        resolve(artifact, candidate)
