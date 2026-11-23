"""Short-lived local image landmarks; only a checked transform leaves this module.

There is no image/descriptor serialization. Landmarks are restricted to OCR ink
neighbourhoods: a stationary desk must never register two different worksheets.
"""
from collections import OrderedDict
from dataclasses import dataclass, field
from hashlib import sha256
from math import atan2, degrees, hypot
from threading import RLock, Timer
from time import monotonic
from weakref import WeakKeyDictionary, WeakSet

import cv2
import numpy as np

from .document_preview_accumulator import _scale, _spread, _text_key, _TEMPLATE
from .document_text_overlap import compare as compare_text_overlap

METHOD = 'local_orb_v1'
EXTRACTION_REASONS = frozenset({'ready', 'geometry', 'sha', 'no_text', 'text_spread', 'shape', 'dark',
                                'keypoints', 'landmark_spread', 'opencv_error'})
_registry = WeakKeyDictionary()
_registry_lock = RLock()


def register_cache(database, cache):
    with _registry_lock:
        _registry.setdefault(database, WeakSet()).add(cache)


def clear_registered_landmarks(database, child_id=None):
    """Privacy/restore boundary, including standalone services on the same DB."""
    with _registry_lock:
        caches = list(_registry.get(database, ()))
    for cache in caches:
        cache.clear(child_id)


@dataclass(frozen=True)
class Landmarks:
    frame_sha256: str
    points: np.ndarray = field(repr=False)
    descriptors: np.ndarray = field(repr=False)


class LandmarkCache:
    """One fixed source, bounded RAM, and five minutes of validated idle time."""
    def __init__(self, *, ttl=300, limit=4, clock=monotonic):
        self._items = OrderedDict()
        self._ttl, self._limit, self._clock = ttl, limit, clock
        self._lock = RLock()
        self._timer = None
        self._timer_generation = 0

    def _schedule(self):
        self._timer_generation += 1
        generation = self._timer_generation
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if self._items:
            delay = max(.001, min(value[3] + self._ttl for value in self._items.values()) - self._clock())
            self._timer = Timer(delay, self._expire, args=(generation,))
            self._timer.daemon = True
            self._timer.start()

    def _expire(self, generation):
        with self._lock:
            if generation != self._timer_generation:
                return
            now = self._clock()
            for key, (_, _, _, at) in list(self._items.items()):
                if now - at >= self._ttl:
                    del self._items[key]
            self._schedule()

    def clear(self, child_id=None):
        with self._lock:
            for scope in list(self._items):
                if child_id is None or scope[1] == child_id:
                    del self._items[scope]
            self._schedule()

    def activate(self, scope):
        with self._lock:
            now = self._clock()
            for key, (_, _, _, at) in list(self._items.items()):
                if key != scope or now - at >= self._ttl:
                    del self._items[key]
            self._schedule()

    def get(self, scope, anchor_id, frame_sha):
        with self._lock:
            self.activate(scope)
            value = self._items.get(scope)
            if value is None or value[:2] != (anchor_id, frame_sha):
                return None
            self._items.move_to_end(scope)
            # Merely looking up an anchor cannot extend its lifetime. The
            # caller refreshes it only after verified registration commits.
            return value[2]

    def touch(self, scope, anchor_id, frame_sha):
        """Renew idle time without replacing the original geometry/descriptors."""
        with self._lock:
            value = self._items.get(scope)
            if value is None or value[:2] != (anchor_id, frame_sha):
                return False
            now = self._clock()
            if now - value[3] >= self._ttl:
                del self._items[scope]
                self._schedule()
                return False
            self._items[scope] = (*value[:3], now)
            self._items.move_to_end(scope)
            self._schedule()
            return True

    def put(self, scope, anchor_id, landmarks):
        with self._lock:
            self.activate(scope)
            self._items[scope] = (anchor_id, landmarks.frame_sha256, landmarks, self._clock())
            self._items.move_to_end(scope)
            while len(self._items) > self._limit:
                self._items.popitem(last=False)
            self._schedule()


def _usable_text(frame):
    return [line for line in frame['lines'] if line['confidence'] >= .9
            and sum(char.isalnum() for char in line['text']) >= 2
            and not _TEMPLATE.match(_text_key(line['text']))]


def _registration_support(points, scale, *, metrics=None):
    """Require broad 2-D support without depending on the full frame centre.

    The existing quarter-frame span is retained on each axis. A hull filling
    at least one quarter of that box therefore covers at least 1/64 of the
    frame. Three occupied local quadrants retain three spatial directions;
    tiny patches, straight lines and two endpoint clusters cannot qualify.
    This checks coordinate support, not physical page identity.
    """
    points, scale = np.asarray(points, dtype=float), np.asarray(scale, dtype=float)
    if (points.ndim != 2 or points.shape[1] != 2 or len(points) < 3 or scale.shape != (2,)
            or not np.isfinite(points).all() or not np.isfinite(scale).all() or np.any(scale <= 0)):
        return False
    normalized = points / scale
    low, high = normalized.min(axis=0), normalized.max(axis=0)
    span = high - low
    box_area = float(np.prod(span))
    hull_area = float(cv2.contourArea(cv2.convexHull(normalized.astype(np.float32))))
    hull_ratio = min(1., max(0., hull_area / box_area)) if box_area > 0 else 0.
    occupied = len({tuple(point > (low + high) / 2) for point in normalized})
    if metrics is not None:
        metrics.update(hull_frame_area=round(hull_area, 6), hull_bbox_ratio=round(hull_ratio, 6), support_cells=occupied)
    return bool(np.all(span >= .25) and hull_ratio >= .25 and occupied >= 3)


def extract_landmarks(image, frame, diagnostics=None):
    """Decode exact validated bytes once; retain only bounded ink descriptors."""
    if diagnostics is not None:
        diagnostics.clear()
        diagnostics.update(method=METHOD, status='unavailable', reason='geometry')

    def report(reason, **counts):
        if diagnostics is not None:
            diagnostics.update(reason=reason, status='ready' if reason == 'ready' else 'unavailable', **counts)

    def spread_counts(points, prefix):
        return {prefix + '_span': np.round(np.ptp(points, axis=0), 4).tolist(),
                prefix + '_quadrants': len({tuple(point > .5) for point in points})}

    try:
        if frame is None:
            return None
        if sha256(image).hexdigest() != frame['frame_sha256']:
            report('sha')
            return None
        lines = _usable_text(frame)
        report('no_text', line_count=len(frame['lines']),
               high_confidence_lines=sum(line['confidence'] >= .9 for line in frame['lines']), usable_lines=len(lines))
        if len(lines) < 6:
            return None
        centers = np.asarray([np.mean(line['original_polygon'], axis=0) for line in lines])
        text_support = {}
        supported = _registration_support(centers, np.ones(2), metrics=text_support)
        report('text_spread', **spread_counts(centers, 'text'), **{'text_' + key: value for key, value in text_support.items()})
        if not supported:
            return None
        gray = cv2.imdecode(np.frombuffer(image, np.uint8), cv2.IMREAD_GRAYSCALE)
        width, height = (int(value) + 1 for value in frame['frame_size'])
        report('shape', expected_frame_size=[width, height],
               decoded_frame_size=[gray.shape[1], gray.shape[0]] if gray is not None else None)
        if gray is None or gray.shape != (height, width):
            return None
        brightness, contrast = float(gray.mean()), float(gray.std())
        report('dark', brightness=round(brightness, 1), contrast=round(contrast, 1))
        if brightness < 20 or contrast < 7:
            return None
        factor = min(1., 1920 / max(width, height))
        if factor < 1:
            gray = cv2.resize(gray, (round(width * factor), round(height * factor)), interpolation=cv2.INTER_AREA)
        h, w = gray.shape
        mask = np.zeros_like(gray)
        for line in lines:
            polygon = np.rint(np.asarray(line['original_polygon']) * (w - 1, h - 1)).astype(np.int32)
            cv2.fillConvexPoly(mask, polygon, 255)
        mask = cv2.dilate(mask, np.ones((5, 5), np.uint8))
        points, descriptors = cv2.ORB_create(nfeatures=2500, edgeThreshold=15, fastThreshold=12).detectAndCompute(gray, mask)
        report('keypoints', raw_keypoints=len(points) if points is not None else 0)
        if descriptors is None or points is None or len(points) < 64:
            return None
        # Bound density as well as total memory: one detailed digit is not a page.
        cells, chosen = {}, []
        for index in sorted(range(len(points)), key=lambda n: -points[n].response):
            cell = (int(points[index].pt[0] * 4 / w), int(points[index].pt[1] * 4 / h))
            if cells.get(cell, 0) < 96:
                cells[cell] = cells.get(cell, 0) + 1
                chosen.append(index)
        locations = np.asarray([points[index].pt for index in chosen], dtype=np.float32) / (w - 1, h - 1)
        landmark_support = {}
        supported = _registration_support(locations, np.ones(2), metrics=landmark_support)
        report('landmark_spread', bounded_keypoints=len(chosen), **spread_counts(locations, 'landmark'),
               **{'landmark_' + key: value for key, value in landmark_support.items()})
        if len(chosen) < 64 or not supported:
            return None
        report('ready')
        return Landmarks(frame['frame_sha256'], locations.astype(np.float32), descriptors[chosen].copy())
    except cv2.error as error:
        # Never serialize OpenCV's message, which can contain input/private text.
        code = getattr(error, 'code', None)
        report('opencv_error', error_code='CV_OPERATION_FAILED',
               opencv_code=code if type(code) is int and -1_000_000 <= code <= 1_000_000 else None)
        return None
    except (ValueError, TypeError, KeyError, IndexError, OverflowError):
        report('geometry')
        return None


def _text_evidence(anchor, frame, matrix, diagnostics=None):
    """Digits may establish identity, but labels and shared form headers cannot."""
    # Single digits/operators may be fragments of one arithmetic reading. They
    # participate in exact split/join checks but cannot alone prove identity.
    fixed = [line for line in anchor['lines'] if line['confidence'] >= .9 and not _TEMPLATE.match(_text_key(line['text']))]
    moving = [line for line in frame['lines'] if line['confidence'] >= .9 and not _TEMPLATE.match(_text_key(line['text']))]
    if diagnostics is not None:
        diagnostics['content_conflict_count'] = 0
    if not fixed or not moving:
        return False, False
    target = np.asarray([line['original_polygon'] for line in fixed])
    original = np.asarray([line['original_polygon'] for line in moving])
    aligned = ((original * _scale(frame)) @ matrix[:, :2].T + matrix[:, 2]) / _scale(anchor)
    previous = [{**line, 'canonical_polygon': polygon.tolist()} for line, polygon in zip(fixed, target)]
    incoming = [{**line, 'canonical_polygon': polygon.tolist()} for line, polygon in zip(moving, aligned)]
    accepted, keys, conflicts = [], set(), []
    for group in compare_text_overlap(previous, incoming):
        if group.identity_key is not None and sum(char.isalnum() for char in group.identity_key) >= 2:
            accepted.append(group.center)
            keys.add(group.identity_key)
        if group.broad_conflict:
            conflicts.extend(group.conflict_centers)
    broad_conflict = (len(conflicts) >= 3 and _spread(np.asarray(conflicts), np.ones(2), min_quadrants=2, span=0)
                      and np.ptp(conflicts, axis=0).max() >= .22)
    consistent = (len(accepted) >= 6 and len(keys) >= 4
                  and len({char for key in keys for char in key if char.isalnum()}) >= 4
                  and _spread(np.asarray(accepted), np.ones(2), min_quadrants=2, span=.20))
    if diagnostics is not None:
        diagnostics['content_conflict_count'] = min(300, len(conflicts))
    return bool(consistent), bool(broad_conflict)


def register(anchor, frame, fixed, moving):
    """Return a checked transform or an unconfirmed-content boundary; no warp."""
    if fixed is None or moving is None:
        evidence = {'method': METHOD, 'status': 'landmarks_unavailable'} if fixed is not None else None
        return None, False, evidence
    if fixed.frame_sha256 != anchor['frame_sha256'] or moving.frame_sha256 != frame['frame_sha256']:
        return None, False, None
    anchor_scale, incoming_scale = _scale(anchor), _scale(frame)
    if abs((incoming_scale[0] / incoming_scale[1]) / (anchor_scale[0] / anchor_scale[1]) - 1) > .05:
        return None, False, None
    try:
        matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
        forward = matcher.knnMatch(moving.descriptors, fixed.descriptors, k=2)
        backward = matcher.knnMatch(fixed.descriptors, moving.descriptors, k=2)
        reverse = {pair[0].queryIdx: pair[0].trainIdx for pair in backward
                   if len(pair) == 2 and pair[0].distance < .72 * pair[1].distance}
        matches = [pair[0] for pair in forward if len(pair) == 2 and pair[0].distance < .72 * pair[1].distance
                   and reverse.get(pair[0].trainIdx) == pair[0].queryIdx]
        if len(matches) < 32:
            return None, False, {'method': METHOD, 'status': 'insufficient_matches', 'matches': len(matches)}
        source = np.asarray([moving.points[m.queryIdx] * incoming_scale for m in matches], dtype=np.float32)
        target = np.asarray([fixed.points[m.trainIdx] * anchor_scale for m in matches], dtype=np.float32)
        matrix, mask = cv2.estimateAffinePartial2D(source, target, method=cv2.RANSAC,
            ransacReprojThreshold=.003, maxIters=1000, confidence=.999, refineIters=10)
        if matrix is None or mask is None or not np.isfinite(matrix).all():
            return None, False, None
        inliers = mask.ravel().astype(bool)
        if inliers.sum() < 24 or inliers.mean() < .65:
            return None, False, {'method': METHOD, 'status': 'insufficient_inliers',
                                 'matches': len(matches), 'inliers': int(inliers.sum())}
        residuals = np.linalg.norm(source @ matrix[:, :2].T + matrix[:, 2] - target, axis=1)[inliers]
        scale = hypot(matrix[0, 0], matrix[1, 0])
        angle = abs(degrees(atan2(matrix[1, 0], matrix[0, 0])))
        if (not .9 <= scale <= 1.1 or angle > 5 or np.linalg.norm(matrix[:, 2]) > .08
                or np.quantile(residuals, .9) > .003 or residuals.max() > .005
                or not _registration_support(source[inliers], incoming_scale)
                or not _registration_support(target[inliers], anchor_scale)):
            return None, False, None
        corners = np.array([[0, 0], [1, 0], [1, 1], [0, 1]], dtype=float)
        transformed = ((corners * incoming_scale) @ matrix[:, :2].T + matrix[:, 2]) / anchor_scale
        intersection, _ = cv2.intersectConvexConvex(corners.astype(np.float32), transformed.astype(np.float32))
        if intersection < .80:
            return None, False, None
        content = {}
        consistent, conflict = _text_evidence(anchor, frame, matrix, diagnostics=content)
        stats = {'method': METHOD, 'anchor_frame_sha256': fixed.frame_sha256,
                 'source_frame_sha256': moving.frame_sha256, 'inliers': int(inliers.sum()),
                 'matches': len(matches), 'p90_residual': round(float(np.quantile(residuals, .9)), 6),
                 'status': 'verified' if consistent else 'conflicting_page' if conflict else 'text_identity_unconfirmed',
                 **content}
        # The geometry and independent unchanged text establish a usable
        # coordinate registration, not immutable page content. Preserve edits
        # as competing readings; physical boundaries belong to the source/page
        # token lifecycle. With no independent agreement, remain separate.
        return (matrix if consistent else None), bool(conflict and not consistent), stats
    except (cv2.error, ValueError, TypeError, OverflowError):
        return None, False, None
