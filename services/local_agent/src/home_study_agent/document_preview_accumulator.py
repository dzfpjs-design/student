"""Align local OCR observations to one fixed source frame, without joining pixels."""
from collections import Counter
from math import atan2, degrees, hypot
import re
from unicodedata import normalize

import cv2
import numpy as np

from .local_ocr.text_geometry import validate as validate_geometry
from .document_text_overlap import compare as compare_text_overlap

MAX_SOURCE_FRAMES = 32
MAX_LINES = 300
_ANCHOR_CONFIDENCE = .9
_RESIDUAL = .006
_TEMPLATE = re.compile(
    r"^(?:[【〔\[(]?\s*(?:姓名|班级|年级|日期|学号|考号|学校|座号|成绩|得分|满分|考试时间|注意事项|答题要求)"
    r"|(?:name|class|grade|date|school|student|score|instructions)\b)", re.IGNORECASE,
)


def _text_key(text):
    return ' '.join(normalize('NFC', text).split())


def verified_frame(result, frame_sha256):
    """Only admit trusted engine geometry, bound to the exact OCR bytes and text."""
    lines = result.get('lines')
    if result.get('engine') != 'rapidocr-onnx' or not isinstance(lines, list) or not 0 < len(lines) <= MAX_LINES:
        return None
    try:
        source_text = '\n'.join(line['text'] for line in lines)
        geometry = validate_geometry(result.get('text_geometry'), lines, frame_sha256=frame_sha256, text=source_text)
        size = np.asarray(geometry['frame_size'], dtype=float) - 1
        entries = []
        for line, span in zip(lines, geometry['lines']):
            confidence = line['confidence']
            if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
                return None
            # Store normalized locations from the original decoded frame.
            points = (np.asarray(span['frame_polygon']) / size).clip(0, 1)
            entries.append({'text': line['text'], 'confidence': confidence, 'original_polygon': points.tolist()})
    except (ValueError, TypeError, KeyError, IndexError, OverflowError):
        return None
    return {'frame_sha256': frame_sha256, 'frame_size': size.tolist(), 'geometry_sha256': geometry['geometry_sha256'],
            'lines': entries}


def _scale(frame):
    width, height = frame['frame_size']
    return np.asarray([width, height]) / hypot(width, height)


def _anchor_centers(frame):
    keys = [_text_key(line['text']) for line in frame['lines']]
    counts = Counter(keys)
    scale = _scale(frame)
    return {
        key: np.mean(line['original_polygon'], axis=0) * scale
        for key, line in zip(keys, frame['lines'])
        if counts[key] == 1 and line['confidence'] >= _ANCHOR_CONFIDENCE
        and len({char.casefold() for char in key if char.isalnum()}) >= 8 and not _TEMPLATE.match(key)
    }


def _spread(points, scale, *, min_quadrants=3, span=.25):
    if not len(points):
        return False
    quadrants = {tuple(point > scale / 2) for point in points}
    return len(quadrants) >= min_quadrants and bool(np.all(np.ptp(points, axis=0) >= span * scale))


def qualifies_as_anchor(frame):
    if frame is None:
        return False
    points = np.asarray(list(_anchor_centers(frame).values()))
    return len(points) >= 5 and _spread(points, _scale(frame))


def _alignment(anchor, frame):
    """Fit a near similarity on matched text and test a held-out landmark."""
    anchor_scale = _scale(anchor)
    incoming_scale = _scale(frame)
    if abs((incoming_scale[0] / incoming_scale[1]) / (anchor_scale[0] / anchor_scale[1]) - 1) > .05:
        return None
    fixed, moving = _anchor_centers(anchor), _anchor_centers(frame)
    keys = sorted(fixed.keys() & moving.keys())
    if len(keys) < 5:
        return None
    target = np.asarray([fixed[key] for key in keys], dtype=np.float32)
    source = np.asarray([moving[key] for key in keys], dtype=np.float32)
    # Try each held-out match on small pages. On dense pages sample the
    # fixed coordinate range deterministically so fitting stays bounded.
    holdouts = np.linspace(0, len(keys) - 1, num=min(12, len(keys)), dtype=int)
    for holdout in holdouts:
        selected = np.arange(len(keys)) != holdout
        if not _spread(target[selected], anchor_scale) or not _spread(source[selected], incoming_scale):
            continue
        try:
            matrix, _ = cv2.estimateAffinePartial2D(
                source[selected], target[selected], method=cv2.RANSAC,
                ransacReprojThreshold=_RESIDUAL, maxIters=300, confidence=.99, refineIters=10)
        except cv2.error:
            continue
        if matrix is None or not np.isfinite(matrix).all():
            continue
        scale = hypot(matrix[0, 0], matrix[1, 0])
        rotation = abs(degrees(atan2(matrix[1, 0], matrix[0, 0])))
        if not .9 <= scale <= 1.1 or rotation > 5 or np.linalg.norm(matrix[:, 2]) > .08:
            continue
        predicted = source @ matrix[:, :2].T + matrix[:, 2]
        residuals = np.linalg.norm(predicted - target, axis=1)
        consensus = selected & (residuals <= _RESIDUAL)
        if (residuals[holdout] <= _RESIDUAL and np.count_nonzero(consensus) >= 4
                and np.count_nonzero(residuals > _RESIDUAL) * 5 <= len(keys)
                and _spread(target[consensus], anchor_scale) and _spread(source[consensus], incoming_scale)):
            return matrix
    return None


def _observations(preview_id, frame, *, anchor=None, matrix=None):
    if frame is None:
        return []
    rows = []
    for source in frame['lines']:
        original = np.asarray(source['original_polygon'])
        canonical = original
        if matrix is not None:
            aligned = (original * _scale(frame)) @ matrix[:, :2].T + matrix[:, 2]
            canonical = aligned / _scale(anchor)
            if np.any(canonical < -.006) or np.any(canonical > 1.006):
                return None
            canonical = np.clip(canonical, 0, 1)
            if cv2.contourArea(canonical.astype(np.float32)) <= 0:
                return None
        rows.append({**source, 'canonical_polygon': canonical.tolist(), 'frame_sha256': frame['frame_sha256'],
                     'preview_id': preview_id, 'status': 'current', 'conflict': False})
    return rows


def _boxes(lines):
    polygons = np.asarray([line['canonical_polygon'] for line in lines], dtype=float)
    return polygons.min(axis=1), polygons.max(axis=1)


def _merge(previous, incoming, anchor_id, *, allow_page_reset=True):
    # Compare placement in the fixed original coordinate system. A repeat at
    # another location is a different line. An overlapped contradictory line
    # remains visible as a competing reading.
    if not previous or not incoming:
        return None, 'insufficient_anchors'
    replaced = set()
    older_conflicts = set()
    broad_conflicts = []
    for group in compare_text_overlap(previous, incoming):
        replaced.update(group.replaced_old)
        older_conflicts.update(group.conflicting_old)
        for index in group.conflicting_new:
            incoming[index]['conflict'] = True
        if group.broad_conflict:
            broad_conflicts.extend(group.conflict_centers)
    if allow_page_reset and len(broad_conflicts) >= 3:
        points = np.asarray(broad_conflicts)
        quadrants = {tuple(point > .5) for point in points}
        if len(quadrants) >= 2 and np.ptp(points, axis=0).max() >= .22:
            return None, 'conflicting_page'
    retained = [{**line, 'status': 'historical', 'conflict': line['conflict'] or index in older_conflicts}
                for index, line in enumerate(previous) if index not in replaced]
    combined = incoming + retained
    if len(combined) > MAX_LINES:
        return None, 'line_limit'
    # Repeated capture of identical readable regions does not increase the
    # number of sources. Never evict a unique historical reading silently.
    sources = {anchor_id, *(line['preview_id'] for line in combined)}
    if len(sources) > MAX_SOURCE_FRAMES:
        return None, 'source_limit'
    return combined, 'merged'


def build_page_draft(page_token, preview_id, frame, *, anchor_id=None, anchor=None, previous=None, duplicate=False,
                     visual_anchor=False, visual_matrix=None, visual_conflict=False, registration=None, allow_text_anchor=True):
    current = _observations(preview_id, frame)
    draft = {'page_token': page_token, 'anchor_preview_id': anchor_id, 'frame_count': 1, 'merged': False,
             'reason': 'geometry_unavailable' if frame is None else 'insufficient_anchors', 'lines': current}
    if registration is not None:
        draft['registration'] = registration
    if frame is None:
        return draft
    if anchor is None:
        if (allow_text_anchor and qualifies_as_anchor(frame)) or visual_anchor:
            draft.update(anchor_preview_id=preview_id, reason='anchor_started')
            if visual_anchor:
                draft['alignment_method'] = 'local_orb_v1'
        return draft
    if duplicate:
        return {**draft, 'reason': 'duplicate_frame'}
    if visual_conflict:
        return {**draft, 'anchor_preview_id': preview_id, 'reason': 'conflicting_page',
                'alignment_method': 'local_orb_v1'}
    matrix = visual_matrix if visual_matrix is not None else _alignment(anchor, frame)
    if matrix is None:
        return draft
    aligned = _observations(preview_id, frame, anchor=anchor, matrix=matrix)
    if aligned is None:
        return {**draft, 'reason': 'geometry_outside_anchor'}
    # On a strongly registered source, several corrections are revisions of
    # this reading, not proof that the child physically turned the page.
    lines, reason = _merge(previous['lines'], aligned, anchor_id, allow_page_reset=visual_matrix is None)
    if reason in {'line_limit', 'source_limit'}:
        limited = {**previous, 'reason': reason, 'merged': True,
                   'lines': [{**line, 'status': 'historical'} for line in previous['lines']]}
        if visual_matrix is not None:
            limited.update(alignment_method='local_orb_v1', registration=registration)
        return limited
    if lines is None:
        # The text-only path lacks a checked visual registration; keep its
        # conservative separate-candidate boundary for broad disagreement.
        new_anchor = preview_id if reason == 'conflicting_page' else anchor_id
        result = {**draft, 'anchor_preview_id': new_anchor, 'reason': reason}
        if visual_matrix is not None:
            result['alignment_method'] = 'local_orb_v1'
        return result
    lines.sort(key=lambda line: (
        round(min(point[1] for point in line['canonical_polygon']), 2),
        min(point[0] for point in line['canonical_polygon']),
    ))
    result = {**draft, 'lines': lines, 'frame_count': previous['frame_count'] + 1, 'merged': True, 'reason': reason}
    if visual_matrix is not None:
        result.update(alignment_method='local_orb_v1', registration=registration)
    return result
