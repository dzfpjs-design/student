"""Local original writing references. Never called by a model/ASR origin flag.

The host calls the two distinct capture methods from its native editor or
confirmed-document handler. Existing capture_artifact owns retention/deletion;
no raw-media copy, second score store or cloud authorization is created.
"""
from datetime import datetime, timezone
import json
from uuid import uuid4

from .local_supply import digest, validate_item

VERSION = 'writing_response_origin_v1'


def require(value, reason):
    if not value:
        raise ValueError('WRITING_' + reason)


def writing_item(c, child_id, item_id):
    item = validate_item(c, child_id, item_id)
    node = c.execute('SELECT * FROM knowledge_node WHERE knowledge_node_id=?', (item['knowledge_node_id'],)).fetchone()
    if item['answer_type']=='open_response':
        from .open_response_profile import profile_for_item
        try:
            profile = profile_for_item(item,dict(node))
        except ValueError as error:
            raise ValueError('WRITING_OPEN_PROFILE_CHANGED') from error
        require(profile['response_mode']=='written','WRITTEN_RESPONSE_MODE_REQUIRED')
    else:
        require(item['answer_type'] == 'writing' and node['track'] == 'pet'
                and node['code'].startswith('PET.WRITING.'), 'EXACT_WRITING_TARGET_REQUIRED')
    rubric = json.loads(item['expected_answer_json'])
    require(isinstance(rubric.get('task_points'), list) and rubric['task_points']
            and all(isinstance(v, str) and v.strip() for v in rubric['task_points'])
            and len(set(rubric['task_points'])) == len(rubric['task_points']), 'WRITING_TASK_CRITERIA_REQUIRED')
    return item, dict(node)


def _prompt(c, session_id, prompt_id, item_version, *, live):
    from .diagnostic_prompt import DiagnosticPromptService, _version
    row = c.execute('''SELECT p.*,ds.status AS session_status,ds.state AS session_state,
        cp.status AS child_status FROM diagnostic_prompt p
        JOIN daily_session ds ON ds.session_id=p.origin_session_id AND ds.child_id=p.child_id
        JOIN child_profile cp ON cp.child_id=p.child_id WHERE p.prompt_id=? AND p.origin_session_id=?''',
        (prompt_id, session_id)).fetchone()
    require(row and row['child_status'] == 'active' and row['item_version'] == item_version, 'ORIGINAL_SCOPE_CHANGED')
    item, _ = writing_item(c, row['child_id'], row['diagnostic_item_id'])
    current = DiagnosticPromptService._item(c, row['child_id'], row['diagnostic_item_id'])
    require(current and _version(current) == item_version, 'QUESTION_VERSION_CHANGED')
    require(row['status'] not in {'cancelled', 'invalidated'}, 'ORIGINAL_PROMPT_WITHDRAWN')
    if live:
        require(row['session_status'] == 'active' and row['session_state'] not in {'evolving', 'completed'}
                and row['status'] == 'waiting_answer', 'ORIGINAL_CAPTURE_NOT_ACTIVE')
    return row, item


class WritingOriginalService:
    def __init__(self, database):
        self.database = database

    def capture_native_response(self, session_id, *, prompt_id, item_version, text, client_action_id, expected_scope=None):
        """Internal native-editor input only; never an ASR/model-tool endpoint."""
        require(isinstance(text, str) and 1 <= len(text.strip()) <= 12000, 'ORIGINAL_TEXT_REQUIRED')
        return self._capture(session_id, prompt_id, item_version, client_action_id,
                             {'kind': 'native_editor', 'text': text}, expected_scope=expected_scope)

    def bind_confirmed_document_response(self, session_id, *, prompt_id, item_version,
                                         artifact_id, revision_id, confirmation_id,
                                         use_authorization_id, client_action_id):
        """Reference the exact page-use permission, never a Tutor confirmation."""
        return self._capture(session_id,prompt_id,item_version,client_action_id,
            {'kind':'confirmed_document','artifact_id':artifact_id,'revision_id':revision_id,
             'confirmation_id':confirmation_id,'use_authorization_id':use_authorization_id})

    def _capture(self, session_id, prompt_id, item_version, action_id, supplied, *, expected_scope=None):
        require(isinstance(action_id, str) and 1 <= len(action_id) <= 200, 'ACTION_REQUIRED')
        request_hash = digest([session_id, prompt_id, item_version, supplied])
        key = 'writing-original:' + action_id
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            from .native_writing_rejection import require_not_rejected
            require_not_rejected(c,'capture',session_id,action_id)
            if expected_scope is not None:
                from .native_writing import current_scope
                current_scope(c,session_id,expected_scope)
            old = c.execute('SELECT * FROM capture_artifact WHERE client_action_id=?', (key,)).fetchone()
            if old:
                details = json.loads(old['details_json'])
                require(old['session_id'] == session_id and details.get('request_hash') == request_hash, 'ACTION_CONFLICT')
                # Recovery must not restore a withdrawn artifact or reopen the task.
                return {'artifact_id': old['artifact_id'], 'prompt_id': prompt_id, 'item_version': item_version,
                        'status': old['status'], 'local_only': True, 'cloud_transmission_allowed': False}
            prompt, _ = _prompt(c, session_id, prompt_id, item_version, live=True)
            require(not c.execute('''SELECT 1 FROM capture_artifact WHERE child_id=? AND session_id=?
                AND artifact_type='writing_original' AND json_extract(details_json,'$.prompt_id')=?''',
                (prompt['child_id'], session_id, prompt_id)).fetchone(), 'ORIGINAL_ALREADY_BOUND')
            now = datetime.now(timezone.utc).isoformat()
            if supplied.get('kind') == 'native_editor':
                origin = {'kind':'native_editor'}
                text = supplied['text']
            else:
                require(supplied.get('kind') == 'confirmed_document','ORIGINAL_PROVENANCE_UNSUPPORTED')
                require(c.execute("SELECT 1 FROM sqlite_master WHERE name='writing_ocr_event'").fetchone(),
                        'OCR_USE_AUTHORIZATION_UNAVAILABLE')
                from .writing_ocr_authorization import resolve_written_document
                document = resolve_written_document(c,child_id=prompt['child_id'],session_id=session_id,
                    prompt_id=prompt_id,item_version=item_version,**{k:v for k,v in supplied.items() if k != 'kind'})
                text = document['text']
                origin = {**supplied,'source_version':document['source_version'],'authorization_version':document['authorization_version']}
            require(isinstance(text, str) and 1 <= len(text.strip()) <= 12000, 'ORIGINAL_TEXT_REQUIRED')
            aid = str(uuid4())
            details = {'schema': VERSION, 'origin': origin, 'prompt_id': prompt_id, 'item_version': item_version,
                       'response_hash': digest(text), 'request_hash': request_hash}
            c.execute('''INSERT INTO capture_artifact VALUES(?,?,?,'writing_original',NULL,?,NULL,?,'completed',?,?)''',
                (aid, prompt['child_id'], session_id, text if origin['kind']=='native_editor' else None,
                 json.dumps(details, sort_keys=True), now, key))
            c.commit()
            return {'artifact_id': aid, 'prompt_id': prompt_id, 'item_version': item_version,
                    'status': 'completed', 'local_only': True, 'cloud_transmission_allowed': False}


def original_written_response(c, prompt, attempt):
    return read_original_written_source(c, prompt, attempt)['original']


def read_original_written_source(c, prompt, attempt):
    """Read the original proof and its validated item in the caller's snapshot.

    The item is returned only after the complete original-response validation;
    it is not a reusable permission or a bypass for later source checks.
    """
    _, item = _prompt(c, prompt['origin_session_id'], prompt['prompt_id'], prompt['item_version'], live=False)
    require(prompt['status'] == 'answered' and prompt['attempt_id'] == attempt['attempt_id']
            and (attempt['child_id'], attempt['session_id'], attempt['diagnostic_item_id']) ==
            (prompt['child_id'], prompt['origin_session_id'], prompt['diagnostic_item_id']), 'ORIGINAL_ATTEMPT_REQUIRED')
    rows = c.execute('''SELECT * FROM capture_artifact WHERE child_id=? AND session_id=?
        AND artifact_type='writing_original' AND json_extract(details_json,'$.prompt_id')=?''',
        (prompt['child_id'], prompt['origin_session_id'], prompt['prompt_id'])).fetchall()
    require(len(rows) == 1, 'ORIGINAL_PROVENANCE_MISSING_OR_AMBIGUOUS')
    artifact = rows[0]
    details = json.loads(artifact['details_json'])
    require(artifact['status'] == 'completed' and details['schema'] == VERSION
            and details['item_version'] == prompt['item_version'], 'ORIGINAL_SOURCE_CHANGED')
    require(details.get('attempt_binding') == {'attempt_id': attempt['attempt_id'], 'evidence_id': attempt['evidence_id']},
            'ORIGINAL_ATTEMPT_BINDING_REQUIRED')
    times = [datetime.fromisoformat(row['created_at']) for row in (prompt, artifact, attempt)]
    require(all(at.tzinfo for at in times) and times[0] <= times[1] <= times[2], 'ORIGINAL_TIME_CONFLICT')
    origin = details['origin']
    text = _original_text(c,prompt,artifact,origin)
    require(text == attempt['submitted_answer'] and digest(text) == details['response_hash'], 'ORIGINAL_RESPONSE_CHANGED')
    original = {'schema': VERSION, 'artifact_id': artifact['artifact_id'], 'kind': origin['kind'],
            'source_version': digest(dict(artifact)), 'response_hash': details['response_hash'],
            'local_only': True, 'cloud_transmission_allowed': False}
    return {'original': original, 'item': item}


def _original_text(c,prompt,artifact,origin):
    if origin == {'kind':'native_editor'}:
        return artifact['extracted_text']
    from .rubric_calibration_transaction import unsupported_dependency
    unsupported_dependency()
    require(origin.get('kind') == 'confirmed_document','ORIGINAL_PROVENANCE_UNSUPPORTED')
    from .writing_ocr_authorization import resolve_written_document
    current = resolve_written_document(c,child_id=prompt['child_id'],session_id=prompt['origin_session_id'],
        prompt_id=prompt['prompt_id'],item_version=prompt['item_version'],
        **{k:origin[k] for k in ('artifact_id','revision_id','confirmation_id','use_authorization_id')})
    require(current['source_version']==origin['source_version'] and current['authorization_version']==origin['authorization_version'],
            'ORIGINAL_DOCUMENT_CHANGED')
    return current['text']


def bind_written_attempt(connection, *, child_id, session_id, prompt_id, item_version,
                         attempt_id, original_artifact_id):
    """Call after the original prompt/attempt save, before its transaction commits."""
    c = connection
    require(c.in_transaction, 'ANSWER_WRITE_TRANSACTION_REQUIRED')
    prompt, _ = _prompt(c, session_id, prompt_id, item_version, live=False)
    require(prompt['child_id'] == child_id and prompt['status'] == 'answered' and prompt['attempt_id'] == attempt_id,
            'ORIGINAL_ATTEMPT_REQUIRED')
    artifact = c.execute('SELECT * FROM capture_artifact WHERE artifact_id=? AND child_id=? AND session_id=?',
                         (original_artifact_id, child_id, session_id)).fetchone()
    attempt = c.execute('SELECT * FROM diagnostic_attempt WHERE attempt_id=? AND child_id=? AND session_id=?',
                        (attempt_id, child_id, session_id)).fetchone()
    require(artifact and artifact['artifact_type'] == 'writing_original' and artifact['status'] == 'completed'
            and attempt, 'ORIGINAL_SOURCE_CHANGED')
    details = json.loads(artifact['details_json'])
    require(details.get('schema') == VERSION and details.get('prompt_id') == prompt_id
            and details.get('item_version') == item_version, 'ORIGINAL_SCOPE_CHANGED')
    frozen = {'attempt_id': attempt_id, 'evidence_id': attempt['evidence_id']}
    if c.execute("SELECT 1 FROM sqlite_master WHERE name='writing_reference_revision'").fetchone():
        if c.execute('SELECT 1 FROM writing_reference_revision WHERE prompt_id=?',(prompt_id,)).fetchone():
            evidence=c.execute('SELECT hint_level FROM learning_evidence WHERE evidence_id=? AND child_id=?',
                (attempt['evidence_id'],child_id)).fetchone()
            require(evidence and type(evidence['hint_level']) is int and evidence['hint_level']>=1,
                    'REVISION_ASSISTANCE_MUST_BE_RECORDED')
    if c.execute("SELECT 1 FROM sqlite_master WHERE name='writing_followup'").fetchone():
        followup = c.execute('SELECT * FROM writing_followup WHERE prompt_id=?', (prompt_id,)).fetchone()
        if followup and followup['kind'] == 'revision':
            evidence = c.execute('SELECT hint_level FROM learning_evidence WHERE evidence_id=? AND child_id=?',
                                 (attempt['evidence_id'], child_id)).fetchone()
            require(evidence and type(evidence['hint_level']) is int and evidence['hint_level'] >= 1,
                    'REVISION_ASSISTANCE_MUST_BE_RECORDED')
    require(details.get('attempt_binding') in (None, frozen), 'ORIGINAL_ALREADY_BOUND')
    if details.get('attempt_binding') is None:
        require(prompt['session_status'] == 'active' and prompt['session_state'] not in {'evolving','completed'},
                'ANSWER_SESSION_NOT_ACTIVE')
    details['attempt_binding'] = frozen
    # Validate before mutation, using exactly the state about to be persisted.
    require(_original_text(c,prompt,artifact,details['origin']) == attempt['submitted_answer']
            and details['response_hash'] == digest(attempt['submitted_answer']), 'ORIGINAL_RESPONSE_CHANGED')
    times = [datetime.fromisoformat(row['created_at']) for row in (prompt, artifact, attempt)]
    require(all(at.tzinfo for at in times) and times[0] <= times[1] <= times[2], 'ORIGINAL_TIME_CONFLICT')
    if json.loads(artifact['details_json']) != details:
        c.execute('UPDATE capture_artifact SET details_json=? WHERE artifact_id=?', (json.dumps(details, sort_keys=True), original_artifact_id))
    return original_written_response(c, prompt, attempt)
