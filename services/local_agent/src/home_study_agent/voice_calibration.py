"""Local-only child voice-calibration configuration.

This service intentionally accepts derived feature summaries only.  It has no
audio/blob column and no endpoint that can receive an audio payload.
"""
from __future__ import annotations

import json
import math
import hashlib
from .database import Database
from .repository import utc_now


class VoiceCalibrationService:
    def __init__(self, database: Database, vocabulary_supported: bool = False):
        self.database = database
        self.vocabulary_supported = vocabulary_supported

    def _vocabulary_state(self) -> dict:
        return {
            "vocabulary_applied": self.vocabulary_supported,
            "vocabulary_status": "cloud_asr_context_enabled" if self.vocabulary_supported else "not_supported_by_current_decoder",
        }

    def summary(self, child_id: str) -> dict:
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM child_voice_calibration WHERE child_id = ?", (child_id,)).fetchone()
            return self._row(row) if row else {"child_id": child_id, "status": "not_calibrated", "raw_audio_stored": False, "profile": None, **self._vocabulary_state()}

    def save(self, child_id: str, profile: dict, vocabulary: list[str], microphone: dict, client_action_id: str) -> dict:
        if profile.get("calibrationCount") != 3:
            raise VoiceCalibrationInputError("请完成三段本地校准后再保存。")
        if not all(isinstance(profile.get(key), (int, float)) and math.isfinite(profile[key]) for key in ("energy", "zeroCrossingRate", "environmentSpread")):
            raise VoiceCalibrationInputError("本机声音特征无效，请重新校准。")
        if not 0 < profile["energy"] <= 1 or not 0 <= profile["zeroCrossingRate"] <= 1 or not 0 <= profile["environmentSpread"] <= 1:
            raise VoiceCalibrationInputError("本机声音特征超出合理范围，请重新校准。")
        bands = profile.get("spectralBands")
        if not isinstance(bands, list) or len(bands) != 4 or not all(isinstance(value, (int, float)) and math.isfinite(value) and 0 <= value <= 1 for value in bands) or not 0.95 <= sum(bands) <= 1.05:
            raise VoiceCalibrationInputError("本机声音特征无效，请重新校准。")
        clean_words = list(dict.fromkeys(word.strip() for word in vocabulary if isinstance(word, str) and word.strip()))
        if len(clean_words) > 80 or sum(len(word) for word in clean_words) > 800 or any(len(word) > 32 for word in clean_words):
            raise VoiceCalibrationInputError("常用词数量或长度超出本机配置限制。")
        capabilities = microphone.get("capabilities")
        expected_capability_keys = {
            "transport", "echo_cancellation_requested", "noise_suppression_requested",
            "auto_gain_control_requested", "sample_rate_hz", "channel_count",
        }
        if set(microphone) != {"system_id_hash", "label", "capabilities"} or not isinstance(microphone.get("system_id_hash"), str) or len(microphone["system_id_hash"]) != 64 or not isinstance(microphone.get("label"), str) or not microphone["label"].strip() or len(microphone["label"].strip()) > 200:
            raise VoiceCalibrationInputError("请重新选择一个名称明确的麦克风。")
        if not isinstance(capabilities, dict) or set(capabilities) - expected_capability_keys or capabilities.get("transport") != "browser-media-device" or not all(isinstance(capabilities.get(key), bool) for key in ("echo_cancellation_requested", "noise_suppression_requested", "auto_gain_control_requested")):
            raise VoiceCalibrationInputError("麦克风能力配置无效，请重新选择设备。")
        sample_rate = capabilities.get("sample_rate_hz")
        channels = capabilities.get("channel_count")
        if (sample_rate is not None and (not isinstance(sample_rate, int) or not 8_000 <= sample_rate <= 192_000)) or (channels is not None and (not isinstance(channels, int) or not 1 <= channels <= 2)):
            raise VoiceCalibrationInputError("麦克风能力配置无效，请重新选择设备。")
        now = utc_now()
        stored_profile = {"version": 3, "childId": child_id, "sampleCount": int(profile["sampleCount"]), "calibrationCount": 3, "energy": profile["energy"], "zeroCrossingRate": profile["zeroCrossingRate"], "spectralBands": bands, "environmentSpread": profile["environmentSpread"]}
        stored_microphone = {"system_id_hash": microphone["system_id_hash"], "label": microphone["label"].strip(), "capabilities": capabilities}
        payload_hash = hashlib.sha256(json.dumps({"child_id": child_id, "profile": stored_profile, "vocabulary": clean_words, "microphone": stored_microphone}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not connection.execute("SELECT 1 FROM child_profile WHERE child_id = ?", (child_id,)).fetchone():
                connection.rollback()
                raise UnknownVoiceCalibrationChildError()
            previous = connection.execute("SELECT child_id, payload_hash FROM voice_calibration_save_action WHERE client_action_id = ?", (client_action_id,)).fetchone()
            if previous:
                if previous["child_id"] != child_id or previous["payload_hash"] != payload_hash:
                    connection.rollback()
                    raise VoiceCalibrationActionConflictError()
                row = connection.execute("SELECT * FROM child_voice_calibration WHERE child_id = ?", (child_id,)).fetchone()
                connection.rollback()
                if row is None:
                    raise VoiceCalibrationDeletedActionError()
                return self._row(row)
            current = connection.execute("SELECT 1 FROM child_voice_calibration WHERE child_id = ?", (child_id,)).fetchone()
            if current:
                connection.rollback()
                raise VoiceCalibrationVersionConflictError()
            connection.execute(
                """INSERT INTO child_voice_calibration(child_id, consented_at, profile_json, vocabulary_json, microphone_json, status, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'ready', ?)
                   ON CONFLICT(child_id) DO UPDATE SET consented_at=excluded.consented_at, profile_json=excluded.profile_json,
                   vocabulary_json=excluded.vocabulary_json, microphone_json=excluded.microphone_json, status='ready', updated_at=excluded.updated_at""",
                (child_id, now, json.dumps(stored_profile, ensure_ascii=False), json.dumps(clean_words, ensure_ascii=False), json.dumps(stored_microphone, ensure_ascii=False), now),
            )
            connection.execute("INSERT INTO voice_calibration_save_action(client_action_id, child_id, payload_hash, saved_at) VALUES (?, ?, ?, ?)", (client_action_id, child_id, payload_hash, now))
            connection.commit()
            return self._row(connection.execute("SELECT * FROM child_voice_calibration WHERE child_id = ?", (child_id,)).fetchone())

    def delete(self, child_id: str, client_action_id: str) -> dict:
        payload_hash = hashlib.sha256(f"delete:{child_id}".encode("utf-8")).hexdigest()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not connection.execute("SELECT 1 FROM child_profile WHERE child_id = ?", (child_id,)).fetchone():
                connection.rollback()
                raise UnknownVoiceCalibrationChildError()
            previous = connection.execute("SELECT child_id, payload_hash FROM voice_calibration_save_action WHERE client_action_id = ?", (client_action_id,)).fetchone()
            if previous:
                connection.rollback()
                if previous["child_id"] != child_id or previous["payload_hash"] != payload_hash:
                    raise VoiceCalibrationActionConflictError()
                return {"child_id": child_id, "status": "not_calibrated", "raw_audio_stored": False, "profile": None, **self._vocabulary_state()}
            connection.execute("DELETE FROM child_voice_calibration WHERE child_id = ?", (child_id,))
            connection.execute("INSERT INTO voice_calibration_save_action(client_action_id, child_id, payload_hash, saved_at) VALUES (?, ?, ?, ?)", (client_action_id, child_id, payload_hash, utc_now()))
            connection.commit()
        return {"child_id": child_id, "status": "not_calibrated", "raw_audio_stored": False, "profile": None, **self._vocabulary_state()}

    def _row(self, row) -> dict:
        return {"child_id": row["child_id"], "status": row["status"], "consented_at": row["consented_at"], "updated_at": row["updated_at"], "raw_audio_stored": False, "profile": json.loads(row["profile_json"]), "vocabulary": json.loads(row["vocabulary_json"]), **self._vocabulary_state(), "microphone": json.loads(row["microphone_json"] or '{}')}


class VoiceCalibrationInputError(Exception): pass
class UnknownVoiceCalibrationChildError(Exception): pass
class VoiceCalibrationActionConflictError(Exception): pass
class VoiceCalibrationVersionConflictError(Exception): pass
class VoiceCalibrationDeletedActionError(Exception): pass
