"""Two synthetic paper drafts through actual OCR/HTTP/Runtime and evaluator."""
import base64
import json
import textwrap

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.document_monitoring import DocumentMonitoringService
from home_study_agent.document_vision import DocumentVisionError, DocumentVisionService
from home_study_agent.local_ocr.service import RapidOcrEngine
from test_diagnostic_prompt_http import environment, start
from test_paper_writing_runtime_http import PaperGateway, synthetic_page
from test_voice_runtime_binding import capture, interpret
from test_writing_voice_runtime_http import writing_prompt, readback_receipt


def revised_page(monkeypatch, kind, *, text=None):
    text = text or (('Dear Alex, let us meet at the library at ten this Saturday morning. We will study English and practise '
            'reading stories. Bring your notebook and pencil. We can finish our homework together. Best wishes, Mia.') if kind == 'revision' else (
            'Dear Mia, thank you for the wonderful book. I really liked the funny characters and the surprising ending. '
            'I will return it at school on Monday morning. I hope we can talk about the story then. Best wishes, Alex.'))
    lines = textwrap.wrap(text,68)
    image = np.full((1200,1000,3),230,dtype=np.uint8)
    for index,line in enumerate(lines):
        cv2.putText(image,line,(60,95+index*65),cv2.FONT_HERSHEY_SIMPLEX,.65,(0,0,0),2,cv2.LINE_AA)
    def recognize(self, image, route):
        height,width = image.shape[:2]
        return [{'text':line,'confidence':.99,'kind':'printed','box':[
            [int(width*.06),int(height*(75+index*65)/1200)],
            [int(width*.90),int(height*(75+index*65)/1200)],
            [int(width*.90),int(height*(100+index*65)/1200)],
            [int(width*.06),int(height*(100+index*65)/1200)]]} for index,line in enumerate(lines)]
    monkeypatch.setattr(RapidOcrEngine,'recognize',recognize)
    ok,png = cv2.imencode('.png',image)
    assert ok
    return png.tobytes(),'\n'.join(lines)


@pytest.mark.parametrize('kind',['revision','independent_check'])
def test_two_confirmed_paper_drafts_reach_second_formative_assessment_and_restart(environment, monkeypatch, kind):
    settings,db = environment
    gateway = PaperGateway()
    def no_page_upload(*args,**kwargs):
        raise DocumentVisionError('DOCUMENT_VISION_NOT_CONFIGURED')
    monkeypatch.setattr(DocumentVisionService,'analyze_page',no_page_upload)
    stored_pages = []
    with TestClient(create_app(settings,model_gateway=gateway)) as client:
        parent = client.post('/v1/parent/setup',json={'pin':'8264'})
        assert parent.status_code==200,parent.text
        consent = client.put('/v1/parent/privacy',headers={'X-HSC-Parent':parent.json()['parent_token']},
            json={'cloud_model_text_allowed':True})
        assert consent.status_code==200,consent.text
        sid = start(client)
        prompt = writing_prompt(client,db,sid)
        monitor = client.post(f'/v1/sessions/{sid}/document-monitor/start',json={'client_action_id':'two-draft-monitor'})
        assert monitor.status_code==200,monitor.text

        def say(text,key,control,target):
            gateway.quote,gateway.control = text,control
            voice = capture(client,settings,monkeypatch,sid,text,target,key+'-voice')
            return interpret(client,sid,text,key,voice)

        def submit_page(target, index, png, manuscript):
            prefix = 'draft-'+str(index)
            response = client.post(f'/v1/sessions/{sid}/document-monitor/ocr',json={
                'image_base64':base64.b64encode(png).decode(),'client_action_id':prefix+'-ocr'})
            assert response.status_code==200,response.text
            artifact = response.json()
            assert artifact['extracted_text']==manuscript and artifact['details']['quality']['passed']
            assert not artifact['local_path']
            stored_pages.append((artifact['artifact_id'],artifact['details']))
            DocumentMonitoringService(db).record_revision(sid,artifact['artifact_id'],prefix+'-signature',prefix+'-revision')
            say('作文写完了，帮我看看',prefix+'-look','writing.read_original',target)
            reading = say('这页只有我写的答案',prefix+'-scope','writing.read_original',target)
            assert reading['action']['type']=='read_writing_original',reading
            receipt = readback_receipt(client,sid,reading,playback_id=prefix+'-read')
            read = client.put(f"/v1/agent-turns/{reading['agent_turn_id']}/outcome",json={
                'final_response':'原稿已读回','tool_status':'completed','result':receipt})
            assert read.status_code==200,read.text
            confirmation = say('对，是我写的，识别没有错',prefix+'-confirm','writing.confirm_original',target)
            submitted = client.post(f"/v1/agent-turns/{confirmation['agent_turn_id']}/execute")
            assert submitted.status_code==200,submitted.text
            follow = submitted.json()['turn']['local_evaluation']
            assert follow['status']=='feedback_required', {k:follow.get(k) for k in ('status','reason')}
            evaluated = client.post(f"/v1/agent-turns/{follow['feedback_turn_id']}/execute")
            assert evaluated.status_code==200,evaluated.text
            outcome = evaluated.json()['execution']['result']
            assert outcome['formative_only'] and outcome['certification_status']=='candidate'
            assert outcome['outcome_level'] not in {'independent','transferred','retained'}
            assert json.loads(gateway.evaluations[-1].messages[1]['content'])['response']==manuscript
            return follow,confirmation

        png,first_text = synthetic_page(monkeypatch)
        first,_ = submit_page(prompt,1,png,first_text)
        quote,cap = ('我想按刚才的反馈自己改一遍','writing.revise_original') if kind=='revision' else (
            '再来一道不一样的，我自己写','writing.independent_check')
        chosen = say(quote,'formal-followup',cap,prompt)
        assert chosen.get('runtime_capability')==cap,chosen
        prepared = client.post(f"/v1/agent-turns/{chosen['agent_turn_id']}/execute")
        assert prepared.status_code==200,prepared.text
        target = prepared.json()['execution']['result']['prompt']
        assert target['prompt_id']!=prompt['prompt_id']
        assert (target['diagnostic_item_id']==prompt['diagnostic_item_id'])==(kind=='revision')
        png,second_text = revised_page(monkeypatch,kind)
        second,confirmed = submit_page(target,2,png,second_text)
        assert second['feedback_turn_id']!=first['feedback_turn_id'] and len(gateway.evaluations)==2
        for request in gateway.evaluations:
            assert set(json.loads(request.messages[1]['content']))=={'question','response','rubric'}
            assert all(value not in json.dumps(request.messages) for value in
                (sid,prompt['prompt_id'],target['prompt_id'],'local_pipeline','local_audit','source_objective_id'))
    with TestClient(create_app(settings,model_gateway=gateway)) as client:
        for turn in (confirmed['agent_turn_id'],second['feedback_turn_id']):
            replay = client.post(f'/v1/agent-turns/{turn}/execute')
            assert replay.status_code==200,replay.text
        assert len(gateway.evaluations)==2
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM rubric_assessment').fetchone()[0]==2
            assert c.execute('SELECT COUNT(*) FROM rubric_review_event').fetchone()[0]==0
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==2
            assert c.execute('SELECT COUNT(*) FROM writing_followup').fetchone()[0]==1
            rows = c.execute("SELECT details_json,extracted_text FROM capture_artifact WHERE artifact_type='writing_original'").fetchall()
            assert len(rows)==2 and all(json.loads(r[0])['origin']['kind']=='confirmed_document' and r[1] is None for r in rows)
            for artifact_id,metadata in stored_pages:
                assert json.loads(c.execute('SELECT details_json FROM capture_artifact WHERE artifact_id=?',
                    (artifact_id,)).fetchone()[0])==metadata
