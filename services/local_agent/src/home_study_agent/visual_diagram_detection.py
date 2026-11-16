"""Local candidate discovery; only the separate sealed gate approves crop bytes."""
from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO

import cv2
import numpy as np
from PIL import Image, UnidentifiedImageError

from .document_vision import DiagramRegionGate, DocumentVisionError, MAX_FRAME_PIXELS


@dataclass(frozen=True)
class DiagramCandidates:
    prepared: tuple
    reason: str | None


class MonochromeDiagramDetector:
    def __init__(self, gate=None):
        self.gate = gate or DiagramRegionGate()

    def prepare(self, frame_bytes, *, known_child_names):
        try:
            with Image.open(BytesIO(frame_bytes)) as image:
                if image.width * image.height > MAX_FRAME_PIXELS:
                    return DiagramCandidates((), 'VISUAL_FRAME_TOO_LARGE')
                pixels = cv2.cvtColor(np.array(image.convert('RGB')), cv2.COLOR_RGB2BGR)
        except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
            return DiagramCandidates((), 'VISUAL_FRAME_UNREADABLE')
        gray = cv2.cvtColor(pixels, cv2.COLOR_BGR2GRAY)
        # A closed, sparse, large polygon is a candidate, never a shape fact.
        contours, _ = cv2.findContours((gray < 160).astype(np.uint8), cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        _, labels, stats, _ = cv2.connectedComponentsWithStats((gray < 160).astype(np.uint8), connectivity=8)
        rectangles = []
        height, width = gray.shape
        for contour in contours:
            area = cv2.contourArea(contour)
            x, y, w, h = cv2.boundingRect(contour)
            if min(w, h) < 80 or area < 4096 or area > width * height * .35:
                continue
            polygon = cv2.approxPolyDP(contour, .02 * cv2.arcLength(contour, True), True)
            if not (3 <= len(polygon) <= 6 and cv2.isContourConvex(polygon)):
                continue
            px, py = contour[0, 0]
            component = int(labels[py, px])
            if component:
                # Keep connecting strokes with their figure instead of clipping
                # them at each closed sub-outline (for example, two circuit boxes).
                x, y, w, h = map(int, stats[component, :4])
            if w*h > width*height*.35:
                continue
            if (gray[y:y+h, x:x+w] < 160).mean() > .15:
                continue
            margin = max(16, int(max(w, h) * .08))
            roi = (x-margin, y-margin, x+w+margin, y+h+margin)
            if roi[0] < 0 or roi[1] < 0 or roi[2] > width or roi[3] > height:
                continue
            rectangles.append(roi)
        # Collapse only nested outlines of the same ink component, not nearby figures.
        selected = []
        for roi in sorted(rectangles, key=lambda r: -(r[2]-r[0])*(r[3]-r[1])):
            if any(all(abs(a-b) <= 12 for a, b in zip(roi, prior)) for prior in selected):
                continue
            selected.append(roi)
        if not selected:
            return DiagramCandidates((), 'VISUAL_NO_SUPPORTED_GEOMETRY')
        if len(selected) > 4:
            return DiagramCandidates((), 'VISUAL_CANDIDATES_AMBIGUOUS')
        prepared = []
        for roi in sorted(selected, key=lambda r: (r[1], r[0])):
            try:
                prepared.append(self.gate.prepare_region(frame_bytes, region_xyxy=list(roi),
                    known_child_names=known_child_names))
            except DocumentVisionError as error:
                return DiagramCandidates((), error.code)
        return DiagramCandidates(tuple(prepared), None)
