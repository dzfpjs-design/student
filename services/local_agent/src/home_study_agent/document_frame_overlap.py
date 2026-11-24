"""Stable overlap with the reference image, without changing pose/content gates."""
from math import fsum, isfinite

import numpy as np


def _area(points):
    shifted = np.asarray(points, dtype=float) - points[0]
    return .5 * fsum(float(a[0] * b[1] - a[1] * b[0])
                     for a, b in zip(shifted, np.roll(shifted, -1, axis=0)))


def reference_overlap(projected):
    """Absolute intersection area of a non-reflected frame and the unit square.

    The caller separately checks scale, rotation, translation, image matches,
    spatial holdouts and independent text. Unsupported projections fail closed.
    """
    try:
        polygon = np.asarray(projected, dtype=float)
        if polygon.shape != (4, 2) or not np.isfinite(polygon).all():
            return 0.
        edges = np.roll(polygon, -1, axis=0) - polygon
        following = np.roll(edges, -1, axis=0)
        with np.errstate(over='ignore', invalid='ignore'):
            crosses = edges[:, 0] * following[:, 1] - edges[:, 1] * following[:, 0]
            tolerance = 32 * np.finfo(float).eps * max(1., float(np.abs(edges).max())) ** 2
        if not np.isfinite(crosses).all() or not isfinite(tolerance) or np.any(crosses <= tolerance):
            return 0.
        original_area = _area(polygon)
        if not isfinite(original_area) or original_area <= tolerance:
            return 0.
        points = [p.copy() for p in polygon]
        for axis, boundary, lower in ((0, 0., True), (0, 1., False), (1, 0., True), (1, 1., False)):
            if not points:
                return 0.
            clipped = []
            previous = points[-1]
            previous_inside = previous[axis] >= boundary if lower else previous[axis] <= boundary
            for current in points:
                inside = current[axis] >= boundary if lower else current[axis] <= boundary
                if inside != previous_inside:
                    denominator = float(current[axis] - previous[axis])
                    if not isfinite(denominator) or denominator == 0:
                        return 0.
                    fraction = float(boundary - previous[axis]) / denominator
                    if not isfinite(fraction) or not 0. <= fraction <= 1.:
                        return 0.
                    crossing = previous + fraction * (current - previous)
                    crossing[axis] = boundary
                    if not np.isfinite(crossing).all():
                        return 0.
                    clipped.append(crossing)
                if inside:
                    clipped.append(current)
                previous, previous_inside = current, inside
            points = clipped
        if len(points) < 3:
            return 0.
        area = _area(points)
        allowance = 64 * np.finfo(float).eps * max(1., original_area)
        if not isfinite(area) or area < -allowance or area > min(1., original_area) + allowance:
            return 0.
        return min(1., max(0., area))
    except (TypeError, ValueError, OverflowError):
        return 0.
