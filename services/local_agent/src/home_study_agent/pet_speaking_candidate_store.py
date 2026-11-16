"""Immutable local raw-model candidates. Not human review, scores or authority."""
from datetime import datetime, timezone
import hashlib
import json
import math
from uuid import uuid4

from .pet_multipa_candidate import prepare_input, candidate_result

TABLES = ('pet_speaking_model_candidate', 'pet_speaking_model_candidate_event')


def canonical(v):
    return json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(v):
    return hashlib.sha256(canonical(v).encode()).hexdigest()


def bounded_request(v):
    # Bound even rejected transient payloads before hashing. The converter below
    # remains the authority for the exact upstream shape and original source.
    nodes = [0]; text_bytes = [0]
    def visit(value, depth):
        nodes[0] += 1
        require(depth <= 4 and nodes[0] <= 4096, 'REQUEST_BOUND')
        if type(value) is dict:
            require(len(value) <= 32 and all(type(k) is str and len(k) <= 80 for k in value), 'REQUEST_BOUND')
            for key, item in value.items():
                visit(key, depth + 1); visit(item, depth + 1)
        elif type(value) is list:
            require(len(value) <= 512, 'REQUEST_BOUND')
            for item in value: visit(item, depth + 1)
        elif type(value) is str:
            require(len(value) <= 16000, 'REQUEST_BOUND')
            text_bytes[0] += len(value.encode('utf-8'))
            require(text_bytes[0] <= 262144, 'REQUEST_BOUND')
        elif type(value) is int:
            require(abs(value) <= 9007199254740991, 'REQUEST_BOUND')
        elif type(value) is float:
            require(math.isfinite(value), 'REQUEST_BOUND')
        else:
            require(value is None or type(value) is bool, 'REQUEST_BOUND')
    visit(v, 0)


def require(v, code):
    if not v:
        raise ValueError('PET_ACOUSTIC_CANDIDATE_' + code)


SCHEMA_OBJECTS = (
    ('table', 'pet_speaking_model_candidate', 'CREATE TABLE pet_speaking_model_candidate (\n            candidate_id TEXT PRIMARY KEY,\n            child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,\n            session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,\n            media_id TEXT NOT NULL, assessment_id TEXT NOT NULL,\n            binding_json TEXT NOT NULL, raw_numeric_json TEXT NOT NULL,\n            model TEXT NOT NULL, assets_json TEXT NOT NULL,\n            request_hash TEXT NOT NULL, created_at TEXT NOT NULL)'),
    ('index', 'pet_speaking_candidate_child', 'CREATE INDEX pet_speaking_candidate_child\n            ON pet_speaking_model_candidate(child_id,created_at)'),
    ('table', 'pet_speaking_model_candidate_event', "CREATE TABLE pet_speaking_model_candidate_event (\n            event_id TEXT PRIMARY KEY,\n            candidate_id TEXT NOT NULL REFERENCES pet_speaking_model_candidate(candidate_id) ON DELETE CASCADE,\n            child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,\n            revision INTEGER NOT NULL CHECK(revision>0),\n            operation TEXT NOT NULL CHECK(operation IN ('recorded','withdrawn')),\n            client_action_id TEXT NOT NULL,request_hash TEXT NOT NULL,created_at TEXT NOT NULL,\n            UNIQUE(candidate_id,revision),UNIQUE(child_id,client_action_id))"),
    ('trigger', 'pet_speaking_model_candidate_no_update', "CREATE TRIGGER pet_speaking_model_candidate_no_update BEFORE UPDATE ON pet_speaking_model_candidate\n            BEGIN SELECT RAISE(ABORT,'PET_ACOUSTIC_CANDIDATE_IMMUTABLE'); END"),
    ('trigger', 'pet_speaking_model_candidate_event_no_update', "CREATE TRIGGER pet_speaking_model_candidate_event_no_update BEFORE UPDATE ON pet_speaking_model_candidate_event\n            BEGIN SELECT RAISE(ABORT,'PET_ACOUSTIC_CANDIDATE_IMMUTABLE'); END"),
    ('trigger', 'pet_speaking_candidate_no_delete', "CREATE TRIGGER pet_speaking_candidate_no_delete\n        BEFORE DELETE ON pet_speaking_model_candidate\n        WHEN EXISTS(SELECT 1 FROM child_profile WHERE child_id=OLD.child_id)\n        AND EXISTS(SELECT 1 FROM daily_session WHERE session_id=OLD.session_id)\n        AND NOT EXISTS(SELECT 1 FROM deletion_job WHERE child_id=OLD.child_id\n            AND target_type='child_scope' AND target_id=OLD.child_id\n            AND scope IN ('learning','media','tutor','knowledge','all_history')\n            AND status IN ('scanning','deleting','verifying'))\n        BEGIN SELECT RAISE(ABORT,'PET_ACOUSTIC_CANDIDATE_PRIVACY_DELETE_REQUIRED'); END"),
    ('trigger', 'pet_speaking_candidate_event_no_delete', "CREATE TRIGGER pet_speaking_candidate_event_no_delete\n        BEFORE DELETE ON pet_speaking_model_candidate_event\n        WHEN EXISTS(SELECT 1 FROM pet_speaking_model_candidate WHERE candidate_id=OLD.candidate_id)\n        AND EXISTS(SELECT 1 FROM child_profile WHERE child_id=OLD.child_id)\n        AND NOT EXISTS(SELECT 1 FROM deletion_job WHERE child_id=OLD.child_id\n            AND target_type='child_scope' AND target_id=OLD.child_id\n            AND scope IN ('learning','media','tutor','knowledge','all_history')\n            AND status IN ('scanning','deleting','verifying'))\n        BEGIN SELECT RAISE(ABORT,'PET_ACOUSTIC_CANDIDATE_PRIVACY_DELETE_REQUIRED'); END"),
)


def validate_schema(c):
    """Declared schema89 must already contain this exact numeric-only contract."""
    from .parent_restore_assets import _validate_objects
    _validate_objects(c, SCHEMA_OBJECTS, TABLES)


def ensure_schema(c):
    source = c.execute("SELECT MAX(version) FROM schema_migration").fetchone()[0] or 0
    require(type(source) is int, "SCHEMA_VERSION_REQUIRED")
    if source >= 89:
        validate_schema(c)
        return
    # Older real sources may upgrade; a declared89 source may never repair loss.
    for kind, _, sql in SCHEMA_OBJECTS:
        c.execute(sql.replace("CREATE " + kind.upper() + " ",
                              "CREATE " + kind.upper() + " IF NOT EXISTS ", 1))
    validate_schema(c)


class SpeakingModelCandidateStore:
    def __init__(self, database, *, media_root, data_encryption_key=None):
        self.database = database
        self.media_root = media_root
        self.data_encryption_key = data_encryption_key

    def _args(self, media_id, assessment_id):
        return dict(media_id=media_id, assessment_id=assessment_id, media_root=self.media_root,
                    data_encryption_key=self.data_encryption_key)

    @staticmethod
    def _action(value):
        require(type(value) is str and 0 < len(value) <= 200, 'ACTION_REQUIRED')

    @staticmethod
    def _owned(c, child_id, candidate_id):
        row = c.execute('SELECT * FROM pet_speaking_model_candidate WHERE candidate_id=? AND child_id=?',
                        (candidate_id, child_id)).fetchone()
        require(row is not None, 'NOT_FOUND')
        return row

    @staticmethod
    def _latest(c, candidate_id):
        return c.execute('SELECT * FROM pet_speaking_model_candidate_event WHERE candidate_id=? ORDER BY revision DESC LIMIT 1',
                         (candidate_id,)).fetchone()

    @staticmethod
    def _receipt(row):
        return {k: row[k] for k in ('candidate_id', 'revision', 'operation', 'created_at')}

    def recorded_action(self, child_id, session_id, *, media_id, assessment_id,
                        asset_hashes, client_action_id):
        """Resolve an already committed command before expensive inference.

        A replay returns the original receipt with its present lifecycle. It
        never re-evaluates, revives a withdrawn result, or trusts new raw output.
        """
        self._action(client_action_id)
        with self.database.connect() as c:
            c.execute('BEGIN')
            event = c.execute('SELECT * FROM pet_speaking_model_candidate_event WHERE child_id=? AND client_action_id=?',
                              (child_id, client_action_id)).fetchone()
            if event is None:
                return None
            require(event['operation'] == 'recorded', 'IDEMPOTENCY_CONFLICT')
            row = self._owned(c, child_id, event['candidate_id'])
            require(row['session_id'] == session_id and row['media_id'] == media_id
                    and row['assessment_id'] == assessment_id
                    and json.loads(row['assets_json']) == asset_hashes, 'IDEMPOTENCY_CONFLICT')
            return dict(receipt=self._receipt(event), current=self._get(c, child_id, row['candidate_id']))

    def record(self, child_id, session_id, *, binding, result, asset_hashes, client_action_id, job_claim=None, attested=None, invocation=None):
        self._action(client_action_id)
        require((attested is None)==(invocation is None)and(attested is None or job_claim is not None),'EXECUTION_PAIR_REQUIRED')
        original_result=result
        require(type(binding) is dict and binding.get('child_id') == child_id
                and binding.get('session_id') == session_id, 'SOURCE_SCOPE_REQUIRED')
        args = self._args(binding.get('media_id'), binding.get('assessment_id'))
        # The action hash also covers the transient source transcripts/word text,
        # so a different request cannot reuse a command even though we do not
        # retain that private text in the durable numeric candidate.
        if binding.get('task_family')=='read_aloud' and binding.get('audio',{}).get('frames',0)>=240000:
            from .pet_multipa_reference_segments import validate_result as validate_segments,RESULT
            from .pet_multipa_protocol import MAX_RESULT
            # Validate the separate exact numeric schema before encoding/hashing.
            # Keep the legacy four-level request limit: bind the complete new
            # nested segment result by a fixed digest, never a relaxed raw blob.
            validate_segments(result);encoded=canonical(result).encode()
            require(len(encoded)<=MAX_RESULT-4096,'NUMERIC_RESULT_BOUND')
            result=json.loads(encoded)
            request_result=dict(schema=RESULT,sha256=hashlib.sha256(encoded).hexdigest())
        else:request_result=result
        request = dict(operation='recorded', child_id=child_id, session_id=session_id,
                       binding=binding, result=request_result, asset_hashes=asset_hashes)
        bounded_request(request)
        request_hash = digest(request)
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if job_claim is not None:
                from .pet_speaking_candidate_jobs import validate_claim
                validate_claim(c,job_claim,child_id,session_id,binding,asset_hashes)
            prior = c.execute('SELECT * FROM pet_speaking_model_candidate_event WHERE child_id=? AND client_action_id=?',
                              (child_id, client_action_id)).fetchone()
            if prior:
                require(prior['operation'] == 'recorded' and prior['request_hash'] == request_hash, 'IDEMPOTENCY_CONFLICT')
                answer = dict(receipt=self._receipt(prior), current=self._get(c, child_id, prior['candidate_id']))
                if job_claim is not None:
                    from .pet_speaking_candidate_jobs import complete_claim
                    complete_claim(c,job_claim,prior['candidate_id'])
                c.commit()
                return answer
            candidate = candidate_result(c, session_id, binding=binding, result=result, asset_hashes=asset_hashes, **args)
            # Strict allowlist: no audio bytes/paths, words, ASR text, rationale,
            # arbitrary model JSON, human scores or quality/permission assertions.
            if binding['task_family']=='read_aloud' and binding['audio']['frames']>=240000:
                numeric=dict(schema=candidate['schema'],adapter=candidate['adapter'],segments=candidate['raw_segments'],word_count=candidate['word_count'],alignment_frame_count=candidate['alignment_frame_count'])
            elif binding['task_family']=='read_aloud':
                numeric = dict(schema=candidate['schema'],adapter=candidate['adapter'],sentence=candidate['raw_sentence_outputs'],words=candidate['raw_word_outputs'],word_count=candidate['word_count'],alignment_frames=candidate['alignment_frames'])
            else:
                numeric = dict(sentence=candidate['raw_sentence_outputs'], words=candidate['raw_word_outputs'],
                               word_count=len(candidate['word_text']))
            if binding['task_family']=='read_aloud' and binding['audio']['frames']>=240000:
                from .pet_multipa_protocol import MAX_RESULT
                require(len(canonical(numeric).encode())<=MAX_RESULT-4096,'NUMERIC_RESULT_BOUND')
            else:
                require(len(canonical(numeric).encode()) <= 65536, 'NUMERIC_RESULT_BOUND')
            now = datetime.now(timezone.utc).isoformat(); identifier = uuid4().hex
            c.execute('INSERT INTO pet_speaking_model_candidate VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                      (identifier, child_id, session_id, binding['media_id'], binding['assessment_id'],
                       canonical(binding), canonical(numeric), candidate['model'], canonical(candidate['asset_hashes']), request_hash, now))
            c.execute('INSERT INTO pet_speaking_model_candidate_event VALUES (?,?,?,?,?,?,?,?)',
                      (uuid4().hex, identifier, child_id, 1, 'recorded', client_action_id, request_hash, now))
            event = self._latest(c, identifier)
            answer = dict(receipt=self._receipt(event), current=self._get(c, child_id, identifier))
            if attested is not None:
                from .pet_multipa_execution_receipt import record_attested
                record_attested(c,candidate_id=identifier,child_id=child_id,session_id=session_id,job_claim=job_claim,binding=binding,result=original_result,numeric=numeric,request_hash=request_hash,attested=attested,invocation=invocation)
            if job_claim is not None:
                from .pet_speaking_candidate_jobs import complete_claim
                complete_claim(c,job_claim,identifier)
            c.commit()
            return answer

    def _get(self, c, child_id, candidate_id):
        row = self._owned(c, child_id, candidate_id)
        event = self._latest(c, candidate_id)
        require(event is not None, 'HISTORY_INCOMPLETE')
        binding = json.loads(row['binding_json']); source_status = 'changed'
        try:
            actual, _ = prepare_input(c, row['session_id'], **self._args(row['media_id'], row['assessment_id']))
            if actual == binding:
                source_status = 'current'
        except (ValueError, KeyError):
            source_status = 'changed'
        except OSError:
            source_status = 'unavailable'
        withdrawn = event['operation'] == 'withdrawn'
        return dict(candidate_id=candidate_id, child_id=child_id, binding=binding,
                    raw_numeric=json.loads(row['raw_numeric_json']), model=row['model'],
                    asset_hashes=json.loads(row['assets_json']), revision=event['revision'],
                    status='withdrawn' if withdrawn else 'current' if source_status == 'current' else 'changed',
                    source_status=source_status, lifecycle='withdrawn' if withdrawn else 'recorded',
                    assessment_status='uncalibrated_raw_model_candidate', assessor_installed=False,
                    skill_qualified=False, mastery_claim_allowed=False, certification_claim_allowed=False,
                    asset_authorization='not_verified_by_this_store', local_only=True, cloud_transmission_allowed=False,
                    created_at=row['created_at'])

    def get(self, child_id, candidate_id):
        with self.database.connect() as c:
            c.execute('BEGIN')
            return self._get(c, child_id, candidate_id)

    def history(self, child_id, candidate_id):
        with self.database.connect() as c:
            c.execute('BEGIN'); self._owned(c, child_id, candidate_id)
            return [self._receipt(r) for r in c.execute('SELECT * FROM pet_speaking_model_candidate_event WHERE candidate_id=? ORDER BY revision', (candidate_id,))]

    def withdraw(self, child_id, candidate_id, *, expected_revision, client_action_id):
        self._action(client_action_id)
        require(type(expected_revision) is int and expected_revision > 0, 'REVISION_REQUIRED')
        request_hash = digest(dict(operation='withdrawn', child_id=child_id,
                                   candidate_id=candidate_id, expected_revision=expected_revision))
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE'); self._owned(c, child_id, candidate_id)
            prior = c.execute('SELECT * FROM pet_speaking_model_candidate_event WHERE child_id=? AND client_action_id=?', (child_id, client_action_id)).fetchone()
            if prior:
                require(prior['operation'] == 'withdrawn' and prior['request_hash'] == request_hash, 'IDEMPOTENCY_CONFLICT')
                answer = dict(receipt=self._receipt(prior), current=self._get(c, child_id, candidate_id)); c.commit(); return answer
            latest = self._latest(c, candidate_id)
            require(latest['revision'] == expected_revision, 'REVISION_CONFLICT')
            require(latest['operation'] != 'withdrawn', 'ALREADY_WITHDRAWN')
            now = datetime.now(timezone.utc).isoformat()
            c.execute('INSERT INTO pet_speaking_model_candidate_event VALUES (?,?,?,?,?,?,?,?)',
                      (uuid4().hex, candidate_id, child_id, expected_revision + 1, 'withdrawn', client_action_id, request_hash, now))
            answer = dict(receipt=self._receipt(self._latest(c, candidate_id)), current=self._get(c, child_id, candidate_id))
            c.commit(); return answer


def delete_child_scope(c, child_id):
    require(c.in_transaction and c.execute('''SELECT 1 FROM deletion_job WHERE child_id=?
        AND target_type='child_scope' AND target_id=?
        AND scope IN ('learning','media','tutor','knowledge','all_history')
        AND status IN ('scanning','deleting','verifying')''', (child_id, child_id)).fetchone(), 'PRIVACY_DELETE_REQUIRED')
    return {TABLES[1]: c.execute('DELETE FROM pet_speaking_model_candidate_event WHERE child_id=?', (child_id,)).rowcount,
            TABLES[0]: c.execute('DELETE FROM pet_speaking_model_candidate WHERE child_id=?', (child_id,)).rowcount}
