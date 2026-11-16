from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from test_app import client_for
from home_study_agent.database import Database
from home_study_agent.voice_calibration import VoiceCalibrationService, VoiceCalibrationVersionConflictError


def _profile(child_id: str) -> dict:
    return {"version": 2, "childId": child_id, "consentedAt": "2026-08-28T10:00:00Z", "updatedAt": "2026-08-28T10:00:00Z", "sampleCount": 48000, "calibrationCount": 3, "energy": 0.12, "zeroCrossingRate": 0.08, "spectralBands": [0.2, 0.3, 0.3, 0.2], "environmentSpread": 0.1}

def _microphone() -> dict:
    return {
        "system_id_hash": "a" * 64, "label": "USB Microphone",
        "capabilities": {
            "transport": "browser-media-device", "echo_cancellation_requested": True,
            "noise_suppression_requested": True, "auto_gain_control_requested": True,
            "sample_rate_hz": 48_000, "channel_count": 1,
        },
    }


def _payload(child_id: str, action_id: str = "voice-save-action-001") -> dict:
    return {"profile": _profile(child_id), "vocabulary": ["奥数", "英语", "奥数"], "microphone": _microphone(), "client_action_id": action_id}


def test_parent_voice_calibration_persists_features_and_never_accepts_audio(tmp_path) -> None:
    with client_for(tmp_path) as client:
        token = client.post('/v1/parent/setup', json={"pin": "2468"}).json()["parent_token"]
        headers = {"X-HSC-Parent": token}
        saved = client.put('/v1/parent/children/child-primary-4/voice-calibration', headers=headers, json=_payload('child-primary-4'))
        replayed = client.put('/v1/parent/children/child-primary-4/voice-calibration', headers=headers, json=_payload('child-primary-4'))
        assert saved.status_code == 200, saved.json()
        assert saved.json()["raw_audio_stored"] is False
        assert saved.json()["vocabulary"] == ["奥数", "英语"]
        assert saved.json()["vocabulary_applied"] is False
        assert saved.json()["vocabulary_status"] == "not_supported_by_current_decoder"
        assert replayed.status_code == 200 and replayed.json()["updated_at"] == saved.json()["updated_at"]
    with client_for(tmp_path) as restarted:
        token = restarted.post('/v1/parent/unlock', json={"pin": "2468"}).json()["parent_token"]
        restored = restarted.get('/v1/parent/children/child-primary-4/voice-calibration', headers={"X-HSC-Parent": token})
        deleted = restarted.request('DELETE', '/v1/parent/children/child-primary-4/voice-calibration', headers={"X-HSC-Parent": token}, json={"client_action_id": "voice-delete-action-001"})
        replayed_delete = restarted.request('DELETE', '/v1/parent/children/child-primary-4/voice-calibration', headers={"X-HSC-Parent": token}, json={"client_action_id": "voice-delete-action-001"})
    assert restored.status_code == 200 and restored.json()["profile"]["calibrationCount"] == 3
    assert restored.json()["microphone"] == _microphone()
    assert deleted.status_code == 200 and deleted.json()["status"] == 'not_calibrated'
    assert replayed_delete.status_code == 200 and replayed_delete.json()["status"] == 'not_calibrated'


def test_calibration_reports_vocabulary_as_applied_when_cloud_asr_context_is_available(tmp_path) -> None:
    database = Database(tmp_path / "voice-context.db")
    database.initialize()
    with database.connect() as connection:
        connection.execute("INSERT INTO child_profile(child_id, nickname, grade, curriculum_region, status, created_at, updated_at) VALUES ('child-primary-4', '小四', '四年级', '江苏', 'active', datetime('now'), datetime('now'))")
        connection.commit()
    service = VoiceCalibrationService(database, vocabulary_supported=True)

    saved = service.save(
        "child-primary-4", _profile("child-primary-4"), ["奥数", "PET"],
        _microphone(), "voice-context-save",
    )

    assert saved["vocabulary_applied"] is True
    assert saved["vocabulary_status"] == "cloud_asr_context_enabled"


def test_session_scoped_voice_calibration_recovers_only_its_child_after_restart(tmp_path) -> None:
    """Feature-only synthetic calibration crosses parent save -> child session -> restart.

    The fixture is a deliberately non-identifying aggregate feature profile;
    neither this endpoint nor the database receives a WAV/base64 recording.
    """
    with client_for(tmp_path) as client:
        token = client.post('/v1/parent/setup', json={"pin": "2468"}).json()["parent_token"]
        saved = client.put('/v1/parent/children/child-primary-4/voice-calibration', headers={"X-HSC-Parent": token}, json=_payload('child-primary-4', 'synthetic-feature-only-save'))
        primary_session = client.post('/v1/sessions', json={"child_id": "child-primary-4", "client_action_id": "synthetic-primary-session"}).json()["session_id"]
        # The product permits one active child-learning session at a time.
        # Mark this test session complete before opening the other child's
        # independent session; calibration itself remains untouched.
        with Database(tmp_path / 'test.db').connect() as connection:
            connection.execute("UPDATE daily_session SET status = 'completed' WHERE session_id = ?", (primary_session,))
            connection.commit()
        junior_session = client.post('/v1/sessions', json={"child_id": "child-junior-3", "client_action_id": "synthetic-junior-session"}).json()["session_id"]
        primary = client.get(f'/v1/sessions/{primary_session}/voice-calibration')
        junior = client.get(f'/v1/sessions/{junior_session}/voice-calibration')
        missing = client.get('/v1/sessions/unknown-session/voice-calibration')
    assert saved.status_code == 200
    assert primary.status_code == 200 and primary.json()["child_id"] == 'child-primary-4'
    assert primary.json()["profile"]["calibrationCount"] == 3 and primary.json()["microphone"] == _microphone()
    assert primary.json()["raw_audio_stored"] is False
    assert junior.status_code == 200 and junior.json() == {"child_id": "child-junior-3", "status": "not_calibrated", "raw_audio_stored": False, "profile": None, "vocabulary_applied": False, "vocabulary_status": "not_supported_by_current_decoder"}
    assert missing.status_code == 404 and missing.json()["code"] == 'SESSION_NOT_FOUND'
    with client_for(tmp_path) as restarted:
        restored = restarted.get(f'/v1/sessions/{primary_session}/voice-calibration')
    assert restored.status_code == 200 and restored.json()["profile"] == primary.json()["profile"]


def test_voice_calibration_rejects_malicious_features_and_unknown_child(tmp_path) -> None:
    with client_for(tmp_path) as client:
        token = client.post('/v1/parent/setup', json={"pin": "2468"}).json()["parent_token"]
        headers = {"X-HSC-Parent": token}
        invalid = client.put('/v1/parent/children/child-primary-4/voice-calibration', headers=headers, json={**_payload('child-primary-4', 'voice-save-action-002'), "profile": {**_profile('child-primary-4'), "energy": -1}, "vocabulary": ["x" * 33]})
        invalid_microphone = client.put('/v1/parent/children/child-primary-4/voice-calibration', headers=headers, json={**_payload('child-primary-4', 'voice-save-action-003'), "microphone": {**_microphone(), "unbounded_browser_blob": {"anything": ["must-not-store"]}}})
        invalid_capabilities = client.put('/v1/parent/children/child-primary-4/voice-calibration', headers=headers, json={**_payload('child-primary-4', 'voice-save-action-004'), "microphone": {**_microphone(), "capabilities": {**_microphone()["capabilities"], "sample_rate_hz": 999_999}}})
        blank_label = client.put('/v1/parent/children/child-primary-4/voice-calibration', headers=headers, json={**_payload('child-primary-4', 'voice-save-action-005'), "microphone": {**_microphone(), "label": ""}})
        unknown = client.put('/v1/parent/children/not-a-child/voice-calibration', headers=headers, json=_payload('not-a-child', 'voice-save-action-006'))
    assert invalid.status_code == 422
    assert invalid_microphone.status_code == 422
    assert invalid_capabilities.status_code == 422
    assert blank_label.status_code == 422
    assert unknown.status_code == 404 and unknown.json()["code"] == 'CHILD_NOT_FOUND'


def test_voice_calibration_save_action_replays_only_the_same_payload(tmp_path) -> None:
    with client_for(tmp_path) as client:
        token = client.post('/v1/parent/setup', json={"pin": "2468"}).json()["parent_token"]
        headers = {"X-HSC-Parent": token}
        saved = client.put('/v1/parent/children/child-primary-4/voice-calibration', headers=headers, json=_payload('child-primary-4', 'voice-save-action-unique'))
        conflict = client.put('/v1/parent/children/child-primary-4/voice-calibration', headers=headers, json={**_payload('child-primary-4', 'voice-save-action-unique'), "vocabulary": ["不同词"]})
    assert saved.status_code == 200
    assert conflict.status_code == 409 and conflict.json()["code"] == 'VOICE_CALIBRATION_ACTION_CONFLICT'


def test_voice_calibration_save_cannot_revive_a_record_deleted_after_response_loss(tmp_path) -> None:
    with client_for(tmp_path) as client:
        token = client.post('/v1/parent/setup', json={"pin": "2468"}).json()["parent_token"]
        headers = {"X-HSC-Parent": token}
        saved = client.put('/v1/parent/children/child-primary-4/voice-calibration', headers=headers, json=_payload('child-primary-4', 'voice-save-before-delete'))
        deleted = client.request('DELETE', '/v1/parent/children/child-primary-4/voice-calibration', headers=headers, json={"client_action_id": "voice-delete-after-save"})
        replay = client.put('/v1/parent/children/child-primary-4/voice-calibration', headers=headers, json=_payload('child-primary-4', 'voice-save-before-delete'))
    assert saved.status_code == 200 and deleted.status_code == 200
    assert replay.status_code == 409 and replay.json()["code"] == 'VOICE_CALIBRATION_DELETED'


def test_voice_calibration_serializes_same_action_and_rejects_new_payload(tmp_path) -> None:
    database = Database(tmp_path / "voice-concurrency.db")
    database.initialize()
    with database.connect() as connection:
        connection.execute("INSERT INTO child_profile(child_id, nickname, grade, curriculum_region, status, created_at, updated_at) VALUES ('child-primary-4', '小四', '四年级', '江苏', 'active', datetime('now'), datetime('now'))")
        connection.commit()
    service = VoiceCalibrationService(database)
    payload = _profile('child-primary-4')
    microphone = _microphone()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: service.save('child-primary-4', payload, ['奥数'], microphone, 'voice-concurrent-action'), range(2)))
    assert all(result["profile"]["calibrationCount"] == 3 for result in results)
    with pytest.raises(VoiceCalibrationVersionConflictError):
        service.save('child-primary-4', payload, ['不同词'], microphone, 'voice-new-payload-action')
