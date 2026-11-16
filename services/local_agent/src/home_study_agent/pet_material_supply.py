"""Durable source-bound local practice artifacts in the existing media store."""

from datetime import timedelta
import hashlib
import json
import os
import uuid

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .pet_local_audio import render_original_practice
from .pet_media_files import write_new_managed_media
from .pet_listening_requirement import listening_requirement
from .local_supply import LocalSupplyError, validate_item


def material_projection(row):
    details = json.loads(row["details_json"])
    return {"artifact_id": row["artifact_id"], "status": row["status"],
            **{key: details.get(key) for key in ("prompt_id", "item_version", "diagnostic_item_id",
                "origin", "original_recording_verified", "requirement_version", "engine")},
            "parent_review_required": True, "local_only": True, "cloud_transmission_allowed": False}


def materialize(service, session_id, *, prompt_id, item_version, client_action_id):
    from .pet_media_evidence import (PetMediaEvidenceError, _session, _prompt, _hash, _json,
                                     _now, _date, _text, inspect_local_pcm)
    _text(client_action_id, "client_action_id")
    if not service.data_encryption_key or len(service.data_encryption_key) not in {16, 24, 32}:
        raise PetMediaEvidenceError("data_encryption_key_not_ready")
    request_hash = _hash(dict(session_id=session_id, prompt_id=prompt_id, item_version=item_version))
    key = "pet-material:" + client_action_id
    with service.database.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        session = _session(c, session_id, active=True)
        prompt, item = _prompt(c, session_id, prompt_id, item_version)
        try:
            validate_item(c, session["child_id"], item["diagnostic_item_id"])
        except LocalSupplyError as exc:
            raise PetMediaEvidenceError("verified_original_stimulus_unavailable") from exc
        if prompt["status"] != "waiting_answer":
            raise PetMediaEvidenceError("prompt_not_waiting")
        # A published original must retain the complete independent-review
        # lineage, not merely a parent-approved label or caller-provided text.
        reviewed = None
        if item["source_ref"].startswith("parent_approved_specialized:"):
            from .pet_reviewed_source import published_listening_source
            try:
                reviewed = published_listening_source(c, session["child_id"], item["diagnostic_item_id"])
            except LocalSupplyError as exc:
                raise PetMediaEvidenceError("verified_original_stimulus_unavailable") from exc
        if ((not item["source_ref"].startswith("hsc_original") and reviewed is None)
                or item["answer_type"] != "listening_text" or not item.get("stimulus_text")):
            raise PetMediaEvidenceError("verified_original_stimulus_unavailable")
        requirement = listening_requirement(c, session["child_id"], item["diagnostic_item_id"])
        row = c.execute("SELECT * FROM capture_artifact WHERE client_action_id=?", (key,)).fetchone()
        if row:
            details = json.loads(row["details_json"])
            if (details.get("request_hash") != request_hash or row["child_id"] != session["child_id"]
                    or row["session_id"] != session_id):
                raise PetMediaEvidenceError("material_action_conflict")
            if details.get("requirement_version") != requirement["version"]:
                raise PetMediaEvidenceError("material_requirement_changed")
            if row["status"] == "completed":
                audio = inspect_local_pcm(row["local_path"], media_root=service.media_root,
                                           data_encryption_key=service.data_encryption_key)
                if audio["status"] != "usable_signal" or audio["sha256"] != details.get("audio_sha256"):
                    raise PetMediaEvidenceError("material_audio_changed")
                return material_projection(row)
            if row["status"] not in {"processing", "failed"}:
                raise PetMediaEvidenceError("material_source_withdrawn")
            if (row["status"] == "processing" and details.get("runtime_epoch") == service.runtime_epoch
                    and _now() - _date(details["started_at"]) < timedelta(seconds=60)):
                raise PetMediaEvidenceError("material_generation_in_progress")
            artifact_id, path = row["artifact_id"], row["local_path"]
        else:
            artifact_id = uuid.uuid4().hex
            path = str(service.media_root / (artifact_id + ".wav.enc"))
            details = dict(prompt_id=prompt_id, item_version=item_version, diagnostic_item_id=item["diagnostic_item_id"],
                request_hash=request_hash, origin="local_synthesized_practice", original_recording_verified=False,
                requirement_version=requirement["version"], stimulus_sha256=hashlib.sha256(item["stimulus_text"].encode()).hexdigest())
            c.execute("""INSERT INTO capture_artifact
                (artifact_id,child_id,session_id,artifact_type,local_path,details_json,status,created_at,client_action_id)
                VALUES(?,?,?,'audio',?,?,'processing',?,?)""",
                (artifact_id, session["child_id"], session_id, path, _json(details), _now().isoformat(), key))
        details.update(runtime_epoch=service.runtime_epoch, started_at=_now().isoformat(), lease=uuid.uuid4().hex)
        c.execute("UPDATE capture_artifact SET status='processing',details_json=? WHERE artifact_id=?", (_json(details), artifact_id))
        c.commit()
    try:
        # Rendering is outside the write transaction; completion rechecks the
        # original source and reservation before creating any durable file.
        content, engine = render_original_practice(item["stimulus_text"])
        nonce = os.urandom(12)
        encrypted = b"HSC1" + nonce + AESGCM(service.data_encryption_key).encrypt(nonce, content, b"hsc-raw-media-v1")
        with service.database.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            _session(c, session_id, active=True)
            current_prompt, _ = _prompt(c, session_id, prompt_id, item_version)
            try:
                validate_item(c, session["child_id"], item["diagnostic_item_id"])
            except LocalSupplyError as exc:
                raise PetMediaEvidenceError("verified_original_stimulus_unavailable") from exc
            current = c.execute("SELECT * FROM capture_artifact WHERE artifact_id=?", (artifact_id,)).fetchone()
            if (not current or current["status"] != "processing" or json.loads(current["details_json"]) != details
                    or current["local_path"] != path or current["child_id"] != session["child_id"]
                    or current["session_id"] != session_id or current_prompt["status"] != "waiting_answer"
                    or listening_requirement(c, session["child_id"], item["diagnostic_item_id"]) != requirement):
                raise PetMediaEvidenceError("material_reservation_or_source_changed")
            try:
                write_new_managed_media(encrypted, media_root=service.media_root, filename=artifact_id + ".wav.enc")
            except FileExistsError:
                # Recover a crash after durable file fsync but before DB commit.
                # Never overwrite: the reserved owned file is verified below.
                pass
            audio = inspect_local_pcm(path, media_root=service.media_root, data_encryption_key=service.data_encryption_key)
            if audio["status"] != "usable_signal" or audio["sha256"] != hashlib.sha256(content).hexdigest():
                raise PetMediaEvidenceError("material_signal_unusable")
            details.update(engine=engine, audio_sha256=audio["sha256"])
            c.execute("UPDATE capture_artifact SET status='completed',details_json=? WHERE artifact_id=?", (_json(details), artifact_id))
            result = material_projection(c.execute("SELECT * FROM capture_artifact WHERE artifact_id=?", (artifact_id,)).fetchone())
            c.commit()
            return result
    except (ValueError, OSError) as exc:
        with service.database.connect() as c:
            # Do not resurrect deletion, cancellation or a newer lease.
            c.execute("UPDATE capture_artifact SET status='failed' WHERE artifact_id=? AND status='processing' AND details_json=?",
                      (artifact_id, _json(details)))
            c.commit()
        if isinstance(exc, PetMediaEvidenceError):
            raise
        raise PetMediaEvidenceError("local_material_generation_unavailable") from exc
