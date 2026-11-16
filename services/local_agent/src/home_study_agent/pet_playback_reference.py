"""Ephemeral stop-control reference for one current, reviewed whole audio file.

This capsule is never a transcript, playback receipt, answer or scoring grant.
The full original text stays inside the local service and is never truncated.
"""
import hashlib
import json
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

class PcmPlaybackReference(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: Literal['pet_full_audio_control_reference_v1']
    child_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    media_id: str = Field(min_length=1, max_length=128)
    prompt_id: str = Field(min_length=1, max_length=128)
    item_version: str = Field(min_length=1, max_length=200)
    diagnostic_item_id: str = Field(min_length=1, max_length=200)
    source_version: str = Field(min_length=1, max_length=200)
    audio_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    source_review_event_id: str = Field(min_length=1, max_length=128)
    spoken_text_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    generation: int = Field(ge=1, strict=True)
    local_only: Literal[True]
    cloud_transmission_allowed: Literal[False]

    @model_validator(mode='before')
    @classmethod
    def strict_privacy_flags(cls, value):
        if not isinstance(value, dict) or value.get('local_only') is not True or value.get('cloud_transmission_allowed') is not False:
            raise ValueError('PCM_REFERENCE_PRIVACY_FLAGS_INVALID')
        return value


VERSION = 'pet_full_audio_control_reference_v1'
PROOF_VERSION = 'full_original_stimulus_v1'


def _text(item):
    from .pet_media_evidence import PetMediaEvidenceError
    text = item.get('stimulus_text')
    if not isinstance(text, str) or not text.strip() or len(text) > 16000:
        raise PetMediaEvidenceError('full_audio_spoken_reference_unavailable')
    return text


def full_reference_proof(row, item, audio):
    text = _text(item)
    return dict(version=PROOF_VERSION, child_id=row['child_id'],
        diagnostic_item_id=row['diagnostic_item_id'], item_version=row['item_version'],
        artifact_id=row['artifact_id'], source_version=row['source_version'],
        audio_sha256=audio['sha256'], spoken_text_sha256=hashlib.sha256(text.encode()).hexdigest())


def verified_text(row, item, source, audio, review):
    from .pet_media_evidence import PetMediaEvidenceError
    if not review or json.loads(review['payload_json']).get('operation') != 'approve':
        raise PetMediaEvidenceError('full_audio_spoken_reference_unreviewed')
    text = _text(item)
    proof = full_reference_proof(row, item, audio)
    details = json.loads(source['details_json'])
    generated = (details.get('origin') == 'local_synthesized_practice'
        and details.get('stimulus_sha256') == proof['spoken_text_sha256']
        and details.get('audio_sha256') == audio['sha256'] and bool(details.get('engine')))
    approved_full = json.loads(review['payload_json']).get('full_spoken_reference') == proof
    if not generated and not approved_full:
        raise PetMediaEvidenceError('full_audio_spoken_reference_unavailable')
    return text


def resolve(service, session_id, *, media_id, audio_sha256, expected=None, child_id=None):
    from .pet_media_evidence import (PetMediaEvidenceError, _owned, _session, _current, _date, _now)
    from .pet_listening_reuse import listening_source_review
    from .exam_listening_delivery import playback_permission
    with service.database.connect() as c:
        _session(c, session_id, active=True)
        row = _owned(c, session_id, media_id)
        prompt, item, source, audio, reasons = _current(c, row, service.media_root, service.data_encryption_key)
        state = json.loads(row['state_json'])
        if (reasons or row['kind'] != 'listening'
                or state['status'] != 'playing' or state['runtime_epoch'] != service.runtime_epoch
                or audio['sha256'] != audio_sha256 or (child_id is not None and row['child_id'] != child_id)):
            raise PetMediaEvidenceError('playback_control_reference_owner_changed')
        try:
            exam_lease = playback_permission(c, row, starting=False, state=state)
            if exam_lease is None and prompt['status'] != 'waiting_answer':
                raise PetMediaEvidenceError('playback_control_reference_owner_changed')
        except ValueError as exc:
            raise PetMediaEvidenceError('playback_control_reference_owner_changed') from exc
        review = listening_source_review(c, row)
        if (not review or _date(review['created_at']) > _now()
                or json.loads(review['payload_json']).get('operation') != 'approve'):
            raise PetMediaEvidenceError('full_audio_spoken_reference_unreviewed')
        text = verified_text(row, item, source, audio, review)
        proof = full_reference_proof(row, item, audio)
        capsule = dict(version=VERSION, child_id=row['child_id'], session_id=session_id,
            media_id=media_id, prompt_id=row['prompt_id'], item_version=row['item_version'],
            diagnostic_item_id=row['diagnostic_item_id'], source_version=row['source_version'],
            audio_sha256=audio_sha256, source_review_event_id=review['event_id'],
            spoken_text_sha256=proof['spoken_text_sha256'], generation=state['generation'],
            local_only=True, cloud_transmission_allowed=False)
        if expected is not None and expected != capsule:
            raise PetMediaEvidenceError('playback_control_reference_changed')
        return capsule, text
