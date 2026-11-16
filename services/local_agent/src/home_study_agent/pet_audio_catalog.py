"""Reviewed public tariff, independently checked in the backend at each live boundary."""
from datetime import datetime, timezone
import re
import json
import base64
import hashlib
import stat
import os
from pathlib import Path
from decimal import Decimal
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

CATALOG_ID = 'aliyun-cn-qwen35-omni-plus-20260315-20261024-v1'
MODEL = 'qwen3.5-omni-plus-2026-03-15'
VERIFIED_AT = '2026-10-24T05:04:30+00:00'
REVIEW_BEFORE = '2026-11-23T05:04:30+00:00'
RATES = (371, 7, 40)
TRUSTED_KEYS = {'hsc-maintainer-20261024-v1': {'issuer': '家庭学习伙伴项目维护者', 'publicKeyPem': '-----BEGIN PUBLIC KEY-----\nMCowBQYDK2VwAyEACaMR8RCdhJQxU01vZiB4I0ZP0DxspA/fzSctIGtPwm0=\n-----END PUBLIC KEY-----\n'}}  # Dedicated project-maintainer public root; not an Aliyun official key.
SIGNED_FIELDS = {'schema', 'revision', 'id', 'verified_at', 'review_before', 'region', 'currency',
    'endpoint_family', 'protocol', 'model', 'text_input_cny_per_million', 'audio_input_cny_per_million',
    'text_output_with_audio_cny_per_million', 'audio_tokens_per_second', 'source_urls', 'evidence_sha256'}


def _need(value, code):
    if not value:
        raise ValueError('PET_CATALOG_' + code)


def _strict_json(data):
    def pairs(rows):
        out = {}
        for k, v in rows:
            _need(k not in out, 'DUPLICATE_KEY')
            out[k] = v
        return out
    return json.loads(data, object_pairs_hook=pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError('PET_CATALOG_NONFINITE')))


def verify_signed_catalog(data, *, at=None, _trusted_keys=None):
    """Test policy injection is a Python call argument, never an env/HTTP trust root."""
    _need(type(data) is bytes and 0 < len(data) <= 16384, 'SIZE')
    e = _strict_json(data)
    _need(type(e) is dict and set(e) == {'schema', 'key_id', 'payload_base64', 'signature_base64'}
        and e['schema'] == 'pet_audio_catalog_signed_v1' and type(e['key_id']) is str
        and re.fullmatch(r'[A-Za-z0-9-]{8,100}', e['key_id']) and type(e['payload_base64']) is str
        and type(e['signature_base64']) is str, 'ENVELOPE')
    _need(json.dumps(e, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode() == data, 'ENVELOPE_CANONICAL')
    key = (TRUSTED_KEYS if _trusted_keys is None else _trusted_keys).get(e['key_id'])
    _need(key is not None, 'SIGNER_NOT_CONFIGURED')
    body = base64.b64decode(e['payload_base64'], validate=True)
    signature = base64.b64decode(e['signature_base64'], validate=True)
    _need(base64.b64encode(body).decode() == e['payload_base64'] and len(signature) == 64
        and base64.b64encode(signature).decode() == e['signature_base64'] and body
        and all(32 <= b <= 126 for b in body), 'ENCODING')
    public = serialization.load_pem_public_key(key['publicKeyPem'].encode())
    _need(isinstance(public, Ed25519PublicKey), 'KEY_TYPE')
    public.verify(signature, body)
    p = _strict_json(body)
    _need(type(p) is dict and set(p) == SIGNED_FIELDS and json.dumps(p, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode() == body, 'CANONICAL')
    _need(p['schema'] == 'pet_audio_catalog_v1' and type(p['revision']) is int and 0 < p['revision'] <= 2147483647
        and type(p['id']) is str and re.fullmatch(r'[A-Za-z0-9-]{8,100}', p['id']), 'ID')
    _need(p['region'] == 'cn-beijing' and p['currency'] == 'CNY'
        and p['endpoint_family'] == 'aliyun-compatible-cn-beijing-v1'
        and p['protocol'] == 'qwen_omni_audio_json_sse_v1', 'PROTOCOL')
    _need(type(p['model']) is str and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{1,110}-20\d\d-\d\d-\d\d', p['model']), 'FIXED_MODEL')
    def stamp(value):
        _need(type(value) is str and re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ', value), 'TIME')
        n = datetime.fromisoformat(value.replace('Z', '+00:00'))
        _need(n.strftime('%Y-%m-%dT%H:%M:%SZ') == value, 'TIME')
        return n
    start, end, now = stamp(p['verified_at']), stamp(p['review_before']), at or datetime.now(timezone.utc)
    _need(now.tzinfo is not None and start <= now < end and 0 < (end-start).total_seconds() <= 30 * 86400, 'EXPIRED')
    _need(type(p['source_urls']) is list and 1 <= len(p['source_urls']) <= 4
        and all(type(u) is str and re.fullmatch(r'https://help\.aliyun\.com/zh/model-studio/[A-Za-z0-9/_-]{1,180}', u) for u in p['source_urls'])
        and len(set(p['source_urls'])) == len(p['source_urls']) and type(p['evidence_sha256']) is str
        and re.fullmatch(r'[a-f0-9]{64}', p['evidence_sha256']), 'SOURCES')
    _need(type(p['audio_tokens_per_second']) is int and 0 < p['audio_tokens_per_second'] <= 1000, 'METERING')
    def micros(value):
        _need(type(value) is str and re.fullmatch(r'(?:0|[1-9]\d{0,5})(?:\.\d{1,6})?', value), 'PRICE')
        n = int(Decimal(value) * 1000000)
        _need(n > 0, 'PRICE')
        return n
    def ceiling(n):
        value = (n + 999999) // 1000000
        _need(0 < value <= 1000000, 'PRICE_BOUND')
        return value
    rates = (ceiling(micros(p['audio_input_cny_per_million']) * p['audio_tokens_per_second']),
        ceiling(micros(p['text_input_cny_per_million'])), ceiling(micros(p['text_output_with_audio_cny_per_million'])))
    return {'payload': p, 'rates': rates, 'sha256': hashlib.sha256(data).hexdigest(), 'issuer': key['issuer']}


def read_signed_catalog(file, digest, *, at=None):
    _need(type(file) is str and Path(file).is_absolute() and type(digest) is str and re.fullmatch(r'[a-f0-9]{64}', digest), 'FILE')
    path = Path(file)
    before = path.lstat()
    _need(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and 0 < before.st_size <= 16384, 'FILE')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        held = os.fstat(fd)
        _need((held.st_dev, held.st_ino, held.st_size) == (before.st_dev, before.st_ino, before.st_size) and held.st_nlink == 1, 'FILE_CHANGED')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            data = stream.read(16385)
        after, end = path.lstat(), os.fstat(fd)
        _need(len(data) == before.st_size and len(data) <= 16384
            and (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            and (held.st_size, held.st_mtime_ns) == (end.st_size, end.st_mtime_ns) and end.st_nlink == 1
            and hashlib.sha256(data).hexdigest() == digest, 'FILE_CHANGED')
    finally:
        os.close(fd)
    return verify_signed_catalog(data, at=at)


def require_control(file, expected):
    """Opaque desktop revocation bytes only; never signer, price or permission authority."""
    _need(type(file) is str and Path(file).is_absolute() and Path(file).name == 'pet-audio-catalog-control.bin'
        and type(expected) is str and (expected == 'ABSENT' or re.fullmatch(r'[a-f0-9]{64}', expected)), 'CONTROL_REQUIRED')
    path = Path(file)
    try:
        for parent in path.parents:
            entry = parent.lstat()
            _need(stat.S_ISDIR(entry.st_mode) and not stat.S_ISLNK(entry.st_mode)
                and not getattr(entry, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0), 'CONTROL_ALIAS')
        try:
            before = path.lstat()
        except FileNotFoundError:
            _need(expected == 'ABSENT' and path.parent.is_dir(), 'CONTROL_CHANGED')
            return
        _need(expected != 'ABSENT' and stat.S_ISREG(before.st_mode) and before.st_nlink == 1
            and not getattr(before, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0)
            and 0 < before.st_size <= 16384, 'CONTROL_CHANGED')
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        try:
            held = os.fstat(fd)
            _need((held.st_dev, held.st_ino, held.st_size) == (before.st_dev, before.st_ino, before.st_size)
                and held.st_nlink == 1, 'CONTROL_CHANGED')
            with os.fdopen(fd, 'rb', closefd=False) as stream:
                data = stream.read(16385)
            after, end = path.lstat(), os.fstat(fd)
            _need(len(data) == before.st_size and len(data) <= 16384
                and (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                and (held.st_size, held.st_mtime_ns) == (end.st_size, end.st_mtime_ns) and end.st_nlink == 1
                and hashlib.sha256(data).hexdigest() == expected, 'CONTROL_CHANGED')
        finally:
            os.close(fd)
    except OSError:
        raise ValueError('PET_CATALOG_CONTROL_UNAVAILABLE') from None


def current(endpoint, at=None):
    now = at if at is not None else datetime.now(timezone.utc)
    if getattr(endpoint, 'catalog_file', '') or getattr(endpoint, 'catalog_sha256', ''):
        try:
            signed = read_signed_catalog(endpoint.catalog_file, endpoint.catalog_sha256, at=now)
            payload = signed['payload']
            return (endpoint.catalog_id == payload['id'] and endpoint.tariff_version == payload['id'] and endpoint.model == payload['model']
                and isinstance(endpoint.configuration_id, str) and re.fullmatch(r'[A-Za-z0-9-]{8,100}', endpoint.configuration_id) is not None
                and re.fullmatch(r'https://(?:dashscope\.aliyuncs\.com|[a-z0-9][a-z0-9-]{0,62}\.cn-beijing\.maas\.aliyuncs\.com)/compatible-mode/v1', endpoint.base_url) is not None
                and all(type(v) is int for v in (endpoint.input_second_micros, endpoint.input_text_token_micros, endpoint.output_token_micros))
                and (endpoint.input_second_micros, endpoint.input_text_token_micros, endpoint.output_token_micros) == signed['rates'])
        except Exception:
            return False
    return (endpoint.catalog_id == CATALOG_ID and endpoint.tariff_version == CATALOG_ID
        and endpoint.model == MODEL and isinstance(endpoint.configuration_id, str)
        and re.fullmatch(r'[A-Za-z0-9-]{8,100}', endpoint.configuration_id) is not None
        and re.fullmatch(r'https://(?:dashscope\.aliyuncs\.com|[a-z0-9][a-z0-9-]{0,62}\.cn-beijing\.maas\.aliyuncs\.com)/compatible-mode/v1', endpoint.base_url) is not None
        and (endpoint.input_second_micros, endpoint.input_text_token_micros, endpoint.output_token_micros) == RATES
        and all(type(rate) is int for rate in (endpoint.input_second_micros, endpoint.input_text_token_micros, endpoint.output_token_micros))
        and now.tzinfo is not None and datetime.fromisoformat(VERIFIED_AT) <= now < datetime.fromisoformat(REVIEW_BEFORE))
