"""Local-only, cohort-separated benchmarks for the shared OCR evaluation pack."""
from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from time import perf_counter
from typing import Any

import cv2
import numpy as np

from home_study_agent.local_ocr import LocalOcrService
from home_study_agent.local_ocr.service import _layout_formula_diagnostics


def mendeley_mark_benchmark(root: Path) -> dict:
    """Align paired pages and emit aggregate correction-mark candidates only."""
    raw_root, corrected_root = root / "raw_pages", root / "corrected_pages"
    raw_pages = {path.relative_to(raw_root).as_posix(): path for path in raw_root.rglob("*.jpg")}
    corrected_pages = {path.relative_to(corrected_root).as_posix(): path for path in corrected_root.rglob("*.jpg")}
    keys = sorted(raw_pages.keys() & corrected_pages.keys())
    pages = [_mark_pair(key, raw_pages[key], corrected_pages[key]) for key in keys]
    pdfs = {path.name: _sha256(path) for path in sorted((root / "raw_pdf").glob("*.pdf"))}
    return {
        "cohort": "mendeley_exam_marks",
        "license": "CC BY 4.0 (source stated; review before redistribution)",
        "scope": "English university exams; correction-mark/layout evaluation only, not Chinese K-9 OCR.",
        "paired_page_count": len(pages),
        "missing_raw_or_corrected_pairs": sorted(raw_pages.keys() ^ corrected_pages.keys()),
        "raw_pdf_sha256": pdfs,
        "metrics": {
            "alignment_success_rate": _rate(pages, "alignment_ok", True),
            "pages_with_new_red_ink_candidates": _rate(pages, "has_mark_candidates", True),
            "mark_component_count": sum(page["mark_component_count"] for page in pages),
            "circle_candidate_count": sum(page["shape_candidates"]["circle"] for page in pages),
            "cross_candidate_count": sum(page["shape_candidates"]["cross"] for page in pages),
            "tick_candidate_count": sum(page["shape_candidates"]["tick"] for page in pages),
        },
        "failure_examples": [page["sample_id"] for page in pages if not page["alignment_ok"] or not page["has_mark_candidates"]][:12],
        "candidate_boundary": "Components are change-derived red-ink candidates, not ground-truth tick/cross/circle labels.",
        "annotation_schema": {"sample_id": "raw_pages-relative matching key", "bbox_xywh": "coordinates in raw page pixels", "area_pixels": "component area", "shape_candidate": "heuristic candidate only"},
        "pages": pages,
    }


def mathwriting_benchmark(
    root: Path,
    predictions_path: Path | None = None,
    runtime_report_path: Path | None = None,
) -> dict:
    labels_path = root / "rendered_png" / "labels.jsonl"
    labels = [json.loads(line) for line in labels_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    by_split: dict[str, int] = {}
    for label in labels:
        by_split[label.get("split", "unknown")] = by_split.get(label.get("split", "unknown"), 0) + 1
    report = {
        "cohort": "mathwriting_excerpt",
        "license": "CC BY-NC-SA 4.0 (source stated; evaluation-only boundary)",
        "scope": "500 rendered InkML formulas; not proof of photographed-homework accuracy.",
        "formula_count": len(labels),
        "splits": by_split,
        "metrics": {
            "latex_normalized_exact_match": "not_evaluated_without_local_formula_engine_predictions",
            "math_structure_exact_match": "not_evaluated_without_local_formula_engine_predictions",
        },
        "metric_boundary": "Structure is strict normalized LaTeX token equality, not symbolic mathematical equivalence.",
    }
    if predictions_path is not None and runtime_report_path is not None:
        raise ValueError("choose either local formula predictions or one local formula runtime report")
    if runtime_report_path is not None:
        return _apply_formula_runtime_report(report, runtime_report_path)
    if predictions_path is None:
        return report
    predictions = {item["id"]: str(item.get("latex", "")) for item in _json_lines(predictions_path) if item.get("id")}
    eligible = [item for item in labels if _formula_truth(item)]
    report["metrics"] = _formula_metrics(eligible, predictions)
    report["split_metrics"] = {
        split: _formula_metrics([item for item in eligible if item.get("split", "unknown") == split], predictions)
        for split in sorted({item.get("split", "unknown") for item in eligible})
    }
    return report


def formula_layout_diagnostic_benchmark(mathwriting_root: Path, mendeley_root: Path) -> dict:
    """Measure only visual-heuristic trigger rates on two public cohorts.

    This is deliberately not a formula-recognition or detector-quality metric:
    neither cohort supplies page-formula bounding-box truth for this heuristic.
    """
    labels_path = mathwriting_root / "rendered_png" / "labels.jsonl"
    labels = [json.loads(line) for line in labels_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    math_paths = [Path(item["image"]) for item in labels if isinstance(item.get("image"), str)]
    mendeley_paths = sorted((mendeley_root / "raw_pages").rglob("*.jpg"))
    return {
        "cohort": "formula_layout_heuristic_diagnostics",
        "diagnostic_only": True,
        "unverified": True,
        "metric_boundary": "Trigger counts only, not precision, recall, formula detection accuracy, OCR quality, or review workload.",
        "provider_eligible": False,
        "review_eligible": False,
        "affects_ocr_text": False,
        "affects_mistake_or_knowledge": False,
        "cohorts": {
            "mathwriting_rendered_formulas": _layout_trigger_summary(math_paths),
            "mendeley_raw_exam_pages": _layout_trigger_summary(mendeley_paths),
        },
    }


def _layout_trigger_summary(paths: list[Path]) -> dict:
    pages_read = 0
    pages_with_candidates = 0
    candidate_count = 0
    resized_to_budget = 0
    unreadable = 0
    for path in paths:
        image = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if image is None:
            unreadable += 1
            continue
        pages_read += 1
        diagnostic = _layout_formula_diagnostics(image, [])
        resized_to_budget += diagnostic["resized_to_budget"]
        count = int(diagnostic["candidate_count"])
        candidate_count += count
        pages_with_candidates += bool(count)
    return {
        "input_page_count": len(paths),
        "pages_read": pages_read,
        "unreadable_page_count": unreadable,
        "resized_to_budget_page_count": resized_to_budget,
        "pages_with_candidate_count": pages_with_candidates,
        "candidate_count": candidate_count,
    }


def _apply_formula_runtime_report(report: dict, runtime_report_path: Path) -> dict:
    """Attach only a schema-checked, local formula benchmark summary.

    This prevents an aggregate report from laundering an unrelated provider run
    into MathWriting quality.  The runtime JSON intentionally contributes no
    formula labels, predictions, source paths, or image bytes.
    """
    def reject(reason: str) -> dict:
        report["metrics"] = {
            "latex_normalized_exact_match": "not_evaluated_invalid_local_formula_runtime_report",
            "math_structure_exact_match": "not_evaluated_invalid_local_formula_runtime_report",
        }
        report["runtime_report_validation"] = f"rejected: {reason}"
        return report

    try:
        source = json.loads(runtime_report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as cause:
        return reject(f"unreadable_local_report ({type(cause).__name__})")
    if not isinstance(source, dict):
        return reject("local_report_not_an_object")
    expected_count = report["formula_count"]
    if source.get("cohort") != "mathwriting_excerpt":
        return reject("cohort_mismatch")
    if source.get("formula_count") != expected_count:
        return reject("formula_count_mismatch")
    metrics = source.get("metrics")
    runtime = source.get("provider_runtime")
    required_metrics = {
        "latex_normalized_exact_match",
        "math_structure_exact_match",
        "latex_normalized_cer_mean",
        "latex_token_error_rate_mean",
        "prediction_coverage",
    }
    if not isinstance(metrics, dict) or not isinstance(runtime, dict) or not required_metrics.issubset(metrics):
        return reject("missing_required_metrics_or_runtime")
    if runtime.get("implicit_network_download") is not False:
        return reject("implicit_network_policy_not_proven")
    safe_runtime_keys = {
        "provider", "cpu_only", "checkpoint_bytes", "attempted_formula_count", "failure_count",
        "cold_start_seconds", "latency_p50_seconds", "latency_p95_seconds", "wall_seconds",
        "peak_rss_bytes", "manual_formula_edit_fallback", "formula_specialist", "implicit_network_download",
    }
    safe_metric_keys = required_metrics | {"eligible_formula_count"}
    report["metrics"] = {key: metrics[key] for key in sorted(safe_metric_keys) if key in metrics}
    report["provider_runtime"] = {key: runtime[key] for key in sorted(safe_runtime_keys) if key in runtime}
    if isinstance(source.get("split_metrics"), dict):
        report["split_metrics"] = source["split_metrics"]
    report["runtime_report_validation"] = "accepted_local_formula_runtime_report"
    report["runtime_report_scope"] = source.get("scope", "Not provided by runtime report.")
    return report


def scut_smoke_benchmark(root: Path, service: LocalOcrService) -> dict:
    paths = sorted(root.glob("hccdoc/*")) + sorted(root.glob("ept/*"))
    samples = []
    for path in paths:
        started = perf_counter()
        image = cv2.imread(str(path), cv2.IMREAD_COLOR)
        dimensions = {"width": int(image.shape[1]), "height": int(image.shape[0])} if image is not None else None
        preprocessing = "none"
        if image is not None and min(image.shape[:2]) < 80:
            scale = 80 / min(image.shape[:2])
            image = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
            encoded, png = cv2.imencode(".png", image)
            payload = png.tobytes() if encoded else path.read_bytes()
            preprocessing = "evaluation_only_upscale_to_minimum_80px"
        else:
            payload = path.read_bytes()
        job = service.enqueue(payload, request_id=f"shared-scut:{path.name}")
        completed = service.run(job["job_id"], payload)
        item = {"sample_id": path.relative_to(root).as_posix(), "status": completed["status"], "latency_seconds": round(perf_counter() - started, 3), "source_dimensions": dimensions, "preprocessing": preprocessing}
        if completed["status"] == "completed":
            result = completed["result"]
            item["manual_review_required"] = result["manual_review"]["required"]
            item["quality_issues"] = result["quality"]["issues"]
        else:
            item["failure_code"] = completed.get("error_code")
            if dimensions and min(dimensions.values()) < 80:
                item["failure_boundary"] = "source_below_minimum_80px_dimension"
        samples.append(item)
    return {
        "cohort": "scut_public_samples",
        "license": "non-commercial research/sample-only; full datasets require application.",
        "scope": "Five public Chinese handwriting images for smoke testing only; no Chinese handwriting accuracy claim.",
        "sample_count": len(samples),
        "metrics": {"completed_rate": _rate(samples, "status", "completed"), "ocr_latency_p95_seconds": _p95([item["latency_seconds"] for item in samples])},
        "samples": samples,
    }


def family_camera_status() -> dict:
    return {
        "cohort": "family_document_camera",
        "status": "no_completed_local_page_capture",
        "scope": "Required separate Nanjing K-9/high-camera benchmark; no family images are included in this shared pack.",
        "metrics": {"page_detection": None, "printed_cer": None, "handwriting_field_accuracy": None, "math_structure_accuracy": None, "marking_accuracy": None, "end_to_end_mistake_entry_success_rate": None},
    }


def build_mark_review_template(report: dict[str, Any], *, per_shape: int = 8, failure_page_limit: int = 8) -> dict:
    """Build a small, deterministic human-review packet without copying page pixels.

    Precision cannot be inferred from changed-pixel components.  Reviewers label each
    sampled component and count all real new marks on its page, allowing recall to
    include marks the detector did not propose.
    """
    selected: list[dict] = []
    for shape in ("circle", "cross", "tick"):
        matching = [
            {"sample_id": page["sample_id"], **annotation}
            for page in report.get("pages", [])
            for annotation in page.get("annotations", [])
            if annotation.get("shape_candidate") == shape
        ]
        selected.extend(matching[:per_shape])
    seen = set()
    annotations = []
    for annotation in selected:
        if annotation["annotation_id"] in seen:
            continue
        seen.add(annotation["annotation_id"])
        annotations.append({
            "annotation_id": annotation["annotation_id"],
            "sample_id": annotation["sample_id"],
            "bbox_xywh": annotation["bbox_xywh"],
            "shape_candidate": annotation["shape_candidate"],
            "verdict": "pending",  # true_positive | false_positive
            "ground_truth_mark_id": None,
            "reviewer_note": None,
        })
    pages = sorted({item["sample_id"] for item in annotations} | set(report.get("failure_examples", [])[:failure_page_limit]))
    return {
        "schema_version": 1,
        "status": "pending_manual_review",
        "scope": "Small stratified review only; images remain external/local and are not stored in this file.",
        "instructions": [
            "For every reviewed page, count distinct newly added correction marks after alignment.",
            "For every component choose true_positive or false_positive. A true positive must reference one ground_truth_mark_id.",
            "Use the same ground_truth_mark_id only when components are fragments of the same physical mark; duplicates count once for recall.",
            "Do not use these results as Chinese K-9, handwriting, or formula accuracy.",
        ],
        "pages": [{"sample_id": sample_id, "ground_truth_mark_count": None} for sample_id in pages],
        "annotations": annotations,
    }


def evaluate_mark_review(review: dict[str, Any]) -> dict:
    pages = review.get("pages", [])
    annotations = review.get("annotations", [])
    complete_pages = all(isinstance(page.get("ground_truth_mark_count"), int) and page["ground_truth_mark_count"] >= 0 for page in pages)
    complete_annotations = all(item.get("verdict") in {"true_positive", "false_positive"} for item in annotations)
    unlinked_true_positives = [
        item for item in annotations
        if item.get("verdict") == "true_positive" and not item.get("ground_truth_mark_id")
    ]
    if not complete_pages or not complete_annotations:
        return {
            "status": "pending_manual_review",
            "pending_page_count": sum(not isinstance(page.get("ground_truth_mark_count"), int) for page in pages),
            "pending_annotation_count": sum(item.get("verdict") not in {"true_positive", "false_positive"} for item in annotations),
            "unlinked_true_positive_count": len(unlinked_true_positives),
            "metrics": {"precision": None, "recall": None},
        }
    if unlinked_true_positives:
        return {
            "status": "pending_manual_review",
            "pending_page_count": 0,
            "pending_annotation_count": 0,
            "unlinked_true_positive_count": len(unlinked_true_positives),
            "metrics": {"precision": None, "recall": None},
        }
    positive_ids = {
        str(item.get("ground_truth_mark_id"))
        for item in annotations
        if item.get("verdict") == "true_positive" and item.get("ground_truth_mark_id")
    }
    true_positive = len(positive_ids)
    false_positive = sum(item.get("verdict") == "false_positive" for item in annotations)
    truth_count = sum(page["ground_truth_mark_count"] for page in pages)
    false_negative = max(0, truth_count - true_positive)
    return {
        "status": "complete",
        "metrics": {
            "precision": round(true_positive / (true_positive + false_positive), 4) if true_positive + false_positive else None,
            "recall": round(true_positive / truth_count, 4) if truth_count else None,
            "true_positive": true_positive,
            "false_positive": false_positive,
            "false_negative": false_negative,
        },
        "reviewed_page_count": len(pages),
        "reviewed_component_count": len(annotations),
    }


def render_mark_review_overlays(root: Path, report: dict[str, Any], review: dict[str, Any], output_dir: Path) -> dict:
    """Write local-only raw/corrected/diff overlays for a selected review packet."""
    output_dir.mkdir(parents=True, exist_ok=True)
    selected = {page["sample_id"] for page in review.get("pages", [])}
    by_page: dict[str, list[dict]] = {}
    for annotation in review.get("annotations", []):
        by_page.setdefault(annotation["sample_id"], []).append(annotation)
    page_results = []
    for sample_id in sorted(selected):
        raw = cv2.imread(str(root / "raw_pages" / sample_id), cv2.IMREAD_COLOR)
        corrected = cv2.imread(str(root / "corrected_pages" / sample_id), cv2.IMREAD_COLOR)
        if raw is None or corrected is None:
            page_results.append({"sample_id": sample_id, "status": "source_decode_failed"})
            continue
        if raw.shape != corrected.shape:
            corrected = cv2.resize(corrected, (raw.shape[1], raw.shape[0]), interpolation=cv2.INTER_AREA)
        diff = cv2.absdiff(raw, corrected)
        overlay = corrected.copy()
        for annotation in by_page.get(sample_id, []):
            x, y, width, height = annotation["bbox_xywh"]
            cv2.rectangle(overlay, (x, y), (x + width, y + height), (0, 180, 0), 3)
            cv2.putText(overlay, annotation["annotation_id"].rsplit("#", 1)[-1], (x, max(20, y - 5)), cv2.FONT_HERSHEY_SIMPLEX, .65, (0, 110, 0), 2, cv2.LINE_AA)
        canvas = np.hstack((raw, overlay, diff))
        destination = output_dir / (sample_id.replace("/", "__").rsplit(".", 1)[0] + "__raw_corrected_diff.jpg")
        cv2.imwrite(str(destination), canvas, [cv2.IMWRITE_JPEG_QUALITY, 92])
        crop_paths = []
        for annotation in by_page.get(sample_id, []):
            x, y, width, height = annotation["bbox_xywh"]
            padding = max(24, int(max(width, height) * .8))
            left, top = max(0, x - padding), max(0, y - padding)
            right, bottom = min(raw.shape[1], x + width + padding), min(raw.shape[0], y + height + padding)
            crop = np.hstack((raw[top:bottom, left:right], overlay[top:bottom, left:right], diff[top:bottom, left:right]))
            crop_destination = output_dir / (annotation["annotation_id"].replace("/", "__").replace("#", "__") + "__crop.jpg")
            cv2.imwrite(str(crop_destination), crop, [cv2.IMWRITE_JPEG_QUALITY, 94])
            crop_paths.append(str(crop_destination))
        page_results.append({"sample_id": sample_id, "status": "rendered", "overlay_path": str(destination), "candidate_crop_paths": crop_paths})
    return {"rendered_page_count": sum(item["status"] == "rendered" for item in page_results), "pages": page_results}


def _mark_pair(sample_id: str, raw_path: Path, corrected_path: Path) -> dict:
    raw = cv2.imread(str(raw_path), cv2.IMREAD_COLOR)
    corrected = cv2.imread(str(corrected_path), cv2.IMREAD_COLOR)
    if raw is None or corrected is None:
        return {"sample_id": sample_id, "alignment_ok": False, "alignment_status": "page_decode_failed", "failure": "page_decode_failed", "mark_component_count": 0, "has_mark_candidates": False, "shape_candidates": {"circle": 0, "cross": 0, "tick": 0}, "annotations": []}
    aligned, alignment_status = _align_corrected(raw, corrected)
    alignment_ok = alignment_status in {"ecc_affine", "phase_correlation"}
    red = _new_red_ink_mask(raw, aligned)
    components = _mark_components(red)
    shapes = {"circle": 0, "cross": 0, "tick": 0}
    for component in components:
        shapes[component["shape_candidate"]] += 1
    annotations = [{"annotation_id": f"{sample_id}#{index + 1}", "bbox_xywh": component["bbox_xywh"], "area_pixels": component["area_pixels"], "shape_candidate": component["shape_candidate"]} for index, component in enumerate(components)]
    return {"sample_id": sample_id, "alignment_ok": alignment_ok, "alignment_status": alignment_status, "mark_component_count": len(components), "has_mark_candidates": bool(components), "new_red_ink_pixel_ratio": round(float((red > 0).mean()), 5), "shape_candidates": shapes, "annotations": annotations}


def _align_corrected(raw: np.ndarray, corrected: np.ndarray) -> tuple[np.ndarray, str]:
    if raw.shape != corrected.shape:
        corrected = cv2.resize(corrected, (raw.shape[1], raw.shape[0]), interpolation=cv2.INTER_AREA)
    raw_gray = cv2.cvtColor(raw, cv2.COLOR_BGR2GRAY)
    corrected_gray = cv2.cvtColor(corrected, cv2.COLOR_BGR2GRAY)
    scale = min(1.0, 900 / max(raw.shape[:2]))
    template = cv2.resize(raw_gray, None, fx=scale, fy=scale) if scale < 1 else raw_gray
    moving = cv2.resize(corrected_gray, None, fx=scale, fy=scale) if scale < 1 else corrected_gray
    warp = np.eye(2, 3, dtype=np.float32)
    try:
        cv2.findTransformECC(template, moving, warp, cv2.MOTION_AFFINE, (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 40, 1e-5))
        warp[:, 2] /= scale
        return cv2.warpAffine(corrected, warp, (raw.shape[1], raw.shape[0]), flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP, borderMode=cv2.BORDER_REPLICATE), "ecc_affine"
    except cv2.error:
        shift, response = cv2.phaseCorrelate(np.float32(template), np.float32(moving))
        if response >= .08:
            translation = np.float32([[1, 0, shift[0] / scale], [0, 1, shift[1] / scale]])
            return cv2.warpAffine(corrected, translation, (raw.shape[1], raw.shape[0]), flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP, borderMode=cv2.BORDER_REPLICATE), "phase_correlation"
        return corrected, "resize_only_low_texture"


def _new_red_ink_mask(raw: np.ndarray, corrected: np.ndarray) -> np.ndarray:
    hsv = cv2.cvtColor(corrected, cv2.COLOR_BGR2HSV)
    red = (cv2.inRange(hsv, (0, 55, 65), (16, 255, 255)) > 0) | (cv2.inRange(hsv, (165, 55, 65), (180, 255, 255)) > 0)
    raw_hsv = cv2.cvtColor(raw, cv2.COLOR_BGR2HSV)
    raw_red = (cv2.inRange(raw_hsv, (0, 55, 65), (16, 255, 255)) > 0) | (cv2.inRange(raw_hsv, (165, 55, 65), (180, 255, 255)) > 0)
    mask = np.where(red & ~raw_red, 255, 0).astype(np.uint8)
    return cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))


def _mark_components(mask: np.ndarray) -> list[dict]:
    count, _, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    output = []
    for index in range(1, count):
        x, y, width, height, area = stats[index]
        if area < 18:
            continue
        aspect = width / max(1, height)
        fill = area / max(1, width * height)
        if .72 <= aspect <= 1.35 and fill < .35 and min(width, height) >= 14:
            shape = "circle"
        elif .55 <= aspect <= 1.8 and fill > .32:
            shape = "cross"
        else:
            shape = "tick"
        output.append({"bbox_xywh": [int(x), int(y), int(width), int(height)], "area_pixels": int(area), "shape_candidate": shape})
    return output


def _formula_truth(item: dict) -> str:
    return str(item.get("normalized_label") or item.get("label") or "")


def _normalize_latex(value: str) -> str:
    return "".join(value.split())


def _latex_tokens(value: str) -> list[str]:
    normalized = _normalize_latex(value)
    tokens: list[str] = []
    index = 0
    while index < len(normalized):
        if normalized[index] == "\\":
            end = index + 1
            while end < len(normalized) and normalized[end].isalpha():
                end += 1
            tokens.append(normalized[index:end])
            index = end
        else:
            tokens.append(normalized[index])
            index += 1
    return tokens


def _formula_metrics(items: list[dict], predictions: dict[str, str]) -> dict:
    matched = sum(_normalize_latex(predictions.get(item["id"], "")) == _normalize_latex(_formula_truth(item)) for item in items)
    normalized_cer = [_character_error_rate(_normalize_latex(_formula_truth(item)), _normalize_latex(predictions.get(item["id"], ""))) for item in items]
    token_error = [_token_error_rate(_latex_tokens(_formula_truth(item)), _latex_tokens(predictions.get(item["id"], ""))) for item in items]
    return {
        "latex_normalized_exact_match": round(matched / len(items), 4) if items else None,
        "math_structure_exact_match": round(matched / len(items), 4) if items else None,
        "latex_normalized_cer_mean": round(sum(normalized_cer) / len(normalized_cer), 4) if normalized_cer else None,
        "latex_token_error_rate_mean": round(sum(token_error) / len(token_error), 4) if token_error else None,
        "eligible_formula_count": len(items),
        "prediction_coverage": round(sum(item["id"] in predictions for item in items) / len(items), 4) if items else None,
    }


def _token_error_rate(expected: list[str], actual: list[str]) -> float:
    if not expected:
        return 0.0 if not actual else 1.0
    return _levenshtein(expected, actual) / len(expected)


def _character_error_rate(expected: str, actual: str) -> float:
    if not expected:
        return 0.0 if not actual else 1.0
    return _levenshtein(list(expected), list(actual)) / len(expected)


def _levenshtein(expected: list[str], actual: list[str]) -> int:
    previous = list(range(len(actual) + 1))
    for index, expected_item in enumerate(expected, 1):
        current = [index]
        for actual_index, actual_item in enumerate(actual, 1):
            current.append(min(current[-1] + 1, previous[actual_index] + 1, previous[actual_index - 1] + (expected_item != actual_item)))
        previous = current
    return previous[-1]


def _json_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _rate(items: list[dict], key: str, expected: object) -> float | None:
    observed = [item for item in items if key in item]
    return round(sum(item[key] == expected for item in observed) / len(observed), 4) if observed else None


def _p95(values: list[float]) -> float | None:
    return sorted(values)[max(0, int(len(values) * .95) - 1)] if values else None
