"""Attested native text input, kept separate from ASR and model tool arguments."""
from datetime import datetime, timezone
import hashlib
import hmac
import json

from fastapi import Request
from fastapi.responses import JSONResponse
from .writing_response_origin import WritingOriginalService, _prompt, _original_text, require, VERSION
from .local_supply import digest

FIELDS=('child_id','session_id','prompt_id','item_version','diagnostic_item_id','text','client_action_id')


def original_receipt(c, prompt, artifact_id, text):
    artifact=c.execute("SELECT * FROM capture_artifact WHERE artifact_id=? AND artifact_type='writing_original' AND child_id=? AND session_id=?",
        (artifact_id,prompt['child_id'],prompt['origin_session_id'])).fetchone()
    require(artifact is not None and artifact['status']=='completed','NATIVE_ORIGINAL_UNAVAILABLE')
    details=json.loads(artifact['details_json'])
    require(details.get('schema')==VERSION and details.get('prompt_id')==prompt['prompt_id'] and details.get('item_version')==prompt['item_version']
        and details.get('origin')=={'kind':'native_editor'},'NATIVE_ORIGINAL_SCOPE_CHANGED')
    require(_original_text(c,prompt,artifact,details['origin'])==text and details.get('response_hash')==digest(text),
        'NATIVE_ORIGINAL_TEXT_CHANGED')
    revision=dict(artifact)
    revision['details_json']={k:v for k,v in details.items() if k!='attempt_binding'}
    return dict(child_id=prompt['child_id'],session_id=prompt['origin_session_id'],prompt_id=prompt['prompt_id'],
        item_version=prompt['item_version'],diagnostic_item_id=prompt['diagnostic_item_id'],artifact_id=artifact_id,
        original_revision=digest(revision),text_sha256=hashlib.sha256(text.encode()).hexdigest(),
        status='completed',local_only=True,cloud_transmission_allowed=False)


def validate_submission(c,prompt,artifact_id,revision,text):
    require(isinstance(artifact_id,str) and isinstance(revision,str),'NATIVE_ORIGINAL_REFERENCE_REQUIRED')
    actual=original_receipt(c,prompt,artifact_id,text)
    require(hmac.compare_digest(actual['original_revision'],revision),'NATIVE_ORIGINAL_REVISION_CHANGED')


def current_scope(c,session_id,value):
    prompt,_=_prompt(c,session_id,value['prompt_id'],value['item_version'],live=False)
    current=c.execute('SELECT prompt_id FROM diagnostic_prompt WHERE origin_session_id=? ORDER BY rowid DESC LIMIT 1',(session_id,)).fetchone()
    require(prompt['session_status']=='active' and prompt['session_state']=='study'
        and current and current['prompt_id']==value['prompt_id']
        and prompt['child_id']==value['child_id'] and prompt['diagnostic_item_id']==value['diagnostic_item_id'],
        'NATIVE_SCOPE_CHANGED')
    return prompt


def register_native_writing(application,database,settings,complete_prompt_result=None):

    @application.post('/v1/sessions/{session_id}/native-writing-resume')
    async def resume_native_writing(session_id: str,request: Request):
        try:
            from .native_writing_resume import resume_saved
            packet,replayed=resume_saved(database,session_id,await request.json())
        except (ValueError,KeyError,TypeError):
            return JSONResponse({'error':'native_writing_resume_unavailable'},status_code=409,
                                headers={'Cache-Control':'no-store'})
        if not replayed and complete_prompt_result is not None:
            packet['result']=await complete_prompt_result(session_id,packet['result'])
        return JSONResponse(packet,headers={'Cache-Control':'no-store'})

    @application.get('/v1/sessions/{session_id}/native-writing-receipt')
    def read_native_writing_receipt(session_id: str, child_id: str, prompt_id: str, item_version: str,
                                   diagnostic_item_id: str, text_sha256: str, capture_action_id: str,
                                   answer_action_id: str = '', resume_action_id: str = ''):
        try:
            value = dict(child_id=child_id, session_id=session_id, prompt_id=prompt_id,
                         item_version=item_version, diagnostic_item_id=diagnostic_item_id)
            require(all(v.strip() and len(v)<=200 for v in value.values())
                    and 1<=len(capture_action_id)<=200 and len(answer_action_id)<=200 and len(resume_action_id)<=200
                    and len(text_sha256)==64 and all(ch in '0123456789abcdef' for ch in text_sha256),
                    'NATIVE_INPUT_INVALID')
            from .diagnostic_prompt import DiagnosticPromptService
            from .writing_response_origin import read_original_written_source
            with database.connect() as c:
                # Recovery must never resume scoring, create feedback, or repair a prompt.
                c.execute('PRAGMA query_only=ON')
                c.execute('BEGIN')
                from .native_writing_rejection import read_rejection
                rejected = read_rejection(c,dict(**value,text_sha256=text_sha256,
                    capture_action_id=capture_action_id,answer_action_id=answer_action_id))
                if rejected is not None:
                    return JSONResponse(rejected,headers={'Cache-Control':'no-store'})
                prompt = current_scope(c, session_id, value)
                require(not DiagnosticPromptService._scheduled_source_reason(c, prompt), 'NATIVE_SOURCE_CHANGED')
                artifact = c.execute('SELECT * FROM capture_artifact WHERE client_action_id=?',
                                     ('writing-original:'+capture_action_id,)).fetchone()
                packet = dict(**value, text_sha256=text_sha256, capture_action_id=capture_action_id,
                              answer_action_id=answer_action_id, state='capture_unknown', original=None, result=None,
                              local_only=True, cloud_transmission_allowed=False)
                if artifact is not None:
                    require(artifact['child_id']==child_id and artifact['session_id']==session_id,
                            'NATIVE_ORIGINAL_SCOPE_CHANGED')
                    text = artifact['extracted_text']
                    require(isinstance(text,str) and hashlib.sha256(text.encode()).hexdigest()==text_sha256,
                            'NATIVE_ORIGINAL_TEXT_CHANGED')
                    original = original_receipt(c,prompt,artifact['artifact_id'],text)
                    details = json.loads(artifact['details_json'])
                    require(details.get('request_hash')==digest([session_id,prompt_id,item_version,
                            {'kind':'native_editor','text':text}]), 'NATIVE_ORIGINAL_SCOPE_CHANGED')
                    packet.update(state='captured',original=original)
                    if prompt['answer_action_id'] is not None:
                        require(answer_action_id and prompt['answer_action_id']==answer_action_id
                                and prompt['writing_original_artifact_id']==artifact['artifact_id'],
                                'NATIVE_ANSWER_ACTION_CHANGED')
                        if prompt['status']=='answer_saved':
                            require(json.loads(prompt['answer_json'])['answer']==text, 'NATIVE_ORIGINAL_TEXT_CHANGED')
                            packet['state']='answer_saved'
                        else:
                            require(prompt['status']=='answered', 'NATIVE_ANSWER_UNAVAILABLE')
                            attempt = DiagnosticPromptService._receipt(c,prompt)
                            require(attempt is not None, 'NATIVE_ANSWER_UNAVAILABLE')
                            read_original_written_source(c,prompt,attempt)
                            packet.update(state='answered',result=DiagnosticPromptService._result(c,prompt))
                if resume_action_id:
                    from .native_writing_resume import read_link
                    packet['resume']=read_link(c,session_id,resume_action_id,packet)
                return JSONResponse(packet,headers={'Cache-Control':'no-store'})
        except (ValueError,KeyError,TypeError):
            return JSONResponse({'error':'native_writing_receipt_unavailable'},status_code=409,
                                headers={'Cache-Control':'no-store'})

    @application.post('/v1/sessions/{session_id}/native-writing-original')
    async def capture_native_writing(session_id: str,request: Request):
        attested = False
        try:
            value=await request.json()
            require(isinstance(value,dict) and set(value)==set(FIELDS) and all(isinstance(value[k],str)
                and value[k].strip() and len(value[k])<=(4000 if k=='text' else 200) for k in FIELDS),'NATIVE_INPUT_INVALID')
            require(value['session_id']==session_id,'NATIVE_SESSION_CHANGED')
            key=settings.native_launch_attestation_key
            issued=request.headers.get('X-HSC-Native-Writing-Time','')
            timestamp=datetime.fromisoformat(issued.replace('Z','+00:00'))
            require(timestamp.tzinfo is not None and abs((datetime.now(timezone.utc)-timestamp).total_seconds())<=60,
                'NATIVE_CAPTURE_EXPIRED')
            require(isinstance(key,str) and len(key)==64,'NATIVE_AUTHORITY_UNAVAILABLE')
            message=json.dumps(['native_writing_capture_v1',*[value[k] for k in FIELDS],issued],ensure_ascii=False,separators=(',',':'))
            expected=hmac.new(bytes.fromhex(key),message.encode(),hashlib.sha256).hexdigest()
            require(hmac.compare_digest(expected,request.headers.get('X-HSC-Native-Writing','')),'NATIVE_AUTHORITY_REQUIRED')
            attested = True
            with database.connect() as c:
                current_scope(c,session_id,value)
            original=WritingOriginalService(database).capture_native_response(session_id,prompt_id=value['prompt_id'],
                item_version=value['item_version'],text=value['text'],client_action_id=value['client_action_id'],expected_scope=value)
            with database.connect() as c:
                prompt=current_scope(c,session_id,value)
                result=original_receipt(c,prompt,original['artifact_id'],value['text'])
            return JSONResponse(result,headers={'Cache-Control':'no-store'})
        except (ValueError,KeyError,TypeError):
            if attested:
                from .native_writing_rejection import record_rejection
                record_rejection(database,'capture',value)
            return JSONResponse({'error':'native_writing_original_unavailable'},status_code=409,headers={'Cache-Control':'no-store'})
