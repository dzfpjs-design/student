"""Run a parent-approved local OCR benchmark without exporting images or text."""
from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
from time import perf_counter

from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.evaluation.ocr_benchmark import character_error_rate
from home_study_agent.local_ocr import LocalOcrService


PRINTED_SCRIPTS = ("chinese", "english", "numeric")
SPECIALIST_CAPABILITIES = {
    "handwriting": "handwriting_field",
    "math": "formula_structure",
    "marking": "correction_mark",
}
GRADE_BAND_TO_PROFILE_GRADE = {"grade4": "四年级", "grade9": "初三"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path, help="Local, uncommitted benchmark manifest")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="Local aggregate JSON; no extracted text is written")
    parser.add_argument("--app-e2e", action="store_true", help="Also verify opted-in corrected cases through an isolated local app database")
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    cases = manifest.get("cases", [])
    if not cases:
        report = {
            "privacy": "Local-only benchmark. Output contains aggregate metrics, status, and no page bytes or extracted text.",
            "status": "awaiting_parent_approved_local_samples",
            "asset_status": manifest.get("asset_status", "No local benchmark cases were supplied."),
            "results": [],
            "family_high_camera": _family_high_camera_strata([]),
            "representative_strata": _representative_strata([]),
            "metrics": {
                "page_detection_rate": None,
                "question_block_exact_rate": None,
                "printed_cer_mean_across_declared_printed_scripts": None,
                "printed_cer_by_script": {script: None for script in PRINTED_SCRIPTS},
                "ocr_latency_p95_seconds": None,
                "handwriting_accuracy": "not_evaluated_without_parent_approved_local_samples",
                "math_structure_accuracy": "not_evaluated_without_parent_approved_local_samples",
                "marking_accuracy": "not_evaluated_without_parent_approved_local_samples",
                "end_to_end_mistake_entry_success_rate": "not_evaluated_without_opted_in_corrected_cases",
                "end_to_end_latency_p95_seconds": None,
            },
        }
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"cases": 0, "status": report["status"], "output": str(args.output)}, ensure_ascii=False))
        return
    service = LocalOcrService.default(args.data_dir / "real-ocr-benchmark.db")
    results: list[dict] = []
    for index, case in enumerate(cases):
        category = case.get("category")
        if category not in {"printed", "handwriting", "math", "marking"}:
            raise SystemExit(f"Unsupported category in case {index}: {category}")
        path = Path(case["path"]).expanduser().resolve()
        if not path.is_file():
            results.append({"case_id": case.get("case_id", str(index)), "category": category, "status": "missing_local_file"})
            continue
        started = perf_counter()
        payload = path.read_bytes()
        job = service.enqueue(payload, request_id=f"real-benchmark:{case.get('case_id', index)}")
        completed = service.run(job["job_id"], payload)
        elapsed = perf_counter() - started
        result = {
            "case_id": case.get("case_id", str(index)),
            "category": category,
            "grade_band": case.get("grade_band", "unspecified"),
            "source_kind": case.get("source_kind", "unspecified"),
            "status": completed["status"],
            "latency_seconds": round(elapsed, 3),
        }
        if category == "printed":
            # Keep character-level CER cohorts independent.  A mixed printed
            # page is deliberately not guessed into one of these three claims.
            script = str(case.get("printed_script", "unspecified")).lower()
            result["printed_script"] = script if script in PRINTED_SCRIPTS else "unspecified"
        if completed["status"] == "completed":
            output = completed["result"]
            result["page_detected"] = "page_not_detected" not in output["quality"]["issues"]
            result["question_block_count"] = len(output["question_blocks"])
            result["question_block_expected"] = case.get("expected_question_count")
            result["question_block_exact"] = result["question_block_expected"] == result["question_block_count"] if result["question_block_expected"] is not None else None
            if category == "printed":
                expected = str(case.get("expected_text", ""))
                actual = "\n".join(line["text"] for line in output["lines"])
                result["printed_cer"] = round(character_error_rate(expected, actual), 4) if expected else None
            else:
                # LocalOcrService is intentionally an ordinary printed OCR
                # pipeline.  It is not evidence for handwriting, formula, or
                # correction-mark accuracy, even if a reviewer has supplied
                # ground-truth counts for this local page.
                result["accuracy"] = "not_evaluated_missing_local_specialist_provider"
                result["required_capability"] = SPECIALIST_CAPABILITIES[category]
                result["manual_review_required"] = True
                _record_specialist_truth_readiness(case, result)
        else:
            result["error_code"] = completed.get("error_code")
        results.append(result)
    if args.app_e2e:
        _run_e2e_cases(cases, results, args.data_dir / "e2e-app")
    summary = {category: [item for item in results if item["category"] == category] for category in ("printed", "handwriting", "math", "marking")}
    e2e = [item for item in results if item.get("end_to_end_attempted")]
    family_high_camera = _family_high_camera_strata(results)
    representative_strata = _representative_strata(results)
    report = {
        "acceptance_status": _acceptance_status(results, e2e),
        "privacy": "Local-only benchmark. Output contains aggregate metrics, status, and no page bytes or extracted text.",
        "results": results,
        "family_high_camera": family_high_camera,
        "representative_strata": representative_strata,
        "metrics": {
            "page_detection_rate": _rate(summary["printed"] + summary["handwriting"] + summary["math"] + summary["marking"], "page_detected", True),
            "question_block_exact_rate": _rate(summary["printed"] + summary["handwriting"] + summary["math"] + summary["marking"], "question_block_exact", True),
            "printed_cer_mean_across_declared_printed_scripts": _mean([item["printed_cer"] for item in summary["printed"] if item.get("printed_cer") is not None]),
            "printed_cer_by_script": {
                script: _mean([item["printed_cer"] for item in summary["printed"] if item.get("printed_script") == script and item.get("printed_cer") is not None])
                for script in PRINTED_SCRIPTS
            },
            "ocr_latency_p95_seconds": _percentile([item["latency_seconds"] for item in results if item.get("latency_seconds") is not None], .95),
            "ocr_failure_buckets": _ocr_failure_buckets(results),
            "handwriting_accuracy": _specialist_status(summary["handwriting"]),
            "math_structure_accuracy": _specialist_status(summary["math"]),
            "marking_accuracy": _specialist_status(summary["marking"]),
            "end_to_end_mistake_entry_success_rate": _rate(e2e, "end_to_end_success", True) if e2e else "not_evaluated_without_opted_in_corrected_cases",
            "end_to_end_latency_p95_seconds": _percentile([item["end_to_end_latency_seconds"] for item in e2e if item.get("end_to_end_latency_seconds") is not None], .95),
            "end_to_end_manual_review_items_created": sum(int(item.get("review_queue_items_created", 0)) for item in e2e),
            "end_to_end_manual_correction_count": sum(item.get("review_action") == "confirm" for item in e2e),
            "end_to_end_manual_skip_count": sum(item.get("review_action") == "skip" for item in e2e),
            "end_to_end_failure_buckets": _failure_buckets(e2e),
            "end_to_end_restart_recovery_rate": _rate(e2e, "restart_weakness_evidence_retained", True) if e2e else "not_evaluated_without_opted_in_corrected_cases",
            "end_to_end_raw_media_zero_retention_rate": _rate(e2e, "raw_media_retained", False) if e2e else "not_evaluated_without_opted_in_corrected_cases",
        },
    }
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"cases": len(results), "output": str(args.output)}, ensure_ascii=False))


def _acceptance_status(results: list[dict], e2e: list[dict]) -> str:
    if any(item.get("status") != "completed" for item in results):
        return "failed"
    if any(item.get("end_to_end_success") is not True for item in e2e):
        return "failed"
    if any(str(item.get("accuracy", "")).startswith("not_evaluated") for item in results):
        return "blocked_missing_specialist_models"
    return "passed"


def _rate(items: list[dict], key: str, expected: object) -> float | None:
    observed = [item for item in items if key in item]
    return round(sum(item[key] == expected for item in observed) / len(observed), 4) if observed else None


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * percentile)))
    return round(ordered[index], 3)


def _failure_buckets(items: list[dict]) -> dict[str, int]:
    """Aggregate application-entry failure stages without IDs, text, or paths."""
    buckets: dict[str, int] = {}
    for item in items:
        if item.get("end_to_end_success") is True:
            continue
        failure = str(item.get("end_to_end_failure") or "unknown")
        bucket = failure.split(":", 1)[0]
        buckets[bucket] = buckets.get(bucket, 0) + 1
    return dict(sorted(buckets.items()))


def _ocr_failure_buckets(items: list[dict]) -> dict[str, int]:
    buckets: dict[str, int] = {}
    for item in items:
        if item.get("status") == "completed":
            continue
        bucket = str(item.get("error_code") or item.get("status") or "unknown")
        buckets[bucket] = buckets.get(bucket, 0) + 1
    return dict(sorted(buckets.items()))


def _specialist_status(items: list[dict]) -> str:
    return (
        "not_evaluated_missing_local_specialist_provider"
        if items else "not_evaluated_without_parent_approved_local_samples"
    )


def _record_specialist_truth_readiness(case: dict, result: dict) -> None:
    """Record only whether local review truth exists; never launder it as model quality."""
    category = result["category"]
    if category in {"handwriting", "math"}:
        review = case.get("manual_field_review")
        if not isinstance(review, dict):
            return
        expected, correct = review.get("expected_fields"), review.get("correct_fields")
        if not isinstance(expected, int) or not isinstance(correct, int) or expected < 1 or not 0 <= correct <= expected:
            result["manual_truth_status"] = "invalid_manual_field_review"
            return
        result["manual_truth_status"] = "available_for_future_specialist_evaluation"
        return
    if category == "marking":
        review = case.get("manual_mark_review")
        if not isinstance(review, dict):
            return
        truth, true_positive, false_positive = review.get("ground_truth_mark_count"), review.get("true_positive_count"), review.get("false_positive_count")
        if not all(isinstance(value, int) and value >= 0 for value in (truth, true_positive, false_positive)) or true_positive > truth:
            result["manual_truth_status"] = "invalid_manual_mark_review"
            return
        result["manual_truth_status"] = "available_for_future_specialist_evaluation"


def _family_high_camera_strata(results: list[dict]) -> dict:
    """Keep the two requested school stages and modalities statistically isolated."""
    report: dict[str, dict] = {}
    for grade in ("grade4", "grade9"):
        items = [item for item in results if item.get("grade_band") == grade and item.get("source_kind") == "high_camera"]
        report[grade] = {
            "case_count": len(items),
            "boundary": "Only explicitly declared high_camera cases. Values are not combined across modalities or grades.",
            "metrics": {
                "printed_cer_by_script": {
                    script: _mean([
                        item["printed_cer"] for item in items
                        if item.get("category") == "printed"
                        and item.get("printed_script") == script
                        and item.get("printed_cer") is not None
                    ])
                    for script in PRINTED_SCRIPTS
                },
                "handwriting_field_accuracy": _specialist_status([item for item in items if item.get("category") == "handwriting"]),
                "math_structure_field_accuracy": _specialist_status([item for item in items if item.get("category") == "math"]),
                "marking_precision": _specialist_status([item for item in items if item.get("category") == "marking"]),
                "marking_recall": _specialist_status([item for item in items if item.get("category") == "marking"]),
            },
        }
    return report


def _representative_strata(results: list[dict]) -> dict:
    """Report every requested local cohort independently, including missing ones.

    A baseline may be useful precisely because it says a cohort or specialist
    is unavailable.  This report never substitutes RapidOCR text CER for any
    specialist capability.
    """
    report: dict[str, dict] = {}
    for grade in ("grade4", "grade9"):
        grade_items = [item for item in results if item.get("grade_band") == grade]
        strata: dict[str, dict] = {}
        for script in PRINTED_SCRIPTS:
            items = [item for item in grade_items if item.get("category") == "printed" and item.get("printed_script") == script]
            values = [item["printed_cer"] for item in items if item.get("printed_cer") is not None]
            strata[f"printed_{script}"] = {
                "sample_count": len(items),
                "status": (
                    "evaluated" if values
                    else "not_evaluated_without_completed_local_cases" if items
                    else "not_evaluated_without_parent_approved_local_samples"
                ),
                "printed_cer_mean": _mean(values),
                "boundary": "Printed-only CER; not handwriting, formula, or marking quality.",
            }
        for category, capability in SPECIALIST_CAPABILITIES.items():
            items = [item for item in grade_items if item.get("category") == category]
            metric_names = {
                "handwriting": ("handwriting_field_accuracy",),
                "math": ("math_structure_accuracy",),
                "marking": ("marking_precision", "marking_recall"),
            }[category]
            strata[capability] = {
                "sample_count": len(items),
                "status": "not_evaluated_missing_local_specialist_provider" if items else "not_evaluated_without_parent_approved_local_samples",
                "metrics": {name: None for name in metric_names},
                "boundary": "Manual truth may be retained locally, but no ordinary OCR result is scored as this specialist capability.",
            }
        report[grade] = strata
    return report


def _run_e2e_cases(cases: list[dict], results: list[dict], data_dir: Path) -> None:
    """Run only manifest-opted-in corrections in a disposable app database.

    The corrected text and knowledge query are read locally and deliberately not
    returned in the aggregate report. This checks workflow persistence, not OCR
    or handwriting/formula recognition quality.
    """
    opted_in = [(case, result) for case, result in zip(cases, results) if case.get("end_to_end")]
    if not opted_in:
        return
    settings = Settings(data_dir=data_dir, database_path=data_dir / "agent.db", data_encryption_key=b"0" * 32, maintenance_enabled=False)
    restart_checks: list[tuple[dict, str, str | None]] = []
    with TestClient(create_app(settings)) as client:
        client.app.state.privacy_lifecycle.update_preferences({
            "raw_media_storage_allowed": False,
            "cloud_model_text_allowed": False,
            "cloud_audio_allowed": False,
            "model_improvement_allowed": False,
        })
        parent_token = client.post("/v1/parent/setup", json={"pin": "2468"}).json()["parent_token"]
        parent_headers = {"X-HSC-Parent": parent_token}
        child_grades = {
            str(child["child_id"]): str(child["grade"])
            for child in client.get("/v1/bootstrap").json().get("children", [])
        }
        for index, (case, result) in enumerate(opted_in):
            result["end_to_end_attempted"] = True
            configured_child_id = str(case["end_to_end"].get("child_id", "child-junior-3"))
            expected_grade = GRADE_BAND_TO_PROFILE_GRADE.get(str(case.get("grade_band", "")))
            if not expected_grade or child_grades.get(configured_child_id) != expected_grade:
                result["end_to_end_success"] = False
                result["end_to_end_failure"] = "grade_band_child_profile_mismatch"
                continue
            if result.get("status") != "completed":
                result["end_to_end_success"] = False
                result["end_to_end_failure"] = "ocr_not_completed"
                continue
            config = case["end_to_end"]
            path = Path(case["path"]).expanduser().resolve()
            started = perf_counter()
            step = "start_session"
            try:
                session_id = _start_study_session(client, configured_child_id, index)
                payload = base64.b64encode(path.read_bytes()).decode("ascii")
                step = "ocr"
                artifact = client.post(f"/v1/sessions/{session_id}/ocr", json={"image_base64": payload, "client_action_id": f"benchmark-e2e-ocr-{index}"})
                artifact.raise_for_status()
                artifact_data = artifact.json()
                review_items = artifact_data.get("ocr_review", {}).get("items", [])
                result["review_queue_items_created"] = len(review_items)
                review_id: str | None = None
                step = "manual_review"
                required_review_action = config.get("review_action")
                if required_review_action and not review_items:
                    raise RuntimeError("required_manual_review_not_created")
                if review_items:
                    review_id = str(review_items[0]["review_id"])
                    action = str(required_review_action or "confirm")
                    step = "review"
                    if action == "confirm":
                        reviewed = client.put(
                            f"/v1/ocr-review-items/{review_id}", headers=parent_headers,
                            json={"child_id": configured_child_id, "corrected_text": str(config["corrected_question_text"]),
                                  "client_action_id": f"benchmark-e2e-review-confirm-{index}"},
                        )
                    elif action == "skip":
                        reviewed = client.post(
                            f"/v1/ocr-review-items/{review_id}/skip", headers=parent_headers,
                            json={"child_id": configured_child_id, "client_action_id": f"benchmark-e2e-review-skip-{index}"},
                        )
                    else:
                        raise ValueError("invalid_review_action")
                    reviewed.raise_for_status()
                    result["review_action"] = action
                step = "prepare_candidate"
                candidates = client.post(f"/v1/capture-artifacts/{artifact_data['artifact_id']}/mistake-candidates/prepare", json={"child_id": config.get("child_id", "child-junior-3"), "client_action_id": f"benchmark-e2e-prepare-{index}"})
                candidates.raise_for_status()
                items = candidates.json().get("items", [])
                if not items:
                    raise RuntimeError("no_mistake_candidate")
                candidate_id = items[0]["candidate_id"]
                corrected = str(config["corrected_question_text"])
                step = "save_correction"
                edited = client.put(f"/v1/mistake-capture-candidates/{candidate_id}", json={"child_id": config.get("child_id", "child-junior-3"), "question_text": corrected, "client_action_id": f"benchmark-e2e-edit-{index}"})
                edited.raise_for_status()
                subject = str(config.get("subject", "数学"))
                step = "find_knowledge"
                knowledge = client.get(f"/v1/children/{config.get('child_id', 'child-junior-3')}/mistake-knowledge-candidates", params={"q": str(config["knowledge_query"]), "subject": subject})
                knowledge.raise_for_status()
                entries = knowledge.json().get("items", [])
                if not entries:
                    raise RuntimeError("no_knowledge_candidate")
                step = "capture_mistake"
                saved = client.post(f"/v1/mistake-capture-candidates/{candidate_id}/capture", json={"child_id": config.get("child_id", "child-junior-3"), "knowledge_node_id": entries[0]["knowledge_node_id"], "subject": subject, "title": entries[0]["title"], "submitted_answer": str(config.get("submitted_answer", "待人工确认")), "client_action_id": f"benchmark-e2e-capture-{index}"})
                saved.raise_for_status()
                result["end_to_end_success"] = saved.json().get("mistake", {}).get("status") == "awaiting_confirmation"
                step = "finish_session"
                result["session_completion_persisted"] = _finish_benchmark_session(client, session_id, index)
                with client.app.state.repository.database.connect() as db:
                    row = db.execute("SELECT local_path, extracted_text FROM capture_artifact WHERE artifact_id = ?", (artifact_data["artifact_id"],)).fetchone()
                result["raw_media_retained"] = bool(row and row["local_path"])
                result["derived_ocr_evidence_retained"] = bool(row and row["extracted_text"])
                restart_checks.append((result, configured_child_id, review_id))
            except Exception as error:  # aggregate only: no source text or bytes leave this runner
                result["end_to_end_success"] = False
                result["end_to_end_failure"] = f"{step}:{type(error).__name__}"
            result["end_to_end_latency_seconds"] = round(perf_counter() - started, 3)
    with TestClient(create_app(settings)) as restarted:
        token = restarted.post("/v1/parent/unlock", json={"pin": "2468"}).json()["parent_token"]
        headers = {"X-HSC-Parent": token}
        for result, child_id, review_id in restart_checks:
            history = restarted.get(f"/v1/children/{child_id}/ocr-review-history", headers=headers)
            weaknesses = restarted.get(f"/v1/children/{child_id}/weakness-clusters", headers=headers)
            history.raise_for_status(); weaknesses.raise_for_status()
            result["restart_review_action_retained"] = (
                review_id is None or any(item.get("review_id") == review_id for item in history.json().get("items", []))
            )
            result["restart_weakness_evidence_retained"] = bool(weaknesses.json().get("items", []))


def _finish_benchmark_session(client: TestClient, session_id: str, index: int) -> bool:
    """Close one evaluated session through the ordinary child workflow.

    This avoids opening competing sessions for the same child when a local
    manifest contains several opted-in pages.  It is deliberately not a direct
    database mutation: the report and its local-only evidence remain part of
    the same durable user journey being measured.
    """
    workspace = client.get(f"/v1/sessions/{session_id}")
    workspace.raise_for_status()
    items = workspace.json().get("plan_items", [])
    if not items:
        raise RuntimeError("no_plan_item_to_complete")
    item_id = items[0]["plan_item_id"]
    started = client.post(f"/v1/plan-items/{item_id}/start", json={"client_action_id": f"benchmark-e2e-item-start-{index}"})
    started.raise_for_status()
    completed = client.post(f"/v1/plan-items/{item_id}/complete", json={
        "actual_minutes": 1, "completion_evidence_type": "child_confirmed", "completion_note": None,
        "client_action_id": f"benchmark-e2e-item-complete-{index}",
    })
    completed.raise_for_status()
    reflection = client.post(f"/v1/sessions/{session_id}/transition", json={
        "expected_state": "study", "target_state": "reflection", "reason": "benchmark_complete",
        "client_action_id": f"benchmark-e2e-reflection-transition-{index}",
    })
    reflection.raise_for_status()
    finished = client.post(f"/v1/sessions/{session_id}/reflection/complete", json={
        "learned_text": "本地评测已完成", "difficult_text": "", "feeling": "",
        "next_action": "下次继续本机复测", "client_action_id": f"benchmark-e2e-reflection-complete-{index}",
    })
    finished.raise_for_status()
    return bool(finished.json().get("daily_report_ready"))


def _start_study_session(client: TestClient, child_id: str, index: int) -> str:
    session = client.post("/v1/sessions", json={"child_id": child_id, "client_action_id": f"benchmark-e2e-session-{index}"})
    session.raise_for_status()
    session_id = session.json()["session_id"]
    transitions = [
        ("check_in", "classroom_recall", "benchmark-e2e-transition-a"),
        ("classroom_recall", "homework_capture", "benchmark-e2e-transition-b"),
        ("plan_review", "study", "benchmark-e2e-transition-c"),
    ]
    client.post(f"/v1/sessions/{session_id}/transition", json={"expected_state": transitions[0][0], "target_state": transitions[0][1], "reason": "benchmark", "client_action_id": f"{transitions[0][2]}-{index}"}).raise_for_status()
    client.post(f"/v1/sessions/{session_id}/recalls", json={"subject": "数学", "summary": "本地评测。", "client_action_id": f"benchmark-e2e-recall-{index}"}).raise_for_status()
    client.post(f"/v1/sessions/{session_id}/transition", json={"expected_state": transitions[1][0], "target_state": transitions[1][1], "reason": "benchmark", "client_action_id": f"{transitions[1][2]}-{index}"}).raise_for_status()
    client.post(f"/v1/sessions/{session_id}/homework", json={"subject": "数学", "title": "本地评测", "expected_minutes": 10, "client_action_id": f"benchmark-e2e-homework-{index}"}).raise_for_status()
    client.post(f"/v1/sessions/{session_id}/plan/generate", json={"client_action_id": f"benchmark-e2e-plan-{index}"}).raise_for_status()
    client.post(f"/v1/sessions/{session_id}/transition", json={"expected_state": transitions[2][0], "target_state": transitions[2][1], "reason": "benchmark", "client_action_id": f"{transitions[2][2]}-{index}"}).raise_for_status()
    return session_id


if __name__ == "__main__":
    main()
