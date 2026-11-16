"""Safety checks for an already authorized writing-body derivative, not raw OCR."""
import re
import json
import math

from .cloud_privacy import sanitize_cloud_text, sensitive_categories
from .document_learning import UNTRUSTED_INSTRUCTION, PRIVATE_CONTEXT, VISUAL_REQUIRED
from .writing_response_origin import require

VERSION = 'child_reviewed_writing_formative_text_v1'


def validate_read_ancestor(c, session_id, packet, audit):
    """Only a verified correction ancestor can differ from the final read ID."""
    from .writing_ocr_authorization import _confirmation
    final=c.execute('SELECT binding_version FROM writing_ocr_confirmation WHERE confirmation_id=?',
                    (audit.get('confirmation_id'),)).fetchone()
    require(final is not None, 'PAPER_REVIEW_LINEAGE_CHANGED')
    row,source,_=_confirmation(c,session_id,audit['confirmation_id'],final[0])
    require(source.get('writing_review_required') and all(row[k]==packet[k] for k in
        ('child_id','session_id','prompt_id','item_version','artifact_id','revision_id','response_start','response_end')),
        'PAPER_REVIEW_LINEAGE_CHANGED')
    # _confirmation revalidates the whole chain, including each literal edit,
    # original voice, full preceding readback, timestamps and retractions.
    seen=set()
    while row['confirmation_id'] != packet['confirmation_id']:
        require(row['confirmation_id'] not in seen, 'PAPER_REVIEW_LINEAGE_CHANGED')
        seen.add(row['confirmation_id'])
        saved=c.execute('SELECT review_json FROM writing_ocr_review WHERE confirmation_id=?',
                        (row['confirmation_id'],)).fetchone()
        previous=json.loads(saved[0]).get('previous') if saved else None
        require(previous, 'PAPER_REVIEW_LINEAGE_CHANGED')
        row,_,_=_confirmation(c,session_id,previous['confirmation_id'],previous['binding_version'])
    require(row['binding_version']==packet['binding_version'], 'PAPER_REVIEW_LINEAGE_CHANGED')


def validate_text(source, response, names, *, isolated_response):
    # The caller has resolved the writing-specific authorization and exact
    # review chain. Keep the entire original page in privacy checks, including
    # text outside the selected answer; never promote its OCR confidence.
    require(source['artifact_status'] in {'completed','quality_warning'}, 'PAPER_OCR_SOURCE_UNAVAILABLE')
    try:
        details=json.loads(source.get('details_json') or '{}')
    except (ValueError,TypeError):
        require(False, 'PAPER_QUALITY_REQUIRES_RECAPTURE_OR_SPECIALIST_REVIEW')
    require(isinstance(details,dict), 'PAPER_QUALITY_REQUIRES_RECAPTURE_OR_SPECIALIST_REVIEW')
    pipeline=details.get('local_pipeline',{})
    require(isinstance(pipeline,dict), 'PAPER_QUALITY_REQUIRES_RECAPTURE_OR_SPECIALIST_REVIEW')
    supported_cause=False
    for container,key in ((details.get('quality'),'issues'),(pipeline.get('quality'),'issues'),
                          (details.get('manual_review'),'reasons'),(pipeline.get('manual_review'),'reasons')):
        if container is None:
            continue
        require(isinstance(container,dict), 'PAPER_QUALITY_REQUIRES_RECAPTURE_OR_SPECIALIST_REVIEW')
        issues=container.get(key,[])
        # Full text readback can establish the child's transcription despite
        # glare/low confidence. It cannot establish orientation, formulas,
        # missing/cropped content or any unknown future quality capability.
        allowed={'glare','low_ocr_confidence'}
        if key=='reasons':
            allowed.add('missing_question_start')
        require(isinstance(issues,list) and all(isinstance(issue,str) and issue in allowed for issue in issues),
                'PAPER_QUALITY_REQUIRES_RECAPTURE_OR_SPECIALIST_REVIEW')
        flag=container.get('passed' if key=='issues' else 'required')
        require(type(flag) is bool and bool(issues)==(not flag if key=='issues' else flag),
                'PAPER_QUALITY_REQUIRES_RECAPTURE_OR_SPECIALIST_REVIEW')
        if key=='issues' and 'glare' in issues:
            supported_cause=True
        if 'low_ocr_confidence' in issues:
            confidence=(source.get('ocr_confidence'),source.get('artifact_confidence'))
            require(all(type(value) in (int,float) and math.isfinite(value) and 0 <= value <= 1
                        for value in confidence) and min(confidence) < .78,
                    'PAPER_QUALITY_REQUIRES_RECAPTURE_OR_SPECIALIST_REVIEW')
            supported_cause=True
    if not supported_cause and source['artifact_status']=='completed' and source.get('review_required')==0:
        from .document_learning import DocumentLearningService
        rules=object.__new__(DocumentLearningService)
        checked,lines,_=rules._local(source,isolated_response=isolated_response)
        supported_cause=bool(checked.get('source')=='local_ocr_rules' and not checked.get('reason') and lines)
    require(supported_cause, 'PAPER_QUALITY_REQUIRES_RECAPTURE_OR_SPECIALIST_REVIEW')
    for text in (source['text'], response):
        require(isinstance(text,str) and 0 < len(text) <= 12000
                and len(re.findall(r'[\w\u4e00-\u9fff]',text)) >= 6,
                'PAPER_OCR_INSUFFICIENT_NEED_CLEARER_PAGE_OR_VISUAL_CAPABILITY')
        require(not UNTRUSTED_INSTRUCTION.search(text), 'PAPER_UNTRUSTED_PAGE_INSTRUCTION_BLOCKED')
        require(not sensitive_categories(text) and not PRIVATE_CONTEXT.search(text)
                and sanitize_cloud_text(text,names).text == text, 'PAPER_PRIVATE_TEXT')
        require(isolated_response or not VISUAL_REQUIRED.search(text), 'PAPER_TEXT_REVIEW_VISUAL_DEPENDENCY')
    return VERSION
