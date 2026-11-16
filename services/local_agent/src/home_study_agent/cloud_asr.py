from __future__ import annotations

import base64
from dataclasses import dataclass
import time
from typing import Callable

import httpx

from .config import Settings


class CloudAsrError(RuntimeError):
    pass


@dataclass(frozen=True)
class CloudAsrResult:
    transcript: str
    model: str
    latency_ms: int
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


class CloudAsrNoSpeechError(CloudAsrError):
    """A valid successful recognition response contained no usable text."""

    def __init__(self, result: CloudAsrResult):
        super().__init__("CLOUD_ASR_EMPTY_TRANSCRIPT")
        self.result = result


class CloudAsrAudioPermissionError(CloudAsrError):
    def __init__(self, result: CloudAsrResult | None = None):
        super().__init__("CLOUD_ASR_AUDIO_NOT_ALLOWED")
        self.result = result


class CloudAsrGateway:
    def __init__(self, settings: Settings, runtime_policy=None,
                 cloud_audio_authorized: Callable[[], bool] | None = None):
        self.settings = settings
        self.runtime_policy = runtime_policy
        self.cloud_audio_authorized = cloud_audio_authorized

    def _require_audio_permission(self, result: CloudAsrResult | None = None) -> None:
        try:
            allowed = self.cloud_audio_authorized is not None and self.cloud_audio_authorized() is True
        except Exception:
            allowed = False
        if not allowed:
            raise CloudAsrAudioPermissionError(result)

    @property
    def configured(self) -> bool:
        return (
            self.settings.cloud_asr_provider == "dashscope"
            and bool(self.settings.cloud_asr_api_key)
            and bool(self.settings.cloud_asr_model)
        )

    @property
    def context_supported(self) -> bool:
        return self.configured and self.settings.cloud_asr_model.strip().lower().startswith("qwen3-asr-")

    def transcribe(self, wav_bytes: bytes, context_text: str | None = None) -> CloudAsrResult:
        if not self.configured:
            raise CloudAsrError("CLOUD_ASR_NOT_CONFIGURED")
        self._require_audio_permission()
        if self.runtime_policy and not self.runtime_policy.endpoint_available(
            "qwen", self.settings.cloud_asr_model
        ):
            raise CloudAsrError("CLOUD_ASR_ROUTE_UNAVAILABLE")
        started = time.monotonic()
        messages: list[dict] = []
        cleaned_context = " ".join((context_text or "").split())[:2_000]
        if cleaned_context and self.context_supported:
            # Qwen3-ASR defines system messages as recognition context only.
            messages.append({"role": "system", "content": [{"text": cleaned_context}]})
        messages.append({
            "role": "user",
            "content": [{
                "type": "input_audio",
                "input_audio": {
                    "data": "data:audio/wav;base64," + base64.b64encode(wav_bytes).decode("ascii")
                },
            }],
        })
        payload = {
            "model": self.settings.cloud_asr_model,
            "messages": messages,
            "stream": False,
            # Do not force one language: homework conversations commonly mix
            # Chinese with English words, letters, formulas, and PET practice.
            "asr_options": {"enable_itn": True},
        }
        from .money_egress import dispatch_asr, money_active
        active_money = money_active()
        until = started + self.settings.cloud_asr_timeout_seconds
        def send(actual_wav):
            if actual_wav is not wav_bytes or active_money and time.monotonic() >= until:
                from .money_egress import BudgetHold
                raise BudgetHold('MONEY_EGRESS_DEADLINE')
            options = {'timeout': max(0.001, until-time.monotonic()), 'follow_redirects': False, 'trust_env': False} if active_money else {'timeout': self.settings.cloud_asr_timeout_seconds}
            with httpx.Client(**options) as client:
                if active_money and time.monotonic() >= until:
                    from .money_egress import BudgetHold
                    raise BudgetHold('MONEY_EGRESS_DEADLINE')
                # A queued request must consult the latest durable consent at
                # the actual send boundary, after opening the HTTP client.
                self._require_audio_permission()
                return client.post(
                    f"{self.settings.cloud_asr_base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.settings.cloud_asr_api_key}"},
                    json=payload,
                )
        try:
            response = dispatch_asr(wav_bytes, self.settings, send, until)
            response.raise_for_status()
            body = response.json()
            content = body["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise TypeError("ASR response content must be text")
            transcript = content.strip()
            usage = body.get("usage")
            if usage is not None and not isinstance(usage, dict):
                raise TypeError("ASR response usage must be an object")
            for key in ("prompt_tokens", "completion_tokens"):
                value = usage.get(key) if usage is not None else None
                if value is not None and (type(value) is not int or value < 0):
                    raise TypeError("ASR response token usage must be a nonnegative integer")
        except httpx.TimeoutException as error:
            self._record_failure("MODEL_TIMEOUT")
            raise CloudAsrError("CLOUD_ASR_TIMEOUT") from error
        except httpx.HTTPStatusError as error:
            status = error.response.status_code
            self._record_failure(f"MODEL_HTTP_{status}")
            code = "CLOUD_ASR_RATE_LIMITED" if status == 429 else f"CLOUD_ASR_HTTP_{status}"
            raise CloudAsrError(code) from error
        except httpx.HTTPError as error:
            self._record_failure("MODEL_NETWORK_ERROR")
            raise CloudAsrError("CLOUD_ASR_REQUEST_FAILED") from error
        except (KeyError, IndexError, TypeError, ValueError) as error:
            self._record_failure("MODEL_RESPONSE_INVALID")
            raise CloudAsrError("CLOUD_ASR_REQUEST_FAILED") from error
        result = CloudAsrResult(
            transcript=transcript,
            model=self.settings.cloud_asr_model,
            latency_ms=round((time.monotonic() - started) * 1000),
            prompt_tokens=usage.get("prompt_tokens") if isinstance(usage, dict) else None,
            completion_tokens=usage.get("completion_tokens") if isinstance(usage, dict) else None,
        )
        # An already-sent request cannot be recalled, but its text must not be
        # used after consent was withdrawn while the provider was responding.
        self._require_audio_permission(result)
        if self.runtime_policy:
            self.runtime_policy.record_success("qwen", self.settings.cloud_asr_model)
        # Silence and unrecognised speech are expected recognition outcomes,
        # not endpoint failures. They must not trip the shared route breaker.
        if not transcript:
            raise CloudAsrNoSpeechError(result)
        return result

    def _record_failure(self, code: str) -> None:
        if self.runtime_policy:
            self.runtime_policy.record_failure("qwen", self.settings.cloud_asr_model, code)
