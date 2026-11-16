"""Actual parent-purpose API and nonseed paper-to-formative HTTP, no network."""
import base64
import json
import textwrap

import cv2
import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.document_monitoring import DocumentMonitoringService
from home_study_agent.document_vision import DocumentVisionError, DocumentVisionService
from home_study_agent.local_ocr.service import RapidOcrEngine
from test_diagnostic_prompt_http import start
from test_paper_writing_runtime_http import PaperGateway
from test_voice_runtime_binding import capture, interpret
from test_writing_voice_runtime_http import readback_receipt
from test_writing_question_authorization import published_question, CHILD, LICENSE
from test_writing_question_evaluator import TEXT


def manuscript_page(monkeypatch):
    lines = textwrap.wrap(TEXT,68)
    pixels = np.full((1200,1000,3),230,dtype=np.uint8)
    for index,line in enumerate(lines):
        cv2.putText(pixels,line,(60,95+index*65),cv2.FONT_HERSHEY_SIMPLEX,.65,(0,0,0),2,cv2.LINE_AA)
    def recognize(self,image,route):
        height,width = image.shape[:2]
        return [{'text':line,'confidence':.99,'kind':'printed','box':[
            [int(width*.06),int(height*(75+index*65)/1200)], [int(width*.90),int(height*(75+index*65)/1200)],
            [int(width*.90),int(height*(100+index*65)/1200)], [int(width*.06),int(height*(100+index*65)/1200)]]}
            for index,line in enumerate(lines)]
    monkeypatch.setattr(RapidOcrEngine,'recognize',recognize)
    monkeypatch.setattr(RapidOcrEngine,'health',lambda self:{'ready':True,'route':'cpu','implicit_network_download':False})
    ok,png = cv2.imencode('.png',pixels)
    assert ok
    return png.tobytes(),'\n'.join(lines)


@pytest.mark.parametrize('permission',['absent','granted','revoked_before_evaluation','revoked_during_inference','revoked_response_ready'])
def test_actual_published_question_parent_authorization_paper_confirmation_and_evaluator(published_question,monkeypatch,permission):
    db,_,_,published,_ = published_question
    settings = Settings(data_dir=db.path.parent,database_path=db.path,maintenance_enabled=False)
    monkeypatch.setenv('HSC_DATA_DIR',str(settings.data_dir))
    monkeypatch.setenv('HSC_DATABASE_PATH',str(db.path))
    def no_page_upload(*args,**kwargs): raise DocumentVisionError('DOCUMENT_VISION_NOT_CONFIGURED')
    monkeypatch.setattr(DocumentVisionService,'analyze_page',no_page_upload)
    png,text = manuscript_page(monkeypatch)
    class Gateway(PaperGateway):
        before_return = None

        async def complete(self,request):
            result = await super().complete(request)
            if request.request_type=='learning_outcome_evaluation' and self.before_return:
                await self.before_return()
            return result

    gateway = Gateway()
    path = '/v1/parent/children/'+CHILD+'/writing-question-authorizations/'+published['diagnostic_item_id']
    application = create_app(settings,model_gateway=gateway)
    with TestClient(application) as client:
        parent = client.post('/v1/parent/setup',json={'pin':'8264'})
        assert parent.status_code==200,parent.text
        headers = {'X-HSC-Parent':parent.json()['parent_token']}
        privacy = client.put('/v1/parent/privacy',headers=headers,json={'cloud_model_text_allowed':True})
        assert privacy.status_code==200,privacy.text
        initial = client.get(path,headers=headers)
        assert initial.status_code==200 and initial.json()['status']=='authorization_required'
        if permission!='absent':
            grant = client.post(path+'/grant',headers=headers,json={
                **{key:initial.json()[key] for key in ('item_version','source_version','curriculum_version')},
                'expected_revision':0,'client_action_id':'parent-purpose','license_basis':LICENSE})
            assert grant.status_code==200 and grant.json()['current']['status']=='authorized',grant.text
        sid = start(client)
        # Same initial study-phase fixture as the existing paper HTTP tests.
        # No question, answer, OCR result or authorization is fabricated here.
        with db.connect() as c:
            c.execute("UPDATE daily_session SET state='study' WHERE session_id=?",(sid,))
            c.commit()
        response = client.post(f'/v1/sessions/{sid}/diagnostic-prompt/prepare',json={
            'track':'pet','preferred_item_id':published['diagnostic_item_id'],'client_action_id':'published-question'})
        assert response.status_code==200,response.text
        prompt = response.json()['prompt']
        assert prompt['diagnostic_item_id']==published['diagnostic_item_id']
        monitor = client.post(f'/v1/sessions/{sid}/document-monitor/start',json={'client_action_id':'published-monitor'})
        assert monitor.status_code==200,monitor.text
        page = client.post(f'/v1/sessions/{sid}/document-monitor/ocr',json={
            'image_base64':base64.b64encode(png).decode(),'client_action_id':'original-page'})
        assert page.status_code==200,page.text
        artifact = page.json()
        assert artifact['details']['quality']['passed'] and artifact['extracted_text']==text
        revision = DocumentMonitoringService(db).record_revision(sid,artifact['artifact_id'],'published-paper','paper-revision')
        def say(quote,key,control):
            gateway.quote,gateway.control = quote,control
            voice = capture(client,settings,monkeypatch,sid,quote,prompt,key+'-voice')
            return interpret(client,sid,quote,key,voice)
        say('作文写完了，帮我看看','published-look','writing.read_original')
        reading = say('这页只有我写的答案','published-scope','writing.read_original')
        assert reading['action']['type']=='read_writing_original',reading
        playback = readback_receipt(client,sid,reading)
        read = client.put(f"/v1/agent-turns/{reading['agent_turn_id']}/outcome",json={
            'final_response':'原稿已读回','tool_status':'completed','result':playback})
        assert read.status_code==200,read.text
        confirmed = say('对，是我写的，识别没有错','published-confirm','writing.confirm_original')
        submitted = client.post(f"/v1/agent-turns/{confirmed['agent_turn_id']}/execute")
        assert submitted.status_code==200,submitted.text
        follow = submitted.json()['turn']['local_evaluation']
        assert follow['status']==('local_feedback_required' if permission=='absent' else 'feedback_required'),follow
        assert not gateway.evaluations
        for request in gateway.requests:
            serialized = json.dumps(request.messages)
            assert initial.json()['preview']['question'] not in serialized
            assert all(line not in serialized for line in text.splitlines())
            assert all(value not in serialized for value in (CHILD,sid,prompt['prompt_id'],artifact['artifact_id'],revision['revision_id']))
        if permission=='absent':
            assert 'AUTHORIZATION_REQUIRED' in follow['reason']
            return
        if permission=='revoked_before_evaluation':
            revoked = client.post(path+'/revoke',headers=headers,json={'expected_revision':1,'client_action_id':'parent-revoke'})
            assert revoked.status_code==200,revoked.text
        if permission=='revoked_during_inference':
            async def revoke_during_inference():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application),base_url='http://testserver') as parent_client:
                    response = await parent_client.post(path+'/revoke',headers=headers,
                        json={'expected_revision':1,'client_action_id':'parent-late-revoke'})
                    assert response.status_code==200,response.text
            gateway.before_return = revoke_during_inference
        if permission=='revoked_response_ready':
            evaluator = application.state.learning_outcome_evaluator
            persist = evaluator._persist
            def crash_before_persist(*args): raise RuntimeError('synthetic crash after completed model call')
            monkeypatch.setattr(evaluator,'_persist',crash_before_persist)
        evaluated = client.post(f"/v1/agent-turns/{follow['feedback_turn_id']}/execute")
        if permission.startswith('revoked'):
            assert evaluated.status_code==409,evaluated.text
            if permission=='revoked_response_ready':
                with db.connect() as c:
                    assert c.execute("SELECT COUNT(*) FROM learning_evaluation_execution WHERE state='response_ready'").fetchone()[0]==1
                    assert c.execute("SELECT COUNT(*) FROM model_call WHERE purpose='learning_outcome_evaluation' AND status='completed'").fetchone()[0]==1
                revoked = client.post(path+'/revoke',headers=headers,json={'expected_revision':1,'client_action_id':'parent-recovery-revoke'})
                assert revoked.status_code==200,revoked.text
                monkeypatch.setattr(evaluator,'_persist',persist)
                replay = client.post(f"/v1/agent-turns/{follow['feedback_turn_id']}/execute")
                assert replay.status_code==409,replay.text
            assert len(gateway.evaluations)==(0 if permission=='revoked_before_evaluation' else 1)
            with db.connect() as c:
                assert c.execute('SELECT COUNT(*) FROM rubric_assessment').fetchone()[0]==0
                assert c.execute('SELECT COUNT(*) FROM agent_outcome WHERE outcome_level IN (\'understood_now\',\'independent\',\'transferred\',\'retained\')').fetchone()[0]==0
            return
        assert evaluated.status_code==200,evaluated.text
        result = evaluated.json()['execution']['result']
        assert result['formative_only'] and not result['automatic_certification']
        assert result['outcome_level'] not in {'independent','transferred','retained'}
        assert len(gateway.evaluations)==1
        outbound = gateway.evaluations[0]
        body = json.loads(outbound.messages[1]['content'])
        assert set(body)=={'question','response','rubric'} and body['response']==text
        assert body['question']==initial.json()['preview']['question']
        for private in (CHILD,sid,prompt['prompt_id'],artifact['artifact_id'],revision['revision_id'],
            base64.b64encode(png).decode(),'knowledge_node_id','standard_quote','license_basis','local_pipeline'):
            assert private not in json.dumps(outbound.messages)
    with TestClient(create_app(settings,model_gateway=gateway)) as client:
        replay = client.post(f"/v1/agent-turns/{follow['feedback_turn_id']}/execute")
        assert replay.status_code==200,replay.text
        assert len(gateway.evaluations)==1
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM rubric_assessment').fetchone()[0]==1
            assert c.execute('SELECT COUNT(*) FROM rubric_review_event').fetchone()[0]==0
            assert json.loads(c.execute('SELECT details_json FROM capture_artifact WHERE artifact_id=?',
                (artifact['artifact_id'],)).fetchone()[0])==artifact['details']
