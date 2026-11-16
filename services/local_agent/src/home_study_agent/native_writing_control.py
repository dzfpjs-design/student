"""Local-only targets for semantic native manuscript receipt/submit controls."""
import hashlib
import json

from .native_writing import current_scope, original_receipt


def source(c, session_id, child_id):
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='diagnostic_prompt'").fetchone():
        return None
    row=c.execute('''SELECT * FROM diagnostic_prompt WHERE origin_session_id=? AND child_id=?
        ORDER BY rowid DESC LIMIT 1''',(session_id,child_id)).fetchone()
    if row is None:
        return None
    scope=dict(child_id=child_id,session_id=session_id,prompt_id=row['prompt_id'],
               item_version=row['item_version'],diagnostic_item_id=row['diagnostic_item_id'])
    try:
        item = json.loads(row['item_json'])
        if not isinstance(item, dict) or item.get('answer_type') not in {'writing','open_response'}:
            return None
        prompt=current_scope(c,session_id,scope)
        originals=c.execute('''SELECT * FROM capture_artifact WHERE session_id=? AND child_id=?
            AND artifact_type='writing_original' AND json_extract(details_json,'$.prompt_id')=?''',
            (session_id,child_id,row['prompt_id'])).fetchall()
        if originals:
            if len(originals)!=1 or json.loads(originals[0]['details_json']).get('origin')!={'kind':'native_editor'}:
                return None
            text=originals[0]['extracted_text']
            original=original_receipt(c,prompt,originals[0]['artifact_id'],text)
        else:
            draft=c.execute("SELECT payload_json FROM session_draft WHERE session_id=? AND section='study'",(session_id,)).fetchone()
            value=json.loads(draft[0]).get('native_writing_draft') if draft else None
            if not isinstance(value,dict) or value.get('input_kind')!='typed' or value.get('scope')!=scope:
                return None
            text=value.get('text')
        if not isinstance(text,str) or not text.strip() or len(text.strip().encode('utf-16-le'))//2>4000:
            return None
        target={**scope,'text_sha256':hashlib.sha256(text.strip().encode()).hexdigest()}
        if originals and prompt['answer_action_id'] and prompt['writing_original_artifact_id']==original['artifact_id']:
            from .diagnostic_prompt import DiagnosticPromptService
            from .local_supply import digest
            if prompt['status']=='answer_saved':
                payload=json.loads(prompt['answer_json'])
            elif prompt['status']=='answered':
                attempt=DiagnosticPromptService._receipt(c,prompt)
                if attempt is None:return None
                payload={'answer':attempt['submitted_answer'],'hint_level':attempt['hint_level']}
            else:return None
            if payload.get('answer')!=text:return None
            target['saved_answer']={'answer_action_id':prompt['answer_action_id'],
                'original_artifact_id':original['artifact_id'],'original_revision':original['original_revision'],
                'authorized_turn_id':prompt['authorized_turn_id'],'payload_sha256':digest(payload)}
        return target,text
    except (ValueError,KeyError,TypeError):
        return None


def snapshot(c, session_id, child_id):
    value=source(c,session_id,child_id)
    return value[0] if value else None


def validate(c, session_id, child_id, target):
    from .agent_runtime import InvalidAgentPlanError
    actual=snapshot(c,session_id,child_id)
    # A pre-save capture control stays bound to its original text after saving.
    # A newly interpreted saved-answer control additionally freezes the saved key.
    if actual and target and 'saved_answer' not in target:
        actual={k:v for k,v in actual.items() if k!='saved_answer'}
    if not target or actual!=target:
        raise InvalidAgentPlanError('NATIVE_WRITING_CONTROL_SOURCE_CHANGED')


def validate_retained_turn(c, turn_id, prompt, answer):
    """The retained-input bridge may not borrow generic ASR answer authority."""
    if turn_id is None:
        return False
    row=c.execute('SELECT action_json FROM agent_turn WHERE agent_turn_id=?',(turn_id,)).fetchone()
    if row is None or json.loads(row['action_json']).get('type')!='submit_retained_native_writing':
        return False
    from .agent_runtime import AgentRuntimeStore, InvalidAgentPlanError
    receipt=AgentRuntimeStore.inspect_voice_control_execution(c,turn_id)
    target=(receipt or {}).get('targets',{}).get('native_writing')
    scope=dict(child_id=prompt['child_id'],session_id=prompt['origin_session_id'],prompt_id=prompt['prompt_id'],
               item_version=prompt['item_version'],diagnostic_item_id=prompt['diagnostic_item_id'])
    if (not receipt or receipt['control']!='native_writing.submit_retained'
            or {k:v for k,v in (target or {}).items() if k!='saved_answer'}!={**scope,'text_sha256':hashlib.sha256(answer.encode()).hexdigest()}):
        raise InvalidAgentPlanError('NATIVE_WRITING_CONTROL_ANSWER_CHANGED')
    return True


def validate_saved_retained_turn(c, turn_id, *, child_id, session_id, binding, answer):
    from .native_writing_resume import validate_active_resume
    if validate_active_resume(c,turn_id,child_id=child_id,session_id=session_id,binding=binding,answer=answer):
        return True
    row=c.execute('SELECT action_json FROM agent_turn WHERE agent_turn_id=?',(turn_id,)).fetchone()
    if row is None or json.loads(row['action_json']).get('type')!='submit_retained_native_writing':
        return False
    from .agent_runtime import InvalidAgentPlanError
    prompt=c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=?',((binding or {}).get('prompt_id'),)).fetchone()
    if (prompt is None or prompt['child_id']!=child_id or prompt['origin_session_id']!=session_id
            or binding!={k:prompt[k] for k in ('prompt_id','item_version')}
            or prompt['authorized_turn_id']!=turn_id or not prompt['writing_original_artifact_id']):
        raise InvalidAgentPlanError('NATIVE_WRITING_SAVED_ORIGINAL_REQUIRED')
    original_receipt(c,prompt,prompt['writing_original_artifact_id'],answer)
    from .realtime_native_special_sources import semantic_version
    native = semantic_version(c, turn_id, capability='native_writing.submit_retained', completion=True)
    if native is not None:
        # Historical recognition of an already stored original is not a new
        # submit grant and must survive consumption of the client claim.
        action=json.loads(row['action_json'])
        target=action.get('native_writing_target') or {}
        scope=dict(child_id=child_id,session_id=session_id,prompt_id=prompt['prompt_id'],
            item_version=prompt['item_version'],diagnostic_item_id=prompt['diagnostic_item_id'])
        if {k:v for k,v in target.items() if k!='saved_answer'}!={**scope,'text_sha256':hashlib.sha256(answer.encode()).hexdigest()}:
            raise InvalidAgentPlanError('NATIVE_WRITING_CONTROL_ANSWER_CHANGED')
        return True
    return validate_retained_turn(c,turn_id,prompt,answer)
