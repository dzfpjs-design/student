from __future__ import annotations

from observation_http_fixture import BoundObservationFrames, classify_public_frame, synthetic_frame

import base64
import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cv2
import numpy as np
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.behavior_model import ObservableEvent
from home_study_agent.behavior_model.app_integration import BehaviorAppIntegration
from home_study_agent.config import Settings


def client_for(tmp_path: Path, *, native_launch_attestation_key: str | None = None) -> TestClient:
    return TestClient(create_app(Settings(
        data_dir=tmp_path, database_path=tmp_path / "agent.db", data_encryption_key=b"0" * 32,
        maintenance_enabled=False, native_launch_attestation_key=native_launch_attestation_key,
    )))


def to_study(client: TestClient, prefix: str = "integrated") -> tuple[str, str]:
    session = client.post("/v1/sessions", json={"child_id": "child-junior-3", "client_action_id": f"{prefix}-session-001"}).json()
    session_id = session["session_id"]
    client.post(f"/v1/sessions/{session_id}/transition", json={"expected_state": "check_in", "target_state": "classroom_recall", "reason": "ready", "client_action_id": f"{prefix}-transition-001"})
    client.post(f"/v1/sessions/{session_id}/recalls", json={"subject": "数学", "summary": "学习判别式。", "client_action_id": f"{prefix}-recall-001"})
    client.post(f"/v1/sessions/{session_id}/transition", json={"expected_state": "classroom_recall", "target_state": "homework_capture", "reason": "ready", "client_action_id": f"{prefix}-transition-002"})
    client.post(f"/v1/sessions/{session_id}/homework", json={"subject": "数学", "title": "判别式练习", "expected_minutes": 20, "client_action_id": f"{prefix}-homework-001"})
    plan = client.post(f"/v1/sessions/{session_id}/plan/generate", json={"client_action_id": f"{prefix}-plan-001"}).json()
    client.post(f"/v1/sessions/{session_id}/transition", json={"expected_state": "plan_review", "target_state": "study", "reason": "ready", "client_action_id": f"{prefix}-transition-003"})
    return session_id, plan["plan_items"][0]["plan_item_id"]


def numbered_page() -> str:
    image = np.full((500, 900, 3), 255, dtype=np.uint8)
    cv2.putText(image, "1. Compute 125 x 32", (45, 180), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 0, 0), 3)
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    return base64.b64encode(encoded.tobytes()).decode("ascii")


class LowConfidenceLocalOcr:
    name = "simulated-document-camera-ocr"
    capability = {"printed_zh", "digits"}

    def health(self) -> dict:
        return {"ready": True, "route": "cpu"}

    def recognize(self, _image: np.ndarray, _route: str) -> list[dict]:
        return [{
            "text": "1. 判别式练习", "confidence": .42,
            "box": [[30, 30], [400, 30], [400, 85], [30, 85]], "kind": "printed",
        }]


def parent_headers(client: TestClient) -> dict[str, str]:
    response = client.post("/v1/parent/setup", json={"pin": "1234"})
    if response.status_code == 409:
        response = client.post("/v1/parent/unlock", json={"pin": "1234"})
    return {"X-HSC-Parent": response.json()["parent_token"]}


def historical_behavior_session(client: TestClient, child_id: str, session_id: str, at: str) -> None:
    """A real owned historical source row; no learning completion or score is seeded."""
    with client.app.state.behavior_model.database.connect() as connection:
        connection.execute("""INSERT INTO daily_session(session_id,child_id,status,state,
            started_at,last_checkpoint_at,client_action_id) VALUES(?,?,'completed','idle',?,?,?)""",
            (session_id,child_id,at,at,'historical-'+session_id))
        connection.commit()


def test_ocr_revision_inferences_are_retracted_without_removing_independent_evidence(tmp_path: Path, monkeypatch) -> None:
    with client_for(tmp_path) as client:
        session_id, _ = to_study(client, 'ocr-inference-repair')
        service = client.app.state.behavior_model
        adapter = BehaviorAppIntegration(service, service.database)
        for event_id, source, event_type in (
            ('document-revision:old-stall', 'local_document_progress', 'work.stall'),
            ('document-revision:old-write', 'local_document_progress', 'work.writing'),
            ('independent-confirmation', 'child_self_report', 'work.writing'),
        ):
            service.ingest(ObservableEvent(event_id=event_id, child_id='child-junior-3', session_id=session_id,
                occurred_at=datetime.now(timezone.utc).isoformat(), event_type=event_type, source=source, confidence=.8))
        assert adapter.document_revision(session_id, {'study_signal': {'kind': 'progress_observed'}}, 'new-write') is None
        assert adapter.can_offer_document_intervention(session_id, {'kind': 'possible_stall', 'may_prompt_child': True}) is False
        original = service.retract_event
        def fail_first(event_id, *args):
            if event_id == 'document-revision:old-stall':
                raise sqlite3.OperationalError('synthetic temporary error')
            return original(event_id, *args)
        monkeypatch.setattr(service, 'retract_event', fail_first)
        assert adapter.retract_unreliable_document_signals() == {'examined': 2, 'repaired': 1, 'failed': 1}
        monkeypatch.setattr(service, 'retract_event', original)
        assert adapter.retract_unreliable_document_signals() == {'examined': 1, 'repaired': 1, 'failed': 0}
        assert adapter.retract_unreliable_document_signals() == {'examined': 0, 'repaired': 0, 'failed': 0}
        with service.database.connect() as connection:
            rows = connection.execute("SELECT event_id FROM behavior_retraction ORDER BY event_id").fetchall()
            assert [row['event_id'] for row in rows] == ['document-revision:old-stall', 'document-revision:old-write']
            assert connection.execute("SELECT COUNT(*) FROM observable_event WHERE event_id='independent-confirmation'").fetchone()[0] == 1


def test_completed_daily_report_persists_authoritative_empty_behavior_context(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        session_id, item_id = to_study(client, "report-safe-evening")
        assert client.post(f"/v1/plan-items/{item_id}/start", json={"client_action_id": "report-safe-start-001"}).status_code == 200
        assert client.post(f"/v1/plan-items/{item_id}/complete", json={
            "actual_minutes": 12, "completion_evidence_type": "child_confirmed",
            "completion_note": None, "client_action_id": "report-safe-complete-001",
        }).status_code == 200
        assert client.post(f"/v1/sessions/{session_id}/transition", json={
            "expected_state": "study", "target_state": "reflection", "reason": "tasks_completed",
            "client_action_id": "report-safe-reflection-001",
        }).status_code == 200
        finished = client.post(f"/v1/sessions/{session_id}/reflection/complete", json={
            "learned_text": "会比较分数", "difficult_text": "还要核对通分", "feeling": "",
            "next_action": "明天先做一道通分题", "client_action_id": "report-safe-finish-001",
        })
        report = client.get(f"/v1/sessions/{session_id}/report", headers=parent_headers(client))
        with client.app.state.repository.database.connect() as db:
            event_sources = [row["source"] for row in db.execute("SELECT source FROM observable_event")]
            states = [row["state"] for row in db.execute("SELECT state FROM probabilistic_state")]
            interventions = db.execute("SELECT COUNT(*) FROM intervention").fetchone()[0]
    assert finished.status_code == 200 and finished.json()["daily_report_ready"] is True
    assert report.status_code == 200
    assert report.json()["evidence"]["behavior_context_authoritative"] is True
    assert report.json()["evidence"]["behavior_intervention_feedback"] == []
    assert report.json()["evidence"]["behavior_model_summary"]["active_hypotheses"] == []
    assert event_sources and set(event_sources).issubset({"plan", "child_self_report"})
    assert not set(states) & {"blocked_candidate", "distraction_candidate", "fatigue_candidate", "frequent_switching"}
    assert interventions == 0
    assert not any("可观察候选" in fact for fact in report.json()["summary"]["facts"])


def test_main_ocr_flow_persists_pipeline_evidence_then_reaches_mistake_candidate(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        headers = parent_headers(client)
        session_id, _ = to_study(client, "ocr-main")
        ocr = client.post(f"/v1/sessions/{session_id}/ocr", json={"image_base64": numbered_page(), "client_action_id": "ocr-main-action-001"})
        assert ocr.status_code == 200
        artifact = ocr.json()
        assert artifact["details"]["local_pipeline"]["privacy"]["raw_image_uploaded"] is False
        assert artifact["details"]["local_pipeline"]["queue"]["task_id"].startswith("ocr:")
        assert client.app.state.media_ai._ocr_engine is None
        prepared = client.post(f"/v1/capture-artifacts/{artifact['artifact_id']}/mistake-candidates/prepare", json={"child_id": "child-junior-3", "client_action_id": "ocr-main-prepare-001"})
        assert prepared.status_code == 200 and prepared.json()["items"]
        candidate = prepared.json()["items"][0]
        edited = client.put(f"/v1/mistake-capture-candidates/{candidate['candidate_id']}", json={"child_id": "child-junior-3", "question_text": "判别式练习题", "client_action_id": "ocr-main-edit-001"})
        knowledge = client.get("/v1/children/child-junior-3/mistake-knowledge-candidates", params={"q": "判别式", "subject": "数学"}).json()["items"]
        saved = client.post(f"/v1/mistake-capture-candidates/{candidate['candidate_id']}/capture", json={"child_id": "child-junior-3", "knowledge_node_id": knowledge[0]["knowledge_node_id"], "subject": "数学", "title": knowledge[0]["title"], "submitted_answer": "我不会", "client_action_id": "ocr-main-capture-001"})
        replay = client.get("/v1/children/child-junior-3/behavior/replay", params={"session_id": session_id}, headers=headers).json()
    assert edited.status_code == 200
    assert saved.status_code == 200
    assert saved.json()["mistake"]["status"] == "awaiting_confirmation"
    ocr_event = next(event for event in replay["events"] if event["event_type"] == "document.ocr_completed")
    assert ocr_event["source"] == "local_ocr"
    assert "extracted_text" not in ocr_event["payload"]


def test_parent_only_ocr_review_api_edits_undo_and_restart_recovery(tmp_path: Path) -> None:
    review_artifact = {
        "artifact_id": "review-artifact-001", "confidence": .3, "extracted_text": "x=2",
        "details": {"lines": [{"text": "x=2", "confidence": .3, "box": [[1, 1], [20, 1], [20, 8], [1, 8]]}],
                    "local_pipeline": {"manual_review": {"required": True, "handwriting": "not inferred", "math": "not structurally parsed", "reasons": ["low_ocr_confidence"]}}},
    }
    with client_for(tmp_path) as client:
        prepared = client.app.state.ocr_review_queue.prepare(review_artifact, "child-junior-3", "session-local", "review-prepare-001")["items"]
        item = prepared[0]
        denied = client.get("/v1/capture-artifacts/review-artifact-001/ocr-review-items", params={"child_id": "child-junior-3"})
        headers = parent_headers(client)
        listed = client.get("/v1/capture-artifacts/review-artifact-001/ocr-review-items", params={"child_id": "child-junior-3"}, headers=headers)
        changed = client.put(f"/v1/ocr-review-items/{item['review_id']}", json={"child_id": "child-junior-3", "corrected_text": "x=3", "client_action_id": "review-edit-001"}, headers=headers)
        replayed_change = client.put(f"/v1/ocr-review-items/{item['review_id']}", json={"child_id": "child-junior-3", "corrected_text": "x=3", "client_action_id": "review-edit-001"}, headers=headers)
        conflicting_change = client.put(f"/v1/ocr-review-items/{item['review_id']}", json={"child_id": "child-junior-3", "corrected_text": "x=4", "client_action_id": "review-edit-001"}, headers=headers)
        blank = client.put(f"/v1/ocr-review-items/{item['review_id']}", json={"child_id": "child-junior-3", "corrected_text": "   ", "client_action_id": "review-blank-001"}, headers=headers)
        history = client.get("/v1/children/child-junior-3/ocr-review-history", headers=headers)
    assert denied.status_code == 401
    assert listed.status_code == 200 and listed.json()["items"][0]["raw_media_retained"] is False
    assert changed.json()["status"] == "confirmed"
    assert replayed_change.status_code == 200 and replayed_change.json()["status"] == "confirmed"
    assert conflicting_change.status_code == 409 and conflicting_change.json()["code"] == "OCR_REVIEW_ACTION_CONFLICT"
    assert blank.status_code == 422
    assert history.json()["items"][0]["review_id"] == item["review_id"]
    with client_for(tmp_path) as restarted:
        headers = parent_headers(restarted)
        recovered = restarted.get("/v1/children/child-junior-3/ocr-review-history", headers=headers)
        undone = restarted.post(f"/v1/ocr-review-items/{item['review_id']}/undo", json={"child_id": "child-junior-3", "client_action_id": "review-undo-001"}, headers=headers)
    assert recovered.status_code == 200
    assert recovered.json()["items"][0]["status"] == "confirmed"
    assert undone.json()["status"] == "pending"


def test_parent_ocr_batch_api_is_atomic_and_idempotent(tmp_path: Path) -> None:
    review_artifact = {
        "artifact_id": "review-artifact-batch", "confidence": .31, "extracted_text": "第一行\n第二行",
        "details": {
            "lines": [
                {"text": "第一行", "confidence": .31, "box": [[1, 1], [20, 1], [20, 8], [1, 8]]},
                {"text": "第二行", "confidence": .37, "box": [[1, 12], [20, 12], [20, 20], [1, 20]]},
            ],
            "local_pipeline": {"manual_review": {
                "required": True, "handwriting": "not inferred", "reasons": ["low_ocr_confidence"],
            }},
        },
    }
    with client_for(tmp_path) as client:
        prepared = client.app.state.ocr_review_queue.prepare(
            review_artifact, "child-junior-3", "session-local", "review-batch-prepare"
        )["items"]
        headers = parent_headers(client)
        payload = {
            "child_id": "child-junior-3",
            "review_ids": [item["review_id"] for item in prepared],
            "decision": "confirm_proposed",
            "client_action_id": "review-batch-action-001",
        }
        first = client.post("/v1/ocr-review-items/batch", json=payload, headers=headers)
        replay = client.post("/v1/ocr-review-items/batch", json=payload, headers=headers)
        pending = client.get("/v1/children/child-junior-3/ocr-review-queue", headers=headers)

    assert first.status_code == 200 and replay.json() == first.json()
    assert first.json()["processed_count"] == 2
    assert all(item["corrected_text"] == item["proposed_text"] for item in first.json()["items"])
    assert pending.json()["items"] == []


def test_simulated_document_camera_ocr_keeps_derived_learning_data_after_raw_page_deletion_and_restart(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        headers = parent_headers(client)
        assert client.put("/v1/parent/privacy", headers=headers, json={"raw_media_storage_allowed": True}).status_code == 200
        client.app.state.ocr_workflow.adapter.ocr.engines = [LowConfidenceLocalOcr()]
        session_id, _ = to_study(client, "document-camera-sim")
        captured = client.post(
            f"/v1/sessions/{session_id}/ocr",
            json={"image_base64": numbered_page(), "client_action_id": "document-camera-sim-ocr-001"},
        )
        assert captured.status_code == 200
        artifact = captured.json()
        assert artifact["details"]["local_pipeline"]["quality"]["passed"] in {True, False}
        assert artifact["ocr_review"]["items"]
        review = artifact["ocr_review"]["items"][0]
        assert review["review_kind"] == "text"
        corrected = client.put(
            f"/v1/ocr-review-items/{review['review_id']}",
            headers=headers,
            json={"child_id": "child-junior-3", "corrected_text": "判别式练习", "client_action_id": "document-camera-sim-review-001"},
        )
        prepared = client.post(
            f"/v1/capture-artifacts/{artifact['artifact_id']}/mistake-candidates/prepare",
            json={"child_id": "child-junior-3", "client_action_id": "document-camera-sim-candidate-001"},
        )
        # A low-confidence heading is not a complete question. This retention
        # test separately saves a manually supplied question, not an OCR result.
        assert prepared.status_code == 422
        assert "不能拼凑" in prepared.text
        knowledge = client.get(
            "/v1/children/child-junior-3/mistake-knowledge-candidates",
            params={"q": "判别式", "subject": "数学"},
        ).json()["items"][0]
        saved = client.post(
            "/v1/mistakes/capture",
            json={"child_id": "child-junior-3", "knowledge_node_id": knowledge["knowledge_node_id"], "subject": "数学", "title": knowledge["title"], "prompt_summary": "x²-3x+2=0 的判别式是多少？", "submitted_answer": "不知道", "source_type": "manual", "source_ref": artifact["artifact_id"], "client_action_id": "document-camera-sim-mistake-001"},
        )
        with sqlite3.connect(tmp_path / "agent.db") as connection:
            raw_path = Path(connection.execute("SELECT local_path FROM capture_artifact WHERE artifact_id = ?", (artifact["artifact_id"],)).fetchone()[0])
        assert raw_path.is_file()
        removed = client.request(
            "DELETE", f"/v1/parent/capture-artifacts/{artifact['artifact_id']}/raw-media", headers=headers,
            json={"confirmation": "确认删除", "client_action_id": "document-camera-sim-raw-delete-001"},
        )
    assert corrected.status_code == 200
    assert saved.status_code == 200
    assert removed.status_code == 200
    assert removed.json()["derived_artifact_retained"] is True
    assert removed.json()["raw_file_deleted"] is True
    assert not raw_path.exists()
    with client_for(tmp_path) as restarted:
        headers = parent_headers(restarted)
        history = restarted.get("/v1/children/child-junior-3/ocr-review-history", headers=headers)
        artifacts = restarted.get("/v1/parent/children/child-junior-3/capture-artifacts", headers=headers)
    assert history.status_code == 200
    assert history.json()["items"][0]["corrected_text"] == "判别式练习"
    retained = next(item for item in artifacts.json()["items"] if item["artifact_id"] == artifact["artifact_id"])
    assert retained["raw_file_stored"] is False
    with sqlite3.connect(tmp_path / "agent.db") as connection:
        retained_row = connection.execute("SELECT local_path, extracted_text, details_json FROM capture_artifact WHERE artifact_id = ?", (artifact["artifact_id"],)).fetchone()
        mistake_count = connection.execute("SELECT COUNT(*) FROM mistake_case WHERE child_id = 'child-junior-3'").fetchone()[0]
    assert retained_row[0] is None
    assert retained_row[1] == "1. 判别式练习"
    assert "local_pipeline" in retained_row[2]
    assert mistake_count == 1


def test_raw_page_deletion_retry_keeps_derived_artifact_when_file_is_temporarily_busy(tmp_path: Path, monkeypatch) -> None:
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    raw_path = media_dir / "simulated-camera-page.jpg.enc"
    raw_path.write_bytes(b"encrypted-local-page")
    original_unlink = Path.unlink
    with client_for(tmp_path) as client:
        headers = parent_headers(client)
        session_id, _ = to_study(client, "raw-delete-retry")
        with sqlite3.connect(tmp_path / "agent.db") as connection:
            connection.execute(
                """INSERT INTO capture_artifact(artifact_id, child_id, session_id, artifact_type, local_path,
                   extracted_text, confidence, details_json, status, created_at, client_action_id)
                   VALUES ('raw-delete-retry-artifact', 'child-junior-3', ?, 'document_ocr', ?, '派生文本', .8, '{}', 'completed', datetime('now'), 'raw-delete-retry-capture')""",
                (session_id, str(raw_path)),
            )
            connection.commit()

        def busy_unlink(path: Path, *args, **kwargs):
            if path.resolve() == raw_path.resolve():
                raise PermissionError("simulated_camera_file_busy")
            return original_unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", busy_unlink)
        failed = client.request(
            "DELETE", "/v1/parent/capture-artifacts/raw-delete-retry-artifact/raw-media", headers=headers,
            json={"confirmation": "确认删除", "client_action_id": "raw-delete-retry-action-001"},
        )
        monkeypatch.setattr(Path, "unlink", original_unlink)
        retried = client.post(
            f"/v1/parent/deletions/{failed.json()['details']['deletion_id']}/retry", headers=headers,
            json={"confirmation": "确认删除"},
        )
    assert failed.status_code == 500
    assert failed.json()["code"] == "FILE_DELETE_FAILED"
    assert retried.status_code == 200
    assert retried.json()["derived_artifact_retained"] is True
    assert retried.json()["raw_file_deleted"] is True
    assert not raw_path.exists()
    with sqlite3.connect(tmp_path / "agent.db") as connection:
        retained = connection.execute(
            "SELECT local_path, extracted_text FROM capture_artifact WHERE artifact_id = 'raw-delete-retry-artifact'"
        ).fetchone()
    assert retained == (None, "派生文本")


def test_parent_can_retract_only_own_local_observation_and_restart_keeps_evidence(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        headers = parent_headers(client)
        model = client.app.state.behavior_model
        historical_behavior_session(client, "child-junior-3", "session-a", "2026-08-28T10:00:00Z")
        historical_behavior_session(client, "child-primary-4", "session-b", "2026-08-28T10:00:00Z")
        model.ingest(ObservableEvent(event_id="vision-junior", child_id="child-junior-3", session_id="session-a", occurred_at="2026-08-28T10:00:00Z", event_type="work.stall", source="local_vision", confidence=.95))
        model.ingest(ObservableEvent(event_id="vision-primary", child_id="child-primary-4", session_id="session-b", occurred_at="2026-08-28T10:00:00Z", event_type="work.stall", source="local_vision", confidence=.8))
        state = model.replay("child-junior-3")["states"][0]
        prompt = model.decide_intervention("child-junior-3", "session-a", "2026-08-28T10:01:00Z")
        assert prompt["status"] == "suggested"
        denied = client.post(f"/v1/children/child-junior-3/behavior/states/{state['state_id']}/retract", json={"note": "孩子在心算", "client_action_id": "parent-retract-001"})
        wrong_child = client.post(f"/v1/children/child-primary-4/behavior/states/{state['state_id']}/retract", json={"note": "不属于这个孩子", "client_action_id": "parent-retract-002"}, headers=headers)
        retracted = client.post(f"/v1/children/child-junior-3/behavior/states/{state['state_id']}/retract", json={"note": "孩子在心算", "client_action_id": "parent-retract-003"}, headers=headers)
        replayed = client.post(f"/v1/children/child-junior-3/behavior/states/{state['state_id']}/retract", json={"note": "孩子在心算", "client_action_id": "parent-retract-003"}, headers=headers)
        conflicting = client.post(f"/v1/children/child-junior-3/behavior/states/{state['state_id']}/retract", json={"note": "改成另一种说法", "client_action_id": "parent-retract-003"}, headers=headers)
    assert denied.status_code == 401
    assert wrong_child.status_code == 404
    assert retracted.status_code == 200
    assert replayed.status_code == 200 and replayed.json()["idempotent"] is True
    assert conflicting.status_code == 409
    with client_for(tmp_path) as restarted:
        headers = parent_headers(restarted)
        replay = restarted.get("/v1/children/child-junior-3/behavior/replay", headers=headers).json()
        primary = restarted.get("/v1/children/child-primary-4/behavior/replay", headers=headers).json()
    assert replay["states"][0]["retracted"]["note"] == "孩子在心算"
    assert replay["interventions"][0]["status"] == "withdrawn"
    assert replay["interventions"][0]["withdrawal_reason"] == "corrected_observation"
    assert restarted.app.state.behavior_model.pending_feedback("child-junior-3", "session-a", "2026-08-28T10:02:00Z") == {"status": "no_pending"}
    report_evidence = restarted.app.state.behavior_model.behavior_model_summary("child-junior-3", "session-a", "2026-08-28")
    assert report_evidence["active_candidates"] == []
    assert report_evidence["retracted_hypothesis_count"] == 1
    assert "retracted" not in primary["states"][0]


def test_managed_behavior_records_follow_backup_restore_and_child_deletion(tmp_path: Path) -> None:
    from privacy_restore_operation import restore_operation
    from synthetic_native_restore_request import signed_restore_post, KEY
    from test_native_privacy_permit_http import certificate, signed_header

    legacy_path = tmp_path / "behavior-model.db"
    from home_study_agent.behavior_model import BehaviorModelService
    legacy = BehaviorModelService(legacy_path)
    legacy.ingest(ObservableEvent(event_id="legacy-behavior-junior", child_id="child-junior-3", session_id="legacy-session", occurred_at="2026-08-27T10:00:00Z", event_type="work.stall", source="local_vision", confidence=.95))
    with client_for(tmp_path, native_launch_attestation_key=KEY) as client:
        model = client.app.state.behavior_model
        lifecycle = client.app.state.privacy_lifecycle
        headers = parent_headers(client)
        assert model.replay("child-junior-3", "legacy-session")["events"][0]["event_id"] == "legacy-behavior-junior"
        model.ingest(ObservableEvent(event_id="backup-behavior-junior", child_id="child-junior-3", session_id="backup-session-junior", occurred_at="2026-08-28T10:00:00Z", event_type="work.stall", source="local_vision", confidence=.95))
        model.ingest(ObservableEvent(event_id="backup-behavior-primary", child_id="child-primary-4", session_id="backup-session-primary", occurred_at="2026-08-28T10:00:00Z", event_type="work.stall", source="local_vision", confidence=.95))
        exported = json.loads(Path(lifecycle.export_child("child-junior-3")["path"]).read_text(encoding="utf-8"))
        assert {row["event_id"] for row in exported["behavior_events"]} == {"legacy-behavior-junior", "backup-behavior-junior"}
        assert exported["behavior_interventions"] == []
        backup = lifecycle.create_backup()
        with client.app.state.repository.database.connect() as connection:
            connection.execute("DELETE FROM probabilistic_state WHERE child_id = 'child-junior-3'")
            connection.execute("DELETE FROM observable_event WHERE child_id = 'child-junior-3'")
            connection.commit()
        assert model.replay("child-junior-3")["events"] == []
        restored = signed_restore_post(client, headers=headers, json={
            "backup_id": backup["backup_id"], "confirmation": "确认恢复",
            "operation": restore_operation(client, headers=headers)})
        assert restored.status_code == 200, restored.text
        assert restored.json()["status"] == "restored"
        assert {row["event_id"] for row in model.replay("child-junior-3")["events"]} == {"legacy-behavior-junior", "backup-behavior-junior"}
        delete_headers = parent_headers(client)
        delete_operation = restore_operation(client, headers=delete_headers)
        delete_operation["request_id"] = "behavior-delete-001"
        delete_body = {"scope": "media", "confirmation": "确认删除",
                       "client_action_id": "behavior-delete-001", "operation": delete_operation}
        delete_path = "/v1/parent/children/child-junior-3/delete"
        delete_proof = certificate(delete_path, delete_body, delete_operation,
                                   kind="delete", child_id="child-junior-3", scope="media")
        deleted = client.post(delete_path, json=delete_body,
                              headers={**delete_headers, **signed_header(delete_proof)})
        assert deleted.status_code == 200 and deleted.json()["status"] == "completed"
        assert model.replay("child-junior-3")["events"] == []
        assert model.replay("child-primary-4")["events"][0]["event_id"] == "backup-behavior-primary"
        with sqlite3.connect(legacy_path.with_name("behavior-model.db.migrated")) as archive:
            assert archive.execute("SELECT COUNT(*) FROM observable_event WHERE child_id = 'child-junior-3'").fetchone()[0] == 0

    with client_for(tmp_path) as restarted:
        assert restarted.app.state.behavior_model.replay("child-junior-3")["events"] == []
        assert restarted.app.state.behavior_model.replay("child-primary-4")["events"][0]["event_id"] == "backup-behavior-primary"


def test_parent_behavior_personalization_is_authorized_isolated_and_resettable(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        model = client.app.state.behavior_model
        historical_behavior_session(client, "child-junior-3", "session-a", "2025-08-28T10:00:00Z")
        model.ingest(ObservableEvent(event_id="personalization-state", child_id="child-junior-3", session_id="session-a", occurred_at="2025-08-28T10:00:00Z", event_type="work.stall", source="local_vision", confidence=.95))
        decision = model.decide_intervention("child-junior-3", "session-a", "2025-08-28T10:01:00Z")
        model.record_feedback(decision["intervention_id"], False, "2025-08-28T10:02:00Z")
        denied = client.get("/v1/children/child-junior-3/behavior/personalization")
        headers = parent_headers(client)
        junior = client.get("/v1/children/child-junior-3/behavior/personalization", headers=headers)
        primary = client.get("/v1/children/child-primary-4/behavior/personalization", headers=headers)
        reset = client.post("/v1/children/child-junior-3/behavior/personalization/reset", json={"client_action_id": "personalization-reset-001"}, headers=headers)
    assert denied.status_code == 401
    assert junior.json()["items"][0]["sample_size"] == 1
    assert primary.json()["items"] == []
    assert reset.status_code == 200 and reset.json()["items"][0]["sample_size"] == 0


def test_child_session_can_reset_only_its_own_reminder_preference(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        session_id = client.post("/v1/sessions", json={"child_id": "child-primary-4", "client_action_id": "child-reset-session-001"}).json()["session_id"]
        model = client.app.state.behavior_model
        with model._connect() as db:
            db.execute("INSERT INTO intervention(intervention_id, child_id, session_id, decided_at, candidate_state, message, feedback, intervention_type) VALUES ('child-policy', 'child-primary-4', ?, '2025-08-28T10:00:00Z', 'blocked_candidate', '询问', 'declined', 'default_ask_first')", (session_id,))
            db.execute("INSERT INTO intervention(intervention_id, child_id, session_id, decided_at, candidate_state, message, feedback, intervention_type) VALUES ('other-policy', 'child-junior-3', 'other-session', '2025-08-28T10:00:00Z', 'blocked_candidate', '询问', 'declined', 'default_ask_first')")
            db.commit()
        reset = client.post(f"/v1/sessions/{session_id}/behavior/personalization/reset", json={"client_action_id": "child-reset-action-001"})
        primary = model.personalization("child-primary-4")
        junior = model.personalization("child-junior-3")
    assert reset.status_code == 200 and reset.json()["items"][0]["sample_size"] == 0
    assert primary["items"][0]["sample_size"] == 0
    assert junior["items"][0]["sample_size"] == 1


def test_main_asr_flow_aligns_audio_metadata_without_transcript(tmp_path: Path, monkeypatch) -> None:
    with client_for(tmp_path) as client:
        headers = parent_headers(client)
        session_id = client.post("/v1/sessions", json={"child_id": "child-primary-4", "client_action_id": "asr-main-session-001"}).json()["session_id"]
        monkeypatch.setattr(client.app.state.media_ai, "transcribe", lambda *_args: {
            "voice_input_id": "voice-local-001", "purpose": "agent_command", "duration_ms": 1200,
            "created_at": "2026-08-28T10:00:00+00:00", "transcript": "我需要帮助", "local_path": None,
        })
        response = client.post(f"/v1/sessions/{session_id}/asr", json={
            "wav_base64": base64.b64encode(b"local-only-test-audio-payload").decode("ascii"),
            "purpose": "agent_command", "client_action_id": "asr-main-action-001",
        })
        replay = client.get("/v1/children/child-primary-4/behavior/replay", params={"session_id": session_id}, headers=headers).json()
    assert response.status_code == 200
    audio_event = next(event for event in replay["events"] if event["event_type"] == "audio.transcribed")
    assert audio_event["source"] == "local_asr"
    assert audio_event["payload"] == {"purpose": "agent_command", "duration_ms": 1200}
    assert "我需要帮助" not in json.dumps(audio_event, ensure_ascii=False)


def test_main_asr_flow_rejects_incomplete_adapter_result_without_a_server_error(tmp_path: Path, monkeypatch) -> None:
    with client_for(tmp_path) as client:
        session_id = client.post("/v1/sessions", json={"child_id": "child-primary-4", "client_action_id": "asr-invalid-adapter-session-001"}).json()["session_id"]
        monkeypatch.setattr(client.app.state.media_ai, "transcribe", lambda *_args: {"transcript": "缺少宿主字段"})
        response = client.post(f"/v1/sessions/{session_id}/asr", json={
            "wav_base64": base64.b64encode(b"local-only-test-audio-payload").decode("ascii"),
            "purpose": "agent_command", "client_action_id": "asr-invalid-adapter-action-001",
        })
    assert response.status_code == 502
    assert response.json()["code"] == "ASR_ADAPTER_RESULT_INVALID"


def test_main_behavior_flow_persists_plan_child_correction_and_intervention_across_restart(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        headers = parent_headers(client)
        session_id, item_id = to_study(client, "behavior-main")
        assert client.post(f"/v1/plan-items/{item_id}/start", json={"client_action_id": "behavior-main-start-001"}).status_code == 200
        correction = client.post(f"/v1/sessions/{session_id}/observation/correct", json={"correction_text": "我在心算，不是停住。", "client_action_id": "behavior-main-correction-001"})
        recent_at = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        client.app.state.behavior_model.ingest(ObservableEvent(
            event_id="behavior-main-stall", child_id="child-junior-3", session_id=session_id,
            occurred_at=recent_at, event_type="work.stall", source="local_vision", confidence=.95,
        ))
        for day in range(1, 4):
            client.app.state.behavior_model.ingest(ObservableEvent(
                event_id=f"behavior-main-switch-{day}", child_id="child-junior-3", session_id=session_id,
                occurred_at=f"2026-08-0{day}T10:00:00Z", event_type="interaction.switch", source="app_interaction", confidence=.9,
            ))
        decision = client.post("/v1/children/child-junior-3/behavior/interventions/decide", params={"session_id": session_id}, headers=headers).json()
        feedback = client.post(f"/v1/behavior/interventions/{decision['intervention_id']}/feedback", json={"session_id": session_id, "accepted": False, "note": "先自己想想", "client_action_id": "behavior-main-feedback-001"})
        effect = client.post(f"/v1/behavior/interventions/{decision['intervention_id']}/effect", json={"session_id": session_id, "outcome_state": "writing", "probability": .7, "client_action_id": "behavior-main-effect-001"})
        replay = client.get("/v1/children/child-junior-3/behavior/replay", params={"session_id": session_id}, headers=headers).json()
        assert correction.status_code == 200
        assert feedback.status_code == 200 and effect.status_code == 200
        assert {event["event_type"] for event in replay["events"]} >= {"task.started", "child.check_in", "work.stall"}
    with client_for(tmp_path) as restarted:
        headers = parent_headers(restarted)
        replay = restarted.get("/v1/children/child-junior-3/behavior/replay", params={"session_id": session_id}, headers=headers).json()
        baseline = restarted.get("/v1/children/child-junior-3/behavior/baseline", headers=headers).json()
    assert replay["interventions"][0]["feedback"] == "declined"
    assert replay["interventions"][0]["outcome"]["state"] == "writing"
    assert baseline["status"] == "insufficient_evidence"


def test_global_behavior_feedback_and_effect_are_bound_to_the_current_session_child(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        session_id, _ = to_study(client, "bound-feedback")
        model = client.app.state.behavior_model
        historical_behavior_session(client, "child-primary-4", "primary-source-session", datetime.now(timezone.utc).isoformat())
        model.ingest(ObservableEvent(event_id="other-child-prompt", child_id="child-primary-4", session_id="primary-source-session", occurred_at=datetime.now(timezone.utc).isoformat(), event_type="work.stall", source="local_vision", confidence=.95))
        decision = model.decide_intervention("child-primary-4", "primary-source-session", datetime.now(timezone.utc).isoformat())
        assert decision["status"] == "suggested"
        forged_feedback = client.post(f"/v1/behavior/interventions/{decision['intervention_id']}/feedback", json={"session_id": session_id, "accepted": True, "client_action_id": "bound-feedback-action-001"})
        forged_effect = client.post(f"/v1/behavior/interventions/{decision['intervention_id']}/effect", json={"session_id": session_id, "outcome_state": "writing", "probability": .8, "client_action_id": "bound-effect-action-001"})
    assert forged_feedback.status_code == 404
    assert forged_effect.status_code == 404


def test_behavior_history_is_parent_only(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        denied = client.get("/v1/children/child-junior-3/behavior/replay")
        allowed = client.get("/v1/children/child-junior-3/behavior/replay", headers=parent_headers(client))
    assert denied.status_code == 401
    assert allowed.status_code == 200


def test_local_ai_status_reports_model_health_without_local_paths(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        response = client.get("/v1/local-ai/status")
    assert response.status_code == 200
    payload = response.json()
    assert "model_dir" not in payload["asr"]
    assert "model_directory" not in payload["ocr"]
    assert str(tmp_path) not in json.dumps(payload, ensure_ascii=False)


def test_observation_api_downsamples_to_cpu_budget_and_skips_excess_rate(tmp_path: Path) -> None:
    image = np.full((1080, 1920, 3), 150, dtype=np.uint8)
    ok, encoded = cv2.imencode(".jpg", image)
    assert ok
    with client_for(tmp_path) as client:
        session_id = client.post("/v1/sessions", json={"child_id": "child-primary-4", "client_action_id": "budget-session-001"}).json()["session_id"]
        client_capture = BoundObservationFrames(client, session_id)
        first = client_capture.post(json={"frame_base64": base64.b64encode(encoded.tobytes()).decode("ascii")}).json()
        second = client_capture.post(json={"frame_base64": base64.b64encode(encoded.tobytes()).decode("ascii")}).json()
    assert first["metrics"]["input_width"] == 640
    assert first["metrics"]["input_height"] == 360
    assert first["metrics"]["source_width"] == 1920
    assert second["analysis_skipped"] == "fps_budget"


def test_live_observation_uses_longitudinal_policy_and_child_feedback_survives_restart(tmp_path: Path, monkeypatch) -> None:
    """The camera route must not create the legacy shared-DB reminder record."""
    with client_for(tmp_path) as client:
        headers = parent_headers(client)
        session_id, item_id = to_study(client, "live-policy")
        assert client.post(f"/v1/plan-items/{item_id}/start", json={"client_action_id": "live-policy-start-001"}).status_code == 200
        assert client.put("/v1/parent/children/child-junior-3/family-rule", headers=headers, json={
            "latest_end_time": None, "max_learning_minutes": 90, "task_segment_minutes": 25,
            "break_minutes": 5, "pet_min_minutes": 0, "competition_minutes": 0,
            "observation_mode": "gentle", "reminder_budget_per_10m": 1,
        }).status_code == 200
        observed_at = datetime.now(timezone.utc).isoformat()
        classify_public_frame(client.app.state.observation_engine, 'paused_like', age_seconds=60)
        client_capture = BoundObservationFrames(client, session_id)
        response = client_capture.post(json={"frame_base64": synthetic_frame()})
        intervention = response.json()["intervention"]
        assert response.status_code == 200 and intervention and response.json()["longitudinal_recorded"] is True
        pending = client.get(f"/v1/sessions/{session_id}/behavior/interventions/pending")
        assert pending.status_code == 200 and pending.json()["intervention"]["intervention_id"] == intervention["intervention_id"]
        with sqlite3.connect(tmp_path / "agent.db") as db:
            assert db.execute("SELECT COUNT(*) FROM intervention_record").fetchone()[0] == 0
            assert db.execute("SELECT COUNT(*) FROM intervention WHERE child_id = 'child-junior-3'").fetchone()[0] == 1
    with client_for(tmp_path) as restarted:
        recovered = restarted.get(f"/v1/sessions/{session_id}/behavior/interventions/pending")
        assert recovered.status_code == 200 and recovered.json()["status"] == "pending"
        accepted = restarted.post(f"/v1/sessions/{session_id}/behavior/interventions/{intervention['intervention_id']}/feedback", json={"accepted": True, "client_action_id": "live-policy-answer-001"})
        no_pending = restarted.get(f"/v1/sessions/{session_id}/behavior/interventions/pending")
        replay = restarted.get("/v1/children/child-junior-3/behavior/replay", params={"session_id": session_id}, headers=parent_headers(restarted)).json()
        primary = restarted.get("/v1/children/child-primary-4/behavior/personalization", headers=parent_headers(restarted)).json()
    assert accepted.status_code == 200 and accepted.json()["feedback"] == "accepted"
    assert no_pending.json()["status"] == "no_pending"
    assert replay["interventions"][0]["feedback"] == "accepted"
    assert primary["items"] == []


def test_manual_shadow_keeps_local_observation_out_of_longitudinal_model_after_restart(tmp_path: Path, monkeypatch) -> None:
    """A usable frame may retain local reversible evidence, never a habit/model event in shadow mode."""
    frame = synthetic_frame()
    with client_for(tmp_path) as client:
        headers = parent_headers(client)
        session_id, _ = to_study(client, "manual-shadow")
        assert client.put("/v1/parent/children/child-junior-3/family-rule", headers=headers, json={
            "latest_end_time": None, "max_learning_minutes": 90, "task_segment_minutes": 25,
            "break_minutes": 5, "pet_min_minutes": 0, "competition_minutes": 0,
            "observation_mode": "shadow", "reminder_budget_per_10m": 1,
        }).status_code == 200
        observed_at = datetime.now(timezone.utc).isoformat()
        classify_public_frame(client.app.state.observation_engine, 'writing_like', age_seconds=0)
        client_capture = BoundObservationFrames(client, session_id)
        response = client_capture.post(json={"frame_base64": frame})
        assert response.status_code == 200 and response.json()["longitudinal_recorded"] is False

    with client_for(tmp_path) as restarted:
        classify_public_frame(restarted.app.state.observation_engine, 'writing_like', age_seconds=0)
        restarted_capture = BoundObservationFrames(restarted, session_id)
        retried = restarted_capture.post(json={"frame_base64": frame})
        with restarted.app.state.repository.database.connect() as db:
            longitudinal = db.execute("SELECT COUNT(*) FROM observable_event WHERE session_id = ? AND source = 'local_vision'", (session_id,)).fetchone()[0]
    assert retried.status_code == 200 and retried.json()["longitudinal_recorded"] is False
    assert longitudinal == 0


def test_explicit_spoken_reminder_feedback_uses_the_same_behavior_record(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        session_id = client.post("/v1/sessions", json={"child_id": "child-primary-4", "client_action_id": "spoken-feedback-session-001"}).json()["session_id"]
        now = datetime.now(timezone.utc).isoformat()
        model = client.app.state.behavior_model
        model.ingest(ObservableEvent(event_id="spoken-feedback-stall", child_id="child-primary-4", session_id=session_id, occurred_at=now, event_type="work.stall", source="local_vision", confidence=.95))
        decision = model.decide_intervention("child-primary-4", session_id, now)
        spoken = client.post(f"/v1/sessions/{session_id}/agent-turn", json={"transcript": "这类提醒少一点", "client_action_id": "spoken-feedback-answer-001"})
        replay = client.get("/v1/children/child-primary-4/behavior/replay", headers=parent_headers(client)).json()
        with sqlite3.connect(tmp_path / "agent.db") as db:
            legacy_count = db.execute("SELECT COUNT(*) FROM intervention_record").fetchone()[0]
    assert decision["status"] == "suggested"
    assert spoken.json()["intent"] == "behavior_intervention_feedback"
    assert replay["interventions"][0]["feedback"] == "declined"
    assert replay["interventions"][0]["feedback_note"] == "less_like_this"
    assert legacy_count == 0


def test_spoken_feedback_crash_window_replays_same_action_and_rejects_payload_conflicts(tmp_path: Path) -> None:
    action_id = "spoken-crash-window-001"
    with client_for(tmp_path) as client:
        session_id = client.post("/v1/sessions", json={"child_id": "child-primary-4", "client_action_id": "spoken-crash-session-001"}).json()["session_id"]
        now = datetime.now(timezone.utc).isoformat()
        model = client.app.state.behavior_model
        model.ingest(ObservableEvent(event_id="spoken-crash-stall", child_id="child-primary-4", session_id=session_id, occurred_at=now, event_type="work.stall", source="local_vision", confidence=.95))
        decision = model.decide_intervention("child-primary-4", session_id, now)
        # Simulate a crash after the managed behavior tables commit but before agent_turn.
        model.record_feedback(
            decision["intervention_id"], False, now, "later", f"voice:{action_id}", "child-primary-4", session_id,
            hashlib.sha256("稍后".encode("utf-8")).hexdigest(),
        )
        assert model.feedback_for_action(f"voice:{action_id}", "child-junior-3", session_id) is None
        wrong_payload = client.post(f"/v1/sessions/{session_id}/agent-turn", json={"transcript": "愿意", "client_action_id": action_id})
        with closing(sqlite3.connect(tmp_path / "agent.db")) as db:
            assert db.execute("SELECT COUNT(*) FROM agent_turn WHERE client_action_id = ?", (action_id,)).fetchone()[0] == 0
            assert db.execute("SELECT COUNT(*) FROM learning_input_arrival WHERE client_action_id = ?", (action_id,)).fetchone()[0] == 0
    with client_for(tmp_path) as restarted:
        replayed = restarted.post(f"/v1/sessions/{session_id}/agent-turn", json={"transcript": "稍后", "client_action_id": action_id})
        retried = restarted.post(f"/v1/sessions/{session_id}/agent-turn", json={"transcript": "稍后", "client_action_id": action_id})
        changed_after_main_write = restarted.post(f"/v1/sessions/{session_id}/agent-turn", json={"transcript": "这类提醒少一点", "client_action_id": action_id})
    assert decision["status"] == "suggested"
    assert wrong_payload.status_code == 409 and wrong_payload.json()["code"] == "AGENT_ACTION_PAYLOAD_CONFLICT"
    assert replayed.status_code == 200 and replayed.json()["intent"] == "behavior_intervention_feedback"
    assert replayed.json()["action"]["response_kind"] == "later"
    assert retried.json()["agent_turn_id"] == replayed.json()["agent_turn_id"]
    assert changed_after_main_write.status_code == 409 and changed_after_main_write.json()["code"] == "AGENT_ACTION_PAYLOAD_CONFLICT"


def test_unready_camera_scene_never_qualifies_a_prompt_candidate(tmp_path: Path, monkeypatch) -> None:
    image = np.full((240, 320, 3), 150, dtype=np.uint8)
    ok, encoded = cv2.imencode(".jpg", image)
    assert ok
    with client_for(tmp_path) as client:
        session_id = client.post("/v1/sessions", json={"child_id": "child-primary-4", "client_action_id": "unready-camera-session-001"}).json()["session_id"]
        monkeypatch.setattr(client.app.state.observation_engine, "_classify", lambda *_args: ("paused_like", .95, "synthetic paused candidate"))
        analysis = client.post(f"/v1/sessions/{session_id}/observation-frame", json={"frame_base64": base64.b64encode(encoded.tobytes()).decode("ascii")})
    assert analysis.status_code == 200
    assert analysis.json()["observation_ready"] is False
    assert analysis.json()["prompt_candidate"] is False
    assert analysis.json()["longitudinal_recorded"] is False
    with sqlite3.connect(tmp_path / "agent.db") as db:
        assert db.execute("SELECT COUNT(*) FROM observable_event WHERE source = 'local_vision'").fetchone()[0] == 0


def test_paused_observation_frame_is_rejected_without_new_state_or_behavior_evidence(tmp_path: Path) -> None:
    image = np.full((240, 320, 3), 150, dtype=np.uint8)
    cv2.line(image, (20, 20), (300, 220), (30, 30, 30), 4)
    ok, encoded = cv2.imencode(".jpg", image)
    assert ok
    with client_for(tmp_path) as client:
        session_id = client.post("/v1/sessions", json={"child_id": "child-primary-4", "client_action_id": "paused-observation-session"}).json()["session_id"]
        paused = client.post(f"/v1/sessions/{session_id}/observation/pause", json={"client_action_id": "paused-observation-pause"})
        analysis = client.post(f"/v1/sessions/{session_id}/observation-frame", json={"frame_base64": base64.b64encode(encoded.tobytes()).decode("ascii")})
    assert paused.status_code == 200
    assert analysis.status_code == 409
    assert analysis.json()["code"] == "OBSERVATION_PAUSED"
    with sqlite3.connect(tmp_path / "agent.db") as db:
        assert db.execute("SELECT COUNT(*) FROM child_state_event WHERE session_id = ?", (session_id,)).fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM observable_event WHERE session_id = ?", (session_id,)).fetchone()[0] == 0


def test_dark_history_never_counts_toward_stillness_after_a_usable_baseline(tmp_path: Path, monkeypatch) -> None:
    class FaceDetector:
        def detectMultiScale(self, *_args, **_kwargs):
            return ((20, 20, 50, 50),)

    dark = np.full((240, 320, 3), 8, dtype=np.uint8)
    bright = np.full((240, 320, 3), 150, dtype=np.uint8)
    cv2.line(bright, (20, 20), (300, 220), (30, 30, 30), 4)
    times = iter([datetime.now(timezone.utc), datetime.now(timezone.utc) + timedelta(seconds=30)])
    with client_for(tmp_path) as client:
        session_id = client.post("/v1/sessions", json={"child_id": "child-primary-4", "client_action_id": "dark-history-session"}).json()["session_id"]
        engine = client.app.state.observation_engine
        engine._face = FaceDetector()
        monkeypatch.setattr(engine, "_now", lambda: next(times))
        client_capture = BoundObservationFrames(client, session_id)
        first = client_capture.post(json={"frame_base64": base64.b64encode(cv2.imencode(".jpg", dark)[1].tobytes()).decode("ascii")})
        recovered = client_capture.post(json={"frame_base64": base64.b64encode(cv2.imencode(".jpg", bright)[1].tobytes()).decode("ascii")})
    assert first.json()["state"] == "unknown"
    assert recovered.json()["state"] == "present"
    assert recovered.json()["duration_ms"] == 0
