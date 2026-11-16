"""Safe server-side boundary for optional natural TTS.

The only remote-compatible provider is a deployment-managed PCM relay. It is
where a legally cleared cloud TTS account/voice is configured. This process
forwards short response *text* only; it never receives microphone audio and
never exposes credentials to Electron. The protocol is intentionally narrow so
an unreviewed vendor SDK cannot become an implicit production dependency.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
from collections import OrderedDict
from dataclasses import dataclass
from hashlib import sha256
import io
import json
import threading
from time import monotonic
from typing import AsyncIterator
from urllib.parse import urlparse
from uuid import uuid4
import wave

import httpx
import websockets

from .child_safety import classify_child_safety
from .cloud_privacy import sensitive_categories
from .config import Settings
from .database import Database
from .money_r4.money_budget import BudgetHold
from .repository import UnknownSessionError, utc_now


MAX_TEXT_CHARS = 1000
MAX_FRAME_BYTES = 512_000
MAX_RESPONSE_BYTES = 8_000_000
MAX_RESPONSE_FRAMES = 600
MAX_RESPONSE_AUDIO_SECONDS = 300
PCM_FRAME_HEADER_BYTES = 8
MAX_COMPLETION_BYTES = 2048
CACHE_MAX_ITEMS = 20
CACHE_TTL_SECONDS = 600.0
CACHE_MAX_ENTRY_BYTES = 1_000_000
CACHE_MAX_TOTAL_BYTES = 8_000_000
QWEN_SHUTDOWN_TIMEOUT_SECONDS = 1.0
# Dynamic replies can mention a child, teacher, family member, or class even
# when the safety classifier does not mark them sensitive. Cache only this
# deployment-reviewed set of generic UI acknowledgements; never cache a
# generated/personalized reply merely because it is short.
SAFE_CACHE_TEXTS = frozenset({"我们继续下一步。", "我在，慢慢说。", "我没有听清，请再说一遍。", "好的，我已停下。"})
PRIMARY_ADULT_VOICE_IDS = frozenset({"longyingtao_v3", "longanwen_v3", "longxiaochun_v3", "longyingmu_v3", "longanrou_v3"})


def supports_cosyvoice_companion(model: str, voice: str, region: str) -> bool:
    if region not in {"cn-beijing", "ap-southeast-1"}:
        return False
    if model == "cosyvoice-v3-plus":
        return voice == "longanhuan"
    return (model == "cosyvoice-v3-flash" and voice in PRIMARY_ADULT_VOICE_IDS
            and (region == "cn-beijing" or voice not in {"longxiaochun_v3", "longanrou_v3"}))
QWEN_AUDIO_ADULT_VOICE_IDS = frozenset({"longanlingxin", "longanlufeng"})
QWEN_VOICE_CLONE_MODEL = "qwen3-tts-vc-2026-01-22"
QWEN_VOICE_SYNTHESIS_URL = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"


@contextlib.asynccontextmanager
async def _closing_async_iterator(source: AsyncIterator[bytes]):
    # Python 3.9 lacks contextlib.aclosing. Closing nested adapters must not
    # wait for async-generator garbage collection after a consumer cancels.
    try:
        yield source
    finally:
        await source.aclose()


async def _run_sync_started_bounded(function, timeout_seconds: float) -> None:
    """Start a blocking SDK shutdown call exactly once without stalling asyncio."""
    completed = threading.Event()
    errors: list[BaseException] = []

    def run() -> None:
        try:
            function()
        except BaseException as error:
            errors.append(error)
        finally:
            completed.set()

    worker = threading.Thread(target=run, name="qwen-tts-shutdown", daemon=True)
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    worker.start()
    while not completed.is_set():
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError("Qwen TTS SDK shutdown timed out")
        await asyncio.sleep(min(0.002, remaining))
    if errors:
        raise errors[0]


class VoiceSynthesisError(RuntimeError):
    code = "TTS_UNAVAILABLE"
    retryable = True


class VoiceSynthesisProviderRejectedError(VoiceSynthesisError):
    def __init__(self, header):
        super().__init__('语音服务拒绝了合成任务，请查看本机诊断分类。')
        # Never log the vendor's raw message: it may echo text or credentials.
        vendor_code = header.get('error_code')
        self.code = {
            'InvalidParameter': 'TTS_PROVIDER_INVALID_PARAMETER',
            'InvalidApiKey': 'TTS_PROVIDER_CREDENTIAL_INVALID',
            'AccessDenied': 'TTS_PROVIDER_ACCESS_DENIED',
            'Throttling': 'TTS_PROVIDER_RATE_LIMITED',
            'ModelNotFound': 'TTS_PROVIDER_MODEL_UNAVAILABLE',
        }.get(vendor_code if isinstance(vendor_code, str) else None, 'TTS_PROVIDER_REJECTED')
        self.retryable = self.code == 'TTS_PROVIDER_RATE_LIMITED'


class VoiceSynthesisNotConfiguredError(VoiceSynthesisError):
    code = "TTS_NOT_CONFIGURED"
    retryable = False


class VoiceSynthesisConsentRequiredError(VoiceSynthesisError):
    code = "TTS_CLOUD_TEXT_CONSENT_REQUIRED"
    retryable = False


class VoiceSynthesisSafetyError(VoiceSynthesisError):
    code = "TTS_TEXT_NOT_ELIGIBLE"
    retryable = False


class VoiceSynthesisProtocolError(VoiceSynthesisError):
    code = "TTS_PROVIDER_PROTOCOL_ERROR"


@dataclass(frozen=True)
class CachedPcm:
    frames: tuple[bytes, ...]
    created_at: float
    byte_count: int


class VoiceSynthesisService:
    def __init__(self, database: Database, settings: Settings, client: httpx.AsyncClient | None = None):
        self.database = database
        self.settings = settings
        self.client = client or httpx.AsyncClient(timeout=settings.tts_timeout_seconds)
        self._owns_client = client is None
        self._cache: OrderedDict[str, CachedPcm] = OrderedDict()
        self._cache_bytes = 0

    @property
    def configured(self) -> bool:
        if self.settings.tts_provider == "relay_pcm":
            return bool(self.settings.tts_relay_url and self.settings.tts_api_key and self.settings.tts_voice_id)
        if self.settings.tts_provider == "aliyun_cosyvoice":
            return bool(self.settings.tts_api_key and self.settings.tts_workspace_id
                        and supports_cosyvoice_companion(self.settings.tts_model, self.settings.tts_voice_id, self.settings.tts_region))
        if self.settings.tts_provider == "qwen_audio_tts":
            return bool(self.settings.tts_api_key and self.settings.tts_voice_id in QWEN_AUDIO_ADULT_VOICE_IDS and self.settings.tts_workspace_id and self.settings.tts_model)
        if self.settings.tts_provider == "qwen_voice_clone":
            return bool(self.settings.tts_api_key and self.settings.tts_voice_id and self.settings.tts_model == QWEN_VOICE_CLONE_MODEL)
        return False

    def public_status(self) -> dict:
        return {
            "configured": self.configured,
            "provider": self.settings.tts_provider if self.configured else "disabled",
            "transport": "pcm16_frame_v1" if self.configured else None,
            "system_voice_is_production": False,
        }

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    def assert_eligible(self, session_id: str, text: str, cloud_text_allowed: bool) -> None:
        """Validate before StreamingResponse sends a 200 header."""
        text = text.strip()
        if not self.configured:
            raise VoiceSynthesisNotConfiguredError("自然语音尚未由家长配置。")
        if not cloud_text_allowed:
            raise VoiceSynthesisConsentRequiredError("家长尚未允许将必要回复文本发送到语音服务。")
        if not text or len(text) > MAX_TEXT_CHARS:
            raise VoiceSynthesisSafetyError("播报文本长度无效。")
        if sensitive_categories(text) or classify_child_safety(text):
            raise VoiceSynthesisSafetyError("这段内容仅以文字显示，不发送到语音服务。")
        self._active_session_child(session_id)

    def assert_stream_protocol(self, protocol: str) -> None:
        if protocol not in {"pcm16_frame_v1", "pcm16_frame_v2"}:
            raise VoiceSynthesisProtocolError("不支持这版语音完整性合同。")
        if protocol == "pcm16_frame_v2" and self.settings.tts_provider == "relay_pcm":
            raise VoiceSynthesisProtocolError("旧语音中继没有可靠生成结束证据。")

    @staticmethod
    def _completion_frame(request_id: str, frame_count: int, pcm_bytes: int) -> bytes:
        payload = json.dumps({"type": "generation_completed", "request_id": request_id,
                              "frame_count": frame_count, "pcm_bytes": pcm_bytes,
                              "sample_count": pcm_bytes // 2}, separators=(",", ":")).encode("utf-8")
        if len(payload) > MAX_COMPLETION_BYTES:
            raise VoiceSynthesisProtocolError("语音结束合同超出大小限制。")
        return bytes(4) + len(payload).to_bytes(4, "little") + payload

    async def stream(self, session_id: str, text: str, client_action_id: str, cloud_text_allowed: bool,
                     *, protocol: str = "pcm16_frame_v1") -> AsyncIterator[bytes]:
        text = text.strip()
        self.assert_eligible(session_id, text, cloud_text_allowed)
        self.assert_stream_protocol(protocol)
        if protocol == "pcm16_frame_v2" and (not isinstance(client_action_id, str) or not 8 <= len(client_action_id) <= 128):
            raise VoiceSynthesisProtocolError("语音请求身份无效。")
        child_id = self._active_session_child(session_id)
        cacheable = text in SAFE_CACHE_TEXTS
        key = sha256(f"{self.settings.tts_provider}|{self.settings.tts_voice_id}|{text}".encode("utf-8")).hexdigest()
        started = monotonic()
        cached = self._cache_get(key) if cacheable else None
        frame_count = 0
        pcm_bytes = 0
        generation_completed = False
        first_frame_ms: int | None = None
        try:
            if cached:
                for frame in cached.frames:
                    _, payload_size = self._validate_frame(frame)
                    frame_count += 1
                    pcm_bytes += payload_size
                    if first_frame_ms is None:
                        first_frame_ms = 0
                    yield frame
                self._audit(child_id, session_id, client_action_id, "completed", len(text), first_frame_ms, round((monotonic() - started) * 1000), frame_count, True, None)
                generation_completed = True
                if protocol == "pcm16_frame_v2":
                    yield self._completion_frame(client_action_id, frame_count, pcm_bytes)
                return
            frames: list[bytes] | None = [] if cacheable else None
            response_bytes = 0
            audio_seconds = 0.0
            async with _closing_async_iterator(self._provider_frames(text)) as source:
                async for frame in source:
                    sample_rate, payload_size = self._validate_frame(frame)
                    response_bytes += len(frame)
                    frame_count += 1
                    pcm_bytes += payload_size
                    audio_seconds += payload_size / 2 / sample_rate
                    if response_bytes > MAX_RESPONSE_BYTES or frame_count > MAX_RESPONSE_FRAMES or audio_seconds > MAX_RESPONSE_AUDIO_SECONDS:
                        raise VoiceSynthesisProtocolError("语音响应超出安全时长或大小限制。")
                    if first_frame_ms is None:
                        first_frame_ms = round((monotonic() - started) * 1000)
                    if frames is not None:
                        frames.append(frame)
                    yield frame
            if frame_count == 0:
                raise VoiceSynthesisProtocolError("语音服务没有返回音频。")
            if frames is not None:
                self._cache_put(key, tuple(frames))
            self._audit(child_id, session_id, client_action_id, "completed", len(text), first_frame_ms, round((monotonic() - started) * 1000), frame_count, False, None)
            generation_completed = True
            if protocol == "pcm16_frame_v2":
                yield self._completion_frame(client_action_id, frame_count, pcm_bytes)
        except (asyncio.CancelledError, GeneratorExit):
            if not generation_completed:
                self._audit(child_id, session_id, client_action_id, "cancelled", len(text), first_frame_ms, round((monotonic() - started) * 1000), frame_count, False, "CLIENT_CANCELLED")
            raise
        except BudgetHold as error:
            self._audit(child_id, session_id, client_action_id, "failed", len(text), first_frame_ms, round((monotonic() - started) * 1000), frame_count, False, error.code)
            raise
        except VoiceSynthesisError as error:
            self._audit(child_id, session_id, client_action_id, "failed", len(text), first_frame_ms, round((monotonic() - started) * 1000), frame_count, False, error.code)
            raise
        except (httpx.HTTPError, ValueError) as error:
            self._audit(child_id, session_id, client_action_id, "failed", len(text), first_frame_ms, round((monotonic() - started) * 1000), frame_count, False, "TTS_PROVIDER_UNAVAILABLE")
            raise VoiceSynthesisError("语音服务暂时不可用。") from error

    async def _relay_frames(self, text: str) -> AsyncIterator[bytes]:
        from .money_egress import deny_unpriced
        deny_unpriced('voice_synthesis.py:_relay_frames')
        url = self.settings.tts_relay_url or ""
        parsed = urlparse(url)
        if parsed.scheme != "https":
            raise VoiceSynthesisNotConfiguredError("语音服务地址必须使用 HTTPS。")
        headers = {
            "Authorization": f"Bearer {self.settings.tts_api_key}",
            "Accept": "application/octet-stream",
            "X-HSC-TTS-Protocol": "pcm16-frame-v1",
        }
        payload = {"text": text, "voice_id": self.settings.tts_voice_id, "format": "pcm_s16le", "sample_rate": 24000}
        pending = b""
        total = 0
        async with self.client.stream("POST", url, headers=headers, json=payload) as response:
            if response.status_code >= 400:
                raise VoiceSynthesisError("语音服务拒绝了本次合成请求。")
            async for chunk in response.aiter_bytes():
                pending += chunk
                while len(pending) >= PCM_FRAME_HEADER_BYTES:
                    sample_rate = int.from_bytes(pending[:4], "little")
                    payload_size = int.from_bytes(pending[4:8], "little")
                    if sample_rate not in {8000, 16000, 22050, 24000, 32000, 44100, 48000} or payload_size <= 0 or payload_size > MAX_FRAME_BYTES or payload_size % 2:
                        raise VoiceSynthesisProtocolError("语音服务返回了无效的 PCM 帧。")
                    if len(pending) < PCM_FRAME_HEADER_BYTES + payload_size:
                        break
                    frame = pending[:PCM_FRAME_HEADER_BYTES + payload_size]
                    pending = pending[PCM_FRAME_HEADER_BYTES + payload_size:]
                    total += len(frame)
                    if total > MAX_RESPONSE_BYTES:
                        raise VoiceSynthesisProtocolError("语音响应超出安全大小限制。")
                    yield frame
        if pending:
            raise VoiceSynthesisProtocolError("语音服务返回了不完整的 PCM 帧。")

    async def _provider_frames(self, text: str) -> AsyncIterator[bytes]:
        adapter = {"relay_pcm": self._relay_frames, "aliyun_cosyvoice": self._aliyun_cosyvoice_frames,
                   "qwen_audio_tts": self._qwen_audio_frames, "qwen_voice_clone": self._qwen_voice_clone_frames}.get(self.settings.tts_provider)
        if adapter is None:
            raise VoiceSynthesisNotConfiguredError("自然语音尚未由家长配置。")
        async with _closing_async_iterator(adapter(text)) as source:
            async for frame in source:
                yield frame

    async def _qwen_voice_clone_frames(self, text: str) -> AsyncIterator[bytes]:
        """Synthesize with the one-time enrolled family reference voice.

        Enrollment uploads the reference clip once from Electron. Runtime calls
        send only response text and the provider-issued voice id.
        """
        from .money_egress import qwen_voice_clone_egress
        voice_id = self.settings.tts_voice_id or ""
        if not self.settings.tts_api_key or not voice_id or self.settings.tts_model != QWEN_VOICE_CLONE_MODEL:
            raise VoiceSynthesisNotConfiguredError("家庭参考音色尚未完成注册和试听。")
        try:
            request_headers = {"Authorization": f"Bearer {self.settings.tts_api_key}", "Content-Type": "application/json", "Accept": "text/event-stream", "X-DashScope-SSE": "enable"}
            request_json = {"model": QWEN_VOICE_CLONE_MODEL, "input": {"text": text, "voice": voice_id, "language_type": "Chinese"}}
            async with qwen_voice_clone_egress(self.settings, text, self.client, QWEN_VOICE_SYNTHESIS_URL, request_headers, request_json) as response:
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").lower()
                if "text/event-stream" not in content_type:
                    source = await self._qwen_complete_audio(response)
                    for frame in self._wav_frames(source):
                        yield frame
                    return

                sample_rate: int | None = None
                buffered = bytearray()
                data_offset = data_size = emitted_bytes = 0
                response_bytes = 0
                emitted = 0
                final_url = ""
                supplier_request_id = None
                supplier_completed = False
                transport_done = False
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    value = line[5:].strip()
                    if value == "[DONE]":
                        if not supplier_completed or transport_done:
                            raise VoiceSynthesisProtocolError("家庭参考音色流没有可靠结束事件。")
                        transport_done = True
                        continue
                    if supplier_completed:
                        raise VoiceSynthesisProtocolError("家庭参考音色结束后仍返回数据。")
                    event = json.loads(value)
                    identity = event.get("request_id") if isinstance(event, dict) else None
                    if not isinstance(identity, str) or not 1 <= len(identity) <= 128 or (supplier_request_id is not None and identity != supplier_request_id):
                        raise VoiceSynthesisProtocolError("家庭参考音色事件不属于同一请求。")
                    supplier_request_id = identity
                    if event.get("code"):
                        raise VoiceSynthesisError("家庭参考音色合成失败。")
                    output = event.get("output", {})
                    if not isinstance(output, dict):
                        raise VoiceSynthesisProtocolError("家庭参考音色事件结构无效。")
                    reason = output.get("finish_reason")
                    if reason not in (None, "", "null", "stop"):
                        raise VoiceSynthesisProtocolError("家庭参考音色没有正常生成结束。")
                    supplier_completed = reason == "stop"
                    audio = output.get("audio", {})
                    if not isinstance(audio, dict):
                        raise VoiceSynthesisProtocolError("家庭参考音色事件没有有效音频结构。")
                    final_url = str(audio.get("url") or final_url)
                    encoded = audio.get("data")
                    if not encoded:
                        continue
                    if isinstance(encoded, str) and encoded.startswith("data:"):
                        encoded = encoded.split(",", 1)[-1]
                    chunk = base64.b64decode(encoded, validate=True)
                    response_bytes += len(chunk)
                    if response_bytes > MAX_RESPONSE_BYTES:
                        raise VoiceSynthesisProtocolError("家庭参考音色响应超出安全大小限制。")
                    buffered.extend(chunk)
                    if sample_rate is None:
                        header = self._streaming_wav_header(bytes(buffered))
                        if not header:
                            continue
                        sample_rate, data_offset = header
                        data_size = int.from_bytes(buffered[data_offset - 4:data_offset], "little")
                        if data_size <= 0 or data_size % 2 or data_size > MAX_RESPONSE_BYTES:
                            raise VoiceSynthesisProtocolError("家庭参考音色流没有有效声明样本。")
                    available = min(len(buffered) - data_offset, data_size) - emitted_bytes
                    even_length = available - available % 2
                    while even_length:
                        length = min(even_length, MAX_FRAME_BYTES)
                        start = data_offset + emitted_bytes
                        payload = bytes(buffered[start:start + length])
                        emitted_bytes += length
                        even_length -= length
                        emitted += 1
                        yield sample_rate.to_bytes(4, "little") + len(payload).to_bytes(4, "little") + payload
                if not supplier_completed:
                    raise VoiceSynthesisProtocolError("家庭参考音色流缺少生成结束事件。")
                if emitted:
                    verified = self._wav_frames(bytes(buffered))
                    if sum(len(frame) - PCM_FRAME_HEADER_BYTES for frame in verified) != emitted_bytes:
                        raise VoiceSynthesisProtocolError("家庭参考音色流的声明和实际样本不一致。")
                    return
                source = await self._download_qwen_audio(final_url)
                for frame in self._wav_frames(source):
                    yield frame
        except VoiceSynthesisError:
            raise
        except (httpx.HTTPError, ValueError, TypeError) as error:
            raise VoiceSynthesisError("家庭参考音色暂时不可用。") from error

    async def _qwen_complete_audio(self, response: httpx.Response) -> bytes:
        body = json.loads((await response.aread()).decode("utf-8"))
        audio = body.get("output", {}).get("audio", {})
        encoded = audio.get("data")
        if encoded:
            if isinstance(encoded, str) and encoded.startswith("data:"):
                encoded = encoded.split(",", 1)[-1]
            return base64.b64decode(encoded, validate=True)
        return await self._download_qwen_audio(str(audio.get("url") or ""))

    async def _download_qwen_audio(self, source_url: str) -> bytes:
        # A separately billed/download contract is not proven for ACTIVE mode.
        # Preserve the original NORMAL URL capability; never send an unpriced GET.
        from .money_egress import deny_unpriced
        deny_unpriced('qwen_voice_clone_audio_download')
        parsed = urlparse(source_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or not parsed.hostname.endswith(".aliyuncs.com"):
            raise VoiceSynthesisProtocolError("家庭参考音色返回了不可信的音频地址。")
        if parsed.scheme == "http":
            source_url = parsed._replace(scheme="https").geturl()
        downloaded = await self.client.get(source_url)
        downloaded.raise_for_status()
        if len(downloaded.content) > MAX_RESPONSE_BYTES:
            raise VoiceSynthesisProtocolError("家庭参考音色响应超出安全大小限制。")
        return downloaded.content

    @staticmethod
    def _streaming_wav_header(source: bytes) -> tuple[int, int] | None:
        if len(source) < 12 or source[:4] != b"RIFF" or source[8:12] != b"WAVE":
            return None
        offset = 12
        channels = sample_width = sample_rate = 0
        while offset + 8 <= len(source):
            kind = source[offset:offset + 4]
            length = int.from_bytes(source[offset + 4:offset + 8], "little")
            start = offset + 8
            if kind == b"fmt ":
                if start + min(length, 16) > len(source) or length < 16:
                    return None
                if int.from_bytes(source[start:start + 2], "little") != 1:
                    raise VoiceSynthesisProtocolError("家庭参考音色不是 PCM WAV。")
                channels = int.from_bytes(source[start + 2:start + 4], "little")
                sample_rate = int.from_bytes(source[start + 4:start + 8], "little")
                sample_width = int.from_bytes(source[start + 14:start + 16], "little") // 8
            elif kind == b"data":
                if channels != 1 or sample_width != 2 or sample_rate not in {16000, 24000, 48000}:
                    raise VoiceSynthesisProtocolError("家庭参考音色返回了无效 PCM 参数。")
                return sample_rate, start
            if start + length > len(source):
                return None
            offset = start + length + length % 2
        return None

    @classmethod
    def _wav_frames(cls, source: bytes) -> list[bytes]:
        if len(source) > MAX_RESPONSE_BYTES:
            raise VoiceSynthesisProtocolError("家庭参考音色响应超出安全大小限制。")
        if (len(source) < 12 or source[:4] != b"RIFF" or source[8:12] != b"WAVE"
                or int.from_bytes(source[4:8], "little") != len(source) - 8):
            raise VoiceSynthesisProtocolError("家庭参考音色没有返回完整 WAV。")
        # A complete RIFF container is not necessarily complete PCM. Read only
        # its declared data chunk, not trailing LIST/JUNK metadata or a prefix.
        try:
            with wave.open(io.BytesIO(source), "rb") as reader:
                sample_rate = reader.getframerate()
                frame_count = reader.getnframes()
                if (reader.getnchannels() != 1 or reader.getsampwidth() != 2
                        or reader.getcomptype() != "NONE" or sample_rate not in {16000, 24000, 48000}
                        or frame_count <= 0 or frame_count * 2 > MAX_RESPONSE_BYTES):
                    raise VoiceSynthesisProtocolError("家庭参考音色返回了无效 PCM 参数。")
                pcm = reader.readframes(frame_count + 1)
                if len(pcm) != frame_count * 2:
                    raise VoiceSynthesisProtocolError("家庭参考音色没有返回完整 PCM 音频。")
        except VoiceSynthesisProtocolError:
            raise
        except (wave.Error, EOFError, RuntimeError) as error:
            raise VoiceSynthesisProtocolError("家庭参考音色没有返回有效 WAV。") from error
        frames: list[bytes] = []
        for offset in range(0, len(pcm), 48_000):
            payload = pcm[offset:offset + 48_000]
            frames.append(sample_rate.to_bytes(4, "little") + len(payload).to_bytes(4, "little") + payload)
        return frames

    async def _qwen_audio_frames(self, text: str) -> AsyncIterator[bytes]:
        """Bridge the official DashScope callback stream to framed PCM.

        Qwen-Audio-TTS has a different voice catalogue from CosyVoice.  It is
        an explicit temporary/fallback provider, never a silent substitution
        for the parent-selected CosyVoice main voice.
        """
        from .money_egress import deny_unpriced
        deny_unpriced('voice_synthesis.py:_qwen_audio_frames')
        if not self.settings.tts_workspace_id or self.settings.tts_voice_id not in QWEN_AUDIO_ADULT_VOICE_IDS:
            raise VoiceSynthesisNotConfiguredError("Qwen-Audio-TTS 需要工作空间和已审核的成人女声音色。")
        try:
            import dashscope
            from dashscope.audio.tts_v2 import AudioFormat, SpeechSynthesizer
            from dashscope.audio.tts_v2.speech_synthesizer import ResultCallback
        except ImportError as error:
            raise VoiceSynthesisNotConfiguredError("Qwen-Audio-TTS 运行依赖未安装。") from error
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[tuple[str, object]] = asyncio.Queue(maxsize=32)
        dropped = threading.Event()

        class Callback(ResultCallback):
            def on_data(self, data: bytes) -> None:
                def enqueue() -> None:
                    if queue.full(): dropped.set(); return
                    queue.put_nowait(("data", bytes(data)))
                loop.call_soon_threadsafe(enqueue)
            def on_complete(self) -> None:
                def enqueue_complete() -> None:
                    if queue.full(): dropped.set(); return
                    queue.put_nowait(("complete", None))
                loop.call_soon_threadsafe(enqueue_complete)
            def on_error(self, message: object) -> None:
                def enqueue_error() -> None:
                    if queue.full(): dropped.set(); return
                    queue.put_nowait(("error", str(message)))
                loop.call_soon_threadsafe(enqueue_error)

        # DashScope v1 SDK reads this process-local value for the websocket
        # handshake.  Electron injects it only into this local child process.
        dashscope.api_key = self.settings.tts_api_key
        url = f"wss://{self.settings.tts_workspace_id}.{self.settings.tts_region}.maas.aliyuncs.com/api-ws/v1/inference"
        synthesizer = SpeechSynthesizer(model=self.settings.tts_model, voice=self.settings.tts_voice_id, format=AudioFormat.PCM_24000HZ_MONO_16BIT, callback=Callback(), workspace=self.settings.tts_workspace_id, url=url)
        completing: asyncio.Task[object] | None = None
        cancelled = False
        closed = False

        async def cancel_once() -> None:
            nonlocal cancelled
            if not cancelled:
                cancelled = True
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await _run_sync_started_bounded(synthesizer.streaming_cancel, QWEN_SHUTDOWN_TIMEOUT_SECONDS)

        async def close_once() -> None:
            nonlocal closed
            if not closed:
                closed = True
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await _run_sync_started_bounded(synthesizer.close, QWEN_SHUTDOWN_TIMEOUT_SECONDS)

        try:
            await asyncio.to_thread(synthesizer.streaming_call, text)
            completing = asyncio.create_task(asyncio.to_thread(synthesizer.streaming_complete, int(self.settings.tts_timeout_seconds * 1000)))
            while True:
                kind, value = await asyncio.wait_for(queue.get(), timeout=self.settings.tts_timeout_seconds)
                if dropped.is_set(): raise VoiceSynthesisProtocolError("Qwen-Audio-TTS 音频队列超出安全限制。")
                if kind == "data":
                    frame = value if isinstance(value, bytes) else b""
                    if not frame or len(frame) > MAX_FRAME_BYTES or len(frame) % 2:
                        raise VoiceSynthesisProtocolError("Qwen-Audio-TTS 返回了无效 PCM 帧。")
                    yield (24000).to_bytes(4, "little") + len(frame).to_bytes(4, "little") + frame
                elif kind == "error":
                    raise VoiceSynthesisError("Qwen-Audio-TTS 合成失败。")
                elif kind == "complete":
                    await completing
                    return
        except asyncio.CancelledError:
            await cancel_once()
            raise
        except (TimeoutError, ValueError) as error:
            raise VoiceSynthesisError("Qwen-Audio-TTS 暂时不可用。") from error
        finally:
            if completing and not completing.done():
                # A provider error or a client disconnect must terminate the
                # SDK stream before its completion worker is abandoned.  The
                # guard also makes cancellation idempotent across this finally
                # block and the CancelledError path above.
                with contextlib.suppress(Exception): await cancel_once()
                completing.cancel()
                with contextlib.suppress(asyncio.CancelledError): await completing
            await close_once()

    async def _aliyun_cosyvoice_frames(self, text: str) -> AsyncIterator[bytes]:
        """Official Model Studio duplex WebSocket mapped to our framed PCM contract.

        Model Studio credentials never reach the renderer.  This adapter accepts
        only model-bound adult companion candidates and raw PCM, avoiding
        browser-side vendor SDKs and MP3 frame reassembly.
        """
        from .money_egress import cosyvoice_egress
        workspace = self.settings.tts_workspace_id
        voice_id = self.settings.tts_voice_id
        if not workspace or not supports_cosyvoice_companion(self.settings.tts_model, voice_id, self.settings.tts_region):
            raise VoiceSynthesisNotConfiguredError("阿里云 CosyVoice 的模型、区域与成人主声音色不匹配。")
        if not self.settings.tts_region.replace("-", "").isalnum():
            raise VoiceSynthesisNotConfiguredError("阿里云区域配置无效。")
        url = f"wss://{workspace}.{self.settings.tts_region}.maas.aliyuncs.com/api-ws/v1/inference"
        task_id = str(uuid4())
        headers = {"Authorization": f"Bearer {self.settings.tts_api_key}", "X-DashScope-WorkSpace": workspace}
        packets = (
            json.dumps({"header": {"action": "run-task", "task_id": task_id, "streaming": "duplex"}, "payload": {"task_group": "audio", "task": "tts", "function": "SpeechSynthesizer", "model": self.settings.tts_model, "parameters": {"text_type": "PlainText", "voice": voice_id, "format": "pcm", "sample_rate": 24000, "volume": 50, "rate": 1.0, "pitch": 1.0, "enable_ssml": False}, "input": {}}}, ensure_ascii=False),
            json.dumps({"header": {"action": "continue-task", "task_id": task_id, "streaming": "duplex"}, "payload": {"input": {"text": text}}}, ensure_ascii=False),
            json.dumps({"header": {"action": "finish-task", "task_id": task_id, "streaming": "duplex"}, "payload": {"input": {}}}, ensure_ascii=False),
        )
        try:
            async with cosyvoice_egress(self.settings, text, url, task_id, packets) as money_send:
                async with websockets.connect(url, additional_headers=headers, open_timeout=self.settings.tts_timeout_seconds, close_timeout=2, max_size=MAX_RESPONSE_BYTES) as socket:
                    await money_send.send(socket, packets[0])
                    started = False
                    while not started:
                        event = await asyncio.wait_for(socket.recv(), timeout=self.settings.tts_timeout_seconds)
                        if isinstance(event, bytes):
                            raise VoiceSynthesisProtocolError("阿里云 CosyVoice 在任务启动前返回了音频。")
                        payload = json.loads(event)
                        if not isinstance(payload, dict) or not isinstance(payload.get("header"), dict) or payload["header"].get("task_id") != task_id:
                            raise VoiceSynthesisProtocolError("阿里云 CosyVoice 事件不属于当前任务。")
                        event_name = payload.get("header", {}).get("event") or payload.get("header", {}).get("action")
                        if event_name == "task-started":
                            started = True
                            money_send.task_started()
                        elif event_name == "task-failed": raise VoiceSynthesisProviderRejectedError(payload.get('header', {}))
                    await money_send.send(socket, packets[1])
                    await money_send.send(socket, packets[2])
                    finished = False
                    while not finished:
                        event = await asyncio.wait_for(socket.recv(), timeout=self.settings.tts_timeout_seconds)
                        if isinstance(event, bytes):
                            if not event or len(event) > MAX_FRAME_BYTES or len(event) % 2:
                                raise VoiceSynthesisProtocolError("阿里云 CosyVoice 返回了无效 PCM 帧。")
                            yield (24000).to_bytes(4, "little") + len(event).to_bytes(4, "little") + event
                            continue
                        payload = json.loads(event)
                        if not isinstance(payload, dict) or not isinstance(payload.get("header"), dict) or payload["header"].get("task_id") != task_id:
                            raise VoiceSynthesisProtocolError("阿里云 CosyVoice 事件不属于当前任务。")
                        event_name = payload.get("header", {}).get("event") or payload.get("header", {}).get("action")
                        if event_name == "task-finished":
                            finished = True
                            money_send.task_finished()
                        elif event_name == "task-failed": raise VoiceSynthesisProviderRejectedError(payload.get('header', {}))
                money_send.socket_closed(socket)
        except asyncio.CancelledError:
            # Closing the socket on cancellation stops further audio; CosyVoice
            # v2+ also supports finish-task/cancel, but client disconnect must
            # never delay barge-in behind an additional network round trip.
            raise
        except (OSError, ValueError, websockets.WebSocketException) as error:
            raise VoiceSynthesisError("阿里云 CosyVoice 暂时不可用。") from error

    def _active_session_child(self, session_id: str) -> str:
        with self.database.connect() as connection:
            row = connection.execute("SELECT child_id FROM daily_session WHERE session_id = ? AND status = 'active'", (session_id,)).fetchone()
        if not row:
            raise UnknownSessionError(session_id)
        return str(row["child_id"])

    @staticmethod
    def _validate_frame(frame: bytes) -> tuple[int, int]:
        if len(frame) < PCM_FRAME_HEADER_BYTES:
            raise VoiceSynthesisProtocolError("语音服务返回了不完整的 PCM 帧。")
        sample_rate = int.from_bytes(frame[:4], "little")
        payload_size = int.from_bytes(frame[4:8], "little")
        if sample_rate not in {8000, 16000, 22050, 24000, 32000, 44100, 48000} or payload_size <= 0 or payload_size > MAX_FRAME_BYTES or payload_size % 2 or len(frame) != PCM_FRAME_HEADER_BYTES + payload_size:
            raise VoiceSynthesisProtocolError("语音服务返回了无效的 PCM 帧。")
        return sample_rate, payload_size

    def _cache_get(self, key: str) -> CachedPcm | None:
        value = self._cache.get(key)
        if not value or monotonic() - value.created_at > CACHE_TTL_SECONDS:
            removed = self._cache.pop(key, None)
            if removed: self._cache_bytes -= removed.byte_count
            return None
        self._cache.move_to_end(key)
        return value

    def _cache_put(self, key: str, frames: tuple[bytes, ...]) -> None:
        byte_count = sum(len(frame) for frame in frames)
        if not frames or byte_count > CACHE_MAX_ENTRY_BYTES:
            return
        previous = self._cache.pop(key, None)
        if previous: self._cache_bytes -= previous.byte_count
        self._cache[key] = CachedPcm(frames, monotonic(), byte_count)
        self._cache_bytes += byte_count
        self._cache.move_to_end(key)
        while len(self._cache) > CACHE_MAX_ITEMS or self._cache_bytes > CACHE_MAX_TOTAL_BYTES:
            _, removed = self._cache.popitem(last=False)
            self._cache_bytes -= removed.byte_count

    def _audit(self, child_id: str, session_id: str, action_id: str, status: str, text_chars: int, first_frame_ms: int | None, total_ms: int, frame_count: int, cache_hit: bool, error_code: str | None) -> None:
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO audit_event(audit_id, actor, action, child_id, summary_json, occurred_at) VALUES (?, 'system', 'voice_playback', ?, ?, ?)",
                (str(uuid4()), child_id, json.dumps({"session_id": session_id, "request_id": action_id, "provider": self.settings.tts_provider, "status": status, "text_chars": text_chars, "first_frame_ms": first_frame_ms, "total_ms": total_ms, "frame_count": frame_count, "cache_hit": cache_hit, "error_code": error_code}, ensure_ascii=False), utc_now()),
            )
            connection.commit()
