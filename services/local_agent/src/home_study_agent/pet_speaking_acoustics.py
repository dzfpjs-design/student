"""Current recording readiness, not a pronunciation or fluency scoring substitute.

The installed Zipformer is an ASR transducer. Its text and VAD activity cannot
certify phones, stress, fluency, intelligibility, speaker identity or a PET stage.
This projection exposes concrete source/recording obligations for the next local
audio assessor, without copying bytes, persisting a second score or calling ASR.
"""

import hashlib
import io
import wave

import numpy as np

from .local_media_ai import voice_activity_evidence


def project_speaking_readiness(connection, session_id, *, media_id, media_root, data_encryption_key=None):
    owned = not connection.in_transaction
    if owned:
        connection.execute("BEGIN")
    try:
        return _project(connection, session_id, media_id=media_id, media_root=media_root,
                        data_encryption_key=data_encryption_key)
    finally:
        if owned:
            connection.rollback()


def _project(connection, session_id, *, media_id, media_root, data_encryption_key):
    from .pet_media_evidence import _owned, _current, _read_audio, _hash, PetMediaEvidenceError
    row = _owned(connection, session_id, media_id)
    if row["kind"] != "speaking":
        raise PetMediaEvidenceError("not_speaking_binding")
    prompt, item, source, audio, reasons = _current(connection, row, media_root, data_encryption_key)
    activity = None
    if not reasons:
        try:
            content = _read_audio(source["local_path"], media_root=media_root, data_encryption_key=data_encryption_key)
            if hashlib.sha256(content).hexdigest() != audio["sha256"]:
                raise ValueError("audio_changed_during_analysis")
            with wave.open(io.BytesIO(content), "rb") as stream:
                pcm = stream.readframes(stream.getnframes())
                samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
                activity = voice_activity_evidence(samples, stream.getframerate())
        except (OSError, ValueError, wave.Error) as exc:
            reasons.append(str(exc))
    obligations = []
    if reasons:
        obligations.append({"action": "restore_or_replace_original_recording", "status": "blocked", "reasons": reasons})
    elif not activity or not activity["human_voice_detected"]:
        obligations.append({"action": "review_original_recording_signal", "status": "needs_review",
            "reasons": ["sustained_voice_candidate_not_detected_not_a_speech_diagnosis"]})
    obligations.extend([
        {"action": "verify_audio_transcript_alignment", "status": "required", "reasons": ["stored_asr_text_is_not_audio_alignment"]},
        {"action": "assess_original_audio_with_validated_rubric", "status": "assessor_not_installed",
         "reasons": ["no_validated_phoneme_prosody_intelligibility_assessor_in_model_manifest"]},
    ])
    report = {"version": "pet_speaking_recording_readiness_v1", "child_id": row["child_id"], "session_id": session_id,
        "media_id": media_id, "prompt_id": row["prompt_id"], "item_version": row["item_version"],
        "voice_input_id": row["voice_input_id"], "source_version": row["source_version"],
        "audio_sha256": audio["sha256"], "signal_usable": not reasons and audio["status"] == "usable_signal",
        "status": "source_unavailable" if reasons else "recording_review_needed" if not activity or not activity["human_voice_detected"] else "ready_for_audio_assessment",
        "speech_activity": ({"policy": "existing_strict_webrtcvad_30ms_v1",
            "speech_candidate": activity["human_voice_detected"], "candidate_frames": activity["speech_frames"],
            "analyzed_frames": activity["frame_count"], "longest_candidate_run_frames": activity["longest_speech_run"],
            "frame_ms": activity["frame_ms"], "scope": "signal_activity_not_words_pronunciation_or_speaker_identity"} if activity else None),
        "task_family": "read_aloud" if item and item["answer_type"] == "read_aloud" else "spoken_response",
        "rubric_dimensions_required": ["audio_text_alignment", "intelligibility", "pronunciation", "prosody", "fluency"],
        "assessment_status": "missing_validated_audio_assessment", "skill_qualified": False,
        "mastery_claim_allowed": False, "certification_claim_allowed": False,
        "obligations": obligations, "local_only": True, "cloud_transmission_allowed": False}
    report["evidence_version"] = "pet_speaking_recording_readiness_v1:" + _hash(report)
    return report
