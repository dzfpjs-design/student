"""Real OCR serialization/HTTP/runtime; synthetic pixels, ASR and no-network model."""
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
from home_study_agent.model_gateway import ModelResult
from test_diagnostic_prompt_http import environment, start
from test_voice_runtime_binding import capture, interpret
from test_writing_voice_runtime_http import writing_prompt, readback_receipt


class PaperGateway:
    configured = True

    def __init__(self):
        self.quote, self.control = '', 'writing.read_original'
        self.requests, self.evaluations = [], []

    async def complete(self, request):
        self.requests.append(request)
        if request.request_type == 'voice_interaction':
            output = {'role':'control','control':self.control,'quote':self.quote,'confidence':.99}
        else:
            assert request.request_type == 'learning_outcome_evaluation'
            self.evaluations.append(request)
            payload = json.loads(request.messages[1]['content'])
            assert set(payload) == {'question','response','rubric'}
            output = {'understanding':'independent','confidence':.99,'evidence_summary':'Synthetic formative evidence',
                'spoken_feedback':'Make the meeting time easier to find.','next_check':'Revise the meeting time sentence.',
                'rubric_dimensions':[{'dimension_id':d['dimension_id'],'score':2,'response_quotes':[payload['response']],
                    'rationale':'Grounded in the original response'} for d in payload['rubric']['dimensions']],
                'criterion_evidence':[{'criterion':point,'response_quote':payload['response']} for point in payload['rubric']['task_points']]}
            from formative_response_fixture import criterion_reply
            output=criterion_reply(payload,output)
        return ModelResult(content=json.dumps(output),provider='fixture',model='fixture',latency_ms=1)


def synthetic_page(monkeypatch, numbered=False):
    text = ('Dear Alex, please meet me at the library on Saturday morning at ten. '
            'We will read English stories and practise new words together. Bring your workbook and a pencil. '
            'I will bring some short stories to share. See you soon, Mia.')
    # The numbered variant isolates downstream metadata/feedback checks. It
    # does not replace the ordinary unnumbered manuscript regression.
    lines = textwrap.wrap(('1. ' if numbered else '')+text,68)
    image = np.full((1200,1000,3),230,dtype=np.uint8)
    for index,line in enumerate(lines):
        cv2.putText(image,line,(60,95+index*65),cv2.FONT_HERSHEY_SIMPLEX,.65,(0,0,0),2,cv2.LINE_AA)
    success,png = cv2.imencode('.png',image)
    assert success
    def recognize(self, image, route):
        height,width = image.shape[:2]
        return [{'text':line,'confidence':.99,'kind':'printed',
            'box':[[int(width*.06),int(height*((75+index*65)/1200))],
                   [int(width*.90),int(height*((75+index*65)/1200))],
                   [int(width*.90),int(height*((100+index*65)/1200))],
                   [int(width*.06),int(height*((100+index*65)/1200))]]} for index,line in enumerate(lines)]
    # Only recognizer inference is substituted. Quality, layout, queue, journal,
    # artifact metadata and monitor publication use production code.
    monkeypatch.setattr(RapidOcrEngine,'recognize',recognize)
    monkeypatch.setattr(RapidOcrEngine,'health',lambda self:{'ready':True,'route':'cpu','implicit_network_download':False})
    return png.tobytes(), '\n'.join(lines)


@pytest.mark.parametrize('consent',[True,False])
@pytest.mark.parametrize('numbered',[False,True],ids=['plain_manuscript','numbered_manuscript'])
def test_actual_paper_confirmation_formative_feedback_revision_and_restart(environment,monkeypatch,consent,numbered):
    settings,db = environment
    gateway = PaperGateway()
    png,manuscript = synthetic_page(monkeypatch,numbered)
    def no_page_upload(*args,**kwargs):
        raise DocumentVisionError('DOCUMENT_VISION_NOT_CONFIGURED')
    monkeypatch.setattr(DocumentVisionService,'analyze_page',no_page_upload)
    with TestClient(create_app(settings,model_gateway=gateway)) as client:
        if consent:
            parent = client.post('/v1/parent/setup',json={'pin':'8264'})
            assert parent.status_code==200, parent.text
            granted = client.put('/v1/parent/privacy',headers={'X-HSC-Parent':parent.json()['parent_token']},
                json={'cloud_model_text_allowed':True})
            assert granted.status_code==200, granted.text
        sid = start(client)
        prompt = writing_prompt(client,db,sid)
        monitor = client.post(f'/v1/sessions/{sid}/document-monitor/start',json={'client_action_id':'paper-monitor'})
        assert monitor.status_code==200, monitor.text
        ocr = client.post(f'/v1/sessions/{sid}/document-monitor/ocr',json={
            'image_base64':base64.b64encode(png).decode(),'client_action_id':'paper-ocr'})
        assert ocr.status_code==200, ocr.text
        artifact = ocr.json()
        assert artifact['extracted_text']==manuscript
        assert artifact['details']['quality']['passed'], artifact['details']['quality']
        assert 'local_pipeline' in artifact['details'] and 'monitor_generation' in artifact['details']
        assert not artifact['local_path']
        fallback = artifact['details'].get('cloud_vision_fallback')
        assert not fallback or fallback['status']=='blocked'
        with db.connect() as c:
            stored=c.execute('SELECT details_json FROM capture_artifact WHERE artifact_id=?',
                (artifact['artifact_id'],)).fetchone()[0]
            assert json.loads(stored)==artifact['details']
        if not numbered:
            pipeline=artifact['details']['local_pipeline']
            assert pipeline['manual_review']['required']
            assert all(b['block_kind']=='fragment' and b['needs_manual_boundary_check']
                and 'missing_question_start' in b['boundary_review_reasons'] for b in pipeline['question_blocks'])
            assert not pipeline['wrong_question_candidates']
            rejected=client.post(f"/v1/capture-artifacts/{artifact['artifact_id']}/mistake-candidates/prepare",
                json={'child_id':artifact['child_id'],'client_action_id':'paper-is-not-question'})
            assert rejected.status_code==422, rejected.text
        revision = DocumentMonitoringService(db).record_revision(sid,artifact['artifact_id'],'paper-signature','paper-revision')
        def say(text,key,control,source=prompt):
            gateway.quote,gateway.control = text,control
            voice = capture(client,settings,monkeypatch,sid,text,source,key+'-voice')
            return interpret(client,sid,text,key,voice)
        say('作文写完了，帮我看看','paper-look','writing.read_original')
        reading = say('这页只有我写的答案','paper-scope','writing.read_original')
        assert reading['action']['type']=='read_writing_original', {
            'action':reading['action']['type'],'reply':reading.get('response_text'),
            'confidence':artifact['confidence'],'quality':artifact['details']['quality']}
        playback = readback_receipt(client,sid,reading)
        read = client.put(f"/v1/agent-turns/{reading['agent_turn_id']}/outcome",json={
            'final_response':'原稿已读回','tool_status':'completed','result':playback})
        assert read.status_code==200, read.text
        confirmation = say('yes','paper-confirm','writing.confirm_original')
        submitted = client.post(f"/v1/agent-turns/{confirmation['agent_turn_id']}/execute")
        assert submitted.status_code==200, submitted.text
        follow = submitted.json()['turn']['local_evaluation']
        assert follow['status']==('feedback_required' if consent else 'local_feedback_required'), {
            k:follow.get(k) for k in ('status','reason')}
        with db.connect() as c:
            attempt = c.execute('SELECT submitted_answer FROM diagnostic_attempt').fetchone()
            assert attempt['submitted_answer']==manuscript and attempt['submitted_answer']!='yes'
        for request in gateway.requests:
            # Check whole OCR lines, not newline-containing prose inside JSON
            # where escaping could make an accidental disclosure pass this test.
            assert all(line not in json.dumps(request.messages) for line in manuscript.splitlines())
            assert all(value not in json.dumps(request.messages) for value in (
                sid,prompt['prompt_id'],artifact['artifact_id'],revision['revision_id'],'local_pipeline',
                base64.b64encode(png).decode()))
        if not consent:
            local = client.post(f"/v1/agent-turns/{follow['local_feedback_turn_id']}/execute")
            assert local.status_code==200, local.text
            assert not gateway.evaluations
            assert all(line not in json.dumps(r.messages)
                for r in gateway.requests for line in manuscript.splitlines())
            return
        evaluated = client.post(f"/v1/agent-turns/{follow['feedback_turn_id']}/execute")
        assert evaluated.status_code==200, evaluated.text
        assessment = evaluated.json()['execution']['result']
        assert assessment['formative_only'] and not assessment['automatic_certification']
        assert assessment['certification_status']=='candidate'
        assert assessment['outcome_level'] not in {'independent','transferred','retained'}
        assert len(gateway.evaluations)==1
        request = gateway.evaluations[0]
        assert json.loads(request.messages[1]['content'])['response']==manuscript
        assert set(request.context_manifest)=={'purpose','raw_audio_or_video','fields','redaction_count'}
        for value in (sid,prompt['prompt_id'],artifact['artifact_id'],revision['revision_id'],'local_pipeline','local_audit',base64.b64encode(png).decode()):
            assert value not in json.dumps(request.messages)
        chosen = say('我想按刚才的反馈自己改一遍','paper-revise','writing.revise_original')
        assert chosen.get('runtime_capability')=='writing.revise_original', chosen
        prepared = client.post(f"/v1/agent-turns/{chosen['agent_turn_id']}/execute")
        assert prepared.status_code==200, prepared.text
        next_prompt = prepared.json()['execution']['result']['prompt']
        assert next_prompt['prompt_id']!=prompt['prompt_id']
        assert next_prompt['diagnostic_item_id']==prompt['diagnostic_item_id']
    with TestClient(create_app(settings,model_gateway=gateway)) as client:
        replay = client.post(f"/v1/agent-turns/{follow['feedback_turn_id']}/execute")
        assert replay.status_code==200, replay.text
        replay = client.post(f"/v1/agent-turns/{chosen['agent_turn_id']}/execute")
        assert replay.status_code==200, replay.text
        assert replay.json()['execution']['result']['prompt']['prompt_id']==next_prompt['prompt_id']
        assert len(gateway.evaluations)==1
        with db.connect() as c:
            original=c.execute('SELECT details_json FROM capture_artifact WHERE artifact_id=?',
                (artifact['artifact_id'],)).fetchone()[0]
            assert json.loads(original)==artifact['details']
            wrapper=c.execute("SELECT details_json,extracted_text FROM capture_artifact WHERE artifact_type='writing_original'").fetchone()
            assert json.loads(wrapper['details_json'])['origin']['kind']=='confirmed_document'
            assert wrapper['extracted_text'] is None
            assert c.execute('SELECT COUNT(*) FROM rubric_assessment').fetchone()[0]==1
            assert c.execute('SELECT COUNT(*) FROM rubric_review_event').fetchone()[0]==0
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==1
            assert c.execute('SELECT COUNT(*) FROM writing_followup').fetchone()[0]==1
