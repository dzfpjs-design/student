"""Exact continuation of a durable answer under a fresh Runtime admission.

No new answer, status table, or long-lived authority. The existing prompt resume
borrows one writer transaction; its old authorized_turn_id remains unchanged.
"""
from contextlib import nullcontext
from contextvars import ContextVar
from datetime import datetime, timezone
import json

from .local_supply import digest
from .writing_response_origin import require

_active = ContextVar('native_saved_answer_resume', default=None)


def _id(session_id, action_id):
    return 'native-writing-resume:'+digest([session_id,action_id])


def _connection(c):
    from .diagnostic_prompt import _BorrowedConnection
    while isinstance(c,_BorrowedConnection):c=c.connection
    return c


def _request(request):
    require(isinstance(request,dict) and set(request)=={'target','authorized_turn_id','client_action_id','capture_action_id'},
        'NATIVE_RESUME_INVALID')
    require(all(isinstance(request[k],str) and 1<=len(request[k])<=200 for k in
        ('authorized_turn_id','client_action_id','capture_action_id')) and isinstance(request['target'],dict),
        'NATIVE_RESUME_INVALID')
    target=request['target']
    fields={'child_id','session_id','prompt_id','item_version','diagnostic_item_id','text_sha256'}
    require(set(target)==fields|{'saved_answer'} and all(isinstance(target[k],str) and 1<=len(target[k])<=200 for k in fields),
        'NATIVE_RESUME_INVALID')
    saved=target['saved_answer']
    require(isinstance(saved,dict) and set(saved)=={'answer_action_id','original_artifact_id','original_revision','authorized_turn_id','payload_sha256'}
        and all(isinstance(saved[k],str) and 1<=len(saved[k])<=200 for k in saved if k!='authorized_turn_id')
        and (saved['authorized_turn_id'] is None or isinstance(saved['authorized_turn_id'],str) and 1<=len(saved['authorized_turn_id'])<=200),
        'NATIVE_RESUME_INVALID')
    require(all(len(value)==64 and all(ch in '0123456789abcdef' for ch in value) for value in
        (target['text_sha256'],saved['original_revision'],saved['payload_sha256'])),'NATIVE_RESUME_INVALID')
    require(request['client_action_id']=='native-writing-resume:'+request['authorized_turn_id'],'NATIVE_RESUME_IDENTITY_REQUIRED')


def _current(c, session_id, request, *, admission):
    from .native_writing import current_scope
    from .native_writing_control import snapshot, validate_retained_turn
    from .diagnostic_prompt import DiagnosticPromptService
    target=request['target']
    require(target['session_id']==session_id,'NATIVE_RESUME_SESSION_CHANGED')
    row=current_scope(c,session_id,target)
    require(row['status'] in {'answer_saved','answered'} and row['answer_action_id'], 'NATIVE_ANSWER_NOT_SAVED')
    require(not DiagnosticPromptService._scheduled_source_reason(c,row),'NATIVE_SOURCE_CHANGED')
    actual=snapshot(c,session_id,target['child_id'])
    require(actual==target and isinstance(target.get('saved_answer'),dict),'NATIVE_SAVED_ANSWER_CHANGED')
    original=c.execute('SELECT client_action_id FROM capture_artifact WHERE artifact_id=?',
        (target['saved_answer']['original_artifact_id'],)).fetchone()
    require(original and original[0]=='writing-original:'+request['capture_action_id'],'NATIVE_CAPTURE_ACTION_CHANGED')
    if admission:
        from .agent_runtime import AgentRuntimeStore
        control=AgentRuntimeStore.inspect_voice_control_execution(c,request['authorized_turn_id'])
        require(control and control['control']=='native_writing.submit_retained'
            and control['targets'].get('native_writing')==target,'NATIVE_RESUME_ADMISSION_CHANGED')
        # Validate the new admitted command; never make the original command live again.
        text=c.execute('SELECT extracted_text FROM capture_artifact WHERE artifact_id=?',
            (target['saved_answer']['original_artifact_id'],)).fetchone()[0]
        require(validate_retained_turn(c,request['authorized_turn_id'],row,text),'NATIVE_RESUME_ADMISSION_REQUIRED')
    return row


def validate_active_resume(c, original_turn_id, *, child_id, session_id, binding, answer):
    scope=_active.get()
    if scope is None or _connection(c) is not scope[0]:return False
    require(c.in_transaction,'NATIVE_RESUME_TRANSACTION_REQUIRED')
    request=scope[1]
    row=_current(c,session_id,request,admission=True)
    saved=request['target']['saved_answer']
    require(row['child_id']==child_id and row['authorized_turn_id']==original_turn_id==saved['authorized_turn_id']
        and binding=={k:row[k] for k in ('prompt_id','item_version')},'NATIVE_RESUME_SAVED_BINDING_CHANGED')
    payload=json.loads(row['answer_json'])
    require(payload['answer']==answer and digest(payload)==saved['payload_sha256'],'NATIVE_RESUME_SAVED_TEXT_CHANGED')
    return True


def _packet(c, row, request):
    from .native_writing import original_receipt
    from .diagnostic_prompt import DiagnosticPromptService
    from .writing_response_origin import read_original_written_source
    attempt=DiagnosticPromptService._receipt(c,row)
    require(attempt is not None,'NATIVE_RESUME_ATTEMPT_REQUIRED')
    read_original_written_source(c,row,attempt)
    target=request['target']; saved=target['saved_answer']
    original=original_receipt(c,row,saved['original_artifact_id'],attempt['submitted_answer'])
    return {**{k:v for k,v in target.items() if k!='saved_answer'},
        'capture_action_id':request['capture_action_id'],'answer_action_id':saved['answer_action_id'],
        'state':'answered','original':original,'result':DiagnosticPromptService._result(c,row),
        'local_only':True,'cloud_transmission_allowed':False,
        'resume':{'client_action_id':request['client_action_id'],'authorized_turn_id':request['authorized_turn_id'],
            'original_authorized_turn_id':saved['authorized_turn_id'],'attempt_id':attempt['attempt_id']}}


def read_link(c, session_id, action_id, packet):
    event=c.execute("SELECT * FROM audit_event WHERE audit_id=? AND action='native_writing_resumed' AND child_id=?",
        (_id(session_id,action_id),packet['child_id'])).fetchone()
    if event is None:return None
    value=json.loads(event['summary_json']); request=value['request']
    _request(request)
    require(request['client_action_id']==action_id,'NATIVE_RESUME_ACTION_CHANGED')
    row=_current(c,session_id,request,admission=False)
    expected=_packet(c,row,request)
    require(row['attempt_id']==value['attempt_id'] and all(packet.get(k)==expected[k] for k in (
        'child_id','session_id','prompt_id','item_version','diagnostic_item_id','text_sha256',
        'capture_action_id','answer_action_id')),'NATIVE_RESUME_RECEIPT_CHANGED')
    return expected['resume']


def resume_saved(database, session_id, request):
    _request(request)
    request=json.loads(json.dumps(request))
    from .diagnostic_prompt import DiagnosticPromptService
    with database.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        row=_current(c,session_id,request,admission=False)
        old=c.execute('SELECT * FROM audit_event WHERE audit_id=?',(_id(session_id,request['client_action_id']),)).fetchone()
        if old:
            value=json.loads(old['summary_json'])
            require(old['action']=='native_writing_resumed' and old['child_id']==row['child_id']
                and value['request']==request and value['attempt_id']==row['attempt_id'],'NATIVE_RESUME_ACTION_CONFLICT')
            return _packet(c,row,request),True
        require(row['status']=='answer_saved','NATIVE_RESUME_REQUIRES_SAVED_ANSWER')
        _current(c,session_id,request,admission=True)
        class TransactionPrompt(DiagnosticPromptService):
            def __init__(self):self.database=None
            def _transaction(self):return nullcontext(c)
        token=_active.set((c,request))
        try:
            result=TransactionPrompt().resume(session_id,prompt_id=row['prompt_id'])
            require(result['status']=='answered','NATIVE_RESUME_NOT_COMPLETED')
            row=_current(c,session_id,request,admission=True)
            packet=_packet(c,row,request)
            require(c.in_transaction,'NATIVE_RESUME_TRANSACTION_REQUIRED')
            c.execute('INSERT INTO audit_event(audit_id,actor,action,child_id,summary_json,occurred_at) VALUES(?,?,?,?,?,?)',
                (_id(session_id,request['client_action_id']),'native_writing','native_writing_resumed',row['child_id'],
                 json.dumps({'request':request,'attempt_id':row['attempt_id']},sort_keys=True),datetime.now(timezone.utc).isoformat()))
            # Final same-writer checks include in-transaction source/authority changes.
            _current(c,session_id,request,admission=True)
            c.commit()
            return packet,False
        finally:
            _active.reset(token)
