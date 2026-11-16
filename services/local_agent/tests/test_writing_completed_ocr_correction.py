"""A clean ordinary OCR page can still have a literal transcription correction."""
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from test_paper_writing_runtime_http import PaperGateway
from test_writing_ocr_warning_voice import finish_read
from test_writing_voice_runtime_http import environment, read_turn, capture, interpret
import json
import pytest


def test_completed_unflagged_page_correction_keeps_ordinary_quality_gate(environment,monkeypatch):
    settings,db=environment
    gateway=PaperGateway()
    app=create_app(settings,model_gateway=gateway)
    with TestClient(app) as client:
        sid,prompt,reading=read_turn(client,db,settings,monkeypatch,gateway)
        finish_read(client,sid,reading,'completed-page-read')
        quote='识别更正，把“Saturday”改成“Sunday”'
        voice=capture(client,settings,monkeypatch,sid,quote,prompt,'completed-page-edit-voice')
        corrected=interpret(client,sid,quote,'completed-page-edit',voice)
        finish_read(client,sid,corrected,'completed-page-corrected-read')
        app.state.privacy_lifecycle.update_preferences({'cloud_model_text_allowed':True})
        gateway.quote,gateway.control='是我写的，刚才读出的文字没有错','writing.confirm_original'
        voice=capture(client,settings,monkeypatch,sid,gateway.quote,prompt,'completed-page-confirm-voice')
        confirmed=interpret(client,sid,gateway.quote,'completed-page-confirm',voice)
        response=client.post(f"/v1/agent-turns/{confirmed['agent_turn_id']}/execute")
        assert response.status_code==200,response.text
        follow=response.json()['turn']['local_evaluation']
        assert follow['status']=='feedback_required',{key:follow.get(key) for key in ('status','reason')}


@pytest.mark.parametrize('empty_failure',[{'quality':{'passed':False,'issues':[]}},
    {'manual_review':{'required':True,'reasons':[]}},
    {'local_pipeline':{'quality':{'passed':False,'issues':[]}}},
    {'local_pipeline':{'manual_review':{'required':True,'reasons':[]}}},
    {'quality':{'passed':0,'issues':[]}}, {'manual_review':{'required':'false','reasons':[]}},
    {'quality':{'passed':False,'issues':['glare']},'manual_review':{'required':True,'reasons':[]}}])
def test_completed_fallback_cannot_promote_warning_or_unreviewed_source(environment,monkeypatch,empty_failure):
    from home_study_agent.document_learning import DocumentLearningService
    from home_study_agent.paper_writing_review_projection import validate_text
    settings,db=environment;gateway=PaperGateway()
    with TestClient(create_app(settings,model_gateway=gateway)) as client:
        sid,_,turn=read_turn(client,db,settings,monkeypatch,gateway)
        with db.connect() as c:
            revision=c.execute('SELECT revision_id FROM writing_ocr_confirmation WHERE confirmation_id=?',
                (turn['action']['writing_ocr_confirmation']['confirmation_id'],)).fetchone()[0]
            source=object.__new__(DocumentLearningService)._load(c,sid,revision)
            assert validate_text(source,source['text'],[],isolated_response=False)
            with pytest.raises(ValueError,match='PAPER_QUALITY_REQUIRES_RECAPTURE_OR_SPECIALIST_REVIEW'):
                validate_text({**source,'details_json':json.dumps(empty_failure)},source['text'],[],isolated_response=False)
            for patch in ({'artifact_status':'quality_warning'},{'review_required':1},
                {'ocr_confidence':.2,'artifact_confidence':.2},
                {'details_json':json.dumps({'quality':{'passed':False,'issues':['orientation_ambiguous']}})}):
                with pytest.raises(ValueError,match='PAPER_QUALITY_REQUIRES_RECAPTURE_OR_SPECIALIST_REVIEW'):
                    validate_text({**source,**patch},source['text'],[],isolated_response=False)
