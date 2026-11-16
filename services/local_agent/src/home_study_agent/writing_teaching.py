"""Read durable formative feedback and pin subsequent real writing prompts.

No model dispatch, score, synthetic question, or cloud permission is created here.
The host owns Runtime admission and prompt creation in the transaction below.
"""
from datetime import datetime, timezone
import json
import hashlib

from .diagnostic_prompt import DiagnosticPromptService, _version
from .learning_obligations import question_identity, project_item_exposure
from .learning_rubric import RubricEvidenceService
from .local_supply import LocalSupplyError, digest
from .writing_response_origin import _prompt, original_written_response, require, writing_item


FOLLOWUP_ACTION_FIELDS = frozenset({
    'kind', 'parent_prompt_id', 'parent_item_version', 'child_id', 'session_id',
    'assessment_id', 'feedback_version', 'diagnostic_item_id', 'item_version',
    'source_version', 'knowledge_node_id', 'required_hint_level', 'mastery_claim_allowed',
})


def _blocked_action(reason):
    return dict(status='blocked', reason=reason, action=None, binding_version=None,
                local_only=True, cloud_transmission_allowed=False)


def _previous_writing_exposure(c, child_id, item):
    """Prior work/assistance never becomes a new task just because 24 hours elapsed."""
    rows = c.execute('''SELECT DISTINCT di.* FROM diagnostic_item di JOIN (
        SELECT da.diagnostic_item_id FROM diagnostic_attempt da
          JOIN daily_session ds ON ds.session_id=da.session_id AND ds.child_id=da.child_id
          JOIN learning_evidence le ON le.evidence_id=da.evidence_id
            AND le.child_id=da.child_id AND le.session_id=da.session_id
          WHERE da.child_id=?
        UNION SELECT ma.diagnostic_item_id FROM mistake_attempt ma
          JOIN mistake_case mc ON mc.mistake_id=ma.mistake_id AND mc.child_id=ma.child_id
          JOIN learning_evidence le ON le.evidence_id=ma.evidence_id AND le.child_id=ma.child_id
          WHERE ma.child_id=?
        ) attempted ON attempted.diagnostic_item_id=di.diagnostic_item_id''', (child_id, child_id))
    identity = question_identity(item, connection=c)
    if any(row['diagnostic_item_id'] == item['diagnostic_item_id']
           or question_identity(row, connection=c) == identity for row in rows):
        return True
    # Reuse the existing exact-content/owned-review Tutor index and its receipt
    # quality rules, but do not age assistance into a never-seen writing task.
    from .learning_obligations import _question_catalog, _tutor_exposure_index
    catalog, identities = _question_catalog(c)
    exact, bound = _tutor_exposure_index(c, child_id, catalog, identities)
    return bool(bound.get(identity) or (not item.get('stimulus_text') and not item.get('stimulus_asset')
                and exact.get(' '.join(item['prompt'].split()))))


class WritingTeachingService:
    def __init__(self, database):
        self.database = database

    def initialize(self):
        with self.database.connect() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS writing_followup (
                prompt_id TEXT PRIMARY KEY REFERENCES diagnostic_prompt(prompt_id) ON DELETE CASCADE,
                parent_prompt_id TEXT NOT NULL REFERENCES diagnostic_prompt(prompt_id) ON DELETE CASCADE,
                child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
                assessment_id TEXT NOT NULL REFERENCES rubric_assessment(assessment_id) ON DELETE CASCADE,
                kind TEXT NOT NULL CHECK(kind IN ('revision','independent_check')),
                binding_json TEXT NOT NULL, client_action_id TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(child_id,client_action_id), UNIQUE(parent_prompt_id,assessment_id,kind))''')
            c.execute('''CREATE TABLE IF NOT EXISTS writing_reference_revision (
                prompt_id TEXT PRIMARY KEY REFERENCES diagnostic_prompt(prompt_id) ON DELETE CASCADE,
                parent_prompt_id TEXT NOT NULL REFERENCES diagnostic_prompt(prompt_id) ON DELETE CASCADE,
                child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
                reference_turn_id TEXT NOT NULL REFERENCES agent_turn(agent_turn_id) ON DELETE CASCADE,
                request_turn_id TEXT NOT NULL REFERENCES agent_turn(agent_turn_id) ON DELETE CASCADE,
                binding_json TEXT NOT NULL, client_action_id TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(child_id,client_action_id), UNIQUE(parent_prompt_id,request_turn_id))''')
            c.commit()

    def read(self, session_id, *, prompt_id, item_version, connection=None):
        if connection is None:
            with self.database.connect() as c:
                c.execute('BEGIN')
                return self.read(session_id, prompt_id=prompt_id, item_version=item_version, connection=c)
        c = connection
        prompt, item = _prompt(c, session_id, prompt_id, item_version, live=False)
        result = dict(session_id=session_id, child_id=prompt['child_id'], prompt_id=prompt_id, item_version=item_version,
            diagnostic_item_id=item['diagnostic_item_id'], knowledge_node_id=item['knowledge_node_id'],
            question=item['prompt'], response_text=None,
            success_criteria=list(json.loads(item['expected_answer_json'])['task_points']),
            status='waiting_answer', original=None, assessment_id=None, feedback=None, blockers=[],
            followups=[],
            automatic_certification=False, local_only=True, cloud_transmission_allowed=False)
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='writing_followup'").fetchone():
            for linked in c.execute('''SELECT f.kind,f.assessment_id,p.prompt_id,p.item_version,p.status
                FROM writing_followup f JOIN diagnostic_prompt p ON p.prompt_id=f.prompt_id
                AND p.child_id=f.child_id AND p.origin_session_id=f.session_id
                WHERE f.parent_prompt_id=? AND f.child_id=? AND f.session_id=? ORDER BY f.created_at,f.prompt_id''',
                (prompt_id,prompt['child_id'],session_id)):
                result['followups'].append(dict(linked))
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='writing_reference_revision'").fetchone():
            for linked in c.execute('''SELECT p.prompt_id,p.item_version,p.status,f.reference_turn_id,f.request_turn_id
                FROM writing_reference_revision f JOIN diagnostic_prompt p ON p.prompt_id=f.prompt_id
                  AND p.child_id=f.child_id AND p.origin_session_id=f.session_id
                WHERE f.parent_prompt_id=? AND f.child_id=? AND f.session_id=? ORDER BY f.created_at,f.prompt_id''',
                (prompt_id,prompt['child_id'],session_id)):
                result['followups'].append({**dict(linked),'kind':'revision','assessment_id':None,'reference_only':True})
        if prompt['status'] != 'answered':
            result['status'] = prompt['status']
            return result
        attempt = c.execute('SELECT * FROM diagnostic_attempt WHERE attempt_id=?', (prompt['attempt_id'],)).fetchone()
        require(attempt, 'EXACT_ATTEMPT_REQUIRED')
        result['attempt_id'], result['evidence_id'] = attempt['attempt_id'], attempt['evidence_id']
        result['response_text'] = attempt['submitted_answer']
        try:
            result['original'] = original_written_response(c, prompt, attempt)
        except ValueError as error:
            result.update(status='blocked', blockers=[str(error)])
            return result
        service = RubricEvidenceService(None)
        rows = (c.execute('''SELECT assessment_id FROM rubric_assessment WHERE child_id=?
            AND json_extract(payload_json,'$.source_evidence_id')=? ORDER BY created_at DESC,rowid DESC''',
            (prompt['child_id'], attempt['evidence_id'])).fetchall() if service.available(c) else [])
        if not rows:
            result.update(status='feedback_required', blockers=['writing_formative_feedback_missing'])
            return result
        current = service.get(prompt['child_id'], rows[0][0], connection=c)
        from .writing_rubric import prepare_writing_review
        try:
            binding = prepare_writing_review(c, prompt['child_id'], current['assessment_id'])
        except ValueError as error:
            result.update(status='blocked', blockers=[str(error)])
            return result
        result['assessment_id'] = current['assessment_id']
        review = current['review']
        if review and review['operation'] == 'retract':
            result.update(status='blocked', blockers=['writing_feedback_review_withdrawn'])
            return result
        # Prefer an explicit correction; never fall back to an older fluent model reply.
        dimensions = review['dimensions'] if review else current['dimensions']
        if not review:
            model = c.execute('''SELECT mc.* FROM rubric_assessment ra JOIN model_call mc ON mc.call_id=ra.model_call_id
                WHERE ra.assessment_id=? AND mc.child_id=? AND mc.session_id=?
                AND mc.purpose='learning_outcome_evaluation' ''', (current['assessment_id'], prompt['child_id'], session_id)).fetchone()
            if (not model or model['status'] != 'completed' or model['safety_status'] != 'passed'
                    or model['verification_status'] not in {'verified', 'partially_verified'}):
                result.update(status='feedback_required', blockers=['writing_formative_feedback_missing'])
                return result
        from .writing_rubric import writing_dimensions
        definitions = writing_dimensions(c,prompt['child_id'],item['diagnostic_item_id'])
        complete = bool(dimensions and {d['dimension_id'] for d in dimensions} == {d['dimension_id'] for d in definitions}
            and all(isinstance(d.get('rationale'), str) and d['rationale'].strip() and d['rationale'] != 'not_assessed'
                and service._quotes(d.get('response_quotes'), current['response']) for d in dimensions))
        if not complete:
            result.update(status='feedback_required', blockers=['writing_criterion_feedback_incomplete'])
            return result
        result.update(status='feedback_ready', feedback={'assessment_id':current['assessment_id'],
            'binding_version':binding['binding_version'], 'review_revision':current['review_revision'],
            'rubric_version':current['rubric_version'], 'dimensions':dimensions,
            'authority':'human_formative' if review else 'uncalibrated_model_formative',
            'automatic_certification':False})
        return result

    def resolve_reference_revision(self, session_id, *, prompt_id, item_version,
                                   reference_turn_id, request_turn_id, connection=None):
        """An admitted explicit revision request, not a rubric or independent check."""
        if connection is None:
            with self.database.connect() as c:
                c.execute('BEGIN')
                return self.resolve_reference_revision(session_id,prompt_id=prompt_id,item_version=item_version,
                    reference_turn_id=reference_turn_id,request_turn_id=request_turn_id,connection=c)
        c = connection
        base = dict(status='blocked',reason=None,local_only=True,cloud_transmission_allowed=False)
        try:
            prompt,item = _prompt(c,session_id,prompt_id,item_version,live=False)
            require(prompt['session_status']=='active' and prompt['session_state'] not in {'evolving','completed'},
                    'REVISION_SESSION_NOT_ACTIVE')
            reference,source = _reference_receipt(c,session_id,prompt,reference_turn_id)
            request = _revision_request(c,session_id,prompt,source,reference,request_turn_id)
            action = _reference_revision_action(prompt,reference_turn_id,request_turn_id)
            version = _reference_revision_version(action,source,reference,request)
            return {**base,'status':'ready','action':action,'binding_version':version}
        except ValueError as error:
            if str(error).startswith(('WRITING_','ANSWER_WRITING_','VOICE_CAPTURE_')):
                return {**base,'reason':str(error)}
            raise

    def select_independent_action(self, session_id, *, prompt_id, item_version, connection=None):
        """Read eligible sources without generating, reserving or consuming a question.

        The host freezes the returned action in its admitted request. Selection is
        not an execution receipt: revalidate that exact action, never select on retry.
        """
        if connection is None:
            with self.database.connect() as c:
                c.execute('BEGIN')
                return self.select_independent_action(session_id, prompt_id=prompt_id,
                    item_version=item_version, connection=c)
        c = connection
        try:
            scope = self.resolve_next_action(session_id, prompt_id=prompt_id, item_version=item_version,
                kind='independent_check', connection=c)
            if scope.get('reason') != 'writing_exact_new_source_required':
                return _blocked_action(scope.get('reason') or 'writing_feedback_required')
            _, original = _prompt(c, session_id, prompt_id, item_version, live=False)
            # Ordering is reproducible, not a ranking of educational quality. The
            # resolver validates ownership/review/source chains for every candidate.
            candidates = c.execute('''SELECT diagnostic_item_id FROM diagnostic_item
                WHERE knowledge_node_id=? AND answer_type=? AND status='active'
                ORDER BY diagnostic_item_id''', (original['knowledge_node_id'],original['answer_type'])).fetchall()
            for row in candidates:
                resolved = self.resolve_next_action(session_id, prompt_id=prompt_id, item_version=item_version,
                    kind='independent_check', diagnostic_item_id=row['diagnostic_item_id'], connection=c)
                if resolved['status'] == 'ready':
                    return resolved
            return _blocked_action('writing_no_eligible_independent_source')
        except ValueError as error:
            if not isinstance(error, LocalSupplyError) and not str(error).startswith('WRITING_'):
                raise
            return _blocked_action(str(error))

    def revalidate_action(self, session_id, *, action, binding_version, connection=None):
        """Recheck an exact locally frozen packet; a hash is not Runtime admission.

        Use the supplied connection inside a host transaction. The existing
        bind_writing_followup remains the mandatory prompt-write transaction guard.
        """
        fields=FOLLOWUP_ACTION_FIELDS | ({'parent_session_id'} if isinstance(action,dict) and 'parent_session_id' in action else set())
        if (not isinstance(action, dict) or set(action) != fields
                or not isinstance(binding_version, str)
                or any(not isinstance(action[key], str) or not action[key]
                       for key in fields - {'required_hint_level', 'mastery_claim_allowed'})
                or type(action['required_hint_level']) is not int
                or type(action['mastery_claim_allowed']) is not bool
                or action['kind'] not in {'revision', 'independent_check'}
                or action['session_id'] != session_id or digest(action) != binding_version):
            return _blocked_action('writing_followup_binding_invalid')
        if connection is None:
            with self.database.connect() as c:
                c.execute('BEGIN')
                return self.revalidate_action(session_id, action=action,
                    binding_version=binding_version, connection=c)
        try:
            resolved = self.resolve_next_action(action.get('parent_session_id',session_id), prompt_id=action['parent_prompt_id'],
                item_version=action['parent_item_version'], kind=action['kind'],
                diagnostic_item_id=action['diagnostic_item_id'], target_session_id=session_id, connection=connection)
            if resolved['status'] != 'ready':
                return _blocked_action(resolved['reason'])
            if resolved['action'] != action or resolved['binding_version'] != binding_version:
                return _blocked_action('writing_followup_binding_changed')
            return resolved
        except ValueError as error:
            if not isinstance(error, LocalSupplyError) and not str(error).startswith('WRITING_'):
                raise
            return _blocked_action(str(error))

    def resolve_next_action(self, session_id, *, prompt_id, item_version, kind, diagnostic_item_id=None,
                            target_session_id=None, connection=None):
        if connection is None:
            with self.database.connect() as c:
                c.execute('BEGIN')
                return self.resolve_next_action(session_id, prompt_id=prompt_id, item_version=item_version,
                    kind=kind, diagnostic_item_id=diagnostic_item_id, target_session_id=target_session_id, connection=c)
        c = connection
        require(kind in {'revision','independent_check'}, 'FOLLOWUP_KIND_INVALID')
        current = self.read(session_id, prompt_id=prompt_id, item_version=item_version, connection=c)
        blocked = {'status':'blocked', 'reason':None, 'local_only':True, 'cloud_transmission_allowed':False}
        if current['status'] != 'feedback_ready':
            return {**blocked, 'reason':current['blockers'][0] if current['blockers'] else 'writing_answer_required'}
        target_session_id = target_session_id or session_id
        session = c.execute('SELECT * FROM daily_session WHERE session_id=? AND child_id=?',
                            (target_session_id, current['child_id'])).fetchone()
        if not session or session['status'] != 'active' or session['state'] in {'evolving','completed'}:
            return {**blocked, 'reason':'writing_session_not_active'}
        if target_session_id != session_id:
            parent = c.execute('SELECT status,state,ended_at FROM daily_session WHERE session_id=? AND child_id=?',
                               (session_id, current['child_id'])).fetchone()
            if not parent or parent['status'] != 'completed' or parent['state'] != 'completed' or not parent['ended_at']:
                return {**blocked, 'reason':'writing_parent_day_not_closed'}
        if kind == 'revision':
            if diagnostic_item_id not in (None,current['diagnostic_item_id']):
                return {**blocked, 'reason':'writing_revision_requires_original_question'}
            diagnostic_item_id = current['diagnostic_item_id']
        if not diagnostic_item_id:
            return {**blocked, 'reason':'writing_exact_new_source_required'}
        try:
            item, _ = writing_item(c, current['child_id'], diagnostic_item_id)
        except ValueError as error:
            return {**blocked, 'reason':str(error)}
        if item['knowledge_node_id'] != current['knowledge_node_id']:
            return {**blocked, 'reason':'writing_task_family_changed'}
        if kind == 'independent_check':
            original, _ = writing_item(c, current['child_id'], current['diagnostic_item_id'])
            if item['answer_type']!=original['answer_type']:
                return {**blocked, 'reason':'writing_task_family_changed'}
            if item['answer_type']=='open_response':
                previous_rubric=json.loads(original['expected_answer_json'])
                next_rubric=json.loads(item['expected_answer_json'])
                if any(previous_rubric[key]!=next_rubric[key] for key in ('review_profile','response_mode','task_family','profile_version')):
                    return {**blocked, 'reason':'writing_task_family_changed'}
            exposure = project_item_exposure(c, current['child_id'], diagnostic_item_id, as_of=datetime.now(timezone.utc).isoformat())
            if (question_identity(item, connection=c) == question_identity(original, connection=c) or exposure['answer_repeated']
                    or exposure['tutor_assisted'] or _previous_writing_exposure(c, current['child_id'], item)):
                return {**blocked, 'reason':'writing_new_unexposed_question_required'}
        public = DiagnosticPromptService._item(c, current['child_id'], diagnostic_item_id)
        action = {'kind':kind, 'parent_prompt_id':prompt_id, 'parent_item_version':item_version,
            'child_id':current['child_id'], 'session_id':target_session_id, 'assessment_id':current['assessment_id'],
            'feedback_version':digest(current['feedback']), 'diagnostic_item_id':diagnostic_item_id,
            'item_version':_version(public), 'source_version':digest(item), 'knowledge_node_id':item['knowledge_node_id'],
            'required_hint_level':1 if kind == 'revision' else 0, 'mastery_claim_allowed':False}
        if target_session_id != session_id:
            action['parent_session_id'] = session_id
        return {'status':'ready', 'action':action, 'binding_version':digest(action),
                'local_only':True, 'cloud_transmission_allowed':False}


def _reference_revision_action(prompt,reference_turn_id,request_turn_id):
    return dict(kind='revision',parent_prompt_id=prompt['prompt_id'],parent_item_version=prompt['item_version'],
        diagnostic_item_id=prompt['diagnostic_item_id'],item_version=prompt['item_version'],child_id=prompt['child_id'],
        session_id=prompt['origin_session_id'],reference_turn_id=reference_turn_id,request_turn_id=request_turn_id,
        required_hint_level=1,mastery_claim_allowed=False,reference_only=True)


def _reference_revision_version(action,source,reference,request):
    return digest({'action':action,'source_version':source['source_version'],
        'reference_version':digest(reference),'request_version':request['version']})


def _reference_receipt(c, session_id, prompt, reference_turn_id):
    from .agent_runtime import AgentRuntimeStore
    row = c.execute('''SELECT t.*,p.context_manifest_json,p.status AS plan_status,
        s.capability_name,s.status AS step_status,s.arguments_json FROM agent_turn t
        JOIN agent_plan p ON p.agent_turn_id=t.agent_turn_id AND p.child_id=t.child_id AND p.session_id=t.session_id
        JOIN agent_step s USING(plan_id) WHERE t.agent_turn_id=? AND t.child_id=? AND t.session_id=?''',
        (reference_turn_id,prompt['child_id'],session_id)).fetchall()
    require(len(row)==1,'REFERENCE_RECEIPT_REQUIRED')
    row=dict(row[0])
    require(row['tool_status']=='completed' and row['plan_status']=='completed'
        and row['step_status']=='completed' and row['capability_name']=='conversation.respond', 'REFERENCE_RECEIPT_NOT_COMPLETED')
    require(json.loads(row['arguments_json']).get('response')==row['response_text'], 'REFERENCE_RECEIPT_CHANGED')
    action,manifest = json.loads(row['action_json']),json.loads(row['context_manifest_json'])
    reference = manifest.get('writing_reference')
    require(isinstance(reference,dict) and action.get('source')=='writing_local_reference'
        and action.get('writing_reference')==reference and reference.get('reference_only') is True
        and reference.get('automatic_certification') is False and reference.get('local_only') is True
        and reference.get('cloud_transmission_allowed') is False, 'REFERENCE_RECEIPT_CHANGED')
    source = AgentRuntimeStore.writing_reference_source(c,session_id,prompt['prompt_id'],prompt['item_version'])
    require(all(reference.get(k)==source[k] for k in ('child_id','session_id','prompt_id','item_version','evidence_id','attempt_id','source_version','task_points')),
            'REFERENCE_SOURCE_CHANGED')
    require(row['completed_at'] and row['created_at']<=row['completed_at'], 'REFERENCE_RECEIPT_TIME_INVALID')
    index,entries = reference.get('point_index'),reference.get('entries')
    require(type(index) is int and isinstance(entries,list) and 0<=index<=len(source['task_points']) and len(entries)==index,
            'REFERENCE_ENTRIES_INVALID')
    for position,entry in enumerate(entries):
        require(isinstance(entry,dict) and entry.get('task_point')==source['task_points'][position]
            and entry.get('semantic_coverage_verified') is False, 'REFERENCE_ENTRIES_INVALID')
        if entry.get('status')=='child_linked_quote':
            quote=entry.get('response_quote')
            require(isinstance(quote,str) and len(quote)>=8 and source['response'].count(quote)==1, 'REFERENCE_ENTRIES_INVALID')
        else:
            require(entry.get('status')=='child_reported_missing' and entry.get('response_quote') is None, 'REFERENCE_ENTRIES_INVALID')
    return row,source


def _revision_request(c, session_id, prompt, source, reference, request_turn_id, *, live=True):
    from .voice_capture_binding import resolve_voice_capture
    rows=c.execute('''SELECT t.*,p.status AS plan_status,s.status AS step_status,s.capability_name,s.error_code AS step_error_code
        FROM agent_turn t JOIN agent_plan p ON p.agent_turn_id=t.agent_turn_id AND p.child_id=t.child_id AND p.session_id=t.session_id
        JOIN agent_step s USING(plan_id) WHERE t.agent_turn_id=? AND t.child_id=? AND t.session_id=?''',
        (request_turn_id,prompt['child_id'],session_id)).fetchall()
    require(len(rows)==1,'REVISION_REQUEST_REQUIRED')
    row=dict(rows[0])
    failed_receipt = (not live and row['plan_status']=='failed' and row['step_status']=='failed'
        and row['tool_status']=='failed' and row['tool_error_code']==row['step_error_code'])
    storage_failure = failed_receipt and row['tool_error_code']=='OperationalError'
    interrupted = failed_receipt and row['tool_error_code'] in {'APP_RESTARTED','OperationalError'}
    require((interrupted or (row['plan_status'] in ({'running'} if live else {'running','completed','waiting_child'})
        and row['step_status'] in ({'running'} if live else {'running','completed'}))) and row['capability_name']=='writing.revise_original',
            'REVISION_REQUEST_NOT_ADMITTED')
    action=json.loads(row['action_json'])
    expected=dict(prompt_id=prompt['prompt_id'],item_version=prompt['item_version'],
        reference_turn_id=reference['agent_turn_id'],source_version=source['source_version'])
    require(action.get('type')=='revise_writing_original' and action.get('writing_revision_binding')==expected
        and isinstance(row['transcript'],str) and row['transcript'].strip()
        and action.get('request_quote')==row['transcript'], 'REVISION_REQUEST_CHANGED')
    from .realtime_native_special_sources import semantic_version
    native = semantic_version(c, request_turn_id, capability='writing.revise_original', completion=not live)
    if native is None:
        ref=action.get('writing_revision_semantic_receipt')
        require(isinstance(ref,dict) and set(ref)=={'request_key'}, 'REVISION_SEMANTIC_RECEIPT_REQUIRED')
        require(c.execute("SELECT 1 FROM sqlite_master WHERE name='voice_control_intent_request'").fetchone(), 'REVISION_SEMANTIC_RECEIPT_REQUIRED')
        receipt=c.execute('SELECT * FROM voice_control_intent_request WHERE request_key=? AND child_id=? AND session_id=?',
            (ref['request_key'],prompt['child_id'],session_id)).fetchone()
        fingerprint=hashlib.sha256(json.dumps([prompt['child_id'],session_id,row['transcript']],ensure_ascii=False).encode()).hexdigest()
        require(receipt and receipt['status']=='completed' and receipt['fingerprint']==fingerprint, 'REVISION_SEMANTIC_RECEIPT_CHANGED')
        decision=json.loads(receipt['decision_json'])
        require(decision.get('status')=='control' and decision.get('control')=='writing.revise_original'
            and json.loads(receipt['binding_json']).get('writing_revision')==expected, 'REVISION_SEMANTIC_RECEIPT_CHANGED')
        require(receipt['completed_at'] and datetime.fromisoformat(reference['completed_at'])
            <= datetime.fromisoformat(receipt['created_at']) <= datetime.fromisoformat(receipt['completed_at'])
            <= datetime.now(timezone.utc), 'REVISION_SEMANTIC_RECEIPT_TIME_INVALID')
    require(datetime.fromisoformat(reference['completed_at']) <= datetime.fromisoformat(row['created_at']) <= datetime.now(timezone.utc),
            'REVISION_REQUEST_PRECEDES_REFERENCE')
    voice=None
    if action.get('voice_input_id') is not None:
        voice=resolve_voice_capture(c,session_id=session_id,child_id=prompt['child_id'],voice_input_id=action['voice_input_id'],transcript=row['transcript'])
        require(voice['prompt_id'] is None or (voice['prompt_id'],voice['item_version'])==(prompt['prompt_id'],prompt['item_version']), 'REVISION_VOICE_TARGET_CHANGED')
        voice={k:v for k,v in voice.items() if k!='raw_audio_available'}
    proof=({'kind':'native_special_source_v1','version':native} if native is not None else
        {k:receipt[k] for k in ('request_key','child_id','session_id','fingerprint','binding_json','status','decision_json','created_at','completed_at')})
    # Completion updates transport status/response, not the original request.
    original={key:row[key] for key in ('agent_turn_id','child_id','session_id','transcript','intent','created_at')}
    original.update(binding=expected,request_quote=action['request_quote'],semantic_key='native:'+native if native is not None else ref['request_key'],
        voice_input_id=action.get('voice_input_id'))
    result = {'version':digest({'turn':original,'semantic':proof,'voice':voice})}
    if storage_failure:
        # A storage exception cannot authorize a request. Only the exact prompt
        # already committed by this request can recover its historical proof.
        require(c.execute("SELECT 1 FROM sqlite_master WHERE name='writing_reference_revision'").fetchone(),
                'REVISION_COMMITTED_LINEAGE_REQUIRED')
        links=c.execute('SELECT * FROM writing_reference_revision WHERE request_turn_id=?', (request_turn_id,)).fetchall()
        require(len(links)==1, 'REVISION_COMMITTED_LINEAGE_REQUIRED')
        link=links[0]
        require((link['child_id'],link['session_id'],link['parent_prompt_id'],link['reference_turn_id'])==
            (prompt['child_id'],session_id,prompt['prompt_id'],reference['agent_turn_id']), 'REVISION_COMMITTED_LINEAGE_CHANGED')
        stored=json.loads(link['binding_json'])
        target,_=_prompt(c,session_id,link['prompt_id'],prompt['item_version'],live=False)
        require(target['prompt_id']!=prompt['prompt_id'] and target['child_id']==prompt['child_id']
            and target['diagnostic_item_id']==prompt['diagnostic_item_id'], 'REVISION_COMMITTED_LINEAGE_CHANGED')
        version=_reference_revision_version(_reference_revision_action(prompt,reference['agent_turn_id'],request_turn_id),
            source,reference,result)
        expected_link=dict(session_id=session_id,parent_prompt_id=prompt['prompt_id'],parent_item_version=prompt['item_version'],
            prompt_id=target['prompt_id'],item_version=target['item_version'],reference_turn_id=reference['agent_turn_id'],
            request_turn_id=request_turn_id,binding_version=version)
        require(stored==expected_link, 'REVISION_COMMITTED_LINEAGE_CHANGED')
        request=json.loads(target['request_json'])
        require(request.get('writing_reference_revision')=={k:expected_link[k] for k in
            ('parent_prompt_id','parent_item_version','reference_turn_id','request_turn_id','binding_version')},
            'REVISION_COMMITTED_REQUEST_CHANGED')
        require(datetime.fromisoformat(row['created_at']) <= datetime.fromisoformat(target['created_at'])
            <= datetime.fromisoformat(link['created_at']) <= datetime.now(timezone.utc), 'REVISION_COMMITTED_TIME_CHANGED')
    return result


def bind_reference_revision(connection, *, session_id, parent_prompt_id, parent_item_version,
        prompt_id, item_version, reference_turn_id, request_turn_id, binding_version, client_action_id):
    """Same transaction as real prompt preparation; no rubric or score is created."""
    c=connection
    require(c.in_transaction,'PROMPT_WRITE_TRANSACTION_REQUIRED')
    require(isinstance(client_action_id,str) and 1<=len(client_action_id)<=200,'ACTION_REQUIRED')
    target,_=_prompt(c,session_id,prompt_id,item_version,live=False)
    request=dict(session_id=session_id,parent_prompt_id=parent_prompt_id,parent_item_version=parent_item_version,
        prompt_id=prompt_id,item_version=item_version,reference_turn_id=reference_turn_id,request_turn_id=request_turn_id,binding_version=binding_version)
    encoded=json.dumps(request,sort_keys=True)
    prior=c.execute('SELECT * FROM writing_reference_revision WHERE child_id=? AND client_action_id=?',(target['child_id'],client_action_id)).fetchone()
    if prior:
        require(prior['binding_json']==encoded,'ACTION_CONFLICT')
        return dict(prior)
    _prompt(c,session_id,prompt_id,item_version,live=True)
    require(prompt_id!=parent_prompt_id,'FOLLOWUP_REQUIRES_NEW_PROMPT')
    resolved=WritingTeachingService(None).resolve_reference_revision(session_id,prompt_id=parent_prompt_id,item_version=parent_item_version,
        reference_turn_id=reference_turn_id,request_turn_id=request_turn_id,connection=c)
    require(resolved['status']=='ready' and resolved['binding_version']==binding_version,'REFERENCE_REVISION_BINDING_CHANGED')
    action=resolved['action']
    require(target['child_id']==action['child_id'] and target['diagnostic_item_id']==action['diagnostic_item_id']
        and item_version==action['item_version'],'REFERENCE_REVISION_SAME_ITEM_REQUIRED')
    request_created=c.execute('SELECT created_at FROM agent_turn WHERE agent_turn_id=?',(request_turn_id,)).fetchone()[0]
    require(datetime.fromisoformat(target['created_at'])>=datetime.fromisoformat(request_created),'FOLLOWUP_PRECEDES_REQUEST')
    require(not c.execute('SELECT 1 FROM writing_reference_revision WHERE prompt_id=? OR (parent_prompt_id=? AND request_turn_id=?)',
        (prompt_id,parent_prompt_id,request_turn_id)).fetchone(),'FOLLOWUP_ALREADY_BOUND')
    require(not c.execute('SELECT 1 FROM writing_followup WHERE prompt_id=?',(prompt_id,)).fetchone(),'FOLLOWUP_ALREADY_BOUND')
    c.execute('INSERT INTO writing_reference_revision VALUES(?,?,?,?,?,?,?,?,?)',(prompt_id,parent_prompt_id,target['child_id'],
        session_id,reference_turn_id,request_turn_id,encoded,client_action_id,datetime.now(timezone.utc).isoformat()))
    return dict(c.execute('SELECT * FROM writing_reference_revision WHERE prompt_id=?',(prompt_id,)).fetchone())


def bind_writing_followup(connection, *, session_id, parent_prompt_id, parent_item_version,
                          prompt_id, item_version, kind, binding_version, client_action_id,parent_session_id=None):
    """Invoke inside actual prompt creation transaction. Do not commit on failure."""
    c = connection
    require(c.in_transaction, 'PROMPT_WRITE_TRANSACTION_REQUIRED')
    require(isinstance(client_action_id, str) and 1 <= len(client_action_id) <= 200, 'ACTION_REQUIRED')
    target, _ = _prompt(c, session_id, prompt_id, item_version, live=False)
    request = dict(session_id=session_id,parent_prompt_id=parent_prompt_id,parent_item_version=parent_item_version,
        prompt_id=prompt_id,item_version=item_version,kind=kind,binding_version=binding_version)
    if parent_session_id is not None:
        require(isinstance(parent_session_id,str) and parent_session_id!=session_id,'FOLLOWUP_PARENT_SESSION_INVALID')
        request['parent_session_id']=parent_session_id
    encoded = json.dumps(request, sort_keys=True)
    prior = c.execute('SELECT * FROM writing_followup WHERE child_id=? AND client_action_id=?',
                      (target['child_id'],client_action_id)).fetchone()
    if prior:
        require(prior['binding_json'] == encoded, 'ACTION_CONFLICT')
        return dict(prior)
    _prompt(c, session_id, prompt_id, item_version, live=True)
    require(prompt_id != parent_prompt_id, 'FOLLOWUP_REQUIRES_NEW_PROMPT')
    resolved = WritingTeachingService(None).resolve_next_action(parent_session_id or session_id, prompt_id=parent_prompt_id,
        item_version=parent_item_version, kind=kind, diagnostic_item_id=target['diagnostic_item_id'],target_session_id=session_id, connection=c)
    require(resolved['status'] == 'ready' and resolved['binding_version'] == binding_version, 'FOLLOWUP_BINDING_CHANGED')
    action = resolved['action']
    require(action['child_id'] == target['child_id'] and action['item_version'] == item_version, 'FOLLOWUP_SCOPE_CHANGED')
    assessment = RubricEvidenceService(None).get(target['child_id'], action['assessment_id'], connection=c)
    feedback_at = assessment['review']['created_at'] if assessment['review'] else assessment['created_at']
    require(datetime.fromisoformat(target['created_at']) >= datetime.fromisoformat(feedback_at), 'FOLLOWUP_PRECEDES_FEEDBACK')
    require(not c.execute('''SELECT 1 FROM writing_followup WHERE prompt_id=? OR
        (parent_prompt_id=? AND assessment_id=? AND kind=?)''', (prompt_id,parent_prompt_id,action['assessment_id'],kind)).fetchone(),
        'FOLLOWUP_ALREADY_BOUND')
    c.execute('INSERT INTO writing_followup VALUES(?,?,?,?,?,?,?,?,?)', (prompt_id,parent_prompt_id,target['child_id'],
        session_id,action['assessment_id'],kind,encoded,client_action_id,datetime.now(timezone.utc).isoformat()))
    return dict(c.execute('SELECT * FROM writing_followup WHERE prompt_id=?', (prompt_id,)).fetchone())
