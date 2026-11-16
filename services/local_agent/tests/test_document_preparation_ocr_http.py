"""Preparation OCR uses the real local pipeline, with no extra media disclosure."""
import base64
import json

import cv2
import numpy as np
import pytest

from home_study_agent.database import Database
from home_study_agent.local_ocr.service import RapidOcrEngine
from home_study_agent.document_vision import DocumentVisionService
from home_study_agent.device_registry import DeviceRegistry
from home_study_agent.visual_capture_producer import VisualCaptureProducer
from test_app import client_for, advance_to_study


@pytest.mark.parametrize('phase', ['homework_capture', 'plan_review'])
def test_preparation_local_ocr_is_durable_without_cloud_or_source_image(tmp_path, monkeypatch, phase):
    text='1. Calculate 12 plus 8.'
    lines=[{'text':text, 'confidence':.99, 'box':[[55,100],[900,100],[900,170],[55,170]]}]
    monkeypatch.setattr(RapidOcrEngine,'recognize',lambda *a,**k:lines)
    monkeypatch.setattr(RapidOcrEngine,'recognize_page_orientation',lambda *a,**k:lines)
    def prohibited(*args,**kwargs): raise AssertionError('preparation must not upload or retain a page')
    monkeypatch.setattr(DocumentVisionService,'analyze_page',prohibited)
    monkeypatch.setattr(VisualCaptureProducer,'prepare',prohibited)
    image=np.full((300,1000,3),230,dtype=np.uint8)
    cv2.putText(image,'12 plus 8', (55,140),cv2.FONT_HERSHEY_SIMPLEX,1.1,(0,0,0),2)
    ok,png=cv2.imencode('.png',image); assert ok
    with client_for(tmp_path, agent_runtime_enabled=True) as client:
        sid,_=advance_to_study(client,child_id='child-primary-4')
        db=Database(tmp_path/'test.db')
        with db.connect() as c:
            c.execute('UPDATE daily_session SET state=? WHERE session_id=?',(phase,sid)); c.commit()
        DeviceRegistry(db).register(sid,'document_camera','a'*64,{})
        assert client.post(f'/v1/sessions/{sid}/document-monitor/start',json={'client_action_id':'synthetic-start'}).status_code==200
        ocr=client.post(f'/v1/sessions/{sid}/document-monitor/ocr',json={'image_base64':base64.b64encode(png.tobytes()).decode(),'client_action_id':'synthetic-ocr'})
        assert ocr.status_code==200,ocr.text
        artifact=ocr.json(); assert text in artifact['extracted_text']
        result=client.post(f'/v1/sessions/{sid}/document-monitor/revisions',json={'artifact_id':artifact['artifact_id'],'content_signature':'synthetic-page','client_action_id':'synthetic-revision'})
        assert result.status_code==200,result.text
        with db.connect() as c:
            row=c.execute('SELECT local_path,details_json FROM capture_artifact WHERE artifact_id=?',(artifact['artifact_id'],)).fetchone()
            assert row['local_path'] is None
            assert json.loads(row['details_json'])['monitor_capture_phase']==phase
            assert c.execute('SELECT COUNT(*) FROM document_page_revision WHERE artifact_id=?',(artifact['artifact_id'],)).fetchone()[0]==1
    reopened=Database(tmp_path/'test.db'); reopened.initialize()
    with reopened.connect() as c:
        assert c.execute('SELECT extracted_text FROM capture_artifact WHERE artifact_id=?',(artifact['artifact_id'],)).fetchone()[0].find(text)>=0
        assert c.execute('SELECT COUNT(*) FROM document_page_revision WHERE artifact_id=?',(artifact['artifact_id'],)).fetchone()[0]==1


@pytest.mark.parametrize('restriction', ['no_lease', 'paused'])
def test_preparation_ocr_requires_live_lease_and_observation_permission(tmp_path, restriction):
    with client_for(tmp_path, agent_runtime_enabled=True) as client:
        sid,_=advance_to_study(client,child_id='child-primary-4')
        db=Database(tmp_path/'test.db')
        with db.connect() as c:
            c.execute("UPDATE daily_session SET state='homework_capture',observation_status=? WHERE session_id=?",('paused' if restriction=='paused' else 'enabled',sid)); c.commit()
        if restriction=='paused': DeviceRegistry(db).register(sid,'document_camera','a'*64,{})
        assert client.post(f'/v1/sessions/{sid}/document-monitor/start',json={'client_action_id':'synthetic-start'}).status_code==200
        response=client.post(f'/v1/sessions/{sid}/document-monitor/ocr',json={'image_base64':base64.b64encode(b'x'*2048).decode(),'client_action_id':'synthetic-ocr'})
        assert response.status_code==409
        with db.connect() as c:
            assert c.execute("SELECT COUNT(*) FROM capture_artifact WHERE session_id=? AND artifact_type='document_ocr'",(sid,)).fetchone()[0]==0


def test_phase_change_during_ocr_does_not_create_revision_or_upload(tmp_path, monkeypatch):
    image=np.full((300,1000,3),230,dtype=np.uint8)
    cv2.putText(image,'12 plus 8', (55,140),cv2.FONT_HERSHEY_SIMPLEX,1.1,(0,0,0),2)
    ok,png=cv2.imencode('.png',image); assert ok
    with client_for(tmp_path, agent_runtime_enabled=True) as client:
        sid,_=advance_to_study(client,child_id='child-primary-4')
        db=Database(tmp_path/'test.db')
        with db.connect() as c:
            c.execute("UPDATE daily_session SET state='homework_capture' WHERE session_id=?",(sid,)); c.commit()
        DeviceRegistry(db).register(sid,'document_camera','a'*64,{})
        assert client.post(f'/v1/sessions/{sid}/document-monitor/start',json={'client_action_id':'synthetic-start'}).status_code==200
        def change_phase(*args,**kwargs):
            with db.connect() as c:
                c.execute("UPDATE daily_session SET state='plan_review' WHERE session_id=?",(sid,)); c.commit()
            return [{'text':'12 plus 8','confidence':.99,'box':[[55,100],[900,100],[900,170],[55,170]]}]
        def prohibited(*args,**kwargs): raise AssertionError('phase change cannot upload or retain image')
        monkeypatch.setattr(RapidOcrEngine,'recognize',change_phase)
        monkeypatch.setattr(RapidOcrEngine,'recognize_page_orientation',change_phase)
        monkeypatch.setattr(DocumentVisionService,'analyze_page',prohibited)
        monkeypatch.setattr(VisualCaptureProducer,'prepare',prohibited)
        result=client.post(f'/v1/sessions/{sid}/document-monitor/ocr',json={'image_base64':base64.b64encode(png.tobytes()).decode(),'client_action_id':'synthetic-ocr'})
        assert result.status_code==409,result.text
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM document_page_revision WHERE session_id=?',(sid,)).fetchone()[0]==0
            assert c.execute("SELECT COUNT(*) FROM capture_artifact WHERE session_id=? AND local_path IS NOT NULL",(sid,)).fetchone()[0]==0
