from __future__ import annotations

import json

from home_study_agent.document_monitoring import DocumentMonitoringService

from test_answer_attempt_ledger import setup_case


def legacy_details(database, *, trusted_engine: bool) -> None:
    with database.connect() as connection:
        row = connection.execute(
            "SELECT details_json FROM capture_artifact WHERE artifact_id='page'"
        ).fetchone()
        details = json.loads(row[0])
        details.pop("cloud_vision_fallback", None)
        pipeline = details["local_pipeline"]
        pipeline["engine"] = "rapidocr-onnx" if trusted_engine else "unknown-legacy-engine"
        pipeline["route"] = "cpu"
        for block in pipeline["question_blocks"]:
            block.pop("block_kind", None)
            block["line_count"] = 3
        extracted = "\n".join(block["text"] for block in pipeline["question_blocks"])
        connection.execute(
            "UPDATE capture_artifact SET details_json=?,extracted_text=? WHERE artifact_id='page'",
            (json.dumps(details, ensure_ascii=False), extracted),
        )
        connection.commit()


def test_known_legacy_rapidocr_question_block_replays_without_raw_image(tmp_path) -> None:
    database, _ = setup_case(tmp_path)
    legacy_details(database, trusted_engine=True)
    monitor = DocumentMonitoringService(database)
    monitor.start("study", "legacy-monitor-start")
    result = monitor.record_revision("study", "page", "legacy-signature", "legacy-revision")
    assert result["observed_question_count"] == 1
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM observed_question").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM mistake_capture_candidate").fetchone()[0] == 1


def test_unknown_missing_block_kind_contract_is_not_promoted(tmp_path) -> None:
    database, _ = setup_case(tmp_path)
    legacy_details(database, trusted_engine=False)
    monitor = DocumentMonitoringService(database)
    monitor.start("study", "unknown-monitor-start")
    result = monitor.record_revision("study", "page", "unknown-signature", "unknown-revision")
    assert result["observed_question_count"] == 0
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM observed_question").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM mistake_capture_candidate").fetchone()[0] == 0
