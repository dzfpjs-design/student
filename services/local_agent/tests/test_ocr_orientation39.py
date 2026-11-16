"""Contract controls supplement the frozen actual installed-engine red evidence."""
import json
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from home_study_agent.local_ocr.service import RapidOcrEngine, LocalOcrService
from home_study_agent.local_ocr.workflow import LocalOcrWorkflow
from home_study_agent.local_ocr.runtime_adapter import CpuScheduledMediaAdapter
from home_study_agent.local_ocr.resource_budget import CpuResourceQueue
from home_study_agent.local_media_ai import LocalMediaAI
from test_document_monitoring import _service
from test_local_ocr_pipeline import FakeEngine, image_bytes


def test_global_trials_disable_crop_classifier_but_direct_calls_restore_it():
    calls=[]
    class InstalledApi:
        def __call__(self,image,*,use_cls):
            calls.append(use_cls)
            return SimpleNamespace(txts=[],scores=[],boxes=[])
    engine=RapidOcrEngine()
    engine._engine=InstalledApi()
    engine.recognize_page_orientation(np.zeros((100,100,3),np.uint8),'cpu')
    engine.recognize(np.zeros((100,100,3),np.uint8),'cpu')
    assert calls==[False,True]


def test_tied_global_orientations_are_reviewable_not_approved(tmp_path):
    class AmbiguousEngine(FakeEngine):
        def recognize_page_orientation(self,image,route):
            return [{'text':'88888888','confidence':.99,'box':[[50,80],[300,80],[300,110],[50,110]]}]
    service=LocalOcrService(tmp_path/'ocr.db',[AmbiguousEngine()])
    payload=image_bytes()
    result=service.run(service.enqueue(payload,'ambiguous')['job_id'],payload)['result']
    assert 'orientation_ambiguous' in result['quality']['issues']
    assert result['quality']['passed'] is False
    assert result['manual_review']['required'] is True


def test_formula_warning_caps_artifact_and_revision_after_restart(tmp_path):
    class FormulaEngine(FakeEngine):
        def recognize(self,image,route):
            return [{'text':'1. Calculate 12 + 8 = 20','confidence':.99,
                     'box':[[60,80],[360,80],[360,110],[60,110]]}]
    database,monitor=_service(tmp_path)
    monitor.start('study','start')
    media=LocalMediaAI(database,tmp_path,tmp_path/'models')
    ocr=LocalOcrService(tmp_path/'ocr.db',[FormulaEngine()])
    workflow=LocalOcrWorkflow(CpuScheduledMediaAdapter(CpuResourceQueue(tmp_path/'queue.db'),ocr),media)
    payload=image_bytes()
    artifact=workflow.process('study',payload,'formula',False)
    revision=monitor.record_revision('study',artifact['artifact_id'],'frame','revision')
    assert artifact['details']['local_pipeline']['manual_review']['required'] is True
    assert artifact['status']=='quality_warning'
    assert artifact['confidence']<=.49
    assert revision['review_required'] is True
    replay=workflow.process('study',payload,'formula',False)
    assert replay['artifact_id']==artifact['artifact_id']
    with database.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM capture_artifact').fetchone()[0]==1
        assert c.execute('SELECT review_required FROM document_page_revision').fetchone()[0]==1
