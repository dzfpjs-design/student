"""One parent's revocable permission for one confirmed paper question's variants."""
import json
import math
import re
from uuid import uuid4

from .cloud_privacy import sanitize_cloud_text, sensitive_categories
from .local_supply import LocalSupplyError, digest, item_snapshot, validated_variant_seed
from .repository import utc_now

VERSION = 'paper_question_variant_generation_v1'
EVENT_TABLE = 'paper_variant_authorization_event'
JOB_TABLE = 'paper_variant_job_authorization'
FLAGS = {'local_only': True, 'cloud_transmission_allowed': False}
LICENSE_OPTIONS = ('CC0-1.0', 'original_author', 'explicit_cloud_processing_permission')


def _require(value, reason):
    if not value:
        raise ValueError('PAPER_VARIANT_' + reason)


def _exists(c, table):
    return bool(c.execute('SELECT 1 FROM sqlite_master WHERE name=?', (table,)).fetchone())


def _consent(c):
    from .writing_question_authorization import _consent as text_consent
    try:
        return text_consent(c)
    except ValueError as error:
        raise ValueError(str(error).replace('WRITING_QUESTION_', 'PAPER_VARIANT_', 1)) from None


def _license(value):
    from .writing_question_authorization import _license as license_declaration
    try:
        return license_declaration(value)
    except ValueError as error:
        raise ValueError(str(error).replace('WRITING_QUESTION_', 'PAPER_VARIANT_', 1)) from None


def _owned(c, child_id, mistake_id):
    child = c.execute('SELECT status FROM child_profile WHERE child_id=?', (child_id,)).fetchone()
    mistake = c.execute('SELECT * FROM mistake_case WHERE mistake_id=? AND child_id=?', (mistake_id, child_id)).fetchone()
    if not child or not mistake:
        raise KeyError('PAPER_VARIANT_NOT_FOUND')
    _require(child['status'] == 'active', 'CHILD_UNAVAILABLE')
    return mistake


def _metadata(value):
    if isinstance(value, list):
        for entry in value:
            _metadata(entry)
    elif isinstance(value, dict):
        _require(value.get('local_only') is not True and value.get('cloud_transmission_allowed') is not False,
                 'RESTRICTED_SOURCE')
        _require(value.get('license_status') not in {'private', 'family_owned_index', 'unknown'}, 'RESTRICTED_SOURCE')
        for key, entry in value.items():
            _require(not (entry and key in {'knowledge_context', 'atomic_evidence', 'source_id',
                'source_ref', 'knowledge_source_id', 'pdf', 'pdf_path', 'family_context', 'raw_audio',
                'raw_video', 'original_page', 'document_text'}), 'RESTRICTED_SOURCE')
            _metadata(entry)


def _scope(c, child_id, mistake_id):
    mistake = _owned(c, child_id, mistake_id)
    try:
        seed = validated_variant_seed(c, child_id, mistake_id)
    except LocalSupplyError as error:
        raise ValueError('PAPER_VARIANT_' + str(error)) from None
    _require(seed['diagnostic_item_id'] is None and mistake['source_type'] == 'document_camera'
             and seed['source_ref'].startswith('confirmed_paper_question_v1:'), 'CONFIRMED_PAPER_REQUIRED')
    artifact_id, _, candidate_id = mistake['source_ref'].partition('#')
    artifact = c.execute('SELECT * FROM capture_artifact WHERE artifact_id=? AND child_id=?', (artifact_id, child_id)).fetchone()
    candidate = c.execute('SELECT * FROM mistake_capture_candidate WHERE candidate_id=? AND child_id=?', (candidate_id, child_id)).fetchone()
    _require(artifact and candidate, 'SOURCE_UNAVAILABLE')
    from .paper_formula_review import resolve
    review = resolve(c, artifact, candidate)
    question, page = seed['prompt'], artifact['extracted_text'] or ''
    _require(8 <= len(question) <= 1000, 'QUESTION_LENGTH_UNSUPPORTED')
    start = page.find(question)
    projection = None
    if start >= 0:
        _require(page.find(question, start + 1) < 0, 'EXACT_QUESTION_SPAN_REQUIRED')
    for value in (artifact['confidence'], candidate['confidence']):
        _require(type(value) in (int, float) and math.isfinite(value) and (0 if review else .85) <= value <= 1, 'SOURCE_REVIEW_REQUIRED')
    try:
        details = json.loads(artifact['details_json'] or '{}')
    except (ValueError, TypeError):
        raise ValueError('PAPER_VARIANT_SOURCE_METADATA_INVALID') from None
    _require(isinstance(details, dict), 'SOURCE_METADATA_INVALID')
    revision = None
    if _exists(c, 'document_page_revision'):
        row = c.execute('SELECT * FROM document_page_revision WHERE artifact_id=?', (artifact_id,)).fetchone()
        if row:
            _require(row['child_id'] == child_id and row['session_id'] == artifact['session_id'], 'PAGE_REVISION_CHANGED')
            revision = dict(row)
    sidecar_policy = None
    if 'visual_capture' in details:
        # Verify the typed empty visual compartment against the completed OCR
        # job. Its local-only flags remain intact; only confirmed text is scoped.
        _require(revision is not None, 'PAGE_REVISION_CHANGED')
        from .paper_ocr_metadata import validate_visual_capture_sidecar
        try:
            sidecar_policy = validate_visual_capture_sidecar(c, {**dict(artifact), **revision}, details,
                                                           allow_quality_warning=bool(review))
        except ValueError:
            raise ValueError('PAPER_VARIANT_VISUAL_METADATA_RESTRICTED') from None
    if 'page_source' in details:
        from .paper_ocr_metadata import validate_page_source_sidecar
        try:
            validate_page_source_sidecar(dict(artifact), details)
        except ValueError:
            raise ValueError('PAPER_VARIANT_PAGE_SOURCE_METADATA_RESTRICTED') from None
    for key, value in details.items():
        if key not in {'visual_capture', 'page_source'}:
            _metadata({key: value})
    if review:
        projection = {'formula_question_review': review}
    elif start < 0:
        from .paper_question_projection import project_question_lines
        projection = project_question_lines(artifact, candidate, question)
    names = [r[0] for r in c.execute('SELECT nickname FROM child_profile')]
    _require(not sensitive_categories(question) and sanitize_cloud_text(question, names).text == question, 'PRIVATE_QUESTION')
    _require(not re.search(r'忽略.{0,12}(?:指令|规则)|系统提示|(?:ignore|override).{0,30}(?:instruction|system)|https?://|data:|base64', question, re.I), 'UNTRUSTED_QUESTION')
    _require(not re.search(r'如图|下图|图中|根据图|看图|diagram|shown in (?:the )?figure', question, re.I), 'TEXT_ONLY_QUESTION_REQUIRED')
    from .learning_state import NODES, SPECIALIZED_NODES
    node = c.execute('SELECT * FROM knowledge_node WHERE knowledge_node_id=?', (seed['knowledge_node_id'],)).fetchone()
    original = next((n for n in (*NODES, *SPECIALIZED_NODES) if n[0] == seed['knowledge_node_id']), None)
    keys = ('knowledge_node_id', 'track', 'subject', 'grade_band', 'code', 'title', 'description')
    _require(node and node['source_ref'] == 'hsc_original_v1' and original
             and tuple(node[k] for k in keys) == original
             and not c.execute('SELECT 1 FROM knowledge_source WHERE source_ref=?', (node['source_ref'],)).fetchone(),
             'TEACHING_TARGET_LOCAL_ONLY')
    question_identity = {'question': question, 'region': candidate['region_json'], 'node': dict(node)}
    if sidecar_policy:
        question_identity['visual_sidecar_policy'] = sidecar_policy
    if projection:
        question_identity['projection'] = projection
    return {'child_id': child_id, 'mistake_id': mistake_id, 'artifact_id': artifact_id,
        'candidate_id': candidate_id, 'purpose_version': VERSION,
        'question_start': start if projection is None else None,
        'question_end': start + len(question) if projection is None else None, 'question_hash': digest(question),
        'question_version': digest(question_identity),
        'source_version': digest({'seed': seed, 'artifact': dict(artifact), 'candidate': dict(candidate), 'revision': revision}),
        'question_preview': question, **({'question_projection': projection} if projection else {})}


def _binding(scope, consent):
    return {k: v for k, v in dict(scope, consent_version=consent).items() if k != 'question_preview'}


def _latest(c, child_id, mistake_id):
    if not _exists(c, EVENT_TABLE):
        return None
    return c.execute('SELECT * FROM '+EVENT_TABLE+' WHERE child_id=? AND mistake_id=? ORDER BY revision DESC LIMIT 1',
                     (child_id, mistake_id)).fetchone()


def _read(c, child_id, mistake_id):
    _owned(c, child_id, mistake_id)
    latest = _latest(c, child_id, mistake_id)
    view = {'child_id': child_id, 'mistake_id': mistake_id, 'revision': latest['revision'] if latest else 0,
        'status': 'authorization_required', 'reason': None, 'message': '请核对这道题和使用权，仅授权题目文字用于准备变式。',
        'question_preview': None, 'question_version': None, 'source_version': None, 'authorization_id': None,
        'license_options': list(LICENSE_OPTIONS),
        'rights_review_authority': 'parent_declaration_not_legal_or_network_verification', **FLAGS}
    try:
        scope = _scope(c, child_id, mistake_id)
        view.update({k: scope[k] for k in ('question_preview', 'question_version', 'source_version')})
        consent = _consent(c)
        if latest:
            _require(latest['command'] == 'grant', 'REVOKED')
            _require(latest['purpose_version'] == VERSION and json.loads(latest['binding_json']) == _binding(scope, consent), 'AUTHORIZATION_VERSION_CHANGED')
            _license(json.loads(latest['license_json']))
            view.update(status='authorized', authorization_id=latest['event_id'], message='已授权这道题的变式准备；不会发送孩子原答案或整页。')
    except ValueError as error:
        if not str(error).startswith('PAPER_VARIANT_'):
            raise
        view.update(status='invalidated' if latest else 'blocked', reason=str(error), message='题目、来源或许可需要重新核对，暂不发送。')
    if latest and latest['command'] == 'revoke':
        restored = latest['actor_id'] == 'system:restore'
        view.update(status='revoked', authorization_id=None,
            reason='PAPER_VARIANT_RESTORE_REAUTHORIZATION_REQUIRED' if restored else 'PAPER_VARIANT_REVOKED',
            message='备份恢复后需要重新授权。重新授权会新建准备任务，不恢复旧任务。' if restored
                else '已撤回。再次授权会新建准备任务，不恢复旧任务。')
    return view


def _job_fingerprint(job):
    return digest({k: job[k] for k in ('job_id', 'child_id', 'mistake_id', 'stages_json',
        'requested_count', 'generation_mode', 'source_snapshot_json', 'client_action_id')})


def validate_job_authorization(c, *, job_id):
    """Pure read on the caller's transaction; never select a newer grant for a job."""
    _require(c.in_transaction, 'TRANSACTION_REQUIRED')
    _require(_exists(c, JOB_TABLE), 'JOB_AUTHORIZATION_REQUIRED')
    binding = c.execute('SELECT * FROM '+JOB_TABLE+' WHERE job_id=?', (job_id,)).fetchone()
    job = c.execute('SELECT * FROM variant_generation_job WHERE job_id=?', (job_id,)).fetchone()
    _require(binding and job, 'JOB_AUTHORIZATION_REQUIRED')
    _require(job['child_id'] == binding['child_id'] and job['mistake_id'] == binding['mistake_id']
             and binding['purpose_version'] == VERSION and _job_fingerprint(job) == binding['dispatch_scope_hash'], 'JOB_BINDING_CHANGED')
    view = _read(c, binding['child_id'], binding['mistake_id'])
    _require(view['status'] == 'authorized' and view['authorization_id'] == binding['authorization_event_id'], 'JOB_AUTHORIZATION_REVOKED')
    event = _latest(c, binding['child_id'], binding['mistake_id'])
    return {'question': view['question_preview'], 'question_version': view['question_version'],
        'source_version': view['source_version'], 'authorization_id': event['event_id'],
        'authorization_version': digest(dict(event)), 'purpose_version': VERSION, **FLAGS}


def _stop_jobs(c, child_id, mistake_id):
    c.execute("""UPDATE variant_generation_job SET status='blocked',error_code='PAPER_VARIANT_AUTHORIZATION_REVOKED',updated_at=?
        WHERE child_id=? AND mistake_id=? AND generation_mode='model' AND status IN ('pending','processing')""",
        (utc_now(), child_id, mistake_id))


def _generation_job(c, event_id):
    row = c.execute('''SELECT j.job_id,j.status,j.requested_count,j.error_code FROM variant_generation_job j
        JOIN paper_variant_job_authorization b USING(job_id) WHERE b.authorization_event_id=?''', (event_id,)).fetchone()
    return dict(row) if row else None


def _result(c, event, child_id, mistake_id):
    return {'receipt': {k: event[k] for k in ('event_id', 'revision', 'command')},
        'current': _read(c, child_id, mistake_id), 'generation_job': _generation_job(c, event['event_id']), **FLAGS}


class PaperVariantAuthorizationService:
    """The host supplies actor_id only after parent and local-session authentication."""
    def __init__(self, database):
        self.database = database

    def initialize(self):
        from .variant_supply import VariantSupplyService
        VariantSupplyService(self.database).ensure_schema()
        with self.database.connect() as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS paper_variant_authorization_event(
                event_id TEXT PRIMARY KEY,child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                mistake_id TEXT NOT NULL REFERENCES mistake_case(mistake_id) ON DELETE CASCADE,
                artifact_id TEXT NOT NULL REFERENCES capture_artifact(artifact_id) ON DELETE CASCADE,
                candidate_id TEXT NOT NULL REFERENCES mistake_capture_candidate(candidate_id) ON DELETE CASCADE,
                revision INTEGER NOT NULL CHECK(revision>0),command TEXT NOT NULL CHECK(command IN ('grant','revoke')),
                purpose_version TEXT NOT NULL,question_version TEXT NOT NULL,source_version TEXT NOT NULL,
                binding_json TEXT NOT NULL,license_json TEXT NOT NULL,actor_id TEXT NOT NULL,request_hash TEXT NOT NULL,
                client_action_id TEXT NOT NULL,created_at TEXT NOT NULL,
                UNIQUE(child_id,mistake_id,revision),UNIQUE(child_id,client_action_id));
                CREATE TABLE IF NOT EXISTS paper_variant_job_authorization(
                job_id TEXT PRIMARY KEY REFERENCES variant_generation_job(job_id) ON DELETE CASCADE,
                child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                mistake_id TEXT NOT NULL REFERENCES mistake_case(mistake_id) ON DELETE CASCADE,
                authorization_event_id TEXT NOT NULL UNIQUE REFERENCES paper_variant_authorization_event(event_id) ON DELETE CASCADE,
                purpose_version TEXT NOT NULL,dispatch_scope_hash TEXT NOT NULL,created_at TEXT NOT NULL);
                CREATE TRIGGER IF NOT EXISTS paper_variant_authorization_no_update BEFORE UPDATE ON paper_variant_authorization_event
                BEGIN SELECT RAISE(ABORT,'immutable paper purpose authorization'); END;
                CREATE TRIGGER IF NOT EXISTS paper_variant_job_authorization_no_update BEFORE UPDATE ON paper_variant_job_authorization
                BEGIN SELECT RAISE(ABORT,'immutable paper job authorization'); END;''')
            c.commit()

    def read(self, child_id, *, mistake_id):
        with self.database.connect() as c:
            c.execute('BEGIN')
            return _read(c, child_id, mistake_id)

    def grant(self, child_id, *, mistake_id, question_version, source_version, license_basis,
              expected_revision, client_action_id, actor_id):
        return self._command(child_id, mistake_id, 'grant', expected_revision, client_action_id, actor_id,
            {'question_version': question_version, 'source_version': source_version, 'license_basis': _license(license_basis)})

    def revoke(self, child_id, *, mistake_id, expected_revision, client_action_id, actor_id):
        return self._command(child_id, mistake_id, 'revoke', expected_revision, client_action_id, actor_id, {})

    def _command(self, child_id, mistake_id, command, revision, key, actor, data):
        _require(actor == 'parent_ui', 'PARENT_AUTH_REQUIRED')
        _require(isinstance(key, str) and 1 <= len(key.strip()) <= 200, 'ACTION_INVALID')
        _require(type(revision) is int and revision >= 0, 'REVISION_CONFLICT')
        request_hash = digest({'child_id': child_id, 'mistake_id': mistake_id, 'command': command,
            'revision': revision, 'actor': actor, 'data': data, 'purpose': VERSION})
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            _owned(c, child_id, mistake_id)
            old = c.execute('SELECT * FROM '+EVENT_TABLE+' WHERE child_id=? AND client_action_id=?', (child_id, key)).fetchone()
            if old:
                _require(old['request_hash'] == request_hash, 'REQUEST_CONFLICT')
                return _result(c, old, child_id, mistake_id)
            latest = _latest(c, child_id, mistake_id)
            _require((latest['revision'] if latest else 0) == revision, 'REVISION_CONFLICT')
            if command == 'grant':
                scope = _scope(c, child_id, mistake_id)
                _require(all(data[k] == scope[k] for k in ('question_version', 'source_version')), 'AUTHORIZATION_VERSION_CHANGED')
                binding, license_basis = _binding(scope, _consent(c)), data['license_basis']
            else:
                _require(latest, 'AUTHORIZATION_REQUIRED')
                binding, license_basis = json.loads(latest['binding_json']), json.loads(latest['license_json'])
            event_id = str(uuid4())
            c.execute('INSERT INTO '+EVENT_TABLE+' VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (event_id, child_id, mistake_id, binding['artifact_id'], binding['candidate_id'], revision + 1, command,
                 VERSION, binding['question_version'], binding['source_version'], json.dumps(binding), json.dumps(license_basis),
                 actor, request_hash, key, utc_now()))
            _stop_jobs(c, child_id, mistake_id)
            if command == 'grant':
                self._create_job(c, child_id, mistake_id, event_id)
            event = c.execute('SELECT * FROM '+EVENT_TABLE+' WHERE event_id=?', (event_id,)).fetchone()
            result = _result(c, event, child_id, mistake_id)
            c.commit()
            return result

    @staticmethod
    def _create_job(c, child_id, mistake_id, event_id):
        from .variant_supply import TRANSFER_STAGES
        gate = c.execute('''SELECT g.requirements_json FROM mistake_mastery_gate g JOIN mistake_case m USING(mistake_id)
            WHERE m.mistake_id=? AND m.child_id=? AND m.status NOT IN ('excluded','mastered')''', (mistake_id, child_id)).fetchone()
        _require(gate, 'DIAGNOSIS_REQUIRED')
        requirements = json.loads(gate['requirements_json'])
        _require(all(type(requirements.get(stage, 0)) is int and 0 <= requirements.get(stage, 0) <= 6 for stage in TRANSFER_STAGES), 'STAGES_INVALID')
        stages = [stage for stage in TRANSFER_STAGES for _ in range(requirements.get(stage, 0))]
        _require(stages and len(stages) <= 12, 'NO_SUPPLY_REQUIRED')
        job_id, now = str(uuid4()), utc_now()
        seed = validated_variant_seed(c, child_id, mistake_id)
        c.execute('''INSERT INTO variant_generation_job(job_id,mistake_id,child_id,stages_json,requested_count,
            status,created_at,updated_at,client_action_id,source_snapshot_json,generation_mode)
            VALUES(?,?,?,?,?,'pending',?,?,?,?,'model')''', (job_id, mistake_id, child_id, json.dumps(stages), len(stages),
                now, now, 'paper-variant-grant:'+event_id, json.dumps(item_snapshot(seed))))
        job = c.execute('SELECT * FROM variant_generation_job WHERE job_id=?', (job_id,)).fetchone()
        c.execute('INSERT INTO '+JOB_TABLE+' VALUES(?,?,?,?,?,?,?)',
            (job_id, child_id, mistake_id, event_id, VERSION, _job_fingerprint(job), now))
        validate_job_authorization(c, job_id=job_id)


def export_child(c, child_id):
    return {key: [dict(r) for r in c.execute('SELECT * FROM '+table+' WHERE child_id=?', (child_id,))] if _exists(c, table) else []
        for key, table in (('paper_variant_authorizations', EVENT_TABLE), ('paper_variant_job_authorizations', JOB_TABLE))}


def delete_child_scope(c, child_id, scope):
    """No commit/DDL: delete references before events in the host's transaction."""
    _require(c.in_transaction, 'TRANSACTION_REQUIRED')
    _require(scope in {'learning', 'knowledge', 'media', 'all_history'}, 'DELETE_SCOPE_UNSUPPORTED')
    return {table: c.execute('DELETE FROM '+table+' WHERE child_id=?', (child_id,)).rowcount if _exists(c, table) else 0
        for table in (JOB_TABLE, EVENT_TABLE)}


def quarantine_after_restore(c, restore_id):
    """Restore never reinstates a past grant. Only the staged caller commits."""
    _require(c.in_transaction, 'TRANSACTION_REQUIRED')
    _require(isinstance(restore_id, str) and 0 < len(restore_id) <= 200, 'RESTORE_ID_INVALID')
    if not _exists(c, EVENT_TABLE):
        return 0
    rows = c.execute('''SELECT e.* FROM paper_variant_authorization_event e WHERE e.command='grant'
        AND NOT EXISTS(SELECT 1 FROM paper_variant_authorization_event n
            WHERE n.child_id=e.child_id AND n.mistake_id=e.mistake_id AND n.revision>e.revision)''').fetchall()
    for row in rows:
        key = 'restore:'+digest([restore_id, row['child_id'], row['mistake_id']])
        c.execute('INSERT INTO '+EVENT_TABLE+' VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (str(uuid4()), row['child_id'], row['mistake_id'], row['artifact_id'], row['candidate_id'], row['revision']+1,
             'revoke', VERSION, row['question_version'], row['source_version'], row['binding_json'], row['license_json'],
             'system:restore', digest([restore_id, row['event_id']]), key, utc_now()))
        _stop_jobs(c, row['child_id'], row['mistake_id'])
    return len(rows)
