"""Local acoustic evidence for the existing rubric review transaction.

These helpers validate recording identity and frame references, not a human's
judgment, speech recognition, pronunciation scores or PET certification. The
host must authorize review and persist the returned audio_evidence in the same
rubric_review_event as its versioned review, never in a parallel score table.
"""
import hashlib
import io
import json
import wave

import numpy as np

from .learning_obligations import withdrawn_evidence_ids
from .learning_rubric import RubricEvidenceService
from .local_supply import validate_item, digest


DIMENSIONS = ("audio_text_alignment", "intelligibility", "pronunciation", "prosody", "fluency")
SCHEMA = "pet_speaking_audio_references_v1"


def _require(condition, reason):
    if not condition:
        raise ValueError("PET_SPEAKING_REVIEW_" + reason)


def _snapshot(connection, operation):
    owned = not connection.in_transaction
    if owned:
        connection.execute("BEGIN")
    try:
        return operation()
    finally:
        if owned:
            connection.rollback()


def _prepare(c, session_id, *, media_id, assessment_id, media_root, data_encryption_key, propagate_io=False):
    from .pet_media_evidence import _owned, _current, _read_audio, _date, _now
    row = _owned(c, session_id, media_id)
    _require(row["kind"] == "speaking", "SPEAKING_BINDING_REQUIRED")
    _require(json.loads(row["state_json"])["status"] != "cancelled", "BINDING_CANCELLED")
    prompt, item, source, audio, reasons = _current(c, row, media_root, data_encryption_key)
    if propagate_io and audio.get('retryable_io'):
        raise OSError('PET_SPEAKING_SOURCE_READ_UNAVAILABLE')
    _require(not reasons, "SOURCE_CHANGED_OR_UNAVAILABLE")
    validate_item(c, row["child_id"], row["diagnostic_item_id"])
    _require(prompt["status"] == "answered" and prompt["attempt_id"], "ORIGINAL_ATTEMPT_REQUIRED")
    attempt = c.execute("SELECT * FROM diagnostic_attempt WHERE attempt_id=? AND child_id=? AND session_id=?",
        (prompt["attempt_id"], row["child_id"], session_id)).fetchone()
    _require(attempt is not None and attempt["diagnostic_item_id"] == row["diagnostic_item_id"], "ORIGINAL_RESPONSE_CONFLICT")
    from .pet_speaking_origin import original_answer_voice
    origin = original_answer_voice(c, prompt, attempt, row, audio['sha256'])
    _require(_date(prompt["created_at"]) <= _date(source["created_at"]) <= _date(attempt["created_at"]) <= _now(),
             "RECORDING_ANSWER_TIME_CONFLICT")
    _require(attempt["evidence_id"] not in withdrawn_evidence_ids(c, row["child_id"]), "EVIDENCE_WITHDRAWN")
    current = RubricEvidenceService(None).get(row["child_id"], assessment_id, connection=c)
    _require(current["source_status"] == "current"
             and current["source_evidence_id"] == attempt["evidence_id"] and current["session_id"] == session_id
             and current["question"] == item["prompt"] and current["response"] == attempt["submitted_answer"]
             and current["material"].get("diagnostic_item_id") == row["diagnostic_item_id"], "ASSESSMENT_SOURCE_CONFLICT")
    content = _read_audio(source["local_path"], media_root=media_root, data_encryption_key=data_encryption_key)
    _require(hashlib.sha256(content).hexdigest() == audio["sha256"], "AUDIO_CHANGED_DURING_READ")
    with wave.open(io.BytesIO(content), "rb") as stream:
        samples = np.frombuffer(stream.readframes(stream.getnframes()), dtype="<i2").astype(np.float64) / 32768
        rate = stream.getframerate()
    # A 100 ms envelope is only a navigation aid. No pause, stress or fluency
    # judgment is inferred from amplitude, silence, VAD or the stored transcript.
    width = max(1, rate // 10)
    waveform = [{"start_frame": start, "end_frame": min(start + width, len(samples)),
                 "rms": round(float(np.sqrt(np.mean(samples[start:start + width] ** 2))), 6),
                 "peak": round(float(np.max(np.abs(samples[start:start + width]))), 6)}
                for start in range(0, len(samples), width)]
    binding = {key: row[key] for key in ("child_id", "session_id", "media_id", "prompt_id", "item_version",
                "diagnostic_item_id", "voice_input_id", "source_version")}
    binding.update(assessment_id=assessment_id, assessment_fingerprint=current["fingerprint"],
                   attempt_id=attempt["attempt_id"], evidence_id=attempt["evidence_id"], audio_sha256=audio["sha256"],
                   answer_voice_origin=origin)
    return {**binding, "binding_version": "pet_speaking_review_v2:" + digest(binding),
            "review_revision": current["review_revision"], "audio": {key: audio[key] for key in ("sha256", "frames", "sample_rate")},
            "waveform": waveform, "waveform_scope": "amplitude_navigation_not_speech_assessment",
            "dimensions_required": list(DIMENSIONS), "task_family": "read_aloud" if item["answer_type"] == "read_aloud" else "spoken_response",
            "transcript_status": "unverified_asr_requires_original_audio_review",
            "status": "ready_for_authorized_audio_review", "skill_qualified": False,
            "mastery_claim_allowed": False, "certification_claim_allowed": False,
            "local_only": True, "cloud_transmission_allowed": False}, content


def prepare_speaking_audio_review(connection, session_id, *, media_id, assessment_id, media_root, data_encryption_key=None):
    """Read original waveform and reference scope without changing review/score."""
    return _snapshot(connection, lambda: _prepare(connection, session_id, media_id=media_id,
        assessment_id=assessment_id, media_root=media_root, data_encryption_key=data_encryption_key)[0])


def read_speaking_review_audio(connection, session_id, *, media_id, assessment_id, audio_sha256,
                              media_root, data_encryption_key=None):
    """Parent-local adapter only; no arbitrary paths, upload or playback receipt."""
    def read():
        packet, content = _prepare(connection, session_id, media_id=media_id, assessment_id=assessment_id,
            media_root=media_root, data_encryption_key=data_encryption_key)
        _require(packet["audio"]["sha256"] == audio_sha256, "AUDIO_HASH_CONFLICT")
        return content
    return _snapshot(connection, read)


def validate_speaking_audio_review(connection, session_id, *, media_id, assessment_id, review,
                                   media_root, data_encryption_key=None):
    """Call INSIDE the authorized rubric review write transaction.

    The returned audio_evidence may be persisted alongside its existing review;
    it carries no score, actor or authority flag. Revalidate it when projecting.
    """
    def validate():
        packet, _ = _prepare(connection, session_id, media_id=media_id, assessment_id=assessment_id,
            media_root=media_root, data_encryption_key=data_encryption_key)
        _require(isinstance(review, dict) and set(review) == {"schema", "binding_version", "audio_sha256", "observations"}
                 and review["schema"] == SCHEMA and review["binding_version"] == packet["binding_version"]
                 and review["audio_sha256"] == packet["audio"]["sha256"], "BINDING_CONFLICT")
        observations = review["observations"]
        _require(isinstance(observations, list) and len(observations) == len(DIMENSIONS), "DIMENSIONS_REQUIRED")
        seen = set()
        for entry in observations:
            _require(isinstance(entry, dict) and set(entry) == {"dimension_id", "start_frame", "end_frame", "heard_text", "rationale"}, "OBSERVATION_INVALID")
            dimension = entry["dimension_id"]
            _require(isinstance(dimension, str) and dimension in DIMENSIONS and dimension not in seen, "DIMENSIONS_REQUIRED")
            seen.add(dimension)
            _require(type(entry["start_frame"]) is int and type(entry["end_frame"]) is int
                     and 0 <= entry["start_frame"] < entry["end_frame"] <= packet["audio"]["frames"], "FRAME_RANGE_INVALID")
            _require(all(isinstance(entry[field], str) and 0 < len(entry[field].strip()) <= 800
                         for field in ("heard_text", "rationale")), "OBSERVATION_REQUIRED")
        return {"audio_evidence": json.loads(json.dumps(review)), "status": "audio_references_validated_not_scored",
                "skill_qualified": False, "mastery_claim_allowed": False, "certification_claim_allowed": False,
                "local_only": True, "cloud_transmission_allowed": False}
    return _snapshot(connection, validate)
