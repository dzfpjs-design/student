"""The quality-warning exception is purpose-bound, never the default identity gate."""
import base64
from dataclasses import replace
import json
import pytest
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.document_learning import DocumentLearningService
from home_study_agent.paper_ocr_metadata import validate_capture_identity
from home_study_agent.writing_ocr_authorization import _document
from test_diagnostic_prompt_http import environment, start
from test_paper_writing_runtime_http import PaperGateway, synthetic_page
from test_writing_voice_runtime_http import writing_prompt
from test_writing_voice_runtime_http import capture, interpret
from test_writing_ocr_warning_voice import finish_read
from home_study_agent.tutor_ocr_evidence import evaluate_tutor_ocr_evidence


def test_unreviewed_warning_stays_ineligible_for_ordinary_consumers(environment, monkeypatch):
    settings,db=environment
    settings=replace(settings,data_encryption_key=b'W'*32)
    png,_=synthetic_page(monkeypatch)
    with TestClient(create_app(settings,model_gateway=PaperGateway())) as client:
        sid=start(client)
        writing_prompt(client,db,sid)
        root=f'/v1/sessions/{sid}/document-monitor'
        assert client.post(root+'/start',json={'client_action_id':'ordinary-monitor'}).status_code==200
        artifact=client.post(root+'/ocr',json={'image_base64':base64.b64encode(png).decode(),'client_action_id':'ordinary-ocr'})
        assert artifact.status_code==200,artifact.text
        identity=artifact.json()['artifact_id']
        revision=client.post(root+'/revisions',json={'artifact_id':identity,'content_signature':'ordinary-page',
            'client_action_id':'ordinary-revision'})
        assert revision.status_code==200,revision.text
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            reader=object.__new__(DocumentLearningService)
            source=reader._load(c,sid,revision.json()['revision_id'])
            details=json.loads(source['details_json'])
            assert validate_capture_identity(c,source,details)['status']=='completed'
            c.execute("UPDATE capture_artifact SET status='quality_warning',confidence=.49 WHERE artifact_id=?",(identity,))
            c.execute('UPDATE document_page_revision SET review_required=1,ocr_confidence=.49 WHERE artifact_id=?',(identity,))
            source=reader._load(c,sid,revision.json()['revision_id'])
            with pytest.raises(ValueError,match='PAPER_VISUAL_SOURCE_CHANGED'):
                validate_capture_identity(c,source,details)
            with pytest.raises(ValueError,match='PAPER_WRITING_REVIEW_REQUIRED'):
                validate_capture_identity(c,source,details,allow_quality_warning=True)
            with pytest.raises(ValueError,match='DOCUMENT_REVIEW_REQUIRED'):
                _document(c,source['child_id'],sid,identity,source['revision_id'],0,len(source['text']))
            checked,lines,_=reader._local(source)
            assert checked['status']=='blocked' and not lines
            assert checked['reason']=='ocr_unreliable_need_clearer_page_or_visual_capability'
            raw=dict(c.execute('SELECT * FROM capture_artifact WHERE artifact_id=?',(identity,)).fetchone())
            assert evaluate_tutor_ocr_evidence(raw,'confirmed').code=='OCR_REVIEW_REQUIRED'
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0]==0
            assert c.execute('SELECT count(*) FROM writing_ocr_event').fetchone()[0]==0
            c.rollback()


def test_purpose_opt_in_rechecks_real_review_and_never_grants_tutor_or_ordinary_ocr(environment,monkeypatch):
    settings,db=environment
    settings=replace(settings,data_encryption_key=b'W'*32)
    png,_=synthetic_page(monkeypatch)
    gateway=PaperGateway()
    with TestClient(create_app(settings,model_gateway=gateway)) as client:
        sid=start(client);prompt=writing_prompt(client,db,sid)
        root=f'/v1/sessions/{sid}/document-monitor'
        assert client.post(root+'/start',json={'client_action_id':'purpose-monitor'}).status_code==200
        result=client.post(root+'/ocr',json={'image_base64':base64.b64encode(png).decode(),'client_action_id':'purpose-ocr'})
        assert result.status_code==200,result.text
        identity=result.json()['artifact_id']
        revision=client.post(root+'/revisions',json={'artifact_id':identity,'content_signature':'purpose-page',
            'client_action_id':'purpose-revision'}).json()['revision_id']
        with db.connect() as c:
            c.execute("UPDATE capture_artifact SET status='quality_warning',confidence=.49 WHERE artifact_id=?",(identity,))
            c.execute('UPDATE document_page_revision SET review_required=1,ocr_confidence=.49 WHERE revision_id=?',(revision,))
            c.commit()
        def say(quote,key,control):
            gateway.quote,gateway.control=quote,control
            voice=capture(client,settings,monkeypatch,sid,quote,prompt,key+'-voice')
            return interpret(client,sid,quote,key,voice)
        say('作文写好了，帮我看看','purpose-look','writing.read_original')
        reading=say('这页只有我写的答案','purpose-scope','writing.read_original')
        finish_read(client,sid,reading,'purpose-read')
        confirmed=say('是我自己写的，刚读出的文字没有错','purpose-confirm','writing.confirm_original')
        submitted=client.post(f"/v1/agent-turns/{confirmed['agent_turn_id']}/execute")
        assert submitted.status_code==200,submitted.text
        with db.connect() as c:
            c.execute('BEGIN')
            reader=object.__new__(DocumentLearningService)
            source=reader._load(c,sid,revision);details=json.loads(source['details_json'])
            event=c.execute("SELECT event_id,confirmation_id FROM writing_ocr_event WHERE kind='authorize'").fetchone()
            review={'prompt_id':prompt['prompt_id'],'item_version':prompt['item_version'],
                'confirmation_id':event['confirmation_id'],'use_authorization_id':event['event_id']}
            assert validate_capture_identity(c,source,details,allow_quality_warning=True,writing_review=review)['status']=='completed'
            # The same reviewed page is still not ordinary OCR/Tutor content.
            with pytest.raises(ValueError,match='PAPER_VISUAL_SOURCE_CHANGED'):
                validate_capture_identity(c,source,details)
            assert reader._local(source)[0]['status']=='blocked'
            raw=dict(c.execute('SELECT * FROM capture_artifact WHERE artifact_id=?',(identity,)).fetchone())
            assert evaluate_tutor_ocr_evidence(raw,'confirmed').code=='OCR_REVIEW_REQUIRED'
            for field in ('prompt_id','item_version','confirmation_id','use_authorization_id'):
                with pytest.raises(ValueError):
                    validate_capture_identity(c,source,details,allow_quality_warning=True,writing_review={**review,field:'unrelated'})
            for field in ('child_id','session_id','artifact_id','revision_id'):
                with pytest.raises(ValueError):
                    validate_capture_identity(c,{**source,field:'unrelated'},details,allow_quality_warning=True,writing_review=review)
            for operation in ("UPDATE writing_ocr_event SET created_at='2999-01-01T00:00:00+00:00' WHERE kind='authorize'",
                "DELETE FROM writing_ocr_event WHERE kind='ended'", "DELETE FROM writing_ocr_event WHERE kind='authorize'",
                "UPDATE ocr_job SET publication_started=0", "UPDATE ocr_job SET image_hash='wrong-frame'",
                "UPDATE capture_artifact SET extracted_text='changed original' WHERE artifact_type='document_ocr'"):
                c.execute('SAVEPOINT changed_source');c.execute(operation)
                with pytest.raises(ValueError):
                    validate_capture_identity(c,source,details,allow_quality_warning=True,writing_review=review)
                c.execute('ROLLBACK TO changed_source');c.execute('RELEASE changed_source')
            for invalid in (1,'true',None):
                with pytest.raises(ValueError,match='PAPER_WRITING_REVIEW_REQUIRED'):
                    validate_capture_identity(c,source,details,allow_quality_warning=invalid,writing_review=review)
            c.rollback()
