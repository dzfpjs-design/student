"""Verify offline authority receipts; existing audit_event is the only journal.

Trust is installed separately on this computer. Uploading a public key, an alias
or a model's confidence never establishes an immutable revision or blind review.
"""
import base64
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from uuid import uuid4

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .rubric_calibration_evidence import digest, encoded, is_hash, require, timestamp

IMPORT = 'rubric_authority_import_v1'
REVOKE = 'rubric_authority_revoke_v1'
QUARANTINE = 'rubric_authority_restore_quarantine_v1'
ACTIONS = (IMPORT, REVOKE, QUARANTINE)
KINDS = {'scorer_revision', 'holdout', 'blind_label', 'printed_blind_label', 'audio_projection', 'audio_holdout', 'audio_blind_display', 'audio_blind_label'}
LOCAL = {'local_only': True, 'cloud_transmission_allowed': False}
COMMON_FIELDS = {'receipt_id', 'child_id', 'status'}
PAYLOAD_FIELDS = {
    'scorer_revision': COMMON_FIELDS | {'provider', 'model', 'model_revision', 'verification_method',
        'evidence_hash', 'endpoint_scope_sha256', 'verified_at', 'expires_at'},
    'holdout': COMMON_FIELDS | {'corpus_kind', 'blind_protocol_version', 'provenance_receipt_hash',
        'scope_hash', 'frozen_at', 'cases', 'development_group_hashes', 'application_scope', 'printed_application_scope'},
    'blind_label': COMMON_FIELDS | {'identity_authority', 'identity_receipt_hash', 'display_receipt_hash',
        'submission_receipt_hash', 'mask_version', 'protocol_hash', 'assessment_fingerprint', 'evidence_id',
        'source_version', 'reviewer_principal_id', 'reviewer_id', 'review_event_id', 'review_event_hash',
        'displayed_at', 'submitted_at'},
    'printed_blind_label': COMMON_FIELDS | {'identity_authority', 'identity_receipt_hash', 'display_receipt_hash',
        'submission_receipt_hash', 'mask_version', 'protocol_hash', 'candidate_sha256', 'evidence_id',
        'source_version', 'reviewer_principal_id', 'reviewer_id', 'displayed_at', 'submitted_at', 'scores'},
}


def _payload_shape(kind, payload):
    if kind.startswith('audio_'):
        from .rubric_audio_projection import authority_shape
        authority_shape(kind,payload);return
    optional = {'development_group_hashes', 'application_scope', 'printed_application_scope'} if kind == 'holdout' else set()
    require(PAYLOAD_FIELDS[kind] - optional <= set(payload) <= PAYLOAD_FIELDS[kind], 'AUTHORITY_PAYLOAD_FIELDS')
    for key, value in payload.items():
        if key not in {'cases', 'development_group_hashes', 'application_scope', 'printed_application_scope', 'scores'}:
            require(isinstance(value, str) and 0 < len(value) <= 300, 'AUTHORITY_PAYLOAD_FIELDS')
    if kind == 'printed_blind_label':
        from .paper_printed_score_candidate import decimal
        scores = payload['scores']
        require(isinstance(scores, dict) and 1 <= len(scores) <= 8, 'PRINTED_LABEL_POINTS')
        for key, value in scores.items():
            require(isinstance(key, str) and 0 < len(key) <= 200, 'PRINTED_LABEL_POINTS')
            require(decimal(value) <= 20, 'PRINTED_LABEL_POINTS')
        return
    if kind != 'holdout':
        return
    cases = payload['cases']
    require(isinstance(cases, list) and 1 <= len(cases) <= 10000, 'HOLDOUT_CASES_REQUIRED')
    for case in cases:
        require(isinstance(case, dict) and set(case) == {'evidence_id', 'group_hash', 'stratum', 'source_ids'}, 'CASE_FIELDS')
        require(all(isinstance(case[k], str) and 0 < len(case[k]) <= 200 for k in ('evidence_id', 'group_hash', 'stratum')),
                'CASE_FIELDS')
        require(isinstance(case['source_ids'], list) and len(case['source_ids']) <= 200
                and all(isinstance(v, str) and 0 < len(v) <= 200 for v in case['source_ids']), 'SOURCE_IDS_REQUIRED')
    groups = payload.get('development_group_hashes', [])
    require(isinstance(groups, list) and len(groups) <= 10000
            and all(isinstance(v, str) and len(v) == 64 for v in groups), 'HOLDOUT_LEAKAGE')
    rules = payload.get('application_scope', [])
    require(isinstance(rules, list) and len(rules) <= 20, 'SCOPE_FIELDS')
    for rule in rules:
        require(isinstance(rule, dict) and set(rule) == {'profile_id', 'answer_types', 'stratum'}
                and all(isinstance(rule[k], str) and 0 < len(rule[k]) <= 200 for k in ('profile_id', 'stratum'))
                and isinstance(rule['answer_types'], list) and 1 <= len(rule['answer_types']) <= 20
                and all(isinstance(v, str) and 0 < len(v) <= 100 for v in rule['answer_types']), 'SCOPE_FIELDS')
    printed_rules = payload.get('printed_application_scope', [])
    require(isinstance(printed_rules, list) and len(printed_rules) <= 10000, 'PRINTED_SCOPE_FIELDS')
    identities, printed_groups = set(), set()
    excluded = {case['group_hash'] for case in cases} | set(groups)
    for rule in printed_rules:
        require(isinstance(rule, dict) and set(rule) == {
            'question_sha256', 'source_text_sha256', 'criteria_hash', 'group_hash', 'stratum'}, 'PRINTED_SCOPE_FIELDS')
        require(all(is_hash(rule[k]) for k in ('question_sha256', 'source_text_sha256', 'criteria_hash', 'group_hash'))
                and isinstance(rule['stratum'], str) and 0 < len(rule['stratum']) <= 200, 'PRINTED_SCOPE_FIELDS')
        identity = (rule['question_sha256'], rule['source_text_sha256'], rule['criteria_hash'])
        require(identity not in identities and rule['group_hash'] not in printed_groups
                and rule['group_hash'] not in excluded, 'PRINTED_SCOPE_LEAKAGE')
        identities.add(identity)
        printed_groups.add(rule['group_hash'])


def trusted_issuers():
    path = os.environ.get('HSC_RUBRIC_CALIBRATION_TRUST_FILE')
    if not path:
        return []
    try:
        data = Path(path).read_bytes()
        require(len(data) <= 131072, 'TRUST_CONFIGURATION_INVALID')
        value = json.loads(data)
        require(value.get('version') == 'rubric_authority_trust_v1'
                and isinstance(value.get('issuers'), list), 'TRUST_CONFIGURATION_INVALID')
        return value['issuers']
    except (OSError, ValueError, TypeError, AttributeError):
        return []


def _verify_signature(public_key_pem, signature, content):
    key = serialization.load_pem_public_key(public_key_pem.encode())
    require(isinstance(key, Ed25519PublicKey), 'SIGNATURE_INVALID')
    key.verify(base64.b64decode(signature, validate=True), content)


class SignatureChecks:
    """Pure successful signature math, retained only by one ReadScope.

    Exact immutable bytes include the current public key. This holds no issuer
    scope, expiration, revocation or other authorization decision.
    """
    def __init__(self):
        self.checked = set()
        self.size = 0

    def verify(self, public_key_pem, signature, content):
        key = public_key_pem, signature, content
        if key in self.checked:
            return
        _verify_signature(*key)
        size = len(public_key_pem) + len(signature) + len(content)
        if len(self.checked) < 1024 and self.size + size <= 4_000_000:
            self.checked.add(key)
            self.size += size

    def clear(self):
        self.checked.clear()
        self.size = 0


_PARSED_ROWS_MAX_BYTES = 4_000_000
_PARSED_ROWS_MAX_COUNT = 1024


class _ParsedAuthorityRows:
    """Pure syntax only, one complete fresh-SQL snapshot in one audio scope."""
    def __init__(self, connection):
        self.connection = connection
        self._drop()

    def _drop(self):
        self.snapshot = None
        self.imports = None
        self.invalid = None
        self.malformed = False
        self.bad_kinds = None

    def clear(self):
        self._drop()
        self.connection = None

    def resolve(self, c, child_id, kind, reference_id, signatures):
        require(self.connection is c, 'AUTHORITY_PARSE_SCOPE_CHANGED')
        current = rows(c, child_id)  # Original SQL is ALWAYS fresh.
        snapshot = (child_id, tuple(tuple(row.items()) for row in current))
        size = sum(len(v) if isinstance(v, bytes) else len(str(v).encode())
                   for row in current for v in row.values())
        bounded = len(current) <= _PARSED_ROWS_MAX_COUNT and size <= _PARSED_ROWS_MAX_BYTES
        if not bounded or snapshot != self.snapshot:
            self._drop()
            imports, invalid, malformed, bad_kinds = {}, set(), False, set()
            for row in current:
                try:
                    value = json.loads(row['summary_json'])
                    if row['action'] == IMPORT:
                        envelope = value['envelope']
                        k = envelope['kind']
                        # Non-string JSON fields cannot match the original
                        # audio string selector; do not create a new denial.
                        if isinstance(k, str):
                            try:
                                rid = envelope['receipt_id']
                            except (KeyError, TypeError):
                                # Original lookup short-circuits by kind. A
                                # missing ID only invalidates this same kind.
                                bad_kinds.add(k)
                                continue
                            if isinstance(rid, str):
                                imports.setdefault((k, rid), []).append((row['audit_id'], value))
                    else:
                        invalid.add(value['import_id'])
                except (ValueError, KeyError, TypeError):
                    malformed = True
                    break
            if bounded:
                self.snapshot, self.imports, self.invalid, self.malformed, self.bad_kinds = snapshot, imports, invalid, malformed, bad_kinds
        else:
            imports, invalid, malformed, bad_kinds = self.imports, self.invalid, self.malformed, self.bad_kinds
        if malformed or kind in bad_kinds:
            return None
        matches = imports.get((kind, reference_id), [])
        if len(matches) != 1 or matches[0][0] in invalid:
            return None
        try:
            # Never expose cached nested objects. Original fresh-json mutation
            # isolation is retained for adapters and every caller.
            value = copy.deepcopy(matches[0][1])
            return verify(child_id, value['envelope'], value['signature'], _signatures=signatures)
        except (ValueError, TypeError, KeyError, AttributeError):
            return None


def verify(child_id, envelope, signature, *, now=None, _signatures=None):
    now = timestamp(now or datetime.now(timezone.utc))
    require(isinstance(envelope, dict) and set(envelope) == {
        'version', 'key_id', 'kind', 'child_id', 'receipt_id', 'issued_at', 'expires_at', 'payload'}, 'ENVELOPE_FIELDS')
    require(envelope['version'] == 'rubric_authority_envelope_v1' and isinstance(envelope['kind'], str)
            and envelope['kind'] in KINDS and isinstance(envelope['key_id'], str)
            and envelope['child_id'] == child_id and isinstance(envelope['receipt_id'], str)
            and 0 < len(envelope['receipt_id']) <= 200, 'AUTHORITY_SCOPE_MISMATCH')
    content = encoded(envelope).encode()
    require(len(content) <= 2_000_000, 'ENVELOPE_TOO_LARGE')
    issuers = [i for i in trusted_issuers() if isinstance(i, dict) and i.get('key_id') == envelope['key_id']]
    require(len(issuers) == 1, 'SIGNER_NOT_TRUSTED')
    issuer = issuers[0]
    require(isinstance(issuer.get('kinds'), list) and isinstance(issuer.get('child_ids'), list)
            and envelope['kind'] in issuer['kinds'] and child_id in issuer['child_ids']
            and now < timestamp(issuer.get('expires_at')), 'SIGNER_SCOPE_EXPIRED')
    require(timestamp(envelope['issued_at']) <= now < timestamp(envelope['expires_at']), 'AUTHORITY_EXPIRED')
    try:
        check = _verify_signature if _signatures is None else _signatures.verify
        check(issuer['public_key_pem'], signature, content)
    except (InvalidSignature, ValueError, TypeError, KeyError, AttributeError):
        require(False, 'SIGNATURE_INVALID')
    payload = envelope['payload']
    require(isinstance(payload, dict) and payload.get('child_id') == child_id
            and payload.get('receipt_id') == envelope['receipt_id'] and payload.get('status') == 'current',
            'AUTHORITY_SCOPE_MISMATCH')
    _payload_shape(envelope['kind'], payload)
    return {**payload, 'signed_authority': {'envelope_hash': digest(envelope),
            'issuer_policy_hash': digest(issuer), 'key_id': envelope['key_id']}}


def rows(c, child_id):
    return [dict(r) for r in c.execute('SELECT * FROM audit_event WHERE child_id=? AND action IN (?,?,?) ORDER BY rowid',
                                     (child_id, *ACTIONS))]


def delete_for_child(c, child_id):
    require(c.in_transaction, 'TRANSACTION_REQUIRED')
    return c.execute('DELETE FROM audit_event WHERE child_id=? AND action IN (?,?,?)',
                     (child_id, *ACTIONS)).rowcount


def resolve(c, *, child_id, kind, reference_id, _signatures=None, _row_parser=None):
    if _signatures is not None:
        require(type(_signatures) is SignatureChecks, 'AUTHORITY_SIGNATURE_SCOPE_INVALID')
    if kind == 'assessment_scorer':
        from .rubric_calibration_host import actual_scorer_receipt
        return actual_scorer_receipt(c, child_id, reference_id)
    if _row_parser is not None:
        require(type(_row_parser) is _ParsedAuthorityRows, 'AUTHORITY_PARSE_SCOPE_INVALID')
        return _row_parser.resolve(c, child_id, kind, reference_id, _signatures)
    imports, invalid = [], set()
    for row in rows(c, child_id):
        try:
            value = json.loads(row['summary_json'])
            if row['action'] == IMPORT:
                envelope = value['envelope']
                if envelope['kind'] == kind and envelope['receipt_id'] == reference_id:
                    imports.append((row, value))
            else:
                invalid.add(value['import_id'])
        except (ValueError, KeyError, TypeError):
            return None
    if len(imports) != 1 or imports[0][0]['audit_id'] in invalid:
        return None
    try:
        return verify(child_id, imports[0][1]['envelope'], imports[0][1]['signature'], _signatures=_signatures)
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


def _owned(c, child_id):
    if not c.execute("SELECT 1 FROM child_profile WHERE child_id=? AND status='active'", (child_id,)).fetchone():
        raise KeyError(child_id)


def _append(c, child_id, action, payload, *, actor='parent'):
    event_id = str(uuid4())
    c.execute('INSERT INTO audit_event(audit_id,actor,action,child_id,summary_json,occurred_at) VALUES (?,?,?,?,?,?)',
              (event_id, actor, action, child_id, encoded(payload), datetime.now(timezone.utc).isoformat()))
    return event_id


def import_receipt(c, *, child_id, envelope, signature, client_action_id):
    require(c.in_transaction, 'TRANSACTION_REQUIRED')
    _owned(c, child_id)
    require(isinstance(client_action_id, str) and 0 < len(client_action_id) <= 200, 'IDEMPOTENCY_KEY_REQUIRED')
    request_hash = digest([envelope, signature])
    prior = rows(c, child_id)
    for row in prior:
        value = json.loads(row['summary_json'])
        if value.get('client_action_id') == client_action_id:
            require(row['action'] == IMPORT and value.get('request_hash') == request_hash, 'IDEMPOTENCY_CONFLICT')
            return _view(c, row['audit_id'], child_id, value)
    verify(child_id, envelope, signature)
    if envelope['kind']=='audio_holdout':
        from .rubric_audio_projection import validate_holdout_import
        validate_holdout_import(c,child_id,envelope)
    if envelope['kind']in {'audio_blind_display','audio_blind_label'}:
        from .rubric_audio_projection import validate_blind_import
        validate_blind_import(c,child_id,envelope,prior)
    for row in prior:
        previous = json.loads(row['summary_json']).get('envelope', {})
        require(previous.get('kind') != envelope['kind'] or previous.get('receipt_id') != envelope['receipt_id'],
                'AUTHORITY_REFERENCE_IMMUTABLE')
    value = {'envelope': envelope, 'signature': signature, 'request_hash': request_hash,
             'client_action_id': client_action_id}
    event_id = _append(c, child_id, IMPORT, value)
    return _view(c, event_id, child_id, value)


def _view(c, event_id, child_id, value):
    envelope = value['envelope']
    proof = resolve(c, child_id=child_id, kind=envelope['kind'], reference_id=envelope['receipt_id'])
    status = 'current' if proof else 'unavailable'
    for row in rows(c, child_id):
        if row['action'] in {REVOKE, QUARANTINE} and json.loads(row['summary_json']).get('import_id') == event_id:
            status = 'revoked' if row['action'] == REVOKE else 'restore_quarantined'
    return {'event_id': event_id, 'receipt_id': envelope['receipt_id'], 'kind': envelope['kind'],
            'envelope_hash': digest(envelope), 'status': status,
            'score_qualified': False, **LOCAL}


def revoke(c, *, child_id, kind, receipt_id, expected_hash, client_action_id):
    require(c.in_transaction, 'TRANSACTION_REQUIRED')
    _owned(c, child_id)
    require(isinstance(kind, str) and kind in KINDS and isinstance(receipt_id, str)
            and 0 < len(receipt_id) <= 200, 'AUTHORITY_SCOPE_MISMATCH')
    require(isinstance(client_action_id, str) and 0 < len(client_action_id) <= 200, 'IDEMPOTENCY_KEY_REQUIRED')
    request_hash = digest([kind, receipt_id, expected_hash])
    prior = rows(c, child_id)
    for row in prior:
        value = json.loads(row['summary_json'])
        if value.get('client_action_id') == client_action_id:
            require(row['action'] == REVOKE and value.get('request_hash') == request_hash, 'IDEMPOTENCY_CONFLICT')
            return {'event_id': row['audit_id'], 'status': 'revoked', **LOCAL}
    matches = [(r, json.loads(r['summary_json'])) for r in prior if r['action'] == IMPORT]
    matches = [(r, v) for r, v in matches if v['envelope']['kind'] == kind and v['envelope']['receipt_id'] == receipt_id]
    require(len(matches) == 1 and digest(matches[0][1]['envelope']) == expected_hash, 'AUTHORITY_VERSION_CHANGED')
    event_id = _append(c, child_id, REVOKE, {'import_id': matches[0][0]['audit_id'],
                       'client_action_id': client_action_id, 'request_hash': request_hash})
    return {'event_id': event_id, 'status': 'revoked', **LOCAL}


def quarantine_after_restore(c):
    require(c.in_transaction, 'TRANSACTION_REQUIRED')
    imports = c.execute('SELECT audit_id,child_id FROM audit_event WHERE action=?', (IMPORT,)).fetchall()
    for row in imports:
        previous = rows(c, row['child_id'])
        if not any(r['action'] == QUARANTINE and json.loads(r['summary_json']).get('import_id') == row['audit_id'] for r in previous):
            _append(c, row['child_id'], QUARANTINE, {'import_id': row['audit_id'], 'reason': 'backup_restore'}, actor='system')
    return len(imports)
