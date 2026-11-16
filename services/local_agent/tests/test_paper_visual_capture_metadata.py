"""Real producer sidecar, authorized OCR response; synthetic inference only."""
import base64
from copy import deepcopy
from dataclasses import replace
import json

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.document_learning import DocumentLearningService
from home_study_agent.document_vision import DocumentVisionError,DocumentVisionService
from home_study_agent.paper_ocr_metadata import validate_visual_capture_sidecar,VERSION
from home_study_agent.paper_writing_projection import _metadata
from test_diagnostic_prompt_http import environment
from test_diagnostic_prompt_http import start
from test_paper_writing_runtime_http import PaperGateway,synthetic_page
from test_writing_voice_runtime_http import writing_prompt
from test_paper_writing_runtime_http import test_actual_paper_confirmation_formative_feedback_revision_and_restart as paper_journey


def test_real_producer_plain_manuscript_stays_text_only_through_http_restart(environment, monkeypatch):
    paper_journey(environment, monkeypatch, consent=True, numbered=False)


def test_actual_sidecar_scope_shape_flags_and_borrowed_snapshot(environment,monkeypatch):
    settings,db=environment
    settings=replace(settings,data_encryption_key=b'S'*32)
    png,_=synthetic_page(monkeypatch)
    def no_cloud(*a,**k):raise DocumentVisionError('DOCUMENT_VISION_NOT_CONFIGURED')
    monkeypatch.setattr(DocumentVisionService,'analyze_page',no_cloud)
    with TestClient(create_app(settings,model_gateway=PaperGateway())) as client:
        sid=start(client)
        writing_prompt(client,db,sid)
        root=f'/v1/sessions/{sid}/document-monitor'
        assert client.post(root+'/start',json={'client_action_id':'sidecar-monitor'}).status_code==200
        artifact=client.post(root+'/ocr',json={'image_base64':base64.b64encode(png).decode(),'client_action_id':'sidecar-ocr'})
        assert artifact.status_code==200,artifact.text
        identity=artifact.json()['artifact_id']
        revision=client.post(root+'/revisions',json={'artifact_id':identity,'content_signature':'actual-plain-page',
            'client_action_id':'sidecar-revision'})
        assert revision.status_code==200,revision.text
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            reader=object.__new__(DocumentLearningService)
            source=reader._load(c,sid,revision.json()['revision_id'])
            details=json.loads(source['details_json'])
            assert details['visual_capture']['reason']=='VISUAL_NO_SUPPORTED_GEOMETRY'
            before=c.total_changes
            with monkeypatch.context() as m:
                m.setattr(db,'connect',lambda:pytest.fail('validator opened another connection'))
                assert validate_visual_capture_sidecar(c,source,details)==VERSION
            assert c.in_transaction and c.total_changes==before
            for key,value in [('version','unknown'),('status','unknown'),('status','restricted'),('status','prepared'),
                ('reason','VISUAL_FRAME_UNREADABLE'),('reason',None),('candidate_count',True),('candidate_count',1),
                ('expires_at','2099-01-01T00:00:00+00:00'),('local_only',False),('cloud_transmission_allowed',True),
                ('retention_policy_id','not-real-authority'),('facts',{'description':'private fact'}),
                ('original_page','not-allowed')]:
                bad=deepcopy(details);bad['visual_capture'][key]=value
                with pytest.raises(ValueError,match='WRITING_PAPER_VISUAL_'):
                    validate_visual_capture_sidecar(c,source,bad)
            for value in (None,[],{'local_only':True}):
                bad=deepcopy(details);bad['visual_capture']=value
                with pytest.raises(ValueError,match='WRITING_PAPER_VISUAL_'):
                    validate_visual_capture_sidecar(c,source,bad)
            for key in ('artifact_id','revision_id','child_id','session_id','monitor_id'):
                with pytest.raises(ValueError,match='WRITING_PAPER_VISUAL_SOURCE_CHANGED'):
                    validate_visual_capture_sidecar(c,{**source,key:'wrong-original'},details)
            # Mutate the stored compartment too: matching JSON alone must not
            # replace the completed OCR's original frame and generation.
            for key,value in [('monitor_frame_sha256','0'*64),('monitor_generation',details['monitor_generation']+1)]:
                c.execute('SAVEPOINT original_source')
                bad=deepcopy(details);bad[key]=value
                c.execute('UPDATE capture_artifact SET details_json=? WHERE artifact_id=?',(json.dumps(bad),identity))
                with pytest.raises(ValueError,match='WRITING_PAPER_VISUAL_SOURCE_CHANGED'):
                    validate_visual_capture_sidecar(c,source,bad)
                c.execute('ROLLBACK TO original_source');c.execute('RELEASE original_source')
            # A later monitor epoch is not the original capture's identity.
            c.execute('UPDATE document_monitor_session SET generation=generation+1 WHERE monitor_id=?',(source['monitor_id'],))
            assert validate_visual_capture_sidecar(c,source,details)==VERSION
            c.rollback()
        with db.connect() as c:
            assert json.loads(c.execute('SELECT details_json FROM capture_artifact WHERE artifact_id=?',(identity,)).fetchone()[0])==details
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==0
        for payload in ({'local_pipeline':{'local_only':True}}, {'local_pipeline':{'cloud_transmission_allowed':False}},
            {'knowledge_context':'private course'}, {'original_page':'pixels'}, {'local_pipeline':{'raw_video':'video'}}):
            with pytest.raises(ValueError,match='PAPER_RESTRICTED_SOURCE'):
                _metadata(payload)
