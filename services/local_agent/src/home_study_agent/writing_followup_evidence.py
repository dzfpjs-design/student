"""Historical admission proof for an answered formal writing followup.

No new authorization, selection, score or cloud permission is created here.
"""
import hashlib
import json
from datetime import datetime

from .diagnostic_prompt import DiagnosticPromptService, _version
from .learning_obligations import question_identity, withdrawn_evidence_ids
from .local_supply import digest
from .writing_response_origin import _prompt, original_written_response, require
from .writing_teaching import FOLLOWUP_ACTION_FIELDS, WritingTeachingService


def _object(value, reason):
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        require(False, reason)
    require(isinstance(parsed, dict), reason)
    return parsed


def _time(value):
    try:
        at = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError('WRITING_FOLLOWUP_TIME_INVALID') from None
    require(at.tzinfo is not None, 'FOLLOWUP_TIME_INVALID')
    return at


def _independent_exposure(c, child_id, item, attempt):
    from .learning_obligations import _question_catalog, _tutor_exposure_index
    catalog, identities = _question_catalog(c)
    identity, cutoff = question_identity(item, connection=c), _time(attempt['created_at'])
    previous = c.execute('''SELECT a.attempt_id,a.diagnostic_item_id,a.created_at FROM diagnostic_attempt a
        JOIN learning_evidence e ON e.evidence_id=a.evidence_id AND e.child_id=a.child_id AND e.session_id=a.session_id
        WHERE a.child_id=? AND a.attempt_id!=?
        UNION ALL SELECT a.attempt_id,a.diagnostic_item_id,a.created_at FROM mistake_attempt a
        JOIN mistake_case m USING(mistake_id) WHERE m.child_id=? AND a.child_id=m.child_id''',
        (child_id,attempt['attempt_id'],child_id)).fetchall()
    require(not any(identities.get(row['diagnostic_item_id'])==identity and _time(row['created_at'])<=cutoff
        for row in previous), 'FOLLOWUP_PRIOR_ANSWER_EXPOSURE')
    exact, bound = _tutor_exposure_index(c,child_id,catalog,identities)
    hints = bound.get(identity, [])
    if not item.get('stimulus_text') and not item.get('stimulus_asset'):
        hints = [*hints,*exact.get(' '.join(item['prompt'].split()), [])]
    require(not any(at<=cutoff for _,at,_ in hints), 'FOLLOWUP_PRIOR_TUTOR_EXPOSURE')


def validate_formal_followup_evaluation(c, *, child_id, session_id, prompt_id, item_version, attempt_id):
    """Return local proof or raise WRITING_*; use the caller's read/write transaction.

    Unlike live admission, later speech and this exact attempt are not new prior
    exposure. All original source, feedback, voice and cancellation facts remain live.
    """
    target,item = _prompt(c,session_id,prompt_id,item_version,live=False)
    require(target['child_id']==child_id and target['status']=='answered' and target['attempt_id']==attempt_id,
            'FOLLOWUP_ANSWER_SCOPE_CHANGED')
    attempt = c.execute('SELECT * FROM diagnostic_attempt WHERE attempt_id=? AND child_id=? AND session_id=?',
        (attempt_id,child_id,session_id)).fetchone()
    require(attempt and attempt['evidence_id'] not in withdrawn_evidence_ids(c,child_id), 'FOLLOWUP_ANSWER_WITHDRAWN')
    original = original_written_response(c,target,attempt)
    link = c.execute('SELECT * FROM writing_followup WHERE prompt_id=? AND child_id=? AND session_id=?',
        (prompt_id,child_id,session_id)).fetchone()
    require(link and link['kind'] in {'revision','independent_check'}, 'FOLLOWUP_LINEAGE_REQUIRED')
    request = _object(target['request_json'], 'FOLLOWUP_ORIGINAL_COMMAND_REQUIRED')
    require('writing_reference_revision' not in request and isinstance(request.get('writing_authorized_turn_id'),str),
            'FOLLOWUP_ORIGINAL_COMMAND_REQUIRED')
    source = c.execute('''SELECT t.*,p.plan_id,p.objective_id,p.context_manifest_json,p.plan_json,p.status AS plan_status,
        s.step_id,s.status AS step_status,s.capability_name,s.arguments_json,s.idempotency_key,s.error_code
        FROM agent_turn t JOIN agent_plan p ON p.agent_turn_id=t.agent_turn_id
        AND p.child_id=t.child_id AND p.session_id=t.session_id JOIN agent_step s USING(plan_id)
        WHERE t.agent_turn_id=? AND t.child_id=? AND t.session_id=? AND s.step_index=0''',
        (request['writing_authorized_turn_id'],child_id,session_id)).fetchall()
    require(len(source)==1, 'FOLLOWUP_ORIGINAL_COMMAND_REQUIRED')
    source = source[0]
    require(source['plan_status'] in {'running','completed','waiting_child'}
        and source['step_status'] in {'running','completed','waiting_child'}
        and source['tool_status'] in {'pending','completed'}
        and not source['error_code'] and not source['tool_error_code'], 'FOLLOWUP_COMMAND_NOT_CURRENT')
    from .agent_runtime import AgentRuntimeStore
    AgentRuntimeStore.assert_turn_source_not_deleted(c,source['agent_turn_id'])
    action,manifest,plan = (_object(source[key], 'FOLLOWUP_COMMAND_CHANGED')
        for key in ('action_json','context_manifest_json','plan_json'))
    daily = None
    if 'daily_writing_binding' in manifest:
        from .writing_daily_followup import inspect_execution
        daily = inspect_execution(c,source['agent_turn_id'],prompt=target)
    packet = action.get('writing_followup_binding')
    require(isinstance(packet,dict) and set(packet)=={'action','binding_version'}
        and isinstance(packet['action'],dict) and set(packet['action'])==
            (FOLLOWUP_ACTION_FIELDS | {'parent_session_id'} if daily else FOLLOWUP_ACTION_FIELDS),
        'FOLLOWUP_BINDING_INVALID')
    frozen = packet['action']
    require(digest(frozen)==packet['binding_version'] and 'writing_revision_binding' not in action
        and action.get('request_quote')==source['transcript'] and source['intent']!='runtime_continuation',
        'FOLLOWUP_COMMAND_CHANGED')
    control, action_type = (('writing.revise_original','revise_writing_original') if link['kind']=='revision'
        else ('writing.independent_check','check_writing_independently'))
    if daily:
        require(link['kind']=='revision' and daily['binding']==packet,'FOLLOWUP_KIND_CHANGED')
        control,action_type='task.start','start_next_task'
    require(source['capability_name']==control and action.get('type')==action_type, 'FOLLOWUP_KIND_CHANGED')
    expected_request = {key:frozen[key] for key in ('parent_prompt_id','parent_item_version','kind')}
    expected_request['binding_version'] = packet['binding_version']
    if daily:
        expected_request['parent_session_id']=frozen['parent_session_id']
    require(request.get('writing_followup')==expected_request
        and request.get('preferred_item_id')==item['diagnostic_item_id']
        and source['idempotency_key']==target['prepare_action_id']==link['client_action_id']
        and json.loads(link['binding_json'])==dict(expected_request,session_id=session_id,prompt_id=prompt_id,item_version=item_version),
        'FOLLOWUP_PROMPT_BINDING_CHANGED')
    require(daily or manifest.get('writing_voice_binding')=={'request_quote':source['transcript'],'followup':packet},
            'FOLLOWUP_COMMAND_CHANGED')
    parent_session=frozen['parent_session_id'] if daily else session_id
    parent,parent_item = _prompt(c,parent_session,link['parent_prompt_id'],frozen['parent_item_version'],live=False)
    parent_attempt = c.execute('SELECT * FROM diagnostic_attempt WHERE attempt_id=?', (parent['attempt_id'],)).fetchone()
    require(parent_attempt and parent['child_id']==child_id and parent_attempt['child_id']==child_id
        and parent_attempt['session_id']==parent_session
        and parent_attempt['evidence_id'] not in withdrawn_evidence_ids(c,child_id),
            'FOLLOWUP_PARENT_WITHDRAWN')
    parent_original = original_written_response(c,parent,parent_attempt)
    require(original['artifact_id']!=parent_original['artifact_id'] and prompt_id!=parent['prompt_id'],
            'FOLLOWUP_NEW_ORIGINAL_REQUIRED')
    view = WritingTeachingService(None).read(parent_session,prompt_id=parent['prompt_id'],item_version=parent['item_version'],connection=c)
    require(view['status']=='feedback_ready' and view['assessment_id']==link['assessment_id'], 'FOLLOWUP_FEEDBACK_CHANGED')
    expected = dict(kind=link['kind'],parent_prompt_id=parent['prompt_id'],parent_item_version=parent['item_version'],
        child_id=child_id,session_id=session_id,assessment_id=link['assessment_id'],feedback_version=digest(view['feedback']),
        diagnostic_item_id=item['diagnostic_item_id'],item_version=_version(DiagnosticPromptService._item(c,child_id,item['diagnostic_item_id'])),
        source_version=digest(item),knowledge_node_id=item['knowledge_node_id'],
        required_hint_level=1 if link['kind']=='revision' else 0,mastery_claim_allowed=False)
    if daily:
        expected['parent_session_id']=parent_session
    require(digest(expected)==digest(frozen) and item['knowledge_node_id']==parent_item['knowledge_node_id'],
            'FOLLOWUP_SOURCE_OR_FEEDBACK_CHANGED')
    same = item['diagnostic_item_id']==parent_item['diagnostic_item_id']
    require(same==(link['kind']=='revision'), 'FOLLOWUP_QUESTION_CHANGED')
    evidence = c.execute('SELECT hint_level FROM learning_evidence WHERE evidence_id=? AND child_id=?',
        (attempt['evidence_id'],child_id)).fetchone()
    require(evidence and (evidence[0]>=1 if same else evidence[0]==0), 'FOLLOWUP_ASSISTANCE_CHANGED')
    if not same:
        require(question_identity(item, connection=c)!=question_identity(parent_item, connection=c), 'FOLLOWUP_NEW_QUESTION_REQUIRED')
        _independent_exposure(c,child_id,item,attempt)
    arrival = c.execute('''SELECT * FROM learning_input_arrival WHERE arrival_id=? AND child_id=? AND session_id=?
        AND client_action_id=?''', (manifest.get('learning_input_arrival_id'),child_id,session_id,source['client_action_id'])).fetchone()
    require(arrival and arrival['utterance']==source['transcript'], 'FOLLOWUP_ARRIVAL_CHANGED')
    snapshots = [entry['record'] for entry in json.loads(arrival['snapshot_json'])
        if entry.get('kind')==('daily_writing_arrival' if daily else 'writing_followup_arrival')]
    require(len(snapshots)==1 and isinstance(snapshots[0],dict), 'FOLLOWUP_ARRIVAL_CHANGED')
    snapshot = snapshots[0]
    selected = ({'status':'ready',**daily['binding']} if daily else
        (snapshot.get('packets') or {}).get(link['kind']) or {})
    require(selected.get('status')=='ready' and {key:selected.get(key) for key in packet}==packet,
            'FOLLOWUP_ARRIVAL_CHANGED')
    semantic = None
    if not daily:
        reference = action.get('writing_followup_semantic_receipt')
        require(isinstance(reference,dict) and set(reference)=={'request_key'}, 'FOLLOWUP_SEMANTIC_REQUIRED')
        semantic = c.execute('SELECT * FROM voice_control_intent_request WHERE request_key=? AND child_id=? AND session_id=?',
            (reference['request_key'],child_id,session_id)).fetchone()
        fingerprint = hashlib.sha256(json.dumps([child_id,session_id,source['transcript']],ensure_ascii=False).encode()).hexdigest()
        require(semantic and semantic['status']=='completed' and semantic['fingerprint']==fingerprint
            and json.loads(semantic['decision_json'])=={'status':'control','control':control}
            and json.loads(semantic['binding_json']).get('writing_followups',{}).get(link['kind'])==selected,
            'FOLLOWUP_SEMANTIC_CHANGED')
    if action.get('voice_input_id'):
        require(isinstance(action.get('voice_input_binding'),dict)
            and action['voice_input_binding'].get('voice_input_id')==action['voice_input_id']
            and c.execute("SELECT 1 FROM sqlite_master WHERE name='voice_capture_binding'").fetchone(),
            'FOLLOWUP_VOICE_CHANGED')
        voice = AgentRuntimeStore.resolve_captured_voice(c,session_id=session_id,child_id=child_id,
            voice_input_id=action['voice_input_id'],transcript=source['transcript'])
        require(AgentRuntimeStore.voice_capture_snapshot(voice)==action.get('voice_input_binding'), 'FOLLOWUP_VOICE_CHANGED')
    else:
        require(not action.get('voice_input_binding'), 'FOLLOWUP_VOICE_CHANGED')
    bound = plan.get('local_learning_binding') or {}
    require(all(bound.get(key)==value for key,value in dict(child_id=child_id,session_id=session_id,prompt_id=prompt_id,
        item_version=item_version,objective_id=source['objective_id'],assessment_id=link['assessment_id'],
        parent_prompt_id=parent['prompt_id'],required_hint_level=expected['required_hint_level']).items()),
        'FOLLOWUP_OBJECTIVE_BINDING_CHANGED')
    feedback = c.execute('''SELECT p.objective_id,p.agent_turn_id FROM rubric_assessment r
        JOIN agent_step s USING(step_id) JOIN agent_plan p USING(plan_id) WHERE r.assessment_id=? AND r.child_id=?''',
        (link['assessment_id'],child_id)).fetchone()
    require(feedback and (daily or snapshot.get('feedback_turn_id')==feedback['agent_turn_id'])
        and snapshot.get('source_objective_id' if daily else 'objective_id')==feedback['objective_id']
        and bound.get('source_objective_id')==feedback['objective_id'], 'FOLLOWUP_ORIGINAL_GOAL_CHANGED')
    require((daily or _time(semantic['created_at'])<=_time(semantic['completed_at'])<=_time(source['created_at']))
        and _time(arrival['created_at'])<=_time(source['created_at'])
        and _time(source['created_at'])
        <=_time(target['created_at'])<=_time(link['created_at'])<=_time(attempt['created_at']), 'FOLLOWUP_TIME_INVALID')
    return {'version':'daily_writing_followup_evidence_v1' if daily else 'formal_writing_followup_evidence_v1','request_turn_id':source['agent_turn_id'],
        'packet':packet,'lineage':dict(link),'source_version':digest(dict(source)),
        'semantic_version':digest(daily['record'] if daily else {key:semantic[key] for key in ('request_key','fingerprint','binding_json','decision_json','created_at','completed_at')}),
        'arrival_version':digest(dict(arrival)), 'original':original,'parent_original':parent_original,
        'local_only':True,'cloud_transmission_allowed':False}
