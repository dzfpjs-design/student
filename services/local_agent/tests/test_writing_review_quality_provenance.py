"""Unknown warning causes cannot acquire a writing-purpose exception."""
import json
import pytest
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.document_learning import DocumentLearningService
from home_study_agent.paper_writing_review_projection import validate_text
from home_study_agent.writing_ocr_authorization import _confirmation
from test_writing_ocr_warning_voice import warning_read
from test_writing_voice_runtime_http import environment, ControlGateway, TEXT


@pytest.mark.parametrize('details',[None,{}, {'quality':None}, {'quality':{}},
    {'quality':{'passed':False,'issues':[]}}, {'local_pipeline':{'manual_review':{'required':True,'reasons':[]}}},
    {'quality':{'passed':False,'issues':None}}, {'quality':{'passed':False,'issues':'glare'}},
    {'quality':[]}, {'local_pipeline':[]}, {'quality':{'passed':True,'issues':['glare']}},
    {'quality':{'issues':['glare']}}, {'manual_review':{'reasons':['missing_question_start']}}])
def test_warning_needs_explicit_supported_quality_provenance(details):
    source={'artifact_status':'quality_warning','text':TEXT,'details_json':json.dumps(details)}
    with pytest.raises(ValueError,match='PAPER_QUALITY_REQUIRES_RECAPTURE_OR_SPECIALIST_REVIEW'):
        validate_text(source,TEXT,[],isolated_response=False)


def test_confirmation_upstream_does_not_prove_missing_quality_cause(environment,monkeypatch):
    settings,db=environment
    gateway=ControlGateway('')
    with TestClient(create_app(settings,model_gateway=gateway)) as client:
        # No fake original/evidence: this only creates a pending raw-page read.
        sid,_,turn,revision=warning_read(client,db,settings,monkeypatch,gateway,quality_details={})
        binding=turn['action']['writing_ocr_confirmation']
        with db.connect() as c:
            row,review,_=_confirmation(c,sid,binding['confirmation_id'],binding['binding_version'])
            assert review['writing_review_required'] and review['text']==TEXT
            reader=object.__new__(DocumentLearningService)
            source=reader._load(c,sid,row['revision_id'])
            assert json.loads(source['details_json'])=={}
            with pytest.raises(ValueError,match='PAPER_QUALITY_REQUIRES_RECAPTURE_OR_SPECIALIST_REVIEW'):
                validate_text(source,review['text'],[],isolated_response=False)


@pytest.mark.parametrize('cause',['glare','low_ocr_confidence'])
def test_supported_cause_is_explicit_and_bound_to_its_measurement(cause):
    details=({'quality':{'passed':False,'issues':['glare']}} if cause=='glare' else
        {'quality':{'passed':True,'issues':[]},'local_pipeline':{
            'manual_review':{'required':True,'reasons':['low_ocr_confidence']}}})
    source={'artifact_status':'quality_warning','text':TEXT,'details_json':json.dumps(details),
        'ocr_confidence':.49,'artifact_confidence':.49}
    assert validate_text(source,TEXT,[],isolated_response=False)=='child_reviewed_writing_formative_text_v1'
    if cause=='low_ocr_confidence':
        for value in (None,True,float('nan'),1.1,-.1,.99):
            with pytest.raises(ValueError,match='PAPER_QUALITY_REQUIRES_RECAPTURE_OR_SPECIALIST_REVIEW'):
                validate_text({**source,'ocr_confidence':value,'artifact_confidence':value},TEXT,[],isolated_response=False)
    details.setdefault('quality',{})['issues']=['glare','orientation_ambiguous']
    details['quality']['passed']=False
    with pytest.raises(ValueError,match='PAPER_QUALITY_REQUIRES_RECAPTURE_OR_SPECIALIST_REVIEW'):
        validate_text({**source,'details_json':json.dumps(details)},TEXT,[],isolated_response=False)


@pytest.mark.parametrize('raw',['{','[]','null','false','0','"glare"'])
def test_malformed_serialized_provenance_fails_closed(raw):
    with pytest.raises(ValueError,match='PAPER_QUALITY_REQUIRES_RECAPTURE_OR_SPECIALIST_REVIEW'):
        validate_text({'artifact_status':'quality_warning','text':TEXT,'details_json':raw},TEXT,[],isolated_response=False)
