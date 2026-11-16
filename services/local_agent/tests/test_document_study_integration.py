"""HTTP contracts for continuous observation; no cameras or cloud are faked as real."""
import base64
import json
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import pytest
import cv2
import numpy as np

from test_app import advance_to_study, client_for


def _page(client, sid, artifact_id, text):
    with client.app.state.document_monitoring.database.connect() as connection:
        child = connection.execute("SELECT child_id FROM daily_session WHERE session_id=?", (sid,)).fetchone()[0]
        connection.execute("""INSERT INTO capture_artifact(artifact_id,child_id,session_id,artifact_type,
            local_path,extracted_text,confidence,details_json,status,created_at,client_action_id)
            VALUES(?,?,?,'document_ocr',NULL,?,.93,'{}','completed',?,?)""",
            (artifact_id, child, sid, text, datetime.now(timezone.utc).isoformat(), "ocr:" + artifact_id))
        connection.commit()
    response = client.post(f"/v1/sessions/{sid}/document-monitor/revisions", json={
        "artifact_id": artifact_id, "content_signature": artifact_id, "client_action_id": "revision:" + artifact_id,
    })
    assert response.status_code == 200, response.text
    return response.json()


def test_page_to_background_understanding_and_long_term_memory_survives_restart(tmp_path):
    with client_for(tmp_path) as client:
        sid, _ = advance_to_study(client, action_prefix="page-memory")
        root = f"/v1/sessions/{sid}/document-monitor"
        client.post(root + "/start", json={"client_action_id": "monitor-start"})
        first = _page(client, sid, "paper-first", "第1题 125 × 8 = 1000")
        second = _page(client, sid, "paper-second", "第1题 125 × 8 = 1000\n第2题 360 ÷ 4 = 90")
        assert second["previous_revision_id"] == first["revision_id"]
        assert client.portal.call(client.app.state.document_learning.process_pending, 2)
        context = client.get(root + "/learning-context").json()
        assert context["current_revision_id"] == second["revision_id"]
        assert context["status"] == "completed"
        assert context["cloud_transmission_allowed"] is False
        assert context["analyses"][0]["homework_progress"]["confirmed_completion"] is False
        client.app.state.companion_memory.process_pending(50)
        memories = client.app.state.companion_memory.recall("child-junior-3", "高拍仪")["memories"]
        assert memories
        assert client.app.state.companion_memory.recall("child-primary-4", "高拍仪")["memories"] == []
    with client_for(tmp_path) as client:
        context = client.get(root + "/learning-context").json()
        assert context["current_revision_id"] == second["revision_id"]
        assert client.app.state.companion_memory.recall("child-junior-3", "高拍仪")["memories"]
        deleted = client.app.state.privacy_lifecycle.delete_child_scope(
            "child-junior-3", "media", "确认删除", "delete-page-study")
        assert deleted["deleted_counts"]["document_learning_analysis"] == 2
        client.app.state.companion_memory.process_pending(50)
        assert client.app.state.companion_memory.recall("child-junior-3", "高拍仪")["memories"] == []
        assert client.get(root + "/learning-context").json()["status"] == "empty"


def test_continuous_ocr_does_not_block_pause_and_discards_late_result(tmp_path, monkeypatch):
    entered, finish = Event(), Event()
    with client_for(tmp_path) as client:
        sid, _ = advance_to_study(client, action_prefix="continuous-stop")
        root = f"/v1/sessions/{sid}/document-monitor"
        assert client.post(root + "/start", json={"client_action_id": "monitor-start"}).status_code == 200

        def slow_ocr(*args, **kwargs):
            entered.set()
            assert finish.wait(5), "pause was blocked by CPU OCR"
            return {"artifact_id": "late-result-not-a-revision"}

        monkeypatch.setattr(client.app.state.ocr_workflow, "process", slow_ocr)
        payload = {"image_base64": base64.b64encode(b"synthetic-frame" * 4).decode(), "client_action_id": "monitor-frame"}
        with ThreadPoolExecutor(max_workers=2) as pool:
            future = pool.submit(client.post, root + "/ocr", json=payload)
            try:
                assert entered.wait(3)
                busy = client.post(root + "/ocr", json={**payload, "client_action_id": "other-frame"})
                assert busy.status_code == 409
                assert "DOCUMENT_OCR_BUSY" in busy.text
                assert client.post(root + "/pause", json={"client_action_id": "monitor-pause"}).status_code == 200
            finally:
                finish.set()
            assert future.result(timeout=3).status_code == 409
        assert client.get(root).json()["latest_revision"] is None


def test_continuous_ocr_rejects_before_start_and_after_session_end(tmp_path, monkeypatch):
    with client_for(tmp_path) as client:
        sid, _ = advance_to_study(client, action_prefix="continuous-lifecycle")
        root = f"/v1/sessions/{sid}/document-monitor"
        calls = []
        monkeypatch.setattr(client.app.state.ocr_workflow, "process", lambda *args: calls.append(args))
        payload = {"image_base64": base64.b64encode(b"synthetic-frame" * 4).decode(), "client_action_id": "monitor-frame"}
        assert client.post(root + "/ocr", json=payload).status_code == 409
        assert client.post(root + "/start", json={"client_action_id": "monitor-start"}).status_code == 200
        with client.app.state.document_monitoring.database.connect() as connection:
            connection.execute("UPDATE daily_session SET state='completed',status='completed' WHERE session_id=?", (sid,))
            connection.commit()
        assert client.post(root + "/ocr", json=payload).status_code == 409
        assert calls == []


def test_low_quality_page_uses_separate_cloud_evidence_without_replacing_local_text(tmp_path, monkeypatch):
    with client_for(tmp_path) as client:
        sid, _ = advance_to_study(client, action_prefix="cloud-page")
        root = f"/v1/sessions/{sid}/document-monitor"
        client.post(root + "/start", json={"client_action_id": "monitor-start"})

        def local_ocr(session, image, action, store_raw, **kwargs):
            assert store_raw is False
            with client.app.state.document_monitoring.database.connect() as connection:
                connection.execute("""INSERT INTO capture_artifact(artifact_id,child_id,session_id,artifact_type,
                    local_path,extracted_text,confidence,details_json,status,created_at,client_action_id)
                    VALUES('vision-page','child-junior-3',?,'document_ocr',NULL,'本地低置信文字',.3,'{}',
                    'quality_warning',?,?)""", (session, datetime.now(timezone.utc).isoformat(), action))
                connection.commit()
            return client.app.state.ocr_workflow.media_ai._artifact_for_workflow('vision-page')

        calls = []
        def vision(image, **kwargs):
            assert kwargs["still_allowed"]()
            assert kwargs["session_id"] == sid
            calls.append(kwargs["request_id"])
            return {"status": "needs_review", "blocks": [{"text": "125 × 8", "confidence": .35}],
                    "can_update_mastery": False, "request_id": kwargs["request_id"]}

        monkeypatch.setattr(client.app.state.ocr_workflow, "process", local_ocr)
        monkeypatch.setattr(client.app.state.document_vision, "analyze_page", vision)
        payload = {"image_base64": base64.b64encode(b"synthetic-frame" * 4).decode(), "client_action_id": "cloud-page-action"}
        response = client.post(root + "/ocr", json=payload)
        assert response.status_code == 200, response.text
        artifact = response.json()
        assert artifact["extracted_text"] == "本地低置信文字"
        assert artifact["confidence"] == .3
        assert artifact["details"]["cloud_vision_fallback"]["can_update_mastery"] is False
        assert artifact["local_path"] is None
        assert client.post(root + "/ocr", json=payload).json()["artifact_id"] == artifact["artifact_id"]
        assert len(calls) == 1
        assert calls == [f"artifact:{artifact['artifact_id']}"]
        changed = {**payload, "image_base64": base64.b64encode(b"different-frame" * 4).decode()}
        assert client.post(root + "/ocr", json=changed).status_code == 409
        client.post(root + "/pause", json={"client_action_id": "monitor-pause"})
        client.post(root + "/resume", json={"client_action_id": "monitor-resume"})
        stale = client.post(root + "/revisions", json={"artifact_id": artifact["artifact_id"],
            "content_signature": "stale-signature", "client_action_id": "stale-page-revision"})
        assert stale.status_code == 409
        assert client.get(root).json()["latest_revision"] is None


@pytest.mark.parametrize("invalidate", ["pause", "delete"])
def test_real_ocr_persistence_cannot_outlive_monitor_epoch(tmp_path, monkeypatch, invalidate):
    entered, finish = Event(), Event()
    with client_for(tmp_path) as client:
        sid, _ = advance_to_study(client, action_prefix="ocr-epoch")
        root = f"/v1/sessions/{sid}/document-monitor"
        client.post(root + "/start", json={"client_action_id": "epoch-start"})
        generation = client.get(root).json()["generation"]
        media = client.app.state.ocr_workflow.media_ai
        def quality(image):
            entered.set()
            assert finish.wait(5)
            return {"passed": True, "issues": [], "messages": []}
        monkeypatch.setattr(media, "_image_quality", quality)
        frame = cv2.imencode(".png", np.full((160, 160, 3), 255, dtype=np.uint8))[1].tobytes()
        prepared = {"quality": {"passed": True, "issues": [], "messages": []}, "lines": [], "engine": "test"}
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(media.ocr, sid, frame, "epoch-frame", False,
                                  prepared_result=prepared, monitor_generation=generation)
            try:
                assert entered.wait(3)
                if invalidate == "pause":
                    assert client.post(root + "/pause", json={"client_action_id": "epoch-pause"}).status_code == 200
                else:
                    client.app.state.privacy_lifecycle.delete_child_scope(
                        "child-junior-3", "media", "确认删除", "delete-inflight-page")
            finally:
                finish.set()
            with pytest.raises(ValueError, match="invalidated"):
                pending.result(timeout=3)
        with media.database.connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM capture_artifact WHERE session_id=?", (sid,)).fetchone()[0] == 0


def test_first_ocr_insert_contains_retry_identity(tmp_path, monkeypatch):
    from hashlib import sha256
    with client_for(tmp_path) as client:
        sid, _ = advance_to_study(client, action_prefix="first-insert")
        root = f"/v1/sessions/{sid}/document-monitor"
        client.post(root + "/start", json={"client_action_id": "first-start"})
        generation = client.get(root).json()["generation"]
        media = client.app.state.ocr_workflow.media_ai
        monkeypatch.setattr(media, "_image_quality", lambda image: {"passed": True, "issues": [], "messages": []})
        frame = cv2.imencode(".png", np.full((160, 160, 3), 255, dtype=np.uint8))[1].tobytes()
        artifact = media.ocr(sid, frame, "first-frame", False,
            prepared_result={"quality": {"passed": True, "issues": [], "messages": []}, "lines": [], "engine": "test"},
            monitor_generation=generation)
        with media.database.connect() as connection:
            details = json.loads(connection.execute("SELECT details_json FROM capture_artifact WHERE artifact_id=?",
                (artifact["artifact_id"],)).fetchone()[0])
        assert details["monitor_generation"] == generation
        assert details["monitor_frame_sha256"] == sha256(frame).hexdigest()
