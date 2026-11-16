"""Verify historical breaks without treating timer controls as learning data."""
from datetime import datetime
import json

from .scheduled_writing_goal import require,validate_origin


def validate_break_control(c,row,prompt,objective_id,*,control_prompt=None):
    from .repository import PlanInputError
    try:
        _validate(c,row,prompt,objective_id,control_prompt or prompt)
    except (ValueError,KeyError,TypeError,AttributeError,PlanInputError):
        require(False,'BREAK_CONTROL_CHANGED')


def _validate(c,row,prompt,objective_id,control_prompt):
    from .agent_runtime import AgentRuntimeStore
    from .voice_control_intent import VoiceControlIntent
    check=lambda value:require(value,'BREAK_CONTROL_CHANGED')
    origin=c.execute('''SELECT * FROM agent_plan WHERE child_id=? AND session_id=? AND objective_id=?
        AND json_extract(context_manifest_json,'$.scheduled_writing_goal.prompt_id')=? ORDER BY rowid LIMIT 1''',
        (prompt['child_id'],prompt['origin_session_id'],objective_id,prompt['prompt_id'])).fetchone()
    check(origin)
    validate_origin(c,origin,prompt)
    marker=json.loads(origin['context_manifest_json'])['scheduled_writing_goal']
    check(all(control_prompt[k]==prompt[k] for k in ('child_id','origin_session_id','diagnostic_item_id','item_version')))
    actual=c.execute('''SELECT t.*,s.status AS step_status,s.idempotency_key,s.error_code,s.result_json,
        p.status AS plan_status FROM agent_turn t JOIN agent_plan p USING(agent_turn_id)
        JOIN agent_step s USING(plan_id) WHERE s.step_id=? AND p.child_id=t.child_id AND p.session_id=t.session_id''',
        (row['step_id'],)).fetchone()
    check(actual and actual['tool_status']==actual['step_status']=='completed' and not actual['error_code']
        and actual['plan_status'] in ('completed','waiting_child')
        and actual['child_id']==prompt['child_id'] and actual['session_id']==prompt['origin_session_id'])
    action=json.loads(actual['action_json'])
    check(set(action)=={'type','source','local_only','cloud_transmission_allowed','voice_input_id','voice_input_binding','agent_runtime'}
        and action['source']=='content_free_voice_control' and action['local_only'] is True
        and action['cloud_transmission_allowed'] is False
        and json.loads(row['arguments_json'])=={'local_only':True,'cloud_transmission_allowed':False})
    voice=AgentRuntimeStore.resolve_captured_voice(c,session_id=actual['session_id'],child_id=actual['child_id'],
        voice_input_id=action['voice_input_id'],transcript=actual['transcript'])
    check(AgentRuntimeStore.voice_capture_snapshot(voice)==action['voice_input_binding']
        and voice['prompt_id']==control_prompt['prompt_id'] and voice['item_version']==control_prompt['item_version'])
    gate=VoiceControlIntent.inspect_execution(c,row['agent_turn_id'])
    check(gate and gate['control']==row['capability_name'] and gate['control'] in ('break.begin','break.resume'))
    scope=gate['targets']['break']
    key=actual['idempotency_key']
    receipt=c.execute('SELECT * FROM break_control_receipt WHERE client_action_id=?',(key,)).fetchone()
    check(receipt and receipt['child_id']==prompt['child_id'] and receipt['session_id']==prompt['origin_session_id']
        and json.loads(receipt['binding_json'])==scope and scope['resume_item_id']==marker['plan_item_id']
        and scope['planned_break_item'] is None)
    operation=gate['control'].split('.')[1]
    proof=json.loads(receipt['result_json'])
    check(receipt['operation']==operation and proof==gate['recorded_receipt']
        and proof['child_id']==receipt['child_id'] and proof['operation']==operation
        and proof['recorded_at']==receipt['created_at']
        and json.loads(actual['result_json'])['result']['break_receipt']==proof)
    transition=c.execute('SELECT * FROM session_transition WHERE transition_id=? AND session_id=? AND client_action_id=?',
        (receipt['transition_id'],receipt['session_id'],key)).fetchone()
    expected=('study','break') if operation=='begin' else ('break','study')
    check(transition and (transition['from_state'],transition['to_state'])==expected
        and all(proof[k]==transition[k] for k in ('transition_id','session_id','from_state','to_state')))
    event=c.execute('SELECT * FROM plan_item_event WHERE client_action_id=? AND plan_item_id=? AND event_type=?',
        (key+(':pause' if operation=='begin' else ':resume'),marker['plan_item_id'],
            'paused' if operation=='begin' else 'resumed')).fetchone()
    check(event and datetime.fromisoformat(control_prompt['created_at'])<=datetime.fromisoformat(actual['created_at'])
        <=datetime.fromisoformat(event['occurred_at'])<=datetime.fromisoformat(receipt['created_at']))
    times=[datetime.fromisoformat(value) for value in
        (actual['created_at'],event['occurred_at'],transition['occurred_at'],receipt['created_at'])]
    check(times[0]<=times[1]<=times[2]<=times[3] if operation=='begin'
        else times[0]<=times[2]<=times[1]<=times[3])
    if operation=='resume':
        previous=c.execute('SELECT * FROM break_control_receipt WHERE client_action_id=? AND operation=\'begin\' AND child_id=? AND session_id=?',
            (scope['origin_action_id'],receipt['child_id'],receipt['session_id'])).fetchone()
        check(previous)
        original=json.loads(previous['binding_json'])
        check(original['plan']==scope['plan'] and original['resume_item_id']==marker['plan_item_id']
            and datetime.fromisoformat(previous['created_at'])<datetime.fromisoformat(actual['created_at']))
