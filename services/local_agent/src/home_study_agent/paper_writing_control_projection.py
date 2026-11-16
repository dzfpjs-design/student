"""Historical camera controls are execution receipts, never writing evidence."""
from datetime import datetime, timedelta
import hashlib
import json

from .agent_runtime import AgentRuntimeStore, _aware_datetime
from .writing_response_origin import require


def validate_document_control(c, row, prompt):
    try:
        _validate(c,row,prompt)
    except (KeyError,TypeError,ValueError,AttributeError):
        require(False,'PAPER_DOCUMENT_CONTROL_CHANGED')


def _validate(c,row,prompt):
    actual=c.execute('''SELECT t.*,s.status AS step_status,s.result_json,s.error_code,s.updated_at,
        p.status AS plan_status,p.context_manifest_json FROM agent_turn t
        JOIN agent_plan p USING(agent_turn_id) JOIN agent_step s USING(plan_id)
        WHERE s.step_id=? AND p.child_id=t.child_id AND p.session_id=t.session_id''',(row['step_id'],)).fetchone()
    require(actual and actual['tool_status']==actual['step_status']=='completed'
        and actual['plan_status'] in {'completed','waiting_child'} and not actual['error_code']
        and actual['child_id']==prompt['child_id'] and actual['session_id']==prompt['origin_session_id'],
        'PAPER_DOCUMENT_CONTROL_CHANGED')
    action=json.loads(actual['action_json']);arguments=json.loads(row['arguments_json'])
    require(set(action)=={'type','command','source','local_only','cloud_transmission_allowed',
        'voice_input_id','voice_input_binding','agent_runtime'} and action['type']=='control_document_monitor'
        and action['source']=='content_free_voice_control' and action['command'] in {'pause','resume'}
        and action['local_only'] is True and action['cloud_transmission_allowed'] is False
        and arguments=={'command':action['command'],'local_only':True,'cloud_transmission_allowed':False}
        and arguments['local_only'] is True and arguments['cloud_transmission_allowed'] is False,
        'PAPER_DOCUMENT_CONTROL_CHANGED')
    capture=AgentRuntimeStore.resolve_captured_voice(c,session_id=prompt['origin_session_id'],child_id=prompt['child_id'],
        voice_input_id=action['voice_input_id'],transcript=actual['transcript'])
    require(AgentRuntimeStore.voice_capture_snapshot(capture)==action['voice_input_binding']
        and capture['prompt_id']==prompt['prompt_id'] and capture['item_version']==prompt['item_version'],
        'PAPER_DOCUMENT_CONTROL_CHANGED')
    manifest=json.loads(actual['context_manifest_json']);frozen=manifest['voice_control_binding']
    journal=c.execute('SELECT * FROM voice_control_intent_request WHERE request_key=?',(frozen['request_key'],)).fetchone()
    fingerprint=hashlib.sha256(json.dumps([prompt['child_id'],prompt['origin_session_id'],actual['transcript']],ensure_ascii=False).encode()).hexdigest()
    require(journal and journal['status']=='completed' and journal['child_id']==prompt['child_id']
        and journal['session_id']==prompt['origin_session_id'] and journal['fingerprint']==fingerprint
        and json.loads(journal['binding_json'])==frozen['targets']
        and json.loads(journal['decision_json'])=={'status':'control','control':'document.'+action['command']}
        and frozen['control']=='document.'+action['command'], 'PAPER_DOCUMENT_CONTROL_CHANGED')
    result=json.loads(actual['result_json'])
    require(isinstance(result,dict) and set(result)<= {'execution_receipt','client_bridge_receipt'} and result,
        'PAPER_DOCUMENT_CONTROL_CHANGED')
    receipt=next(iter(result.values()))
    require(all(value==receipt for value in result.values()) and set(receipt)=={
        'capability','execution_location','action_type','status','completed_at','output'}
        and receipt['capability']=='document.monitor_control' and receipt['execution_location']=='client_device_bridge'
        and receipt['action_type']=='control_document_monitor' and receipt['status']=='completed',
        'PAPER_DOCUMENT_CONTROL_CHANGED')
    active=action['command']=='resume';status='active' if active else 'paused'
    require(receipt['output']=={'device_type':'document_camera','device_status':status,'frame_confirmed':active,
        'lease_confirmed':True,'persisted_status':status}
        and type(receipt['output']['frame_confirmed']) is bool and type(receipt['output']['lease_confirmed']) is bool,
        'PAPER_DOCUMENT_CONTROL_CHANGED')
    instant=lambda value:datetime.fromisoformat(value.replace('Z','+00:00'))
    completed=instant(receipt['completed_at']);started=instant(actual['created_at']);saved=instant(actual['updated_at'])
    require(completed.tzinfo and started-timedelta(seconds=30)<=completed<=saved+timedelta(seconds=30)
        and started<=saved and instant(journal['created_at'])<=instant(journal['completed_at'])<=started,
        'PAPER_DOCUMENT_CONTROL_CHANGED')
    # Live device validation already ran when this receipt was stored. Requiring
    # an active lease now would destroy valid history on every application exit.
    if active:
        leases=c.execute("SELECT * FROM sensor_lease WHERE session_id=? AND device_type='document_camera'",
                          (prompt['origin_session_id'],)).fetchall()
        # updated_at is the server commit anchor; completed_at is a client
        # clock with the separate bounded skew contract checked above.
        require(any(_aware_datetime(lease['opened_at'])<=saved<=_aware_datetime(lease['expires_at'])
            and (not lease['released_at'] or saved<=_aware_datetime(lease['released_at'])) for lease in leases),
            'PAPER_DOCUMENT_CONTROL_CHANGED')
