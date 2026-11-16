"""Ephemeral local playback controls. Never a voice input or learning record."""
import asyncio
import base64
import binascii
from io import BytesIO
import re
import json
import time
import threading
import wave
from .pet_playback_reference import PcmPlaybackReference
from uuid import UUID
from typing import Optional
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from .attention_wake_match import valid_attention_word, playback_contains_attention, registered_prefix_remainder, privacy_control_mixture

class PlaybackControlRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    child_id: str = Field(min_length=1, max_length=128)
    playback_lease_id: UUID
    playback_generation: int = Field(ge=1, le=2147483647, strict=True)
    capture_epoch: int = Field(ge=1, le=2147483647, strict=True)
    source_kind: str = Field(max_length=24)
    current_playback_text: str = Field(default="", max_length=16000)
    pcm_reference: Optional[PcmPlaybackReference] = None
    attention_wake_word: Optional[str] = Field(default=None, min_length=2, max_length=12, strict=True)
    wav_base64: str = Field(max_length=106728)

def normalized(text):
    return re.sub(r'[\s，。！？,.!?、]', '', text)

CONTROLS = frozenset(('停', '停止', '暂停', '停止播放', '暂停播放', '别说了', '不要说了', '停一下', '等一下', '先暂停'))

class ExpiredControl(Exception):
    pass

class LocalPlaybackControl:
    def __init__(self, media_ai, timeout_seconds=4, clock=None, pcm_reference_resolver=None):
        self.media_ai = media_ai
        self.pcm_reference_resolver = pcm_reference_resolver
        self.timeout_seconds = min(4, max(.01, timeout_seconds))
        self.worker = None
        self.clock = clock or time.monotonic

    def current_session(self, session_id, child_id):
        session = self.media_ai._session(session_id)  # read only; active required
        if session['child_id'] != child_id or session['status'] != 'active':
            raise ValueError('LOCAL_CONTROL_SCOPE_CHANGED')

    async def recognize(self, session_id, p):
        binding = dict(playback_lease_id=str(p.playback_lease_id),
                       playback_generation=p.playback_generation, capture_epoch=p.capture_epoch)
        def result(status, control=None):
            return {'status': status, 'control': control, **binding,
                    'cloud_used': False, 'persisted': False, 'learning_evidence': False}
        deadline = self.clock() + self.timeout_seconds
        abandoned = threading.Event()
        try:
            self.current_session(session_id, p.child_id)
            if p.attention_wake_word is not None and (
                    p.source_kind != 'local_sapi' or not valid_attention_word(p.attention_wake_word)):
                return result('reference_unavailable')
            if p.source_kind == 'local_sapi' and p.current_playback_text.strip() and p.pcm_reference is None:
                playback_text = p.current_playback_text
            elif p.source_kind == 'local_pcm' and p.pcm_reference is not None and not p.current_playback_text and self.pcm_reference_resolver:
                playback_text = self.pcm_reference_resolver(session_id, p.child_id, p.pcm_reference.model_dump())
            else:
                return result('reference_unavailable')
            if self.worker is not None and not self.worker.done():
                return result('busy')
            wav_bytes = base64.b64decode(p.wav_base64, validate=True)
            if not 44 < len(wav_bytes) <= 80044:
                return result('invalid_audio')
            with wave.open(BytesIO(wav_bytes), 'rb') as wav:
                if wav.getcomptype() != 'NONE' or wav.getnchannels() != 1 or wav.getsampwidth() != 2 or wav.getframerate() != 16000 or not 1920 <= wav.getnframes() <= 40000:
                    return result('invalid_audio')
                if len(wav.readframes(wav.getnframes())) != wav.getnframes() * 2:
                    return result('invalid_audio')
            # Shield keeps the single-worker admission occupied if CPU decoding
            # outlives an HTTP cancellation/timeout. Late work cannot publish.
            def decode():
                if abandoned.is_set() or self.clock() >= deadline:
                    raise ExpiredControl()
                return self.media_ai._recognize_wav(wav_bytes, prefer_cloud=False,
                    session=None, purpose='agent_command')
            worker = asyncio.create_task(asyncio.to_thread(decode))
            self.worker = worker
            def release(done):
                if self.worker is done:
                    self.worker = None
                if not done.cancelled():
                    done.exception()
            worker.add_done_callback(release)
            try:
                transcript, _, _, cloud_used = await asyncio.wait_for(
                    asyncio.shield(worker), self.timeout_seconds)
            except asyncio.TimeoutError:
                abandoned.set()
                return result('timed_out')
            self.current_session(session_id, p.child_id)
            if abandoned.is_set() or self.clock() >= deadline:
                return result('timed_out')
            if p.source_kind == 'local_pcm':
                if self.pcm_reference_resolver(session_id, p.child_id, p.pcm_reference.model_dump()) != playback_text:
                    return result('reference_unavailable')
            if cloud_used is not False:
                return result('local_recognition_rejected')
            command = normalized(transcript)
            if command in CONTROLS:
                if command in normalized(playback_text):
                    return result('echo_rejected')
                return result('recognized', 'stop_playback')
            if p.attention_wake_word is not None and type(transcript) is str and 0 < len(transcript) <= 512:
                if privacy_control_mixture(transcript):
                    return result('not_control')
                remainder = registered_prefix_remainder(transcript, p.attention_wake_word)
                if remainder in CONTROLS:
                    if remainder in normalized(playback_text):
                        return result('echo_rejected')
                    return result('recognized', 'stop_playback')
                if not playback_contains_attention(transcript, p.attention_wake_word):
                    return result('not_control')
                if playback_contains_attention(playback_text, p.attention_wake_word):
                    return result('echo_rejected')
                return result('recognized', 'wake_companion')
            return result('not_control')
        except asyncio.CancelledError:
            abandoned.set()
            raise
        except ExpiredControl:
            return result('timed_out')
        except (ValueError, binascii.Error, wave.Error, EOFError):
            return result('rejected')
        except Exception:
            # Model/driver errors never forward provider bodies or use cloud.
            return result('local_recognition_unavailable')

def create_router(media_ai, require_local_session, pet_media=None):
    router = APIRouter()
    service = LocalPlaybackControl(media_ai, pcm_reference_resolver=pet_media.resolve_playback_control_reference if pet_media else None)

    @router.post('/v1/sessions/{session_id}/voice/local-playback-control')
    async def control(session_id: str, request: Request):
        require_local_session(request)
        if request.client is None or request.client.host not in ('127.0.0.1', '::1'):
            raise HTTPException(403, detail={'code': 'LOCAL_CONTROL_LOOPBACK_REQUIRED'})
        async def body():
            raw = bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > 208000:
                    raise ValueError('CONTROL_BODY_BOUND')
            return bytes(raw)
        try:
            raw = await asyncio.wait_for(body(), 2)
            payload = PlaybackControlRequest.model_validate(json.loads(raw))
        except (ValidationError, ValueError, asyncio.TimeoutError):
            # Framework validation normally echoes invalid field inputs. This
            # route returns only a fixed code, never text/audio/body fragments.
            raise HTTPException(400, detail={'code': 'LOCAL_CONTROL_REQUEST_INVALID'}) from None
        result = await service.recognize(session_id, payload)
        if await request.is_disconnected():
            result = {**result, 'status': 'cancelled', 'control': None}
        return result
    return router
