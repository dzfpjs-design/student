"""Versioned owner money ledger. No provider/network or provisioning API.

The service gets one owner-created policy at startup. Request APIs cannot select
a run, database, tariff, cap or money amount. Legacy reservations are permanent;
only the original owned nonstreaming text capability can settle proven usage.
"""
import hashlib
import threading
from contextlib import contextmanager
if __package__:
    from .money_files import OwnerDirectory, OwnerFiles, OwnerFileError
else:
    from money_files import OwnerDirectory, OwnerFiles, OwnerFileError
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import struct
import time
import uuid
import wave
from dataclasses import dataclass
from typing import Any, Callable, Dict, Tuple

CAP_MICRO_CNY = 50_000_000
REGION = "cn-beijing"
ASR_MODEL = "qwen3-asr-flash"
OMNI_MODEL = "qwen3.8-omni-flash-realtime"
MAX_WAV_BYTES = 64 * 1024 * 1024
MAX_POLICY_BYTES = 8192
WAV_SAMPLE_RATES = (8000, 16000, 24000, 32000, 44100, 48000)
ASR_MICRO_CNY_PER_SECOND = 220
INPUT_TOKENS_MAX = 196608
OUTPUT_TOKENS_MAX = 65536
RESPONSE_MICRO_CNY = INPUT_TOKENS_MAX * 6 + OUTPUT_TOKENS_MAX * 12
TARIFF = {
    "schema": 2, "region": REGION, "asr_model": ASR_MODEL,
    "asr_micro_cny_per_second": ASR_MICRO_CNY_PER_SECOND,
    "asr_minimum_seconds": 1, "asr_rounding": "ceil_whole_seconds",
    "omni_model": OMNI_MODEL, "input_tokens_max": INPUT_TOKENS_MAX,
    "output_tokens_max": OUTPUT_TOKENS_MAX,
    "max_input_micro_cny_per_token": 6, "max_output_micro_cny_per_token": 12,
    "per_response_micro_cny": RESPONSE_MICRO_CNY,
    "public_speech_responses": 2, "refunds": False,
    "automatic_rtc": "HOLD_NO_PROVEN_PRE_RESPONSE_BOUND",
    "streaming_asr": "HOLD_NO_FINITE_RECORDED_WAV_CONTRACT",
    "tts": {"provider": "aliyun_cosyvoice", "region": "cn-beijing", "model": "cosyvoice-v3-flash",
            "plain_text_utf8_byte_micro_cny_upper": 100, "codepoints_max": 1000,
            "refunds_or_usage_settlement": False},
    "qwen_voice_clone": {"provider": "qwen_voice_clone", "region": "cn-beijing",
            "model": "qwen3-tts-vc-2026-01-22", "plain_text_utf8_byte_micro_cny_upper": 100,
            "codepoints_max": 1000, "refunds_or_usage_settlement": False,
            "tariff_facts_sha256": "cb122a3333f2abc523da30ffbc6e73992938dd4982cc2ba47e34e1f2c1556da7"},
    "text": {"provider": "qwen", "endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1",
             "input_tokens_max": 991808, "context_max": 1000000, "answer_model_max": 131072,
             "models": {"qwen3.7-plus": [6, 24], "qwen3.8-flash": [1, 3]},
             "thinking": False, "stream": False,
             "settlement": "original_owned_complete_nonstreaming_usage_only",
             "tariff_facts_sha256": "b1a99a9fefcffda9ebe6bb8826400657e072fc962497cd18cdeb2b2b9d6eed2b",
             "deepseek": {"provider": "deepseek", "endpoint": "https://api.deepseek.com",
                 "input_tokens_max": 1048576, "context_max": 1048576, "answer_model_max": 393216,
                 "models": {"deepseek-v4-pro": [9, 27], "deepseek-v4-flash": [2, 8]},
                 "exact_response_model_only": True,
                 "primary_context_facts_sha256": "416f75c02017769b1455eefb8e4914b7d0ecc071346365f95e120b2776ab155b"}},
}


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


TARIFF_SHA256 = digest(canonical(TARIFF).encode("ascii"))


class BudgetHold(RuntimeError):
    """No permission to send. code is finite and contains no private input."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def strict_int(value: Any, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise BudgetHold("INVALID_INTEGER")
    return value


def ceil_ratio(numerator: int, denominator: int) -> int:
    strict_int(numerator, 0, (1 << 63) - 1)
    strict_int(denominator, 1, (1 << 63) - 1)
    return (numerator + denominator - 1) // denominator


def identifier(value: Any) -> str:
    if type(value) is not str or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value) is None:
        raise BudgetHold("INVALID_IDENTIFIER")
    return value


def _exact_object(pairs: Any) -> Dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise BudgetHold("DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def _policy(path: Path, raw: bytes) -> Tuple[Dict[str, Any], str]:
    if not path.is_absolute() or path.parent.resolve() != path.parent:
        raise BudgetHold("OWNER_POLICY_PATH")
    try:
        value = json.loads(raw, object_pairs_hook=_exact_object)
    except (ValueError, UnicodeError) as exc:
        raise BudgetHold("OWNER_POLICY_JSON") from exc
    fields = {"schema", "run_id", "authorization_id", "ledger_id", "ledger_file",
              "cap_micro_cny", "region", "asr_model", "omni_model", "tariff_sha256"}
    if type(value) is not dict or set(value) != fields:
        raise BudgetHold("OWNER_POLICY_FIELDS")
    strict_int(value["schema"], 2, 2)
    strict_int(value["cap_micro_cny"], CAP_MICRO_CNY, CAP_MICRO_CNY)
    for key in ("run_id", "authorization_id", "ledger_id"):
        identifier(value[key])
    if (value["ledger_file"] != "money-budget.sqlite3" or value["region"] != REGION
            or value["asr_model"] != ASR_MODEL or value["omni_model"] != OMNI_MODEL
            or value["tariff_sha256"] != TARIFF_SHA256):
        raise BudgetHold("OWNER_CONTRACT_MISMATCH")
    return value, digest(raw)


@dataclass(frozen=True)
class Quote:
    operation: str
    micro_cny: int
    steps: int
    basis: str


@dataclass(frozen=True)
class Reservation:
    attempt_id: str
    operation: str
    quote_micro_cny: int
    step_states: Tuple[str, ...]
    created: bool  # informational only: never a dispatch permission


@dataclass(frozen=True)
class DispatchClaim:
    attempt_id: str
    step: int
    nonce: str  # returned only by the successful one-time transition


@dataclass(frozen=True)
class Balance:
    cap_micro_cny: int
    committed_micro_cny: int  # every attempt once, including reserved/unknown/closed
    remaining_micro_cny: int
    attempts: int


@dataclass(frozen=True)
class _OwnedTextClaim:
    claim: DispatchClaim
    quote: Quote
    owner_instance: str


def _text_contract(model):
    if type(model) is not str:
        raise BudgetHold('MONEY_UNKNOWN_PRICING')
    for spec in (TARIFF['text'], TARIFF['text']['deepseek']):
        if model in spec['models']:
            return spec
    raise BudgetHold('MONEY_UNKNOWN_PRICING')


def _text_basis(quote: Quote) -> dict:
    try:
        b = json.loads(quote.basis, object_pairs_hook=_exact_object)
        expected = {"schema", "provider", "endpoint", "model", "region", "payload_sha256",
                    "max_tokens", "input_tokens_max", "tariff_sha256"}
        if type(b) is not dict or set(b) != expected:
            raise BudgetHold("TEXT_QUOTE_CONTRACT_MISMATCH")
        spec = _text_contract(b['model'])
        limit = strict_int(b["max_tokens"], 1, spec["answer_model_max"])
        rates = spec['models'][b['model']]
        if (type(b["schema"]) is not int or b["schema"] != 2 or b["provider"] != spec['provider']
                or b["endpoint"] != spec['endpoint'] or b["region"] != REGION
                or rates is None or type(b["input_tokens_max"]) is not int
                or b["input_tokens_max"] != spec['input_tokens_max']
                or b["tariff_sha256"] != TARIFF_SHA256
                or type(b["payload_sha256"]) is not str
                or re.fullmatch('[a-f0-9]{64}', b["payload_sha256"]) is None
                or quote.operation != 'text_http' or quote.steps != 1
                or quote.micro_cny != b['input_tokens_max'] * rates[0] + limit * rates[1]
                or quote.basis != canonical(b)):
            raise BudgetHold("TEXT_QUOTE_CONTRACT_MISMATCH")
        return b
    except (ValueError, TypeError, KeyError):
        raise BudgetHold("TEXT_QUOTE_CONTRACT_MISMATCH") from None


def _quote_owned_text(model: str, max_tokens: int, payload_sha256: str) -> Quote:
    spec = _text_contract(model)
    rates = spec['models'][model]
    limit = strict_int(max_tokens, 1, spec['answer_model_max'])
    b = dict(schema=2, provider=spec['provider'], endpoint=spec['endpoint'], model=model,
             region=REGION, payload_sha256=payload_sha256, max_tokens=limit,
             input_tokens_max=spec['input_tokens_max'], tariff_sha256=TARIFF_SHA256)
    q = Quote('text_http', b['input_tokens_max'] * rates[0] + limit * rates[1], 1, canonical(b))
    _text_basis(q)
    return q


def _usage_charge(quote: Quote, usage: tuple) -> int:
    b = _text_basis(quote)
    if type(usage) is not tuple or len(usage) != 4 or usage[3] != b['model']:
        raise BudgetHold('MONEY_TEXT_USAGE_UNCONFIRMED')
    prompt = strict_int(usage[0], 0, b['input_tokens_max'])
    completion = strict_int(usage[1], 0, b['max_tokens'])
    total = strict_int(usage[2], 0, b['input_tokens_max'] + b['max_tokens'])
    spec = _text_contract(b['model'])
    if total != prompt + completion or total > spec['context_max']:
        raise BudgetHold('MONEY_TEXT_USAGE_UNCONFIRMED')
    rates = spec['models'][b['model']]
    return prompt * rates[0] + completion * rates[1]


def _quote_cosyvoice(text_bytes: bytes, packets_sha256: str, endpoint_sha256: str) -> Quote:
    if type(text_bytes) is not bytes:
        raise BudgetHold('MONEY_TTS_PAYLOAD_UNCONFIRMED')
    try: text = text_bytes.decode('utf-8')
    except UnicodeError: raise BudgetHold('MONEY_TTS_PAYLOAD_UNCONFIRMED') from None
    if not 1 <= len(text) <= 1000 or text_bytes != text.encode('utf-8'):
        raise BudgetHold('MONEY_TTS_PAYLOAD_UNCONFIRMED')
    basis = dict(schema=2, model='cosyvoice-v3-flash', region=REGION,
        text_utf8_bytes=len(text_bytes), text_sha256=digest(text_bytes), packets_sha256=packets_sha256,
        endpoint_sha256=endpoint_sha256, tariff_sha256=TARIFF_SHA256)
    q = Quote('tts_cosyvoice', len(text_bytes) * 100, 1, canonical(basis))
    _validate_tts_quote(q)
    return q


def _validate_tts_quote(quote):
    try:
        b = json.loads(quote.basis, object_pairs_hook=_exact_object)
        fields = {'schema', 'model', 'region', 'text_utf8_bytes', 'text_sha256', 'packets_sha256', 'endpoint_sha256', 'tariff_sha256'}
        if type(b) is not dict or set(b) != fields: raise BudgetHold('MONEY_TTS_QUOTE_UNCONFIRMED')
        size = strict_int(b['text_utf8_bytes'], 1, 4000)
        if (type(b['schema']) is not int or b['schema'] != 2 or b['model'] != 'cosyvoice-v3-flash'
                or b['region'] != REGION or b['tariff_sha256'] != TARIFF_SHA256
                or any(type(b[k]) is not str or re.fullmatch('[a-f0-9]{64}', b[k]) is None
                       for k in ('text_sha256', 'packets_sha256', 'endpoint_sha256'))
                or quote.operation != 'tts_cosyvoice' or quote.steps != 1 or quote.micro_cny != size * 100
                or quote.basis != canonical(b)):
            raise BudgetHold('MONEY_TTS_QUOTE_UNCONFIRMED')
    except (ValueError, UnicodeError, TypeError, KeyError):
        raise BudgetHold('MONEY_TTS_QUOTE_UNCONFIRMED') from None


def _quote_qwen_voice_clone(text_bytes: bytes, request_sha256: str, endpoint_sha256: str) -> Quote:
    if type(text_bytes) is not bytes:
        raise BudgetHold('MONEY_TTS_PAYLOAD_UNCONFIRMED')
    try: text = text_bytes.decode('utf-8')
    except UnicodeError: raise BudgetHold('MONEY_TTS_PAYLOAD_UNCONFIRMED') from None
    if not 1 <= len(text) <= 1000 or text_bytes != text.encode('utf-8'):
        raise BudgetHold('MONEY_TTS_PAYLOAD_UNCONFIRMED')
    basis = dict(schema=2, model='qwen3-tts-vc-2026-01-22', region=REGION,
        text_utf8_bytes=len(text_bytes), text_sha256=digest(text_bytes), request_sha256=request_sha256,
        endpoint_sha256=endpoint_sha256, tariff_sha256=TARIFF_SHA256)
    quote = Quote('tts_qwen_voice_clone', len(text_bytes) * 100, 1, canonical(basis))
    _validate_qwen_voice_clone_quote(quote)
    return quote


def _validate_qwen_voice_clone_quote(quote):
    try:
        if type(quote.micro_cny) is not int or type(quote.steps) is not int:
            raise BudgetHold('MONEY_TTS_QUOTE_UNCONFIRMED')
        b = json.loads(quote.basis, object_pairs_hook=_exact_object)
        fields = {'schema', 'model', 'region', 'text_utf8_bytes', 'text_sha256', 'request_sha256', 'endpoint_sha256', 'tariff_sha256'}
        if type(b) is not dict or set(b) != fields: raise BudgetHold('MONEY_TTS_QUOTE_UNCONFIRMED')
        size = strict_int(b['text_utf8_bytes'], 1, 4000)
        if (type(b['schema']) is not int or b['schema'] != 2 or b['model'] != 'qwen3-tts-vc-2026-01-22'
                or b['region'] != REGION or b['tariff_sha256'] != TARIFF_SHA256
                or any(type(b[k]) is not str or re.fullmatch('[a-f0-9]{64}', b[k]) is None
                       for k in ('text_sha256', 'request_sha256', 'endpoint_sha256'))
                or quote.operation != 'tts_qwen_voice_clone' or quote.steps != 1 or quote.micro_cny != size * 100
                or quote.basis != canonical(b)):
            raise BudgetHold('MONEY_TTS_QUOTE_UNCONFIRMED')
    except (ValueError, UnicodeError, TypeError, KeyError):
        raise BudgetHold('MONEY_TTS_QUOTE_UNCONFIRMED') from None


def _settlement_evidence(claim, quote, instance, usage, charge):
    return digest(canonical([2, claim.attempt_id, claim.nonce, instance,
                             digest(quote.basis.encode()), list(usage), charge]).encode())


def quote_asr_wav(wav_bytes: bytes, *, region: str = REGION, model: str = ASR_MODEL) -> Quote:
    """Interpret actual canonical WAV bytes; external duration/amount is never accepted.

    Deliberately narrow first slice: canonical 44-byte-header mono PCM16 WAV.
    Ambiguous/extra chunks, truncation, unsupported formats and streams HOLD.
    No content is persisted: the ledger records only digest/frame/rate/format.
    """
    if region != REGION or model != ASR_MODEL:
        raise BudgetHold("UNKNOWN_TARIFF")
    if type(wav_bytes) is not bytes or not 46 <= len(wav_bytes) <= MAX_WAV_BYTES:
        raise BudgetHold("WAV_SIZE_OR_TYPE")
    if (wav_bytes[:4] != b"RIFF" or wav_bytes[8:12] != b"WAVE"
            or wav_bytes[12:16] != b"fmt " or wav_bytes[36:40] != b"data"):
        raise BudgetHold("WAV_NOT_CANONICAL_PCM16")
    riff_size, = struct.unpack_from("<I", wav_bytes, 4)
    fmt_size, audio_format, channels, rate, byte_rate, block_align, width = struct.unpack_from("<IHHIIHH", wav_bytes, 16)
    data_size, = struct.unpack_from("<I", wav_bytes, 40)
    if (riff_size != len(wav_bytes) - 8 or fmt_size != 16 or audio_format != 1
            or channels != 1 or width != 16 or block_align != 2 or byte_rate != rate * 2
            or rate not in WAV_SAMPLE_RATES
            or data_size != len(wav_bytes) - 44 or data_size % 2):
        raise BudgetHold("WAV_NOT_CANONICAL_PCM16")
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as parsed:
            frames = parsed.getnframes()
            sample_rate = parsed.getframerate()
            if (parsed.getnchannels() != 1 or parsed.getsampwidth() != 2
                    or parsed.getcomptype() != "NONE" or sample_rate != rate
                    or frames * 2 != data_size or len(parsed.readframes(frames)) != data_size
                    or parsed.readframes(1) != b""):
                raise BudgetHold("WAV_INTERPRETER_MISMATCH")
    except (wave.Error, EOFError) as exc:
        raise BudgetHold("WAV_PARSE") from exc
    strict_int(frames, 1, MAX_WAV_BYTES // 2)
    seconds = max(1, ceil_ratio(frames, sample_rate))
    basis = canonical({"model": model, "region": region, "wav_sha256": digest(wav_bytes),
                       "frames": frames, "sample_rate": sample_rate, "billed_seconds": seconds,
                       "tariff_sha256": TARIFF_SHA256})
    return Quote("asr_wav", seconds * ASR_MICRO_CNY_PER_SECOND, 1, basis)


def _response_quote(operation: str, *, region: str, model: str) -> Quote:
    if region != REGION or model != OMNI_MODEL:
        raise BudgetHold("UNKNOWN_TARIFF")
    steps = {"manual_response": 1, "public_speech_pair": 2}.get(operation)
    if steps is None:
        raise BudgetHold("UNKNOWN_OPERATION")
    return Quote(operation, RESPONSE_MICRO_CNY * steps, steps,
                 canonical({"model": model, "region": region, "tariff_sha256": TARIFF_SHA256,
                            "input_tokens_max": INPUT_TOKENS_MAX, "output_tokens_max": OUTPUT_TOKENS_MAX,
                            "responses": steps}))


def _validate_stored_quote(quote: Quote) -> None:
    if quote.operation == 'tts_qwen_voice_clone':
        _validate_qwen_voice_clone_quote(quote)
        return
    """Existing row tariff/model/basis must still express the fixed contract."""
    if quote.operation == 'text_http':
        _text_basis(quote)
        return
    if quote.operation == 'tts_cosyvoice':
        _validate_tts_quote(quote)
        return
    if quote.operation in ("manual_response", "public_speech_pair"):
        expected = _response_quote(quote.operation, region=REGION, model=OMNI_MODEL)
        if quote != expected:
            raise BudgetHold("LEDGER_QUOTE_CONTRACT_MISMATCH")
        return
    if quote.operation != "asr_wav" or type(quote.basis) is not str or len(quote.basis) > MAX_POLICY_BYTES:
        raise BudgetHold("LEDGER_QUOTE_CONTRACT_MISMATCH")
    try:
        basis = json.loads(quote.basis, object_pairs_hook=_exact_object)
    except (ValueError, UnicodeError) as exc:
        raise BudgetHold("LEDGER_QUOTE_CONTRACT_MISMATCH") from exc
    fields = {"model", "region", "wav_sha256", "frames", "sample_rate", "billed_seconds", "tariff_sha256"}
    if type(basis) is not dict or set(basis) != fields:
        raise BudgetHold("LEDGER_QUOTE_CONTRACT_MISMATCH")
    frames = strict_int(basis["frames"], 1, (MAX_WAV_BYTES - 44) // 2)
    rate = strict_int(basis["sample_rate"], 1, 48000)
    seconds = strict_int(basis["billed_seconds"], 1, MAX_WAV_BYTES)
    if (basis["model"] != ASR_MODEL or basis["region"] != REGION
            or basis["tariff_sha256"] != TARIFF_SHA256 or rate not in WAV_SAMPLE_RATES
            or type(basis["wav_sha256"]) is not str
            or re.fullmatch(r"[a-f0-9]{64}", basis["wav_sha256"]) is None
            or seconds != max(1, ceil_ratio(frames, rate)) or quote.steps != 1
            or quote.micro_cny != seconds * ASR_MICRO_CNY_PER_SECOND
            or quote.basis != canonical(basis)):
        raise BudgetHold("LEDGER_QUOTE_CONTRACT_MISMATCH")


class MoneyBudget:
    """Trusted server startup opens an existing owner policy, never provisions it.

    All request methods omit run_id, path, cap and caller-supplied quote. Never
    send merely because reserve returned. Claim immediately before each egress.
    """

    def __init__(self, owner_policy: Path):
        self._policy_path = Path(owner_policy)
        self._lock = threading.Lock()
        self._closed = False
        self._poisoned = False
        self._instance_nonce = uuid.uuid4().hex
        self._text_caps = {}
        self._files = None
        self._db = None
        try:
            self._files = OwnerFiles(self._policy_path)
            self._binding, self._policy_sha = _policy(self._policy_path, self._files.read_policy())
            self._db_path = self._policy_path.parent / self._binding["ledger_file"]
            with self._files.guard():
                self._db = sqlite3.connect(self._db_path.as_uri() + "?mode=rw", uri=True,
                    timeout=0, isolation_level=None, check_same_thread=False)
                thread_modes = {r[0] for r in self._db.execute("PRAGMA compile_options") if r[0].startswith("THREADSAFE=")}
                if sqlite3.threadsafety == 0 or thread_modes not in ({"THREADSAFE=1"}, {"THREADSAFE=2"}):
                    raise BudgetHold("LEDGER_THREAD_MODE_UNSUPPORTED")
                self._db.execute("PRAGMA busy_timeout=0")
                self._db.execute("PRAGMA foreign_keys=ON")
                self._db.execute("PRAGMA synchronous=FULL")
                if self._db.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
                    raise BudgetHold("LEDGER_NOT_WAL")
                if self._db.execute("PRAGMA synchronous").fetchone()[0] != 2:
                    raise BudgetHold("LEDGER_NOT_FULL")
                if self._db.execute('PRAGMA user_version').fetchone() != (2,):
                    raise BudgetHold('LEDGER_SCHEMA_VERSION_REQUIRED')
                if self._db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise BudgetHold("LEDGER_INTEGRITY")
            with self._operation():
                self._validate()
        except BaseException as error:
            if self._db is not None: self._db.close()
            if self._files is not None: self._files.close()
            if isinstance(error, OwnerFileError): raise BudgetHold(str(error)) from None
            if isinstance(error, sqlite3.Error): raise self._sql_hold(error) from None
            if isinstance(error, BudgetHold) or not isinstance(error, Exception): raise
            raise BudgetHold("OWNER_NATIVE_IDENTITY_UNCONFIRMED") from None

    def __enter__(self) -> "MoneyBudget":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    @staticmethod
    def _sql_hold(error):
        code = getattr(error, 'sqlite_errorcode', None)
        return BudgetHold("LEDGER_BUSY" if type(code) is int and code & 255 in (5, 6)
            else "LEDGER_SQL_UNCONFIRMED")

    @contextmanager
    def _operation(self, write=True):
        # Never wait on an event loop, another callback or a database writer.
        if not self._lock.acquire(False): raise BudgetHold("LEDGER_BUSY")
        committed = False
        try:
            if self._closed: raise BudgetHold("LEDGER_CLOSED")
            if self._poisoned: raise BudgetHold("LEDGER_OWNER_HOLD")
            with self._files.guard():
                self._db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                try:
                    yield
                    try:
                        self._db.execute("COMMIT")
                        committed = True
                    except BaseException:
                        self._poisoned = True
                        raise
                except BaseException:
                    if self._db.in_transaction: self._db.execute("ROLLBACK")
                    raise
        except OwnerFileError as error:
            if committed: self._poisoned = True
            raise BudgetHold(str(error)) from None
        except sqlite3.Error as error: raise self._sql_hold(error) from None
        except BudgetHold: raise
        except Exception: raise BudgetHold("LEDGER_OPERATION_UNCONFIRMED") from None
        finally: self._lock.release()

    def close(self) -> None:
        if not self._lock.acquire(False): raise BudgetHold("LEDGER_BUSY")
        try:
            if self._closed: return
            self._poisoned = True
            try:
                self._db.close()
                self._files.close()
            except Exception: raise BudgetHold("LEDGER_CLOSE_UNCONFIRMED") from None
            self._closed = True
        finally: self._lock.release()

    def _validate(self) -> Balance:
        binding, policy_sha = _policy(self._policy_path, self._files.read_policy())
        if binding != self._binding or policy_sha != self._policy_sha:
            raise BudgetHold("OWNER_POLICY_CHANGED")
        row = self._db.execute("SELECT binding_json, policy_sha256 FROM owner WHERE singleton=1").fetchall()
        if row != [(canonical(self._binding), self._policy_sha)]:
            raise BudgetHold("LEDGER_OWNER_CONTRACT_MISMATCH")
        if self._db.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise BudgetHold('LEDGER_FOREIGN_KEY_MISMATCH')
        if self._db.execute('SELECT state FROM run_control WHERE singleton=1').fetchall() != [('ACTIVE',)]:
            raise BudgetHold('LEDGER_RUN_POISONED')
        # One global legacy-state guard per validation, independent of history size.
        if self._db.execute("SELECT 1 FROM steps s JOIN attempts a USING(attempt_id) WHERE s.state='settled' AND a.operation!='text_http' LIMIT 1").fetchone():
            raise BudgetHold('LEDGER_LEGACY_SETTLEMENT_FORBIDDEN')
        # Each attempt contributes its quote once, independently of state. Also
        # detect broken step/quote metadata before admitting any new liability.
        totals = self._db.execute("""SELECT a.quote_micro_cny, typeof(a.quote_micro_cny), a.step_count,
            COUNT(s.ordinal), COALESCE(SUM(s.quote_micro_cny),0), MIN(s.ordinal), MAX(s.ordinal), a.operation, a.basis_json
            FROM attempts a LEFT JOIN steps s ON s.attempt_id=a.attempt_id GROUP BY a.attempt_id""").fetchall()
        committed = 0
        for quote, kind, count, actual_count, step_sum, lo, hi, operation, basis in totals:
            strict_int(quote, 1, CAP_MICRO_CNY)
            strict_int(count, 1, 2)
            if kind != "integer" or (actual_count, step_sum, lo, hi) != (count, quote, 0, count - 1):
                raise BudgetHold("LEDGER_STEP_QUOTE_MISMATCH")
            _validate_stored_quote(Quote(operation, quote, count, basis))
            if operation != 'text_http':
                committed += quote
            # Text liability is checked per immutable quote/unique settlement below.
        texts = self._db.execute("SELECT attempt_id,quote_micro_cny,step_count,basis_json FROM attempts WHERE operation='text_http'").fetchall()
        for attempt, amount, count, basis in texts:
            q = Quote('text_http', amount, count, basis)
            state, nonce = self._db.execute('SELECT state,claim_nonce FROM steps WHERE attempt_id=? AND ordinal=0', (attempt,)).fetchone()
            dispatch = self._db.execute('SELECT claim_nonce,owner_instance FROM text_dispatch WHERE attempt_id=?', (attempt,)).fetchall()
            settlements = self._db.execute('SELECT claim_nonce,owner_instance,prompt_tokens,completion_tokens,total_tokens,reported_model,charged_micro_cny,evidence_sha256 FROM settlements WHERE attempt_id=?', (attempt,)).fetchall()
            if state == 'reserved':
                if dispatch or settlements: raise BudgetHold('LEDGER_TEXT_STATE_MISMATCH')
            else:
                if len(dispatch) != 1 or dispatch[0][0] != nonce or re.fullmatch('[a-f0-9]{32}', dispatch[0][1]) is None:
                    raise BudgetHold('LEDGER_TEXT_STATE_MISMATCH')
            if state == 'settled':
                if len(settlements) != 1: raise BudgetHold('LEDGER_TEXT_SETTLEMENT_MISSING')
                sn, instance, prompt, completion, total, model, charge, evidence = settlements[0]
                usage = (prompt, completion, total, model)
                claim = DispatchClaim(attempt, 0, nonce)
                if ((sn, instance) != dispatch[0] or charge != _usage_charge(q, usage)
                        or evidence != _settlement_evidence(claim, q, instance, usage, charge)):
                    raise BudgetHold('LEDGER_TEXT_SETTLEMENT_MISMATCH')
                committed += charge
            else:
                if settlements: raise BudgetHold('LEDGER_TEXT_STATE_MISMATCH')
                committed += amount
        if self._db.execute("SELECT 1 FROM text_dispatch d JOIN attempts a USING(attempt_id) WHERE a.operation!='text_http'").fetchone() or self._db.execute("SELECT 1 FROM settlements s JOIN attempts a USING(attempt_id) WHERE a.operation!='text_http'").fetchone():
            raise BudgetHold('LEDGER_LEGACY_SETTLEMENT_FORBIDDEN')
        if not 0 <= committed <= CAP_MICRO_CNY:
            raise BudgetHold("LEDGER_TOTAL_OUT_OF_BOUNDS")
        return Balance(CAP_MICRO_CNY, committed, CAP_MICRO_CNY - committed, len(totals))

    def balance(self) -> Balance:
        with self._operation(write=False):
            return self._validate()

    def reserve_asr_wav(self, attempt_id: str, wav_bytes: bytes) -> Reservation:
        return self._reserve(attempt_id, quote_asr_wav(wav_bytes, region=self._binding["region"], model=self._binding["asr_model"]))

    def reserve_manual_response(self, attempt_id: str) -> Reservation:
        return self._reserve(attempt_id, _response_quote("manual_response", region=self._binding["region"], model=self._binding["omni_model"]))

    def reserve_public_speech_pair(self, attempt_id: str) -> Reservation:
        return self._reserve(attempt_id, _response_quote("public_speech_pair", region=self._binding["region"], model=self._binding["omni_model"]))

    def reserve_automatic_rtc(self, attempt_id: str) -> Reservation:
        identifier(attempt_id)
        raise BudgetHold("AUTOMATIC_RTC_NO_PROVEN_FINITE_BOUND")

    def reserve_streaming_asr(self, attempt_id: str) -> Reservation:
        identifier(attempt_id)
        raise BudgetHold("STREAMING_ASR_NO_FINITE_WAV_CONTRACT")

    def _view(self, attempt_id: str, created: bool = False) -> Reservation:
        row = self._db.execute("SELECT operation,quote_micro_cny FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
        if row is None:
            raise BudgetHold("ATTEMPT_NOT_RESERVED")
        states = tuple(r[0] for r in self._db.execute("SELECT state FROM steps WHERE attempt_id=? ORDER BY ordinal", (attempt_id,)))
        return Reservation(attempt_id, row[0], row[1], states, created)

    def inspect_attempt(self, attempt_id: str) -> Reservation:
        identifier(attempt_id)
        with self._operation(write=False):
            self._validate()
            return self._view(attempt_id)

    def _reserve(self, attempt_id: str, quote: Quote) -> Reservation:
        identifier(attempt_id)
        strict_int(quote.micro_cny, 1, CAP_MICRO_CNY)
        _validate_stored_quote(quote)
        with self._operation():
            balance = self._validate()
            old = self._db.execute("SELECT operation,quote_micro_cny,step_count,basis_json FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
            if old is not None:
                if old != (quote.operation, quote.micro_cny, quote.steps, quote.basis):
                    raise BudgetHold("ATTEMPT_INPUT_CONFLICT")
                result = self._view(attempt_id)  # no capability; no new charge
            else:
                if quote.micro_cny > balance.remaining_micro_cny:
                    raise BudgetHold("INSUFFICIENT_BUDGET")
                self._db.execute("INSERT INTO attempts VALUES(?,?,?,?,?,?)", (attempt_id, quote.operation, quote.micro_cny, quote.steps, quote.basis, time.time_ns()))
                for ordinal in range(quote.steps):
                    self._db.execute("INSERT INTO steps VALUES(?,?,?,?,NULL)", (attempt_id, ordinal, quote.micro_cny // quote.steps, "reserved"))
                result = self._view(attempt_id, True)
            return result

    def claim_dispatch(self, attempt_id: str, step: int = 0) -> DispatchClaim:
        identifier(attempt_id)
        strict_int(step, 0, 1)
        with self._operation():
            self._validate()
            if self._db.execute('SELECT operation FROM attempts WHERE attempt_id=?', (attempt_id,)).fetchone() == ('text_http',):
                raise BudgetHold('OWNED_TEXT_CAPABILITY_REQUIRED')
            row = self._db.execute("SELECT state FROM steps WHERE attempt_id=? AND ordinal=?", (attempt_id, step)).fetchone()
            if row is None:
                raise BudgetHold("STEP_NOT_RESERVED")
            if row[0] != "reserved":
                raise BudgetHold("DISPATCH_ALREADY_CLAIMED")
            if step == 1 and self._db.execute("SELECT state FROM steps WHERE attempt_id=? AND ordinal=0", (attempt_id,)).fetchone() != ("closed",):
                raise BudgetHold("PREVIOUS_RESPONSE_NOT_CLOSED")
            claim = DispatchClaim(attempt_id, step, uuid.uuid4().hex)
            changed = self._db.execute("UPDATE steps SET state='dispatching',claim_nonce=? WHERE attempt_id=? AND ordinal=? AND state='reserved'", (claim.nonce, attempt_id, step)).rowcount
            if changed != 1:
                raise BudgetHold("DISPATCH_CLAIM_RACE")
            return claim

    def _finish(self, claim: DispatchClaim, state: str) -> None:
        if type(claim) is not DispatchClaim or state not in ("unknown", "closed"):
            raise BudgetHold("INVALID_CLAIM")
        identifier(claim.attempt_id)
        strict_int(claim.step, 0, 1)
        if type(claim.nonce) is not str or re.fullmatch(r"[a-f0-9]{32}", claim.nonce) is None:
            raise BudgetHold("INVALID_CLAIM")
        with self._operation():
            self._validate()
            changed = self._db.execute("UPDATE steps SET state=? WHERE attempt_id=? AND ordinal=? AND claim_nonce=? AND state='dispatching'", (state, claim.attempt_id, claim.step, claim.nonce)).rowcount
            if changed != 1:
                raise BudgetHold("CLAIM_NOT_IN_FLIGHT")

    def mark_unknown(self, claim: DispatchClaim) -> None:
        self._finish(claim, "unknown")

    def mark_closed(self, claim: DispatchClaim) -> None:
        """Provider result/owned closure diagnostic, never a monetary refund."""
        self._finish(claim, "closed")

    def _claim_owned_text(self, quote: Quote) -> _OwnedTextClaim:
        """Only the actual HTTP owner calls this; no supplied amount or attempt."""
        _text_basis(quote)
        attempt = 'text-' + uuid.uuid4().hex
        with self._operation():
            balance = self._validate()
            if quote.micro_cny > balance.remaining_micro_cny:
                raise BudgetHold('INSUFFICIENT_BUDGET')
            claim = DispatchClaim(attempt, 0, uuid.uuid4().hex)
            self._db.execute('INSERT INTO attempts VALUES(?,?,?,?,?,?)',
                (attempt, quote.operation, quote.micro_cny, 1, quote.basis, time.time_ns()))
            self._db.execute('INSERT INTO steps VALUES(?,?,?,?,?)', (attempt, 0, quote.micro_cny, 'dispatching', claim.nonce))
            self._db.execute('INSERT INTO text_dispatch VALUES(?,?,?)', (attempt, claim.nonce, self._instance_nonce))
        cap = _OwnedTextClaim(claim, quote, self._instance_nonce)
        self._text_caps[id(cap)] = cap
        return cap

    def _claim_owned_tts(self, quote: Quote) -> DispatchClaim:
        if quote.operation == 'tts_qwen_voice_clone': _validate_qwen_voice_clone_quote(quote)
        else: _validate_tts_quote(quote)
        attempt = 'tts-' + uuid.uuid4().hex
        with self._operation():
            balance = self._validate()
            if quote.micro_cny > balance.remaining_micro_cny: raise BudgetHold('INSUFFICIENT_BUDGET')
            claim = DispatchClaim(attempt, 0, uuid.uuid4().hex)
            self._db.execute('INSERT INTO attempts VALUES(?,?,?,?,?,?)', (attempt, quote.operation, quote.micro_cny, 1, quote.basis, time.time_ns()))
            self._db.execute('INSERT INTO steps VALUES(?,?,?,?,?)', (attempt, 0, quote.micro_cny, 'dispatching', claim.nonce))
        return claim

    def _require_text_cap(self, cap):
        if (type(cap) is not _OwnedTextClaim or self._text_caps.get(id(cap)) is not cap
                or cap.owner_instance != self._instance_nonce):
            raise BudgetHold('OWNED_TEXT_CAPABILITY_REQUIRED')

    def _unknown_owned_text(self, cap, *, poison=False):
        self._require_text_cap(cap)
        try:
            with self._operation():
                self._validate()
                changed = self._db.execute("UPDATE steps SET state='unknown' WHERE attempt_id=? AND ordinal=0 AND state='dispatching' AND claim_nonce=?", (cap.claim.attempt_id, cap.claim.nonce)).rowcount
                if changed != 1: raise BudgetHold('CLAIM_NOT_IN_FLIGHT')
                if poison:
                    if self._db.execute("UPDATE run_control SET state='POISONED' WHERE singleton=1 AND state='ACTIVE'").rowcount != 1:
                        raise BudgetHold('LEDGER_POISON_UNCONFIRMED')
        except BaseException:
            self._poisoned = True
            raise
        finally:
            self._text_caps.pop(id(cap), None)

    def _settle_owned_text(self, cap, usage):
        self._require_text_cap(cap)
        charge = _usage_charge(cap.quote, usage)
        try:
            with self._operation():
                self._validate()
                changed = self._db.execute("UPDATE steps SET state='settled' WHERE attempt_id=? AND ordinal=0 AND state='dispatching' AND claim_nonce=? AND EXISTS(SELECT 1 FROM text_dispatch WHERE attempt_id=? AND owner_instance=?)", (cap.claim.attempt_id, cap.claim.nonce, cap.claim.attempt_id, cap.owner_instance)).rowcount
                if changed != 1: raise BudgetHold('CLAIM_NOT_IN_FLIGHT')
                evidence = _settlement_evidence(cap.claim, cap.quote, cap.owner_instance, usage, charge)
                self._db.execute('INSERT INTO settlements VALUES(?,?,?,?,?,?,?,?,?)', (cap.claim.attempt_id, cap.claim.nonce, cap.owner_instance, *usage, charge, evidence))
        except BaseException:
            self._poisoned = True
            raise
        finally:
            self._text_caps.pop(id(cap), None)

    def dispatch_once(self, attempt_id: str, send: Callable[[], Any], step: int = 0) -> Any:
        """Synchronous trusted adapter: claim commits before invoking actual egress.

        ASR uses step0. Public WS must reserve the pair first, then use step0
        around initial response and step1 around the response after tool output.
        A send callback must perform exactly its one pre-agreed provider response
        and return after its completion; it is not an untrusted plugin boundary.
        """
        claim = self.claim_dispatch(attempt_id, step)
        try:
            result = send()
        except BaseException:
            self.mark_unknown(claim)
            raise
        self.mark_closed(claim)
        return result

    def dispatch_asr_wav(self, attempt_id: str, wav_bytes: bytes, send: Callable[[bytes], Any]) -> Any:
        """Actual WAV bytes are quoted, conflict-checked and passed to one HTTP send.

        Adapter must disable transport retries; a new request requires a new
        attempt. Callback uses its supplied bytes, not renderer-provided duration.
        """
        self.reserve_asr_wav(attempt_id, wav_bytes)
        return self.dispatch_once(attempt_id, lambda: send(wav_bytes))

    async def dispatch_once_async(self, attempt_id: str, send: Callable[[], Any], step: int = 0) -> Any:
        """Async equivalent; cancellation also retains the full quote."""
        claim = self.claim_dispatch(attempt_id, step)
        try:
            result = await send()
        except BaseException:
            self.mark_unknown(claim)
            raise
        self.mark_closed(claim)
        return result
