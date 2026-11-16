"""Purpose-bound confirmed text isolation. No pixels, facts or authorization grant."""
from datetime import datetime
import json
import re

import cv2
import numpy as np

from .local_supply import digest
from .paper_ocr_metadata import validate_capture_identity, validate_retention_reference
from .writing_response_origin import require
from .local_ocr.text_geometry import validate

VERSION='confirmed_paper_text_regions_v1'
_SIDECAR={'version','status','reason','candidate_count','expires_at','retention_policy_id','local_only','cloud_transmission_allowed'}
_REGION={'version','source_version','frame_sha256','region_xyxy','width','height','frame_width','frame_height',
    'coordinate_space','crop_to_frame','safe_png_sha256','gate_version','page_corners','captured_generation',
    'local_only','cloud_transmission_allowed','mastery_claim_allowed','region_version'}
_MUTABLE={'facts','facts_version','confirmation_id','history','commands','review_status'}


def _mutable(region, validate_facts):
    def facts(value,version):
        require(version==digest({'region_version':region['region_version'],'facts':validate_facts(value)}),
            'PAPER_TEXT_REGION_FACTS_CHANGED')
    if region.get('facts') is not None: facts(region['facts'],region.get('facts_version'))
    else: require(region.get('facts_version') is None and region.get('confirmation_id') is None,
                  'PAPER_TEXT_REGION_FACTS_CHANGED')
    history=region.get('history',[])
    require(isinstance(history,list) and len(history)<=128,'PAPER_TEXT_REGION_RECORD_INVALID')
    for entry in history:
        require(isinstance(entry,dict) and set(entry)=={'facts','facts_version','confirmation_id'},'PAPER_TEXT_REGION_RECORD_INVALID')
        facts(entry['facts'],entry['facts_version'])
    commands=region.get('commands',{})
    require(isinstance(commands,dict) and len(commands)<=512,'PAPER_TEXT_REGION_RECORD_INVALID')
    for key,entry in commands.items():
        require(isinstance(key,str) and 0<len(key)<=200 and isinstance(entry,dict)
            and set(entry)=={'request','result'} and isinstance(entry['request'],str)
            and re.fullmatch('[0-9a-f]{64}',entry['request']) and isinstance(entry['result'],dict),
            'PAPER_TEXT_REGION_RECORD_INVALID')
        result=entry['result']
        require(set(result)<= {'facts_version','status','binding','local_only','cloud_transmission_allowed','mastery_claim_allowed'}
            and result.get('local_only') is True and result.get('cloud_transmission_allowed') is False
            and result.get('mastery_claim_allowed') is False,'PAPER_TEXT_REGION_RECORD_INVALID')


def project_region(c, *, source, details, confirmation, response, allow_quality_warning=False, writing_review=None):
    """Borrow the exact confirmed source snapshot, never fetch a newer page."""
    try:
        return _project(c,source,details,confirmation,response,allow_quality_warning=allow_quality_warning,writing_review=writing_review)
    except (KeyError,TypeError,OverflowError,cv2.error) as error:
        raise ValueError('WRITING_PAPER_TEXT_REGION_INVALID') from error
    except ValueError as error:
        if str(error).startswith('WRITING_'):raise
        raise ValueError('WRITING_PAPER_TEXT_REGION_INVALID') from error


def _project(c,source,details,confirmation,response,*,allow_quality_warning=False,writing_review=None):
    job=validate_capture_identity(c,source,details,allow_quality_warning=allow_quality_warning,writing_review=writing_review)
    meta=details.get('visual_capture')
    require(isinstance(meta,dict) and set(meta)==_SIDECAR and meta['version']=='visual_capture_candidates_v1'
        and meta['status']=='prepared' and meta['reason'] is None and type(meta['candidate_count']) is int
        and 0<meta['candidate_count']<=8 and meta['local_only'] is True and meta['cloud_transmission_allowed'] is False,
        'PAPER_TEXT_REGION_SIDECAR_INVALID')
    require(isinstance(meta['expires_at'],str) and datetime.fromisoformat(meta['expires_at']).tzinfo is not None,
        'PAPER_TEXT_REGION_SIDECAR_INVALID')
    # expires_at is the historical in-memory staging deadline, not the lifetime
    # of a sealed OCR provenance record. It cannot authorize image use now.
    validate_retention_reference(c,source,meta['retention_policy_id'])
    regions=details.get('visual_materials')
    require(isinstance(regions,dict) and len(regions)==meta['candidate_count'], 'PAPER_TEXT_REGION_INCOMPLETE')
    pipeline=details['local_pipeline'];result=json.loads(job['result_json'])
    geometry=pipeline.get('text_geometry')
    require(isinstance(result,dict) and type(job['pipeline_version']) is int and job['pipeline_version']>=5
        and result.get('pipeline_version')==job['pipeline_version']
        and geometry is not None and result.get('text_geometry')==geometry
        and result.get('lines')==details['lines'], 'PAPER_TEXT_REGION_ORIGINAL_CHANGED')
    validate(geometry,details['lines'],frame_sha256=job['image_hash'],text=source['text'])
    require(all(confirmation[k]==source[k] for k in ('child_id','session_id','artifact_id','revision_id')),
        'PAPER_TEXT_REGION_CONFIRMATION_CHANGED')
    start,end=confirmation['response_start'],confirmation['response_end']
    require(source['text'][start:end]==response,'PAPER_TEXT_REGION_RESPONSE_CHANGED')
    selected=[line for line in geometry['lines'] if line['end']>start and line['start']<end]
    # Character boxes are not available: never guess within a partially selected
    # OCR line. Existing voice clarification can select full line anchors.
    require(selected and selected[0]['start']==start and selected[-1]['end']==end
        and '\n'.join(source['text'][r['start']:r['end']] for r in selected)==response,
        'PAPER_TEXT_REGION_PARTIAL_LINE')
    from .visual_material import _region, validate_facts
    scope={k:source[k] for k in ('child_id','session_id','artifact_id','revision_id')}
    region_versions=[]
    for identity,region in regions.items():
        require(isinstance(identity,str) and re.fullmatch('[A-Za-z][A-Za-z0-9_-]{0,39}',identity),
            'PAPER_TEXT_REGION_RECORD_INVALID')
        require(isinstance(region,dict) and _REGION<=set(region) and set(region)<=_REGION|_MUTABLE,
            'PAPER_TEXT_REGION_RECORD_INVALID')
        require(region['local_only'] is True and region['cloud_transmission_allowed'] is False
            and region['mastery_claim_allowed'] is False
            and region.get('review_status') in {None,'unconfirmed','confirmed'},
            'PAPER_TEXT_REGION_RESTRICTED')
        _mutable(region,validate_facts)
        _,_,actual=_region(c,scope,identity,region['region_version'])
        require(actual==region and region['coordinate_space']=='original_frame_pixels'
            and [region['frame_width'],region['frame_height']]==geometry['frame_size']
            and region['captured_generation']==job['monitor_generation'],'PAPER_TEXT_REGION_ORIGINAL_CHANGED')
        x0,y0,x1,y1=region['region_xyxy']
        require(all(type(v) in (int,float) and np.isfinite(v) for v in (x0,y0,x1,y1))
            and 0<=x0<x1<=region['frame_width'] and 0<=y0<y1<=region['frame_height'], 'PAPER_TEXT_REGION_RECORD_INVALID')
        exclusion=np.asarray([[x0-3,y0-3],[x1+3,y0-3],[x1+3,y1+3],[x0-3,y1+3]],np.float32)
        for line in selected:
            area,_=cv2.intersectConvexConvex(np.asarray(line['frame_polygon'],np.float32),exclusion)
            require(area==0,'PAPER_TEXT_REGION_OVERLAP')
        region_versions.append([identity,region['region_version']])
    return dict(version=VERSION,geometry_sha256=geometry['geometry_sha256'],
        excluded_regions=sorted(region_versions),response_start=start,response_end=end,
        local_only=True,cloud_transmission_allowed=False)
