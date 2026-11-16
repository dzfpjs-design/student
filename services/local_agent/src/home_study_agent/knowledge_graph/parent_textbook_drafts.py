"""Encrypted, parent-only editing state; never a source or approved knowledge."""
from __future__ import annotations

import hashlib
import json
import os
from uuid import uuid4

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..repository import utc_now
from ..privacy_operation_receipt import generation, initialize as initialize_generation
from .parent_textbook_capture import CaptureConflictError, CaptureNotFoundError, ParentTextbookCaptureService
from .schema import ensure_schema


class ParentTextbookDraftService:
    SCOPES = ('capture', 'language')
    PAGE_FIELDS = {'material_kind', 'title', 'ocr_text', 'grade_scope', 'subject_scope',
                   'textbook_version', 'volume', 'unit', 'lesson', 'page_number', 'source_ref'}

    def __init__(self, database):
        self.database = database
        self.capture = ParentTextbookCaptureService(database, None)
        with database.external_effect_guard(), database.connect() as con:
            ensure_schema(con, utc_now()); initialize_generation(con); con.commit()

    @staticmethod
    def _bytes(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')

    @staticmethod
    def _aad(child_id, scope, revision):
        return json.dumps(['hsc-parent-draft-v1', child_id, scope, revision], separators=(',', ':')).encode()

    def _check_owner(self, con, child_id, scope):
        if scope not in self.SCOPES:
            raise CaptureNotFoundError('draft mode is unavailable')
        if not con.execute("SELECT 1 FROM child_profile WHERE child_id=? AND status='active'", (child_id,)).fetchone():
            raise CaptureNotFoundError('child profile is unavailable')

    def _validate(self, value):
        if value is None:
            return
        fields = {'page', 'capture_ref', 'capture_version', 'review_drafts', 'action_ids'}
        if not isinstance(value, dict) or not fields <= set(value) or set(value) - fields - {'objective_draft'}:
            raise ValueError('invalid draft fields')
        page = value['page']
        if not isinstance(page, dict) or set(page) - self.PAGE_FIELDS:
            raise ValueError('invalid page fields')
        for name, field in page.items():
            if name == 'page_number':
                if isinstance(field, bool) or not isinstance(field, int) or not 1 <= field <= 100000:
                    raise ValueError('invalid page number')
            elif not isinstance(field, str) or len(field) > (200000 if name == 'ocr_text' else 600):
                raise ValueError('invalid page field size')
        if page.get('material_kind', 'textbook') not in {'textbook', 'workbook'}:
            raise ValueError('invalid material kind')
        for name in ('capture_ref', 'capture_version'):
            if not isinstance(value[name], str) or len(value[name]) > 80:
                raise ValueError('invalid capture reference')
        if bool(value['capture_ref']) != bool(value['capture_version']):
            raise ValueError('capture version is required')
        reviews = value['review_drafts']
        if not isinstance(reviews, dict) or len(reviews) > 50:
            raise ValueError('invalid review drafts')
        for ref, draft in reviews.items():
            if not isinstance(ref, str) or not 1 <= len(ref) <= 80 or not isinstance(draft, dict) or set(draft) != {'text', 'source_version'}:
                raise ValueError('invalid review draft')
            if not isinstance(draft['text'], str) or len(draft['text']) > 200000 or not isinstance(draft['source_version'], str) or not 1 <= len(draft['source_version']) <= 80:
                raise ValueError('invalid review draft size')
        actions = value['action_ids']
        if not isinstance(actions, dict) or len(actions) > 100:
            raise ValueError('invalid action identities')
        for name, action in actions.items():
            if not isinstance(name, str) or not 1 <= len(name) <= 160 or not isinstance(action, str) or not 8 <= len(action) <= 128:
                raise ValueError('invalid action identity size')
        objective = value.get('objective_draft')
        if objective is not None:
            required = {'question_number', 'full_question', 'official_answers', 'candidate_node_id', 'paper', 'answer_key'}
            if not isinstance(objective, dict) or not required <= set(objective) or set(objective) - required - {'raw_editing'}:
                raise ValueError('invalid objective editing fields')
            for name, limit in [('question_number', 40), ('full_question', 1200)]:
                if not isinstance(objective[name], str) or len(objective[name]) > limit:
                    raise ValueError('invalid objective editing text')
            answers = objective['official_answers']
            if not isinstance(answers, list) or len(answers) > 24 or any(
                    not isinstance(answer, str) or len(answer) > 200 for answer in answers):
                raise ValueError('invalid objective answer editing')
            node = objective['candidate_node_id']
            if node is not None and (not isinstance(node, str) or not 1 <= len(node) <= 120):
                raise ValueError('invalid objective candidate node')
            for role in ('paper', 'answer_key'):
                ref = objective[role]
                if (not isinstance(ref, dict) or set(ref) != {'capture_ref', 'source_version'}
                        or any(not isinstance(ref[k], str) or not 1 <= len(ref[k]) <= 80 for k in ref)):
                    raise ValueError('invalid objective page binding')
            if objective['paper']['capture_ref'] == objective['answer_key']['capture_ref']:
                raise ValueError('two distinct objective original pages required')
            raw = objective.get('raw_editing')
            if 'raw_editing' in objective:
                if (not isinstance(raw, dict) or set(raw) != {'full_question', 'official_answers_text'}
                        or any(not isinstance(text, str) or len(text) > 100000 for text in raw.values())):
                    raise ValueError('invalid objective raw editing')
                raw_answers = raw['official_answers_text'].split('\n')
                normalized_answers = raw_answers if len(raw_answers) <= 24 and all(len(answer) <= 200 for answer in raw_answers) else []
                normalized_question = raw['full_question'] if len(raw['full_question']) <= 1200 else ''
                if objective['official_answers'] != normalized_answers or objective['full_question'] != normalized_question:
                    raise ValueError('objective candidate does not match raw editing')
        if len(self._bytes(value)) > 1000000:
            raise ValueError('draft is too large')

    def _stale_refs(self, con, child_id, value):
        if value is None:
            return []
        refs = {ref: item['source_version'] for ref, item in value['review_drafts'].items()}
        if value['capture_ref']:
            ref = value['capture_ref']
            if ref in refs and refs[ref] != value['capture_version']:
                return [ref]
            refs[ref] = value['capture_version']
        objective = value.get('objective_draft')
        objective_refs = {}
        if objective:
            for role in ('paper', 'answer_key'):
                ref = objective[role]
                if ref['capture_ref'] in refs and refs[ref['capture_ref']] != ref['source_version']:
                    return [ref['capture_ref']]
                refs[ref['capture_ref']] = ref['source_version']
                objective_refs[ref['capture_ref']] = ref['source_version']
        if not refs:
            return []
        rows = con.execute('SELECT * FROM kg_parent_textbook_ocr_artifact WHERE child_id=?', (child_id,)).fetchall()
        owned = {self.capture._ref(row['artifact_id']): row for row in rows}
        stale = {ref for ref, version in refs.items() if ref not in owned or self.capture.source_version(owned[ref]) != version}
        for ref in objective_refs:
            if ref not in owned:
                continue
            row = owned[ref]
            if row['status'] not in {'needs_review', 'ready', 'imported'} or not row['raw_image_path']:
                stale.add(ref)
            try:
                self.capture._validate_retained_source(con, row)
            except ValueError:
                stale.add(ref)
        return sorted(stale)

    def _public(self, con, child_id, scope, row):
        payload = None
        if row is not None and row['encrypted_payload'] is not None:
            try:
                data = bytes(row['encrypted_payload'])
                if len(data) < 32 or data[:4] != b'HSD1':
                    raise ValueError('invalid encrypted header')
                _, key = self.capture._media_runtime()
                plain = AESGCM(key).decrypt(data[4:16], data[16:], self._aad(child_id, scope, row['revision']))
                if hashlib.sha256(plain).hexdigest() != row['payload_sha256']:
                    raise ValueError('draft checksum mismatch')
                payload = json.loads(plain)
                self._validate(payload)
            except Exception as error:
                raise ValueError('saved draft could not be decrypted; it was preserved') from error
        return {'child_id':child_id, 'scope':scope, 'storage_generation':generation(con),
                'revision': row['revision'] if row else 0, 'payload': payload,
                'last_action_id': row['last_action_id'] if row else None,
                'last_expected_revision': row['last_expected_revision'] if row else None,
                'updated_at': row['updated_at'] if row else None,
                'stale_refs': self._stale_refs(con, child_id, payload),
                'cloud_transmission_allowed': False}

    def read(self, child_id, scope):
        with self.database.external_effect_guard(), self.database.connect() as con:
            self._check_owner(con, child_id, scope)
            row = con.execute('SELECT * FROM kg_parent_textbook_form_draft WHERE child_id=? AND scope=?', (child_id, scope)).fetchone()
            return self._public(con, child_id, scope, row)

    def save(self, child_id, scope, expected_revision, client_action_id, payload, *, expected_generation=None):
        self._validate(payload)
        if payload and payload.get('objective_draft') is not None and scope != 'capture':
            raise ValueError('objective editing belongs to capture drafts only')
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
            raise ValueError('invalid expected revision')
        if not isinstance(client_action_id, str) or not 8 <= len(client_action_id) <= 128:
            raise ValueError('invalid draft action identity')
        plain = self._bytes(payload)
        digest = hashlib.sha256(plain).hexdigest()
        with self.database.external_effect_guard(), self.database.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            self._check_owner(con, child_id, scope)
            if not expected_generation or generation(con) != expected_generation:
                raise CaptureConflictError('local storage was restored or changed; the old window did not write')
            row = con.execute('SELECT * FROM kg_parent_textbook_form_draft WHERE child_id=? AND scope=?', (child_id, scope)).fetchone()
            # Do not replace unreadable work with a fresh blank, even on a CAS match.
            current = self._public(con, child_id, scope, row)
            # A replay must not make a changed original page look freshly saved.
            # This is editing-only; it never creates an item, approval or review task.
            if payload and payload.get('objective_draft') is not None:
                if self._stale_refs(con, child_id, payload):
                    raise CaptureConflictError('objective original page changed; saved editing was preserved')
                node_id = payload['objective_draft']['candidate_node_id']
                if node_id is not None:
                    child = con.execute('SELECT grade FROM child_profile WHERE child_id=?', (child_id,)).fetchone()
                    tracks = ('school',) if child['grade'] == '初三' else ('school', 'competition_math', 'pet')
                    node = con.execute("SELECT * FROM knowledge_node WHERE knowledge_node_id=? AND status='active'", (node_id,)).fetchone()
                    if (not node or node['node_type'] != 'skill' or node['track'] not in tracks
                            or node['track'] == 'school' and node['grade_band'] != child['grade']):
                        raise CaptureConflictError('objective candidate node is not current for this child')
            if row and row['last_action_id'] == client_action_id:
                if row['payload_sha256'] != digest or row['last_expected_revision'] != expected_revision:
                    raise CaptureConflictError('draft action identity was used for another request')
                return current
            if current['revision'] != expected_revision:
                raise CaptureConflictError('a newer draft exists; this window did not overwrite it')
            stale = self._stale_refs(con, child_id, payload)
            if stale:
                raise CaptureConflictError('draft source is unavailable or changed; original editing was preserved')
            revision = expected_revision + 1
            encrypted = None
            if payload is not None:
                _, key = self.capture._media_runtime()
                nonce = os.urandom(12)
                encrypted = b'HSD1' + nonce + AESGCM(key).encrypt(nonce, plain, self._aad(child_id, scope, revision))
            con.execute('''INSERT INTO kg_parent_textbook_form_draft VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(child_id,scope) DO UPDATE SET revision=excluded.revision,
                encrypted_payload=excluded.encrypted_payload,payload_sha256=excluded.payload_sha256,
                last_action_id=excluded.last_action_id,last_expected_revision=excluded.last_expected_revision,
                updated_at=excluded.updated_at''',
                (child_id, scope, revision, encrypted, digest, client_action_id, expected_revision, utc_now()))
            row = con.execute('SELECT * FROM kg_parent_textbook_form_draft WHERE child_id=? AND scope=?', (child_id, scope)).fetchone()
            result = self._public(con, child_id, scope, row)
            con.execute('INSERT INTO audit_event(audit_id,actor,action,child_id,summary_json,occurred_at) VALUES(?,?,?,?,?,?)',
                        (str(uuid4()), 'parent', 'parent_textbook_draft_saved', child_id,
                         json.dumps({'scope':scope, 'revision':revision, 'cleared':payload is None}), utc_now()))
            con.commit()
            return result

    @classmethod
    def purge(cls, con, child_id):
        """Keep a CAS tombstone so pre-deletion windows cannot revive erased text."""
        digest = hashlib.sha256(cls._bytes(None)).hexdigest()
        count = con.execute('SELECT COUNT(*) FROM kg_parent_textbook_form_draft WHERE child_id=? AND encrypted_payload IS NOT NULL', (child_id,)).fetchone()[0]
        for scope in cls.SCOPES:
            con.execute('''INSERT INTO kg_parent_textbook_form_draft VALUES(?,?,1,NULL,?,?,0,?)
                ON CONFLICT(child_id,scope) DO UPDATE SET revision=revision+1,
                encrypted_payload=NULL,payload_sha256=excluded.payload_sha256,
                last_action_id=excluded.last_action_id,last_expected_revision=revision,
                updated_at=excluded.updated_at''',
                (child_id, scope, digest, 'privacy-tombstone-' + os.urandom(16).hex(), utc_now()))
        return count

    def export(self, con, child_id):
        rows = con.execute('SELECT * FROM kg_parent_textbook_form_draft WHERE child_id=? ORDER BY scope', (child_id,)).fetchall()
        return [{'scope':row['scope'], **self._public(con, child_id, row['scope'], row)} for row in rows]
