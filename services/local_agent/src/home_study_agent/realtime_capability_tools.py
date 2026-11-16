"""Native cloud capability proposals; all authority and execution remain local.

No model API, ASR, semantic classifier, private-context upload, or schema migration.
The host must authenticate the actual supplier input/call envelope. A model chooses
concrete registered capabilities; it never supplies source IDs or evidence grants.
"""
from __future__ import annotations
import asyncio
from datetime import datetime, timezone
import hashlib
import inspect
import json
import re
from uuid import uuid4

from .agent_runtime import (AgentRuntimeStore, CapabilityRegistry, CapabilitySpec,
    DEFAULT_CAPABILITIES, InvalidAgentPlanError, LOCAL_COMPOSED_CAPABILITIES, OBJECTIVE_NEUTRAL_CAPABILITIES, OBJECTIVE_MODES, OBJECTIVE_TYPES,
    action_from_plan, parse_agent_plan_output)
from .learning_input_revision import LearningInputRevisionService
from .privacy_operation_receipt import generation

VERSION = 1
PROTOCOL = 'qwen_runtime_capability_v1'
SOURCE = 'qwen_realtime_capability'
KEY = 'qwen_realtime_capability_v1'
FACTS = 'runtime_execution_facts_v1'
# Static semantic contracts; every effect still requires its original local
# source, scope, claim and real execution receipt.
NATIVE_CAPABILITIES = frozenset(item.name for item in DEFAULT_CAPABILITIES) | {'learning.prepare_request'}
PREPARE = 'learning.prepare_request'
PREPARE_SPEC = CapabilitySpec(PREPARE, '1',
    '保存孩子本轮新学习或复习请求及教材澄清；可继续先前同一请求。缺教材版本、课题名称或已核验来源时只返回实际缺项，不编造课文，不改孩子档案年级，不评分。',
    ('learning',), (), 'server', 'low', (), ('learning_request_saved',), 'prepare_learning_request')
# Deliberately not a confidence estimate from the supplier. This adapter does not
# mint a ModelGateway verified model_call or an independent learning observation.
UNKNOWN_CONFIDENCE = 0.0
INPUT_IDENTITY = ('voice_input_id', 'supplier_session_id', 'input_item_id', 'response_id')
IDENTITY = INPUT_IDENTITY + ('call_id',)
META = ('subject', 'requested_grade', 'lesson', 'textbook_edition', 'textbook_volume', 'lesson_title')
ANSWER_CAPS = frozenset(('learning.evaluate_response', 'diagnostic.submit', 'exam.submit',
                         'pet_vocabulary.submit', 'mistake.submit_response', 'mistake.dialogue'))
UNCERTAIN_ERRORS = frozenset(('TimeoutError','ReadTimeout','WriteTimeout','ConnectionError','BrokenPipeError',
    'MODEL_TIMEOUT','REMOTE_OUTCOME_UNKNOWN','UNKNOWN_OUTCOME'))
FORBIDDEN_MODEL = frozenset(('child_id','session_id','voice_input_id','client_action_id','source',
    'local_only','cloud_transmission_allowed','mastery_update_allowed','model_call_id','provenance'))

class RealtimeCapabilityConflict(ValueError):
    pass


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _fail(code):
    raise RealtimeCapabilityConflict(code)


def _identity(value, *, require_call=True):
    if type(value) is not dict or value.get('version') != VERSION or type(value.get('version')) is not int:
        _fail('REALTIME_CAPABILITY_ENVELOPE_INVALID')
    fields = IDENTITY if require_call else INPUT_IDENTITY
    for name in fields:
        if type(value.get(name)) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{1,160}', value[name]):
            _fail('REALTIME_CAPABILITY_IDENTITY_INVALID')
    return {key:value[key] for key in fields}


def _immutable_capture(capture):
    value = AgentRuntimeStore.voice_capture_snapshot(capture)
    # An old question's *current* validity is not authority over unrelated new
    # learning. Answer tools recheck the full original reference separately.
    value.pop('question_validation', None)
    value.pop('activity_validation', None)
    return value


class RealtimeCapabilityTools:
    def __init__(self, database, *, runtime=None, registry=None, authorize=None,
                 execute_turn=None, page_source_store=None, authorize_receipt=None, complete_turn=None,
                 current_graph_version=None):
        self.database = database
        self.runtime = runtime or AgentRuntimeStore(database, page_source_store=page_source_store)
        self.registry = registry or CapabilityRegistry(DEFAULT_CAPABILITIES + (PREPARE_SPEC,))
        self.authorize = authorize  # required literal-True host-owned input/call validator
        self.authorize_receipt = self.authorize if authorize_receipt is None else authorize_receipt
        self.complete_turn = complete_turn
        self.current_graph_version = current_graph_version
        self.digest = _hash
        self.execute_turn = execute_turn  # existing app execute_agent_turn dispatcher
        self.page_source_store = page_source_store
        self.arrivals = LearningInputRevisionService(database)
        self.arrivals.initialize()

    @staticmethod
    def tool_name(capability):
        name='runtime_' + capability.replace('.', '_')
        return name if len(name)<=64 else name[:55]+'_'+_hash(capability)[:8]

    def catalog(self, session_id):
        with self.database.connect() as c:
            owner = c.execute('SELECT state,status FROM daily_session WHERE session_id=?', (session_id,)).fetchone()
            if not owner or owner['status'] != 'active': _fail('REALTIME_CAPABILITY_OWNER_CHANGED')
            values=[]
            # Native tools are static protocol contracts across this connection.
            # Actual stage availability is checked at every admission, not frozen
            # at connect (otherwise begin-break hides resume-break until reconnect).
            for spec in self.registry.catalog():
                name=spec['name']

                from .realtime_native_contracts import schema as public_schema
                schema=public_schema(spec)
                if name==PREPARE:
                    schema={'type':'object','properties':{key:{'type':'string','maxLength':120} for key in META},'additionalProperties':False}
                values.append({'name':name,'tool_name':self.tool_name(name),
                    'description':spec['description'],'required_arguments':list(schema.get('required',[])),
                    'arguments_schema':schema,'execution_location':spec['execution_location'],
                    'allowed_states':list(spec['allowed_states'])})
        return {'version':VERSION,'protocol':PROTOCOL,'capabilities':values}

    def _scope(self, c, session_id, envelope, *, require_permission=True, _authorize=None, require_owner=True, execution_turn_id=None):
        identity=_identity(envelope, require_call='call_id' in envelope)
        session=c.execute('''SELECT d.*,p.status AS child_status FROM daily_session d
            JOIN child_profile p ON p.child_id=d.child_id WHERE d.session_id=?''', (session_id,)).fetchone()
        if not session or require_owner and (session['status'] != 'active' or session['child_status'] != 'active'):
            _fail('REALTIME_CAPABILITY_OWNER_CHANGED')
        if require_permission:
            consent=c.execute('SELECT cloud_audio_allowed,cloud_model_text_allowed FROM privacy_preference WHERE singleton_id=1').fetchone()
            if not consent or any(type(consent[k]) is not int or consent[k] != 1 for k in ('cloud_audio_allowed','cloud_model_text_allowed')):
                _fail('REALTIME_CAPABILITY_CONSENT_REQUIRED')
        voice=c.execute('SELECT * FROM voice_input WHERE voice_input_id=? AND session_id=? AND child_id=?',
                        (identity['voice_input_id'], session_id, session['child_id'])).fetchone()
        if not voice or voice['status'] != 'completed' or not voice['transcript'].strip():
            _fail('REALTIME_CAPABILITY_INPUT_NOT_ADMITTED')
        from .realtime_native_safety import native_input_blocked
        if native_input_blocked(c,voice):_fail('REALTIME_CAPABILITY_INPUT_LOCAL_ONLY')
        capture=self.runtime.resolve_captured_voice(c,session_id=session_id,child_id=session['child_id'],
            voice_input_id=voice['voice_input_id'],transcript=voice['transcript'],page_source_store=self.page_source_store,
            _execution_turn_id=execution_turn_id)
        if capture.get('source_kind')=='household_local_transcript':
            from .realtime_household_input import verify_household_witness
            if voice['purpose']!='household_local_transcript' or verify_household_witness(c,dict(voice)) is not True:_fail('REALTIME_CAPABILITY_INPUT_NOT_REALTIME')
        elif capture.get('source_kind') != 'realtime_transcript': _fail('REALTIME_CAPABILITY_INPUT_NOT_REALTIME')
        if envelope.get('input_source')=='household_local_transcript' and voice['purpose']!='household_local_transcript':_fail('REALTIME_NATIVE_INPUT_SOURCE_INVALID')
        if capture.get('local_recovery_only') is True:_fail('REALTIME_CAPABILITY_INPUT_LOCAL_ONLY')
        try:
            authorizer=self.authorize if _authorize is None else _authorize
            owned=authorizer is not None and authorizer(c,session_id,session['child_id'],dict(envelope),dict(voice)) is True
        except Exception:
            owned=False
        if not owned: _fail('REALTIME_CAPABILITY_INPUT_OWNER_UNCONFIRMED')
        return dict(session),dict(voice),capture

    @staticmethod
    def _packet(c, row):
        plan=c.execute('SELECT context_manifest_json FROM agent_plan WHERE agent_turn_id=?', (row['agent_turn_id'],)).fetchone()
        return json.loads(plan[0]).get(KEY) if plan else None

    def _answer_authority(self, c, session, voice, capture, capability, args):
        # Explicit score tool only. Native speech itself is never an answer.
        if capability not in ANSWER_CAPS: return
        if capture.get('source_kind')=='household_local_transcript' or capture.get('question_validation') != 'valid': _fail('REALTIME_NO_SCORING_AUTHORITY')
        mapping={'learning.evaluate_response':('learning_reply','evaluate_child_response'),
            'diagnostic.submit':('diagnostic','submit_diagnostic_answer'),'exam.submit':('exam','submit_exam_answer'),
            'pet_vocabulary.submit':('pet_vocabulary','submit_pet_vocabulary_answer'),
            'mistake.submit_response':('mistake','continue_mistake_dialogue'),'mistake.dialogue':('mistake','continue_mistake_dialogue')}
        kind,action=mapping[capability];reference=self.runtime.captured_question(capture)
        if not reference or reference['kind']!=kind or not self.runtime.capture_matches_answer(capture,action,reference['binding']):
            _fail('REALTIME_NO_SCORING_AUTHORITY')
        field='response' if capability=='learning.evaluate_response' else 'utterance' if capability=='mistake.dialogue' else 'answer'
        if args.get(field)!=voice['transcript']:_fail('REALTIME_ANSWER_REWRITE_FORBIDDEN')

    def _prepare_args(self, c, session, voice, args, objective, *, speech_started_at=None):
        if set(args) - set(META): _fail('REALTIME_LEARNING_FIELDS_INVALID')
        # These are low-risk *proposed* request metadata from the same native
        # audio/model call, not transcript copying, textbook truth or a profile
        # update. The canonical original is stored independently by the host.
        for key,value in args.items():
            if type(value) is not str or not value.strip() or len(value)>120:
                _fail('REALTIME_LEARNING_FIELDS_INVALID')
        unconfirmed=[]
        from .realtime_native_curriculum import check_input_scope, current_request
        prior=current_request(c,session['child_id'],session['session_id'])
        mode=(objective or {}).get('mode')
        if mode is None:
            mode='continue' if prior and all(key not in args or args[key]==prior['requested'].get(key)
                for key in ('subject','requested_grade')) else 'new'
        if mode=='new' and prior and args and set(args) <= {'lesson','textbook_edition','textbook_volume','lesson_title'}:
            mode='continue'  # A scope-only clarification cannot detach from its saved course.
        if mode not in {'new','continue'} or mode=='continue' and not prior:
            _fail('REALTIME_LEARNING_GOAL_NOT_CURRENT')
        if mode=='continue':
            check_input_scope(prior,voice,speech_started_at=speech_started_at)
            args={**prior['requested'],**args}
        goal={'mode':mode,'type':'learning','summary':prior['summary'] if mode=='continue' else voice['transcript'][:300],
              'success_criteria':['核对教材与课题的真实来源','形成有来源的复习与学习证据'],'phase':'material_clarification'}
        return args,goal,unconfirmed

    def _restored_chain(self,c,session,voice,envelope):
        if envelope.get('input_source')!='persisted_native':return
        from .realtime_native_context import _native_witness
        if _native_witness(self,c,voice) is None:_fail('REALTIME_NATIVE_RESTORED_SOURCE_INVALID')
        rows=c.execute("""SELECT * FROM agent_turn WHERE session_id=? AND child_id=?
            AND json_extract(action_json,'$.voice_input_id')=?
            AND intent IN ('qwen_native_dialogue_unverified','qwen_realtime_capability')""",
            (session['session_id'],session['child_id'],voice['voice_input_id'])).fetchall()
        for previous in rows:
            if previous['intent']=='qwen_realtime_capability':
                packet=self._packet(c,previous)
                source=(packet or {}).get('input_source');identity=(packet or {}).get('identity') or {}
            else:
                action=json.loads(previous['action_json']);source=action.get('input_source');identity=action.get('native_identity') or {}
            if source!='persisted_native' or identity.get('supplier_session_id')!=envelope['supplier_session_id']:
                _fail('REALTIME_NATIVE_RESTORED_SOURCE_ALREADY_CONSUMED')

    def _formal_lesson(self,c,session,voice,capture,args):
        prior=c.execute('''SELECT o.objective_id,t.agent_turn_id,t.action_json,s.result_json
            FROM agent_objective o JOIN agent_plan p USING(objective_id) JOIN agent_step s USING(plan_id)
            JOIN agent_turn t USING(agent_turn_id) WHERE o.child_id=? AND o.session_id=? AND o.status='active'
              AND s.capability_name=? AND s.status='completed' ORDER BY p.rowid DESC LIMIT 1''',
            (session['child_id'],session['session_id'],PREPARE)).fetchone()
        active=c.execute("SELECT objective_id,objective_type FROM agent_objective WHERE child_id=? AND session_id=? AND status='active'",(session['child_id'],session['session_id'])).fetchone()
        if not prior:
            if active and active['objective_type']=='learning':return None
            if args.get('material_scope')=='prepared_material':_fail('REALTIME_LESSON_MATERIAL_SOURCE_REQUIRED')
            return {'objective':{'mode':'new','type':'learning','summary':voice['transcript'][:300],
                'success_criteria':['根据真实讲解提出可核验的理解检查','实际作答与核验后再记录学习结果'],'phase':'explain'},
                'lineage':{'version':1,'prepare_objective_id':None,'prepare_turn_id':None,'metadata_status':'none','material_source_verified':False}}
        if json.loads(prior['action_json']).get('source')!=SOURCE:return None
        other=c.execute('SELECT 1 FROM agent_plan p JOIN agent_step s USING(plan_id) WHERE p.objective_id=? AND s.capability_name!=? LIMIT 1',(prior['objective_id'],PREPARE)).fetchone()
        if other:return None
        requested=(json.loads(prior['result_json']).get('result') or {}).get('requested') or {}
        if args.get('question_source')!='current_question' and (args.get('material_scope')=='prepared_material' or args.get('material_scope') is None and any(requested.get(k) for k in ('requested_grade','lesson','textbook_edition','lesson_title'))):
            _fail('REALTIME_LESSON_MATERIAL_SOURCE_REQUIRED')
        return {'objective':{'mode':'new','type':'learning','summary':voice['transcript'][:300],
            'success_criteria':['根据真实讲解提出可核验的理解检查','实际作答与核验后再记录学习结果'],'phase':'explain'},
            'lineage':{'version':1,'prepare_objective_id':prior['objective_id'],'prepare_turn_id':prior['agent_turn_id'],
                'metadata_status':'proposed','material_source_verified':False}}

    def admit(self, session_id, proposal):
        identity=_identity(proposal)
        allowed=set(IDENTITY)|{'version','capability_name','args','objective','previous_action_id','input_source'}
        if set(proposal)-allowed or type(proposal.get('capability_name')) is not str or type(proposal.get('args')) is not dict:
            _fail('REALTIME_CAPABILITY_PROPOSAL_INVALID')
        if proposal.get('input_source') not in (None,'persisted_native','household_local_transcript'):_fail('REALTIME_NATIVE_INPUT_SOURCE_INVALID')
        if len(_json(proposal))>24000 or set(proposal['args']) & FORBIDDEN_MODEL:
            _fail('REALTIME_CAPABILITY_PROPOSAL_INVALID')
        if proposal['capability_name']=='system.inspect_learning' and set(proposal['args'])-{'scope','track'}:
            _fail('REALTIME_CAPABILITY_ARGUMENTS_INVALID')
        action_id='qwen-tool-'+_hash([session_id,identity])
        fingerprint=_hash(proposal)
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            existing=c.execute('SELECT * FROM agent_turn WHERE client_action_id=?', (action_id,)).fetchone()
            if existing:
                packet=self._packet(c,existing)
                if not packet or packet['fingerprint']!=fingerprint: _fail('REALTIME_CAPABILITY_REPLAY_CONFLICT')
                return self._receipt(c,existing,replayed=True)
            session,voice,capture=self._scope(c,session_id,proposal)
            # A reconnect/new call cannot bypass an unresolved previous effect.
            earlier=c.execute('''SELECT t.*,s.status AS step_status,s.error_code AS step_error FROM agent_turn t JOIN agent_plan p USING(agent_turn_id)
                JOIN agent_step s USING(plan_id) WHERE t.session_id=? AND t.child_id=?
                  AND json_extract(p.context_manifest_json,?) IS NOT NULL ORDER BY t.rowid DESC''',
                (session_id,session['child_id'],'$.'+KEY)).fetchall()
            semantic=_hash([identity['voice_input_id'],proposal['capability_name'],proposal['args'],proposal.get('objective')])
            for previous in earlier:
                packet=self._packet(c,previous)
                if packet['semantic_fingerprint']==semantic:
                    return self._receipt(c,previous,replayed=True)
            from .realtime_native_client import require_resolved_chain
            require_resolved_chain(self,c,session_id,session['child_id'],identity['voice_input_id'])
            self._restored_chain(c,session,voice,proposal)
            same_input=[r for r in earlier if self._packet(c,r)['identity']['voice_input_id']==identity['voice_input_id']]
            if same_input and proposal.get('previous_action_id') != same_input[0]['client_action_id']:
                _fail('REALTIME_CAPABILITY_CONTINUATION_RECEIPT_REQUIRED')
            capability=self.registry.require(proposal['capability_name'],session['state'],arguments=proposal['args'])
            from .realtime_native_contracts import compose_arguments,validate_public_arguments
            validate_public_arguments(capability,proposal['args'])
            args=compose_arguments(c,session,voice,capability,proposal['args']);objective=proposal.get('objective');unconfirmed=[]
            if objective is not None and (type(objective) is not dict or set(objective)-{'mode','type','summary','success_criteria','phase'}):
                _fail('REALTIME_CAPABILITY_OBJECTIVE_INVALID')
            if capability.name==PREPARE:
                args,objective,unconfirmed=self._prepare_args(c,session,voice,args,objective,
                    speech_started_at=capture.get('speech_started_at'))
            elif capability.name=='learning.evaluate_response':
                # The explicit scoring operation must continue its exact frozen
                # learning goal. A supplier cannot switch objectives while using
                # another question's answer reservation.
                reference=self.runtime.captured_question(capture)
                self._answer_authority(c,session,voice,capture,capability.name,args)
                objective={'mode':'continue','type':'learning','summary':None,'success_criteria':[],
                    'phase':reference['binding']['phase']}
            elif objective is None:
                objective={'mode':'none','type':None,'summary':None,'success_criteria':[],'phase':'execute'}
            if capture.get('source_kind')=='household_local_transcript' and (capability.name=='tutor.capture_check_response' or capability.name=='learning.review_revisit' and proposal['args'].get('operation') not in {'start','propose','confirm','cancel','status','select_1','select_2','select_3','resume_decline'}):_fail('REALTIME_NO_SCORING_AUTHORITY')
            if capability.name=='tutor.playback_control' and not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tutor_playback_checkpoint'").fetchone():
                _fail('REALTIME_TUTOR_PLAYBACK_REQUIRED')
            turn_id=str(uuid4());now=_now()
            arrival=self.arrivals.capture_arrival(session_id,action_id,voice['transcript'],connection=c)
            from .realtime_native_sources import compose as compose_sources
            from .mistake_learning import graph_version_from_reader
            graph_version=graph_version_from_reader(self.current_graph_version,c) if capability.name=='mistake.learn' else None
            composed=compose_sources(c,session,voice,capture,capability.name,args,self.runtime,arrival_id=arrival['arrival_id'],turn_id=turn_id,current_graph_version=graph_version)
            args=composed['arguments']
            from .realtime_native_special_sources import compose as compose_special
            special=compose_special(c,session,voice,capture,capability.name,args,self.runtime,arrival_id=arrival['arrival_id'],turn_id=turn_id)
            if special is not None:
                args=special['arguments'];composed={'arguments':args,'action':{**composed['action'],**special['action']},'manifest':{**composed['manifest'],**special['manifest']}}
            from .realtime_native_document_controls import compose as compose_document
            document=compose_document(c,session,voice,capture,capability.name,args,self.runtime,arrival_id=arrival['arrival_id'],turn_id=turn_id)
            if document is not None:
                args=document['arguments'];objective=document.get('objective',objective)
                composed={'arguments':args,'action':{**composed['action'],**document['action']},'manifest':{**composed['manifest'],**document['manifest']}}
            self._answer_authority(c,session,voice,capture,capability.name,args)
            if capability.name=='learning.explain':
                from .realtime_native_curriculum import KEY as CURRICULUM, check_input_scope, current_request, freeze
                prepared=current_request(c,session['child_id'],session_id)
                if (prepared and args.get('question_source')=='spoken_request'
                        and args['subject']==prepared['requested'].get('subject')
                        and (args.get('material_scope') is None or any(
                            r['agent_turn_id']==prepared['prepare_turn_id'] and r['step_status']=='completed' for r in same_input))):
                    # The already validated same-input predecessor chain takes
                    # precedence over an incidental supplier scope label.
                    args['material_scope']='prepared_material'
                if args.get('material_scope')=='prepared_material' and args.get('question_source')=='spoken_request':
                    if not prepared or args['subject']!=prepared['requested'].get('subject'):
                        _fail('REALTIME_LESSON_MATERIAL_SOURCE_REQUIRED')
                    check_input_scope(prepared,voice,speech_started_at=capture.get('speech_started_at'))
                    composed['manifest'][CURRICULUM]=freeze(c,session['child_id'],prepared)
                    objective={'mode':'continue','type':'learning','summary':None,'success_criteria':[], 'phase':'explain'}
                else:
                    lesson=self._formal_lesson(c,session,voice,capture,args)
                    if lesson is not None:
                        objective=lesson['objective'];composed['manifest']['native_prepare_lesson_lineage_v1']=lesson['lineage']
            if capability.name=='learning.evaluate_response':
                from .ordinary_learning_text_projection import _source,TextProjectionBlocked
                try:question=_source(c,self.runtime.captured_question(capture))
                except (TextProjectionBlocked,ValueError,KeyError,TypeError):_fail('REALTIME_NO_SCORING_AUTHORITY')
                if not question.get('question'):_fail('REALTIME_NO_SCORING_AUTHORITY')
                args={'response':voice['transcript'],'subject':question['subject'],'question_asked':question['question']}
            from .realtime_native_authority import compose_native_control,KEY as AUTHORITY_KEY
            authority=compose_native_control(c,session,capability.name,args)
            if authority is not None:
                objective={'mode':'none','type':None,'summary':None,'success_criteria':[],'phase':'execute'}
            # A supplier's incidental goal wording cannot turn a declared
            # goal-neutral software/sensor operation into a learning transition.
            # Normalize before parse, whose capability/type check is stricter.
            if capability.name in OBJECTIVE_NEUTRAL_CAPABILITIES:
                # Retain the original parser's global objective validity. Only
                # legitimate but irrelevant goal semantics may be discarded.
                mode=str(objective.get('mode','')).strip();kind=objective.get('type')
                criteria=objective.get('success_criteria') or []
                summary=objective.get('summary')
                if (mode not in OBJECTIVE_MODES or kind is not None and (type(kind) is not str or kind not in OBJECTIVE_TYPES)
                    or mode in {'new','continue'} and kind not in OBJECTIVE_TYPES
                    or not isinstance(criteria,list)
                    or mode=='new' and (summary is None or not str(summary).strip() or not any(str(item).strip() for item in criteria))):
                    _fail('REALTIME_CAPABILITY_OBJECTIVE_INVALID')
                objective={'mode':'none','type':None,'summary':None,'success_criteria':[],'phase':'execute'}
            normalized={'objective':objective,'next_step':{'capability':capability.name,'arguments':args,'reason':'Native supplier proposed a concrete registered capability'},
                'utterance_binding':{'role':'answer' if capability.name in ANSWER_CAPS else 'task_request','quote':voice['transcript']},
                'spoken_transition':'能力请求已保存，执行结果尚未确认。','requires_child_reply':capability.name in {PREPARE,'learning.explain','learning.request_response','diagnostic.start','diagnostic.replay','pet_vocabulary.start','exam.start'},
                'confidence':UNKNOWN_CONFIDENCE,'after_execution':'stop'}
            if capability.name in LOCAL_COMPOSED_CAPABILITIES:
                # Local IDs come only from actual presented check/failed-coaching
                # sources above, never from the supplier's argument object.
                from .agent_runtime import AgentPlanDecision,PlannedStep,ObjectiveDraft
                decision=AgentPlanDecision(ObjectiveDraft('none',None,None,(),'execute'),
                    PlannedStep(capability,args,'Native concrete tool with local source binding',capability.expected_evidence),
                    normalized['spoken_transition'],False,0.0,'stop',())
            else:
                decision=parse_agent_plan_output(_json(normalized),session['state'],self.registry)
            action=action_from_plan(decision,voice['transcript'],None)
            action.update(composed['action'])
            action.update(source=SOURCE,local_only=True,cloud_transmission_allowed=False,voice_input_id=voice['voice_input_id'],
                voice_input_binding=self.runtime.voice_capture_snapshot(capture),mastery_update_allowed=False,independent_evidence=False)
            packet={'version':VERSION,'identity':identity,'fingerprint':fingerprint,'semantic_fingerprint':semantic,
                    'storage_generation':generation(c),'capture_hash':_hash(_immutable_capture(capture)),
                    'original_transcript_sha256':_hash(voice['transcript']),'root_action_id':'realtime-'+voice['voice_input_id'],
                    'proposal_unverified':True,'model_call_verified':False,'input_source':proposal.get('input_source'),
                    'capability':capability.name,'arguments_sha256':_hash(args),
                    'native_sources_sha256':_hash(composed['manifest']),'native_manifest_keys':sorted(composed['manifest']),
                    'action_authority_sha256':_hash({k:action.get(k) for k in ('type','source','voice_input_id','voice_input_binding','capability','execution_location','mastery_update_allowed','independent_evidence')})}
            from .ordinary_learning_text_projection import prepare_input_policy
            input_action=dict(action)
            if (capability.name=='learning.explain' and (args.get('question')==voice['transcript']
                    or 'native_authorized_spoken_question_v1' in composed['manifest'])
                    and voice['purpose'] in {'native_cloud_dialogue','household_local_transcript'}
                    and capture.get('source_kind') in {'realtime_transcript','household_local_transcript'}):
                # Actual authorized original spoken question, before the local
                # transport envelope. No private target/content flag is removed.
                input_action.pop('local_only',None);input_action.pop('cloud_transmission_allowed',None)
            input_policy=prepare_input_policy(input_action,decision,current_text_authorized=True,
                voice_capture=capture,confirmed_answer=None)
            c.execute('''INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,action_json,
                orchestrator_version,created_at,client_action_id) VALUES(?,?,?,?,?,?,?,?,?,?)''',
                (turn_id,session['child_id'],session_id,voice['transcript'],SOURCE,decision.spoken_transition,_json(action),PROTOCOL,now,action_id))
            if authority is not None:packet['native_authority_sha256']=_hash(authority)
            self.runtime.persist_plan(c,child_id=session['child_id'],session_id=session_id,agent_turn_id=turn_id,
                client_action_id=action_id,decision=decision,context_manifest={KEY:packet,'learning_input_arrival_id':arrival['arrival_id'],**composed['manifest'],**({AUTHORITY_KEY:authority} if authority is not None else {})})
            if input_policy is not None:
                from .ordinary_learning_text_projection import mint_input_policy
                mint_input_policy(c,turn_id,input_policy)
            if capability.name==PREPARE:
                missing=[key for key in ('textbook_edition','lesson_title') if not args.get(key)]
                result={'capability':PREPARE,'response_text':'学习请求已保存，教材来源仍需核对。','result':{
                    'request_saved':True,'requested':args,'material_status':'needs_material_identity' if missing else 'needs_source_verification',
                    'missing_fields':missing,'unconfirmed_fields':unconfirmed,'metadata_status':'proposed','source_verified':False,'requires_child_reply':True},'evidence':['learning_request_saved']}
                self.runtime.complete_turn_execution(c,turn_id,True,None,result)
                c.execute("UPDATE agent_turn SET tool_status='completed',response_text=?,completed_at=? WHERE agent_turn_id=?",(result['response_text'],now,turn_id))
            row=c.execute('SELECT * FROM agent_turn WHERE agent_turn_id=?',(turn_id,)).fetchone()
            value=self._receipt(c,row)
            from .realtime_native_client import required,dispatch
            if required(capability.execution_location,capability.name):value['client_dispatch']=dispatch(self,c,row)
            c.commit();return value

    def validate_turn(self, c, turn_id, *, require_permission=True):
        row=c.execute('SELECT * FROM agent_turn WHERE agent_turn_id=?',(turn_id,)).fetchone()
        packet=self._packet(c,row) if row else None
        if not packet:
            if row and (json.loads(row['action_json']).get('source')==SOURCE or str(row['client_action_id']).startswith('qwen-tool-')):
                _fail('REALTIME_CAPABILITY_TURN_CHANGED')
            return False
        envelope={'version':VERSION,**packet['identity']}
        session,voice,capture=self._scope(c,row['session_id'],envelope,require_permission=require_permission,execution_turn_id=turn_id)
        if packet['storage_generation']!=generation(c) or packet['capture_hash']!=_hash(_immutable_capture(capture)) or packet['original_transcript_sha256']!=_hash(voice['transcript']):
            _fail('REALTIME_CAPABILITY_INPUT_CHANGED')
        manifest=json.loads(c.execute('SELECT context_manifest_json FROM agent_plan WHERE agent_turn_id=?',(turn_id,)).fetchone()[0])
        if packet.get('native_sources_sha256')!=_hash({key:manifest.get(key) for key in packet.get('native_manifest_keys',[])}):_fail('REALTIME_CAPABILITY_TURN_CHANGED')
        action=json.loads(row['action_json'])
        if action.get('source')!=SOURCE or action.get('voice_input_id')!=voice['voice_input_id']:
            _fail('REALTIME_CAPABILITY_TURN_CHANGED')
        step=c.execute('SELECT capability_name,arguments_json FROM agent_step JOIN agent_plan USING(plan_id) WHERE agent_turn_id=? AND step_index=0',(turn_id,)).fetchone()
        if step['capability_name']!=packet['capability'] or _hash(json.loads(step['arguments_json']))!=packet['arguments_sha256'] or _hash({k:action.get(k) for k in ('type','source','voice_input_id','voice_input_binding','capability','execution_location','mastery_update_allowed','independent_evidence')})!=packet['action_authority_sha256']:
            _fail('REALTIME_CAPABILITY_TURN_CHANGED')
        from .realtime_native_special_sources import source_witness
        source_witness(c,row,action,manifest,step['capability_name'])
        if step['capability_name']=='mistake.learn':
            from .realtime_native_mistake_learning import validate as validate_mistake
            from .mistake_learning import graph_version_from_reader
            validate_mistake(c,turn_id,require_claim=False,current_graph_version=graph_version_from_reader(self.current_graph_version,c))
        self._answer_authority(c,session,voice,capture,step['capability_name'],json.loads(step['arguments_json']))
        return True

    def validate_execution_scope(self,c,turn_id):
        row=c.execute('SELECT * FROM agent_turn WHERE agent_turn_id=?',(turn_id,)).fetchone()
        packet=self._packet(c,row) if row else None
        if not packet:return False
        _,voice,capture=self._scope(c,row['session_id'],{'version':VERSION,**packet['identity']},require_owner=True)
        return (packet['storage_generation']==generation(c) and packet['capture_hash']==_hash(_immutable_capture(capture))
            and packet['original_transcript_sha256']==_hash(voice['transcript']))

    @staticmethod
    def _state(status,error,claimed):
        if status=='completed': return 'completed'
        if status=='failed': return 'unknown' if error in UNCERTAIN_ERRORS else 'failed'
        if status=='cancelled': return 'failed'
        return 'unknown' if claimed else 'pending'

    def _receipt(self, c, row, *, replayed=False, retired=False):
        packet=self._packet(c,row)
        if not packet:_fail('REALTIME_CAPABILITY_RECEIPT_NOT_FOUND')
        # Read authority is purpose-scoped separately from dispatch authority:
        # a new owned connection can read an old call without acquiring its execute grant.
        _,voice,capture=self._scope(c,row['session_id'],{'version':VERSION,**packet['identity']},
            require_permission=False,_authorize=self.authorize_receipt,require_owner=not retired)
        if packet['storage_generation']!=generation(c) or packet['capture_hash']!=_hash(_immutable_capture(capture)) or packet['original_transcript_sha256']!=_hash(voice['transcript']):
            _fail('REALTIME_CAPABILITY_INPUT_CHANGED')
        step=c.execute('SELECT s.*,p.status AS plan_status,p.objective_id FROM agent_step s JOIN agent_plan p USING(plan_id) WHERE p.agent_turn_id=? AND s.step_index=0', (row['agent_turn_id'],)).fetchone()
        claim=c.execute('SELECT 1 FROM audit_event WHERE audit_id=?',('qwen-execution-claim-'+row['agent_turn_id'],)).fetchone()
        status=self._state(step['status'],step['error_code'],bool(claim))
        from .realtime_native_client import status as client_status
        status=client_status(self,c,row,status)
        facts={'version':VERSION,'capability':step['capability_name'],'execution_status':status,
               'dialogue_is_learning_evidence':False,'source_kind':'runtime_execution'}
        if status=='completed' and step['capability_name']==PREPARE:
            saved=json.loads(step['result_json'])['result']
            facts.update({key:saved[key] for key in ('request_saved','requested','material_status','missing_fields','unconfirmed_fields','metadata_status','source_verified')})
        if status=='completed' and step['capability_name'] in {'task.pause','break.begin','break.resume'}:
            saved=json.loads(step['result_json']).get('result') or {}
            if step['capability_name']=='task.pause':
                if saved.get('status') in {'paused','no_active_task'}:facts['task_state']=saved['status']
                if type(saved.get('active_seconds')) is int and saved['active_seconds']>=0:facts['active_seconds']=saved['active_seconds']
            else:
                receipt=saved.get('break_receipt') or {}
                if receipt.get('to_state') in {'study','break'}:facts['session_state']=receipt['to_state']
        if status=='completed' and step['capability_name']=='system.inspect_learning':
            saved=json.loads(step['result_json']).get('result') or {}
            # Explicit D030 runtime-fact whitelist: index inventory metadata,
            # never children's progress/memory/answers, titles, source text or paths.
            inventory={}
            for key in ('ready_curriculum_resource_count','local_graph_corpus_count'):
                if type(saved.get(key)) is int and saved[key]>=0: inventory[key]=saved[key]
            facts['inventory']=inventory
            facts['inventory_scope']='local_index_metadata'
            facts['material_content_verified']=False
        from .realtime_native_facts import extend
        extend(c,row,step,facts)
        owner=c.execute('''SELECT d.status AS session_status,p.status AS child_status
            FROM daily_session d JOIN child_profile p ON p.child_id=d.child_id WHERE d.session_id=? AND d.child_id=?''',
            (row['session_id'],row['child_id'])).fetchone()
        consent=c.execute('SELECT cloud_audio_allowed,cloud_model_text_allowed FROM privacy_preference WHERE singleton_id=1').fetchone()
        eligible=bool(not retired and owner and owner['session_status']=='active' and owner['child_status']=='active' and consent
            and all(type(consent[k]) is int and consent[k]==1 for k in ('cloud_audio_allowed','cloud_model_text_allowed')))
        if not eligible: facts={'version':VERSION,'withheld':True,'reason':'current_cloud_permission_or_owner_required'}
        # Do not serialize result_json, response_text, question, answer, paths or
        # IDs. Further per-capability public fields require an explicit projector.
        proof={'version':VERSION,'turn':row['agent_turn_id'],'step':step['step_id'],'status':step['status'],
               'result_sha256':_hash(json.loads(step['result_json'])) if step['result_json'] else None,'facts':facts}
        projection=_hash(proof)
        c.execute('''INSERT INTO audit_event VALUES(?,?,?,?,?,?) ON CONFLICT(audit_id) DO NOTHING''',
            ('runtime-facts-'+row['agent_turn_id']+'-'+projection,FACTS,'runtime_execution_facts',row['child_id'],_json(proof),_now()))
        return {'version':VERSION,'action_id':row['client_action_id'],'agent_turn_id':row['agent_turn_id'],
                'proposal_status':'replayed' if replayed else 'admitted','execution':{'status':status,'location':step['execution_location']},
                'public_result':facts,'public_projection':{'kind':FACTS,'sha256':projection,'eligible':eligible,'verified_model_text':False}}

    def receipt(self, session_id, action_id):
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT * FROM agent_turn WHERE session_id=? AND client_action_id=?',(session_id,action_id)).fetchone()
            if not row or not self._packet(c,row): _fail('REALTIME_CAPABILITY_RECEIPT_NOT_FOUND')
            # A read never re-executes, and remains readable after permission is revoked.
            result=self._receipt(c,row,replayed=True);c.commit();return result

    def receipt_for_identity(self, session_id, identity):
        key='qwen-tool-'+_hash([session_id,_identity(identity)])
        return self.receipt(session_id,key)

    async def execute(self, session_id, action_id):
        # Kept for compatible router callers. Only fresh admit may return an
        # executable dispatch; neither a read nor semantic replay can recreate it.
        return self.receipt(session_id,action_id)

    def pending_completions(self,c,session_id,child_id):
        from .realtime_native_client import pending_completions
        return pending_completions(self,c,session_id,child_id)

    async def client(self,session_id,value):
        from .realtime_native_client import run
        return await run(self,session_id,value)

    def save_dialogue(self, session_id, value):
        identity=_identity(value,require_call=False)
        if set(value)-set(INPUT_IDENTITY)-{'version','text','status','terminal','input_source'} or value.get('status')!='unverified' or value.get('terminal') not in {'completed','interrupted','failed'}:
            _fail('REALTIME_NATIVE_DIALOGUE_INVALID')
        if value.get('input_source') not in (None,'persisted_native','household_local_transcript'):_fail('REALTIME_NATIVE_INPUT_SOURCE_INVALID')
        text=value.get('text')
        if type(text) is not str or not text.strip() or len(text)>12000: _fail('REALTIME_NATIVE_DIALOGUE_INVALID')
        # A native chat response has no function call; its host response identity is enough.
        action_id='qwen-dialogue-'+_hash([session_id,identity])
        fingerprint=_hash(value)
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT * FROM agent_turn WHERE client_action_id=?',(action_id,)).fetchone()
            if row:
                if json.loads(row['action_json']).get('dialogue_fingerprint')!=fingerprint: _fail('REALTIME_NATIVE_DIALOGUE_REPLAY_CONFLICT')
                return {'version':VERSION,'status':'saved','verified':False,'agent_turn_id':row['agent_turn_id'],'replayed':True}
            session,voice,_=self._scope(c,session_id,value,require_permission=False)
            self._restored_chain(c,session,voice,value)
            action={'type':'none','source':'qwen_native_dialogue_unverified','local_only':True,'cloud_transmission_allowed':False,
                'mastery_update_allowed':False,'independent_evidence':False,'dialogue_fingerprint':fingerprint,
                'voice_input_id':voice['voice_input_id'],'native_identity':identity,'terminal':value['terminal'],'input_source':value.get('input_source')}
            turn_id=str(uuid4());now=_now()
            c.execute('''INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,action_json,
                orchestrator_version,tool_status,completed_at,created_at,client_action_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                (turn_id,session['child_id'],session_id,voice['transcript'],'qwen_native_dialogue_unverified',text,_json(action),PROTOCOL,'completed',now,now,action_id))
            c.commit();return {'version':VERSION,'status':'saved','verified':False,'agent_turn_id':turn_id,'replayed':False}
