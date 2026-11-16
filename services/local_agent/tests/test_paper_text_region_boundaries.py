"""Actual producer records, scope/geometry mutation counterexamples in rollbacks."""
import base64
from copy import deepcopy
from dataclasses import replace
import json

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.document_learning import DocumentLearningService
from home_study_agent.document_vision import DiagramRegionGate,DocumentVisionService,DocumentVisionError
from home_study_agent.paper_ocr_metadata import validate_page_source_sidecar
from home_study_agent.paper_text_region_projection import project_region
from home_study_agent.writing_ocr_authorization import WritingOcrAuthorizationService
from test_diagnostic_prompt_http import environment,start
from test_document_vision import RegionEngine
from test_paper_text_regions_http import page,PaperGateway,writing_prompt


def test_real_regions_same_snapshot_transaction_and_cross_child(environment,monkeypatch):
    settings,db=environment;settings=replace(settings,data_encryption_key=b'B'*32);raw,text=page(monkeypatch)
    def local(*a,**k):raise DocumentVisionError('DOCUMENT_VISION_NOT_CONFIGURED')
    monkeypatch.setattr(DocumentVisionService,'analyze_page',local)
    with TestClient(create_app(settings,model_gateway=PaperGateway())) as client:
        client.app.state.visual_capture.detector.gate=DiagramRegionGate(RegionEngine(''))
        sid=start(client);prompt=writing_prompt(client,db,sid)
        root=f'/v1/sessions/{sid}/document-monitor'
        assert client.post(root+'/start',json={'client_action_id':'boundary-monitor'}).status_code==200
        artifact=client.post(root+'/ocr',json={'image_base64':base64.b64encode(raw).decode(),'client_action_id':'boundary-frame'}).json()
        response=client.post(root+'/revisions',json={'artifact_id':artifact['artifact_id'],
            'content_signature':'regions','client_action_id':'boundary-revision'})
        assert response.status_code==200,response.text
        revision=response.json()
        auth=WritingOcrAuthorizationService(db)
        packet=auth.prepare_confirmation(sid,prompt_id=prompt['prompt_id'],item_version=prompt['item_version'],
            artifact_id=artifact['artifact_id'],revision_id=revision['revision_id'],response_start=0,response_end=len(text),client_action_id='read')
        binding={k:packet[k] for k in ('confirmation_id','binding_version')}
        with db.connect() as c:
            other=c.execute('SELECT child_id FROM child_profile WHERE child_id!=? LIMIT 1',(artifact['child_id'],)).fetchone()[0]
        assert client.get(f'/v1/sessions/{sid}/writing-ocr',params=binding).status_code==200
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            source=object.__new__(DocumentLearningService)._load(c,sid,revision['revision_id'])
            original=json.loads(source['details_json'])
            assert source['artifact_type']=='document_ocr'
            assert validate_page_source_sidecar(source,original)=='paper_page_source_sidecar_v1'
            for field,value in [('sha256','0'*64),('encrypted',False),('cloud_transmission_allowed',True)]:
                changed=deepcopy(original);changed['page_source'][field]=value
                with pytest.raises(ValueError):validate_page_source_sidecar(source,changed)
            with pytest.raises(ValueError):validate_page_source_sidecar({**source,'artifact_type':'writing_original'},original)
            confirmation=dict(c.execute('SELECT * FROM writing_ocr_confirmation WHERE confirmation_id=?',(packet['confirmation_id'],)).fetchone())
            before=c.total_changes
            def check(details=original,scope=source,receipt=confirmation):
                return project_region(c,source=scope,details=details,confirmation=receipt,response=text)
            with monkeypatch.context() as m:
                m.setattr(db,'connect',lambda:pytest.fail('hidden connection'))
                projected=check()
            assert c.in_transaction and c.total_changes==before and projected['excluded_regions']
            for key in ('child_id','session_id','artifact_id','revision_id','monitor_id'):
                with pytest.raises(ValueError):check(scope={**source,key:'another-original'})
            with pytest.raises(ValueError):check(receipt={**confirmation,'child_id':other})
            mutations=[]
            for field,value in [('status','unknown'),('candidate_count',0),('candidate_count',2),('cloud_transmission_allowed',True)]:
                d=deepcopy(original);d['visual_capture'][field]=value;mutations.append(d)
            region_id=next(iter(original['visual_materials']))
            for field,value in [('frame_sha256','0'*64),('captured_generation',999),('region_version','0'*64),
                ('facts',{'private_context':'not a typed fact'}),('review_status','retracted'),('commands',{'malicious':{}})]:
                d=deepcopy(original);d['visual_materials'][region_id][field]=value;mutations.append(d)
            for field,value in [('text_geometry',None),('text_geometry',{'version':'unknown'})]:
                d=deepcopy(original);d['local_pipeline'][field]=value;mutations.append(d)
            d=deepcopy(original);d['visual_materials']={};mutations.append(d)
            for d in mutations:
                c.execute('SAVEPOINT bad_source')
                c.execute('UPDATE capture_artifact SET details_json=? WHERE artifact_id=?',(json.dumps(d),artifact['artifact_id']))
                with pytest.raises(ValueError):check(details=d)
                c.execute('ROLLBACK TO bad_source');c.execute('RELEASE bad_source')
            for status in ('cancelled','processing','failed'):
                c.execute('SAVEPOINT bad_job')
                c.execute('UPDATE ocr_job SET status=? WHERE artifact_id=?',(status,artifact['artifact_id']))
                with pytest.raises(ValueError):check()
                c.execute('ROLLBACK TO bad_job');c.execute('RELEASE bad_job')
            c.execute('SAVEPOINT bad_job_version')
            c.execute('UPDATE ocr_job SET pipeline_version=pipeline_version+1 WHERE artifact_id=?',(artifact['artifact_id'],))
            with pytest.raises(ValueError):check()
            c.execute('ROLLBACK TO bad_job_version');c.execute('RELEASE bad_job_version')
            assert check()==projected
            c.rollback()
        with db.connect() as c:
            assert json.loads(c.execute('SELECT details_json FROM capture_artifact WHERE artifact_id=?',(artifact['artifact_id'],)).fetchone()[0])==original
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0]==0
            assert c.execute('SELECT count(*) FROM writing_ocr_event').fetchone()[0]==0
        switched=client.post(f'/v1/sessions/{sid}/switch-child',json={'target_child_id':other,'client_action_id':'boundary-switch-child'})
        assert switched.status_code==200,switched.text
        other_session=switched.json()['session_id']
        denied=client.get(f'/v1/sessions/{other_session}/writing-ocr',params=binding)
        assert denied.status_code==409 and denied.headers['cache-control']=='no-store'
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_attempt WHERE child_id=?',(other,)).fetchone()[0]==0
