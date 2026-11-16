"""Print numeric page-gate diagnostics only; never print OCR text or save derivatives."""
from __future__ import annotations

import json
import sys

import cv2
import numpy as np

from home_study_agent.document_vision import MAX_UPLOAD_PIXELS
from home_study_agent.local_ocr.service import RapidOcrEngine, _prepare_page, _rotate_page


image = cv2.imread(sys.argv[1], cv2.IMREAD_COLOR)
if image is None:
    raise SystemExit("FRAME_DECODE_FAILED")
degrees = int(sys.argv[2]) if len(sys.argv) > 2 else 0
mirrored = (sys.argv[3].lower() == "true") if len(sys.argv) > 3 else False
prepared, preparation, _ = _prepare_page(image)
if mirrored:
    prepared = cv2.flip(prepared, 1)
page = _rotate_page(prepared, degrees)
scale = min(1.0, 1400 / max(page.shape[:2]))
if scale < 1:
    page = cv2.resize(page, None, fx=scale, fy=scale)
if page.shape[0] * page.shape[1] > MAX_UPLOAD_PIXELS:
    factor = (MAX_UPLOAD_PIXELS / (page.shape[0] * page.shape[1])) ** .5
    page = cv2.resize(page, (max(256, int(page.shape[1] * factor)),
                             max(256, int(page.shape[0] * factor))), interpolation=cv2.INTER_AREA)
gray = cv2.cvtColor(page, cv2.COLOR_BGR2GRAY)
saturation = cv2.cvtColor(page, cv2.COLOR_BGR2HSV)[:, :, 1]
lines = RapidOcrEngine().recognize(page, "cpu")
meaningful = [line for line in lines if str(line.get("text", "")).strip()
              and float(line.get("confidence", 0)) >= .05]
mask = np.zeros_like(gray)
accepted = 0
for line in meaningful:
    polygon = np.asarray(line.get("box"), dtype=np.float32)
    if polygon.shape != (4, 2) or not np.isfinite(polygon).all():
        continue
    x, y, width, height = cv2.boundingRect(polygon)
    if (x < 0 or y < 0 or x + width > gray.shape[1] or y + height > gray.shape[0]
            or height > gray.shape[0] * .14 or width > gray.shape[1] * .98):
        continue
    polygon_mask = np.zeros_like(gray)
    cv2.fillConvexPoly(polygon_mask, polygon.astype(np.int32), 255)
    region = gray[y:y + height, x:x + width]
    region_saturation = saturation[y:y + height, x:x + width]
    if np.median(region) < 105 or (region_saturation > 80).mean() > .25:
        continue
    _, local_ink = cv2.threshold(region, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    local_ink = cv2.bitwise_and(local_ink, polygon_mask[y:y + height, x:x + width])
    if not .001 <= (local_ink > 0).mean() <= .55:
        continue
    mask = cv2.bitwise_or(mask, polygon_mask)
    accepted += 1
def stats_for(ink):
    count, _, component_stats, _ = cv2.connectedComponentsWithStats(ink)
    coverage = float(((ink > 0) & (mask > 0)).sum() / max(1, (ink > 0).sum()))
    return {
        "ink_ratio": round(float((ink > 0).mean()), 6),
        "ocr_ink_coverage": round(coverage, 6),
        "component_count": count - 1,
        "max_component_width_ratio": round(float(component_stats[1:, cv2.CC_STAT_WIDTH].max() / gray.shape[1]), 6),
        "max_component_height_ratio": round(float(component_stats[1:, cv2.CC_STAT_HEIGHT].max() / gray.shape[0]), 6),
        "max_component_area_ratio": round(float(component_stats[1:, cv2.CC_STAT_AREA].max() / gray.size), 6),
    }

_, otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
fixed = cv2.inRange(gray, 0, 160)
adaptive = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                 cv2.THRESH_BINARY_INV, 35, 15)
print(json.dumps({
    "perspective_correction": preparation.get("transforms", {}).get("perspective_correction"),
    "page_ratio": preparation.get("metrics", {}).get("page_ratio"),
    "page_width": page.shape[1],
    "page_height": page.shape[0],
    "meaningful_line_count": len(meaningful),
    "accepted_line_count": accepted,
    "thresholds": {"otsu": stats_for(otsu), "fixed_160": stats_for(fixed),
                   "adaptive_35_15": stats_for(adaptive)},
}, ensure_ascii=False))
