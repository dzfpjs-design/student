"""Local writing review, never a promotion of document or Tutor OCR quality."""
import hashlib
import json
import re
from datetime import datetime

from .local_supply import digest
from .writing_response_origin import require


def initialize(c):
    c.execute('''CREATE TABLE IF NOT EXISTS writing_ocr_review(
        confirmation_id TEXT PRIMARY KEY REFERENCES writing_ocr_confirmation(confirmation_id) ON DELETE CASCADE,
        review_json TEXT NOT NULL)''')


def current_page(c, row):
    require(c.execute('''SELECT 1 FROM document_page_revision r
        JOIN document_monitor_session m USING(monitor_id)
        WHERE r.revision_id=? AND r.artifact_id=? AND r.child_id=? AND r.session_id=?
        AND m.child_id=r.child_id AND m.session_id=r.session_id AND m.status='active'
        AND r.revision_index=(SELECT MAX(r2.revision_index) FROM document_page_revision r2
                             WHERE r2.monitor_id=r.monitor_id)''',
        (row['revision_id'], row['artifact_id'], row['child_id'], row['session_id'])).fetchone(),
        'OCR_REVIEW_PAGE_CHANGED')


def correction_words(transcript):
    return re.fullmatch(r'识别更正[，,\s]*把[“「"](.+?)[”」"]改成[“「"](.+?)[”」"][。！!\s]*', transcript)


def corrected_text(text, transcript, *, edit=None):
    words = correction_words(transcript)
    old,new = edit if edit else ((words[1],words[2]) if words else (None,None))
    require(isinstance(old,str) and isinstance(new,str) and text.count(old) == 1 and old != new, 'OCR_CORRECTION_UNIQUE_ANCHOR_REQUIRED')
    result = text.replace(old, new, 1)
    require(result.strip() and len(result) <= 12000, 'DOCUMENT_SPAN_INVALID')
    return result


def readback(question, response, *, version=1):
    if version == 2:
        return (f'这页识别需要核对，不确定的地方请重拍。\n这道题是：{question}\n'
                f'待核对的完整书面作答是：{response}\n'
                '有字读错，请告诉我读错的是哪一段、纸上实际写的是什么。我会更正后重新读全文。'
                '请核对这是不是你自己写的本题作答，读出来的文字有没有错。仅说明作答范围还不能提交。')
    require(version == 1, 'OCR_READBACK_VERSION_INVALID')
    return (f'这页识别需要核对，不确定的地方请重拍。\n这道题是：{question}\n'
            f'待核对的完整书面作答是：{response}\n'
            '有错可以说：识别更正，把“读错的原文”改成“正确原文”。我会重新读完整稿。'
            '核对无误后，请明确说这是你自己写的本题答案，而且识别文字正确；仅确认作答范围还不能提交。')


def explicit_body_confirmation(transcript):
    return bool(re.search(r'(?:我|自己).*(?:写|作答|答案)', transcript)
                and re.search(r'识别(?:文字)?(?:得)?(?:没有错|没错|正确|准确)', transcript))


def uncertain_confirmation(transcript):
    return bool(re.search(r'如果|假如|也许|可能|不确定|不知道|看不清|不是我|别人|他写|她写|不准确|不对|有错误|还要改|需要改|先别|不要|别改|不是让你改|只是举例|举个例',transcript)
        or re.fullmatch(r'(?:这|这一|整)页(?:上)?(?:只有|全是|都是)(?:我|我自己)(?:写的)?(?:答案|作答|作文)[。！!\s]*',transcript))


def ready_to_confirm(c, session_id, binding, transcript, semantic=None):
    from .writing_ocr_authorization import _confirmation
    row, original, _ = _confirmation(c, session_id, binding['confirmation_id'], binding['binding_version'], live=True)
    if not original.get('writing_review_required'):
        return True
    event = c.execute("SELECT * FROM writing_ocr_event WHERE confirmation_id=? AND kind IN ('started','ended') ORDER BY rowid DESC LIMIT 1",
                      (row['confirmation_id'],)).fetchone()
    captured = c.execute('SELECT created_at FROM voice_capture_binding WHERE voice_input_id=?',
                         (binding['voice_input_id'],)).fetchone()
    accepted = explicit_body_confirmation(transcript)
    if semantic:
        receipt = c.execute('SELECT * FROM voice_control_intent_request WHERE request_key=?', (semantic['request_key'],)).fetchone()
        fingerprint = hashlib.sha256(json.dumps([binding['child_id'],session_id,transcript],ensure_ascii=False).encode()).hexdigest()
        accepted = bool(receipt and receipt['status']=='completed' and receipt['fingerprint']==fingerprint
            and json.loads(receipt['decision_json']).get('control')=='writing.confirm_original'
            and json.loads(receipt['binding_json']).get('writing_ocr')==binding)
    return bool(accepted and not uncertain_confirmation(transcript) and event and event['kind'] == 'ended'
                and captured and datetime.fromisoformat(event['created_at']) <= datetime.fromisoformat(captured[0]))


def source(c, row, *, live, seen):
    from .writing_ocr_authorization import _confirmation, _document, _voice
    record = c.execute('SELECT review_json FROM writing_ocr_review WHERE confirmation_id=?',
                       (row['confirmation_id'],)).fetchone()
    if not record:
        return None
    require(row['confirmation_id'] not in seen, 'OCR_REVIEW_CYCLE')
    seen = {*seen, row['confirmation_id']}
    review = json.loads(record[0])
    raw = _document(c, row['child_id'], row['session_id'], row['artifact_id'], row['revision_id'],
                    row['response_start'], row['response_end'], review=True)
    require(review['raw_source_version'] == raw['source_version'], 'OCR_CONFIRMATION_SOURCE_CHANGED')
    expected = raw['text']
    if review.get('previous'):
        previous = review['previous']
        parent, parent_source, _ = _confirmation(c, row['session_id'], previous['confirmation_id'],
                                                previous['binding_version'], _seen=seen)
        require(all(parent[k] == row[k] for k in ('child_id','session_id','prompt_id','item_version',
                    'artifact_id','revision_id','response_start','response_end')), 'OCR_CORRECTION_SCOPE_CHANGED')
        binding = review['voice_binding']
        capture = _voice(c, row['session_id'], row['child_id'], binding['voice_input_id'], review['transcript'])
        require(binding == {**previous, 'child_id': row['child_id'], 'session_id': row['session_id'],
            'prompt_id': row['prompt_id'], 'item_version': row['item_version'], 'artifact_id': row['artifact_id'],
            'revision_id': row['revision_id'], 'voice_input_id': binding['voice_input_id'],
            'voice_version': digest({k:v for k,v in capture.items() if k != 'raw_audio_available'})}
            and capture['question_validation'] == 'valid' and capture['prompt_id'] == row['prompt_id']
            and capture['item_version'] == row['item_version'], 'OCR_CORRECTION_VOICE_CHANGED')
        event = c.execute("SELECT * FROM writing_ocr_event WHERE event_id=? AND confirmation_id=? AND kind='ended'",
                          (review['readback_event_id'], parent['confirmation_id'])).fetchone()
        require(event and json.loads(event['payload_json']).get('readback_sha256') == parent['readback_sha256']
            and datetime.fromisoformat(event['created_at']) <= datetime.fromisoformat(capture['capture_source']['created_at'])
            <= datetime.fromisoformat(row['created_at']), 'OCR_CORRECTION_READBACK_CHANGED')
        edit = None
        if review.get('semantic_receipt'):
            from .writing_review_intent import semantic_edit
            old,new,version=semantic_edit(c,binding,review['transcript'],review['semantic_receipt'])
            require(version==review.get('semantic_receipt_version'),'OCR_CORRECTION_SEMANTIC_CHANGED')
            edit=(old,new)
        expected = corrected_text(parent_source['text'], review['transcript'], edit=edit)
    require(review['text'] == expected, 'OCR_CORRECTION_TEXT_CHANGED')
    version = digest(['writing_ocr_review_v1', review])
    require(row['source_version'] == version, 'OCR_CONFIRMATION_SOURCE_CHANGED')
    if live:
        current_page(c, row)
        require(not c.execute('''SELECT 1 FROM writing_ocr_review
            WHERE json_extract(review_json,'$.previous.confirmation_id')=?''',
            (row['confirmation_id'],)).fetchone(), 'OCR_REVIEW_SUPERSEDED')
    return {'text': expected, 'source_version': version, 'writing_review_required': True,
            'readback_version':review.get('readback_version',1)}


def prepare_correction(service, c, session_id, binding, transcript, key, semantic=None):
    from datetime import timedelta
    from uuid import uuid4
    from .writing_ocr_authorization import _confirmation, _now, _prompt, arrival_binding, _document
    require(isinstance(key, str) and 1 <= len(key) <= 200, 'ACTION_REQUIRED')
    request = digest([session_id, binding, transcript])
    if semantic:
        request = digest([request,semantic['request_key']])
    old = c.execute('SELECT * FROM writing_ocr_confirmation WHERE session_id=? AND client_action_id=?',
                    (session_id, key)).fetchone()
    if old:
        require(old['request_hash'] == request, 'ACTION_CONFLICT')
        return service._result(c, old)
    row, previous_source, _ = _confirmation(c, session_id, binding['confirmation_id'], binding['binding_version'], live=True)
    require(binding == arrival_binding(c, session_id=session_id, confirmation_id=row['confirmation_id'],
        binding_version=row['binding_version'], voice_input_id=binding['voice_input_id']), 'OCR_CORRECTION_SCOPE_CHANGED')
    require(not c.execute("SELECT 1 FROM writing_ocr_event WHERE confirmation_id=? AND kind='authorize'",
                         (row['confirmation_id'],)).fetchone(), 'OCR_CORRECTION_ALREADY_AUTHORIZED')
    current_page(c, row)
    event = c.execute("SELECT * FROM writing_ocr_event WHERE confirmation_id=? AND kind IN ('started','ended') ORDER BY rowid DESC LIMIT 1",
                      (row['confirmation_id'],)).fetchone()
    require(event and event['kind'] == 'ended', 'COMPLETE_READBACK_REQUIRED')
    capture = c.execute('SELECT created_at FROM voice_capture_binding WHERE voice_input_id=?',
                        (binding['voice_input_id'],)).fetchone()
    require(capture and datetime.fromisoformat(event['created_at']) <= datetime.fromisoformat(capture[0]),
            'OCR_CORRECTION_READBACK_CHANGED')
    raw = _document(c, row['child_id'], session_id, row['artifact_id'], row['revision_id'],
                    row['response_start'], row['response_end'], review=True)
    edit = None
    if semantic:
        from .writing_review_intent import semantic_edit
        reference={'request_key':semantic['request_key']}
        old,new,semantic_version=semantic_edit(c,binding,transcript,reference)
        edit=(old,new)
    text = corrected_text(previous_source['text'], transcript, edit=edit)
    review = dict(raw_source_version=raw['source_version'], text=text, transcript=transcript, readback_version=2,
        previous={k:row[k] for k in ('confirmation_id','binding_version')},
        voice_binding=binding, readback_event_id=event['event_id'])
    if semantic:
        review.update(semantic_receipt=reference,semantic_receipt_version=semantic_version)
    _, item = _prompt(c, session_id, row['prompt_id'], row['item_version'], live=True)
    values = list(row)
    values[0], values[12], values[13], values[14], values[15] = uuid4().hex, _now().isoformat(), (_now()+timedelta(minutes=15)).isoformat(), key, request
    values[11] = hashlib.sha256(readback(item['prompt'], text, version=2).encode()).hexdigest()
    # SQL column positions: source=9, binding=10, readback=11.
    values[9] = digest(['writing_ocr_review_v1', review])
    from .writing_ocr_authorization import VERSION
    values[10] = digest([VERSION, values[0], request, values[9]])
    c.execute('INSERT INTO writing_ocr_confirmation VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', values)
    c.execute('INSERT INTO writing_ocr_review VALUES(?,?)', (values[0], json.dumps(review, ensure_ascii=False, sort_keys=True)))
    return service._result(c, c.execute('SELECT * FROM writing_ocr_confirmation WHERE confirmation_id=?', (values[0],)).fetchone())
