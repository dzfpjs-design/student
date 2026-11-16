"""Pure provenance for a complete OCR block; never a content or cloud grant."""
import json
import math

from .local_supply import digest

VERSION = 'paper_question_lines_v2'


def _require(value):
    if not value:
        raise ValueError('PAPER_VARIANT_LINE_PROJECTION_INVALID')


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def project_question_lines(artifact, candidate, question, *, formula_review=False):
    """Freeze exact original offsets in block order, without rewriting the page.

    The caller separately validates ownership, metadata restrictions, source
    quality, rights and purpose consent. This function performs no database I/O.
    """
    from .local_ocr.service import _segment_questions
    try:
        details = json.loads(artifact['details_json'])
        region = json.loads(candidate['region_json'])
    except (TypeError, ValueError):
        raise ValueError('PAPER_VARIANT_LINE_PROJECTION_INVALID') from None
    _require(isinstance(details, dict) and isinstance(region, dict))
    lines = details.get('lines')
    pipeline = details.get('local_pipeline')
    _require(isinstance(lines, list) and 0 < len(lines) <= 500 and isinstance(pipeline, dict))
    for quality in (details.get('quality'), pipeline.get('quality')):
        _require(isinstance(quality, dict))
        if formula_review:
            _require(quality.get('passed') is False and quality.get('issues') == ['formula_review_required'])
        else:
            _require(quality.get('passed') is True and not quality.get('issues'))
    blocks = pipeline.get('question_blocks')
    _require(isinstance(blocks, list) and 0 < len(blocks) <= 500 and all(isinstance(b, dict) for b in blocks))
    _require(question == candidate['question_text'])
    matching = [(i, b) for i, b in enumerate(blocks) if b.get('text') == question]
    _require(len(matching) == 1)
    block_index, block = matching[0]
    _require(block.get('block_kind') == 'question' and block.get('needs_manual_boundary_check') is False
             and not block.get('boundary_review_reasons') and not block.get('context_text'))
    bbox = block.get('bbox_xywh')
    _require(isinstance(bbox, list) and len(bbox) == 4 and all(_number(v) for v in bbox)
             and min(bbox) >= 0 and bbox[2] > 0 and bbox[3] > 0)
    _require(set(region) == {'x', 'y', 'width', 'height'} and all(_number(v) for v in region.values())
             and region == dict(zip(('x', 'y', 'width', 'height'), bbox)))
    widths = [b.get('page_x_range') for b in blocks]
    _require(all(isinstance(w, list) and len(w) == 2 and all(_number(v) for v in w) for w in widths))
    width = max(w[1] for w in widths)
    _require(0 < width <= 40000)
    offsets, boxes, texts = [], [], []
    offset = 0
    for line in lines:
        _require(isinstance(line, dict))
        text, box = line.get('text'), line.get('box')
        _require(isinstance(text, str) and text and '\n' not in text and '\r' not in text)
        _require(isinstance(box, list) and len(box) == 4 and all(
            isinstance(point, list) and len(point) == 2 and all(_number(v) and v >= 0 for v in point) for point in box))
        _require(_number(line.get('confidence')) and 0 <= line['confidence'] <= 1)
        xs, ys = [p[0] for p in box], [p[1] for p in box]
        _require(max(xs) > min(xs) and max(ys) > min(ys))
        boxes.append((min(xs), min(ys), max(xs), max(ys)))
        offsets.append((offset, offset + len(text)))
        texts.append(text)
        offset += len(text) + 1
    page = artifact['extracted_text']
    _require(page == '\n'.join(texts))
    # Replay layout from the immutable original lines, not from caller-provided
    # indices. Persisted boundary decisions must agree with the selected block.
    recomputed = [b for b in _segment_questions(lines, width) if b['text'] == question]
    _require(len(recomputed) == 1)
    keys = ('text', 'line_count', 'block_kind', 'confidence', 'bbox_xywh', 'page_index',
            'context_text', 'boundary_review_reasons', 'needs_manual_boundary_check')
    _require(all(recomputed[0].get(k) == block.get(k) for k in keys))
    x, y, w, h = bbox
    contained = {i for i, (left, top, right, bottom) in enumerate(boxes)
                 if left >= x and top >= y and right <= x + w and bottom <= y + h}
    refs, used = [], set()
    for text in question.split('\n'):
        indices = [i for i in contained if texts[i] == text]
        _require(len(indices) == 1 and indices[0] not in used)
        i = indices[0]
        _require(lines[i]['confidence'] >= .85)
        used.add(i)
        start, end = offsets[i]
        refs.append({'index': i, 'start': start, 'end': end, 'line_hash': digest(lines[i])})
    _require(used == contained and len(refs) == block['line_count'])
    _require('\n'.join(page[r['start']:r['end']] for r in refs) == question)
    return {'version': VERSION, 'artifact_hash': digest(dict(artifact)),
        'page_hash': digest(page), 'lines_hash': digest(lines), 'block_index': block_index,
        'block_hash': digest(block), 'question_hash': digest(question), 'line_refs': refs}
