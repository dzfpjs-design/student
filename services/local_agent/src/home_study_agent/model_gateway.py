from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field, InitVar, replace
import hashlib
import json
import re
from time import monotonic
from typing import Any, Callable
from urllib.parse import urlparse

import httpx

from .config import Settings, dashscope_vision_base_url, validate_tutor_explanation_url, validate_tutor_explanation_model


OUTPUT_TYPES = frozenset({
    "daily_plan", "tutoring", "diagnosis", "report", "dialogue", "knowledge_structuring",
})
RISK_LEVELS = frozenset({"low", "medium", "high"})
VERIFICATION_STATUSES = frozenset({"verified", "partially_verified", "unverified", "rejected"})
SAFETY_STATUSES = frozenset({"passed", "degraded", "blocked", "escalated"})
NATIVE_CURRICULUM_INTENT = 'native_curriculum_intent'
VOICE_INTENT_REQUEST_TYPES = frozenset({"voice_agent_intent", "voice_agent_intent_eval", NATIVE_CURRICULUM_INTENT})
LOW_LATENCY_REQUEST_TYPES = VOICE_INTENT_REQUEST_TYPES | {"agent_plan", "voice_interaction"}
REASONING_TUTOR_REQUEST_TYPES = frozenset({'tutor_whole_explanation','tutor_whole_explanation_check'})
MISTAKE_LEARNING_OUTPUT_LIMITS = {
    'mistake_learning_analyze': 1800, 'mistake_learning_practice': 3600,
    'mistake_learning_coach': 1600, 'mistake_learning_answer': 1800,
    'mistake_learning_verify': 1200, 'mistake_learning_topic': 8192,
}

MISTAKE_REASONING_TYPES = frozenset(MISTAKE_LEARNING_OUTPUT_LIMITS) - {'mistake_learning_topic'}
TUTOR_EXPLANATION_REQUEST_TYPES = frozenset({
    'tutor_whole_explanation', 'tutor_whole_explanation_check',
    'mistake_learning_analyze', 'mistake_learning_practice', 'mistake_learning_coach',
    'mistake_learning_verify', 'mistake_learning_topic', 'mistake_variant_generation',
})
MISTAKE_REASONING_CONFIG_REQUIRED = 'MODEL_MISTAKE_REASONING_CONFIG_REQUIRED'
TUTOR_EXPLANATION_CONFIG_REQUIRED = 'MODEL_TUTOR_EXPLANATION_CONFIG_REQUIRED'


def _is_explanation_request(request_type: str | None) -> bool:
    return request_type in TUTOR_EXPLANATION_REQUEST_TYPES or bool(request_type and request_type.startswith('tutor_hint_level_'))


def requires_json_object(request_type: str) -> bool:
    return request_type in {
        "curriculum_internalization", "atomic_knowledge_extraction", "agent_plan", "voice_interaction",
        "learning_outcome_evaluation", "mistake_variant_generation",
        "document_learning_knowledge_structuring", "paper_scoring_authority_draft", "paper_printed_score_candidate",
        "child_daily_analysis", NATIVE_CURRICULUM_INTENT,
    } or request_type.startswith("tutor_hint_level_") or request_type in REASONING_TUTOR_REQUEST_TYPES or request_type in MISTAKE_LEARNING_OUTPUT_LIMITS


@dataclass(frozen=True)
class ModelRequest(Sequence[dict[str, str]]):
    request_id: str
    child_id: str
    session_id: str
    request_type: str
    risk_level: str
    context_manifest: dict[str, Any]
    provider_preference: str
    policy_version: str
    prompt_version: str
    knowledge_version: str | None
    output_type: str
    evidence_refs: tuple[str, ...]
    messages: tuple[dict[str, str], ...]
    # Operational authority is not a dataclass field: snapshots/asdict cannot
    # copy it into provider payloads, diagnostic logs or persisted requests.
    pre_dispatch: InitVar[Callable[[], None] | None] = None

    def __post_init__(self, pre_dispatch) -> None:
        required = {
            "request_id": self.request_id,
            "child_id": self.child_id,
            "session_id": self.session_id,
            "request_type": self.request_type,
            "provider_preference": self.provider_preference,
            "policy_version": self.policy_version,
            "prompt_version": self.prompt_version,
        }
        missing = [name for name, value in required.items() if not str(value).strip()]
        if missing:
            raise ValueError(f"missing model request fields: {', '.join(missing)}")
        if self.risk_level not in RISK_LEVELS:
            raise ValueError(f"invalid model risk level: {self.risk_level}")
        if self.output_type not in OUTPUT_TYPES:
            raise ValueError(f"invalid model output type: {self.output_type}")
        if not isinstance(self.context_manifest, dict):
            raise ValueError("context_manifest must be an object")
        if pre_dispatch is not None and not callable(pre_dispatch):
            raise TypeError('pre_dispatch must be a local callable')
        object.__setattr__(self, 'pre_dispatch', pre_dispatch)
        normalized_messages: list[dict[str, str]] = []
        for message in self.messages:
            role = message.get("role")
            content = message.get("content")
            if role not in {"system", "user", "assistant"} or not isinstance(content, str) or not content.strip():
                raise ValueError("model messages require a supported role and non-empty content")
            normalized_messages.append({"role": role, "content": content})
        if not normalized_messages:
            raise ValueError("model request requires at least one message")
        object.__setattr__(self, "messages", tuple(normalized_messages))
        object.__setattr__(self, "evidence_refs", tuple(dict.fromkeys(ref for ref in self.evidence_refs if ref)))

    # Keeping the request sequence-compatible makes local test adapters and provider
    # shims consume the same sanitized message collection without a second API.
    def __len__(self) -> int:
        return len(self.messages)

    def __getitem__(self, index):
        return self.messages[index]

    def __iter__(self) -> Iterator[dict[str, str]]:
        return iter(self.messages)


@dataclass(frozen=True)
class ModelResult:
    content: str
    provider: str
    model: str
    latency_ms: int
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    fallback_used: bool = False
    scorer_provenance: dict[str, Any] | None = None
    reply_provenance: dict[str, Any] | None = None


@dataclass(frozen=True)
class ModelEndpoint:
    provider: str
    api_key: str = field(repr=False)
    base_url: str = field(repr=False)
    model: str


def _qwen_hybrid_reasoning_endpoint(endpoint):
    """Configured official hybrid text models only, never a guessed provider."""
    return (all(isinstance(getattr(endpoint,key,None),str) for key in ('provider','base_url','model'))
        and endpoint.provider.strip().lower() == 'qwen'
        and dashscope_vision_base_url(endpoint.base_url) == endpoint.base_url
        and re.fullmatch(r'qwen3\.\d+-(?:plus|flash)(?:-(?:latest|\d{4}-\d{2}-\d{2}))?', endpoint.model) is not None)


def _scorer_request_provenance(endpoint, payload):
    def digest(value):
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode()).hexdigest()
    return {"version": "model_scorer_provenance_v1", "provider": endpoint.provider,
        "requested_model": endpoint.model, "endpoint_scope_sha256": digest(endpoint.base_url),
        "system_prompt_sha256": digest([m['content'] for m in payload['messages'] if m['role'] == 'system']),
        "parameters_sha256": digest({key: value for key, value in payload.items() if key != 'messages'}),
        "immutable_revision_verified": False, "local_only": True, "cloud_transmission_allowed": False}


def _reported_identifier(value):
    return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}', value) else None


class ModelGateway:
    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
        runtime_policy: Any | None = None,
    ):
        self.settings = settings
        self.runtime_policy = runtime_policy
        base_url = validate_tutor_explanation_url(settings.tutor_explanation_base_url)
        model = validate_tutor_explanation_model(settings.tutor_explanation_model)
        key = settings.tutor_explanation_api_key
        self._explanation_endpoint = (ModelEndpoint('deepseek', key, base_url, model)
            if isinstance(key, str) and re.fullmatch(r'[!-~]{16,512}', key) and base_url and model else None)
        self._client = httpx.AsyncClient(
            timeout=settings.model_timeout_seconds,
            transport=transport,
        )
        self._explanation_client = httpx.AsyncClient(
            timeout=httpx.Timeout(75.0, connect=8.0),
            transport=transport,
        ) if self._explanation_endpoint is not None else None

    @property
    def configured(self) -> bool:
        return bool(self.settings.model_api_key)

    def configured_for(self, request_type: str) -> bool:
        if _is_explanation_request(request_type):
            return self._explanation_endpoint is not None
        return self.configured

    def _is_explanation_endpoint(self, endpoint: ModelEndpoint, request_type: str) -> bool:
        return (_is_explanation_request(request_type) and self._explanation_endpoint is not None
                and endpoint == self._explanation_endpoint)

    @property
    def fallback_configured(self) -> bool:
        return len(self._endpoints()) > 1

    async def close(self) -> None:
        await self._client.aclose()
        if self._explanation_client is not None:
            await self._explanation_client.aclose()

    async def complete(self, request: ModelRequest) -> ModelResult:
        if not isinstance(request, ModelRequest):
            raise TypeError("ModelGateway.complete requires ModelRequest")
        from .agent_request_ownership import authorize_external_dispatch

        authorize_external_dispatch()
        if _is_explanation_request(request.request_type) and not self.configured_for(request.request_type):
            raise ModelResponseError(MISTAKE_REASONING_CONFIG_REQUIRED if request.request_type in MISTAKE_REASONING_TYPES
                                     else TUTOR_EXPLANATION_CONFIG_REQUIRED, fallback_eligible=False)
        if not self.configured_for(request.request_type) and request.request_type not in MISTAKE_REASONING_TYPES:
            raise ModelNotConfiguredError()
        if self.runtime_policy:
            self.runtime_policy.authorize(request.request_type)

        started = monotonic()
        configured_endpoints = self._endpoints(request.request_type)
        endpoints = [
            endpoint for endpoint in configured_endpoints
            if not self.runtime_policy
            or self.runtime_policy.endpoint_available(endpoint.provider, endpoint.model)
        ]
        if not endpoints:
            raise ModelResponseError("MODEL_ALL_ROUTES_UNAVAILABLE", fallback_eligible=False)

        last_error: ModelResponseError | None = None
        discarded_prompt_tokens = 0
        discarded_completion_tokens = 0
        discarded_response_received = False
        discarded_prompt_known = False
        discarded_completion_known = False
        for endpoint_index, endpoint in enumerate(endpoints):
            try:
                result = await self._complete_endpoint(endpoint, request.messages, started, request.request_type,
                                                       pre_dispatch=request.pre_dispatch)
                if request.request_type in {"atomic_knowledge_extraction", "agent_plan"} and not self._is_json_object(result.content):
                    last_error = ModelResponseError("MODEL_INVALID_JSON", fallback_eligible=True,
                        response_received=True, prompt_tokens=result.prompt_tokens,
                        completion_tokens=result.completion_tokens, latency_ms=result.latency_ms)
                    if endpoint_index == len(endpoints) - 1:
                        raise last_error
                    discarded_response_received = True
                    discarded_prompt_known |= last_error.prompt_tokens is not None
                    discarded_completion_known |= last_error.completion_tokens is not None
                    discarded_prompt_tokens += last_error.prompt_tokens or 0
                    discarded_completion_tokens += last_error.completion_tokens or 0
                    if self.runtime_policy:
                        self.runtime_policy.record_failure(endpoint.provider, endpoint.model, last_error.code)
                    continue
                if self.runtime_policy:
                    self.runtime_policy.record_success(endpoint.provider, endpoint.model)
                return ModelResult(
                    content=result.content,
                    provider=result.provider,
                    model=result.model,
                    latency_ms=result.latency_ms,
                    prompt_tokens=(result.prompt_tokens or 0) + discarded_prompt_tokens,
                    completion_tokens=(result.completion_tokens or 0) + discarded_completion_tokens,
                    fallback_used=(not self._is_explanation_endpoint(endpoint, request.request_type) and endpoint != (
                        self._endpoints()[0] if request.request_type in MISTAKE_REASONING_TYPES else configured_endpoints[0])),
                    scorer_provenance=result.scorer_provenance,
                    reply_provenance=result.reply_provenance,
                )
            except ModelResponseError as error:
                if discarded_response_received:
                    error.response_received = True
                    error.prompt_tokens = ((error.prompt_tokens or 0) + discarded_prompt_tokens
                        if error.prompt_tokens is not None or discarded_prompt_known else None)
                    error.completion_tokens = ((error.completion_tokens or 0) + discarded_completion_tokens
                        if error.completion_tokens is not None or discarded_completion_known else None)
                    error.latency_ms = round((monotonic() - started) * 1000)
                if self.runtime_policy:
                    self.runtime_policy.record_failure(endpoint.provider, endpoint.model, error.code)
                last_error = error
                if not error.fallback_eligible or endpoint_index == len(endpoints) - 1:
                    raise
        raise last_error or ModelResponseError("MODEL_REQUEST_FAILED")

    @staticmethod
    def _is_json_object(content: str) -> bool:
        value = content.strip()
        if value.startswith("```"):
            value = value.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        try:
            return isinstance(json.loads(value), dict)
        except json.JSONDecodeError:
            return False

    def independent_provider(self, excluded_provider: str) -> tuple[str, str] | None:
        excluded = excluded_provider.strip().lower()
        for endpoint in self._endpoints():
            if endpoint.provider.strip().lower() != excluded:
                return endpoint.provider, endpoint.model
        return None

    async def complete_on_provider(
        self,
        provider: str,
        request: ModelRequest,
    ) -> ModelResult:
        if not isinstance(request, ModelRequest):
            raise TypeError("ModelGateway.complete_on_provider requires ModelRequest")
        from .agent_request_ownership import authorize_external_dispatch

        authorize_external_dispatch()
        target = provider.strip().lower()
        endpoint = next(
            (
                item
                for item in self._endpoints(request.request_type if request.request_type in MISTAKE_REASONING_TYPES
                                            or request.request_type == NATIVE_CURRICULUM_INTENT
                                            or _is_explanation_request(request.request_type) else None)
                if item.provider.strip().lower() == target
            ),
            None,
        )
        if endpoint is None:
            if request.request_type in MISTAKE_REASONING_TYPES:
                raise ModelResponseError(MISTAKE_REASONING_CONFIG_REQUIRED)
            raise ModelNotConfiguredError()
        if self.runtime_policy:
            self.runtime_policy.authorize(request.request_type)
            if not self.runtime_policy.endpoint_available(endpoint.provider, endpoint.model):
                raise ModelResponseError("MODEL_ROUTE_UNAVAILABLE", fallback_eligible=False)
        try:
            result = await self._complete_endpoint(endpoint, request.messages, monotonic(), request.request_type,
                                                   pre_dispatch=request.pre_dispatch)
        except ModelResponseError as error:
            if self.runtime_policy:
                self.runtime_policy.record_failure(endpoint.provider, endpoint.model, error.code)
            raise
        if self.runtime_policy:
            self.runtime_policy.record_success(endpoint.provider, endpoint.model)
        if request.request_type in MISTAKE_REASONING_TYPES and not self._is_explanation_endpoint(endpoint, request.request_type):
            return replace(result, fallback_used=endpoint != self._endpoints()[0])
        return result

    async def complete_on_fallback(self, request: ModelRequest) -> ModelResult:
        """Run a request on the explicitly configured fallback model only."""
        if not isinstance(request, ModelRequest):
            raise TypeError("ModelGateway.complete_on_fallback requires ModelRequest")
        from .agent_request_ownership import authorize_external_dispatch

        authorize_external_dispatch()
        if _is_explanation_request(request.request_type):
            raise ModelResponseError('MODEL_ROUTE_UNAVAILABLE', fallback_eligible=False)
        endpoints = self._endpoints() if request.request_type in MISTAKE_REASONING_TYPES else self._endpoints(request.request_type)
        if len(endpoints) < 2 or request.request_type in MISTAKE_REASONING_TYPES and (not isinstance(endpoints[1].api_key,str) or not endpoints[1].api_key.strip() or not _qwen_hybrid_reasoning_endpoint(endpoints[1])):
            if request.request_type in MISTAKE_REASONING_TYPES:
                raise ModelResponseError(MISTAKE_REASONING_CONFIG_REQUIRED)
            raise ModelNotConfiguredError()
        endpoint = endpoints[1]
        if self.runtime_policy:
            self.runtime_policy.authorize(request.request_type)
            if not self.runtime_policy.endpoint_available(endpoint.provider, endpoint.model):
                raise ModelResponseError("MODEL_ROUTE_UNAVAILABLE", fallback_eligible=False)
        try:
            result = await self._complete_endpoint(
                endpoint, request.messages, monotonic(), request.request_type,
                pre_dispatch=request.pre_dispatch,
            )
            if request.request_type in {"atomic_knowledge_extraction", "agent_plan"} and not self._is_json_object(result.content):
                raise ModelResponseError("MODEL_INVALID_JSON", fallback_eligible=False,
                    response_received=True, prompt_tokens=result.prompt_tokens,
                    completion_tokens=result.completion_tokens, latency_ms=result.latency_ms)
        except ModelResponseError as error:
            if self.runtime_policy:
                self.runtime_policy.record_failure(endpoint.provider, endpoint.model, error.code)
            raise
        if self.runtime_policy:
            self.runtime_policy.record_success(endpoint.provider, endpoint.model)
        return ModelResult(
            content=result.content,
            provider=result.provider,
            model=result.model,
            latency_ms=result.latency_ms,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            fallback_used=True,
            scorer_provenance=result.scorer_provenance,
            reply_provenance=result.reply_provenance,
        )

    def _endpoints(self, request_type: str | None = None) -> list[ModelEndpoint]:
        if _is_explanation_request(request_type):
            if self._explanation_endpoint is None:
                raise ModelResponseError(MISTAKE_REASONING_CONFIG_REQUIRED if request_type in MISTAKE_REASONING_TYPES
                                         else TUTOR_EXPLANATION_CONFIG_REQUIRED, fallback_eligible=False)
            return [self._explanation_endpoint]
        primary_model = (
            self.settings.voice_intent_model_name
            if request_type in LOW_LATENCY_REQUEST_TYPES and self.settings.voice_intent_model_name
            else self.settings.model_name
        )
        endpoints = [
            ModelEndpoint(
                self.settings.model_provider,
                (self.settings.voice_intent_api_key or self.settings.model_api_key)
                if request_type in LOW_LATENCY_REQUEST_TYPES else self.settings.model_api_key,
                self.settings.model_base_url,
                primary_model,
            )
        ]
        if all((
            self.settings.fallback_model_provider,
            self.settings.fallback_model_api_key,
            self.settings.fallback_model_base_url,
            self.settings.fallback_model_name,
        )):
            endpoints.append(ModelEndpoint(
                self.settings.fallback_model_provider or "",
                self.settings.fallback_model_api_key or "",
                self.settings.fallback_model_base_url or "",
                self.settings.fallback_model_name or "",
            ))
        if request_type in MISTAKE_REASONING_TYPES:
            eligible=next((endpoint for endpoint in endpoints
                if isinstance(endpoint.api_key,str) and endpoint.api_key.strip()
                and _qwen_hybrid_reasoning_endpoint(endpoint)),None)
            if eligible is None:
                raise ModelResponseError(MISTAKE_REASONING_CONFIG_REQUIRED)
            return [eligible]  # One configured route; no fallback to a known-fast path.
        return endpoints

    async def _complete_endpoint(
        self,
        endpoint: ModelEndpoint,
        messages: list[dict[str, str]],
        started: float,
        request_type: str,
        pre_dispatch: Callable[[], None] | None = None,
    ) -> ModelResult:
        last_error: Exception | None = None
        dedicated_explanation = self._is_explanation_endpoint(endpoint, request_type)
        attempts = 1 if dedicated_explanation or request_type in MISTAKE_REASONING_TYPES or request_type == NATIVE_CURRICULUM_INTENT else 2
        for attempt in range(attempts):
            # Revocation failures are not provider failures and must never fall
            # through response parsing, network retries or another endpoint.
            if pre_dispatch is not None:
                pre_dispatch()
            payload = self.request_payload(endpoint, messages, request_type, dedicated_explanation=dedicated_explanation)
            scorer_provenance = _scorer_request_provenance(endpoint, payload)
            physical_guard = getattr(pre_dispatch, 'authorize_physical', None)
            if physical_guard is not None:
                physical_guard(scorer_provenance)
            from .money_egress import send_model_http
            try:
                if dedicated_explanation and self._explanation_client is not None:
                    response = await send_model_http(self._explanation_client, endpoint, payload)
                elif request_type in MISTAKE_REASONING_TYPES:
                    response = await send_model_http(self._client, endpoint, payload, timeout_seconds=35)
                else:
                    response = await send_model_http(self._client, endpoint, payload)
                if response.status_code == 429 or response.status_code >= 500:
                    last_error = httpx.HTTPStatusError("retryable", request=response.request, response=response)
                    if attempt + 1 < attempts:
                        continue
                    raise ModelResponseError(f"MODEL_HTTP_{response.status_code}", fallback_eligible=True)
                if response.status_code in {403, 404}:
                    # A Token Plan can be healthy while a requested model is
                    # unavailable to that plan. A separately credentialed
                    # pay-as-you-go endpoint may serve the same Qwen model.
                    raise ModelResponseError(f"MODEL_HTTP_{response.status_code}", fallback_eligible=True)
                response.raise_for_status()
                payload: dict[str, Any] = response.json()
                finish_reason = payload['choices'][0].get('finish_reason')
                if (request_type in REASONING_TUTOR_REQUEST_TYPES or request_type in MISTAKE_LEARNING_OUTPUT_LIMITS or request_type in {'learning_outcome_evaluation', 'agent_plan'}) and finish_reason not in {None, 'stop'}:
                    usage = payload.get('usage') or {}
                    code = 'MODEL_OUTPUT_TRUNCATED' if finish_reason == 'length' else 'MODEL_OUTPUT_TERMINATED'
                    raise ModelResponseError(code, response_received=True,
                        prompt_tokens=usage.get('prompt_tokens'), completion_tokens=usage.get('completion_tokens'),
                        latency_ms=round((monotonic() - started) * 1000))
                content = payload["choices"][0]["message"]["content"]
                if not isinstance(content, str) or not content.strip():
                    raise ModelResponseError("MODEL_EMPTY_RESPONSE", fallback_eligible=True)
                usage = payload.get("usage") or {}
                return ModelResult(
                    content=content.strip(),
                    provider=endpoint.provider,
                    model=endpoint.model,
                    latency_ms=round((monotonic() - started) * 1000),
                    prompt_tokens=usage.get("prompt_tokens"),
                    completion_tokens=usage.get("completion_tokens"),
                    scorer_provenance={**scorer_provenance,
                        "reported_model": _reported_identifier(payload.get("model")),
                        "provider_system_fingerprint": _reported_identifier(payload.get("system_fingerprint"))},
                    reply_provenance={
                        "version": "public_reply_transport_v1", "provider": endpoint.provider,
                        "requested_model": endpoint.model,
                        "reported_model": _reported_identifier(payload.get("model")),
                        "response_id": _reported_identifier(payload.get("id")),
                        "provider_revision": _reported_identifier(payload.get("system_fingerprint")),
                        "endpoint_sha256": hashlib.sha256(endpoint.base_url.encode()).hexdigest(),
                        "content_sha256": hashlib.sha256(content.strip().encode()).hexdigest(),
                        "messages_sha256": hashlib.sha256(json.dumps(messages, ensure_ascii=False,
                            sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                        "finish_reason": finish_reason,
                    },
                )
            except httpx.TimeoutException as error:
                raise ModelResponseError("MODEL_TIMEOUT", fallback_eligible=True) from error
            except httpx.HTTPStatusError as error:
                raise ModelResponseError(f"MODEL_HTTP_{error.response.status_code}") from error
            except httpx.RequestError as error:
                last_error = error
                if attempt + 1 < attempts:
                    continue
                raise ModelResponseError("MODEL_NETWORK_ERROR", fallback_eligible=True) from error
            except (KeyError, TypeError, ValueError) as error:
                raise ModelResponseError("MODEL_INVALID_RESPONSE", fallback_eligible=True) from error
        raise ModelResponseError("MODEL_REQUEST_FAILED", fallback_eligible=True) from last_error

    @staticmethod
    def request_payload(endpoint, messages, request_type, *, dedicated_explanation=False):
        provider = endpoint.provider.strip().lower()
        host = (urlparse(endpoint.base_url).hostname or "").lower()
        thinking = {}
        reasoning = dedicated_explanation or (provider == 'deepseek' and host == 'api.deepseek.com' and request_type in REASONING_TUTOR_REQUEST_TYPES)
        mistake_reasoning = request_type in MISTAKE_REASONING_TYPES and not dedicated_explanation
        if dedicated_explanation:
            thinking.update(thinking={'type': 'enabled'}, reasoning_effort='high')
        elif mistake_reasoning:
            if not _qwen_hybrid_reasoning_endpoint(endpoint):
                raise ModelResponseError(MISTAKE_REASONING_CONFIG_REQUIRED)
            thinking.update(enable_thinking=True, thinking_budget=256)
        elif provider == 'deepseek' and host == 'api.deepseek.com':
            thinking['thinking'] = {'type': 'enabled' if reasoning else 'disabled'}
            if reasoning:
                thinking['reasoning_effort'] = 'high'
        elif provider in {'qwen', 'deepseek'}:
            thinking['enable_thinking'] = False
        deterministic_request = request_type in VOICE_INTENT_REQUEST_TYPES | {'agent_plan', 'voice_interaction'}
        payload = {'model': endpoint.model, 'messages': messages,
            'temperature': 0 if deterministic_request else 0.3,
            'max_tokens': ModelGateway._max_output_tokens(request_type), 'stream': False, **thinking}
        if mistake_reasoning:
            payload['max_tokens'] = 6144
        elif dedicated_explanation:
            payload['max_tokens'] = 12288
        if reasoning or mistake_reasoning:
            # Official thinking mode ignores temperature. Never include the
            # returned reasoning_content in child speech or future context.
            payload.pop('temperature')
        if requires_json_object(request_type):
            payload['response_format'] = {'type': 'json_object'}
            # JSON-mode providers reject a request without an explicit JSON
            # instruction. Do not mutate the caller's frozen message snapshot.
            if not any(isinstance(m.get('content'),str) and 'json' in m['content'].lower() for m in messages):
                payload['messages'] = [{'role':'system','content':'Return only a valid JSON object.'}, *messages]
        return payload

    @staticmethod
    def _max_output_tokens(request_type: str) -> int:
        if request_type in MISTAKE_LEARNING_OUTPUT_LIMITS:
            return MISTAKE_LEARNING_OUTPUT_LIMITS[request_type]
        if request_type in REASONING_TUTOR_REQUEST_TYPES:
            return 4096
        if request_type in VOICE_INTENT_REQUEST_TYPES:
            return 80
        if request_type == "companion_dialogue":
            return 160
        if request_type == "agent_plan":
            return 1600
        if request_type == "voice_interaction":
            return 500
        if request_type == "learning_outcome_evaluation":
            # Scores, source quotes and criterion evidence share one receipt.
            # The former voice-sized limit truncated all eight real probes.
            return 2400
        if request_type == "paper_scoring_authority_draft":
            return 900
        if request_type.startswith("tutor_hint_level_"):
            # A tutor turn carries both spoken narration and its synchronized
            # board timeline.  The old 320-token voice-only cap truncated valid
            # JSON before the final check question could be persisted.
            return 1600
        if request_type.startswith("tutor_"):
            return 320
        if request_type == "atomic_knowledge_extraction":
            # Review lessons can legitimately yield several complete atomic
            # definitions. A 6k cap truncated otherwise valid JSON in the
            # middle of an evidence object, making the entire batch unusable.
            return 10000
        if request_type == "curriculum_internalization":
            return 6000
        if "report" in request_type:
            return 900
        return 600


class ModelNotConfiguredError(Exception):
    pass


class ModelResponseError(Exception):
    def __init__(self, code: str, fallback_eligible: bool = False, *, response_received: bool = False,
                 prompt_tokens: int | None = None, completion_tokens: int | None = None,
                 latency_ms: int | None = None):
        super().__init__(code)
        self.code = code
        self.fallback_eligible = fallback_eligible
        self.response_received = response_received
        # Keep only provider accounting, never failed content or reasoning.
        self.prompt_tokens = prompt_tokens if type(prompt_tokens) is int and prompt_tokens >= 0 else None
        self.completion_tokens = completion_tokens if type(completion_tokens) is int and completion_tokens >= 0 else None
        self.latency_ms = latency_ms if type(latency_ms) is int and latency_ms >= 0 else None


def build_output_envelope(
    request: ModelRequest,
    transport_result: ModelResult,
    *,
    result: dict[str, Any],
    reason_summary: str,
    confidence: float,
    verification_status: str,
    safety_status: str,
) -> dict[str, Any]:
    if verification_status not in VERIFICATION_STATUSES:
        raise ValueError(f"invalid verification status: {verification_status}")
    if safety_status not in SAFETY_STATUSES:
        raise ValueError(f"invalid safety status: {safety_status}")
    if not 0 <= confidence <= 1:
        raise ValueError("model output confidence must be between 0 and 1")
    if not reason_summary.strip():
        raise ValueError("model output reason_summary is required")
    if verification_status == "rejected" and result:
        raise ValueError("rejected model output cannot contain a display result")
    return {
        "request_id": request.request_id,
        "output_type": request.output_type,
        "result": result,
        "reason_summary": reason_summary.strip(),
        "confidence": round(float(confidence), 3),
        "evidence_refs": list(request.evidence_refs),
        "model_provider": transport_result.provider,
        "model_version": transport_result.model,
        "prompt_version": request.prompt_version,
        "knowledge_version": request.knowledge_version,
        "policy_version": request.policy_version,
        "verification_status": verification_status,
        "safety_status": safety_status,
        "fallback_used": bool(transport_result.fallback_used),
    }
