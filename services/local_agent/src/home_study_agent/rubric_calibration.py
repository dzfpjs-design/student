"""Local scoring qualification. Assessments remain the only score journal.

The injected authority resolver reads authenticated, durable host receipts. It is
not an HTTP argument and must never turn caller metadata into provenance.
"""
from datetime import datetime, timedelta, timezone
import json
import re
from uuid import uuid4

from .rubric_calibration_evidence import (
    POLICY, assessment, digest, dimensions, encoded, is_hash, require,
    score_values, source_snapshot, summarize_cases, timestamp,
)
from .rubric_calibration_transaction import calibration_connection, scoped_validation

VERSION = 'rubric_calibration_v1'
TABLES = ('rubric_calibration_protocol', 'rubric_calibration_event')
SCORER_FIELDS = frozenset(('provider', 'model', 'model_revision', 'revision_receipt_id',
    'prompt_hash', 'profile_id', 'profile_version', 'rubric_hash', 'parser_version',
    'projection_version', 'parameters_hash', 'modality'))


def initialize(c):
    """DDL only; the caller owns commit/rollback, including staged restore."""
    c.execute('''CREATE TABLE IF NOT EXISTS rubric_calibration_protocol (
        protocol_id TEXT PRIMARY KEY, child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
        version TEXT NOT NULL, scorer_json TEXT NOT NULL, scope_json TEXT NOT NULL,
        manifest_json TEXT NOT NULL, protocol_hash TEXT NOT NULL, created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS rubric_calibration_event (
        event_id TEXT PRIMARY KEY, protocol_id TEXT NOT NULL REFERENCES rubric_calibration_protocol(protocol_id) ON DELETE CASCADE,
        child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
        revision INTEGER NOT NULL CHECK(revision>0), operation TEXT NOT NULL,
        reference_json TEXT NOT NULL, request_hash TEXT NOT NULL, client_action_id TEXT NOT NULL,
        created_at TEXT NOT NULL, UNIQUE(protocol_id,revision), UNIQUE(child_id,client_action_id))''')
    for table in TABLES:
        c.execute(f'''CREATE TRIGGER IF NOT EXISTS {table}_no_update BEFORE UPDATE ON {table}
            BEGIN SELECT RAISE(ABORT,'RUBRIC_CALIBRATION_IMMUTABLE'); END''')


def _id(value):
    require(isinstance(value, str) and bool(re.fullmatch(r'[A-Za-z0-9_.:/-]{1,200}', value)), 'REFERENCE_INVALID')
    return value


def _exists(c):
    return c.execute("SELECT 1 FROM sqlite_master WHERE name='rubric_calibration_protocol'").fetchone() is not None


def _source_versions(c, child_id, source_ids):
    result = {}
    for sid in source_ids:
        row = c.execute("SELECT * FROM knowledge_source WHERE child_id=? AND source_id=? AND review_status='approved'",
                        (child_id, sid)).fetchone()
        require(row is not None, 'SOURCE_NOT_CURRENT')
        result[sid] = digest(dict(row))
    return result


def _event(c, row, operation, reference, key, request_hash, now):
    revision = c.execute('SELECT COALESCE(MAX(revision),0)+1 FROM rubric_calibration_event WHERE protocol_id=?',
                         (row['protocol_id'],)).fetchone()[0]
    value = {'event_id': str(uuid4()), 'protocol_id': row['protocol_id'], 'revision': revision,
             'operation': operation, 'reference': reference, 'created_at': now}
    c.execute('INSERT INTO rubric_calibration_event VALUES (?,?,?,?,?,?,?,?,?)',
        (value['event_id'], row['protocol_id'], row['child_id'], revision, operation,
         encoded(reference), request_hash, key, now))
    return value


def _public_event(row):
    return {k: row[k] for k in ('event_id', 'protocol_id', 'revision', 'operation', 'created_at')} | {
        'reference': json.loads(row['reference_json'])}


def export_rows(c, child_id):
    if not _exists(c):
        return {'protocols': [], 'events': []}
    return {'protocols': [dict(r) for r in c.execute('SELECT * FROM rubric_calibration_protocol WHERE child_id=?', (child_id,))],
            'events': [dict(r) for r in c.execute('SELECT * FROM rubric_calibration_event WHERE child_id=? ORDER BY rowid', (child_id,))]}


def delete_for_child(c, child_id):
    if not _exists(c):
        return 0
    n = c.execute('DELETE FROM rubric_calibration_event WHERE child_id=?', (child_id,)).rowcount
    return n + c.execute('DELETE FROM rubric_calibration_protocol WHERE child_id=?', (child_id,)).rowcount


def quarantine_after_restore(c, restore_id):
    _id(restore_id)
    from .rubric_calibration_authority import quarantine_after_restore as quarantine_authorities
    quarantine_authorities(c)
    if not _exists(c):
        return 0
    count, now = 0, datetime.now(timezone.utc).isoformat()
    for row in c.execute('SELECT * FROM rubric_calibration_protocol').fetchall():
        if c.execute("SELECT 1 FROM rubric_calibration_event WHERE protocol_id=? AND operation='restore_quarantine'",
                     (row['protocol_id'],)).fetchone():
            continue
        _event(c, row, 'restore_quarantine', {'restore_id': restore_id},
               'restore:' + digest([restore_id, row['protocol_id']]), digest(['restore', restore_id]), now)
        count += 1
    return count


def invalidate_sources(c, child_id, source_ids, *, erasure_id):
    """Borrow the deletion transaction. No raw material or scores are copied."""
    _id(erasure_id)
    if not _exists(c):
        return 0
    sources = set(source_ids)
    count = 0
    for row in c.execute('SELECT * FROM rubric_calibration_protocol WHERE child_id=?', (child_id,)).fetchall():
        manifest = json.loads(row['manifest_json'])
        if manifest.get('schema') == 'original_audio_protocol_v2':
            linked = set(manifest['application_source_ids']) | {s for case in manifest['cases'] for s in case['source_ids']}
        else:
            linked = {s for case in manifest['cases'] for s in case['source_ids']}
        if not sources & linked:
            continue
        key = 'erasure:' + digest([erasure_id, row['protocol_id']])
        if c.execute('SELECT 1 FROM rubric_calibration_event WHERE child_id=? AND client_action_id=?', (child_id, key)).fetchone():
            continue
        _event(c, row, 'source_invalidated', {'erasure_id': erasure_id, 'source_ids': sorted(sources & linked)},
               key, digest([erasure_id, sorted(sources)]), datetime.now(timezone.utc).isoformat())
        count += 1
    return count


class RubricCalibrationService:
    def __init__(self, database, *, authority_resolver=None, clock=None):
        self.database = database
        self.authority_resolver = authority_resolver
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def initialize(self):
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            initialize(c)
            c.commit()

    def _now(self):
        return timestamp(self.clock())

    def _resolve(self, c, child_id, kind, reference_id, *, _scope=None):
        if _scope is not None:
            resolver = _scope.resolve
        elif self.authority_resolver is None:
            from .rubric_calibration_authority import resolve
            resolver = resolve
        else:
            resolver = self.authority_resolver
        value = resolver(c, child_id=child_id, kind=kind, reference_id=reference_id)
        if value is None:
            return None
        require(isinstance(value, dict) and value.get('child_id') == child_id
                and value.get('receipt_id') == reference_id, 'AUTHORITY_SCOPE_MISMATCH')
        return value

    @staticmethod
    def _owned(c, child_id, protocol_id):
        row = c.execute('''SELECT p.* FROM rubric_calibration_protocol p JOIN child_profile ch USING(child_id)
            WHERE p.child_id=? AND p.protocol_id=? AND ch.status='active' ''', (child_id, protocol_id)).fetchone()
        if not row:
            raise KeyError(protocol_id)
        return row

    def _revision(self, c, child_id, scorer, now, *, _scope=None):
        proof = self._resolve(c, child_id, 'scorer_revision', scorer['revision_receipt_id'], _scope=_scope)
        if not proof or not scorer['model_revision']:
            return None
        require(proof.get('status') == 'current' and proof.get('verification_method') in
                {'provider_immutable_revision_attestation', 'signed_release_manifest'}, 'MODEL_REVISION_UNVERIFIED')
        require(all(proof.get(k) == scorer[k] for k in ('provider', 'model', 'model_revision'))
                and is_hash(proof.get('evidence_hash')) and is_hash(proof.get('endpoint_scope_sha256'))
                and timestamp(proof['verified_at']) <= now < timestamp(proof['expires_at']),
                'MODEL_REVISION_UNVERIFIED')
        return digest(proof)

    def freeze(self, child_id, *, scorer, scope, holdout_receipt_id, client_action_id):
        if type(scorer) is dict and scorer.get('modality') == 'original_audio':
            return self._freeze_original_audio(child_id, scorer=scorer, scope=scope, holdout_receipt_id=holdout_receipt_id, client_action_id=client_action_id)
        _id(client_action_id)
        _id(holdout_receipt_id)
        require(isinstance(scorer, dict) and set(scorer) == SCORER_FIELDS, 'SCORER_FIELDS')
        require(all(is_hash(scorer[k]) for k in ('prompt_hash', 'rubric_hash', 'parameters_hash')), 'SCORER_HASH_REQUIRED')
        for key in SCORER_FIELDS - {'prompt_hash', 'rubric_hash', 'parameters_hash', 'model_revision'}:
            _id(scorer[key])
        if scorer['model_revision'] is not None:
            _id(scorer['model_revision'])
        require(scorer['modality'] in {'typed_text', 'original_written_text', 'original_spoken_text'}, 'MODALITY_NOT_SUPPORTED')
        require(isinstance(scope, dict) and set(scope) == {'subject', 'grade', 'task_family', 'strata'}
                and isinstance(scope['strata'], list) and 1 <= len(scope['strata']) <= 20
                and len(set(scope['strata'])) == len(scope['strata']), 'SCOPE_FIELDS')
        _id(scope['task_family'])
        for value in scope['strata']:
            _id(value)
        request_hash = digest(['freeze', scorer, scope, holdout_receipt_id])
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            child = c.execute("SELECT * FROM child_profile WHERE child_id=? AND status='active'", (child_id,)).fetchone()
            if not child:
                raise KeyError(child_id)
            require(child['grade'] == scope['grade'] and isinstance(scope['subject'], str)
                    and 0 < len(scope['subject']) <= 20, 'SCOPE_MISMATCH')
            replay = self._replay(c, child_id, client_action_id, request_hash)
            if replay:
                return replay
            now = self._now()
            revision_hash = self._revision(c, child_id, scorer, now)
            proof = self._resolve(c, child_id, 'holdout', holdout_receipt_id)
            cases = []
            if proof:
                require(proof.get('status') == 'current' and proof.get('corpus_kind') == 'observed_original'
                        and proof.get('blind_protocol_version') == 'blind_labels_v1'
                        and is_hash(proof.get('provenance_receipt_hash')) and proof.get('scope_hash') == digest(scope)
                        and timestamp(proof['frozen_at']) <= now, 'HOLDOUT_UNVERIFIED')
                require(isinstance(proof.get('cases'), list) and 1 <= len(proof['cases']) <= 10000, 'HOLDOUT_CASES_REQUIRED')
                groups, ids = set(), set()
                for case in proof['cases']:
                    require(set(case) == {'evidence_id', 'group_hash', 'stratum', 'source_ids'}, 'CASE_FIELDS')
                    _id(case['evidence_id'])
                    require(is_hash(case['group_hash']) and case['group_hash'] not in groups
                            and case['group_hash'] not in proof.get('development_group_hashes', [])
                            and case['evidence_id'] not in ids and case['stratum'] in scope['strata'], 'HOLDOUT_LEAKAGE')
                    require(isinstance(case['source_ids'], list)
                            and len(set(case['source_ids'])) == len(case['source_ids']), 'SOURCE_IDS_REQUIRED')
                    for source_id in case['source_ids']:
                        _id(source_id)
                    source = source_snapshot(c, child_id, case['evidence_id'])
                    require(source['subject'] == scope['subject'] and source['grade'] == scope['grade']
                            and source['modality'] == scorer['modality'], 'CASE_SCOPE_MISMATCH')
                    require(not c.execute('''SELECT 1 FROM rubric_assessment WHERE child_id=? AND model_call_id!=''
                        AND json_extract(payload_json,'$.source_evidence_id')=?''', (child_id, case['evidence_id'])).fetchone(), 'HOLDOUT_ALREADY_SCORED')
                    cases.append({**case, 'source_version': source['version'], 'diagnostic_item_id': source['diagnostic_item_id'],
                                  'source_versions': _source_versions(c, child_id, case['source_ids'])})
                    ids.add(case['evidence_id'])
                    groups.add(case['group_hash'])
            manifest = {'holdout_receipt_id': holdout_receipt_id, 'holdout_hash': digest(proof) if proof else None,
                        'revision_hash': revision_hash, 'cases': cases, 'policy_hash': digest(POLICY)}
            pid = str(uuid4())
            expires = (now + timedelta(days=POLICY['max_valid_days'])).isoformat()
            phash = digest([VERSION, child_id, scorer, scope, manifest, now.isoformat(), expires])
            c.execute('INSERT INTO rubric_calibration_protocol VALUES (?,?,?,?,?,?,?,?,?)',
                (pid, child_id, VERSION, encoded(scorer), encoded(scope), encoded(manifest), phash, now.isoformat(), expires))
            row = self._owned(c, child_id, pid)
            receipt = _event(c, row, 'freeze', {}, client_action_id, request_hash, now.isoformat())
            result = {'receipt': receipt, 'current': self._read(c, row, now)}
            c.commit()
            return result

    def _freeze_original_audio(self, child_id, *, scorer, scope, holdout_receipt_id, client_action_id):
        if scorer.get('schema') == 'typed_cpu_original_audio_scorer_v2':
            from .rubric_audio_projection import freeze_v2
            return freeze_v2(self,child_id,scorer_value=scorer,scope=scope,holdout_receipt_id=holdout_receipt_id,client_action_id=client_action_id)
        from .rubric_calibration_evidence import audio_selector, audio_candidate_source
        selector = audio_selector(scorer)
        _id(client_action_id); _id(holdout_receipt_id)
        require(type(scope) is dict and set(scope) == {'subject', 'grade', 'task_family', 'strata'}
                and type(scope['strata']) is list and 1 <= len(scope['strata']) <= 20
                and len(set(scope['strata'])) == len(scope['strata']), 'SCOPE_FIELDS')
        require(all(type(scope[k]) is str and 0 < len(scope[k]) <= 200 for k in ('subject', 'grade')), 'SCOPE_FIELDS')
        for value in (scope['task_family'], *scope['strata']):
            _id(value)
        request_hash = digest(['freeze_original_audio', selector, scope, holdout_receipt_id])
        from .pet_multipa_execution_receipt import install_read_scope
        with calibration_connection(self.database) as c, install_read_scope(c):
            c.execute('BEGIN IMMEDIATE')
            replay = self._replay(c, child_id, client_action_id, request_hash)
            if replay:
                return replay
            source = audio_candidate_source(c, child_id, **{k: selector[k] for k in selector if k != 'modality'})
            require(source['task_family'] == scope['task_family'], 'AUDIO_TASK_SCOPE_CHANGED')
            item = c.execute('SELECT k.subject FROM diagnostic_item i JOIN knowledge_node k USING(knowledge_node_id) WHERE i.diagnostic_item_id=?', (source['identity']['diagnostic_item_id'],)).fetchone()
            child = c.execute('SELECT grade FROM child_profile WHERE child_id=?', (child_id,)).fetchone()
            require(item and child and item['subject'] == scope['subject'] and child['grade'] == scope['grade'], 'AUDIO_TASK_SCOPE_CHANGED')
            now = self._now(); expires = (now + timedelta(days=POLICY['max_valid_days'])).isoformat()
            manifest = {'schema': 'original_audio_unqualified_protocol_v1', 'source': source,
                        'holdout_receipt_id': holdout_receipt_id, 'holdout_verified': False,
                        'cases': [], 'policy_hash': digest(POLICY)}
            pid = str(uuid4())
            phash = digest([VERSION, child_id, selector, scope, manifest, now.isoformat(), expires])
            c.execute('INSERT INTO rubric_calibration_protocol VALUES (?,?,?,?,?,?,?,?,?)',
                (pid, child_id, VERSION, encoded(selector), encoded(scope), encoded(manifest), phash, now.isoformat(), expires))
            row = self._owned(c, child_id, pid)
            receipt = _event(c, row, 'freeze', {}, client_action_id, request_hash, now.isoformat())
            result = {'receipt': receipt, 'current': self._read_original_audio(c, row, now)}
            c.commit()
            return result

    def _read_original_audio(self, c, row, now, *, _require_live=False):
        if json.loads(row['scorer_json']).get('schema') == 'typed_cpu_original_audio_scorer_v2':
            from .rubric_audio_projection import read_v2
            return read_v2(self,c,row,now,require_live=_require_live)
        from .rubric_calibration_evidence import audio_selector, audio_candidate_source, AUDIO_MISSING_AUTHORITIES
        from .rubric_calibration_transaction import unsupported_dependency
        unsupported_dependency()
        from .pet_multipa_execution_receipt import install_read_scope,_READ_SCOPE
        if _READ_SCOPE.get()is None:
            with install_read_scope(c):return self._read_original_audio(c,row,now,_require_live=_require_live)
        if not c.in_transaction:
            c.execute('BEGIN')
            try:
                return self._read_original_audio(c, row, now, _require_live=_require_live)
            finally:
                c.rollback()
        selector = audio_selector(json.loads(row['scorer_json']))
        manifest = json.loads(row['manifest_json'])
        require(manifest.get('schema') == 'original_audio_unqualified_protocol_v1', 'AUDIO_PROTOCOL_INVALID')
        require(row['protocol_hash'] == digest([VERSION, row['child_id'], selector, json.loads(row['scope_json']), manifest, row['created_at'], row['expires_at']]), 'AUDIO_PROTOCOL_INVALID')
        events = c.execute('SELECT * FROM rubric_calibration_event WHERE protocol_id=? ORDER BY revision', (row['protocol_id'],)).fetchall()
        operations = {e['operation'] for e in events}
        blockers = set(AUDIO_MISSING_AUTHORITIES) | {'original_audio_holdout_unverified'}
        status = 'pending_unqualified'
        if 'restore_quarantine' in operations:
            status = 'quarantined'; blockers.add('restore_revalidation_required')
        elif 'revoke' in operations or 'source_invalidated' in operations:
            status = 'revoked'; blockers.add('qualification_revoked')
        else:
            try:
                source = audio_candidate_source(c, row['child_id'], **{k: selector[k] for k in selector if k != 'modality'})
                if source['held_execution_verified']:blockers.discard('held_execution_attestation_missing')
                blockers.update(source['blockers'])
                require(source == manifest['source'], 'AUDIO_FROZEN_SOURCE_CHANGED')
            except (ValueError, KeyError, TypeError, OSError):
                status = 'invalidated'; blockers.add('original_audio_source_changed_or_unavailable')
        if now < timestamp(row['created_at']) or now >= timestamp(row['expires_at']):
            status = 'invalidated'; blockers.add('protocol_expired')
        if manifest['policy_hash'] != digest(POLICY) or row['version'] != VERSION:
            status = 'invalidated'; blockers.add('policy_changed')
        if 'qualify' in operations or 'record_case' in operations:
            status = 'invalidated'; blockers.add('original_audio_qualification_not_supported')
        if _require_live:
            require(False, 'AUDIO_PROJECTION_UNAVAILABLE')
        return {'protocol_id': row['protocol_id'], 'protocol_hash': row['protocol_hash'],
            'revision': events[-1]['revision'] if events else 0, 'status': status,
            'score_qualified': False, 'mastery_claim_allowed': False, 'certification_claim_allowed': False,
            'blockers': sorted(blockers), 'scorer': selector, 'scope': json.loads(row['scope_json']),
            'source': manifest['source'], 'metrics': {}, 'expires_at': row['expires_at'],
            'requirements': self.requirements(), 'local_only': True, 'cloud_transmission_allowed': False}

    def _replay(self, c, child_id, key, request_hash):
        old = c.execute('SELECT * FROM rubric_calibration_event WHERE child_id=? AND client_action_id=?', (child_id, key)).fetchone()
        if old:
            require(old['request_hash'] == request_hash, 'IDEMPOTENCY_CONFLICT')
            return {'receipt': _public_event(old), 'current': self._read(c, self._owned(c, child_id, old['protocol_id']), self._now())}
        return None

    def read(self, child_id, protocol_id):
        with self.database.connect() as c:
            c.execute('BEGIN')
            return self._read(c, self._owned(c, child_id, protocol_id), self._now())

    def requirements(self):
        from .rubric_calibration_authority import trusted_issuers
        return {'required_scorer_fields': sorted(SCORER_FIELDS), 'policy': json.loads(encoded(POLICY)),
                'blind_identity': 'independent_authenticated_reviewers_required',
                'supported_modalities': ['typed_text', 'original_written_text'],
                'required_receipt_kinds': ['scorer_revision', 'holdout', 'blind_label', 'assessment_scorer'],
                'authority_resolver_configured': self.authority_resolver is not None or bool(trusted_issuers()),
                'production_authority_available': False}

    def list_for_child(self, child_id, *, limit=50):
        require(type(limit) is int and 1 <= limit <= 100, 'LIMIT_INVALID')
        with self.database.connect() as c:
            c.execute('BEGIN')
            if not c.execute("SELECT 1 FROM child_profile WHERE child_id=? AND status='active'", (child_id,)).fetchone():
                raise KeyError(child_id)
            rows = c.execute('SELECT * FROM rubric_calibration_protocol WHERE child_id=? ORDER BY rowid DESC LIMIT ?',
                             (child_id, limit)).fetchall() if _exists(c) else []
            items = [self._read(c, r, self._now()) for r in rows]
            requirements = self.requirements()
            requirements['production_authority_available'] = any(item['score_qualified'] for item in items)
            return {'items': items, 'requirements': requirements,
                    'local_only': True, 'cloud_transmission_allowed': False}

    def _live(self, c, row, now, *, _scope=None):
        manifest, scorer = json.loads(row['manifest_json']), json.loads(row['scorer_json'])
        blockers = []
        if now < timestamp(row['created_at']) or now >= timestamp(row['expires_at']):
            blockers.append('protocol_expired')
        if manifest['policy_hash'] != digest(POLICY) or row['version'] != VERSION:
            blockers.append('policy_changed')
        proof = self._resolve(c, row['child_id'], 'holdout', manifest['holdout_receipt_id'], _scope=_scope)
        if not manifest['holdout_hash'] or not proof or digest(proof) != manifest['holdout_hash']:
            blockers.append('holdout_unverified')
        try:
            current_revision = self._revision(c, row['child_id'], scorer, now, _scope=_scope)
            if not current_revision or current_revision != manifest['revision_hash']:
                blockers.append('model_revision_unverified')
        except (ValueError, KeyError):
            blockers.append('model_revision_unverified')
        for case in manifest['cases']:
            try:
                source = _scope.sources.get(case['evidence_id']) if _scope is not None else None
                if source is None:
                    source = source_snapshot(c, row['child_id'], case['evidence_id'])
                if source['version'] != case['source_version']:
                    blockers.append('source_changed')
                if _source_versions(c, row['child_id'], case['source_ids']) != case['source_versions']:
                    blockers.append('source_changed')
            except (ValueError, KeyError):
                blockers.append('source_changed')
        return blockers

    def _scored(self, c, row, assessment_id, now, *, _scope=None):
        current, call, source = assessment(c, row['child_id'], assessment_id)
        scorer = json.loads(row['scorer_json'])
        expected = {'protocol_id': row['protocol_id'], 'protocol_hash': row['protocol_hash'],
                    'scorer_hash': digest(scorer), 'source_version': source['version']}
        require(current.get('calibration_binding') == expected, 'ASSESSMENT_BINDING_CHANGED')
        require(current['profile'] == scorer['profile_id'] and current['rubric_version'] == scorer['profile_version']
                and digest(dimensions(current)) == scorer['rubric_hash'], 'RUBRIC_CHANGED')
        scope = json.loads(row['scope_json'])
        require(source['subject'] == scope['subject'] and source['grade'] == scope['grade']
                and source['modality'] == scorer['modality'], 'ASSESSMENT_SCOPE_CHANGED')
        require(timestamp(row['created_at']) <= timestamp(call['created_at']) <= timestamp(current['created_at']) <= now,
                'ASSESSMENT_BEFORE_PROTOCOL')
        proof = self._resolve(c, row['child_id'], 'assessment_scorer', call['call_id'], _scope=_scope)
        require(proof and proof.get('status') == 'current' and proof.get('scorer_hash') == digest(scorer)
                and proof.get('protocol_hash') == row['protocol_hash']
                and proof.get('scope_hash') == digest(scope) and proof.get('stratum') in scope['strata']
                and proof.get('model_revision') == scorer['model_revision'] and is_hash(proof.get('provenance_hash'))
                and call['provider'] == scorer['provider'] and call['model'] == scorer['model'], 'ACTUAL_SCORER_UNVERIFIED')
        provenance = current.get('scorer_provenance')
        revision = self._resolve(c, row['child_id'], 'scorer_revision', scorer['revision_receipt_id'], _scope=_scope)
        require(isinstance(provenance, dict) and provenance.get('version') == 'model_scorer_provenance_v1'
                and digest(provenance) == proof['provenance_hash']
                and provenance.get('provider') == scorer['provider'] and provenance.get('requested_model') == scorer['model']
                and provenance.get('system_prompt_sha256') == scorer['prompt_hash']
                and provenance.get('parameters_sha256') == scorer['parameters_hash']
                and revision and provenance.get('endpoint_scope_sha256') == revision.get('endpoint_scope_sha256'),
                'ACTUAL_PROVENANCE_MISMATCH')
        return current, call, source, digest(proof)

    def _case(self, c, row, reference, now, *, _scope=None, _manifest=None):
        if json.loads(row['scorer_json']).get('schema') == 'typed_cpu_original_audio_scorer_v2':
            from .rubric_audio_projection import case_v2
            return case_v2(self,c,row,reference,now)
        current, call, source, scorer_proof = self._scored(c, row, reference['assessment_id'], now, _scope=_scope)
        manifest = json.loads(row['manifest_json']) if _manifest is None else _manifest
        candidates = [v for v in manifest['cases'] if v['evidence_id'] == source['evidence_id']]
        require(len(candidates) == 1 and candidates[0]['source_version'] == source['version'], 'CASE_NOT_IN_HOLDOUT')
        predictions = score_values(current, current['dimensions'])
        labels, principals, reviewers, refs = [], set(), set(), []
        require(len(reference['blind_receipt_ids']) == 2 and len(set(reference['blind_receipt_ids'])) == 2, 'TWO_BLIND_LABELS_REQUIRED')
        for rid in reference['blind_receipt_ids']:
            proof = self._resolve(c, row['child_id'], 'blind_label', rid, _scope=_scope)
            require(proof and proof.get('status') == 'current'
                    and proof.get('identity_authority') == 'independent_reviewer_registry_v1'
                    and is_hash(proof.get('identity_receipt_hash')) and is_hash(proof.get('display_receipt_hash'))
                    and is_hash(proof.get('submission_receipt_hash')) and proof.get('mask_version') == 'blind_no_predictions_or_peer_labels_v1',
                    'BLIND_IDENTITY_UNVERIFIED')
            require(proof.get('protocol_hash') == row['protocol_hash'] and proof.get('assessment_fingerprint') == current['fingerprint']
                    and proof.get('evidence_id') == source['evidence_id'] and proof.get('source_version') == source['version'], 'BLIND_SOURCE_CHANGED')
            principal = _id(proof.get('reviewer_principal_id'))
            require(principal not in principals, 'INDEPENDENT_REVIEWERS_REQUIRED')
            label = c.execute('''SELECT * FROM rubric_review_event WHERE child_id=? AND assessment_id=? AND event_id=?''',
                              (row['child_id'], current['assessment_id'], proof.get('review_event_id'))).fetchone()
            require(label is not None and label['operation'] == 'approve' and label['reviewer_role'] in {'teacher', 'parent'}, 'LABEL_EVENT_REQUIRED')
            require(label['reviewer_id'] not in reviewers, 'INDEPENDENT_REVIEWERS_REQUIRED')
            require(timestamp(row['created_at']) <= timestamp(proof['displayed_at']) <= timestamp(proof['submitted_at'])
                    == timestamp(label['created_at']) <= now and proof.get('reviewer_id') == label['reviewer_id'], 'BLIND_TIMING_INVALID')
            label_payload = json.loads(label['payload_json'])
            require(proof.get('review_event_hash') == digest(dict(label)), 'LABEL_CHANGED')
            labels.append(score_values(current, label_payload['dimensions']))
            refs.append([rid, digest(proof), label['event_id'], digest(dict(label))])
            principals.add(principal)
            reviewers.add(label['reviewer_id'])
        require(labels[0] == labels[1] and all(v is not None for v in labels[0].values()), 'LABEL_DISAGREEMENT')
        latest = c.execute('SELECT event_id FROM rubric_review_event WHERE child_id=? AND assessment_id=? ORDER BY revision DESC LIMIT 1',
                           (row['child_id'], current['assessment_id'])).fetchone()
        require(latest and latest[0] in {r[2] for r in refs}, 'LABEL_SUPERSEDED')
        if _scope is not None:
            _scope.sources[source['evidence_id']] = source
        return {'stratum': candidates[0]['stratum'], 'group_hash': candidates[0]['group_hash'],
                'evidence_id': source['evidence_id'], 'gold': labels[0], 'predicted': predictions,
                'fingerprint': digest([current['fingerprint'], source['version'], scorer_proof, refs])}

    def _read(self, c, row, now, *, _require_live=False):
        if json.loads(row['scorer_json']).get('modality') == 'original_audio':
            return self._read_original_audio(c, row, now, _require_live=_require_live)
        if not c.in_transaction:
            c.execute('BEGIN')
            try:
                return self._read(c, row, now, _require_live=_require_live)
            finally:
                c.rollback()
        from .rubric_calibration_rows import ReadRows
        from .rubric_calibration_transaction import read_current
        def read_full(connection):
            with ReadRows(connection) as rows:
                return self._read_rows(rows, row, now, _require_live=_require_live)
        return read_current(self, c, row, now, read_full)

    def _read_rows(self, c, row, now, *, _require_live=False):
        from .rubric_calibration_read import ReadScope
        scope_read = ReadScope(c, row['child_id'], self.authority_resolver)
        events = c.execute('SELECT * FROM rubric_calibration_event WHERE protocol_id=? ORDER BY revision', (row['protocol_id'],)).fetchall()
        operations = {e['operation'] for e in events}
        status, blockers, metrics = 'insufficient', [], {}
        if 'restore_quarantine' in operations:
            status, blockers = 'quarantined', ['restore_revalidation_required']
        elif 'revoke' in operations or 'source_invalidated' in operations:
            status, blockers = 'revoked', ['qualification_revoked']
        else:
            manifest = json.loads(row['manifest_json'])
            cases, seen = [], set()
            for event in events:
                if event['operation'] != 'record_case':
                    continue
                ref = json.loads(event['reference_json'])
                try:
                    case = self._case(c, row, ref, now, _scope=scope_read, _manifest=manifest)
                    require(case['fingerprint'] == ref['case_fingerprint'] and case['group_hash'] not in seen, 'CASE_CHANGED')
                    cases.append(case)
                    seen.add(case['group_hash'])
                except (ValueError, KeyError, TypeError) as error:
                    blockers.append(str(error).replace('RUBRIC_CALIBRATION_', '').lower())
            live_blockers = self._live(c, row, now, _scope=scope_read)
            if _require_live:
                require(not live_blockers, 'EXECUTION_AUTHORITY_CHANGED')
            blockers.extend(live_blockers)
            scope = json.loads(row['scope_json'])
            if len(cases) != len(manifest['cases']) or not cases:
                blockers.append('holdout_incomplete')
            if cases:
                metrics, failures = summarize_cases(cases, list(cases[0]['gold']), scope['strata'])
                blockers.extend(failures)
            qualified = [e for e in events if e['operation'] == 'qualify']
            if qualified:
                last = json.loads(qualified[-1]['reference_json'])
                if last['case_digest'] != digest([v['fingerprint'] for v in cases]):
                    blockers.append('certificate_basis_changed')
                status = 'invalidated' if blockers else 'qualified'
            elif not blockers:
                status = 'ready_for_evaluation'
        scope_read.finish()
        if _require_live:
            require(status not in {'revoked', 'quarantined'}, 'EXECUTION_AUTHORITY_CHANGED')
        return {'protocol_id': row['protocol_id'], 'protocol_hash': row['protocol_hash'], 'revision': events[-1]['revision'] if events else 0,
            'status': status, 'score_qualified': status == 'qualified', 'blockers': sorted(set(blockers)),
            'scorer': json.loads(row['scorer_json']), 'scope': json.loads(row['scope_json']), 'metrics': metrics,
            'expires_at': row['expires_at'], 'requirements': self.requirements(),
            'local_only': True, 'cloud_transmission_allowed': False}

    @scoped_validation
    def _command(self, child_id, protocol_id, operation, reference, expected_revision, key):
        _id(key)
        require(type(expected_revision) is int and expected_revision >= 1, 'REVISION_INVALID')
        request_hash = digest([operation, protocol_id, reference, expected_revision])
        from .database import Database
        prepare = operation == 'revoke' and type(self.database) is Database and self.authority_resolver is None
        if prepare:
            with calibration_connection(self.database) as c:
                c.execute('BEGIN')
                row = self._owned(c, child_id, protocol_id)
                if not self._replay(c, child_id, key, request_hash):
                    self._read(c, row, self._now())
                c.rollback()
        connection = calibration_connection(self.database) if prepare else self.database.connect()
        with connection as c:
            c.execute('BEGIN IMMEDIATE')
            row = self._owned(c, child_id, protocol_id)
            if json.loads(row['scorer_json']).get('schema') == 'typed_cpu_original_audio_scorer_v2':
                from .rubric_audio_projection import command_v2
                return command_v2(self,c,row,operation,reference,expected_revision,key,request_hash)
            replay = self._replay(c, child_id, key, request_hash)
            if replay:
                return replay
            now = self._now()
            before = self._read(c, row, now)
            require(before['revision'] == expected_revision, 'REVISION_CONFLICT')
            require(before['status'] not in {'revoked', 'quarantined'}
                    and (operation == 'revoke' or before['status'] != 'invalidated'), 'PROTOCOL_INACTIVE')
            if operation == 'record_case':
                if json.loads(row['scorer_json']).get('modality') == 'original_audio' and json.loads(row['scorer_json']).get('schema') != 'typed_cpu_original_audio_scorer_v2':
                    require(False, 'AUDIO_PROJECTION_UNAVAILABLE')
                require(not c.execute("SELECT 1 FROM rubric_calibration_event WHERE protocol_id=? AND operation='qualify'", (protocol_id,)).fetchone(), 'PROTOCOL_SEALED')
                result = self._case(c, row, reference, now)
                existing = c.execute("SELECT reference_json FROM rubric_calibration_event WHERE protocol_id=? AND operation='record_case'", (protocol_id,)).fetchall()
                require(not any(json.loads(e[0])['evidence_id'] == result['evidence_id'] for e in existing), 'CASE_ALREADY_REGISTERED')
                reference = {**reference, 'case_fingerprint': result['fingerprint'], 'evidence_id': result['evidence_id']}
            if operation == 'evaluate':
                if before['status'] in {'ready_for_evaluation', 'qualified'}:
                    refs = c.execute("SELECT reference_json FROM rubric_calibration_event WHERE protocol_id=? AND operation='record_case' ORDER BY revision", (protocol_id,)).fetchall()
                    reference = {'case_digest': digest([json.loads(r[0])['case_fingerprint'] for r in refs]), 'policy_hash': digest(POLICY)}
                    operation = 'qualify'
                else:
                    reference = {'blockers': before['blockers']}
            receipt = _event(c, row, operation, reference, key, request_hash, now.isoformat())
            result = {'receipt': receipt, 'current': self._read(c, row, now)}
            c.commit()
            return result

    def record_case(self, child_id, protocol_id, *, assessment_id, blind_receipt_ids, expected_revision, client_action_id):
        _id(assessment_id)
        require(isinstance(blind_receipt_ids, list) and len(blind_receipt_ids) == 2, 'TWO_BLIND_LABELS_REQUIRED')
        for rid in blind_receipt_ids:
            _id(rid)
        return self._command(child_id, protocol_id, 'record_case', {'assessment_id': assessment_id,
                             'blind_receipt_ids': blind_receipt_ids}, expected_revision, client_action_id)

    def evaluate(self, child_id, protocol_id, *, expected_revision, client_action_id):
        return self._command(child_id, protocol_id, 'evaluate', {}, expected_revision, client_action_id)

    def revoke(self, child_id, protocol_id, *, expected_revision, client_action_id):
        return self._command(child_id, protocol_id, 'revoke', {}, expected_revision, client_action_id)

    def prepare_score_binding(self, c, child_id, *, protocol_id, evidence_id, as_of):
        """Freeze the exact local source and certificate before model admission."""
        row, now = self._owned(c, child_id, protocol_id), timestamp(as_of)
        return self._score_binding(c, row, evidence_id, now, self._read(c, row, now))

    def _score_binding(self, c, row, evidence_id, now, view):
        if json.loads(row['scorer_json']).get('schema') == 'typed_cpu_original_audio_scorer_v2':
            require(False, 'AUDIO_DIMENSION_AUTHORITY_NOT_FULL_SCORE_BINDING')
        child_id, protocol_id = row['child_id'], row['protocol_id']
        require(view['score_qualified'], 'PROTOCOL_NOT_QUALIFIED')
        source = source_snapshot(c, child_id, evidence_id)
        scope, scorer = json.loads(row['scope_json']), json.loads(row['scorer_json'])
        require(source['grade'] == scope['grade'] and source['subject'] == scope['subject']
                and source['modality'] == scorer['modality'], 'ASSESSMENT_SCOPE_CHANGED')
        require(not any(case['evidence_id'] == evidence_id for case in json.loads(row['manifest_json'])['cases']),
                'HOLDOUT_IS_NOT_LEARNING_QUALIFICATION')
        certificate = c.execute("SELECT event_id FROM rubric_calibration_event WHERE protocol_id=? AND operation='qualify' ORDER BY revision LIMIT 1",
                                (protocol_id,)).fetchone()
        packet = {'assessment_binding': {'protocol_id': protocol_id, 'protocol_hash': row['protocol_hash'],
                  'scorer_hash': digest(scorer), 'source_version': source['version']},
                  'certificate_event_id': certificate[0], 'child_id': child_id, 'source_evidence_id': evidence_id,
                  'session_id': source['session_id'], 'diagnostic_item_id': source['diagnostic_item_id'],
                  'local_only': True, 'cloud_transmission_allowed': False}
        return {**packet, 'binding_version': digest(packet)}

    def revalidate_score_binding(self, c, child_id, binding, *, phase, as_of, assessment_id=None):
        require(phase in {'dispatch', 'persist', 'recover'} and isinstance(binding, dict), 'BINDING_PHASE_INVALID')
        require(binding.get('child_id') == child_id and isinstance(binding.get('assessment_binding'), dict), 'BINDING_SCOPE_INVALID')
        row = self._owned(c, child_id, binding['assessment_binding'].get('protocol_id'))
        now = timestamp(as_of)
        view = self._read(c, row, now)
        return self._revalidate_score_binding(c, row, binding, phase, now, assessment_id, view)

    def _revalidate_score_binding(self, c, row, binding, phase, now, assessment_id, view, *, _allow_abstention=False):
        require(phase in {'dispatch', 'persist', 'recover'} and isinstance(binding, dict), 'BINDING_PHASE_INVALID')
        require(binding.get('child_id') == row['child_id'] and isinstance(binding.get('assessment_binding'), dict)
                and binding['assessment_binding'].get('protocol_id') == row['protocol_id'], 'BINDING_SCOPE_INVALID')
        current = self._score_binding(c, row, binding.get('source_evidence_id'), now, view)
        require(current == binding, 'SCORE_BINDING_CHANGED')
        if phase == 'dispatch':
            require(assessment_id is None, 'DISPATCH_ASSESSMENT_UNEXPECTED')
            return current
        require(isinstance(assessment_id, str), 'ASSESSMENT_REQUIRED')
        receipt = self._qualified_score_receipt(c, row, assessment_id, now, view)
        # Feedback can survive a per-answer abstention, but only after current
        # binding, authority and actual assessment provenance have passed above.
        # Public score consumers never opt into this non-scoring persistence.
        if (_allow_abstention and receipt.get('status') == 'reference'
                and receipt.get('score_qualified') is False
                and receipt.get('blockers') == ['assessment_abstained']):
            return receipt
        require(receipt['score_qualified'] and receipt['source_evidence_id'] == binding['source_evidence_id']
                and receipt['certificate_event_id'] == binding['certificate_event_id'], 'SCORE_BINDING_CHANGED')
        return receipt

    def qualified_score_receipt(self, c, child_id, assessment_id, *, protocol_id, as_of):
        """Read-only score authority. Independence/transfer/retention are NOT granted."""
        row, now = self._owned(c, child_id, protocol_id), timestamp(as_of)
        view = self._read(c, row, now)
        return self._qualified_score_receipt(c, row, assessment_id, now, view)

    def _qualified_score_receipt(self, c, row, assessment_id, now, view):
        if json.loads(row['scorer_json']).get('schema') == 'typed_cpu_original_audio_scorer_v2':
            # The existing route is whole-assessment/feedback authority. Audio
            # dimensions have their own typed reader and cannot enter this lane.
            return {'status':'reference','score_qualified':False,
                    'blockers':['audio_dimensions_not_full_assessment'],
                    'mastery_claim_allowed':False,'certification_claim_allowed':False,
                    'local_only':True,'cloud_transmission_allowed':False}
        child_id, protocol_id = row['child_id'], row['protocol_id']
        if not view['score_qualified']:
            return {'status': 'reference', 'score_qualified': False, 'blockers': view['blockers'],
                    'local_only': True, 'cloud_transmission_allowed': False}
        current, call, source, proof = self._scored(c, row, assessment_id, now)
        require(not any(v['evidence_id'] == source['evidence_id'] for v in json.loads(row['manifest_json'])['cases']), 'HOLDOUT_IS_NOT_LEARNING_QUALIFICATION')
        certificate = c.execute("SELECT * FROM rubric_calibration_event WHERE protocol_id=? AND operation='qualify' ORDER BY revision LIMIT 1", (protocol_id,)).fetchone()
        require(timestamp(call['created_at']) >= timestamp(certificate['created_at']), 'ASSESSMENT_BEFORE_QUALIFICATION')
        scores = score_values(current, current['dimensions'])
        if any(v is None for v in scores.values()):
            return {'status': 'reference', 'score_qualified': False, 'blockers': ['assessment_abstained'],
                    'local_only': True, 'cloud_transmission_allowed': False}
        return {'status': 'calibrated', 'score_qualified': True, 'score_authority': 'calibrated_model_original_response',
            'assessment_id': assessment_id, 'assessment_fingerprint': current['fingerprint'],
            'protocol_id': protocol_id, 'protocol_hash': row['protocol_hash'], 'certificate_event_id': certificate['event_id'],
            'source_evidence_id': source['evidence_id'], 'source_version': source['version'], 'modality': source['modality'],
            'diagnostic_item_id': source['diagnostic_item_id'], 'passed': all(v >= 2 for v in scores.values()),
            'dimensions': current['dimensions'], 'scorer_hash': digest(json.loads(row['scorer_json'])),
            'conditions_status': 'requires_independent_exposure_and_stage_gates', 'independence_qualified': False,
            'local_only': True, 'cloud_transmission_allowed': False}

    def original_audio_dimension_receipt(self,c,child_id,assessment_id,*,as_of):
        """Exact persisted application binding; never pick a latest CPU source."""
        from .rubric_audio_projection import application_receipt
        return application_receipt(self,c,child_id,assessment_id,timestamp(as_of))
