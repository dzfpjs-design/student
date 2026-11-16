"""Orientation evidence must belong to the chosen engine and retain coverage."""
import json

import pytest

from home_study_agent.local_ocr.service import LocalOcrService, _recognition_score
from test_local_ocr_pipeline import image_bytes


def _line(text, confidence=.99, *, y=80, width=220, height=24):
    return {'text':text,'confidence':confidence,
            'box':[[60,y],[60+width,y],[60+width,y+height],[60,y+height]]}


class GlobalEngine:
    name='orientation-contract'
    capability={'printed_zh','printed_en'}

    def __init__(self,trials):
        self.trials=iter(trials)

    def health(self):
        return {'ready':True,'route':'cpu'}

    def recognize(self,image,route):
        raise AssertionError('Global trials must not call ordinary crop-correcting API')

    def recognize_page_orientation(self,image,route):
        value=next(self.trials)
        if isinstance(value,Exception):raise value
        return value


def _process(tmp_path,engines):
    service=LocalOcrService(tmp_path/'ocr.db',engines)
    payload=image_bytes(640,820)
    job=service.run(service.enqueue(payload,'orientation41')['job_id'],payload)
    assert job['status']=='completed',job
    return job['result']


@pytest.mark.parametrize('complete',[
    [_line(text,y=80+i*50,width=64,height=30) for i,text in enumerate(['甲乙','丙丁','戊己','庚辛'])],
    [_line('A full paragraph retained as a tall text region.',width=65,height=130)],
])
def test_credible_coverage_loss_requires_review_without_stitching(tmp_path,complete):
    fragment=[_line('xyz')]
    assert _recognition_score(complete)>_recognition_score(fragment)
    result=_process(tmp_path,[GlobalEngine([complete,fragment,[],[]])])
    assert result['lines']==fragment  # Preview may select a direction; never combine trials.
    assert result['quality']['orientation_evidence']['ambiguous'] is True
    assert 'orientation_text_coverage_loss' in result['quality']['issues']
    assert result['quality']['passed'] is False


def test_legacy_winner_after_global_failure_is_unverified(tmp_path):
    class Legacy:
        name='legacy';capability={'printed_en'}
        def __init__(self):self.calls=0
        def health(self):return {'ready':True,'route':'cpu'}
        def recognize(self,image,route):
            self.calls+=1
            return [_line('Last sentence.',.99 if self.calls==1 else .3),
                    _line('First sentence.',.99 if self.calls==1 else .3,y=130)]
    global_engine=GlobalEngine([[_line('First sentence.',.8)],RuntimeError(),RuntimeError(),RuntimeError()])
    result=_process(tmp_path,[global_engine,Legacy()])
    assert result['engine']=='legacy'
    assert result['quality']['orientation_evidence']['ambiguous']
    assert result['quality']['orientation_evidence']['method']!='whole_page_trials_without_crop_classifier'
    assert 'orientation_engine_unverified' in result['quality']['issues']


def test_global_winner_does_not_compare_scores_across_engine_instances(tmp_path):
    first=GlobalEngine([[_line('Original sentence.',.8)],RuntimeError(),RuntimeError(),RuntimeError()])
    second=GlobalEngine([[_line('Better recognized sentence.',.99)],[],[]])
    # Same display name must not turn two independent engine instances into one authority.
    result=_process(tmp_path,[first,second])
    assert 'orientation_engine_unverified' in result['quality']['issues']
    assert result['quality']['orientation_evidence']['ambiguous']


def test_legacy_only_retains_preview_but_cannot_prove_page_orientation(tmp_path):
    class Legacy:
        name='legacy';capability={'printed_en'}
        def __init__(self):self.calls=0
        def health(self):return {'ready':True,'route':'cpu'}
        def recognize(self,image,route):
            self.calls+=1
            return [_line('A readable original sentence.',.99 if self.calls==1 else .2)]
    result=_process(tmp_path,[Legacy()])
    assert result['lines'][0]['text']=='A readable original sentence.'
    assert 'orientation_engine_unverified' in result['quality']['issues']
    assert not result['quality']['passed']


@pytest.mark.parametrize('text',['Cat','答案六','Short question'])
def test_clear_short_text_is_not_rejected_by_length(tmp_path,text):
    chosen=[_line(text)]
    result=_process(tmp_path,[GlobalEngine([chosen,[_line('weak reading',.2)],[],[]])])
    assert result['lines']==chosen
    assert not result['quality']['orientation_evidence']['ambiguous']
    assert not any(reason.startswith('orientation_') for reason in result['quality']['issues'])


def test_long_low_confidence_garbage_is_not_a_coverage_veto(tmp_path):
    chosen=[_line('Cat')]
    garbage=[_line('unreliable output '*12,.2,width=60,height=140)]
    result=_process(tmp_path,[GlobalEngine([chosen,garbage,[],[]])])
    assert result['lines']==chosen
    assert not result['quality']['orientation_evidence']['ambiguous']


def test_equal_confidence_and_coverage_stays_ambiguous(tmp_path):
    trial=[_line('Same readable sentence.')]
    result=_process(tmp_path,[GlobalEngine([trial,trial,trial,trial])])
    assert 'orientation_ambiguous' in result['quality']['issues']


def test_only_one_successful_trial_is_not_orientation_proof(tmp_path):
    class Disappearing(GlobalEngine):
        def health(self):
            self.health_calls=getattr(self,'health_calls',0)+1
            return {'ready':self.health_calls==1,'route':'cpu'}
    result=_process(tmp_path,[Disappearing([[_line('A readable original sentence.')]])])
    assert result['quality']['orientation_evidence']['ambiguous']
    assert not result['quality']['passed']


def test_empty_lines_are_canonicalized_before_text_and_geometry(tmp_path):
    from hashlib import sha256
    from home_study_agent.local_ocr.text_geometry import validate
    chosen=[_line('A complete sentence.'),_line('  ',y=130),_line('',y=180)]
    result=_process(tmp_path,[GlobalEngine([chosen,[_line('weak',.2)],[],[]])])
    assert result['lines']==chosen[:1]
    validate(result['text_geometry'],result['lines'],frame_sha256=sha256(image_bytes(640,820)).hexdigest(),
             text='A complete sentence.')


def test_cancel_stops_later_trials_without_result_or_cache(tmp_path):
    from threading import Event,Thread
    entered=Event();release=Event()
    class Blocking(GlobalEngine):
        calls=0
        def recognize_page_orientation(self,image,route):
            self.calls+=1
            if self.calls==1:
                entered.set();assert release.wait(3)
            return [_line('Readable sentence.')]
    engine=Blocking([])
    service=LocalOcrService(tmp_path/'ocr.db',[engine])
    payload=image_bytes(640,820);job=service.enqueue(payload,'cancel41');output={}
    worker=Thread(target=lambda:output.update(job=service.run(job['job_id'],payload)))
    worker.start()
    try:
        assert entered.wait(3)
        service.cancel(job['job_id']);release.set();worker.join(3)
        assert not worker.is_alive()
        assert output['job']['status']=='cancelled' and output['job']['result'] is None
        with service._connect() as c:
            assert c.execute('SELECT COUNT(*) FROM ocr_cache').fetchone()[0]==0
        assert engine.calls==1
    finally:
        release.set();worker.join(3)


def test_coverage_warning_persists_to_artifact_and_revision(tmp_path):
    from test_document_monitoring import _service
    from home_study_agent.local_media_ai import LocalMediaAI
    from home_study_agent.local_ocr.workflow import LocalOcrWorkflow
    from home_study_agent.local_ocr.runtime_adapter import CpuScheduledMediaAdapter
    from home_study_agent.local_ocr.resource_budget import CpuResourceQueue
    complete=[_line('A complete original paragraph.',width=65,height=130)]
    engine=GlobalEngine([complete,[_line('xyz')],[],[]])
    db,monitor=_service(tmp_path)
    monitor.start('study','start')
    ocr=LocalOcrService(tmp_path/'ocr.db',[engine])
    flow=LocalOcrWorkflow(CpuScheduledMediaAdapter(CpuResourceQueue(tmp_path/'queue.db'),ocr),
                         LocalMediaAI(db,tmp_path,tmp_path/'models'))
    payload=image_bytes(640,820)
    artifact=flow.process('study',payload,'coverage41',False)
    revision=monitor.record_revision('study',artifact['artifact_id'],'frame','revision41')
    assert artifact['status']=='quality_warning' and artifact['confidence']<=.49
    assert revision['review_required']
    assert flow.process('study',payload,'coverage41',False)['artifact_id']==artifact['artifact_id']
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM capture_artifact').fetchone()[0]==1
