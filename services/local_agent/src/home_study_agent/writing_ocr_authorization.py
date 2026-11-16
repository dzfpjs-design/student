from contextlib import nullcontext
"""Exact local page-use permission, separate from Tutor question confirmation.

Readback is a transport receipt. Explicit child permission is not a writing score.
No raw media is copied, retained longer or authorized for cloud use by this module.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
from uuid import uuid4

from .local_supply import digest
from .writing_response_origin import _prompt, require

VERSION = 'writing_ocr_use_v1'


def _now():
    return datetime.now(timezone.utc)


def _document(c, child_id, session_id, artifact_id, revision_id, start, end, *, review=False):
    require(type(start) is int and type(end) is int, 'DOCUMENT_SPAN_REQUIRED')
    row = c.execute('''SELECT a.*,r.monitor_id,r.revision_index,r.content_signature,r.content_sha256,
        r.ocr_confidence,r.review_required,r.created_at AS revision_created_at FROM document_page_revision r
        JOIN capture_artifact a ON a.artifact_id=r.artifact_id AND a.child_id=r.child_id AND a.session_id=r.session_id
        JOIN document_monitor_session m ON m.monitor_id=r.monitor_id AND m.child_id=r.child_id AND m.session_id=r.session_id
        WHERE r.revision_id=? AND r.artifact_id=? AND r.child_id=? AND r.session_id=?''',
        (revision_id,artifact_id,child_id,session_id)).fetchone()
    require(row and row['artifact_type'] == 'document_ocr'
            and ((row['status'] == 'completed' and row['review_required'] == 0)
                 or (review and row['status'] in {'completed', 'quality_warning'})), 'DOCUMENT_REVIEW_REQUIRED')
    text = row['extracted_text']
    require(isinstance(text,str) and 0 <= start < end <= len(text) and end-start <= 12000,
            'DOCUMENT_SPAN_INVALID')
    from .document_monitoring import DocumentMonitoringService
    require(hashlib.sha256(DocumentMonitoringService._normalized_text(text).encode()).hexdigest() == row['content_sha256'],
            'DOCUMENT_REVISION_CHANGED')
    require(text[start:end].strip(), 'ORIGINAL_TEXT_REQUIRED')
    # Portable paths are not source identity; use the actual OCR/revision content.
    source = {k:row[k] for k in row.keys() if k != 'local_path'}
    source.update(revision_id=revision_id,response_start=start,response_end=end)
    return {'text':text[start:end], 'source_version':digest(source)}


def _readback(question, response):
    return f'这道题是：{question}\n这页选中的书面作答识别为：{response}\n请核对：这是你自己写的本题作答，而且上面的识别文字准确吗？'


def _confirmation(c, session_id, confirmation_id, binding_version, *, live=False, _seen=None):
    row = c.execute('SELECT * FROM writing_ocr_confirmation WHERE confirmation_id=? AND session_id=?',
                    (confirmation_id,session_id)).fetchone()
    require(row and row['binding_version'] == binding_version, 'OCR_CONFIRMATION_SCOPE_CHANGED')
    prompt,item = _prompt(c,session_id,row['prompt_id'],row['item_version'],live=live)
    require(prompt['child_id'] == row['child_id'], 'OCR_CONFIRMATION_SCOPE_CHANGED')
    from .writing_ocr_review import source as review_source, readback as review_readback
    source = review_source(c, row, live=live, seen=_seen or set())
    if source is None:
        source = _document(c,row['child_id'],session_id,row['artifact_id'],row['revision_id'],row['response_start'],row['response_end'])
    require(source['source_version'] == row['source_version'], 'OCR_CONFIRMATION_SOURCE_CHANGED')
    require(not c.execute("SELECT 1 FROM writing_ocr_event WHERE confirmation_id=? AND kind='retract'", (confirmation_id,)).fetchone(),
            'OCR_AUTHORIZATION_WITHDRAWN')
    if live:
        require(_now() < datetime.fromisoformat(row['expires_at']), 'OCR_CONFIRMATION_EXPIRED')
    text = (review_readback(item['prompt'],source['text'],version=source['readback_version'])
            if source.get('writing_review_required') else _readback(item['prompt'],source['text']))
    require(hashlib.sha256(text.encode()).hexdigest() == row['readback_sha256'], 'READBACK_TEXT_CHANGED')
    return row,source,text


def arrival_binding(c, *, session_id, confirmation_id, binding_version, voice_input_id):
    """Host freezes this on receipt of confirmation speech, not from model JSON."""
    row,_,_ = _confirmation(c,session_id,confirmation_id,binding_version,live=True)
    voice = c.execute('SELECT * FROM voice_input WHERE voice_input_id=? AND session_id=? AND child_id=?',
                      (voice_input_id,session_id,row['child_id'])).fetchone()
    require(voice, 'OCR_AUTHORIZING_VOICE_REQUIRED')
    capture = _voice(c,session_id,row['child_id'],voice_input_id,voice['transcript'])
    require(capture['question_validation'] == 'valid' and capture['prompt_id'] == row['prompt_id']
            and capture['item_version'] == row['item_version'], 'OCR_AUTHORIZING_VOICE_SCOPE_CHANGED')
    capture = {k:v for k,v in capture.items() if k != 'raw_audio_available'}
    return dict(confirmation_id=confirmation_id,binding_version=binding_version,child_id=row['child_id'],session_id=session_id,
        prompt_id=row['prompt_id'],item_version=row['item_version'],artifact_id=row['artifact_id'],revision_id=row['revision_id'],
        voice_input_id=voice_input_id,voice_version=digest(capture))


def _voice(c,session_id,child_id,voice_input_id,transcript):
    from .voice_capture_binding import resolve_voice_capture,VoiceCaptureBindingError
    try:
        capture = resolve_voice_capture(c,session_id=session_id,child_id=child_id,voice_input_id=voice_input_id,transcript=transcript)
    except VoiceCaptureBindingError as error:
        raise ValueError('WRITING_OCR_'+str(error)) from error
    source = c.execute('SELECT wav_sha256,created_at,client_action_id FROM voice_capture_binding WHERE voice_input_id=?',
                        (voice_input_id,)).fetchone()
    if not (source and isinstance(source['wav_sha256'],str) and re.fullmatch(r'[0-9a-f]{64}',source['wav_sha256'])):
        # A real native ingress is a transcript witness, never a fabricated WAV
        # hash or a claim that local audio was stored/heard acoustically.
        from .realtime_native_context import _native_witness
        from .local_supply import digest
        from types import SimpleNamespace
        voice = c.execute('SELECT * FROM voice_input WHERE voice_input_id=? AND child_id=? AND session_id=?',
            (voice_input_id,child_id,session_id)).fetchone()
        require(voice and voice['purpose']=='native_cloud_dialogue'
            and _native_witness(SimpleNamespace(page_source_store=None),c,voice) is not None, 'OCR_VOICE_SOURCE_REQUIRED')
        audit = c.execute('SELECT summary_json FROM audit_event WHERE audit_id=?', ('native-input-'+voice_input_id,)).fetchone()
        require(audit, 'OCR_VOICE_SOURCE_REQUIRED')
        return {**capture,'capture_source':{'kind':'native_cloud_transcript',
            'native_input_sha256':digest(json.loads(audit[0])), 'created_at':source['created_at'],
            'client_action_id':source['client_action_id'], 'raw_audio_available':False}}
    return {**capture,'capture_source':dict(source)}


def _semantic_confirmation(c, row, turn, action, binding):
    from .realtime_native_special_sources import semantic_version
    native = semantic_version(c, turn['agent_turn_id'], capability='writing.confirm_original', completion=turn['tool_status']=='completed')
    if native is not None:
        return native
    reference = action.get('writing_ocr_semantic_receipt')
    if reference is None:
        return None
    require(isinstance(reference,dict) and set(reference)=={'request_key'}
        and isinstance(reference['request_key'],str), 'OCR_SEMANTIC_RECEIPT_INVALID')
    require(c.execute("SELECT 1 FROM sqlite_master WHERE name='voice_control_intent_request'").fetchone(),
            'OCR_SEMANTIC_RECEIPT_MISSING')
    receipt = c.execute('SELECT * FROM voice_control_intent_request WHERE request_key=? AND child_id=? AND session_id=?',
        (reference['request_key'],row['child_id'],row['session_id'])).fetchone()
    fingerprint = hashlib.sha256(json.dumps([row['child_id'],row['session_id'],turn['transcript']],ensure_ascii=False).encode()).hexdigest()
    require(receipt and receipt['status']=='completed' and receipt['fingerprint']==fingerprint,
            'OCR_SEMANTIC_RECEIPT_CHANGED')
    decision = json.loads(receipt['decision_json'])
    sources = json.loads(receipt['binding_json'])
    require(isinstance(decision,dict) and decision.get('status')=='control'
        and decision.get('control')=='writing.confirm_original' and isinstance(sources,dict)
        and sources.get('writing_ocr')==binding, 'OCR_SEMANTIC_RECEIPT_CHANGED')
    require(receipt['completed_at'] and datetime.fromisoformat(receipt['created_at'])
        <= datetime.fromisoformat(receipt['completed_at']) <= _now(), 'OCR_SEMANTIC_RECEIPT_TIME_INVALID')
    # Admission may later append an execution receipt; that is not a revision of
    # the classifier's exact original sentence and frozen page-use target.
    return digest({k:receipt[k] for k in ('request_key','child_id','session_id','fingerprint',
        'binding_json','status','decision_json','created_at','completed_at')})


def _turn(c, row, turn_id, binding, *, live):
    turn = c.execute('SELECT * FROM agent_turn WHERE agent_turn_id=? AND child_id=? AND session_id=?',
                     (turn_id,row['child_id'],row['session_id'])).fetchone()
    require(turn, 'OCR_AUTHORIZING_TURN_REQUIRED')
    action = json.loads(turn['action_json'])
    require(action.get('type') == 'confirm_writing_original' and action.get('writing_ocr_binding') == binding
            and action.get('voice_input_id') == binding['voice_input_id'], 'OCR_AUTHORIZING_TURN_CHANGED')
    capture = _voice(c,row['session_id'],row['child_id'],binding['voice_input_id'],turn['transcript'])
    require(capture['question_validation'] == 'valid' and capture['prompt_id'] == row['prompt_id']
        and capture['item_version'] == row['item_version']
        and digest({k:v for k,v in capture.items() if k != 'raw_audio_available'}) == binding['voice_version'],
        'OCR_AUTHORIZING_VOICE_CHANGED')
    normalized = re.sub(r'[\s，。！!,、]', '',turn['transcript'])
    semantic = _semantic_confirmation(c,row,turn,action,binding)
    if c.execute('SELECT 1 FROM writing_ocr_review WHERE confirmation_id=?', (row['confirmation_id'],)).fetchone():
        from .writing_ocr_review import explicit_body_confirmation, uncertain_confirmation
        require((semantic or explicit_body_confirmation(turn['transcript'])) and not uncertain_confirmation(turn['transcript']),
                'OCR_EXPLICIT_BODY_CONFIRMATION_REQUIRED')
    require(not re.search(r'[?？]|不是我|别人|他写|她写|不确定|不知道|看不清|改字|识别.*(?:(?<!没)有错|不对|错误)|需要修改|还要修改',
        turn['transcript']), 'OCR_EXPLICIT_USE_CONFIRMATION_REQUIRED')
    require(semantic or normalized in {'是','是的','对','没错','确认','确认准确','是我自己写的而且识别准确',
        '这是我自己写的答案识别正确','这页是我的书面作答识别正确',
        '对是我写的识别没有错'}, 'OCR_EXPLICIT_USE_CONFIRMATION_REQUIRED')
    step = c.execute('''SELECT s.status,p.status AS plan_status FROM agent_plan p JOIN agent_step s USING(plan_id)
        WHERE p.agent_turn_id=? AND p.child_id=? AND p.session_id=? AND s.capability_name='writing.confirm_original' ''',
        (turn_id,row['child_id'],row['session_id'])).fetchall()
    require(len(step)==1 and step[0]['status'] in ({'running'} if live else {'running','completed'})
            and step[0]['plan_status'] in ({'running'} if live else {'running','completed'}), 'OCR_AUTHORIZING_STEP_NOT_ADMITTED')
    return {**dict(turn),'semantic_receipt_version':semantic}


class WritingOcrAuthorizationService:
    def __init__(self, database, *, runtime_epoch=None):
        self.database = database
        self.runtime_epoch = runtime_epoch or uuid4().hex

    def reset_playback_epoch(self):
        """Call only after a successful restore; saved authority is not replay."""
        self.runtime_epoch = uuid4().hex
        return self.runtime_epoch

    def initialize(self):
        with self.database.connect() as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS writing_ocr_confirmation(
                confirmation_id TEXT PRIMARY KEY, child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
                prompt_id TEXT NOT NULL REFERENCES diagnostic_prompt(prompt_id) ON DELETE CASCADE, item_version TEXT NOT NULL,
                artifact_id TEXT NOT NULL REFERENCES capture_artifact(artifact_id) ON DELETE CASCADE,
                revision_id TEXT NOT NULL REFERENCES document_page_revision(revision_id) ON DELETE CASCADE,
                response_start INTEGER NOT NULL,response_end INTEGER NOT NULL,source_version TEXT NOT NULL,
                binding_version TEXT NOT NULL,readback_sha256 TEXT NOT NULL,created_at TEXT NOT NULL,expires_at TEXT NOT NULL,
                client_action_id TEXT NOT NULL,request_hash TEXT NOT NULL,UNIQUE(child_id,client_action_id));
                CREATE TABLE IF NOT EXISTS writing_ocr_event(
                event_id TEXT PRIMARY KEY,confirmation_id TEXT NOT NULL REFERENCES writing_ocr_confirmation(confirmation_id) ON DELETE CASCADE,
                child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                kind TEXT NOT NULL CHECK(kind IN ('started','ended','authorize','retract')),
                request_json TEXT NOT NULL,payload_json TEXT NOT NULL,created_at TEXT NOT NULL,
                client_action_id TEXT NOT NULL,UNIQUE(child_id,client_action_id));''')
            from .writing_ocr_review import initialize
            initialize(c)
            c.commit()

    def prepare_confirmation(self, session_id, *, prompt_id,item_version,artifact_id,revision_id,
                             response_start,response_end,client_action_id,connection=None):
        require(isinstance(client_action_id,str) and 1 <= len(client_action_id) <= 200, 'ACTION_REQUIRED')
        request = digest([session_id,prompt_id,item_version,artifact_id,revision_id,response_start,response_end])
        with (self.database.connect() if connection is None else nullcontext(connection)) as c:
            if connection is None:
                c.execute('BEGIN IMMEDIATE')
            else:
                require(c.in_transaction, 'WRITE_TRANSACTION_REQUIRED')
            prompt,item = _prompt(c,session_id,prompt_id,item_version,live=True)
            old = c.execute('SELECT * FROM writing_ocr_confirmation WHERE child_id=? AND client_action_id=?',
                            (prompt['child_id'],client_action_id)).fetchone()
            if old:
                require(old['request_hash'] == request, 'ACTION_CONFLICT')
                return self._result(c,old)
            source = _document(c,prompt['child_id'],session_id,artifact_id,revision_id,response_start,response_end,review=True)
            quality = c.execute('''SELECT a.status,r.review_required FROM capture_artifact a
                JOIN document_page_revision r USING(artifact_id) WHERE r.revision_id=?''', (revision_id,)).fetchone()
            review = None
            readback = _readback
            if quality['status'] != 'completed' or quality['review_required']:
                from .writing_ocr_review import current_page, readback as review_readback
                current_page(c, dict(child_id=prompt['child_id'],session_id=session_id,artifact_id=artifact_id,revision_id=revision_id))
                review = dict(raw_source_version=source['source_version'], text=source['text'], readback_version=2)
                source['source_version'] = digest(['writing_ocr_review_v1', review])
                readback = lambda question, text: review_readback(question, text, version=2)
            cid = uuid4().hex
            version = digest([VERSION,cid,request,source['source_version']])
            readback_hash = hashlib.sha256(readback(item['prompt'],source['text']).encode()).hexdigest()
            now = _now()
            c.execute('INSERT INTO writing_ocr_confirmation VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (cid,prompt['child_id'],session_id,prompt_id,item_version,artifact_id,revision_id,response_start,response_end,
                 source['source_version'],version,readback_hash,now.isoformat(),(now+timedelta(minutes=15)).isoformat(),client_action_id,request))
            if review:
                c.execute('INSERT INTO writing_ocr_review VALUES(?,?)', (cid,json.dumps(review,ensure_ascii=False,sort_keys=True)))
            if connection is None:
                c.commit()
            return self._result(c,c.execute('SELECT * FROM writing_ocr_confirmation WHERE confirmation_id=?',(cid,)).fetchone())

    def _result(self,c,row):
        _,source,text = _confirmation(c,row['session_id'],row['confirmation_id'],row['binding_version'])
        return {**{k:row[k] for k in ('confirmation_id','child_id','session_id','prompt_id','item_version','artifact_id','revision_id',
            'binding_version','readback_sha256','expires_at')},'response_text':source['text'],'readback_text':text,
            'local_only':True,'cloud_transmission_allowed':False}

    def read(self,session_id,*,confirmation_id,binding_version):
        with self.database.connect() as c:
            c.execute('BEGIN')
            row,_,_ = _confirmation(c,session_id,confirmation_id,binding_version)
            return self._result(c,row)

    def _event(self,c,row,kind,key,request,payload):
        require(isinstance(key,str) and 1 <= len(key) <= 200, 'ACTION_REQUIRED')
        prior = c.execute('SELECT * FROM writing_ocr_event WHERE child_id=? AND client_action_id=?',(row['child_id'],key)).fetchone()
        encoded = json.dumps(request,sort_keys=True)
        if prior:
            require(prior['confirmation_id']==row['confirmation_id'] and prior['kind']==kind and prior['request_json']==encoded, 'ACTION_CONFLICT')
            return json.loads(prior['payload_json'])
        c.execute('INSERT INTO writing_ocr_event VALUES(?,?,?,?,?,?,?,?)',
            (payload['event_id'],row['confirmation_id'],row['child_id'],kind,encoded,json.dumps(payload,sort_keys=True),_now().isoformat(),key))
        return payload

    def record_readback(self,session_id,*,confirmation_id,binding_version,playback_id,event,readback_sha256,client_action_id):
        require(event in {'started','ended'} and isinstance(playback_id,str) and 1 <= len(playback_id) <= 200,'READBACK_EVENT_INVALID')
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row,_,_ = _confirmation(c,session_id,confirmation_id,binding_version,live=True)
            require(readback_sha256==row['readback_sha256'],'READBACK_TEXT_CHANGED')
            request = dict(binding_version=binding_version,playback_id=playback_id,event=event,readback_sha256=readback_sha256)
            old = c.execute('SELECT * FROM writing_ocr_event WHERE child_id=? AND client_action_id=?',(row['child_id'],client_action_id)).fetchone()
            if old:
                return self._event(c,row,event,client_action_id,request,{'event_id':uuid4().hex})
            latest = c.execute("SELECT * FROM writing_ocr_event WHERE confirmation_id=? AND kind IN ('started','ended') ORDER BY rowid DESC LIMIT 1",(confirmation_id,)).fetchone()
            if event=='ended':
                previous = json.loads(latest['payload_json']) if latest else {}
                require(latest and latest['kind']=='started' and previous.get('playback_id')==playback_id
                        and previous.get('runtime_epoch')==self.runtime_epoch,'READBACK_START_MISSING_OR_STALE')
            payload = dict(event_id=uuid4().hex,confirmation_id=confirmation_id,event=event,playback_id=playback_id,
                           runtime_epoch=self.runtime_epoch,readback_sha256=readback_sha256)
            result = self._event(c,row,event,client_action_id,request,payload)
            c.commit()
            return result

    def authorize_use(self,session_id,*,confirmation_id,binding_version,authorizing_turn_id,client_action_id):
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row,_,_ = _confirmation(c,session_id,confirmation_id,binding_version)
            request = dict(binding_version=binding_version,authorizing_turn_id=authorizing_turn_id)
            old = c.execute('SELECT * FROM writing_ocr_event WHERE child_id=? AND client_action_id=?',(row['child_id'],client_action_id)).fetchone()
            if old:
                return self._event(c,row,'authorize',client_action_id,request,{'event_id':uuid4().hex})
            _confirmation(c,session_id,confirmation_id,binding_version,live=True)
            readback = c.execute("SELECT * FROM writing_ocr_event WHERE confirmation_id=? AND kind IN ('started','ended') ORDER BY rowid DESC LIMIT 1",(confirmation_id,)).fetchone()
            require(readback and readback['kind']=='ended','COMPLETE_READBACK_REQUIRED')
            require(json.loads(readback['payload_json']).get('runtime_epoch') == self.runtime_epoch,
                    'READBACK_RUNTIME_CHANGED')
            turn = c.execute('SELECT action_json FROM agent_turn WHERE agent_turn_id=?',(authorizing_turn_id,)).fetchone()
            binding = json.loads(turn[0]).get('writing_ocr_binding') if turn else None
            require(isinstance(binding,dict),'OCR_AUTHORIZING_BINDING_REQUIRED')
            require(binding==arrival_binding(c,session_id=session_id,confirmation_id=confirmation_id,binding_version=binding_version,
                                             voice_input_id=binding.get('voice_input_id')),'OCR_AUTHORIZING_BINDING_CHANGED')
            turn = _turn(c,row,authorizing_turn_id,binding,live=True)
            voice = c.execute('SELECT created_at FROM voice_input WHERE voice_input_id=?',(binding['voice_input_id'],)).fetchone()
            captured = c.execute('SELECT created_at FROM voice_capture_binding WHERE voice_input_id=?',(binding['voice_input_id'],)).fetchone()
            require(datetime.fromisoformat(readback['created_at']) <= datetime.fromisoformat(captured[0]) <= datetime.fromisoformat(voice[0])
                    <= datetime.fromisoformat(turn['created_at']) <= _now(),'OCR_CONFIRMATION_PRECEDES_READBACK')
            require(not c.execute("SELECT 1 FROM writing_ocr_event WHERE confirmation_id=? AND kind='authorize'",(confirmation_id,)).fetchone(),
                    'OCR_ALREADY_AUTHORIZED')
            uid = uuid4().hex
            payload = dict(event_id=uid,use_authorization_id=uid,confirmation_id=confirmation_id,binding_version=binding_version,
                authorizing_turn_id=authorizing_turn_id,voice_binding=binding,readback_event_id=readback['event_id'],
                semantic_receipt_version=turn['semantic_receipt_version'],
                readback_version=digest(dict(readback)),
                source_version=row['source_version'],local_only=True,cloud_transmission_allowed=False)
            result = self._event(c,row,'authorize',client_action_id,request,payload)
            c.commit()
            return result

    def retract(self,session_id,*,confirmation_id,binding_version,client_action_id):
        # The authorization is part of the original written-answer proof.
        with self.database.external_effect_guard(timeout=2), self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('SELECT * FROM writing_ocr_confirmation WHERE confirmation_id=? AND session_id=?',(confirmation_id,session_id)).fetchone()
            require(row and row['binding_version']==binding_version,'OCR_CONFIRMATION_SCOPE_CHANGED')
            result = self._event(c,row,'retract',client_action_id,{'binding_version':binding_version},
                                 {'event_id':uuid4().hex,'confirmation_id':confirmation_id,'status':'withdrawn'})
            c.commit()
            return result


def resolve_written_document(c, *, child_id,session_id,prompt_id,item_version,artifact_id,revision_id,
                             confirmation_id,use_authorization_id):
    event = c.execute("SELECT * FROM writing_ocr_event WHERE event_id=? AND child_id=? AND confirmation_id=? AND kind='authorize'",
                      (use_authorization_id,child_id,confirmation_id)).fetchone()
    require(event,'OCR_USE_AUTHORIZATION_REQUIRED')
    value = json.loads(event['payload_json'])
    row,source,_ = _confirmation(c,session_id,confirmation_id,value['binding_version'])
    require((row['child_id'],row['prompt_id'],row['item_version'],row['artifact_id'],row['revision_id']) ==
            (child_id,prompt_id,item_version,artifact_id,revision_id),'OCR_AUTHORIZATION_SCOPE_CHANGED')
    require(datetime.fromisoformat(row['created_at']) <= datetime.fromisoformat(event['created_at'])
            < datetime.fromisoformat(row['expires_at']), 'OCR_CONFIRMATION_EXPIRED')
    turn = _turn(c,row,value['authorizing_turn_id'],value['voice_binding'],live=False)
    require(value.get('semantic_receipt_version') == turn['semantic_receipt_version'], 'OCR_SEMANTIC_RECEIPT_CHANGED')
    readback = c.execute("SELECT * FROM writing_ocr_event WHERE event_id=? AND confirmation_id=? AND kind='ended'",
                         (value['readback_event_id'],confirmation_id)).fetchone()
    require(readback and readback['created_at'] <= event['created_at'],'COMPLETE_READBACK_REQUIRED')
    require(value.get('readback_version') == digest(dict(readback)), 'READBACK_RECEIPT_CHANGED')
    return {'text':source['text'],'source_version':source['source_version'],
            'authorization_version':digest(dict(event)),'authorized_at':event['created_at']}
