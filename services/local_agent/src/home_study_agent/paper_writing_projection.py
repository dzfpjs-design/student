"""Purpose-specific text derivative; original page/use records stay local-only."""
import json
from datetime import datetime, timezone

from .cloud_privacy import sanitize_cloud_text
from .document_learning import DocumentLearningService, VERSION as OCR_TEXT_POLICY_VERSION
from .local_supply import digest
from .learning_obligations import withdrawn_evidence_ids
from .writing_response_origin import _prompt, original_written_response, require
from .writing_ocr_authorization import _confirmation, resolve_written_document

VERSION = 'confirmed_writing_formative_text_v1'
MAX_RESPONSE_CHARS = 3000
OCR_METADATA = frozenset({'lines','engine','ocr_confidence','quality','monitor_generation',
    'monitor_frame_sha256','manual_review','local_pipeline','cloud_vision_fallback'})


def _metadata(value):
    if isinstance(value,list):
        for entry in value:
            _metadata(entry)
    elif isinstance(value,dict):
        require(value.get('local_only') is not True and value.get('cloud_transmission_allowed') is not False,
                'PAPER_RESTRICTED_SOURCE')
        for key,entry in value.items():
            require(not (entry and key in {'knowledge_context','atomic_evidence','document_text','original_page',
                'source_ref','source_id','knowledge_source_id','pdf','pdf_path','family_context','raw_audio','raw_video'}),
                'PAPER_RESTRICTED_SOURCE')
            _metadata(entry)


def project_confirmed_writing_text(c, *, child_id,session_id,prompt_id,item_version,
                                   attempt_id,writing_original_artifact_id):
    """Read-only DTO. Only response may feed the final three-field projection."""
    scope=dict(child_id=child_id,session_id=session_id,prompt_id=prompt_id,item_version=item_version,
        attempt_id=attempt_id,writing_original_artifact_id=writing_original_artifact_id)
    if not c.in_transaction:
        c.execute('BEGIN')
        try:
            return project_confirmed_writing_text(c,**scope)
        finally:
            c.rollback()
    base=dict(status='local_required',reason=None,response=None,local_only=True,
        cloud_transmission_allowed=False,automatic_certification=False)
    try:
        prompt,item=_prompt(c,session_id,prompt_id,item_version,live=False)
        require(prompt['child_id']==child_id and prompt['status']=='answered' and prompt['attempt_id']==attempt_id
            and prompt['session_status']=='active' and prompt['session_state'] not in {'evolving','completed'},
            'PAPER_ATTEMPT_SCOPE_CHANGED')
        preference=c.execute('SELECT * FROM privacy_preference WHERE singleton_id=1').fetchone()
        consent=c.execute("SELECT * FROM consent_record WHERE scope='cloud_model_text_allowed' ORDER BY rowid DESC LIMIT 1").fetchone()
        require(preference and preference['cloud_model_text_allowed']==1 and consent and consent['allowed']==1
            and consent['source']=='parent_ui' and consent['policy_version']=='privacy_v1', 'PAPER_PARENT_TEXT_CONSENT_REQUIRED')
        consent_at=datetime.fromisoformat(consent['occurred_at'])
        require(consent_at.tzinfo and consent_at<=datetime.now(timezone.utc),'PAPER_CONSENT_TIME_INVALID')
        attempt=c.execute('SELECT * FROM diagnostic_attempt WHERE attempt_id=? AND child_id=? AND session_id=?',
            (attempt_id,child_id,session_id)).fetchone()
        require(attempt,'PAPER_ATTEMPT_SCOPE_CHANGED')
        require(attempt['evidence_id'] not in withdrawn_evidence_ids(c,child_id),'PAPER_EVIDENCE_WITHDRAWN')
        original=original_written_response(c,prompt,attempt)
        require(original['kind']=='confirmed_document' and original['artifact_id']==writing_original_artifact_id,
                'PAPER_ORIGINAL_SCOPE_CHANGED')
        artifact=c.execute('SELECT * FROM capture_artifact WHERE artifact_id=?',(writing_original_artifact_id,)).fetchone()
        origin=json.loads(artifact['details_json'])['origin']
        document=resolve_written_document(c,child_id=child_id,session_id=session_id,prompt_id=prompt_id,item_version=item_version,
            **{k:origin[k] for k in ('artifact_id','revision_id','confirmation_id','use_authorization_id')})
        packet,reviewed_source,_=_confirmation(c,session_id,origin['confirmation_id'],
            c.execute('SELECT binding_version FROM writing_ocr_confirmation WHERE confirmation_id=?',(origin['confirmation_id'],)).fetchone()[0])
        require(0<len(document['text'])<=MAX_RESPONSE_CHARS,'PAPER_RESPONSE_TOO_LARGE')
        # These two methods only read/transform their input. Avoid the service
        # constructor, which initializes its independent analysis work queue.
        rules=object.__new__(DocumentLearningService)
        source=rules._load(c,session_id,origin['revision_id'])
        require(source['child_id']==child_id and source['artifact_id']==origin['artifact_id'], 'PAPER_SOURCE_CHANGED')
        details=json.loads(source['details_json'])
        require(isinstance(details,dict) and set(details)<=OCR_METADATA|{'visual_capture','visual_materials','page_source'}, 'PAPER_SOURCE_METADATA_UNSUPPORTED')
        sidecar_policy = None
        region_policy = None
        page_source_policy = None
        writing_review = ({k:origin[k] for k in ('confirmation_id','use_authorization_id')}
                          | {'prompt_id':prompt_id,'item_version':item_version}) if reviewed_source.get('writing_review_required') else None
        if 'visual_materials' in details or isinstance(details.get('visual_capture'),dict) and details['visual_capture'].get('status')=='prepared':
            from .paper_text_region_projection import project_region
            from .document_learning import VISUAL_REQUIRED
            require(not item.get('stimulus_asset') and not VISUAL_REQUIRED.search(
                item['prompt']+'\n'+str(item.get('stimulus_text') or '')+'\n'+document['text']), 'PAPER_TEXT_REGION_VISUAL_DEPENDENCY')
            region_text=(source['text'][packet['response_start']:packet['response_end']]
                         if reviewed_source.get('writing_review_required') else document['text'])
            region_policy=project_region(c,source=source,details=details,confirmation=packet,response=region_text,
                allow_quality_warning=writing_review is not None,writing_review=writing_review)
        elif 'visual_capture' in details:
            from .paper_ocr_metadata import validate_visual_capture_sidecar
            sidecar_policy = validate_visual_capture_sidecar(c,source,details,
                allow_quality_warning=writing_review is not None,writing_review=writing_review)
        if 'page_source' in details:
            from .paper_ocr_metadata import validate_page_source_sidecar
            page_source_policy = validate_page_source_sidecar(source, details)
        # The typed visual compartment remains local-only in the original and
        # source hash. Its flags cannot authorize images/facts or other metadata.
        for key,value in details.items():
            if key not in {'visual_capture','visual_materials','page_source'}:
                _metadata({key:value})
        names=[r[0] for r in c.execute('SELECT nickname FROM child_profile')]
        review_policy = None
        if reviewed_source.get('writing_review_required'):
            from .paper_writing_review_projection import validate_text
            review_policy=validate_text(source,document['text'],names,isolated_response=region_policy is not None)
        else:
            checked,lines,_=rules._local(source,isolated_response=region_policy is not None)
            require(checked.get('source')=='local_ocr_rules' and not checked.get('reason') and lines,
                'PAPER_'+str(checked.get('reason') or 'TEXT_RULES_UNAVAILABLE').upper())
        require(sanitize_cloud_text(source['text'],names).text==source['text'], 'PAPER_PRIVATE_TEXT')
        require(document['text']==attempt['submitted_answer'], 'PAPER_RESPONSE_CHANGED')
        audit={**scope,'version':VERSION,'purpose':'formative_feedback',
            **{k:packet[k] for k in ('artifact_id','revision_id','confirmation_id','response_start','response_end')},
            'use_authorization_id':origin['use_authorization_id'],'source_version':document['source_version'],
            'authorization_version':document['authorization_version'],'ocr_text_policy_version':OCR_TEXT_POLICY_VERSION,
            'ocr_source_hash':source['source_hash'],
            'consent_version':digest({'preference':dict(preference),'consent':dict(consent)}),
            'response_hash':digest(document['text']),'local_only':True,'cloud_transmission_allowed':False}
        if sidecar_policy is not None:
            audit['ocr_sidecar_policy_version']=sidecar_policy
        if region_policy is not None:
            audit['text_region_projection']=region_policy
        if page_source_policy is not None:
            audit['page_source_policy_version']=page_source_policy
        if review_policy is not None:
            audit['writing_body_review_policy']=review_policy
        return {**base,'status':'ready_for_runtime_checks','response':document['text'],
            'projection_version':digest(audit),'local_audit':audit}
    except ValueError as error:
        if str(error).startswith('WRITING_'):
            return {**base,'reason':str(error)}
        raise
