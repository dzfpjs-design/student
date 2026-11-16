"""End-to-end local confirmation checks for OCR-backed tutoring."""
from __future__ import annotations

import base64
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
import json
import socket
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cv2
import httpx
import numpy as np
import uvicorn

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.model_gateway import ModelRequest, ModelResult
from home_study_agent.tutor_request_reservation import client_request_hash, normalize_text
from test_app import FakeModelGateway, advance_to_study, client_for
from tutor_ocr_process_fixture import tutor_process_client


class BlockingModelGateway(FakeModelGateway):
    """Keeps one provider call alive while a stale retry arrives."""

    def __init__(self, model: str = "blocking-test") -> None:
        super().__init__()
        self.model = model
        self.started = threading.Event()
        self.release = threading.Event()
        self.returned = threading.Event()

    async def complete(self, messages: ModelRequest) -> ModelResult:
        self.messages.append(messages)
        self.started.set()
        assert await asyncio.to_thread(self.release.wait, 30), "gateway was not explicitly released"
        self.returned.set()
        return ModelResult(
            content="先说说题目已知什么。", provider="blocking-provider", model=self.model, latency_ms=1,
        )


@contextmanager
def _tutor_http_client(tmp_path: Path, gateway):
    application = create_app(Settings(
        data_dir=tmp_path, database_path=tmp_path / "test.db",
        data_encryption_key=b"0" * 32, maintenance_enabled=False,
    ), model_gateway=gateway)
    with closing(socket.socket()) as listener:
        listener.bind(("127.0.0.1", 0))
        server = uvicorn.Server(uvicorn.Config(application, log_level="error", access_log=False))
        failures = []

        def run():
            try:
                server.run(sockets=[listener])
            except BaseException as error:
                failures.append(error)

        worker = threading.Thread(target=run, name="tutor-reservation-http", daemon=True)
        worker.start()
        try:
            deadline = time.monotonic() + 30
            while worker.is_alive() and not server.started and time.monotonic() < deadline:
                time.sleep(.02)
            assert server.started and not failures, failures
            with httpx.Client(
                base_url=f"http://127.0.0.1:{listener.getsockname()[1]}", trust_env=False, timeout=30,
            ) as client:
                yield client
        finally:
            server.should_exit = True
            worker.join(10)
            if worker.is_alive():
                server.force_exit = True
                worker.join(5)
            assert not worker.is_alive(), "owned Tutor HTTP server did not drain"
            assert not failures, failures


class PreSubmissionGateway(FakeModelGateway):
    configured = False


def _encoded_image() -> str:
    image = np.full((300, 1000, 3), 255, dtype=np.uint8)
    cv2.putText(image, "12 + 8 = ?", (75, 175), cv2.FONT_HERSHEY_SIMPLEX, 2.0, (0, 0, 0), 4, cv2.LINE_AA)
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    return base64.b64encode(encoded.tobytes()).decode("ascii")


def _new_artifact(client, session_id: str, action_id: str) -> dict:
    response = client.post(f"/v1/sessions/{session_id}/ocr", json={"image_base64": _encoded_image(), "client_action_id": action_id})
    assert response.status_code == 200, response.json()
    artifact = response.json()
    assert artifact["extracted_text"].strip(), artifact
    assert artifact["local_path"] is None, artifact
    return artifact


def _confirm(client, session_id: str, artifact_id: str, text: str, kind: str, action_id: str) -> dict:
    response = client.post(f"/v1/sessions/{session_id}/tutor-ocr-confirmations", json={
        "artifact_id": artifact_id, "confirmed_text": text, "confirmation_kind": kind, "client_action_id": action_id,
    })
    assert response.status_code == 200, response.json()
    return response.json()


def test_manual_low_quality_correction_is_durable_and_overrides_request_question(tmp_path: Path) -> None:
    gateway = FakeModelGateway()
    with client_for(tmp_path, model_gateway=gateway) as client:
        session_id, plan_item_id = advance_to_study(client, child_id="child-primary-4", action_prefix="ocr-confirm-manual")
        artifact = _new_artifact(client, session_id, "ocr-confirm-manual-artifact")
        # The local OCR text remains available but its automatic quality assessment is low.
        with sqlite3.connect(tmp_path / "test.db") as connection:
            connection.execute("UPDATE capture_artifact SET status = 'quality_warning', confidence = 0.41 WHERE artifact_id = ?", (artifact["artifact_id"],))
            connection.commit()
        confirmed_text = "12加8等于多少？"
        confirmation = _confirm(client, session_id, artifact["artifact_id"], confirmed_text, "manual_correction", "ocr-confirm-manual-save")
        allowed = client.post(f"/v1/sessions/{session_id}/tutor", json={
            "subject": "数学", "question": "伪造的替换题干：999乘999", "hint_level": 1,
            "ocr_confirmation_id": confirmation["confirmation_id"], "client_action_id": "ocr-confirm-manual-tutor",
        })

    assert allowed.status_code == 200, allowed.json()
    assert allowed.json()["plan_item_id"] == plan_item_id
    assert allowed.json()["question"] == confirmed_text
    prompt = gateway.messages[0].messages[-1]["content"]
    assert confirmed_text in prompt
    assert "999乘999" not in prompt
    manifest = gateway.messages[0].context_manifest
    assert manifest["raw_audio_or_video"] is False
    assert manifest["ocr_confirmation"]["confirmation_kind"] == "manual_correction"
    assert confirmation["confirmed_text_hash"] != confirmed_text


def test_red_marked_question_candidate_can_be_verified_without_confirming_the_whole_page(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        session_id, _ = advance_to_study(client, child_id="child-primary-4", action_prefix="ocr-marked-question")
        artifact = _new_artifact(client, session_id, "ocr-marked-question-artifact")
        candidate_text = "2. This is the marked wrong question"
        with sqlite3.connect(tmp_path / "test.db") as connection:
            details = json.loads(connection.execute(
                "SELECT details_json FROM capture_artifact WHERE artifact_id = ?", (artifact["artifact_id"],)
            ).fetchone()[0])
            details.setdefault("local_pipeline", {})["wrong_question_candidates"] = [{
                "text": candidate_text, "confidence": .91, "ocr_confidence": .9,
                "bbox_xywh": [10, 20, 300, 40], "red_pixel_count": 80,
                "red_mark_count": 1, "evidence": "red_correction_mark_near_question",
                "requires_child_confirmation": True,
            }]
            connection.execute(
                "UPDATE capture_artifact SET details_json = ? WHERE artifact_id = ?",
                (json.dumps(details), artifact["artifact_id"]),
            )
            connection.commit()

        confirmed = _confirm(
            client, session_id, artifact["artifact_id"], candidate_text,
            "ocr_verified", "ocr-marked-question-confirm",
        )

    assert confirmed["confirmed_text"] == candidate_text
    assert confirmed["confirmation_kind"] == "ocr_verified"


def test_confirmation_rejects_unreadable_and_non_ocr_artifacts(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        session_id = client.post("/v1/sessions", json={"child_id": "child-primary-4", "client_action_id": "ocr-confirm-deny-session"}).json()["session_id"]
        artifact = _new_artifact(client, session_id, "ocr-confirm-deny-artifact")
        with sqlite3.connect(tmp_path / "test.db") as connection:
            connection.execute("UPDATE capture_artifact SET extracted_text = '' WHERE artifact_id = ?", (artifact["artifact_id"],))
            connection.commit()
        unreadable = client.post(f"/v1/sessions/{session_id}/tutor-ocr-confirmations", json={
            "artifact_id": artifact["artifact_id"], "confirmed_text": "12加8等于多少？", "confirmation_kind": "manual_correction", "client_action_id": "ocr-confirm-unreadable",
        })
        with sqlite3.connect(tmp_path / "test.db") as connection:
            connection.execute("UPDATE capture_artifact SET extracted_text = '12 + 8 = ?', artifact_type = 'completion_photo' WHERE artifact_id = ?", (artifact["artifact_id"],))
            connection.commit()
        non_ocr = client.post(f"/v1/sessions/{session_id}/tutor-ocr-confirmations", json={
            "artifact_id": artifact["artifact_id"], "confirmed_text": "12加8等于多少？", "confirmation_kind": "manual_correction", "client_action_id": "ocr-confirm-non-ocr",
        })

    assert unreadable.status_code == 422 and unreadable.json()["code"] == "OCR_TEXT_UNREADABLE"
    assert non_ocr.status_code == 422 and non_ocr.json()["code"] == "OCR_ARTIFACT_TYPE_DENIED"


def test_confirmation_idempotency_is_bound_to_session_artifact_text_and_kind(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        first_session = client.post("/v1/sessions", json={"child_id": "child-primary-4", "client_action_id": "ocr-action-first-session"}).json()["session_id"]
        first_artifact = _new_artifact(client, first_session, "ocr-action-first-artifact")
        source_text = first_artifact["extracted_text"]
        action_id = "ocr-confirm-shared-action"
        first = _confirm(client, first_session, first_artifact["artifact_id"], source_text, "ocr_verified", action_id)
        repeated = client.post(f"/v1/sessions/{first_session}/tutor-ocr-confirmations", json={
            "artifact_id": first_artifact["artifact_id"], "confirmed_text": source_text, "confirmation_kind": "ocr_verified", "client_action_id": action_id,
        })
        altered_text = client.post(f"/v1/sessions/{first_session}/tutor-ocr-confirmations", json={
            "artifact_id": first_artifact["artifact_id"], "confirmed_text": f"{source_text} 改写", "confirmation_kind": "ocr_verified", "client_action_id": action_id,
        })
        # Allow a second active test session for a different child, then replay
        # the same action id against that child's artifact.
        with sqlite3.connect(tmp_path / "test.db") as connection:
            connection.execute("UPDATE daily_session SET status = 'completed' WHERE session_id = ?", (first_session,))
            connection.commit()
        second_session = client.post("/v1/sessions", json={"child_id": "child-junior-3", "client_action_id": "ocr-action-second-session"}).json()["session_id"]
        second_artifact = _new_artifact(client, second_session, "ocr-action-second-artifact")
        cross_child = client.post(f"/v1/sessions/{second_session}/tutor-ocr-confirmations", json={
            "artifact_id": second_artifact["artifact_id"], "confirmed_text": second_artifact["extracted_text"], "confirmation_kind": "ocr_verified", "client_action_id": action_id,
        })
        modified_verified = client.post(f"/v1/sessions/{second_session}/tutor-ocr-confirmations", json={
            "artifact_id": second_artifact["artifact_id"], "confirmed_text": f"{second_artifact['extracted_text']} 改写", "confirmation_kind": "ocr_verified", "client_action_id": "ocr-confirm-modified-verified",
        })

    assert repeated.status_code == 200 and repeated.json()["confirmation_id"] == first["confirmation_id"]
    assert altered_text.status_code == 409 and altered_text.json()["code"] == "OCR_CONFIRMATION_ACTION_CONFLICT"
    assert cross_child.status_code == 409 and cross_child.json()["code"] == "OCR_CONFIRMATION_ACTION_CONFLICT"
    assert modified_verified.status_code == 422 and modified_verified.json()["code"] == "OCR_VERIFIED_TEXT_MISMATCH"


def test_confirmation_rejects_forgery_cross_session_and_survives_restart(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        first_session, _ = advance_to_study(client, child_id="child-primary-4", action_prefix="ocr-confirm-first-session")
        artifact = _new_artifact(client, first_session, "ocr-confirm-first-artifact")
        verified_text = artifact["extracted_text"]
        confirmation = _confirm(client, first_session, artifact["artifact_id"], verified_text, "ocr_verified", "ocr-confirm-first-save")
        forged = client.post(f"/v1/sessions/{first_session}/tutor", json={
            "subject": "数学", "question": "任意题干", "ocr_confirmation_id": "not-a-real-confirmation", "client_action_id": "ocr-confirm-forged",
        })
        unknown_session = client.post("/v1/sessions/not-this-session/tutor", json={
            "subject": "数学", "question": "任意题干", "ocr_confirmation_id": confirmation["confirmation_id"], "client_action_id": "ocr-confirm-cross",
        })
        # Verify actual ownership isolation separately from unknown-session
        # rejection; checking the host first must not expose confirmation state.
        with sqlite3.connect(tmp_path / "test.db") as connection:
            connection.execute("UPDATE daily_session SET status='completed' WHERE session_id=?", (first_session,))
            connection.commit()
        other_session, _ = advance_to_study(client, child_id="child-junior-3", action_prefix="ocr-confirm-other-session")
        cross_session = client.post(f"/v1/sessions/{other_session}/tutor", json={
            "subject": "数学", "question": "任意题干", "ocr_confirmation_id": confirmation["confirmation_id"], "client_action_id": "ocr-confirm-real-cross",
        })
        # Return the fixture to the original active host for restart validation,
        # never create two concurrent active sessions against product rules.
        with sqlite3.connect(tmp_path / "test.db") as connection:
            connection.execute("UPDATE daily_session SET status='completed' WHERE session_id=?", (other_session,))
            connection.execute("UPDATE daily_session SET status='active' WHERE session_id=?", (first_session,))
            connection.commit()

    gateway = FakeModelGateway()
    with client_for(tmp_path, model_gateway=gateway) as restarted:
        # Confirmation remains local in SQLite across service restart; a new tutor action can resolve it.
        resumed = restarted.post(f"/v1/sessions/{first_session}/tutor", json={
            "subject": "数学", "question": "不能覆盖已确认文本", "ocr_confirmation_id": confirmation["confirmation_id"], "client_action_id": "ocr-confirm-after-restart",
        })
        with sqlite3.connect(tmp_path / "test.db") as connection:
            connection.execute("UPDATE tutor_ocr_confirmation SET expires_at = '2000-01-01T00:00:00+00:00' WHERE confirmation_id = ?", (confirmation["confirmation_id"],))
            connection.commit()
        expired = restarted.post(f"/v1/sessions/{first_session}/tutor", json={
            "subject": "数学", "question": "不能绕过过期确认", "ocr_confirmation_id": confirmation["confirmation_id"], "client_action_id": "ocr-confirm-expired",
        })

    assert forged.status_code == 422 and forged.json()["code"] == "OCR_CONFIRMATION_NOT_FOUND"
    assert unknown_session.status_code == 404
    assert cross_session.status_code == 422 and cross_session.json()["code"] == "OCR_CONFIRMATION_SESSION_MISMATCH"
    assert resumed.status_code == 200, resumed.json()
    assert resumed.json()["question"] == verified_text
    assert expired.status_code == 422 and expired.json()["code"] == "OCR_CONFIRMATION_EXPIRED"


def test_completed_tutor_action_replays_once_and_rejects_other_request_shapes(tmp_path: Path) -> None:
    gateway = FakeModelGateway()
    with client_for(tmp_path, model_gateway=gateway) as client:
        session_id, _ = advance_to_study(client, child_id="child-primary-4", action_prefix="tutor-once")
        artifact = _new_artifact(client, session_id, "tutor-once-artifact")
        confirmation = _confirm(client, session_id, artifact["artifact_id"], artifact["extracted_text"], "ocr_verified", "tutor-once-confirm")
        payload = {
            "subject": "数学", "question": "已确认的本地题干", "hint_level": 1,
            "ocr_confirmation_id": confirmation["confirmation_id"], "client_action_id": "tutor-once-action",
        }
        first = client.post(f"/v1/sessions/{session_id}/tutor", json=payload)
        replayed = client.post(f"/v1/sessions/{session_id}/tutor", json=payload)
        altered = client.post(f"/v1/sessions/{session_id}/tutor", json={**payload, "subject": "语文"})
        cross_session = client.post("/v1/sessions/not-this-session/tutor", json=payload)

    assert first.status_code == 200, first.json()
    assert replayed.status_code == 200 and replayed.json()["turn_id"] == first.json()["turn_id"]
    assert len(gateway.messages) == 1
    assert altered.status_code == 409 and altered.json()["code"] == "TUTOR_ACTION_CONFLICT"
    # The reservation fingerprint rejects an already-completed action reused
    # in another session before it reaches that session's OCR resolver.
    assert cross_session.status_code == 409 and cross_session.json()["code"] == "TUTOR_ACTION_CONFLICT"


class BlockingTutorGateway:
    configured = True

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    async def complete(self, request: ModelRequest) -> ModelResult:
        self.calls += 1
        self.started.set()
        await __import__("asyncio").to_thread(self.release.wait, 5)
        return ModelResult(
            content="先圈出题目给出的两个数，再想想它们之间要做什么运算。检查题：你先找到哪两个数？",
            provider="fake-provider", model="blocking-tutor", latency_ms=10, prompt_tokens=8, completion_tokens=8,
        )


def test_concurrent_tutor_retries_claim_one_model_call(tmp_path: Path) -> None:
    gateway = BlockingTutorGateway()
    with client_for(tmp_path, model_gateway=gateway) as client:
        session_id, _ = advance_to_study(client, child_id="child-primary-4", action_prefix="tutor-reservation-race")
        payload = {
            "subject": "数学", "question": "12加8等于多少？", "hint_level": 1,
            "client_action_id": "tutor-reservation-race-action",
        }
        with ThreadPoolExecutor(max_workers=1) as executor:
            first = executor.submit(client.post, f"/v1/sessions/{session_id}/tutor", json=payload)
            assert gateway.started.wait(3), "first request did not enter the model gateway"
            second = client.post(f"/v1/sessions/{session_id}/tutor", json=payload)
            gateway.release.set()
            first_response = first.result(timeout=5)

    assert first_response.status_code == 200, first_response.json()
    assert second.status_code == 409 and second.json()["code"] == "TUTOR_REQUEST_IN_PROGRESS"
    assert gateway.calls == 1


def test_completed_replay_survives_restart_and_expired_ocr_confirmation(tmp_path: Path) -> None:
    gateway = FakeModelGateway()
    with client_for(tmp_path, model_gateway=gateway) as client:
        session_id, _ = advance_to_study(client, child_id="child-primary-4", action_prefix="tutor-reservation-restart")
        artifact = _new_artifact(client, session_id, "tutor-reservation-restart-artifact")
        confirmation = _confirm(client, session_id, artifact["artifact_id"], artifact["extracted_text"], "ocr_verified", "tutor-reservation-restart-confirm")
        payload = {
            "subject": "数学", "question": "客户端不可信的题干", "hint_level": 1,
            "ocr_confirmation_id": confirmation["confirmation_id"], "client_action_id": "tutor-reservation-restart-action",
        }
        first = client.post(f"/v1/sessions/{session_id}/tutor", json=payload)
        with sqlite3.connect(tmp_path / "test.db") as connection:
            connection.execute("UPDATE tutor_ocr_confirmation SET expires_at = '2000-01-01T00:00:00+00:00' WHERE confirmation_id = ?", (confirmation["confirmation_id"],))
            connection.commit()

    with client_for(tmp_path, model_gateway=FakeModelGateway()) as restarted:
        replayed = restarted.post(f"/v1/sessions/{session_id}/tutor", json=payload)

    assert first.status_code == 200, first.json()
    assert replayed.status_code == 200 and replayed.json()["turn_id"] == first.json()["turn_id"]


def test_saved_turn_repairs_pending_reservation_after_restart_without_second_gateway_call(tmp_path: Path) -> None:
    first_gateway = FakeModelGateway()
    with client_for(tmp_path, model_gateway=first_gateway) as client:
        session_id, _ = advance_to_study(client, child_id="child-primary-4", action_prefix="tutor-reservation-save-window")
        artifact = _new_artifact(client, session_id, "tutor-reservation-save-window-artifact")
        confirmation = _confirm(client, session_id, artifact["artifact_id"], artifact["extracted_text"], "ocr_verified", "tutor-reservation-save-window-confirm")
        payload = {
            "subject": "数学", "question": "客户端占位题干", "hint_level": 1,
            "ocr_confirmation_id": confirmation["confirmation_id"], "client_action_id": "tutor-reservation-save-window-action",
        }
        first = client.post(f"/v1/sessions/{session_id}/tutor", json=payload)
        # Simulate a kill between SQLite tutor_turn commit and
        # reservation.complete().  The saved turn must repair the reservation
        # before any stale-pending retry can reach the gateway.
        with sqlite3.connect(tmp_path / "test.db") as connection:
            connection.execute(
                "UPDATE tutor_request_reservation SET status = 'pending', tutor_turn_id = NULL, updated_at = ? WHERE action_id = ?",
                ((datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat(), payload["client_action_id"]),
            )
            connection.execute(
                "UPDATE tutor_request_attempt SET status = 'pending', finished_at = NULL WHERE action_id = ? AND attempt = 1",
                (payload["client_action_id"],),
            )
            connection.commit()

    replay_gateway = FakeModelGateway()
    with client_for(tmp_path, model_gateway=replay_gateway) as restarted:
        replayed = restarted.post(f"/v1/sessions/{session_id}/tutor", json=payload)
        with sqlite3.connect(tmp_path / "test.db") as connection:
            repaired = connection.execute(
                "SELECT status, tutor_turn_id FROM tutor_request_reservation WHERE action_id = ?", (payload["client_action_id"],)
            ).fetchone()

    assert first.status_code == 200, first.json()
    assert replayed.status_code == 200 and replayed.json()["turn_id"] == first.json()["turn_id"]
    assert replay_gateway.messages == []
    assert repaired == ("completed", first.json()["turn_id"])


def test_gateway_submission_after_restart_requires_visible_confirmation_before_retry(tmp_path: Path) -> None:
    gateway = FakeModelGateway()
    with client_for(tmp_path, model_gateway=gateway) as client:
        session_id, _ = advance_to_study(client, child_id="child-primary-4", action_prefix="tutor-reservation-unknown")
        payload = {"subject": "数学", "question": "17减9等于多少？", "hint_level": 1, "client_action_id": "tutor-reservation-unknown-action"}
        request_hash = client_request_hash(session_id, payload["subject"], payload["question"], 1, None, None)
        old = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
        with sqlite3.connect(tmp_path / "test.db") as connection:
            child_id = connection.execute("SELECT child_id FROM daily_session WHERE session_id = ?", (session_id,)).fetchone()[0]
            connection.execute(
                """INSERT INTO tutor_request_reservation(
                  action_id, session_id, child_id, subject, normalized_question, hint_level, parent_turn_id,
                  ocr_confirmation_id, request_hash, status, tutor_turn_id, attempts, last_error_code, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 1, NULL, NULL, ?, 'pending', NULL, 1, 'GATEWAY_SUBMISSION_STARTED', ?, ?)""",
                (payload["client_action_id"], session_id, child_id, payload["subject"], normalize_text(payload["question"]), request_hash, old, old),
            )
            connection.execute(
                "INSERT INTO tutor_request_attempt(action_id, attempt, status, error_code, started_at, finished_at) VALUES (?, 1, 'pending', NULL, ?, NULL)",
                (payload["client_action_id"], old),
            )
            connection.commit()
        unconfirmed = client.post(f"/v1/sessions/{session_id}/tutor", json=payload)
        ordinary_retry = client.post(f"/v1/sessions/{session_id}/tutor", json=payload)

    assert unconfirmed.status_code == 409 and unconfirmed.json()["code"] == "TUTOR_RESULT_UNCONFIRMED"
    assert "可能会再次请求模型" in unconfirmed.json()["message"]
    assert ordinary_retry.status_code == 409 and ordinary_retry.json()["code"] == "TUTOR_RESULT_UNCONFIRMED"
    assert gateway.messages == []

    restarted_gateway = FakeModelGateway()
    with client_for(tmp_path, model_gateway=restarted_gateway) as restarted:
        after_restart_ordinary_retry = restarted.post(f"/v1/sessions/{session_id}/tutor", json=payload)
        confirmed_retry = restarted.post(f"/v1/sessions/{session_id}/tutor", json={**payload, "confirm_unknown_outcome_retry": True})

    assert after_restart_ordinary_retry.status_code == 409 and after_restart_ordinary_retry.json()["code"] == "TUTOR_RESULT_UNCONFIRMED"
    assert confirmed_retry.status_code == 200, confirmed_retry.json()
    assert len(restarted_gateway.messages) == 1


def test_recent_unpersisted_model_result_stays_pending_until_stale(tmp_path: Path) -> None:
    gateway = FakeModelGateway()
    with client_for(tmp_path, model_gateway=gateway) as client:
        session_id, _ = advance_to_study(client, child_id="child-primary-4", action_prefix="tutor-reservation-fresh-unknown")
        payload = {"subject": "数学", "question": "14减7等于多少？", "hint_level": 1, "client_action_id": "tutor-reservation-fresh-unknown-action"}
        request_hash = client_request_hash(session_id, payload["subject"], payload["question"], 1, None, None)
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(tmp_path / "test.db") as connection:
            child_id = connection.execute("SELECT child_id FROM daily_session WHERE session_id = ?", (session_id,)).fetchone()[0]
            connection.execute(
                """INSERT INTO tutor_request_reservation(
                  action_id, session_id, child_id, subject, normalized_question, hint_level, parent_turn_id,
                  ocr_confirmation_id, request_hash, status, tutor_turn_id, attempts, last_error_code, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 1, NULL, NULL, ?, 'pending', NULL, 1, 'MODEL_RESULT_RECEIVED_UNPERSISTED', ?, ?)""",
                (payload["client_action_id"], session_id, child_id, payload["subject"], normalize_text(payload["question"]), request_hash, now, now),
            )
            connection.execute("INSERT INTO tutor_request_attempt(action_id, attempt, status, error_code, started_at, finished_at) VALUES (?, 1, 'pending', NULL, ?, NULL)", (payload["client_action_id"], now))
            connection.commit()
        pending = client.post(f"/v1/sessions/{session_id}/tutor", json=payload)

    assert pending.status_code == 409 and pending.json()["code"] == "TUTOR_REQUEST_IN_PROGRESS"
    assert gateway.messages == []


def test_gateway_still_running_past_stale_window_is_not_called_twice(tmp_path: Path) -> None:
    gateway = BlockingModelGateway()
    with tutor_process_client(tmp_path, gateway) as client, tutor_process_client(tmp_path, gateway) as retry_client:
        session_id, _ = advance_to_study(client, child_id="child-primary-4", action_prefix="tutor-reservation-running")
        payload = {"subject": "数学", "question": "18减7等于多少？", "hint_level": 1, "client_action_id": "tutor-reservation-running-action"}
        with ThreadPoolExecutor(max_workers=1) as executor:
            original = executor.submit(lambda: client.post(f"/v1/sessions/{session_id}/tutor", json=payload))
            try:
                assert gateway.started.wait(timeout=10), "gateway was not submitted"
                # V15's fake returned after five seconds during second-app startup,
                # refreshing updated_at before the stale retry could observe it.
                assert not gateway.returned.wait(5.2), "synthetic gateway returned before explicit release"
                for confirmed in (False, True):
                    pending = retry_client.post(f"/v1/sessions/{session_id}/tutor",
                        json={**payload, "confirm_unknown_outcome_retry": confirmed})
                    assert pending.status_code == 409 and pending.json()["code"] == "TUTOR_REQUEST_IN_PROGRESS"
                old = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
                with closing(sqlite3.connect(tmp_path / "test.db")) as connection:
                    assert connection.execute(
                        "SELECT status,attempts,last_error_code FROM tutor_request_reservation WHERE action_id=?",
                        (payload["client_action_id"],),
                    ).fetchone() == ("pending", 1, "GATEWAY_SUBMISSION_STARTED")
                    # This revision's durable expiry is updated_at, not a separate lease.
                    connection.execute("UPDATE tutor_request_reservation SET updated_at=? WHERE action_id=?",
                        (old, payload["client_action_id"]))
                    connection.commit()
                ordinary_retry = retry_client.post(f"/v1/sessions/{session_id}/tutor", json=payload)
                assert ordinary_retry.status_code == 409 and ordinary_retry.json()["code"] == "TUTOR_RESULT_UNCONFIRMED"
                assert not gateway.returned.is_set() and len(gateway.messages) == 1
            finally:
                gateway.release.set()
                original_result = original.result(timeout=15)

    assert original_result.status_code == 200, original_result.json()
    assert len(gateway.messages) == 1
    with closing(sqlite3.connect(tmp_path / "test.db")) as connection:
        assert connection.execute("SELECT status,attempts FROM tutor_request_reservation WHERE action_id=?", (payload["client_action_id"],)).fetchone() == ("completed", 1)
        assert connection.execute("SELECT status FROM tutor_request_attempt WHERE action_id=?", (payload["client_action_id"],)).fetchone() == ("completed",)


def test_old_tutor_result_cannot_overwrite_explicit_retry_in_flight(tmp_path: Path) -> None:
    first_gateway = BlockingModelGateway("old-attempt")
    second_gateway = BlockingModelGateway("confirmed-retry")
    with tutor_process_client(tmp_path, first_gateway) as client, tutor_process_client(tmp_path, second_gateway) as retry_client:
        session_id, _ = advance_to_study(client, child_id="child-primary-4", action_prefix="attempt-fence")
        payload = {"subject": "数学", "question": "18减7等于多少？", "hint_level": 1, "client_action_id": "attempt-fence"}
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(client.post, f"/v1/sessions/{session_id}/tutor", json=payload)
            second = None
            try:
                assert first_gateway.started.wait(10)
                with closing(sqlite3.connect(tmp_path / "test.db")) as connection:
                    assert connection.execute(
                        "SELECT status,attempts,last_error_code FROM tutor_request_reservation WHERE action_id=?",
                        (payload["client_action_id"],),
                    ).fetchone() == ("pending", 1, "GATEWAY_SUBMISSION_STARTED")
                    connection.execute("UPDATE tutor_request_reservation SET updated_at=? WHERE action_id=?",
                        ((datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat(), payload["client_action_id"]))
                    connection.commit()
                for _ in range(2):
                    unknown = retry_client.post(f"/v1/sessions/{session_id}/tutor", json=payload)
                    assert unknown.status_code == 409 and unknown.json()["code"] == "TUTOR_RESULT_UNCONFIRMED"
                assert not second_gateway.started.is_set() and not first_gateway.returned.is_set()
                with closing(sqlite3.connect(tmp_path / "test.db")) as connection:
                    assert connection.execute(
                        "SELECT status,attempts,last_error_code FROM tutor_request_reservation WHERE action_id=?",
                        (payload["client_action_id"],),
                    ).fetchone() == ("interrupted", 1, "INTERRUPTED_UNKNOWN_OUTCOME")
                second = executor.submit(retry_client.post, f"/v1/sessions/{session_id}/tutor",
                    json={**payload, "confirm_unknown_outcome_retry": True})
                assert second_gateway.started.wait(10)
                first_gateway.release.set()
                first_result = first.result(15)
                assert first_result.status_code == 409, first_result.json()
                assert first_result.json()["code"] == "TUTOR_SOURCE_AUTHORIZATION_CHANGED"
                with closing(sqlite3.connect(tmp_path / "test.db")) as connection:
                    assert connection.execute("SELECT status,attempts FROM tutor_request_reservation WHERE action_id=?", (payload["client_action_id"],)).fetchone() == ("pending", 2)
                    assert connection.execute("SELECT COUNT(*) FROM tutor_turn WHERE client_action_id=?", (payload["client_action_id"],)).fetchone()[0] == 0
                assert not second_gateway.returned.is_set()
            finally:
                first_gateway.release.set()
                second_gateway.release.set()
                first.result(15)
                if second is not None:
                    second_result = second.result(15)
            assert second_result.status_code == 200, second_result.json()
            assert second_result.json()["model"] == "confirmed-retry"
    replay_gateway = FakeModelGateway()
    with tutor_process_client(tmp_path, replay_gateway) as restarted:
        replay = restarted.post(f"/v1/sessions/{session_id}/tutor", json=payload)
        assert replay.status_code == 200 and replay.json() == second_result.json()
    assert replay_gateway.messages == []
    assert len(first_gateway.messages) == len(second_gateway.messages) == 1
    with closing(sqlite3.connect(tmp_path / "test.db")) as connection:
        assert connection.execute("SELECT attempt,status FROM tutor_request_attempt WHERE action_id=? ORDER BY attempt", (payload["client_action_id"],)).fetchall() == [(1, "interrupted"), (2, "completed")]
        assert connection.execute("SELECT turn_id,model FROM tutor_turn WHERE client_action_id=?", (payload["client_action_id"],)).fetchall() == [(second_result.json()["turn_id"], "confirmed-retry")]


def test_deterministic_pre_submission_failure_may_retry_without_confirmation(tmp_path: Path) -> None:
    gateway = PreSubmissionGateway()
    with client_for(tmp_path, model_gateway=gateway) as client:
        session_id, _ = advance_to_study(client, child_id="child-primary-4", action_prefix="tutor-reservation-presubmit")
        payload = {"subject": "数学", "question": "11加4等于多少？", "hint_level": 1, "client_action_id": "tutor-reservation-presubmit-action"}
        unavailable = client.post(f"/v1/sessions/{session_id}/tutor", json=payload)
        gateway.configured = True
        retried = client.post(f"/v1/sessions/{session_id}/tutor", json=payload)

    assert unavailable.status_code == 503 and unavailable.json()["code"] == "MODEL_NOT_CONFIGURED"
    assert retried.status_code == 200, retried.json()
    assert len(gateway.messages) == 1


def test_reservation_rejects_different_ocr_confirmation_and_requires_confirmation_for_legacy_stale_pending(tmp_path: Path) -> None:
    gateway = FakeModelGateway()
    with client_for(tmp_path, model_gateway=gateway) as client:
        session_id, _ = advance_to_study(client, child_id="child-primary-4", action_prefix="tutor-reservation-fingerprint")
        first_artifact = _new_artifact(client, session_id, "tutor-reservation-first-artifact")
        second_artifact = _new_artifact(client, session_id, "tutor-reservation-second-artifact")
        first_confirmation = _confirm(client, session_id, first_artifact["artifact_id"], first_artifact["extracted_text"], "ocr_verified", "tutor-reservation-first-confirm")
        second_confirmation = _confirm(client, session_id, second_artifact["artifact_id"], second_artifact["extracted_text"], "ocr_verified", "tutor-reservation-second-confirm")
        payload = {
            "subject": "数学", "question": "题干由确认记录决定", "hint_level": 1,
            "ocr_confirmation_id": first_confirmation["confirmation_id"], "client_action_id": "tutor-reservation-fingerprint-action",
        }
        assert client.post(f"/v1/sessions/{session_id}/tutor", json=payload).status_code == 200
        different_confirmation = client.post(f"/v1/sessions/{session_id}/tutor", json={**payload, "ocr_confirmation_id": second_confirmation["confirmation_id"]})

        stale_payload = {
            "subject": "数学", "question": "9加6等于多少？", "hint_level": 1,
            "client_action_id": "tutor-reservation-stale-action",
        }
        request_hash = client_request_hash(session_id, "数学", stale_payload["question"], 1, None, None)
        old = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
        with sqlite3.connect(tmp_path / "test.db") as connection:
            child_id = connection.execute("SELECT child_id FROM daily_session WHERE session_id = ?", (session_id,)).fetchone()[0]
            connection.execute(
                """INSERT INTO tutor_request_reservation(
                  action_id, session_id, child_id, subject, normalized_question, hint_level, parent_turn_id,
                  ocr_confirmation_id, request_hash, status, tutor_turn_id, attempts, last_error_code, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 1, NULL, NULL, ?, 'pending', NULL, 1, NULL, ?, ?)""",
                (stale_payload["client_action_id"], session_id, child_id, "数学", normalize_text(stale_payload["question"]), request_hash, old, old),
            )
            connection.execute(
                "INSERT INTO tutor_request_attempt(action_id, attempt, status, error_code, started_at, finished_at) VALUES (?, 1, 'pending', NULL, ?, NULL)",
                (stale_payload["client_action_id"], old),
            )
            connection.commit()
        ordinary_retry = client.post(f"/v1/sessions/{session_id}/tutor", json=stale_payload)
        recovered = client.post(f"/v1/sessions/{session_id}/tutor", json={**stale_payload, "confirm_unknown_outcome_retry": True})
        with sqlite3.connect(tmp_path / "test.db") as connection:
            reservation = connection.execute("SELECT status, attempts FROM tutor_request_reservation WHERE action_id = ?", (stale_payload["client_action_id"],)).fetchone()
            attempts = connection.execute("SELECT attempt, status, error_code FROM tutor_request_attempt WHERE action_id = ? ORDER BY attempt", (stale_payload["client_action_id"],)).fetchall()

    assert different_confirmation.status_code == 409 and different_confirmation.json()["code"] == "TUTOR_ACTION_CONFLICT"
    assert ordinary_retry.status_code == 409 and ordinary_retry.json()["code"] == "TUTOR_RESULT_UNCONFIRMED"
    assert recovered.status_code == 200, recovered.json()
    assert reservation == ("completed", 2)
    assert attempts == [(1, "interrupted", "INTERRUPTED_UNKNOWN_OUTCOME"), (2, "completed", None)]
