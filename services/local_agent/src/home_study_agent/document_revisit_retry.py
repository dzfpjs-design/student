"""A spoken retry permission is consumed with the next reservation, not before it."""
import json

from . import document_goal_handoff as goal
from .agent_runtime import AgentRuntimeStore

MESSAGE = '刚才的点评结果还没确认。重新生成可能再调用一次云端服务，但不会重做你的作答。要重新生成，还是先保留、稍后再说？'


def reservation(c,key):
    row=c.execute('''SELECT action_id,session_id,child_id,request_hash,status,attempts,last_error_code,tutor_turn_id
        FROM tutor_request_reservation WHERE action_id=?''',(key,)).fetchone()
    return dict(row) if row else None


def confirm(c,turn,work,action):
    offered=work.get('retry_proposal')
    goal.require(offered and offered['response_turn_id']==work.get('response_turn_id'),'REVISIT_RETRY_NOT_OFFERED')
    basis=offered['reservation']
    goal.require(reservation(c,basis['action_id'])==basis and basis['status']=='interrupted'
        and (basis['session_id'],basis['child_id'])==(turn['session_id'],turn['child_id']),'REVISIT_RETRY_ATTEMPT_CHANGED')
    prior=c.execute('''SELECT t.*,s.status AS step_status FROM agent_turn t JOIN agent_plan p USING(agent_turn_id)
        JOIN agent_step s USING(plan_id) WHERE t.agent_turn_id=? AND s.step_index=0
        AND s.capability_name='learning.review_revisit' ''',(offered['turn_id'],)).fetchone()
    goal.require(prior and prior['tool_status']==prior['step_status']=='completed' and prior['response_text']==MESSAGE
        and (prior['session_id'],prior['child_id'])==(turn['session_id'],turn['child_id']),'REVISIT_RETRY_READBACK_CHANGED')
    previous_action=json.loads(prior['action_json'])
    voice=AgentRuntimeStore.resolve_captured_voice(c,session_id=turn['session_id'],child_id=turn['child_id'],
        voice_input_id=previous_action.get('voice_input_id'),transcript=prior['transcript'])
    goal.require(AgentRuntimeStore.voice_capture_snapshot(voice)==previous_action.get('voice_input_binding'),
        'REVISIT_RETRY_VOICE_CHANGED')
    permission={'turn_id':turn['agent_turn_id'],'request_key':action['document_revisit_request'],
        'reservation':basis,'offer':offered,'response_turn_id':work['response_turn_id']}
    work.update(phase='answer_saved',retry_permission=permission)
    work.pop('retry_proposal')
    return permission


def consume(c,turn_id,work):
    permission=work.get('retry_permission')
    goal.require(permission and permission['turn_id']==turn_id,'REVISIT_RETRY_PERMISSION_REQUIRED')
    basis=permission['reservation']
    goal.require(reservation(c,basis['action_id'])==basis and basis['status']=='interrupted','REVISIT_RETRY_ATTEMPT_CHANGED')
    journal=c.execute('SELECT * FROM voice_control_intent_request WHERE request_key=?',(permission['request_key'],)).fetchone()
    goal.require(journal and journal['status']=='completed' and journal['execution_receipt_json'],'REVISIT_RETRY_PERMISSION_CHANGED')
    receipt=json.loads(journal['execution_receipt_json'])
    goal.require(receipt.get('agent_turn_id')==turn_id and receipt['result'].get('retry_permission')==permission
        and not receipt['result'].get('retry_consumed'),'REVISIT_RETRY_PERMISSION_USED')
    receipt['result']['retry_consumed']={'attempt':basis['attempts']+1,'action_id':basis['action_id']}
    c.execute('UPDATE voice_control_intent_request SET execution_receipt_json=? WHERE request_key=?',
        (json.dumps(receipt,ensure_ascii=False),permission['request_key']))
