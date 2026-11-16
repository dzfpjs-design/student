"""Recognize one local capture compartment, never authorize its visual content."""
import json
import re

from .writing_response_origin import require

VERSION = 'paper_ocr_visual_sidecar_v1'
_FIELDS = frozenset({'version','status','reason','candidate_count','expires_at',
    'retention_policy_id','local_only','cloud_transmission_allowed'})
_PAGE_SOURCE_FIELDS = frozenset({'version','status','sha256','byte_length','media_type',
    'encrypted','local_only','cloud_transmission_allowed','retention_reason'})


def validate_visual_capture_sidecar(c, source, details, *, allow_quality_warning=False, writing_review=None):
    """Borrow the caller's authorized OCR snapshot; no connection or mutation.

    Unknown/prepared/restricted visual states remain blocked. A recognized empty
    detector result is not proof a page has no image, nor permission to upload it.
    The caller must still validate the entire OCR text and its confirmed span.
    """
    require(c.in_transaction, 'PAPER_METADATA_TRANSACTION_REQUIRED')
    value = details.get('visual_capture')
    require(isinstance(value,dict) and set(value)==_FIELDS, 'PAPER_VISUAL_METADATA_UNSUPPORTED')
    require(value['version']=='visual_capture_candidates_v1'
        and value['local_only'] is True and value['cloud_transmission_allowed'] is False,
        'PAPER_VISUAL_METADATA_UNSUPPORTED')
    require(value['status']=='unsupported' and value['reason']=='VISUAL_NO_SUPPORTED_GEOMETRY'
        and type(value['candidate_count']) is int and value['candidate_count']==0
        and value['expires_at'] is None, 'PAPER_VISUAL_METADATA_RESTRICTED')
    validate_retention_reference(c,source,value['retention_policy_id'])
    validate_capture_identity(c, source, details, allow_quality_warning=allow_quality_warning, writing_review=writing_review)
    return VERSION


def validate_page_source_sidecar(source, details):
    """Validate retained local provenance without authorizing image upload."""
    value = details.get('page_source')
    require(isinstance(value, dict) and set(value) == _PAGE_SOURCE_FIELDS,
            'PAPER_PAGE_SOURCE_METADATA_UNSUPPORTED')
    require(value['version'] == 'document_page_source_v1'
        and value['status'] == 'retained'
        and value['media_type'] in {'image/jpeg', 'image/png'}
        and value['encrypted'] is True
        and value['local_only'] is True
        and value['cloud_transmission_allowed'] is False
        and value['retention_reason'] == 'accepted_document_page_revision',
        'PAPER_PAGE_SOURCE_METADATA_UNSUPPORTED')
    frame = details.get('monitor_frame_sha256')
    require(isinstance(frame, str) and re.fullmatch('[0-9a-f]{64}', frame)
        and value['sha256'] == frame
        and type(value['byte_length']) is int and value['byte_length'] > 0,
        'PAPER_PAGE_SOURCE_CHANGED')
    require(source.get('artifact_type') == 'document_ocr', 'PAPER_PAGE_SOURCE_CHANGED')
    return 'paper_page_source_sidecar_v1'


def validate_retention_reference(c, source, retention):
    require(retention is None or (isinstance(retention,str) and 0<len(retention)<=128),
        'PAPER_VISUAL_METADATA_UNSUPPORTED')
    if retention is not None:
        saved = c.execute("""SELECT summary_json FROM audit_event WHERE audit_id=? AND child_id=?
            AND actor='parent_ui' AND action='visual_retention_policy_v1'""",
            (retention,source['child_id'])).fetchone()
        require(saved is not None, 'PAPER_VISUAL_SOURCE_CHANGED')
        try:
            policy=json.loads(saved[0])['policy']
            valid=(policy['revision']==retention and policy['status']=='enabled'
                and policy['purpose']=='local_derived_visual_retention_v1'
                and policy['scope']['child_id']==source['child_id']
                and policy['scope']['session_id'] in {None,source['session_id']})
        except (ValueError,KeyError,TypeError):
            valid=False
        require(valid, 'PAPER_VISUAL_SOURCE_CHANGED')


def validate_capture_identity(c, source, details, *, allow_quality_warning=False, writing_review=None):
    """Original completed OCR provenance, independent of today's monitor epoch."""
    require(c.in_transaction, 'PAPER_METADATA_TRANSACTION_REQUIRED')
    require(type(allow_quality_warning) is bool, 'PAPER_WRITING_REVIEW_REQUIRED')
    if allow_quality_warning:
        from .writing_ocr_authorization import resolve_written_document, _confirmation
        require(isinstance(writing_review,dict) and set(writing_review)=={
            'prompt_id','item_version','confirmation_id','use_authorization_id'}, 'PAPER_WRITING_REVIEW_REQUIRED')
        packet=c.execute('SELECT binding_version FROM writing_ocr_confirmation WHERE confirmation_id=?',
                         (writing_review['confirmation_id'],)).fetchone()
        require(packet, 'PAPER_WRITING_REVIEW_REQUIRED')
        _,review,_=_confirmation(c,source['session_id'],writing_review['confirmation_id'],packet[0])
        require(review.get('writing_review_required'), 'PAPER_WRITING_REVIEW_REQUIRED')
        resolved=resolve_written_document(c,child_id=source['child_id'],session_id=source['session_id'],
            artifact_id=source['artifact_id'],revision_id=source['revision_id'],**writing_review)
        require(resolved['source_version']==review['source_version'], 'PAPER_WRITING_REVIEW_CHANGED')
    generation, frame = details.get('monitor_generation'), details.get('monitor_frame_sha256')
    require(type(generation) is int and generation>=0
        and isinstance(frame,str) and re.fullmatch('[0-9a-f]{64}',frame), 'PAPER_VISUAL_SOURCE_CHANGED')
    row = c.execute('''SELECT a.details_json,a.client_action_id FROM document_page_revision r
        JOIN capture_artifact a ON a.artifact_id=r.artifact_id AND a.child_id=r.child_id AND a.session_id=r.session_id
        JOIN document_monitor_session m ON m.monitor_id=r.monitor_id AND m.child_id=r.child_id AND m.session_id=r.session_id
        WHERE r.revision_id=? AND r.artifact_id=? AND r.child_id=? AND r.session_id=? AND r.monitor_id=?
          AND a.artifact_type='document_ocr' AND (a.status='completed' OR (? AND a.status='quality_warning')) ''',
        (*tuple(source[k] for k in ('revision_id','artifact_id','child_id','session_id','monitor_id')), int(allow_quality_warning))).fetchone()
    require(row and json.loads(row['details_json'])==details, 'PAPER_VISUAL_SOURCE_CHANGED')
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ocr_job'").fetchone():
        require(False, 'PAPER_VISUAL_SOURCE_CHANGED')
    jobs = c.execute('''SELECT * FROM ocr_job WHERE request_id=? AND artifact_id=?
        AND child_id=? AND session_id=?''',
        ('pipeline:'+row['client_action_id'],source['artifact_id'],source['child_id'],source['session_id'])).fetchall()
    require(len(jobs)==1, 'PAPER_VISUAL_SOURCE_CHANGED')
    job=jobs[0]
    require(job['status']=='completed' and job['publication_started']==1 and job['parent_artifact_id'] is None
        and job['owner_key']!='isolated' and job['monitor_generation']==generation and job['image_hash']==frame,
        'PAPER_VISUAL_SOURCE_CHANGED')
    pipeline=details.get('local_pipeline')
    require(isinstance(pipeline,dict) and pipeline.get('image_sha256')==frame, 'PAPER_VISUAL_SOURCE_CHANGED')
    # Generation is the original completed OCR job's epoch, not today's monitor
    # epoch: pausing/restarting capture must not rewrite a confirmed manuscript.
    return dict(job)
