"""Local PET media references and delivery journal, never a second scoring store.

Only server-owned media roots and existing artifact/voice IDs are accepted. Parent
source review is an authenticated integration operation, not a client quality flag.
Playback receipts attest reported rendering, not attention or understanding.

Integration contract:
* Initialize with a server-configured managed media_root, never a request path.
* Freeze prompt_id/item_version on arrival, not after transcription/model latency.
* Listening needs an existing owned capture_artifact of type audio, completed.
  Speaking needs completed pet_speaking/pet_read_aloud voice_input with retained
  raw PCM. Neither TTS audit nor a board callback creates such a source here.
* record_playback takes the decoded audio SHA256, actual PCM frame cursor and
  monotonic milliseconds. No seek/skip, duration estimate or VAD substitution.
  Lost acknowledgements retry the exact action/payload. Resume changes generation
  and replays the whole clip. Reads never initiate playback, including restart.
  A valid actual ended arriving before the server's minimum interval is durably
  pending, not rejected or certified. Retry the saved request after the returned
  delay; do not resume/replay. Reads do not settle it. Recovery uses its persisted
  UTC deadline when the original process's monotonic epoch is no longer present.
* review_listening_source reviewer identity/role must come from parent auth.
  The review attests stimulus correspondence only, not learning or ASR accuracy.
* attach_assessment references an existing reviewed original attempt. Current
  rubrics are text-only: all audio skill/stage/certificate grants remain false.
* Export/delete integration must include PET_MEDIA_TABLES; no media is copied.
  This module and its projections are local-only and must not enter ModelRequest.
"""

from array import array
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import math
from pathlib import Path
import sys
import time
import uuid
import wave

from .diagnostic_prompt import DiagnosticPromptError, DiagnosticPromptService, _version
from .learning_obligations import project_child_mastery_evidence
from .learning_rubric import RubricEvidenceService
from .pet_media_files import read_managed_media
from .pet_skill_gate import pet_skill_gate
from .pet_listening_requirement import listening_requirement


VERSION = "pet_media_evidence_v1"
PET_MEDIA_TABLES = ("pet_media_binding", "pet_media_event")
_PROCESS_EPOCH = uuid.uuid4().hex
_MAX_BYTES = 24 * 1024 * 1024
_RECEIPT_GRACE_MS = 2000


class PetMediaEvidenceError(ValueError):
    pass


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _now():
    return datetime.now(timezone.utc)


def _clock_ms():
    return time.monotonic_ns() // 1000000


def _date(value):
    value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        raise ValueError("timezone_required")
    return value


def _text(value, name):
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise PetMediaEvidenceError("invalid_" + name)


def _integer(value, name, minimum=0):
    if type(value) is not int or not minimum <= value <= 2**53 - 1:
        raise PetMediaEvidenceError("invalid_" + name)


def _pending_delay(state, runtime_epoch, now):
    pending = state.get("pending_end")
    if not pending:
        return 0
    remaining = (pending["settle_server_ms"] - _clock_ms() if runtime_epoch == state["runtime_epoch"] else
                 (_date(pending["settle_at"]) - now).total_seconds() * 1000)
    return math.ceil(max(0, remaining))


def _read_audio(local_path, *, media_root, data_encryption_key=None):
    content = read_managed_media(local_path, media_root=media_root, max_bytes=_MAX_BYTES)
    if content.startswith(b"HSC1"):
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        if not data_encryption_key or len(content) < 32:
            raise ValueError("media_decryption_key_required")
        try:
            content = AESGCM(data_encryption_key).decrypt(content[4:16], content[16:], b"hsc-raw-media-v1")
        except InvalidTag as exc:
            raise ValueError("media_decryption_failed") from exc
    return content


def inspect_local_pcm(local_path, *, media_root, data_encryption_key=None):
    """Bounded no-network PCM inspection. Signal thresholds are engineering policy.

    No speech detection, speaker identity, pronunciation or authenticity inference.
    Read via pinned POSIX/Win32 handles, including replacement-race protection.
    """
    result = {"status": "missing_audio", "sha256": None,
              "quality_scope": "signal_only_not_speech_or_identity", "policy_version": "pcm_signal_v1"}
    if not local_path:
        return result
    try:
        content = _read_audio(local_path, media_root=media_root, data_encryption_key=data_encryption_key)
        result["sha256"] = hashlib.sha256(content).hexdigest()
        with wave.open(io.BytesIO(content), "rb") as audio:
            frames, rate = audio.getnframes(), audio.getframerate()
            if (audio.getcomptype() != "NONE" or audio.getsampwidth() != 2 or audio.getnchannels() != 1
                    or rate not in {16000, 24000, 48000} or not rate // 2 <= frames <= rate * 600):
                raise ValueError("unsupported_pcm_format_or_length")
            pcm = audio.readframes(frames)
            if len(pcm) != frames * 2:
                raise ValueError("truncated_audio")
        samples = array("h")
        samples.frombytes(pcm)
        if sys.byteorder != "little":
            samples.byteswap()
        rms = math.sqrt(sum(int(sample) ** 2 for sample in samples) / frames)
        clipping = sum(abs(sample) >= 32760 for sample in samples) / frames
        result.update(frames=frames, sample_rate=rate, channels=1, sample_width=2,
                      rms=round(rms, 3), clipping_fraction=round(clipping, 6),
                      status="usable_signal" if rms >= 100 and clipping <= .01 else "poor_signal")
    except OSError:
        result.update(status="unavailable_or_unsupported_audio", retryable_io=True)
    except (ValueError, EOFError, wave.Error, AttributeError, NotImplementedError):
        result["status"] = "unavailable_or_unsupported_audio"
    return result


def _session(c, session_id, *, active=False):
    try:
        row = DiagnosticPromptService._session(c, session_id)
    except DiagnosticPromptError as exc:
        raise PetMediaEvidenceError(str(exc)) from exc
    if active and (row["status"] != "active" or row["child_status"] != "active"
                   or row["state"] in {"evolving", "completed"}):
        raise PetMediaEvidenceError("inactive_session")
    return row


def _owned(c, session_id, media_id):
    session = _session(c, session_id)
    row = c.execute("SELECT * FROM pet_media_binding WHERE media_id=?", (media_id,)).fetchone()
    if not row or (row["child_id"], row["session_id"]) != (session["child_id"], session_id):
        raise PetMediaEvidenceError("media_ownership_conflict")
    return dict(row)


def _prompt(c, session_id, prompt_id, item_version):
    session = _session(c, session_id)
    try:
        prompt = DiagnosticPromptService._owned(c, session, prompt_id)
    except DiagnosticPromptError as exc:
        raise PetMediaEvidenceError("prompt_ownership_conflict") from exc
    item = DiagnosticPromptService._item(c, session["child_id"], prompt["diagnostic_item_id"])
    if (prompt["status"] in {"cancelled", "invalidated"} or item is None
            or prompt["item_version"] != item_version or _version(item) != item_version):
        raise PetMediaEvidenceError("prompt_source_changed_or_withdrawn")
    from .listening_modality import school_listening_node
    node = c.execute('SELECT * FROM knowledge_node WHERE knowledge_node_id=?', (item['knowledge_node_id'],)).fetchone()
    if item["track"] != "pet" and not (node and school_listening_node(node) and item['answer_type'] == 'listening_text'):
        raise PetMediaEvidenceError("not_pet_item")
    from .exam_listening_delivery import prompt_source
    try:
        prompt_source(c,prompt)
        from .exam_listening_delivery import exam_owner,VERSION,frozen
        if exam_owner(prompt) and VERSION in json.loads(prompt['request_json']):
            if frozen(c,prompt_id)[3]['prompt_id'] != prompt_id:
                raise PetMediaEvidenceError('exam_unit_canonical_prompt_required')
    except ValueError as exc:
        raise PetMediaEvidenceError(str(exc)) from exc
    return prompt, item


def _source(c, row):
    if 'state_json' in row.keys() and json.loads(row['state_json']).get('source_reuse'):
        from .pet_listening_reuse import reused_source
        return reused_source(c, row)
    listening = row["kind"] == "listening"
    table, field = ("capture_artifact", "artifact_id") if listening else ("voice_input", "voice_input_id")
    source = c.execute(f"SELECT * FROM {table} WHERE {field}=? AND child_id=? AND session_id=?",
                       (row[field], row["child_id"], row["session_id"])).fetchone()
    if source is None:
        raise PetMediaEvidenceError("media_source_ownership_or_missing")
    source = dict(source)
    if source["status"] != "completed":
        raise PetMediaEvidenceError("media_source_not_current")
    if listening and source["artifact_type"] != "audio":
        raise PetMediaEvidenceError("not_audio_artifact")
    if listening:
        details = json.loads(source["details_json"])
        if details.get("origin") == "local_synthesized_practice" and any(
                details.get(key) != row[key] for key in ("prompt_id", "item_version", "diagnostic_item_id")):
            raise PetMediaEvidenceError("practice_material_original_binding_conflict")
    if not listening and source["purpose"] not in {"pet_speaking", "pet_read_aloud"}:
        raise PetMediaEvidenceError("not_pet_voice_input")
    if not listening:
        action = c.execute("SELECT * FROM voice_transcription_action WHERE client_action_id=?",
                           (source["client_action_id"],)).fetchone()
        if action and (action["status"] != "completed" or
                       (action["session_id"], action["child_id"]) != (row["session_id"], row["child_id"])):
            raise PetMediaEvidenceError("voice_action_cancelled_or_rebound")
    return source


def _current(c, row, media_root, data_encryption_key=None):
    reasons = []
    prompt = item = source = None
    audio = {"status": "missing_audio", "sha256": None}
    try:
        prompt, item = _prompt(c, row["session_id"], row["prompt_id"], row["item_version"])
        if _session(c, row["session_id"])["child_status"] != "active":
            reasons.append("child_inactive")
    except PetMediaEvidenceError as exc:
        reasons.append(str(exc))
    try:
        source = _source(c, row)
        if _hash(source) != row["source_version"]:
            reasons.append("media_source_revised")
        audio = inspect_local_pcm(source["local_path"], media_root=media_root, data_encryption_key=data_encryption_key)
        if audio["sha256"] != json.loads(row["audio_json"])["sha256"]:
            reasons.append("audio_changed")
        if audio["status"] != "usable_signal":
            reasons.append("actual_audio_missing_or_poor_quality")
    except PetMediaEvidenceError as exc:
        reasons.append(str(exc))
    return prompt, item, source, audio, reasons


def _assessment(c, row, prompt, item, source, now):
    """Reuse the immutable rubric and the exact original diagnostic attempt."""
    if not row["assessment_id"]:
        return None, "assessment_missing"
    rubric = RubricEvidenceService(None)
    if not rubric.available(c) or not prompt or not prompt["attempt_id"]:
        return None, "original_attempt_missing"
    assessment = c.execute("SELECT * FROM rubric_assessment WHERE assessment_id=? AND child_id=?",
                           (row["assessment_id"], row["child_id"])).fetchone()
    attempt = c.execute("SELECT * FROM diagnostic_attempt WHERE attempt_id=? AND child_id=? AND session_id=?",
                        (prompt["attempt_id"], row["child_id"], row["session_id"])).fetchone()
    if not assessment or not attempt or attempt["diagnostic_item_id"] != item["diagnostic_item_id"]:
        return None, "assessment_ownership_or_attempt_conflict"
    current = rubric.get(row["child_id"], row["assessment_id"], connection=c)
    if (current["session_id"] != row["session_id"] or current["source_evidence_id"] != attempt["evidence_id"]
            or current["question"] != item["prompt"] or current["response"] != attempt["submitted_answer"]
            or current["material"].get("diagnostic_item_id") != item["diagnostic_item_id"]
            or (row["kind"] == "speaking" and current["response"] != source["transcript"])):
        return None, "assessment_original_response_conflict"
    receipt = rubric.reviewed_receipt(c, row["child_id"], assessment["step_id"],
        assessment_id=assessment["assessment_id"], now=now)
    if not receipt:
        return None, "assessment_unreviewed_or_withdrawn"
    return receipt, None


def project_pet_media_evidence(connection, session_id, *, media_id, media_root, runtime_epoch=None, as_of=None,
                               data_encryption_key=None):
    """Same-connection read-only projection; never scores, plays, or fetches media.

    Omit runtime_epoch for planning/report callers: running is then resumable only.
    """
    c = connection
    owned_transaction = not c.in_transaction
    if owned_transaction:
        c.execute("BEGIN")
    try:
        row = _owned(c, session_id, media_id)
        now = _date(as_of) if as_of else _now()
        prompt, item, source, audio, reasons = _current(c, row, media_root, data_encryption_key)
        state = json.loads(row["state_json"])
        if _date(row["created_at"]) > now:
            reasons.append("binding_in_future")
        if source:
            try:
                if _date(source["created_at"]) > now:
                    reasons.append("media_source_in_future")
            except (ValueError, TypeError):
                reasons.append("media_source_time_invalid")
        if state["status"] == "cancelled":
            reasons.append("media_cancelled")
        from .pet_listening_reuse import listening_source_review
        review = listening_source_review(c, row)
        approved = bool(review and json.loads(review["payload_json"])["operation"] == "approve"
                        and _date(review["created_at"]) <= now)
        delivery = bool(not reasons and row["kind"] == "listening" and approved and state.get("completed_at")
                        and _date(state["completed_at"]) <= now)
        receipt = None
        assessment_reason = "assessment_missing"
        if not reasons:
            receipt, assessment_reason = _assessment(c, row, prompt, item, source, now)
        answer = attempt = None
        if not reasons and prompt and prompt["attempt_id"]:
            attempt = c.execute("SELECT * FROM diagnostic_attempt WHERE attempt_id=? AND child_id=? AND session_id=?",
                                (prompt["attempt_id"], row["child_id"], session_id)).fetchone()
            if attempt:
                projections = project_child_mastery_evidence(c, row["child_id"], as_of=now.isoformat())
                answer = next((e for projection in projections.values() for e in projection["effective_evidence"]
                               if e["evidence_id"] == attempt["evidence_id"]
                               and e["diagnostic_item_id"] == row["diagnostic_item_id"]), None)
        reliable_answer = bool(answer and answer["correctness"] == 1 and answer["independent"] and not answer["hint_level"])
        end_events = c.execute("""SELECT event_id,generation,created_at,payload_json FROM pet_media_event
            WHERE media_id=? AND operation IN ('playback','playback_settled') ORDER BY rowid""", (media_id,)).fetchall()
        completed = [e for e in end_events if json.loads(e["payload_json"])["event"] == "ended"
                     and json.loads(e["payload_json"]).get("receipt_state", "accepted") == "accepted"
                     and _date(e["created_at"]) <= now]
        delivery_receipt = ({"event_id": completed[0]["event_id"], "generation": completed[0]["generation"],
            "occurred_at": completed[0]["created_at"], "complete_play_count": len(completed),
            "audio_sha256": audio["sha256"], "source_review_event_id": review["event_id"]} if delivery and completed else None)
        delivery = bool(delivery_receipt)
        frozen_requirement = state.get("delivery_requirement")
        current_requirement = listening_requirement(c, row["child_id"], row["diagnostic_item_id"]) if row["kind"] == "listening" else None
        requirement_current = bool(frozen_requirement and current_requirement and frozen_requirement["version"]
                                   and frozen_requirement == current_requirement)
        task_plays = [e for e in completed if requirement_current and
                      json.loads(e["payload_json"]).get("requirement_version") == frozen_requirement["version"]]
        task_complete = bool(delivery and requirement_current and len(task_plays) >= frozen_requirement["required_play_count"])
        answer_after_delivery = bool(delivery and answer and _date(answer["occurred_at"]) >= _date(delivery_receipt["occurred_at"]))
        modality_gate = pet_skill_gate(c, row["child_id"], row["diagnostic_item_id"],
            session_id=session_id, prompt_id=row["prompt_id"], item_version=row["item_version"],
            attempt_id=prompt["attempt_id"] if prompt else None, media_root=media_root,
            evidence_id=attempt['evidence_id'] if attempt else None,
            data_encryption_key=data_encryption_key, as_of=now.isoformat())
        if row['kind'] == 'speaking' and modality_gate.get('assessment_id'):
            rubric = RubricEvidenceService(None)
            current = rubric.get(row['child_id'], modality_gate['assessment_id'], connection=c)
            if current['review']['audio_evidence']['media_id'] == media_id:
                receipt = rubric.reviewed_receipt(c, row['child_id'], current['step_id'],
                    assessment_id=current['assessment_id'], now=now)
                if receipt:
                    assessment_reason = None
                    reliable_answer = bool(receipt['correctness'] == 1 and receipt['independent'] and not receipt['hint_level'])
        full_reference_available = False
        if row['kind'] == 'listening' and not reasons and approved:
            from .pet_playback_reference import verified_text
            try:
                verified_text(row, item, source, audio, review)
                full_reference_available = True
            except (PetMediaEvidenceError, ValueError, TypeError, KeyError):
                pass
        qualification = {"full_audio_control_reference_available": full_reference_available,
            "listening_delivery_qualified": delivery,
            "clip_delivery_qualified": delivery, "delivery_unit": "single_audio_clip",
            "task_delivery_qualified": task_complete,
            "required_play_count": frozen_requirement["required_play_count"] if requirement_current else None,
            "delivery_requirement": frozen_requirement, "current_delivery_requirement": current_requirement,
            "requirement_current": requirement_current, "task_complete_play_count": len(task_plays),
            "requires_new_binding": row["kind"] == "listening" and not requirement_current,
            "skill_scoring_allowed": modality_gate["skill_scoring_allowed"],
            "modality_gate": modality_gate,
            "audio_signal_usable": not reasons and audio["status"] == "usable_signal",
            "answer_evidence_qualified": reliable_answer,
            "content_review_qualified": bool(receipt and receipt["correctness"] == 1 and receipt["independent"]
                                             and not receipt["hint_level"]),
            "answer_after_delivery": answer_after_delivery,
            "skill_qualified": bool(reliable_answer and modality_gate["skill_scoring_allowed"]), "stage_advance_allowed": False,
            "mastery_claim_allowed": False, "certification_claim_allowed": False,
            "assessment_scope": modality_gate.get('assessment_scope', "existing_text_or_exact_answer_only"),
            "blockers": reasons + ([] if row["kind"] != "listening" or delivery else ["original_audio_delivery_unverified"])
                + ([assessment_reason] if assessment_reason else [])
                + (["audio_transcript_alignment_unverified"] if row["kind"] == "speaking" and not modality_gate['skill_scoring_allowed'] else [])
                + modality_gate["blockers"],
            "answer_evidence_id": answer["evidence_id"] if answer else None,
            "assessment_receipt": ({key: receipt[key] for key in (
                "evidence_id", "assessment_id", "review_revision", "score_status", "occurred_at",
                "correctness", "independent", "hint_level")} if receipt else None),
            "delivery_receipt": delivery_receipt}
        status = state["status"]
        resume_required = status in {"paused", "playing"}
        if reasons:
            status = "cancelled" if state["status"] == "cancelled" else "invalidated"
            resume_required = False
        elif status == "playing" and runtime_epoch != state["runtime_epoch"]:
            status = "paused"
        result = {"version": VERSION, "media_id": media_id, "child_id": row["child_id"], "session_id": session_id,
            "prompt_id": row["prompt_id"], "diagnostic_item_id": row["diagnostic_item_id"],
            "item_version": row["item_version"], "source_version": row["source_version"], "kind": row["kind"],
            "artifact_id": row["artifact_id"], "voice_input_id": row["voice_input_id"],
            "revision": state["revision"], "generation": state["generation"], "status": status,
            "resume_required": resume_required, "frame_position": state.get("frame_position", 0),
            "audio": audio, "source_reviewed": approved and not reasons, "qualification": qualification,
            "local_only": True, "cloud_transmission_allowed": False,
            "delivery_scope": "local_player_reported_rendering_not_attention_or_comprehension"}
        result["receipt_grace_ms"] = _RECEIPT_GRACE_MS
        result["receipt_retry_after_ms"] = _pending_delay(state, runtime_epoch, now) if status == "pending_receipt" else 0
        result["pending_playback_receipt"] = (state["pending_end"]["request"] if status == "pending_receipt" else None)
        result["evidence_version"] = _hash({"row": row, "review": dict(review) if review else None,
            "audio": audio, "qualification": qualification})
        return result
    finally:
        if owned_transaction:
            c.rollback()


class PetMediaEvidenceService:
    def __init__(self, database, *, media_root, runtime_epoch=None, data_encryption_key=None):
        self.database = database
        self.media_root = Path(media_root).absolute()
        self.runtime_epoch = runtime_epoch or _PROCESS_EPOCH
        self.data_encryption_key = data_encryption_key
        with database.connect() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS pet_media_binding (
                    media_id TEXT PRIMARY KEY,
                    child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                    session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
                    prompt_id TEXT NOT NULL REFERENCES diagnostic_prompt(prompt_id) ON DELETE CASCADE,
                    diagnostic_item_id TEXT NOT NULL REFERENCES diagnostic_item(diagnostic_item_id) ON DELETE CASCADE,
                    item_version TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('listening','speaking')),
                    artifact_id TEXT REFERENCES capture_artifact(artifact_id) ON DELETE CASCADE,
                    voice_input_id TEXT REFERENCES voice_input(voice_input_id) ON DELETE CASCADE,
                    source_version TEXT NOT NULL, audio_json TEXT NOT NULL, state_json TEXT NOT NULL,
                    assessment_id TEXT, created_at TEXT NOT NULL,
                    CHECK ((kind='listening' AND artifact_id IS NOT NULL AND voice_input_id IS NULL)
                        OR (kind='speaking' AND voice_input_id IS NOT NULL AND artifact_id IS NULL))
                );
                CREATE TABLE IF NOT EXISTS pet_media_event (
                    event_id TEXT PRIMARY KEY,
                    media_id TEXT NOT NULL REFERENCES pet_media_binding(media_id) ON DELETE CASCADE,
                    client_action_id TEXT NOT NULL UNIQUE, operation TEXT NOT NULL, request_hash TEXT NOT NULL,
                    generation INTEGER NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS pet_media_prompt ON pet_media_binding(prompt_id,kind);
                CREATE INDEX IF NOT EXISTS pet_media_events ON pet_media_event(media_id);
                CREATE TRIGGER IF NOT EXISTS pet_media_event_no_update BEFORE UPDATE ON pet_media_event
                    BEGIN SELECT RAISE(ABORT,'PET_MEDIA_HISTORY_IMMUTABLE'); END;
            """)

    def _view(self, c, session_id, media_id):
        return project_pet_media_evidence(c, session_id, media_id=media_id,
            media_root=self.media_root, runtime_epoch=self.runtime_epoch, data_encryption_key=self.data_encryption_key)

    def read(self, session_id, *, media_id):
        with self.database.connect() as c:
            return self._view(c, session_id, media_id)

    def playback_action_receipt(self, session_id, *, media_id, client_action_id):
        """Read the original action separately from the current mutable projection."""
        _text(client_action_id, "client_action_id")
        with self.database.connect() as c:
            c.execute("BEGIN")
            _owned(c, session_id, media_id)
            current = self._view(c, session_id, media_id)
            event = c.execute("SELECT * FROM pet_media_event WHERE media_id=? AND client_action_id=?",
                              (media_id, client_action_id)).fetchone()
            receipt, decision = None, "not_recorded"
            if event and event["operation"] in {"control", "playback"}:
                payload = json.loads(event["payload_json"])
                settled = c.execute("""SELECT * FROM pet_media_event WHERE media_id=? AND operation='playback_settled'
                    AND json_extract(payload_json,'$.pending_event_id')=?""", (media_id, event["event_id"])).fetchone()
                ended = payload.get("event") == "ended"
                accepted = ended and (payload.get("receipt_state", "accepted") == "accepted" or bool(settled))
                decision = "reuse_completed" if accepted else "retry_pending_receipt" if ended else "await_recovery"
                fields = ("generation", "expected_revision", "command") if event["operation"] == "control" else (
                    "generation", "expected_revision", "event", "audio_sha256", "frame_position", "monotonic_ms")
                historical = payload.get("result_state")
                if event["operation"] == "control" and "expected_revision" in payload:
                    historical = {"status": {"pause": "paused", "resume": "playing", "restart": "playing", "cancel": "cancelled"}[payload["command"]],
                        "generation": event["generation"], "revision": payload["expected_revision"] + 1, "frame_position": 0}
                receipt = {"event_id": event["event_id"], "operation": event["operation"],
                    "created_at": event["created_at"], "generation": event["generation"],
                    "request": {"media_id": media_id, "client_action_id": client_action_id,
                                **{key: payload[key] for key in fields if key in payload}},
                    "historical_result": historical,
                    "completion_accepted": accepted, "settled_event_id": settled["event_id"] if settled else None}
            # Preserve actual past renders, but never turn a withdrawn parent
            # source review into a current successful playback instruction.
            if current["status"] in {"invalidated", "cancelled"} or (
                    receipt and current["kind"] == "listening" and not current["source_reviewed"]):
                decision = "current_source_invalid"
            elif receipt and receipt["generation"] != current["generation"] and decision != "reuse_completed":
                decision = "await_recovery"
            return {"child_id": current["child_id"], "session_id": session_id, "media_id": media_id,
                "client_action_id": client_action_id, "receipt": receipt, "replay_decision": decision,
                "current": current, "automatic_playback_allowed": False,
                "local_only": True, "cloud_transmission_allowed": False}

    def materialize_listening(self, session_id, *, prompt_id, item_version, client_action_id):
        """Internal parent-authenticated creation, never automatic child TTS."""
        from .pet_material_supply import materialize
        return materialize(self, session_id, prompt_id=prompt_id, item_version=item_version,
                           client_action_id=client_action_id)

    def speaking_readiness(self, session_id, *, media_id):
        from .pet_speaking_acoustics import project_speaking_readiness
        with self.database.connect() as c:
            return project_speaking_readiness(c, session_id, media_id=media_id,
                media_root=self.media_root, data_encryption_key=self.data_encryption_key)

    def speaking_review(self, session_id, *, media_id, assessment_id):
        from .pet_speaking_review import prepare_speaking_audio_review
        with self.database.connect() as c:
            try:
                return prepare_speaking_audio_review(c, session_id, media_id=media_id, assessment_id=assessment_id,
                    media_root=self.media_root, data_encryption_key=self.data_encryption_key)
            except ValueError as exc:
                raise PetMediaEvidenceError(str(exc)) from exc

    def speaking_review_audio(self, session_id, *, media_id, assessment_id, audio_sha256):
        from .pet_speaking_review import read_speaking_review_audio
        with self.database.connect() as c:
            try:
                return read_speaking_review_audio(c, session_id, media_id=media_id, assessment_id=assessment_id,
                    audio_sha256=audio_sha256, media_root=self.media_root, data_encryption_key=self.data_encryption_key)
            except ValueError as exc:
                raise PetMediaEvidenceError(str(exc)) from exc

    def list_for_prompt(self, session_id, *, prompt_id, item_version):
        """Exact owned prompt collection; no newest/active media fallback or writes."""
        with self.database.connect() as c:
            c.execute("BEGIN")
            prompt, _ = _prompt(c, session_id, prompt_id, item_version)
            rows = c.execute("""SELECT * FROM pet_media_binding
                WHERE prompt_id=? AND child_id=? AND session_id=? AND item_version=?
                ORDER BY created_at,media_id LIMIT 65""", (prompt_id, prompt["child_id"], session_id, item_version)).fetchall()
            if len(rows) > 64:
                raise PetMediaEvidenceError("prompt_media_limit_requires_review")
            result = {"prompt_id": prompt_id, "item_version": item_version, "child_id": prompt["child_id"],
                "session_id": session_id, "listening": [], "speaking": [], "selection_required": False,
                "status": "bound_media" if rows else "missing_media",
                "local_only": True, "cloud_transmission_allowed": False}
            from .pet_material_supply import material_projection
            materials = c.execute("""SELECT * FROM capture_artifact WHERE child_id=? AND session_id=?
                AND json_extract(details_json,'$.origin')='local_synthesized_practice'
                AND json_extract(details_json,'$.prompt_id')=? AND json_extract(details_json,'$.item_version')=?
                ORDER BY created_at,artifact_id LIMIT 65""", (prompt["child_id"], session_id, prompt_id, item_version)).fetchall()
            if len(materials) > 64:
                raise PetMediaEvidenceError("prompt_media_limit_requires_review")
            result["materials"] = [material_projection(material) for material in materials]
            attempt = c.execute("""SELECT * FROM diagnostic_attempt WHERE attempt_id=?
                AND child_id=? AND session_id=?""", (prompt['attempt_id'], prompt['child_id'], session_id)).fetchone() if prompt['attempt_id'] else None
            for row in rows:
                media = self._view(c, session_id, row["media_id"])
                if row['kind'] == 'speaking' and prompt['status'] == 'answered':
                    # Confirmation/control captures belong to the prompt inventory,
                    # but only the final original answer can enter its review list.
                    from .pet_speaking_origin import original_answer_voice
                    if attempt is None or media['status'] in {'invalidated', 'cancelled'}:
                        continue
                    try:
                        original_answer_voice(c, prompt, attempt, row, media['audio']['sha256'])
                    except (ValueError, TypeError, KeyError):
                        continue
                result[media["kind"]].append(media)
            result["selection_required"] = any(len(result[kind]) > 1 for kind in ("listening", "speaking"))
            result['status'] = 'bound_media' if result['listening'] or result['speaking'] else 'missing_media'
            return result

    def read_listening_audio(self, session_id, *, media_id, audio_sha256):
        """Deliver reviewed original listening audio only, never child voice bytes."""
        return self._listening_audio(session_id, media_id, audio_sha256, parent_review=False)

    def review_listening_audio(self, session_id, *, media_id, audio_sha256):
        """Internal parent-authenticated preview needed before source approval."""
        return self._listening_audio(session_id, media_id, audio_sha256, parent_review=True)

    def _listening_audio(self, session_id, media_id, audio_sha256, *, parent_review):
        with self.database.connect() as c:
            c.execute("BEGIN")
            row = _owned(c, session_id, media_id)
            if row["kind"] != "listening":
                raise PetMediaEvidenceError("child_voice_delivery_not_authorized")
            if not parent_review:
                _session(c, session_id, active=True)
            current = self._view(c, session_id, media_id)
            if current["status"] in {"cancelled", "invalidated"}:
                raise PetMediaEvidenceError("original_audio_not_current")
            if not parent_review:
                from .exam_listening_delivery import playback_permission
                try:
                    playback_permission(c,row,starting=True)
                except ValueError as exc:
                    raise PetMediaEvidenceError(str(exc)) from exc
            if not parent_review and not current["source_reviewed"]:
                raise PetMediaEvidenceError("original_audio_not_reviewed")
            expected = json.loads(row["audio_json"])["sha256"]
            if not expected or audio_sha256 != expected:
                raise PetMediaEvidenceError("playback_audio_digest_conflict")
            source = _source(c, row)
            try:
                content = _read_audio(source["local_path"], media_root=self.media_root,
                                      data_encryption_key=self.data_encryption_key)
            except (OSError, ValueError) as exc:
                raise PetMediaEvidenceError("original_audio_unavailable") from exc
            if hashlib.sha256(content).hexdigest() != expected:
                raise PetMediaEvidenceError("audio_changed_during_delivery")
            return content

    @staticmethod
    def _retry(c, key, request):
        _text(key, "client_action_id")
        if key.startswith("pet-media-settle:"):
            raise PetMediaEvidenceError("reserved_action_namespace")
        event = c.execute("SELECT * FROM pet_media_event WHERE client_action_id=?", (key,)).fetchone()
        if event and event["request_hash"] != _hash(request):
            raise PetMediaEvidenceError("action_conflict")
        return event

    @staticmethod
    def _event(c, row, key, operation, request, payload, *, received_ms=None):
        if operation in {"playback", "playback_settled"}:
            state = json.loads(row["state_json"])
            payload = {**payload, "result_state": {key: state[key] for key in
                ("status", "generation", "revision", "frame_position")}}
        if operation == "playback":
            payload = dict(payload, received_monotonic_ms=_clock_ms() if received_ms is None else received_ms,
                           requirement_version=(json.loads(row["state_json"]).get("delivery_requirement") or {}).get("version"),
                           receipt_state="pending" if json.loads(row["state_json"])["status"] == "pending_receipt" else "accepted")
        c.execute("INSERT INTO pet_media_event VALUES (?,?,?,?,?,?,?,?)", (
            uuid.uuid4().hex, row["media_id"], key, operation, _hash(request),
            json.loads(row["state_json"])["generation"], _json(payload), _now().isoformat()))

    def begin_listening(self, session_id, *, prompt_id, item_version, artifact_id, client_action_id):
        return self._begin(session_id, prompt_id, item_version, "listening", artifact_id, client_action_id)

    def bind_speaking(self, session_id, *, prompt_id, item_version, voice_input_id, client_action_id):
        return self._begin(session_id, prompt_id, item_version, "speaking", voice_input_id, client_action_id)

    def _begin(self, session_id, prompt_id, item_version, kind, source_id, key):
        request = dict(operation="begin", session_id=session_id, prompt_id=prompt_id,
                       item_version=item_version, kind=kind, source_id=source_id)
        with self.database.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            retry = self._retry(c, key, request)
            if retry:
                return self._view(c, session_id, retry["media_id"])
            session = _session(c, session_id, active=True)
            prompt, item = _prompt(c, session_id, prompt_id, item_version)
            if prompt["status"] != "waiting_answer":
                raise PetMediaEvidenceError("prompt_not_waiting")
            if kind == "listening" and (item["answer_type"] != "listening_text" or not item["stimulus_text"]):
                raise PetMediaEvidenceError("original_listening_stimulus_missing")
            if kind == "speaking" and pet_skill_gate(c, session["child_id"], item["diagnostic_item_id"])["modality"] != "speaking":
                raise PetMediaEvidenceError("not_speaking_item")
            row = dict(media_id=uuid.uuid4().hex, child_id=session["child_id"], session_id=session_id,
                prompt_id=prompt_id, diagnostic_item_id=item["diagnostic_item_id"], item_version=item_version,
                kind=kind, artifact_id=source_id if kind == "listening" else None,
                voice_input_id=source_id if kind == "speaking" else None)
            source = _source(c, row)
            if kind == "speaking" and not _date(prompt["created_at"]) <= _date(source["created_at"]) <= _now():
                raise PetMediaEvidenceError("voice_outside_original_prompt")
            field = "artifact_id" if kind == "listening" else "voice_input_id"
            existing = c.execute(f"SELECT state_json FROM pet_media_binding WHERE prompt_id=? AND kind=? AND {field}=?",
                                 (prompt_id, kind, source_id)).fetchall()
            if any(json.loads(previous["state_json"])["status"] != "cancelled" for previous in existing):
                raise PetMediaEvidenceError("binding_exists_use_original_action")
            audio = inspect_local_pcm(source["local_path"], media_root=self.media_root,
                                      data_encryption_key=self.data_encryption_key)
            state = dict(status="paused" if kind == "listening" else "bound", generation=1, revision=1,
                         runtime_epoch=self.runtime_epoch, frame_position=0, completed_at=None, started=None)
            if kind == "listening":
                state["delivery_requirement"] = listening_requirement(c, session["child_id"], item["diagnostic_item_id"])
            row.update(source_version=_hash(source), audio_json=_json(audio), state_json=_json(state),
                       assessment_id=None, created_at=_now().isoformat())
            columns = ",".join(row)
            c.execute(f"INSERT INTO pet_media_binding ({columns}) VALUES ({','.join('?' for _ in row)})", tuple(row.values()))
            self._event(c, row, key, "begin", request, {})
            result = self._view(c, session_id, row["media_id"])
            c.commit()
            return result

    def _write(self, session_id, media_id, key, operation, payload, apply, *, generation=None, revision=None, recovery=False, received_ms=None):
        request = dict(operation=operation, session_id=session_id, media_id=media_id, payload=payload)
        with self.database.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = _owned(c, session_id, media_id)
            state = json.loads(row["state_json"])
            retry = self._retry(c, key, request)
            exam_lease = None
            if operation in {'control','playback'}:
                from .exam_listening_delivery import playback_permission, pending_receipt_permission
                try:
                    recovering = operation=='playback' and retry and pending_receipt_permission(c,row,state,retry)
                    exam_lease = None if recovering else playback_permission(c,row,
                        starting=operation=='control' and payload.get('command') in {'resume','restart'},state=state)
                except ValueError as exc:
                    raise PetMediaEvidenceError(str(exc)) from exc
            if retry:
                if generation is not None and retry["generation"] != state["generation"]:
                    raise PetMediaEvidenceError("stale_generation")
                if operation == "playback" and state.get("pending_end") and state["pending_end"]["request"]["client_action_id"] == key:
                    return self._settle_pending(c, session_id, row, state, retry)
                return self._view(c, session_id, media_id)
            if operation != "review_source" or payload.get("operation") != "retract":
                _session(c, session_id, active=True)
            if generation is not None:
                _integer(generation, "generation", 1)
                _integer(revision, "expected_revision", 1)
                if (generation, revision) != (state["generation"], state["revision"]):
                    raise PetMediaEvidenceError("stale_generation_or_revision")
                if state["runtime_epoch"] != self.runtime_epoch and not recovery:
                    raise PetMediaEvidenceError("restart_requires_resume")
            current = _current(c, row, self.media_root, self.data_encryption_key)
            if current[-1] and not recovery:
                raise PetMediaEvidenceError(current[-1][0])
            apply(c, row, state, current)
            if exam_lease is not None:
                state.pop('exam_pending_recovery',None)
                state['exam_lease'] = exam_lease
            state["revision"] += 1
            row["state_json"] = _json(state)
            c.execute("UPDATE pet_media_binding SET state_json=?,assessment_id=? WHERE media_id=?",
                      (row["state_json"], row["assessment_id"], media_id))
            self._event(c, row, key, operation, request, payload, received_ms=received_ms)
            if operation == 'review_source':
                from .exam_listening_delivery import refresh_reviewed_media_owner
                refresh_reviewed_media_owner(c,row)
            result = self._view(c, session_id, media_id)
            c.commit()
            return result

    def _settle_pending(self, c, session_id, row, state, event):
        _session(c, session_id, active=True)
        current = _current(c, row, self.media_root, self.data_encryption_key)
        if current[-1]:
            raise PetMediaEvidenceError(current[-1][0])
        if _pending_delay(state, self.runtime_epoch, _now()):
            return self._view(c, session_id, row["media_id"])
        state.update(status="playback_finished", completed_at=state.get("completed_at") or _now().isoformat(),
                     started=None, pending_end=None, revision=state["revision"] + 1, runtime_epoch=self.runtime_epoch)
        row["state_json"] = _json(state)
        c.execute("UPDATE pet_media_binding SET state_json=? WHERE media_id=?", (row["state_json"], row["media_id"]))
        self._event(c, row, "pet-media-settle:" + event["event_id"], "playback_settled",
                    {"pending_event_id": event["event_id"]},
                    {**json.loads(event["payload_json"]), "receipt_state": "accepted", "pending_event_id": event["event_id"]})
        result = self._view(c, session_id, row["media_id"])
        c.commit()
        return result

    def control_listening(self, session_id, *, media_id, generation, expected_revision, command, client_action_id):
        if command not in {"pause", "resume", "restart", "cancel"}:
            raise PetMediaEvidenceError("invalid_command")

        def apply(c, row, state, current):
            if client_action_id.endswith(":pet:control"):
                from .agent_runtime import AgentRuntimeStore, InvalidAgentPlanError
                try:
                    bound = AgentRuntimeStore.inspect_pet_listening_control(c, client_action_id[:-len(":pet:control")])
                except InvalidAgentPlanError as exc:
                    raise PetMediaEvidenceError(str(exc)) from exc
                expected = dict(session_id=session_id, media_id=media_id, generation=generation,
                    expected_revision=expected_revision, command=command, client_action_id=client_action_id)
                if bound != expected:
                    raise PetMediaEvidenceError("agent_control_binding_conflict")
            if row["kind"] != "listening" or state["status"] == "cancelled":
                raise PetMediaEvidenceError("playback_not_controllable")
            if command in {"resume", "restart"} and current[-1]:
                raise PetMediaEvidenceError(current[-1][0])
            state.update(generation=state["generation"] + 1, runtime_epoch=self.runtime_epoch,
                         status={"pause": "paused", "resume": "playing", "restart": "playing", "cancel": "cancelled"}[command],
                         started=None, pending_end=None, frame_position=0)

        return self._write(session_id, media_id, client_action_id, "control",
            dict(generation=generation, expected_revision=expected_revision, command=command), apply,
            generation=generation, revision=expected_revision, recovery=True)

    def control_from_agent(self, session_id, *, agent_turn_id):
        """Runtime admission and media CAS share this one write transaction."""
        from copy import copy
        from .agent_runtime import AgentRuntimeStore, InvalidAgentPlanError
        from .diagnostic_prompt import _TransactionDatabase
        _text(agent_turn_id, "agent_turn_id")
        with self.database.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            session = _session(c, session_id, active=True)
            owner = c.execute("SELECT 1 FROM agent_turn WHERE agent_turn_id=? AND child_id=? AND session_id=?",
                              (agent_turn_id, session["child_id"], session_id)).fetchone()
            if not owner:
                raise PetMediaEvidenceError("agent_turn_ownership_conflict")
            key = agent_turn_id + ":pet:control"
            prior = c.execute("SELECT media_id FROM pet_media_event WHERE client_action_id=? AND operation='control'", (key,)).fetchone()
            if prior:
                return {"media": self._view(c, session_id, prior["media_id"]), "control_action_id": key,
                        "replayed": True, "replay_audio_allowed": False, "local_only": True, "cloud_transmission_allowed": False}
            try:
                admit = getattr(AgentRuntimeStore, "admit_pet_listening_control", None)
                if admit is not None:
                    admit(c, agent_turn_id)
                bound = AgentRuntimeStore.inspect_pet_listening_control(c, agent_turn_id)
            except InvalidAgentPlanError as exc:
                raise PetMediaEvidenceError(str(exc)) from exc
            if bound["session_id"] != session_id:
                raise PetMediaEvidenceError("agent_turn_ownership_conflict")
            scoped = copy(self)
            scoped.database = _TransactionDatabase(c)
            result = scoped.control_listening(**bound)
            c.commit()
            return {"media": result, "control_action_id": key, "replayed": False,
                    "replay_audio_allowed": False, "local_only": True, "cloud_transmission_allowed": False}

    def record_playback(self, session_id, *, media_id, generation, expected_revision,
                        event, audio_sha256, frame_position, monotonic_ms, client_action_id):
        """Actual render callbacks; pending_receipt requires exact retry, not audio.

        Only an ended already persisted in this generation may settle after a
        restart. A previously unseen old-epoch callback remains rejected.
        """
        received_ms, received_at = _clock_ms(), _now()
        if event not in {"started", "ended"}:
            raise PetMediaEvidenceError("invalid_playback_event")
        _integer(frame_position, "frame_position")
        _integer(monotonic_ms, "monotonic_ms")

        def apply(c, row, state, current):
            audio = current[3]
            if audio_sha256 != audio["sha256"]:
                raise PetMediaEvidenceError("playback_audio_digest_conflict")
            if row["kind"] != "listening" or state["status"] != "playing":
                raise PetMediaEvidenceError("not_playing")
            if event == "started":
                if frame_position != 0 or state["started"] is not None:
                    raise PetMediaEvidenceError("invalid_start")
                state["started"] = dict(monotonic_ms=monotonic_ms, server_ms=received_ms)
            else:
                started = state["started"]
                if not started or frame_position != audio["frames"]:
                    raise PetMediaEvidenceError("incomplete_playback")
                duration_ms = audio["frames"] * 1000 / audio["sample_rate"]
                client_elapsed = monotonic_ms - started["monotonic_ms"]
                server_elapsed = received_ms - started["server_ms"]
                if not (duration_ms <= client_elapsed <= duration_ms + _RECEIPT_GRACE_MS
                        and 0 <= server_elapsed <= duration_ms + _RECEIPT_GRACE_MS):
                    raise PetMediaEvidenceError("playback_clock_conflict")
                if server_elapsed < duration_ms:
                    state.update(status="pending_receipt", frame_position=frame_position, pending_end={
                        "settle_server_ms": started["server_ms"] + duration_ms,
                        "settle_at": (received_at + timedelta(milliseconds=duration_ms - server_elapsed)).isoformat(),
                        "request": dict(media_id=row["media_id"], generation=generation, expected_revision=expected_revision,
                            event=event, audio_sha256=audio_sha256, frame_position=frame_position,
                            monotonic_ms=monotonic_ms, client_action_id=client_action_id)})
                    return
                state.update(status="playback_finished", frame_position=frame_position, started=None,
                             completed_at=state.get("completed_at") or _now().isoformat())

        return self._write(session_id, media_id, client_action_id, "playback",
            dict(generation=generation, expected_revision=expected_revision, event=event,
                 audio_sha256=audio_sha256, frame_position=frame_position, monotonic_ms=monotonic_ms), apply,
            generation=generation, revision=expected_revision, received_ms=received_ms)

    def review_listening_source(self, session_id, *, media_id, audio_sha256, operation,
                                reviewer_id, reviewer_role, client_action_id, full_spoken_reference=False):
        """Parent-authenticated local attestation; HTTP must not trust role payloads."""
        if reviewer_role != "parent":
            raise PetMediaEvidenceError("parent_review_required")
        _text(reviewer_id, "reviewer_id")
        if operation not in {"approve", "retract"}:
            raise PetMediaEvidenceError("invalid_review_operation")

        if type(full_spoken_reference) is not bool or (full_spoken_reference and operation != "approve"):
            raise PetMediaEvidenceError("invalid_full_spoken_reference_attestation")
        payload = dict(audio_sha256=audio_sha256, operation=operation, reviewer_id=reviewer_id, reviewer_role=reviewer_role)

        def apply(c, row, state, current):
            if row["kind"] != "listening" or audio_sha256 != json.loads(row["audio_json"])["sha256"] or not audio_sha256:
                raise PetMediaEvidenceError("original_audio_review_conflict")
            if operation == "approve" and current[-1]:
                raise PetMediaEvidenceError(current[-1][0])
            if full_spoken_reference:
                from .pet_playback_reference import full_reference_proof
                if payload["full_spoken_reference"] != full_reference_proof(row, current[1], current[3]):
                    raise PetMediaEvidenceError("original_audio_review_conflict")

        # Derive the proof before _write computes its canonical request hash.
        if full_spoken_reference:
            with self.database.connect() as c:
                row = _owned(c, session_id, media_id)
                current = _current(c, row, self.media_root, self.data_encryption_key)
                if current[-1]:
                    raise PetMediaEvidenceError(current[-1][0])
                from .pet_playback_reference import full_reference_proof
                payload["full_spoken_reference"] = full_reference_proof(row, current[1], current[3])
        return self._write(session_id, media_id, client_action_id, "review_source", payload,
            apply, recovery=operation == "retract")

    def playback_control_reference(self, session_id, *, media_id, audio_sha256):
        from .pet_playback_reference import resolve
        return resolve(self, session_id, media_id=media_id, audio_sha256=audio_sha256)[0]

    def resolve_playback_control_reference(self, session_id, child_id, reference):
        from .pet_playback_reference import resolve
        return resolve(self, session_id, media_id=reference["media_id"],
            audio_sha256=reference["audio_sha256"], expected=reference, child_id=child_id)[1]

    def attach_assessment(self, session_id, *, media_id, assessment_id, client_action_id):
        _text(assessment_id, "assessment_id")

        def apply(c, row, state, current):
            row["assessment_id"] = assessment_id
            receipt, reason = _assessment(c, row, *current[:3], _now())
            if reason:
                raise PetMediaEvidenceError(reason)

        return self._write(session_id, media_id, client_action_id, "assessment",
                           dict(assessment_id=assessment_id), apply)
