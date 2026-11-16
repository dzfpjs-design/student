"""A verified premature-completion refusal is not teaching or a new answer."""
from datetime import datetime
from hashlib import sha256
import json

from .oral_daily_check import SOURCE,RECEIPT
from .oral_supply_execution import require,resolve_execution

CONTROL_FLAGS = dict(local_only=True, cloud_transmission_allowed=False)


def validate_pending_completion(c,row,prompt,objective_id):
    from .agent_runtime import AgentRuntimeStore
    actual=c.execute('''SELECT t.*,p.status AS plan_status,p.objective_id,s.status AS step_status,
        s.error_code,s.idempotency_key,s.capability_name FROM agent_turn t JOIN agent_plan p
        ON p.agent_turn_id=t.agent_turn_id AND p.child_id=t.child_id AND p.session_id=t.session_id
        JOIN agent_step s USING(plan_id) WHERE p.plan_id=? AND s.step_id=? AND t.agent_turn_id=?''',
        (row['plan_id'],row['step_id'],row['agent_turn_id'])).fetchone()
    require(actual and actual['capability_name']=='task.complete'
        and actual['plan_status']==actual['step_status']==actual['tool_status']=='failed'
        and actual['error_code']=='TaskStartBindingError'
        and actual['tool_error_code']=='ORAL_DAILY_CHECK_FEEDBACK_REQUIRED'
        and actual['objective_id']==objective_id and actual['child_id']==prompt['child_id']
        and actual['session_id']==prompt['origin_session_id'])
    action=json.loads(actual['action_json'])
    require(set(action)=={'type','source','local_only','cloud_transmission_allowed','voice_input_id','voice_input_binding','agent_runtime'}
        and action['type']=='complete_active_task' and action['source']=='content_free_voice_control'
        and all(action.get(k) is v for k,v in CONTROL_FLAGS.items())
        and json.loads(row['arguments_json'])==CONTROL_FLAGS)
    voice=AgentRuntimeStore.resolve_captured_voice(c,session_id=actual['session_id'],child_id=actual['child_id'],
        voice_input_id=action['voice_input_id'],transcript=actual['transcript'])
    require(AgentRuntimeStore.voice_capture_snapshot(voice)==action['voice_input_binding']
        and voice['prompt_id']==prompt['prompt_id'] and voice['item_version']==prompt['item_version'])
    frozen=json.loads(row['context_manifest_json'])['voice_control_binding']
    request=c.execute('SELECT * FROM voice_control_intent_request WHERE request_key=?', (frozen['request_key'],)).fetchone()
    require(request and request['status']=='completed' and request['child_id']==actual['child_id']
        and request['session_id']==actual['session_id'] and request['fingerprint']==sha256(
            json.dumps([actual['child_id'],actual['session_id'],actual['transcript']],ensure_ascii=False).encode()).hexdigest())
    decision=json.loads(request['decision_json'])
    require(decision.get('status')=='control' and decision.get('control')==frozen.get('control')=='task.complete'
        and json.loads(request['binding_json'])==frozen['targets'])
    task=frozen['targets']['task']
    origins=c.execute('''SELECT * FROM agent_plan WHERE child_id=? AND session_id=? AND objective_id=?
        AND (json_extract(context_manifest_json,'$.oral_daily_check_receipt.prompt_id')=?
          OR json_extract(context_manifest_json,'$.oral_delivery_continuation_receipt.resolution.prompt_id')=?)''',
        (prompt['child_id'],prompt['origin_session_id'],objective_id,prompt['prompt_id'],prompt['prompt_id'])).fetchall()
    require(len(origins)==1)
    manifest=json.loads(origins[0]['context_manifest_json'])
    receipt=manifest.get(RECEIPT) or manifest.get('oral_delivery_continuation_receipt')
    continuation=RECEIPT not in manifest
    root=c.execute('SELECT payload_json FROM agent_commitment WHERE commitment_id=? AND child_id=?',
        (receipt['root_commitment_id'],prompt['child_id'])).fetchone()
    require(root and json.loads(root[0]).get('origin')=='daily_learning_closure')
    delivery=receipt['resolution'] if continuation else receipt
    require(delivery['plan_item_id']==task['plan_item_id'] and delivery['objective_id']==objective_id)
    current=c.execute('''SELECT pi.source_type,pi.status,pi.plan_id,dp.version FROM plan_item pi
        JOIN daily_plan dp USING(plan_id) WHERE pi.plan_item_id=? AND dp.child_id=? AND dp.session_id=?''',
        (task['plan_item_id'],prompt['child_id'],prompt['origin_session_id'])).fetchone()
    require(current and current['source_type']==('oral_delivered_continuation' if continuation else SOURCE)
        and current['status'] in ('active','completed')
        and current['plan_id']==task['plan_id'] and current['version']==task['version'])
    resolve_execution(c,prompt['origin_session_id'],task['plan_item_id'])
    require(not c.execute('SELECT 1 FROM plan_item_event WHERE client_action_id=?', (actual['idempotency_key'],)).fetchone())
    attempt=c.execute('SELECT created_at FROM diagnostic_attempt WHERE attempt_id=? AND child_id=? AND session_id=?',
        (prompt['attempt_id'],prompt['child_id'],prompt['origin_session_id'])).fetchone()
    require(attempt and datetime.fromisoformat(prompt['created_at'])<=datetime.fromisoformat(attempt[0])
        <=datetime.fromisoformat(actual['created_at'])<=datetime.fromisoformat(actual['completed_at']))
