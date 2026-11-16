from __future__ import annotations

from contextlib import contextmanager
from io import BytesIO
import hashlib
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING
from threading import Lock
import wave
from uuid import uuid4

import cv2
import numpy as np
import webrtcvad
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .database import Database
from .cloud_asr import CloudAsrError, CloudAsrGateway, CloudAsrNoSpeechError, CloudAsrAudioPermissionError
from .cloud_privacy import sanitize_cloud_text
from .config import Settings
from .local_ocr.service import RapidOcrEngine
from .repository import UnknownSessionError, utc_now

if TYPE_CHECKING:
    from .repository import StudyRepository


ASR_MODEL_NAME = "sherpa-onnx-zipformer-zh-en-2023-11-22"
ASR_MINIMUM_CAPTURE_MS = 120
LOW_INFORMATION_TRANSCRIPTS = frozenset({"啊", "嗯", "呃", "额", "哦", "噢", "哼", "哈", "哎", "唉"})


def is_low_information_transcript(transcript: str, *, dialogue: bool = False) -> bool:
    compact = "".join(character for character in transcript.strip() if character not in " \t\r\n，。！？,.!?~～·…")
    if not compact:
        return True
    return not dialogue and len(compact) <= 3 and all(character in LOW_INFORMATION_TRANSCRIPTS for character in compact)


def voice_activity_evidence(samples: np.ndarray, sample_rate: int) -> dict:
    """Return sustained WebRTC speech-candidate evidence before ASR.

    This admits short words without identifying a speaker or granting intent,
    playback interruption, recording retention or tool permissions.
    """
    if sample_rate not in {8_000, 16_000, 32_000, 48_000}:
        target_rate = 16_000
        source_positions = np.arange(len(samples), dtype=np.float64)
        target_count = max(1, round(len(samples) * target_rate / max(sample_rate, 1)))
        target_positions = np.linspace(0, max(0, len(samples) - 1), target_count)
        samples = np.interp(target_positions, source_positions, samples).astype(np.float32)
        sample_rate = target_rate
    pcm = np.clip(samples * 32768.0, -32768, 32767).astype(np.int16)
    frame_samples = sample_rate * 30 // 1000
    subframe_samples = sample_rate * 5 // 1000
    absolute_samples = np.abs(samples)
    noise_floor = float(np.percentile(absolute_samples, 20)) if len(absolute_samples) else 0.0
    sustained_threshold = max(0.003, noise_floor * 3.0)
    vad = webrtcvad.Vad(3)
    flags: list[bool] = []
    for start in range(0, len(pcm) - frame_samples + 1, frame_samples):
        frame = samples[start:start + frame_samples]
        active_subframes = 0
        for offset in range(0, frame_samples, subframe_samples):
            subframe = frame[offset:offset + subframe_samples]
            rms = float(np.sqrt(np.mean(np.square(subframe)))) if len(subframe) else 0.0
            if rms >= sustained_threshold:
                active_subframes += 1
        # Keyboard clicks can trigger WebRTC VAD's hangover, but they occupy
        # only one tiny part of a 30 ms frame. Human phonemes sustain energy
        # across multiple adjacent 5 ms slices.
        flags.append(
            vad.is_speech(pcm[start:start + frame_samples].tobytes(), sample_rate)
            and active_subframes >= 3
        )
    longest_run = 0
    current_run = 0
    for flag in flags:
        current_run = current_run + 1 if flag else 0
        longest_run = max(longest_run, current_run)
    speech_frames = sum(flags)
    return {
        # Legacy field name: speech admission is not proof of human identity.
        "human_voice_detected": speech_frames >= 4 and longest_run >= 3,
        "speech_frames": speech_frames,
        "longest_speech_run": longest_run,
        "frame_count": len(flags),
        "frame_ms": 30,
        "admission_policy": "webrtc-sustained-short-v2",
        "minimum_speech_frames": 4,
        "minimum_contiguous_frames": 3,
    }


class TranscriptionActionConflictError(Exception):
    pass


class LocalMediaAI:
    def __init__(
        self,
        database: Database,
        data_dir: Path,
        model_root: Path,
        data_encryption_key: bytes | None = None,
        settings: Settings | None = None,
        repository: StudyRepository | None = None,
        cloud_asr_gateway: CloudAsrGateway | None = None,
        model_runtime_policy=None,
    ):
        self.database = database
        self.data_dir = data_dir
        self.model_root = model_root
        self.data_encryption_key = data_encryption_key
        self.repository = repository
        self._cloud_asr = cloud_asr_gateway or (
            CloudAsrGateway(settings, runtime_policy=model_runtime_policy,
                            cloud_audio_authorized=self._cloud_audio_allowed) if settings else None
        )
        self._ocr_engine: RapidOcrEngine | None = None
        self._asr_recognizer = None
        self._asr_decode_slot = Lock()
        from .household_voice_entry import HouseholdEntryReceipts
        def household_generation():
            from .privacy_operation_receipt import generation
            with self.database.connect() as c:
                return generation(c)
        self.household_entries = HouseholdEntryReceipts(generation_reader=household_generation)

    @property
    def cloud_context_supported(self) -> bool:
        return bool(self._cloud_audio_allowed() and self._cloud_asr and self._cloud_asr.context_supported)

    def _cloud_audio_allowed(self) -> bool:
        try:
            with self.database.connect() as connection:
                return self._connection_cloud_audio_allowed(connection)
        except Exception:
            return False

    @staticmethod
    def _connection_cloud_audio_allowed(connection) -> bool:
        try:
            row = connection.execute(
                "SELECT cloud_audio_allowed FROM privacy_preference WHERE singleton_id=1"
            ).fetchone()
            return bool(row and type(row[0]) is int and row[0] == 1)
        except Exception:
            return False

    def status(self) -> dict:
        asr_dir = self.model_root / ASR_MODEL_NAME
        asr_files = self._asr_files(asr_dir)
        ocr_health = RapidOcrEngine().health()
        return {
            "ocr": {"engine": "rapidocr-onnx-cpu", **ocr_health},
            "asr": {
                "ready": all(path.is_file() for path in asr_files.values()),
                "engine": "sherpa-onnx-1.13.6",
                "model": ASR_MODEL_NAME,
                "required_model_file_count": len(asr_files),
                "active_dialogue_engine": "cloud" if self._cloud_audio_allowed() and self._cloud_asr and self._cloud_asr.configured else "local_fallback",
                "cloud_ready": bool(self._cloud_asr and self._cloud_asr.configured),
                "child_context_ready": bool(self._cloud_asr and self._cloud_asr.context_supported),
            },
            "tts": {"ready": True, "engine": "windows-system-voice"},
        }

    @staticmethod
    def _assert_document_monitor_epoch(connection, session_id: str, generation: int | None) -> None:
        if generation is None:
            return
        row = connection.execute("""SELECT m.generation FROM document_monitor_session m
            JOIN daily_session s ON s.session_id=m.session_id AND s.child_id=m.child_id
            WHERE m.session_id=? AND m.status='active' AND s.status='active' AND s.ended_at IS NULL
            AND (s.state='study' OR (s.state IN ('homework_capture','plan_review') AND s.observation_status='enabled'
                AND EXISTS (SELECT 1 FROM sensor_lease l WHERE l.session_id=s.session_id
                    AND l.device_type='document_camera' AND l.status='active' AND julianday(l.expires_at)>julianday('now'))))""",
            (session_id,)).fetchone()
        if not row or row[0] != generation:
            raise ValueError("document observation was invalidated before persistence")

    def ocr(self, session_id: str, image_bytes: bytes, client_action_id: str, store_raw: bool, prepared_result: dict | None = None,
            monitor_generation: int | None = None) -> dict:
        session = self._session(session_id)
        with self.database.connect() as connection:
            self._assert_document_monitor_epoch(connection, session_id, monitor_generation)
            if prepared_result and prepared_result.get('_local_ocr_job_id'):
                from .local_ocr.cache_lifecycle import validate_publication
                validate_publication(connection, prepared_result['_local_ocr_job_id'], session_id, image_bytes)
            existing = connection.execute("SELECT artifact_id FROM capture_artifact WHERE client_action_id = ?", (client_action_id,)).fetchone()
            if existing:
                return self._artifact(connection, existing["artifact_id"])
        if len(image_bytes) < 32:
            raise InvalidMediaInputError("图片内容为空或不完整。")
        image = cv2.imdecode(np.frombuffer(image_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise InvalidMediaInputError("无法解码图片。")
        if image.shape[0] < 80 or image.shape[1] < 80:
            raise InvalidMediaInputError("图片尺寸过小。")
        if prepared_result is not None:
            quality = self._merge_ocr_quality(self._image_quality(image), prepared_result["quality"])
            lines = list(prepared_result.get("lines", []))
            engine_name = str(prepared_result.get("engine", "local-ocr"))
        else:
            quality = self._image_quality(image)
            if self._ocr_engine is None:
                self._ocr_engine = RapidOcrEngine()
            lines = self._ocr_engine.recognize(image, "cpu")
            engine_name = self._ocr_engine.name
        quality.setdefault("issues", [])
        quality.setdefault("messages", [])
        texts = [str(line.get("text", "")) for line in lines]
        scores = [float(line["confidence"]) for line in lines if line.get("confidence") is not None]
        boxes = [line.get("box") for line in lines if line.get("box") is not None]
        if self._content_near_edge(boxes, image.shape[1], image.shape[0]):
            quality["issues"].append("content_near_edge")
            quality["messages"].append("文字靠近画面边缘，可能存在裁切缺失。")
        extracted = "\n".join(texts)
        ocr_confidence = sum(scores) / len(scores) if scores else 0.0
        quality["passed"] = len(quality["issues"]) == 0
        confidence = ocr_confidence if quality["passed"] else min(ocr_confidence, 0.49)
        artifact_id = str(uuid4())
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_document_monitor_epoch(connection, session_id, monitor_generation)
            if prepared_result and prepared_result.get('_local_ocr_job_id'):
                from .local_ocr.cache_lifecycle import validate_publication
                validate_publication(connection, prepared_result['_local_ocr_job_id'], session_id, image_bytes)
            details = {"lines": lines, "engine": engine_name, "ocr_confidence": ocr_confidence, "quality": quality}
            if prepared_result and prepared_result.get('_local_ocr_job_id') and prepared_result.get('_local_pipeline'):
                details['local_pipeline'] = prepared_result['_local_pipeline']
            if monitor_generation is not None:
                from hashlib import sha256
                details.update(monitor_generation=monitor_generation, monitor_frame_sha256=sha256(image_bytes).hexdigest())
            with self._raw_publication(connection, session["child_id"], session_id,
                    artifact_id, ".jpg", image_bytes, store_raw) as local_path:
                connection.execute(
                    """
                    INSERT INTO capture_artifact(
                      artifact_id, child_id, session_id, artifact_type, local_path,
                      extracted_text, confidence, details_json, status, created_at, client_action_id
                    ) VALUES (?, ?, ?, 'document_ocr', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        artifact_id, session["child_id"], session_id, local_path, extracted,
                        confidence,
                        json.dumps(details, ensure_ascii=False),
                        "completed" if quality["passed"] else "quality_warning",
                        now, client_action_id,
                    ),
                )
                if 'local_pipeline' in details:
                    connection.execute('UPDATE ocr_job SET artifact_id=? WHERE job_id=?',
                                       (artifact_id, prepared_result['_local_ocr_job_id']))
            return self._artifact(connection, artifact_id)

    def retain_document_page_source(
        self, connection, artifact_id: str, image_bytes: bytes, expected_sha256: str
    ) -> tuple[dict, str | None]:
        """Attach one encrypted source image to an accepted document page.

        The caller owns the surrounding database transaction. The returned path
        is only for rollback cleanup and must never be exposed through HTTP.
        """
        actual_sha256 = hashlib.sha256(image_bytes).hexdigest()
        if actual_sha256 != expected_sha256:
            raise ValueError("DOCUMENT_PAGE_SOURCE_CHANGED")
        if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            suffix, media_type = ".png", "image/png"
        elif image_bytes.startswith(b"\xff\xd8\xff"):
            suffix, media_type = ".jpg", "image/jpeg"
        else:
            raise ValueError("DOCUMENT_PAGE_SOURCE_FORMAT_UNSUPPORTED")
        row = connection.execute(
            """SELECT artifact_id,child_id,session_id,artifact_type,local_path,details_json
               FROM capture_artifact WHERE artifact_id=?""",
            (artifact_id,),
        ).fetchone()
        if not row or row["artifact_type"] != "document_ocr":
            raise ValueError("DOCUMENT_PAGE_SOURCE_CHANGED")
        details = json.loads(row["details_json"] or "{}")
        if details.get("monitor_frame_sha256") != actual_sha256:
            raise ValueError("DOCUMENT_PAGE_SOURCE_CHANGED")
        projection = {
            "version": "document_page_source_v1",
            "status": "retained",
            "sha256": actual_sha256,
            "byte_length": len(image_bytes),
            "media_type": media_type,
            "encrypted": True,
            "local_only": True,
            "cloud_transmission_allowed": False,
            "retention_reason": "accepted_document_page_revision",
        }
        existing = details.get("page_source")
        if row["local_path"]:
            if not Path(row["local_path"]).is_file():
                raise ValueError("DOCUMENT_PAGE_SOURCE_FILE_MISSING")
            if not isinstance(existing, dict) or existing.get("sha256") != actual_sha256:
                raise ValueError("DOCUMENT_PAGE_SOURCE_METADATA_CONFLICT")
            return projection, None
        path = self._store_raw(
            row["child_id"], row["session_id"], artifact_id, suffix, image_bytes
        )
        details["page_source"] = projection
        connection.execute(
            "UPDATE capture_artifact SET local_path=?,details_json=? WHERE artifact_id=? AND local_path IS NULL",
            (path, json.dumps(details, ensure_ascii=False), artifact_id),
        )
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            Path(path).unlink(missing_ok=True)
            raise ValueError("DOCUMENT_PAGE_SOURCE_CONFLICT")
        return projection, path

    def discard_unreferenced_capture_source(self, path: str) -> None:
        """Remove a staged encrypted page only when no durable row owns it."""
        with self.database.connect() as connection:
            referenced = connection.execute(
                "SELECT 1 FROM capture_artifact WHERE local_path=? LIMIT 1", (path,)
            ).fetchone()
        if not referenced:
            Path(path).unlink(missing_ok=True)

    def transcribe(self, session_id: str, wav_bytes: bytes, purpose: str, client_action_id: str, store_raw: bool,
                   *, local_recovery_only: bool = False, local_only: bool = False, ordinary_response_binding=None) -> dict:
        from .ordinary_response_binding import normalize,stored,validate_capture
        ordinary=normalize(ordinary_response_binding)
        if ordinary and (local_only is not True or purpose!='agent_command'):
            raise InvalidMediaInputError('本地回应模式无效。')
        if type(local_recovery_only) is not bool:
            raise InvalidMediaInputError('本地恢复模式无效。')
        if type(local_only) is not bool or (local_only and (local_recovery_only or purpose != ('agent_command' if ordinary else 'wake_word_setup'))):
            raise InvalidMediaInputError('本地回应模式无效。')
        if local_only:
            store_raw = False
        if local_recovery_only:
            purpose = 'agent_command'
            store_raw = False
        session = self._session(session_id)
        with self.database.connect() as connection:
            if ordinary:connection.execute('BEGIN')
            existing, action = self._assert_transcription_action_owner(connection, session, client_action_id)
            capture = None
            if connection.execute("SELECT 1 FROM sqlite_master WHERE name='voice_capture_binding'").fetchone():
                capture = connection.execute('SELECT * FROM voice_capture_binding WHERE client_action_id=?', (client_action_id,)).fetchone()
                if capture:
                    from .voice_capture_binding import stored_local_recovery_only, VoiceCaptureBindingError
                    if stored_local_recovery_only(capture) != local_recovery_only:
                        raise VoiceCaptureBindingError('VOICE_CAPTURE_ACTION_CONFLICT')
            if ordinary:
                validate_capture(connection,capture,ordinary,page_source_store=self)
            elif capture and stored(capture):
                from .voice_capture_binding import VoiceCaptureBindingError
                raise VoiceCaptureBindingError('VOICE_CAPTURE_ACTION_CONFLICT')
            if local_only:
                from .voice_capture_binding import VoiceCaptureBindingError
                if not capture or capture['child_id'] != session['child_id'] or capture['session_id'] != session_id or (not ordinary and any((
                        capture['prompt_id'], capture['item_version'], capture['question_reference_json'],
                        capture['activity_reference_json'], capture['pending_evaluation_turn_id'],
                        capture['task_choice_reference_json'], capture['local_recovery_only']))):
                    raise VoiceCaptureBindingError('VOICE_CAPTURE_LOCAL_RESPONSE_INVALID')
                if existing and (self._voice_input(connection, existing['voice_input_id'])['model_version'] != ASR_MODEL_NAME
                        or self._voice_input(connection, existing['voice_input_id'])['local_path'] is not None):
                    raise VoiceCaptureBindingError('VOICE_CAPTURE_ACTION_CONFLICT')
            if existing:
                if local_recovery_only and (not capture or connection.execute(
                        'SELECT local_path FROM voice_input WHERE voice_input_id=?',
                        (existing['voice_input_id'],)).fetchone()[0]):
                    from .voice_capture_binding import VoiceCaptureBindingError
                    raise VoiceCaptureBindingError('VOICE_CAPTURE_ACTION_CONFLICT')
                if action and action["status"] == "cancelled":
                    connection.execute("UPDATE voice_input SET status = 'ignored' WHERE voice_input_id = ?", (existing["voice_input_id"],))
                    connection.execute("UPDATE voice_transcription_action SET status = 'ignored', updated_at = ? WHERE client_action_id = ? AND session_id = ? AND child_id = ?", (utc_now(), client_action_id, session_id, session["child_id"]))
                    connection.commit()
                return self._voice_input(connection, existing["voice_input_id"])
        transcript, duration_ms, model_version, cloud_used = self._recognize_wav(
            wav_bytes,
            prefer_cloud=not (local_recovery_only or local_only),
            session=session,
            purpose=purpose,
        )
        if local_only and (cloud_used is not False or model_version != ASR_MODEL_NAME):
            from .voice_capture_binding import VoiceCaptureBindingError
            raise VoiceCaptureBindingError('VOICE_CAPTURE_SOURCE_MODEL_INVALID')
        voice_input_id = str(uuid4())
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            current = connection.execute('SELECT child_id,status FROM daily_session WHERE session_id=?', (session_id,)).fetchone()
            if current is None or current['child_id'] != session['child_id'] or current['status'] != 'active':
                raise InvalidMediaInputError('录音期间学习会话或孩子已变化，未保存到另一位孩子。')
            existing, cancellation = self._assert_transcription_action_owner(connection, session, client_action_id)
            current_capture=connection.execute('SELECT * FROM voice_capture_binding WHERE client_action_id=?',(client_action_id,)).fetchone()
            if ordinary:
                validate_capture(connection,current_capture,ordinary,page_source_store=self)
            elif current_capture and stored(current_capture):
                from .voice_capture_binding import VoiceCaptureBindingError
                raise VoiceCaptureBindingError('VOICE_CAPTURE_ACTION_CONFLICT')
            if existing:
                if local_only and (self._voice_input(connection, existing['voice_input_id'])['model_version'] != ASR_MODEL_NAME
                        or self._voice_input(connection, existing['voice_input_id'])['local_path'] is not None):
                    from .voice_capture_binding import VoiceCaptureBindingError
                    raise VoiceCaptureBindingError('VOICE_CAPTURE_ACTION_CONFLICT')
                return self._voice_input(connection, existing['voice_input_id'])
            if cloud_used and not self._connection_cloud_audio_allowed(connection):
                # The same write transaction owns the final consent check and
                # publication. Never persist a newly returned cloud transcript
                # after revocation won the race; the action may retry locally.
                raise LocalCapabilityNotReadyError(
                    "ASR_CLOUD_AUDIO_REVOKED", "云端语音授权已经收回，这句话没有保存或执行。请再说一次。"
                )
            capture = None
            if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='voice_capture_binding'").fetchone():
                capture = connection.execute('SELECT * FROM voice_capture_binding WHERE client_action_id=?', (client_action_id,)).fetchone()
            if capture and capture['status'] == 'cancelled':
                # A cancelled capture is also a deletion tombstone: no late body may be recreated.
                from .voice_capture_binding import VoiceCaptureBindingError
                raise VoiceCaptureBindingError('VOICE_CAPTURE_CANCELLED')
            if capture:
                from .voice_capture_binding import stored_local_recovery_only, VoiceCaptureBindingError
                if stored_local_recovery_only(capture) != local_recovery_only:
                    raise VoiceCaptureBindingError('VOICE_CAPTURE_ACTION_CONFLICT')
            ignored = bool(cancellation and cancellation['status'] == 'cancelled') or bool(capture and capture['status'] == 'cancelled')
            status = 'ignored' if ignored else 'completed'
            if capture:
                preference = connection.execute('SELECT raw_media_storage_allowed FROM privacy_preference WHERE singleton_id=1').fetchone()
                store_raw = bool(store_raw and preference and preference[0])
            with self._raw_publication(connection, session["child_id"], session_id,
                    voice_input_id, ".wav", wav_bytes, store_raw and not ignored) as local_path:
                connection.execute(
                    """
                    INSERT INTO voice_input(
                      voice_input_id, child_id, session_id, purpose, transcript, local_path,
                      model_version, duration_ms, status, created_at, client_action_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (voice_input_id, session["child_id"], session_id, purpose, transcript, local_path, model_version, duration_ms, status, now, client_action_id),
                )
                connection.execute(
                    """INSERT INTO voice_transcription_action(client_action_id, session_id, child_id, status, updated_at) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(client_action_id) DO UPDATE SET status = CASE WHEN voice_transcription_action.status = 'cancelled' THEN 'ignored' ELSE excluded.status END, updated_at = excluded.updated_at
                    WHERE voice_transcription_action.session_id = excluded.session_id AND voice_transcription_action.child_id = excluded.child_id""",
                    (client_action_id, session_id, session["child_id"], status, now),
                )
            result = self._voice_input(connection, voice_input_id)
            result["cloud_used"] = cloud_used
            return result

    def transcribe_standby(self, wav_bytes: bytes, client_action_id: str, *, entry=False) -> dict:
        """Recognize a household wake phrase without creating child data."""
        entry_generation = self.household_entries.generation_reader() if entry else None
        transcript, duration_ms, model_version, cloud_used = self._recognize_wav(wav_bytes, prefer_cloud=False)
        result = {
            "client_action_id": client_action_id,
            "transcript": transcript,
            "duration_ms": duration_ms,
            "model_version": model_version,
            "status": "completed",
            "persisted": False,
            "cloud_used": cloud_used,
        }
        if entry:
            result['entry_receipt'] = self.household_entries.issue(result, generation_before=entry_generation)
        return result

    def _recognize_wav(
        self,
        wav_bytes: bytes,
        prefer_cloud: bool = False,
        session: dict | None = None,
        purpose: str = "wake_word",
    ) -> tuple[str, int, str, bool]:
        cloud_enabled = bool(prefer_cloud and purpose != "wake_word" and self._cloud_audio_allowed()
                             and self._cloud_asr and self._cloud_asr.configured)
        if not cloud_enabled:
            files = self._asr_files(self.model_root / ASR_MODEL_NAME)
            if not all(path.is_file() for path in files.values()):
                raise LocalCapabilityNotReadyError("ASR_MODEL_NOT_READY", "本地中英双语语音模型尚未安装。")
        try:
            with wave.open(BytesIO(wav_bytes), "rb") as source:
                if source.getnchannels() != 1 or source.getsampwidth() != 2:
                    raise InvalidMediaInputError("语音必须是单声道16位PCM WAV。")
                sample_rate = source.getframerate()
                frame_count = source.getnframes()
                samples = np.frombuffer(source.readframes(frame_count), dtype=np.int16).astype(np.float32) / 32768.0
        except wave.Error as error:
            raise InvalidMediaInputError("无法解码WAV语音。") from error
        if len(samples) * 1000 < sample_rate * ASR_MINIMUM_CAPTURE_MS:
            raise InvalidMediaInputError("录音片段不完整，请再说一次。")
        activity = voice_activity_evidence(samples, sample_rate)
        if not activity["human_voice_detected"]:
            raise LocalCapabilityNotReadyError(
                "ASR_NO_HUMAN_SPEECH", "没有检测到连续人声；键盘声和环境声不会进入语音识别。"
            )

        duration_ms = round(len(samples) / sample_rate * 1000)
        cloud_attempted = False
        if cloud_enabled and self._cloud_asr:
            cloud_attempted = True
            context = self._cloud_asr_context(session)
            call_id = self._start_cloud_asr_call(
                session, purpose, len(wav_bytes), duration_ms, context, voice_activity=activity
            )
            try:
                result = self._cloud_asr.transcribe(wav_bytes, context_text=context["text"] or None)
                if not self._cloud_audio_allowed():
                    raise CloudAsrAudioPermissionError(result)
                transcript = result.transcript.strip()
                if is_low_information_transcript(transcript, dialogue=purpose != 'wake_word'):
                    self._finish_cloud_asr_call(
                        call_id,
                        status="completed",
                        latency_ms=result.latency_ms,
                        output_chars=len(transcript),
                        prompt_tokens=result.prompt_tokens,
                        completion_tokens=result.completion_tokens,
                        model=result.model,
                    )
                    raise LocalCapabilityNotReadyError(
                        "ASR_NO_MEANINGFUL_SPEECH", "这一段没有识别到可用内容。"
                    )
                self._finish_cloud_asr_call(
                    call_id,
                    status="completed",
                    latency_ms=result.latency_ms,
                    output_chars=len(transcript),
                    prompt_tokens=result.prompt_tokens,
                    completion_tokens=result.completion_tokens,
                    model=result.model,
                )
                return transcript, duration_ms, f"cloud:qwen:{result.model}", True
            except CloudAsrAudioPermissionError as error:
                result = error.result
                self._finish_cloud_asr_call(
                    call_id,
                    status="failed",
                    latency_ms=result.latency_ms if result else None,
                    output_chars=0,
                    prompt_tokens=result.prompt_tokens if result else None,
                    completion_tokens=result.completion_tokens if result else None,
                    error_code=str(error),
                    fallback_used=True,
                )
                # Permission denial is not a provider outage. Keep the real
                # local source identity and do not label it a cloud result.
                cloud_attempted = False
            except CloudAsrNoSpeechError as error:
                result = error.result
                self._finish_cloud_asr_call(
                    call_id,
                    status="completed",
                    latency_ms=result.latency_ms,
                    output_chars=0,
                    prompt_tokens=result.prompt_tokens,
                    completion_tokens=result.completion_tokens,
                    model=result.model,
                )
                raise LocalCapabilityNotReadyError(
                    "ASR_NO_MEANINGFUL_SPEECH", "这一段没有识别到可用内容。"
                ) from error
            except CloudAsrError as error:
                allow_local_fallback = purpose == "wake_word"
                self._finish_cloud_asr_call(
                    call_id,
                    status="failed",
                    latency_ms=None,
                    output_chars=None,
                    error_code=str(error),
                    fallback_used=allow_local_fallback,
                )
                if not allow_local_fallback:
                    raise LocalCapabilityNotReadyError(
                        "ASR_CLOUD_DIALOGUE_UNAVAILABLE",
                        "云端语音识别暂时没有成功，这句话没有执行。请稍后再说，我不会用本地猜测代替你的原话。",
                    ) from error

        transcript = self._recognize_locally(samples, sample_rate)
        if is_low_information_transcript(transcript, dialogue=purpose != 'wake_word'):
            raise LocalCapabilityNotReadyError(
                "ASR_NO_MEANINGFUL_SPEECH", "这一段没有识别到可用内容。"
            )
        model_version = f"local-fallback:{ASR_MODEL_NAME}" if cloud_attempted else ASR_MODEL_NAME
        return transcript, duration_ms, model_version, False

    def _recognize_locally(self, samples: np.ndarray, sample_rate: int) -> str:
        # One native decoder owner per instance. A cancelled HTTP waiter does
        # not release this slot while its worker is still loading or decoding.
        if not self._asr_decode_slot.acquire(blocking=False):
            raise LocalCapabilityNotReadyError("LOCAL_ASR_BUSY", "本机语音识别正在处理上一段声音，请稍后再试。")
        try:
            files = self._asr_files(self.model_root / ASR_MODEL_NAME)
            if not all(path.is_file() for path in files.values()):
                raise LocalCapabilityNotReadyError("ASR_MODEL_NOT_READY", "本地中英双语语音模型尚未安装。")
            if self._asr_recognizer is None:
                import sherpa_onnx
    
                self._asr_recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
                    encoder=str(files["encoder"]), decoder=str(files["decoder"]),
                    joiner=str(files["joiner"]), tokens=str(files["tokens"]),
                    num_threads=2, decoding_method="greedy_search", provider="cpu",
                )
            stream = self._asr_recognizer.create_stream()
            stream.accept_waveform(sample_rate, samples)
            self._asr_recognizer.decode_stream(stream)
            transcript = stream.result.text.strip()
            if not transcript:
                raise LocalCapabilityNotReadyError("ASR_NO_SPEECH", "没有识别到清晰语音，请靠近麦克风重试。")
            return transcript
        finally:
            self._asr_decode_slot.release()

    def _cloud_asr_context(self, session: dict | None) -> dict:
        empty = {
            "text": "", "term_count": 0, "recent_turn_count": 0,
            "redaction_count": 0, "sha256": None,
        }
        if not session or not self.database or not self._cloud_asr or not self._cloud_asr.context_supported:
            return empty
        try:
            with self.database.connect() as connection:
                connection.execute("PRAGMA query_only = ON")
                owner = connection.execute(
                    "SELECT child_id FROM daily_session WHERE session_id = ?",
                    (session["session_id"],),
                ).fetchone()
                if not owner or owner["child_id"] != session["child_id"]:
                    return empty
                profile = connection.execute(
                    "SELECT nickname, grade, curriculum_region FROM child_profile WHERE child_id = ?",
                    (session["child_id"],),
                ).fetchone()
                calibration = connection.execute(
                    "SELECT vocabulary_json FROM child_voice_calibration WHERE child_id = ? AND status = 'ready'",
                    (session["child_id"],),
                ).fetchone()
                objective = connection.execute(
                    "SELECT summary FROM agent_objective WHERE child_id = ? AND status = 'active' ORDER BY updated_at DESC LIMIT 1",
                    (session["child_id"],),
                ).fetchone()
                source_filter = ""
                if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='companion_memory_source'").fetchone():
                    # Source identifiers outlive forgotten/expired content. Do
                    # not join live memories or initialize their schema here.
                    source_filter = """AND NOT EXISTS (
                        SELECT 1 FROM companion_memory_source s
                        WHERE s.child_id = t.child_id AND s.session_id = t.session_id
                          AND (s.turn_id = t.agent_turn_id OR s.action_id = t.client_action_id))"""
                candidates = connection.execute(
                    f"""SELECT transcript, response_text, action_json FROM agent_turn t
                       WHERE t.session_id = ? AND t.child_id = ? AND t.tool_status = 'completed'
                         AND t.intent NOT IN ('memory_command','speech_echo_ignored','repetition_confirmation')
                         {source_filter}
                       ORDER BY t.completed_at DESC, t.created_at DESC LIMIT 8""",
                    (session["session_id"], session["child_id"]),
                ).fetchall()
                recent = []
                for row in candidates:
                    try:
                        action = json.loads(row["action_json"])
                    except (TypeError, ValueError):
                        continue
                    if not isinstance(action, dict) or action.get("local_only") is True or action.get("cloud_transmission_allowed") is False or action.get("source") == "local_companion_memory":
                        continue
                    recent.append(row)
                    if len(recent) == 2:
                        break
        except Exception:
            return empty
        if not profile:
            return empty
        names = [str(profile["nickname"] or "")]
        words: list[str] = []
        try:
            loaded = json.loads(calibration["vocabulary_json"]) if calibration else []
            words = [str(word).strip() for word in loaded if str(word).strip()][:40]
        except (TypeError, ValueError, json.JSONDecodeError):
            words = []
        lines = [f"学习场景：{profile['curriculum_region']}{profile['grade']}学生的家庭学习对话。"]
        if words:
            lines.append("识别参考词：" + "、".join(words)[:500] + "。")
        if objective and objective["summary"]:
            lines.append("当前学习主题：" + str(objective["summary"])[:180] + "。")
        for row in reversed(recent):
            if row["transcript"]:
                lines.append("孩子刚才说：" + str(row["transcript"])[:160])
            if row["response_text"]:
                lines.append("学习伙伴刚才说：" + str(row["response_text"])[:160])
        raw = "\n".join(lines)[:1_200]
        sanitized = sanitize_cloud_text(raw, names)
        text = sanitized.text.strip()
        return {
            "text": text,
            "term_count": len(words),
            "recent_turn_count": len(recent),
            "redaction_count": sanitized.redaction_count,
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest() if text else None,
        }

    def _start_cloud_asr_call(
        self,
        session: dict | None,
        purpose: str,
        byte_count: int,
        duration_ms: int,
        context: dict | None = None,
        voice_activity: dict | None = None,
    ) -> str | None:
        if not self.repository or not session or not self._cloud_asr:
            return None
        try:
            return self.repository.start_model_call(
                session_id=session["session_id"],
                child_id=session["child_id"],
                purpose="speech_recognition",
                provider="qwen",
                model=self._cloud_asr.settings.cloud_asr_model,
                input_chars=0,
                prompt_version="cloud-asr-audio-context-v2",
                context_manifest={
                    "dialogue_purpose": purpose,
                    "audio_bytes": byte_count,
                    "duration_ms": duration_ms,
                    "speech_admission": {
                        key: voice_activity[key] for key in (
                            "admission_policy", "speech_frames", "longest_speech_run", "frame_ms",
                            "minimum_speech_frames", "minimum_contiguous_frames"
                        ) if voice_activity and key in voice_activity
                    },
                    "application_cloud_copy_retained": False,
                    "recognition_context_applied": bool(context and context.get("text")),
                    "recognition_context_sha256": context.get("sha256") if context else None,
                    "calibration_term_count": int(context.get("term_count", 0)) if context else 0,
                    "recent_turn_count": int(context.get("recent_turn_count", 0)) if context else 0,
                    "context_redaction_count": int(context.get("redaction_count", 0)) if context else 0,
                },
                risk_level="medium",
                provider_preference="cloud_asr_primary",
                policy_version="voice-cloud-v1",
                knowledge_version=None,
            )
        except Exception:
            return None

    def _finish_cloud_asr_call(self, call_id: str | None, **values) -> None:
        if not call_id or not self.repository:
            return
        try:
            self.repository.finish_model_call(
                call_id,
                verification_status="accepted" if values["status"] == "completed" else "rejected",
                safety_status="passed" if values["status"] == "completed" else "degraded",
                **values,
            )
        except Exception:
            pass

    def _session(self, session_id: str) -> dict:
        with self.database.connect() as connection:
            row = connection.execute("SELECT session_id, child_id, status, state FROM daily_session WHERE session_id = ?", (session_id,)).fetchone()
        if not row:
            raise UnknownSessionError(session_id)
        if row["status"] != "active":
            raise InvalidMediaInputError("学习会话已经结束。")
        return dict(row)

    def ignore_transcription(self, session_id: str, client_action_id: str) -> dict | None:
        """Keep completed local ASR durable but exclude a disconnected caller from agent/behavior work."""
        session = self._session_record(session_id)
        with self.database.connect() as connection:
            row, _ = self._assert_transcription_action_owner(connection, session, client_action_id)
            if not row:
                return None
            connection.execute("UPDATE voice_input SET status = 'ignored' WHERE voice_input_id = ? AND status = 'completed'", (row["voice_input_id"],))
            connection.execute("UPDATE voice_transcription_action SET status = 'ignored', updated_at = ? WHERE client_action_id = ? AND session_id = ? AND child_id = ?", (utc_now(), client_action_id, session_id, session["child_id"]))
            connection.commit()
            return self._voice_input(connection, row["voice_input_id"])

    def cancel_transcription(self, session_id: str, client_action_id: str) -> dict:
        """Record cancellation before a CPU-bound ASR call returns, so it cannot emit behavior evidence."""
        session = self._session_record(session_id)
        now = utc_now()
        with self.database.connect() as connection:
            row, action = self._assert_transcription_action_owner(connection, session, client_action_id)
            connection.execute(
                """INSERT INTO voice_transcription_action(client_action_id, session_id, child_id, status, updated_at) VALUES (?, ?, ?, 'cancelled', ?)
                ON CONFLICT(client_action_id) DO UPDATE SET status = 'cancelled', updated_at = excluded.updated_at
                WHERE voice_transcription_action.session_id = excluded.session_id AND voice_transcription_action.child_id = excluded.child_id""",
                (client_action_id, session_id, session["child_id"], now),
            )
            if row:
                connection.execute("UPDATE voice_input SET status = 'ignored' WHERE voice_input_id = ?", (row["voice_input_id"],))
                connection.execute("UPDATE voice_transcription_action SET status = 'ignored', updated_at = ? WHERE client_action_id = ? AND session_id = ? AND child_id = ?", (now, client_action_id, session_id, session["child_id"]))
                result = self._voice_input(connection, row["voice_input_id"])
            else:
                result = {"client_action_id": client_action_id, "status": "cancelled"}
            connection.commit()
            return result

    def transcription_action(self, session_id: str, client_action_id: str) -> dict | None:
        session = self._session_record(session_id)
        with self.database.connect() as connection:
            row, action = self._assert_transcription_action_owner(connection, session, client_action_id)
            if row:
                if action and action["status"] == "cancelled":
                    connection.execute("UPDATE voice_input SET status = 'ignored' WHERE voice_input_id = ?", (row["voice_input_id"],))
                    connection.execute("UPDATE voice_transcription_action SET status = 'ignored', updated_at = ? WHERE client_action_id = ? AND session_id = ? AND child_id = ?", (utc_now(), client_action_id, session_id, session["child_id"]))
                    connection.commit()
                return self._voice_input(connection, row["voice_input_id"])
            return {"client_action_id": client_action_id, "status": action["status"]} if action else None

    def _session_record(self, session_id: str) -> dict:
        with self.database.connect() as connection:
            row = connection.execute("SELECT session_id, child_id, status, state FROM daily_session WHERE session_id = ?", (session_id,)).fetchone()
        if not row:
            raise UnknownSessionError(session_id)
        return dict(row)

    @staticmethod
    def _assert_transcription_action_owner(connection, session: dict, client_action_id: str):
        voice = connection.execute("SELECT voice_input_id, session_id, child_id FROM voice_input WHERE client_action_id = ?", (client_action_id,)).fetchone()
        action = connection.execute("SELECT session_id, child_id, status FROM voice_transcription_action WHERE client_action_id = ?", (client_action_id,)).fetchone()
        for record in (voice, action):
            if record and (record["session_id"] != session["session_id"] or record["child_id"] != session["child_id"]):
                raise TranscriptionActionConflictError("语音操作号已属于另一位孩子或学习会话。")
        return voice, action

    @contextmanager
    def _raw_publication(self, connection, child_id, session_id, object_id, suffix, data, retain):
        path = None
        try:
            path = self._store_raw(child_id, session_id, object_id, suffix, data) if retain else None
            yield path
            connection.commit()
        except BaseException:
            connection.rollback()
            if path is not None:
                # A lost commit acknowledgement must not erase a durable row's file.
                referenced = connection.execute("""SELECT 1 FROM voice_input WHERE local_path=?
                    UNION ALL SELECT 1 FROM capture_artifact WHERE local_path=? LIMIT 1""",
                    (path, path)).fetchone()
                if not referenced:
                    Path(path).unlink(missing_ok=True)
            raise

    def _store_raw(self, child_id: str, session_id: str, object_id: str, suffix: str, data: bytes) -> str:
        if not self.data_encryption_key:
            raise LocalCapabilityNotReadyError(
                "DATA_ENCRYPTION_KEY_NOT_READY",
                "系统安全存储密钥不可用，原始媒体没有保存。",
            )
        directory = self.data_dir / "media" / child_id / session_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{object_id}{suffix}.enc"
        nonce = os.urandom(12)
        encrypted = AESGCM(self.data_encryption_key).encrypt(nonce, data, b"hsc-raw-media-v1")
        try:
            path.write_bytes(b"HSC1" + nonce + encrypted)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return str(path)

    @staticmethod
    def _image_quality(image: np.ndarray) -> dict:
        from .local_ocr.service import _page_content_quality

        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        brightness = float(gray.mean())
        sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        dark_ratio = float((gray < 45).mean())
        bright_ratio = float((gray > 250).mean())
        possible_hand_ratio = LocalMediaAI._possible_hand_region_ratio(image)
        issues, content_metrics = _page_content_quality(gray)
        messages = [{
            'glare': '文字区域有疑似反光缺损，请调整灯光并核对原稿。',
            'content_unverifiable': '未能确认清晰笔画，请检查页面并重新采集。',
            'low_contrast': '笔画与纸面反差不足，请核对原稿或重新采集。',
            'content_integrity_uncertain': '文字区域存在不确定的空缺，请核对原稿。',
        }[issue] for issue in issues]
        if sharpness < 18:
            issues.append("blurred")
            messages.append("画面偏模糊，请对焦后重拍。")
        if brightness < 45:
            issues.append("too_dark")
            messages.append("画面整体过暗，请增加照明。")
        if (brightness > 248 or bright_ratio > 0.985) and any(
                issue in {'glare', 'low_contrast'} for issue in issues):
            issues.append("overexposed")
            messages.append("画面过亮或有大面积过曝，请调整灯光角度。")
        if dark_ratio > 0.38 and brightness >= 45:
            issues.append("heavy_shadow")
            messages.append("画面阴影较重，请移开遮挡或调整灯光。")
        if possible_hand_ratio >= 0.035:
            issues.append("possible_hand_occlusion")
            messages.append("页面中可能有手部遮挡，请移开手并检查题目是否完整。")
        return {
            "passed": len(issues) == 0,
            "issues": issues,
            "messages": messages,
            "metrics": {
                "brightness": round(brightness, 1),
                "sharpness": round(sharpness, 1),
                "dark_ratio": round(dark_ratio, 4),
                "bright_ratio": round(bright_ratio, 4),
                "possible_hand_region_ratio": round(possible_hand_ratio, 4),
                **content_metrics,
            },
            "version": "document_quality_v3",
        }

    @staticmethod
    def _merge_ocr_quality(capture_quality: dict, pipeline_quality: dict) -> dict:
        """Keep capture safety warnings while preserving OCR pipeline evidence."""
        merged = json.loads(json.dumps(capture_quality, ensure_ascii=False))
        pipeline = json.loads(json.dumps(pipeline_quality, ensure_ascii=False))
        merged["issues"] = list(dict.fromkeys(merged.get("issues", []) + pipeline.get("issues", [])))
        merged["messages"] = list(dict.fromkeys(merged.get("messages", [])))
        for issue in pipeline.get("issues", []):
            if issue not in capture_quality.get("issues", []):
                merged["messages"].append(f"OCR预处理提示：{issue}。请人工确认页面。")
        merged["passed"] = not merged["issues"]
        merged["pipeline_metrics"] = pipeline.get("metrics", {})
        merged["pipeline_transforms"] = pipeline.get("transforms", {})
        merged["pipeline_stages"] = pipeline.get("stages", [])
        return merged

    @staticmethod
    def _possible_hand_region_ratio(image: np.ndarray) -> float:
        height, width = image.shape[:2]
        ycrcb = cv2.cvtColor(image, cv2.COLOR_BGR2YCrCb)
        skin_mask = cv2.inRange(
            ycrcb,
            np.array((35, 133, 77), dtype=np.uint8),
            np.array((245, 180, 135), dtype=np.uint8),
        )
        kernel_size = max(3, round(min(height, width) * 0.008))
        if kernel_size % 2 == 0:
            kernel_size += 1
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel_size, kernel_size))
        skin_mask = cv2.morphologyEx(skin_mask, cv2.MORPH_OPEN, kernel)
        component_count, _, stats, _ = cv2.connectedComponentsWithStats(skin_mask, 8)
        image_area = float(height * width)
        largest_candidate = 0.0
        inner_left, inner_top = width * 0.08, height * 0.08
        inner_right, inner_bottom = width * 0.92, height * 0.92
        for index in range(1, component_count):
            x, y, component_width, component_height, area = stats[index]
            area_ratio = float(area) / image_area
            if area_ratio < 0.02 or area_ratio > 0.35:
                continue
            if x + component_width < inner_left or x > inner_right or y + component_height < inner_top or y > inner_bottom:
                continue
            fill_ratio = float(area) / max(1.0, float(component_width * component_height))
            if fill_ratio < 0.28:
                continue
            largest_candidate = max(largest_candidate, area_ratio)
        return largest_candidate

    @staticmethod
    def _content_near_edge(boxes, width: int, height: int) -> bool:
        if not boxes:
            return False
        margin_x = max(4, round(width * 0.01))
        margin_y = max(4, round(height * 0.01))
        for box in boxes:
            points = np.asarray(box)
            if points.size == 0:
                continue
            if points[:, 0].min() <= margin_x or points[:, 0].max() >= width - margin_x:
                return True
            if points[:, 1].min() <= margin_y or points[:, 1].max() >= height - margin_y:
                return True
        return False

    @staticmethod
    def _asr_files(directory: Path) -> dict[str, Path]:
        return {
            "encoder": directory / "encoder-epoch-34-avg-19.int8.onnx",
            "decoder": directory / "decoder-epoch-34-avg-19.onnx",
            "joiner": directory / "joiner-epoch-34-avg-19.int8.onnx",
            "tokens": directory / "tokens.txt",
        }

    @staticmethod
    def _artifact(connection, artifact_id: str) -> dict:
        row = connection.execute("SELECT artifact_id, child_id, session_id, artifact_type, local_path, extracted_text, confidence, details_json, status, created_at FROM capture_artifact WHERE artifact_id = ?", (artifact_id,)).fetchone()
        result = dict(row)
        result["details"] = json.loads(result.pop("details_json"))
        return result

    def _artifact_for_workflow(self, artifact_id: str) -> dict:
        """Return the final artifact after a local pipeline has appended evidence."""
        with self.database.connect() as connection:
            return self._artifact(connection, artifact_id)

    @staticmethod
    def _voice_input(connection, voice_input_id: str) -> dict:
        row = connection.execute("SELECT voice_input_id, child_id, session_id, purpose, transcript, local_path, model_version, duration_ms, status, created_at, client_action_id FROM voice_input WHERE voice_input_id = ?", (voice_input_id,)).fetchone()
        result = dict(row)
        result["cloud_used"] = str(result.get("model_version", "")).startswith("cloud:")
        return result


class InvalidMediaInputError(Exception):
    pass


class LocalCapabilityNotReadyError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
