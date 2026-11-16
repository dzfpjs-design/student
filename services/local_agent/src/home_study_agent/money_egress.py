"""Private process money owner. No renderer authority, provisioning or refunds.

The fixed Windows slot is independent of every family/profile/restore root.
An existing but incomplete slot is HOLD, never an invitation to normal mode.
No end/reset operation is implemented: a future Root-owned verified terminal
workflow is required to leave an activated authorization.
"""
from contextlib import asynccontextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import threading
import time
import uuid
import httpx
from urllib.parse import urlsplit

from .money_r4.money_budget import (MoneyBudget, BudgetHold, DispatchClaim, REGION, ASR_MODEL,
    OMNI_MODEL, TARIFF_SHA256, TARIFF, _exact_object, _quote_owned_text, _text_contract, _quote_cosyvoice, _quote_qwen_voice_clone, canonical)
from .money_r4.money_files import OwnerFiles, OwnerFileError

SLOT = r'C:\HSC\test-cost-control\joint8h-50cny-20261020'
DOMAIN = 'joint8h_money_request_v1'
RECEIPT_DOMAIN = 'joint8h_money_receipt_v1'
FIELDS = ('operation', 'request_id', 'attempt_id', 'step', 'session_id', 'child_id', 'agent_turn_id', 'connection_id', 'storage_generation', 'endpoint_origin', 'model', 'payload_sha256', 'claim_nonce', 'issued_at_ms')
_owner = None
# Monotonic process mode. A normal boot cannot adopt later provisioning, even
# after an owner is closed/replaced. Only a new process may start ACTIVE.
_slot_lock = threading.Lock()
_slot_mode = 'UNSEEN'
_slot_hold_code = 'MONEY_ACTIVE_RESTART_REQUIRED'
_process_latch = None

def _need(value, code):
    if not value: raise BudgetHold(code)

def _json(value): return json.dumps(value, ensure_ascii=False, separators=(',', ':'))
def _hex(value): return type(value) is str and re.fullmatch('[a-f0-9]{64}', value) is not None

def _reject_json_constant(value):
    raise BudgetHold('MONEY_TEXT_JSON_UNCONFIRMED')

def _text_http_quote(endpoint, request):
    """Interpret the exact prepared HTTP body, never a token estimate or amount."""
    spec = _text_contract(endpoint.model)
    _need(endpoint.provider == spec['provider'] and endpoint.base_url == spec['endpoint'], 'MONEY_UNKNOWN_PRICING')
    _need(str(request.url) == endpoint.base_url + '/chat/completions' and request.method == 'POST', 'MONEY_UNKNOWN_PRICING')
    _need(type(request.content) is bytes and len(request.content) <= 8 * 1024 * 1024, 'MONEY_TEXT_PAYLOAD_UNCONFIRMED')
    try:
        value = json.loads(request.content, object_pairs_hook=_exact_object, parse_constant=_reject_json_constant)
    except (ValueError, UnicodeError): raise BudgetHold('MONEY_TEXT_JSON_UNCONFIRMED') from None
    required = {'model', 'messages', 'max_tokens', 'stream'} | ({'enable_thinking'} if spec['provider'] == 'qwen' else {'thinking'})
    optional = {'temperature', 'response_format'} | ({'reasoning_effort'} if spec['provider'] == 'deepseek' else set())
    _need(type(value) is dict and required <= set(value) <= required | optional, 'MONEY_UNKNOWN_PRICING')
    _need(value['model'] == endpoint.model and value['stream'] is False, 'MONEY_UNKNOWN_PRICING')
    if spec['provider'] == 'qwen':
        _need(value['enable_thinking'] is False, 'MONEY_UNKNOWN_PRICING')
    else:
        _need(value['thinking'] in ({'type': 'enabled'}, {'type': 'disabled'}), 'MONEY_UNKNOWN_PRICING')
        if value['thinking'] == {'type': 'enabled'}:
            _need(value.get('reasoning_effort') == 'high' and 'temperature' not in value, 'MONEY_UNKNOWN_PRICING')
        else: _need('reasoning_effort' not in value, 'MONEY_UNKNOWN_PRICING')
    messages = value['messages']
    _need(type(messages) is list and len(messages) > 0, 'MONEY_UNKNOWN_PRICING')
    for message in messages:
        _need(type(message) is dict and set(message) == {'role', 'content'} and message['role'] in {'system', 'user', 'assistant'}
              and type(message['content']) is str and bool(message['content'].strip()), 'MONEY_UNKNOWN_PRICING')
    if 'temperature' in value:
        _need(type(value['temperature']) in (int, float) and 0 <= value['temperature'] <= 2, 'MONEY_UNKNOWN_PRICING')
    if 'response_format' in value:
        _need(value['response_format'] == {'type': 'json_object'}, 'MONEY_UNKNOWN_PRICING')
    return _quote_owned_text(value['model'], value['max_tokens'], hashlib.sha256(request.content).hexdigest())

def _text_http_usage(response, quote):
    _need(len(response.content) <= 8 * 1024 * 1024, 'MONEY_TEXT_USAGE_UNCONFIRMED')
    try:
        value = response.json(object_pairs_hook=_exact_object, parse_constant=_reject_json_constant)
    except (ValueError, UnicodeError): raise BudgetHold('MONEY_TEXT_USAGE_UNCONFIRMED') from None
    basis = json.loads(quote.basis)
    _need(type(value) is dict and value.get('model') == basis['model'], 'MONEY_TEXT_RESPONSE_MODEL_UNCONFIRMED')
    usage = value.get('usage')
    _need(type(usage) is dict and {'prompt_tokens', 'completion_tokens', 'total_tokens'} <= set(usage), 'MONEY_TEXT_USAGE_UNCONFIRMED')
    counts = tuple(usage[k] for k in ('prompt_tokens', 'completion_tokens', 'total_tokens'))
    _need(all(type(x) is int and x >= 0 for x in counts), 'MONEY_TEXT_USAGE_UNCONFIRMED')
    spec = _text_contract(basis['model'])
    _need(counts[0] <= basis['input_tokens_max'] and counts[1] <= basis['max_tokens'] and counts[2] <= spec['context_max'], 'MONEY_TEXT_USAGE_BOUND_EXCEEDED')
    _need(counts[2] == counts[0] + counts[1], 'MONEY_TEXT_USAGE_UNCONFIRMED')
    # Provider-defined cache counts are part of trustworthy complete usage.
    # We charge the conservative full-input rate; cache never discounts price.
    prompt_details = usage.get('prompt_tokens_details')
    if 'prompt_tokens_details' in usage:
        _need(type(prompt_details) is dict, 'MONEY_TEXT_USAGE_UNCONFIRMED')
        if 'cached_tokens' in prompt_details:
            cached = prompt_details['cached_tokens']
            _need(type(cached) is int and 0 <= cached <= counts[0], 'MONEY_TEXT_USAGE_UNCONFIRMED')
    if spec['provider'] == 'deepseek':
        _need({'prompt_cache_hit_tokens', 'prompt_cache_miss_tokens'} <= set(usage), 'MONEY_TEXT_USAGE_UNCONFIRMED')
        hit, miss = usage['prompt_cache_hit_tokens'], usage['prompt_cache_miss_tokens']
        _need(type(hit) is int and type(miss) is int and hit >= 0 and miss >= 0
              and hit + miss == counts[0], 'MONEY_TEXT_USAGE_UNCONFIRMED')
        if prompt_details is not None and 'cached_tokens' in prompt_details:
            _need(prompt_details['cached_tokens'] == hit, 'MONEY_TEXT_USAGE_UNCONFIRMED')
    details = usage.get('completion_tokens_details')
    if details is not None:
        _need(type(details) is dict, 'MONEY_TEXT_USAGE_UNCONFIRMED')
        reasoning = details.get('reasoning_tokens', 0)
        _need(type(reasoning) is int and 0 <= reasoning <= counts[1]
              and (spec['provider'] == 'deepseek' or reasoning == 0), 'MONEY_TEXT_THINKING_UNCONFIRMED')
    return (*counts, value['model'])

def _cosyvoice_quote(settings, text, url, task_id, packets):
    _need(settings.tts_provider == 'aliyun_cosyvoice' and settings.tts_model == 'cosyvoice-v3-flash'
          and settings.tts_region == 'cn-beijing', 'MONEY_UNKNOWN_PRICING')
    workspace = settings.tts_workspace_id
    _need(type(workspace) is str and re.fullmatch('[A-Za-z0-9_-]{1,128}', workspace)
          and url == f'wss://{workspace}.cn-beijing.maas.aliyuncs.com/api-ws/v1/inference', 'MONEY_UNKNOWN_PRICING')
    _need(type(text) is str and type(task_id) is str and re.fullmatch('[a-f0-9-]{36}', task_id), 'MONEY_TTS_PAYLOAD_UNCONFIRMED')
    _need(settings.tts_voice_id in {'longyingtao_v3', 'longanwen_v3', 'longxiaochun_v3', 'longyingmu_v3', 'longanrou_v3'}, 'MONEY_UNKNOWN_PRICING')
    _need(type(packets) is tuple and len(packets) == 3 and all(type(x) is str and len(x) <= 32768 for x in packets), 'MONEY_TTS_PAYLOAD_UNCONFIRMED')
    def header(action): return dict(action=action, task_id=task_id, streaming='duplex')
    expected = (
        {'header': header('run-task'), 'payload': {'task_group': 'audio', 'task': 'tts', 'function': 'SpeechSynthesizer',
            'model': settings.tts_model, 'parameters': {'text_type': 'PlainText', 'voice': settings.tts_voice_id,
            'format': 'pcm', 'sample_rate': 24000, 'volume': 50, 'rate': 1.0, 'pitch': 1.0, 'enable_ssml': False}, 'input': {}}},
        {'header': header('continue-task'), 'payload': {'input': {'text': text}}},
        {'header': header('finish-task'), 'payload': {'input': {}}},
    )
    try:
        decoded = tuple(json.loads(x, object_pairs_hook=_exact_object, parse_constant=_reject_json_constant) for x in packets)
        _need(canonical(decoded) == canonical(expected), 'MONEY_TTS_PAYLOAD_UNCONFIRMED')
        return _quote_cosyvoice(text.encode('utf-8'), hashlib.sha256(_json(packets).encode()).hexdigest(), hashlib.sha256(url.encode()).hexdigest())
    except (ValueError, UnicodeError): raise BudgetHold('MONEY_TTS_PAYLOAD_UNCONFIRMED') from None

QWEN_CLONE_SYNTHESIS_URL = 'https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation'

def _qwen_voice_clone_http_quote(settings, text, request):
    _need(settings.tts_provider == 'qwen_voice_clone' and settings.tts_model == 'qwen3-tts-vc-2026-01-22'
          and settings.tts_region == 'cn-beijing', 'MONEY_UNKNOWN_PRICING')
    _need(type(settings.tts_voice_id) is str and re.fullmatch('[A-Za-z0-9._:-]{2,120}', settings.tts_voice_id), 'MONEY_UNKNOWN_PRICING')
    _need(type(settings.tts_api_key) is str and bool(settings.tts_api_key), 'MONEY_HTTP_ROUTE_UNCONFIRMED')
    _need(request.method == 'POST' and str(request.url) == QWEN_CLONE_SYNTHESIS_URL, 'MONEY_UNKNOWN_PRICING')
    _need(type(request.content) is bytes and len(request.content) <= 32768 and type(text) is str, 'MONEY_TTS_PAYLOAD_UNCONFIRMED')
    _need(set(request.headers) <= {'host', 'accept', 'accept-encoding', 'connection', 'user-agent', 'authorization',
                                  'content-type', 'x-dashscope-sse', 'content-length'}, 'MONEY_HTTP_ROUTE_UNCONFIRMED')
    _need(request.headers.get('X-DashScope-SSE') == 'enable' and request.headers.get('Accept') == 'text/event-stream'
          and request.headers.get('Content-Type') == 'application/json'
          and request.headers.get('Authorization') == 'Bearer ' + settings.tts_api_key, 'MONEY_HTTP_ROUTE_UNCONFIRMED')
    try:
        value = json.loads(request.content, object_pairs_hook=_exact_object, parse_constant=_reject_json_constant)
        expected = {'model': 'qwen3-tts-vc-2026-01-22', 'input': {'text': text, 'voice': settings.tts_voice_id, 'language_type': 'Chinese'}}
        _need(canonical(value) == canonical(expected), 'MONEY_TTS_PAYLOAD_UNCONFIRMED')
        return _quote_qwen_voice_clone(text.encode('utf-8'), hashlib.sha256(request.content).hexdigest(),
                                      hashlib.sha256(str(request.url).encode('ascii')).hexdigest())
    except (ValueError, UnicodeError, TypeError):
        raise BudgetHold('MONEY_TTS_PAYLOAD_UNCONFIRMED') from None

class _OriginalTtsHttpStream(httpx.AsyncByteStream):
    """Observe the exact original close once, independently of Response flags."""
    def __init__(self, original):
        self.original = original
        self.close_started = self.close_confirmed = False
        self.close_error = None
        self.received_bytes = 0
    async def __aiter__(self):
        async for chunk in self.original:
            _need(type(chunk) is bytes, 'MONEY_TTS_HTTP_RESPONSE_BOUND')
            self.received_bytes += len(chunk)
            _need(self.received_bytes <= 64 * 1024 * 1024, 'MONEY_TTS_HTTP_RESPONSE_BOUND')
            yield chunk
    async def aclose(self):
        if self.close_started:
            _need(self.close_confirmed, 'MONEY_TTS_HTTP_CLOSE_UNCONFIRMED')
            return
        self.close_started = True
        try: await self.original.aclose()
        except BaseException as error:
            self.close_error = error
            raise BudgetHold('MONEY_TTS_HTTP_CLOSE_UNCONFIRMED') from None
        self.close_confirmed = True

class _QwenTtsHttpResources:
    """Private RAM only: preallocated before a claim or physical send.

    An HTTPX exception may retain a response/stream in its original traceback
    even when send never returned it. Keep that exact exception and traceback;
    neither a missing return value nor Response.is_closed is a close ACK.
    """
    __slots__ = ('request', 'client', 'observed', 'cap', 'response', 'original_stream',
                 'send_started', 'close_owned', 'error', 'traceback',
                 'close_error', 'close_traceback')
    def __init__(self, request, client, observed):
        self.request, self.client, self.observed = request, client, observed
        self.cap = self.response = self.original_stream = None
        self.send_started = self.close_owned = False
        self.error = self.traceback = self.close_error = self.close_traceback = None

class _CosyvoiceDispatch:
    def __init__(self, owner, packets):
        self.owner, self.packets = owner, packets
        self.socket = None
        self.ordinal, self.started_seen, self.finished_seen, self.closed_seen = 0, False, False, False

    async def send(self, socket, packet):
        if self.owner is not None:
            self.owner.check()
            _need(self.ordinal < 3 and packet == self.packets[self.ordinal]
                  and (self.ordinal == 0 or self.started_seen)
                  and (self.socket is None or self.socket is socket), 'MONEY_TTS_SEQUENCE_UNCONFIRMED')
            self.socket = socket
        await socket.send(packet)
        self.ordinal += 1

    def task_started(self):
        if self.owner is not None:
            _need(self.ordinal == 1 and not self.started_seen, 'MONEY_TTS_SEQUENCE_UNCONFIRMED')
        self.started_seen = True

    def task_finished(self):
        if self.owner is not None:
            _need(self.ordinal == 3 and self.started_seen and not self.finished_seen, 'MONEY_TTS_SEQUENCE_UNCONFIRMED')
        self.finished_seen = True

    def socket_closed(self, socket):
        if self.owner is not None:
            from websockets.protocol import State
            _need(socket is self.socket and socket.state is State.CLOSED and type(socket.close_code) is int
                  and socket.close_code in (1000, 1001), 'MONEY_TTS_SOCKET_CLOSE_UNCONFIRMED')
        self.closed_seen = True
def _key(seed):
    _need(_hex(seed), 'MONEY_PRIVATE_AUTHORITY_UNAVAILABLE')
    return hmac.new(bytes.fromhex(seed), b'joint8h_money_key_v1', hashlib.sha256).digest()

def _poison_slot(code):
    global _slot_mode, _slot_hold_code
    with _slot_lock:
        if _slot_mode != 'HOLD': _slot_hold_code = code
        _slot_mode = 'HOLD'

def _bind_process_latch(raw):
    global _process_latch, _slot_mode, _slot_hold_code
    with _slot_lock:
        _need(_slot_mode == 'ACTIVE', _slot_hold_code)
        if _process_latch is None: _process_latch = raw
        elif _process_latch != raw:
            _slot_mode, _slot_hold_code = 'HOLD', 'MONEY_ACTIVE_LATCH_CHANGED'
            raise BudgetHold(_slot_hold_code)

def _require_process_mode(active):
    with _slot_lock:
        _need(_slot_mode != 'HOLD', _slot_hold_code)
        _need(_slot_mode == ('ACTIVE' if active else 'NORMAL'), 'MONEY_ACTIVE_RESTART_REQUIRED')

def slot_present(slot=None):
    global _slot_mode, _slot_hold_code
    _need(_slot_lock.acquire(False), 'LEDGER_BUSY')
    try:
        _need(_slot_mode != 'HOLD', _slot_hold_code)
        # Never interpret a Windows spelling as a relative POSIX directory.
        try:
            if slot is None and os.name != 'nt': raise FileNotFoundError
            path = Path(SLOT) if slot is None else Path(slot)
            path.lstat()
        except FileNotFoundError:
            if _slot_mode == 'ACTIVE':
                _slot_mode, _slot_hold_code = 'HOLD', 'MONEY_ACTIVE_LATCH_MISSING'
                raise BudgetHold(_slot_hold_code) from None
            if _slot_mode == 'UNSEEN': _slot_mode = 'NORMAL'
            return False
        except OSError:
            _slot_mode, _slot_hold_code = 'HOLD', 'MONEY_SLOT_UNCONFIRMED'
            raise BudgetHold(_slot_hold_code) from None
        if _slot_mode == 'NORMAL':
            _slot_mode, _slot_hold_code = 'HOLD', 'MONEY_ACTIVE_RESTART_REQUIRED'
            raise BudgetHold(_slot_hold_code)
        if _slot_mode == 'UNSEEN': _slot_mode = 'ACTIVE'
        return True
    finally: _slot_lock.release()

class MoneyOwner:
    def __init__(self, seed, *, slot=None):
        self.slot = Path(SLOT) if slot is None else Path(slot)
        self.native_slot = slot
        self.active = slot_present(slot)
        self.closed = False
        self.budget = self.latch = None
        self._latch_lock = threading.Lock()
        self._bridge_lock = threading.Lock()
        self._requests, self._pairs, self._claims = set(), {}, {}
        self._text_inflight = set()
        self._tts_inflight = set()
        self._tts_http_retained = []
        self.key = None
        try:
            self.key = _key(seed) if self.active else None
            if self.active:
                # Both use existing-only, held/noalias owner files; no repair.
                self.latch = OwnerFiles(self.slot / 'active-latch.json')
                self.raw_latch = self.latch.read_policy()
                value = json.loads(self.raw_latch, object_pairs_hook=_exact_object)
                _need(type(value) is dict and set(value) == {'schema', 'state', 'policy_sha256', 'run_id', 'authorization_id', 'ledger_id', 'tariff_sha256', 'asr_endpoint_base', 'omni_endpoint_origin'}, 'MONEY_ACTIVE_LATCH_FIELDS')
                _need(type(value['schema']) is int and value['schema'] == 2 and value['state'] == 'ACTIVE', 'MONEY_ACTIVE_LATCH_UNCONFIRMED')
                self.binding = value
                self.budget = MoneyBudget(self.slot / 'owner-policy.json')
                _need(value['policy_sha256'] == self.budget._policy_sha and value['tariff_sha256'] == TARIFF_SHA256 and all(value[k] == self.budget._binding[k] for k in ('run_id', 'authorization_id', 'ledger_id')), 'MONEY_ACTIVE_LATCH_BINDING')
                _need(value['asr_endpoint_base'] == 'https://dashscope.aliyuncs.com/compatible-mode/v1', 'MONEY_UNKNOWN_PRICING')
                endpoint = urlsplit(value['omni_endpoint_origin'])
                _need(endpoint.scheme == 'https' and re.fullmatch(r'[A-Za-z0-9_-]+\.cn-beijing\.maas\.aliyuncs\.com', endpoint.netloc or '') and not endpoint.path and not endpoint.query and not endpoint.fragment, 'MONEY_UNKNOWN_PRICING')
                _bind_process_latch(self.raw_latch)
        except BaseException:
            _poison_slot('MONEY_ACTIVE_LATCH_UNCONFIRMED')
            self.close()
            raise

    def check(self):
        _need(not self.closed, 'MONEY_OWNER_CLOSED')
        if self.active and self.budget is not None and self.budget._poisoned:
            _poison_slot('MONEY_TEXT_LEDGER_UNCONFIRMED')
        _require_process_mode(self.active)
        if not self.active:
            _need(not slot_present(self.native_slot), 'MONEY_ACTIVE_RESTART_REQUIRED')
            return False
        _need(self._latch_lock.acquire(False), 'LEDGER_BUSY')
        try:
            _need(self.latch.read_policy() == self.raw_latch, 'MONEY_ACTIVE_LATCH_CHANGED')
        except (OwnerFileError, BudgetHold) as error:
            _poison_slot(error.code if isinstance(error, BudgetHold) else 'MONEY_ACTIVE_LATCH_UNCONFIRMED')
            raise BudgetHold(error.code if isinstance(error, BudgetHold) else str(error)) from None
        finally: self._latch_lock.release()
        return True

    def exclude_family_roots(self, settings):
        if self.check():
            slot = self.slot.resolve()
            for root in (Path(settings.data_dir), Path(settings.database_path).parent):
                root = root.resolve()
                _need(root != slot and root not in slot.parents and slot not in root.parents, 'MONEY_SLOT_PROFILE_OVERLAP')

    def close(self):
        # Never delete ledger/latch or clear liability; failure stays poisoned.
        if self.budget is not None and self.budget._poisoned and not self.budget._closed:
            _poison_slot('MONEY_TEXT_LEDGER_UNCONFIRMED')
        if self._text_inflight or self._tts_inflight:
            _poison_slot('MONEY_OWNER_CLOSE_UNCONFIRMED')
            raise BudgetHold('MONEY_OWNER_CLOSE_UNCONFIRMED')
        self.closed = True
        errors = []
        for value in (self.budget, self.latch):
            if value is not None:
                try: value.close()
                except BaseException as error: errors.append(error)
        if errors:
            _poison_slot('MONEY_OWNER_CLOSE_UNCONFIRMED')
            raise BudgetHold('MONEY_OWNER_CLOSE_UNCONFIRMED')

    def deny(self, operation):
        if self.check(): raise BudgetHold('MONEY_UNKNOWN_PRICING')

    async def _send_model_http(self, client, endpoint, payload, *, timeout_seconds=None):
        timeout_options = _model_http_timeout_options(timeout_seconds)
        _need(self.check(), 'MONEY_NOT_ACTIVE')
        _need(not client.follow_redirects and not any(client.event_hooks.values()), 'MONEY_HTTP_ROUTE_UNCONFIRMED')
        spec = _text_contract(endpoint.model)
        _need(endpoint.provider == spec['provider'] and endpoint.base_url == spec['endpoint'], 'MONEY_UNKNOWN_PRICING')
        try:
            request = client.build_request('POST', f'{endpoint.base_url}/chat/completions',
                headers={'Authorization': f'Bearer {endpoint.api_key}', 'Content-Type': 'application/json'}, json=payload, **timeout_options)
        except (TypeError, ValueError, UnicodeError):
            raise BudgetHold('MONEY_TEXT_PAYLOAD_UNCONFIRMED') from None
        quote = _text_http_quote(endpoint, request)
        try:
            cap = self.budget._claim_owned_text(quote)
        except BaseException:
            if self.budget._poisoned: _poison_slot('MONEY_TEXT_LEDGER_UNCONFIRMED')
            raise
        self._text_inflight.add(id(cap))
        try:
            self.check()
            try:
                response = await client.send(request, follow_redirects=False)
            except BaseException:
                self.budget._unknown_owned_text(cap)
                raise
            try:
                _need(response.request is request and response.is_closed and response.is_stream_consumed
                      and not response.history and _text_http_quote(endpoint, request) == quote,
                      'MONEY_HTTP_ROUTE_UNCONFIRMED')
                self.check()
                if not 200 <= response.status_code < 300:
                    self.budget._unknown_owned_text(cap)
                    return response
                usage = _text_http_usage(response, quote)
            except BudgetHold as error:
                poison = error.code in {'MONEY_TEXT_RESPONSE_MODEL_UNCONFIRMED', 'MONEY_TEXT_USAGE_BOUND_EXCEEDED',
                                        'MONEY_TEXT_THINKING_UNCONFIRMED', 'MONEY_HTTP_ROUTE_UNCONFIRMED'}
                self.budget._unknown_owned_text(cap, poison=poison)
                raise
            self.budget._settle_owned_text(cap, usage)
            return response
        finally:
            self._text_inflight.discard(id(cap))
            try:
                if self.budget._text_caps.get(id(cap)) is cap:
                    self.budget._unknown_owned_text(cap)
            finally:
                if self.budget._poisoned: _poison_slot('MONEY_TEXT_LEDGER_UNCONFIRMED')

    def asr(self, wav, settings, send, until):
        if not self.check(): return send(wav)
        _need(settings.cloud_asr_provider == 'dashscope' and settings.cloud_asr_model == ASR_MODEL and settings.cloud_asr_base_url == self.binding['asr_endpoint_base'], 'MONEY_UNKNOWN_PRICING')
        attempt = 'asr-' + uuid.uuid4().hex
        def egress(actual):
            self.check()
            _need(time.monotonic() < until and actual is wav, 'MONEY_EGRESS_DEADLINE')
            return send(actual)
        return self.budget.dispatch_asr_wav(attempt, wav, egress)

    def execute(self, value):
        _need(self.check() and self.key is not None, 'MONEY_NOT_ACTIVE')
        _need(type(value) is dict and set(value) == set(FIELDS) | {'signature'}, 'MONEY_PROOF_INVALID')
        op = value['operation']
        _need(op in {'pair.reserve', 'pair.claim', 'pair.closed', 'pair.unknown'}, 'MONEY_UNKNOWN_PRICING')
        for k in ('request_id', 'attempt_id', 'session_id', 'child_id', 'agent_turn_id', 'connection_id', 'storage_generation'):
            _need(type(value[k]) is str and re.fullmatch('[A-Za-z0-9_.:-]{1,128}', value[k]), 'MONEY_PROOF_INVALID')
        _need(type(value['step']) is int and value['step'] in (0, 1) and _hex(value['payload_sha256']), 'MONEY_PROOF_INVALID')
        _need(type(value['claim_nonce']) is str and (value['claim_nonce'] == '' or re.fullmatch('[a-f0-9]{32}', value['claim_nonce'])), 'MONEY_PROOF_INVALID')
        issued = value['issued_at_ms']
        _need(type(issued) is int and -5000 <= int(time.time() * 1000) - issued <= 30000, 'MONEY_PROOF_EXPIRED')
        expected = hmac.new(self.key, _json([DOMAIN] + [value[k] for k in FIELDS]).encode(), hashlib.sha256).hexdigest()
        _need(_hex(value['signature']) and hmac.compare_digest(value['signature'], expected), 'MONEY_PROOF_INVALID')
        _need(value['endpoint_origin'] == self.binding['omni_endpoint_origin'] and value['model'] == OMNI_MODEL, 'MONEY_UNKNOWN_PRICING')
        _need(self._bridge_lock.acquire(False), 'LEDGER_BUSY')
        try:
            _need(value['request_id'] not in self._requests and len(self._requests) < 2048, 'MONEY_REQUEST_ALREADY_USED')
            self._requests.add(value['request_id'])
            attempt, step = value['attempt_id'], value['step']
            identity = tuple(value[k] for k in ('session_id', 'child_id', 'agent_turn_id', 'connection_id', 'storage_generation', 'endpoint_origin', 'model', 'payload_sha256'))
            nonce = ''
            if op == 'pair.reserve':
                _need(step == 0 and value['claim_nonce'] == '' and attempt not in self._pairs, 'MONEY_REQUEST_ALREADY_USED')
                reserved = self.budget.reserve_public_speech_pair(attempt)
                # An old idempotent view is NEVER a fresh permission.
                _need(reserved.created is True, 'MONEY_ATTEMPT_ALREADY_EXISTS')
                self._pairs[attempt] = identity
                state = 'reserved'
            else:
                _need(self._pairs.get(attempt) == identity, 'MONEY_ATTEMPT_SCOPE_CHANGED')
                if op == 'pair.claim':
                    _need(value['claim_nonce'] == '', 'MONEY_PROOF_INVALID')
                    claim = self.budget.claim_dispatch(attempt, step)
                    self._claims[(attempt, step)] = claim
                    nonce, state = claim.nonce, 'claimed'
                else:
                    claim = self._claims.get((attempt, step))
                    _need(claim is not None and claim.nonce == value['claim_nonce'], 'MONEY_CLAIM_UNCONFIRMED')
                    (self.budget.mark_closed if op == 'pair.closed' else self.budget.mark_unknown)(claim)
                    del self._claims[(attempt, step)]
                    state = 'closed' if op == 'pair.closed' else 'unknown'
            self.check()
            receipt = {'request_id': value['request_id'], 'operation': op, 'attempt_id': attempt, 'step': step, 'state': state, 'claim_nonce': nonce}
            receipt['signature'] = hmac.new(self.key, _json([RECEIPT_DOMAIN] + [receipt[k] for k in ('request_id', 'operation', 'attempt_id', 'step', 'state', 'claim_nonce')]).encode(), hashlib.sha256).hexdigest()
            return receipt
        finally: self._bridge_lock.release()

def current_owner():
    if _owner is None:
        if slot_present():
            _poison_slot('MONEY_OWNER_NOT_READY')
            raise BudgetHold('MONEY_OWNER_NOT_READY')
    else: _require_process_mode(_owner.active)
    return _owner

def deny_unpriced(operation):
    owner = current_owner()
    if owner is not None: owner.deny(operation)

def _model_http_timeout_options(timeout_seconds):
    # One reviewed per-request override; None preserves the client's default.
    _need(timeout_seconds is None or type(timeout_seconds) is int and timeout_seconds == 35,
          'MONEY_HTTP_ROUTE_UNCONFIRMED')
    return {} if timeout_seconds is None else {'timeout': 35}

async def send_model_http(client, endpoint, payload, *, timeout_seconds=None):
    """Sole physical Gateway seam. No caller amount, usage or settlement route."""
    timeout_options = _model_http_timeout_options(timeout_seconds)
    owner = current_owner()
    if owner is None or not owner.check():
        return await client.post(f'{endpoint.base_url}/chat/completions',
            headers={'Authorization': f'Bearer {endpoint.api_key}', 'Content-Type': 'application/json'}, json=payload, **timeout_options)
    return await owner._send_model_http(client, endpoint, payload, timeout_seconds=timeout_seconds)

@asynccontextmanager
async def qwen_voice_clone_egress(settings, text, client, url, headers, payload):
    """One priced prepared POST; no voice enrollment, retries or refunds."""
    owner = current_owner()
    if owner is None or not owner.check():
        async with client.stream('POST', url, headers=headers, json=payload) as response:
            yield response
        return
    _need(client.auth is None and not client.follow_redirects and not any(client.event_hooks.values()), 'MONEY_HTTP_ROUTE_UNCONFIRMED')
    try: request = client.build_request('POST', url, headers=headers, json=payload)
    except (ValueError, UnicodeError, TypeError): raise BudgetHold('MONEY_TTS_PAYLOAD_UNCONFIRMED') from None
    quote = _qwen_voice_clone_http_quote(settings, text, request)
    # Allocation failure here has no claim and no provider resource to lose.
    observed = _OriginalTtsHttpStream(None)
    resources = _QwenTtsHttpResources(request, client, observed)
    owner._tts_http_retained.append(resources)
    try: cap = owner.budget._claim_owned_tts(quote)
    except BaseException:
        owner._tts_http_retained.remove(resources)
        if owner.budget._poisoned: _poison_slot('MONEY_TTS_LEDGER_UNCONFIRMED')
        raise
    resources.cap = cap
    failed = True
    close_confirmed = True  # Only the pre-send phase is known to own no response.
    try:
        owner._tts_inflight.add(id(cap))
        owner.check()
        _need(_qwen_voice_clone_http_quote(settings, text, request) == quote
              and client.auth is None and not client.follow_redirects and not any(client.event_hooks.values()), 'MONEY_HTTP_ROUTE_UNCONFIRMED')
        resources.send_started = True
        close_confirmed = False
        response = await client.send(request, stream=True, follow_redirects=False, auth=None)
        # Bind the exact originals before any subsequent validation can fail.
        resources.response = response
        resources.original_stream = response.stream
        observed.original = resources.original_stream
        _need(not response.is_closed and not response.is_stream_consumed, 'MONEY_HTTP_ROUTE_UNCONFIRMED')
        resources.close_owned = True
        response.stream = observed
        _need(response.request is request and not response.history, 'MONEY_HTTP_ROUTE_UNCONFIRMED')
        yield response
        owner.check()
        _need(response.stream is observed and response.request is request and not response.history
              and response.is_stream_consumed and 200 <= response.status_code < 300
              and _qwen_voice_clone_http_quote(settings, text, request) == quote, 'MONEY_TTS_COMPLETION_UNCONFIRMED')
        failed = False
    except BaseException as error:
        resources.error, resources.traceback = error, error.__traceback__
        raise
    finally:
        try:
            if resources.close_owned:
                await observed.aclose()
                _need(resources.response.stream is observed, 'MONEY_TTS_HTTP_CLOSE_UNCONFIRMED')
                await resources.response.aclose()
                close_confirmed = observed.close_confirmed and resources.response.is_closed and resources.response.stream is observed
                _need(close_confirmed, 'MONEY_TTS_HTTP_CLOSE_UNCONFIRMED')
        except BaseException as error:
            close_confirmed = False
            resources.close_error, resources.close_traceback = error, error.__traceback__
            _poison_slot('MONEY_TTS_HTTP_CLOSE_UNCONFIRMED')
            raise
        finally:
            if not close_confirmed:
                _poison_slot('MONEY_TTS_HTTP_CLOSE_UNCONFIRMED')
            try:
                (owner.budget.mark_unknown if failed or not close_confirmed else owner.budget.mark_closed)(cap)
                if close_confirmed:
                    owner._tts_inflight.discard(id(cap))
                    owner._tts_http_retained.remove(resources)
            finally:
                if owner.budget._poisoned: _poison_slot('MONEY_TTS_LEDGER_UNCONFIRMED')


@asynccontextmanager
async def cosyvoice_egress(settings, text, url, task_id, packets):
    """Original generator holds the private claim; no usage settlement/refund."""
    owner = current_owner()
    active = owner is not None and owner.check()
    guard = _CosyvoiceDispatch(owner if active else None, packets)
    cap = None
    if active:
        quote = _cosyvoice_quote(settings, text, url, task_id, packets)
        try: cap = owner.budget._claim_owned_tts(quote)
        except BaseException:
            if owner.budget._poisoned: _poison_slot('MONEY_TEXT_LEDGER_UNCONFIRMED')
            raise
        owner._tts_inflight.add(id(cap))
    failed = True
    try:
        yield guard
        if active:
            owner.check()
            _need(guard.ordinal == 3 and guard.started_seen and guard.finished_seen and guard.closed_seen,
                  'MONEY_TTS_COMPLETION_UNCONFIRMED')
        failed = False
    finally:
        if cap is not None:
            try:
                (owner.budget.mark_unknown if failed else owner.budget.mark_closed)(cap)
            finally:
                owner._tts_inflight.discard(id(cap))
                if owner.budget._poisoned: _poison_slot('MONEY_TEXT_LEDGER_UNCONFIRMED')

def money_active():
    owner = current_owner()
    return owner.check() if owner is not None else False

def dispatch_asr(wav, settings, send, until):
    owner = current_owner()
    return owner.asr(wav, settings, send, until) if owner is not None else send(wav)

def open_process_owner(seed):
    global _owner
    _need(_owner is None or _owner.closed, 'MONEY_DUPLICATE_PROCESS_OWNER')
    _owner = MoneyOwner(seed)
    return _owner

def attach_money_owner(application, owner, settings):
    """Wrap either normal OR recovery-only app's original lifespan/close order."""
    from fastapi import APIRouter, Request, HTTPException
    from fastapi.responses import JSONResponse
    from starlette.concurrency import run_in_threadpool
    router = APIRouter()
    @router.post('/v1/private/money-egress')
    async def execute(request: Request):
        try:
            _need(settings.session_token and request.headers.get('X-HSC-Session') == settings.session_token, 'MONEY_PROOF_INVALID')
            raw = bytearray()
            async for chunk in request.stream():
                _need(len(raw) + len(chunk) <= 4096, 'MONEY_PROOF_INVALID')
                raw.extend(chunk)
            value = json.loads(raw, object_pairs_hook=_exact_object)
            receipt = await run_in_threadpool(owner.execute, value)
            return JSONResponse(receipt, headers={'Cache-Control': 'no-store'})
        except (BudgetHold, OwnerFileError, ValueError, TypeError) as error:
            code = error.code if isinstance(error, BudgetHold) else 'MONEY_PROOF_INVALID'
            raise HTTPException(409, detail={'code': code}, headers={'Cache-Control': 'no-store'}) from None
    async def budget_error(request, error):
        return JSONResponse({'detail': {'code': error.code}}, status_code=409, headers={'Cache-Control': 'no-store'})
    application.add_exception_handler(BudgetHold, budget_error)
    # Exact private money routes must remain reachable in a cold restore app.
    # Keep every preexisting route and its order; only move our own newly
    # registered exact path before the restricted app's final catch-all.
    before_routes = tuple(application.router.routes)
    application.include_router(router)
    application.state.money_owner = owner
    from .money_voice_policy import attach_money_voice_policy
    attach_money_voice_policy(application, owner, settings)
    if getattr(application.state, 'recovery_only', False) is True:
        routes = application.router.routes
        _need(tuple(routes[:len(before_routes)]) == before_routes, 'MONEY_ROUTE_BINDING_UNCONFIRMED')
        added = tuple(routes[len(before_routes):])
        _need(added and all(getattr(route, 'path', None) == '/v1/private/money-egress'
            and getattr(route, 'methods', None) in ({'POST'}, {'GET'}) for route in added),
            'MONEY_ROUTE_BINDING_UNCONFIRMED')
        fallback = [i for i, route in enumerate(before_routes) if getattr(route, 'path', None) == '/{path:path}']
        _need(len(fallback) == 1, 'MONEY_ROUTE_BINDING_UNCONFIRMED')
        index = fallback[0]
        routes[:] = before_routes[:index] + added + before_routes[index:]
    original = application.router.lifespan_context
    @asynccontextmanager
    async def lifetime(app):
        try:
            async with original(app): yield
        finally: owner.close()
    application.router.lifespan_context = lifetime
    return application
