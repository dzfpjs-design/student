"""Compare physical OCR regions, including exact arithmetic split/join readings.

Only arithmetic operator whitespace is optional. Digits, operators, word
boundaries and unobserved fragments are never supplied or corrected here.
"""
from dataclasses import dataclass
import re
from unicodedata import normalize

import numpy as np


_MATH = re.compile(r'^[\d\s+\-−×*÷/=<>≤≥≠:：.．,，()（）\[\]{}%％²³]+$')
_OPERATOR_SPACE = re.compile(r'\s*([+\-−×*÷/=<>≤≥≠:：])\s*')
_OPERATORS = frozenset('+-−×*÷/=<>≤≥≠:：')


def text_key(text):
    key = ' '.join(normalize('NFC', text).split())
    return _OPERATOR_SPACE.sub(r'\1', key) if _MATH.fullmatch(key) else key


def _bounds(lines):
    polygons = np.asarray([line['canonical_polygon'] for line in lines], dtype=float)
    return polygons.min(axis=1), polygons.max(axis=1)


def _extent(low, high, indices):
    return low[indices].min(axis=0), high[indices].max(axis=0)


def _complete_extent(old, new):
    overlap = np.maximum(np.minimum(old[1], new[1]) - np.maximum(old[0], new[0]), 0)
    larger = np.maximum(old[1] - old[0], new[1] - new[0])
    return bool(np.all(overlap >= larger * (.8, .5)))


def _math_reading(lines, low, high, indices):
    """Only one horizontally ordered line can be an exact arithmetic tiling."""
    if not all(_MATH.fullmatch(' '.join(normalize('NFC', lines[n]['text']).split())) for n in indices):
        return None
    ordered = sorted(indices, key=lambda n: (low[n, 0], low[n, 1]))
    if len(ordered) > 1:
        heights = high[ordered, 1] - low[ordered, 1]
        centers = (high[ordered, 1] + low[ordered, 1]) / 2
        if np.ptp(centers) > np.median(heights) * .5:
            return None
        for left, right in zip(ordered, ordered[1:]):
            widths = high[[left, right], 0] - low[[left, right], 0]
            # Large overlap is an alternative reading, not adjacent fragments.
            if high[left, 0] - low[right, 0] > min(widths) * .25:
                return None
    pieces = [' '.join(normalize('NFC', lines[n]['text']).split()) for n in ordered]
    joined = pieces[0]
    for piece in pieces[1:]:
        # A detector boundary between digits cannot prove the absence of a
        # space. Only an explicitly observed operator permits a tight join.
        separator = '' if joined[-1] in _OPERATORS or piece[0] in _OPERATORS else ' '
        joined += separator + piece
    return text_key(joined)


def _complete_equation(text):
    key = text_key(text)
    if not _MATH.fullmatch(key) or key.count('=') > 1:
        return False
    number = r'[+\-−]?\d+(?:[.．]\d+)?'
    expression = number + r'(?:[+\-−×*÷/]' + number + r')*'
    if '=' in key:
        left, right = key.split('=')
        return bool(re.fullmatch(expression, left) and re.fullmatch(expression, right))
    return bool(re.fullmatch(expression, key) and re.search(r'\d[+\-−×*÷/]'+number, key))


@dataclass(frozen=True)
class Overlap:
    replaced_old: frozenset
    conflicting_old: frozenset
    conflicting_new: frozenset
    identity_key: object
    center: tuple
    broad_conflict: bool
    conflict_centers: tuple
    matched_new: frozenset


def compare(previous, incoming):
    """Return one decision per bipartite physical-overlap component."""
    if not previous or not incoming:
        return []
    old_low, old_high = _bounds(previous)
    new_low, new_high = _bounds(incoming)
    intersection = np.prod(np.maximum(np.minimum(new_high[:, None], old_high) -
                                      np.maximum(new_low[:, None], old_low), 0), axis=2)
    smaller = np.minimum(np.prod(new_high - new_low, axis=1)[:, None], np.prod(old_high - old_low, axis=1))
    overlaps = intersection >= .52 * smaller
    old_keys, new_keys = [text_key(line['text']) for line in previous], [text_key(line['text']) for line in incoming]
    parent = list(range(len(previous) + len(incoming)))
    def find(n):
        while parent[n] != n:
            parent[n] = parent[parent[n]]
            n = parent[n]
        return n
    for new, old in zip(*np.nonzero(overlaps)):
        parent[find(len(previous) + int(new))] = find(int(old))
    components = {}
    for new, old in zip(*np.nonzero(overlaps)):
        old_set, new_set = components.setdefault(find(int(old)), (set(), set()))
        old_set.add(int(old))
        new_set.add(int(new))
    decisions = []
    for old_set, new_set in components.values():
        old_indices, new_indices = sorted(old_set), sorted(new_set)
        replaced, matched_new = set(), set()
        for new in new_indices:
            same = [old for old in old_indices if overlaps[new, old] and new_keys[new] == old_keys[old] and old not in replaced]
            if same:
                replaced.add(max(same, key=lambda old: intersection[new, old]))
                matched_new.add(new)
        identity = None
        new_math = _math_reading(incoming, new_low, new_high, new_indices)
        # Historical alternatives from different source frames must never be
        # concatenated into an invented old equation.
        sources = {}
        for old in old_indices:
            sources.setdefault(previous[old].get('preview_id'), []).append(old)
        if new_math is not None:
            for indices in sources.values():
                if (_math_reading(previous, old_low, old_high, indices) == new_math
                        and _complete_extent(_extent(old_low, old_high, indices), _extent(new_low, new_high, new_indices))):
                    replaced.update(indices)
                    matched_new.update(new_indices)
                    identity = new_math
        if identity is None and len(new_indices) == 1 and matched_new:
            identity = new_keys[new_indices[0]]
        conflicts = old_set - replaced
        conflicting_new = {new for new in new_indices if any(overlaps[new, old] for old in conflicts)}
        old_extent, new_extent = _extent(old_low, old_high, old_indices), _extent(new_low, new_high, new_indices)
        center = (np.minimum(old_extent[0], new_extent[0]) + np.maximum(old_extent[1], new_extent[1])) / 2
        reliable = [new for new in sorted(conflicting_new) if incoming[new]['confidence'] >= .9
                    and any(previous[old]['confidence'] >= .9 and overlaps[new, old] for old in conflicts)]
        math_only = all(_MATH.fullmatch(' '.join(normalize('NFC', line['text']).split()))
                        for line in [*(previous[n] for n in old_indices), *(incoming[n] for n in new_indices)])
        conflict_centers = []
        if math_only:
            equations = [new for new in reliable if _complete_equation(incoming[new]['text'])]
            # A wide old detector box must not hide several independent, fully
            # observed changed equations. Numeric fragments are not equations.
            independent = []
            for new in equations:
                if all(np.any(np.minimum(new_high[new], new_high[other]) <= np.maximum(new_low[new], new_low[other]))
                       for other in independent):
                    independent.append(new)
            if len(independent) > 1:
                conflict_centers = [tuple((new_low[n] + new_high[n]) / 2) for n in independent]
            elif reliable and _complete_extent(old_extent, new_extent):
                conflict_centers = [tuple(center)]
        else:
            # Preserve word/line semantics outside arithmetic: no concatenation
            # and the existing per-observed-region contradiction accounting.
            conflict_centers = [tuple((new_low[n] + new_high[n]) / 2) for n in reliable]
        decisions.append(Overlap(frozenset(replaced), frozenset(conflicts), frozenset(conflicting_new),
                                 identity, tuple(center), bool(conflict_centers), tuple(conflict_centers), frozenset(matched_new)))
    return decisions
