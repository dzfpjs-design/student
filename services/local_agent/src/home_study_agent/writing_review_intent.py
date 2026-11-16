"""Current-utterance literal roles only; page text and history never leave host."""
import asyncio
import hashlib
import json
import math
import re

from .cloud_privacy import sanitize_cloud_text, sensitive_categories
from .local_supply import digest
from .model_gateway import ModelRequest
from .repository import utc_now
from .writing_response_origin import require

VERSION = 'writing_review_literal_intent_v1'


def project(transcript, names, local_sources):
    if not 0 < len(transcript) <= 500 or sensitive_categories(transcript) or sanitize_cloud_text(transcript,names).text != transcript:
        return None
    spans = [(m.start(),m.end()) for m in re.finditer(r"[A-Za-z0-9][A-Za-z0-9'-]*|[“「\"]([^”」\"]+)[”」\"]",transcript)]
    for source in local_sources:
        source = str(source or '')
        for index in range(len(transcript)-7):
            if transcript[index:index+8] in source:
                spans.append((index,index+8))
    merged=[]
    for start,end in sorted(spans):
        if merged and start < merged[-1][1]:
            merged[-1]=(merged[-1][0],max(end,merged[-1][1]))
        else:
            merged.append((start,end))
    text, boundaries, cursor = '', {0:0}, 0
    for index,(start,end) in enumerate(merged):
        while cursor < start:
            text += transcript[cursor]; cursor += 1; boundaries[len(text)] = cursor
        boundaries[len(text)] = start
        text += f'[片段{index}]'; boundaries[len(text)] = end; cursor = end
    while cursor < len(transcript):
        text += transcript[cursor]; cursor += 1; boundaries[len(text)] = cursor
    return text,boundaries


def parse(content, transcript, projection, source_text):
    text,boundaries = projection
    value=json.loads(content)
    require(isinstance(value,dict) and set(value)=={'role','quote','confidence','old_span','new_span'}
        and value['quote']==text and type(value['confidence']) in (int,float)
        and math.isfinite(value['confidence']) and 0 <= value['confidence'] <= 1, 'OCR_CORRECTION_SEMANTIC_INVALID')
    if value['role'] != 'correction' or value['confidence'] < .9:
        return {'status':'clarify','reason':'WRITING_CORRECTION_AMBIGUOUS'}
    require(not re.search(r'如果|假如|也许|可能|不确定|还没想好|别改|先别|不要|不用改|不想改|不是让你改|只是举例|举个例|他说|她说|老师说',transcript),
            'OCR_CORRECTION_NOT_REQUESTED')
    result={'status':'control','control':'writing.correct_ocr'}
    literals=[]
    for field in ('old_span','new_span'):
        span=value[field]
        require(isinstance(span,list) and len(span)==2 and all(type(v) is int for v in span)
                and span[0] in boundaries and span[1] in boundaries and 0 <= span[0] < span[1] <= len(text),
                'OCR_CORRECTION_LITERAL_SPAN_REQUIRED')
        a,b=boundaries[span[0]],boundaries[span[1]]
        literal=transcript[a:b]
        # Quotation marks are delimiters supplied by the child, not inferred text.
        if len(literal)>1 and literal[0] in '“「"' and literal[-1] in '”」"':
            a,b=a+1,b-1;literal=transcript[a:b]
        require(0 < len(literal) <= 160, 'OCR_CORRECTION_LITERAL_SPAN_REQUIRED')
        result[field]=[a,b];literals.append(literal)
    require(literals[0] != literals[1] and source_text.count(literals[0]) == 1, 'OCR_CORRECTION_UNIQUE_ANCHOR_REQUIRED')
    return result


def semantic_edit(c, binding, transcript, reference):
    require(isinstance(reference,dict) and set(reference)=={'request_key'}, 'OCR_CORRECTION_SEMANTIC_REQUIRED')
    row=c.execute('SELECT * FROM voice_control_intent_request WHERE request_key=?', (reference['request_key'],)).fetchone()
    fingerprint=hashlib.sha256(json.dumps([binding['child_id'],binding['session_id'],transcript],ensure_ascii=False).encode()).hexdigest()
    require(row and row['status']=='completed' and row['fingerprint']==fingerprint
        and (row['child_id'],row['session_id'])==(binding['child_id'],binding['session_id'])
        and json.loads(row['binding_json'])=={'writing_ocr':binding,'writing_review_policy':VERSION},
        'OCR_CORRECTION_SEMANTIC_CHANGED')
    decision=json.loads(row['decision_json'])
    require(decision.get('status')=='control' and decision.get('control')=='writing.correct_ocr', 'OCR_CORRECTION_SEMANTIC_CHANGED')
    values=[]
    for key in ('old_span','new_span'):
        span=decision.get(key)
        require(isinstance(span,list) and len(span)==2 and all(type(v) is int for v in span)
            and 0 <= span[0] < span[1] <= len(transcript), 'OCR_CORRECTION_LITERAL_SPAN_REQUIRED')
        values.append(transcript[span[0]:span[1]])
    version=digest({k:row[k] for k in ('request_key','child_id','session_id','fingerprint','binding_json','status','decision_json','created_at','completed_at')})
    return (*values,version)


async def resolve(owner, *, session_id, child_id, action_id, transcript, binding, voice_capture,
                  names, local_sources, allowed, pre_dispatch):
    from .writing_ocr_authorization import _confirmation, arrival_binding
    projection=project(transcript,names,local_sources)
    if not allowed or not projection or not voice_capture or not getattr(owner.gateway,'configured',False):
        return {'status':'unavailable','reason':'WRITING_CORRECTION_LOCAL_ONLY'}
    key=digest(['writing-review-intent',action_id])
    fingerprint=hashlib.sha256(json.dumps([child_id,session_id,transcript],ensure_ascii=False).encode()).hexdigest()
    saved_binding={'writing_ocr':binding,'writing_review_policy':VERSION}
    try:
        with owner.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            consent=owner.text_consent_version(c)
            owner.assert_current_input(c,session_id,child_id,action_id,transcript,voice_capture)
            _,source,_=_confirmation(c,session_id,binding['confirmation_id'],binding['binding_version'],live=True)
            old=c.execute('SELECT * FROM voice_control_intent_request WHERE request_key=?',(key,)).fetchone()
            if old:
                require(old['fingerprint']==fingerprint and json.loads(old['binding_json'])==saved_binding,'OCR_CORRECTION_ACTION_CONFLICT')
                return {**json.loads(old['decision_json'] or '{"status":"unavailable"}'),'request_key':key}
            c.execute('''INSERT INTO voice_control_intent_request
                (request_key,child_id,session_id,fingerprint,binding_json,status,created_at)
                VALUES(?,?,?,?,?,'pending',?)''',(key,child_id,session_id,fingerprint,json.dumps(saved_binding),utc_now()))
            c.commit()
    except ValueError as error:
        if not str(error).startswith(('WRITING_','CONTROL_')):
            raise
        return {'status':'unavailable','reason':'WRITING_CORRECTION_SCOPE_CHANGED'}
    def check(c):
        if pre_dispatch:
            pre_dispatch(c)
        require(owner.text_consent_version(c)==consent,'OCR_CORRECTION_CONSENT_CHANGED')
        owner.assert_current_input(c,session_id,child_id,action_id,transcript,voice_capture)
        require(arrival_binding(c,session_id=session_id,confirmation_id=binding['confirmation_id'],
            binding_version=binding['binding_version'],voice_input_id=voice_capture['voice_input_id'])==binding,
            'OCR_CORRECTION_SCOPE_CHANGED')
        journal=c.execute('SELECT * FROM voice_control_intent_request WHERE request_key=?',(key,)).fetchone()
        require(journal and journal['status']=='pending' and journal['fingerprint']==fingerprint
            and json.loads(journal['binding_json'])==saved_binding, 'OCR_CORRECTION_SEMANTIC_CHANGED')
    def authorize():
        with owner.database.connect() as c:
            c.execute('BEGIN');check(c)
    messages=({'role':'system','content':
        '只解析当前一句是否明确纠正刚读回的文字，不回答、不补写。片段占位符是本机隐藏的原话，不能猜内容。'
        '输出且仅输出role(correction|ambiguous|other)、quote(逐字复制当前投影句)、confidence(0到1)、'
        'old_span和new_span。明确纠错时，两项是当前投影句中零起点Unicode字符[start,end]范围；'
        'old是被读错的文字，new是孩子明确说出的替代文字。不是X是Y表示old=X,new=Y；'
        '把Y读成X了应该是Y表示old=X,new=Y。占位符须完整选中；不得推断缺失原文或补全正文。'
        '条件、引用、否定更改、多处不明确、只说不对或未给替代文字均ambiguous，两项null。'
        '只确认、请求重读、学习答案或其他命令用other，两项null。'},
        {'role':'user','content':json.dumps({'transcript':projection[0]},ensure_ascii=False)})
    manifest={'lane':VERSION,'local_sources_included':False,'history_included':False,
        'literal_masking':'latin_quoted_and_local_eight_character_overlap',
        'all_literal_text_masked':False,'only_filtered_current_utterance':True}
    call=owner.repository.start_model_call(session_id,child_id,'voice_interaction',owner.settings.model_provider,
        owner.settings.voice_intent_model_name or owner.settings.model_name,sum(len(m['content']) for m in messages),
        VERSION,manifest,'low',owner.settings.model_provider,VERSION,None)
    request=ModelRequest(request_id=call,child_id=child_id,session_id=session_id,request_type='voice_interaction',
        risk_level='low',context_manifest=manifest,provider_preference=owner.settings.model_provider,
        policy_version=VERSION,prompt_version=VERSION,knowledge_version=None,output_type='dialogue',
        evidence_refs=(),messages=messages,pre_dispatch=authorize)
    result=None;decision={'status':'clarify','reason':'WRITING_CORRECTION_NOT_UNAMBIGUOUS'}
    try:
        authorize();result=await asyncio.wait_for(owner.gateway.complete(request),timeout=8)
        decision=parse(result.content,transcript,projection,source['text'])
    except Exception:
        pass
    with owner.database.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        try:
            check(c)
        except ValueError:
            decision={'status':'unavailable','reason':'WRITING_CORRECTION_SCOPE_CHANGED'}
        c.execute("UPDATE voice_control_intent_request SET status='completed',decision_json=?,completed_at=? WHERE request_key=? AND status='pending'",
                  (json.dumps(decision),utc_now(),key));c.commit()
    owner.repository.finish_model_call(call,'completed' if decision['status']=='control' else 'failed',
        result.latency_ms if result else None,len(result.content) if result else 0,error_code=decision.get('reason'),
        verification_status='verified' if decision['status']=='control' else 'rejected',safety_status='passed')
    return {**decision,'request_key':key}
