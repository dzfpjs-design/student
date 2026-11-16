from pathlib import Path

from home_study_agent.local_ocr.review_queue import OcrReviewQueue


def artifact() -> dict:
    return {
        "artifact_id": "artifact-review-001", "confidence": .42, "extracted_text": "x=2",
        "details": {"lines": [{"text": "x=2", "confidence": .42, "box": [[1, 2], [30, 2], [30, 18], [1, 18]]}],
                    "local_pipeline": {"manual_review": {"required": True, "handwriting": "unsupported", "math": "formula candidates require human formula editing", "reasons": ["low_ocr_confidence", "formula_candidate_requires_manual_edit"]},
                                       "formula_routing": {"regions": [{"bbox_xywh": [1, 2, 29, 16], "status": "manual_edit_required", "candidate_latex": "x=2", "source_text_confidence": .42}]}}},
    }


def test_local_review_edits_skip_undo_persist_and_become_truth(tmp_path: Path) -> None:
    queue = OcrReviewQueue(tmp_path / "ocr-review.db")
    prepared = queue.prepare(artifact(), "child-1", "session-1", "prepare-001")["items"]
    assert {item["review_kind"] for item in prepared} == {"text", "handwriting", "formula"}
    assert all(item["raw_media_retained"] is False for item in prepared)
    handwriting = next(item for item in prepared if item["review_kind"] == "handwriting")
    changed = queue.update(handwriting["review_id"], "child-1", "x=3", "edit-001")
    assert changed["status"] == "confirmed" and changed["corrected_text"] == "x=3"
    formula = next(item for item in prepared if item["review_kind"] == "formula")
    assert queue.skip(formula["review_id"], "child-1", "skip-001")["status"] == "skipped"
    assert queue.undo(formula["review_id"], "child-1", "undo-001")["status"] == "pending"
    restarted = OcrReviewQueue(tmp_path / "ocr-review.db")
    items = restarted.list("artifact-review-001", "child-1")["items"]
    assert next(item for item in items if item["review_id"] == handwriting["review_id"])["corrected_text"] == "x=3"
    assert restarted.recent("child-1")["items"][0]["review_id"] == handwriting["review_id"]
    summary = restarted.truth_summary("child-1")
    assert summary["items"] == [{"review_kind": "handwriting", "reviewed": 1, "exact_matches": 0}]
    assert restarted.update(handwriting["review_id"], "child-1", "x=3", "edit-001")["status"] == "confirmed"
    try:
        restarted.update(handwriting["review_id"], "child-1", "x=4", "edit-001")
    except ValueError as error:
        assert "内容不一致" in str(error)
    else:
        raise AssertionError("same action ID accepted a different correction")
    try:
        restarted.update(handwriting["review_id"], "child-1", "   ", "blank-edit-001")
    except ValueError as error:
        assert "不能为空" in str(error)
    else:
        raise AssertionError("blank correction was accepted")


def test_review_queue_releases_database_file_handles_between_operations(tmp_path: Path) -> None:
    database_path = tmp_path / "review.db"
    queue = OcrReviewQueue(database_path)
    queue.pending("child-1")

    moved_path = tmp_path / "review-moved.db"
    database_path.replace(moved_path)
    moved_path.replace(database_path)


def test_low_confidence_printed_text_is_bounded_and_never_mislabeled_as_handwriting(tmp_path: Path) -> None:
    queue = OcrReviewQueue(tmp_path / "ocr-review.db")
    lines = [
        {"text": f"printed {index}", "confidence": .3 + index / 100, "box": [[index, 1], [index + 2, 1]]}
        for index in range(20)
    ]
    artifact = {
        "artifact_id": "printed-low-confidence", "confidence": .4, "extracted_text": "printed",
        "details": {"lines": lines, "local_pipeline": {"manual_review": {
            "required": True, "handwriting": "not inferred; add/edit fields manually unless a local handwriting engine is installed",
            "reasons": ["low_ocr_confidence"],
        }}},
    }

    items = queue.prepare(artifact, "child-1", "session-1", "prepare-printed")["items"]

    assert len(items) == 12
    assert {item["review_kind"] for item in items} == {"text"}
    assert all(item["confidence"] < .78 for item in items)


def test_blurred_quality_reason_never_becomes_a_red_marking_review(tmp_path: Path) -> None:
    queue = OcrReviewQueue(tmp_path / "ocr-review.db")
    artifact = {
        "artifact_id": "blurred-page", "confidence": .9, "extracted_text": "ordinary printed text",
        "details": {"lines": [{"text": "ordinary printed text", "confidence": .9, "box": None}], "local_pipeline": {"manual_review": {
            "required": True, "handwriting": "not inferred", "marking": "not detected", "reasons": ["blurred"],
        }}},
    }

    assert queue.prepare(artifact, "child-1", "session-1", "prepare-blurred") == {"items": []}


def test_unvalidated_layout_formula_candidates_do_not_create_parent_review_work(tmp_path: Path) -> None:
    queue = OcrReviewQueue(tmp_path / "ocr-review.db")
    item = artifact()
    item["details"]["local_pipeline"]["formula_routing"] = {
        "regions": [{
            "bbox_xywh": [1, 2, 29, 16],
            "status": "manual_edit_required",
            "review_eligible": False,
        }],
    }
    item["details"]["local_pipeline"]["manual_review"] = {"handwriting": "not_detected"}

    prepared = queue.prepare(item, "child-1", "session-1", "layout-prepare-001")

    assert prepared["items"] == []


def test_batch_confirmation_is_atomic_idempotent_and_restart_durable(tmp_path: Path) -> None:
    path = tmp_path / "ocr-review-batch.db"
    queue = OcrReviewQueue(path)
    prepared = queue.prepare(artifact(), "child-1", "session-1", "prepare-batch")['items']
    review_ids = [item["review_id"] for item in prepared[:2]]

    first = queue.batch(review_ids, "child-1", "confirm_proposed", "batch-action-001")
    replay = OcrReviewQueue(path).batch(
        review_ids, "child-1", "confirm_proposed", "batch-action-001"
    )

    assert first == replay
    assert first["processed_count"] == 2
    assert first["remaining_count"] == len(prepared) - 2
    assert all(item["status"] == "confirmed" for item in first["items"])
    assert all(item["corrected_text"] == item["proposed_text"] for item in first["items"])
    history = OcrReviewQueue(path).recent("child-1", 10)["items"]
    assert {item["review_id"] for item in history} >= set(review_ids)

    try:
        queue.batch(review_ids, "child-1", "skip", "batch-action-001")
    except ValueError as error:
        assert "内容不一致" in str(error)
    else:
        raise AssertionError("same batch action accepted a conflicting decision")


def test_batch_rejects_mixed_valid_and_unknown_rows_without_partial_update(tmp_path: Path) -> None:
    queue = OcrReviewQueue(tmp_path / "ocr-review-batch-atomic.db")
    prepared = queue.prepare(artifact(), "child-1", "session-1", "prepare-batch-atomic")['items']
    review_id = prepared[0]["review_id"]

    try:
        queue.batch([review_id, "missing-review"], "child-1", "skip", "batch-action-atomic")
    except KeyError:
        pass
    else:
        raise AssertionError("batch containing an unknown row was accepted")

    still_pending = queue.pending("child-1")["items"]
    assert any(item["review_id"] == review_id for item in still_pending)
