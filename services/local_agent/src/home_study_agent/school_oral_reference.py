"""Source-bound local oral content checks, never a replacement assessment."""
from datetime import datetime, timezone
import json
from uuid import uuid4

from .local_supply import digest

KEY = 'school_oral_reference'


def require(value, reason):
    if not value:
        from .agent_runtime import InvalidAgentPlanError
        raise InvalidAgentPlanError('ANSWER_ORAL_REFERENCE_' + reason)


def source(c, binding, *, live):
    from .oral_teaching import project_oral_teaching
    from .school_spoken_score import original_transcript
    from .formative_evaluation_projection import _objective
    from .voice_control_intent import VoiceControlIntent
    child, session = binding['child_id'], binding['session_id']
    prompt = c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=? AND child_id=? AND origin_session_id=?',
        (binding['prompt_id'],child,session)).fetchone()
    require(prompt and prompt['item_version']==binding['item_version'], 'SCOPE_CHANGED')
    view = project_oral_teaching(c,child,session,prompt['prompt_id'],prompt['item_version'])
    require(view['status']=='content_self_check'
        and view['response_authority']=='original_capture_bound_transcript_not_acoustic_review', 'SOURCE_CHANGED')
    attempt=c.execute('SELECT * FROM diagnostic_attempt WHERE attempt_id=?',(prompt['attempt_id'],)).fetchone()
    origin=original_transcript(c,prompt,attempt)
    # Local reasoning history needs source/capture proof, not permission to
    # upload its ancestors. Cloud projection retains its strict default reader.
    def original_reader(connection, parent, previous, _item):
        return original_transcript(connection,parent,previous)['origin']
    context=_objective(c,child,session,binding['objective_id'],prompt,attempt,origin['origin'],
        original_reader=original_reader)
    objective=c.execute('SELECT phase,success_criteria_json FROM agent_objective WHERE objective_id=?',
        (binding['objective_id'],)).fetchone()
    require(objective and objective['phase']==binding['phase']
        and json.loads(objective['success_criteria_json'])==binding['success_criteria'], 'GOAL_CHANGED')
    if live:
        turn=c.execute('SELECT * FROM agent_turn WHERE agent_turn_id=?',(prompt['authorized_turn_id'],)).fetchone()
        require(turn and VoiceControlIntent.scope_valid(c,session,child,turn['client_action_id'],require_cloud=False), 'SUPERSEDED')
        action=json.loads(turn['action_json'])
        latest=c.execute('SELECT voice_input_id FROM voice_input WHERE child_id=? AND session_id=? ORDER BY rowid DESC LIMIT 1',
            (child,session)).fetchone()
        require(latest and latest[0]==action.get('voice_input_id'), 'NEWER_SPEECH')
        require(not c.execute("SELECT 1 FROM deletion_job WHERE child_id=? AND scope IN ('learning','knowledge','media','tutor','all_history') AND status!='completed'",
            (child,)).fetchone(), 'DELETION_PENDING')
    response=('你的口头回答已经保存。这次先在本机核对内容，暂不评分，也不判断已经掌握。'
        '先对照原题的这个要求：'+view['task_points'][0]['task_point'][:260]+
        '。你可以再说一次，补充理由，或让我解释这道题。')
    return dict(view=view,response=response,source_version=digest([view['feedback_version'],binding,context]))


def prepare(runtime,c,session_id,prompt_id,item_version,binding,key):
    from .agent_runtime import AgentPlanDecision,ObjectiveDraft,PlannedStep,PLANNER_VERSION,CapabilityRegistry
    require(binding['session_id']==session_id and binding['prompt_id']==prompt_id
        and binding['item_version']==item_version, 'SCOPE_CHANGED')
    current=source(c,binding,live=True)
    saved=c.execute('SELECT agent_turn_id FROM agent_turn WHERE child_id=? AND session_id=? AND client_action_id=?',
        (binding['child_id'],session_id,key)).fetchone()
    if saved:
        validate(c,saved[0])
        return saved[0]
    ref=dict(binding=binding,source_version=current['source_version'])
    action=dict(type='none',source='oral_local_reference',local_only=True,cloud_transmission_allowed=False,
        mastery_update_allowed=False,**{KEY:ref})
    tid=str(uuid4())
    c.execute('''INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,
        action_json,orchestrator_version,created_at,client_action_id) VALUES(?,?,?,'','oral_local_reference',?,?,?,?,?)''',
        (tid,binding['child_id'],session_id,current['response'],json.dumps(action,ensure_ascii=False),PLANNER_VERSION,
         datetime.now(timezone.utc).isoformat(),key))
    cap=CapabilityRegistry().require('conversation.respond','study')
    decision=AgentPlanDecision(ObjectiveDraft('none',None,None,(),'check'),
        PlannedStep(cap,{'response':current['response']},'Local oral content check, not assessment',()),current['response'],False,0)
    metadata=runtime.persist_plan(c,child_id=binding['child_id'],session_id=session_id,agent_turn_id=tid,
        client_action_id=key,decision=decision,context_manifest={KEY:ref, 'local_only':True,'cloud_transmission_allowed':False})
    c.execute("UPDATE agent_turn SET action_json=json_set(action_json,'$.agent_runtime',json(?)) WHERE agent_turn_id=?",(json.dumps(metadata),tid))
    return tid


def validate(c,turn_id,*,final_response=None,result=None,completion=False):
    row=c.execute('''SELECT t.*,p.context_manifest_json,p.status AS plan_status,
        s.capability_name,s.execution_location,s.status AS step_status,s.arguments_json,s.result_json
        FROM agent_turn t JOIN agent_plan p USING(agent_turn_id) JOIN agent_step s USING(plan_id)
        WHERE t.agent_turn_id=? AND s.step_index=0 AND p.child_id=t.child_id AND p.session_id=t.session_id''',(turn_id,)).fetchone()
    if not row:
        return None
    action,manifest=json.loads(row['action_json']),json.loads(row['context_manifest_json'])
    # This validator is invoked for every completed agent turn. Other valid
    # turn contracts may use an action array, so only inspect mappings that
    # could actually carry the oral-reference binding.
    if not isinstance(action, dict) or not isinstance(manifest, dict):
        return None
    if KEY not in action and KEY not in manifest and row['intent']!='oral_local_reference' and action.get('source')!='oral_local_reference':
        return None
    try:
        ref=manifest[KEY]
        require(action.get(KEY)==ref and action.get('source')=='oral_local_reference'
            and row['intent']=='oral_local_reference' and row['capability_name']=='conversation.respond'
            and row['execution_location']=='server' and action.get('local_only') is True
            and action.get('cloud_transmission_allowed') is False and action.get('mastery_update_allowed') is False
            and (ref['binding']['child_id'],ref['binding']['session_id'])==(row['child_id'],row['session_id'])
            and not {'cancelled'} & {row['tool_status'],row['plan_status'],row['step_status']}, 'BINDING_CHANGED')
        current=source(c,ref['binding'],live=row['tool_status']!='completed')
        require(current['source_version']==ref['source_version']
            and json.loads(row['arguments_json'])=={'response':current['response']}
            and row['response_text']==current['response'], 'CONTENT_CHANGED')
        value=dict(status='content_self_check',oral_feedback=current['view'],feedback_delivery_allowed=True,
            feedback_status='ready',objective_update_applied=False,local_only=True,cloud_transmission_allowed=False)
        if final_response is not None:
            require(final_response==current['response'], 'RESPONSE_CHANGED')
        if completion:
            saved=json.loads(row['result_json']) if row['result_json'] else None
            require(row['step_status']=='completed' and saved and saved.get('response_text')==current['response']
                and saved.get('result')==value and saved.get('capability')=='conversation.respond'
                and result==saved, 'SERVER_RECEIPT_REQUIRED')
        return dict(response_text=current['response'],result=value)
    except (ValueError,KeyError,TypeError):
        require(False,'SOURCE_OR_RECEIPT_CHANGED')
