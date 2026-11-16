"""Actual local pixels/producer/HTTP; synthetic OCR, ASR and declared gateway."""
import base64
from dataclasses import replace
import json
import textwrap

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.document_vision import DiagramRegionGate, DocumentVisionError, DocumentVisionService
from home_study_agent.local_ocr.service import RapidOcrEngine
from test_diagnostic_prompt_http import environment, start
from test_document_vision import RegionEngine
from test_paper_writing_runtime_http import PaperGateway
from test_voice_runtime_binding import capture, interpret
from test_writing_voice_runtime_http import writing_prompt, readback_receipt

ANSWER = ('Dear Alex, please meet me at the library on Saturday morning at ten. '
          'We will read English stories together. Bring your workbook and a pencil. '
          'I will bring some short stories to share. See you soon, Mia.')


def page(monkeypatch, *, prefix='', suffix='', rotate=0, perspective=False, overlap=False):
    lines=textwrap.wrap(prefix+ANSWER,58)+([suffix] if suffix else [])
    image=np.full((1400,1000,3),50,np.uint8)
    cv2.rectangle(image,(80,60),(920,1340),(230,230,230),-1)
    for index,line in enumerate(lines):
        cv2.putText(image,line,(120,160+index*64),cv2.FONT_HERSHEY_SIMPLEX,.65,(0,0,0),2,cv2.LINE_AA)
    cv2.rectangle(image,(280,900),(650,1160),(0,0,0),3)
    if perspective:
        transform=cv2.getPerspectiveTransform(np.float32([[0,0],[999,0],[999,1399],[0,1399]]),
            np.float32([[35,25],[955,50],[990,1360],[10,1370]]))
        image=cv2.warpPerspective(image,transform,(1000,1400),borderValue=(50,50,50))
    if rotate:
        image=cv2.rotate(image,{90:cv2.ROTATE_90_CLOCKWISE,180:cv2.ROTATE_180,270:cv2.ROTATE_90_COUNTERCLOCKWISE}[rotate])
    def recognize(self,image,route):
        h,w=image.shape[:2]
        ink=cv2.cvtColor(image,cv2.COLOR_BGR2GRAY)<100
        ink[:10]=False;ink[:, :10]=False;ink[:, -10:]=False;ink[int(h*.55):]=False
        ys=np.flatnonzero(ink.any(axis=1))
        groups=[g for g in np.split(ys,np.where(np.diff(ys)>2)[0]+1) if len(g)]
        if len(groups)!=len(lines):return []
        result=[]
        for index,(text,g) in enumerate(zip(lines,groups)):
            top,bottom=int(g[0]),int(g[-1]);xs=np.flatnonzero(ink[top:bottom+1].any(axis=0))
            left,right=int(xs[0]),int(xs[-1])
            if overlap:
                # Fault injection only in declared OCR inference: plausible text
                # mislocated onto the real prepared diagram must not be accepted.
                left,right,top,bottom=250,450,900+index*20,910+index*20
            result.append(dict(text=text,confidence=.99,kind='printed',box=[[left,top],[right,top],[right,bottom],[left,bottom]]))
        return result
    monkeypatch.setattr(RapidOcrEngine,'recognize',recognize)
    monkeypatch.setattr(RapidOcrEngine,'recognize_page_orientation',recognize)
    monkeypatch.setattr(RapidOcrEngine,'health',lambda self:dict(ready=True,route='cpu',implicit_network_download=False))
    return cv2.imencode('.png',image)[1].tobytes(),'\n'.join(lines)


@pytest.mark.parametrize('case',['ready','rotated90','perspective','overlap','partial_line','visual_dependency','private_suffix','unrelated_caption','retract_before','retract_inflight'])
def test_actual_prepared_page_confirmed_answer_feedback_restart(environment,monkeypatch,case):
    settings,db=environment
    settings=replace(settings,data_encryption_key=b'R'*32)
    raw,answer=page(monkeypatch,prefix='As shown in the figure, ' if case=='visual_dependency' else '',
        suffix='email: student@example.test' if case=='private_suffix' else ('See the diagram below.' if case=='unrelated_caption' else ''),
        rotate=90 if case=='rotated90' else 0,perspective=case=='perspective',overlap=case=='overlap')
    gateway=PaperGateway()
    def no_cloud(*a,**k):raise DocumentVisionError('DOCUMENT_VISION_NOT_CONFIGURED')
    monkeypatch.setattr(DocumentVisionService,'analyze_page',no_cloud)
    with TestClient(create_app(settings,model_gateway=gateway)) as client:
        parent=client.post('/v1/parent/setup',json={'pin':'8264'}).json()
        assert client.put('/v1/parent/privacy',headers={'X-HSC-Parent':parent['parent_token']},
            json={'cloud_model_text_allowed':True}).status_code==200
        client.app.state.visual_capture.detector.gate=DiagramRegionGate(RegionEngine(''))
        sid=start(client);prompt=writing_prompt(client,db,sid)
        root=f'/v1/sessions/{sid}/document-monitor'
        assert client.post(root+'/start',json={'client_action_id':'region-monitor'}).status_code==200
        response=client.post(root+'/ocr',json={'image_base64':base64.b64encode(raw).decode(),'client_action_id':'region-ocr'})
        assert response.status_code==200,response.text
        artifact=response.json()
        assert artifact['details']['visual_capture']['status']=='prepared',artifact['details']['visual_capture']
        revision=client.post(root+'/revisions',json={'artifact_id':artifact['artifact_id'],
            'content_signature':'real-text-and-diagram','client_action_id':'region-revision'})
        assert revision.status_code==200,revision.text
        assert len(revision.json()['visual_capture']['items'])==1
        def say(text,key,control):
            gateway.quote,gateway.control=text,control
            voice=capture(client,settings,monkeypatch,sid,text,prompt,key+'-voice')
            return interpret(client,sid,text,key,voice)
        say('作文写完了，帮我看看','region-look','writing.read_original')
        sentence='这页只有我写的答案'
        selected=answer
        if case=='partial_line':
            selected=answer[answer.index('Alex,'):]
            sentence='从“Alex,”开始到“Mia.”结束'
        elif case in {'private_suffix','unrelated_caption'}:
            selected=answer.rsplit('\n',1)[0]
            sentence='从“Dear Alex,”开始到“Mia.”结束'
        reading=say(sentence,'region-read','writing.read_original')
        assert reading['action']['type']=='read_writing_original',{'turn':reading,'quality':artifact['details']['quality']}
        playback=readback_receipt(client,sid,reading)
        assert client.put(f"/v1/agent-turns/{reading['agent_turn_id']}/outcome",json={
            'final_response':'原稿已读回','tool_status':'completed','result':playback}).status_code==200
        confirm=say('yes','region-confirm','writing.confirm_original')
        submitted=client.post(f"/v1/agent-turns/{confirm['agent_turn_id']}/execute")
        assert submitted.status_code==200,submitted.text
        follow=submitted.json()['turn']['local_evaluation']
        if case in {'partial_line','visual_dependency','private_suffix','overlap'}:
            assert follow['status']=='local_feedback_required',follow
            assert not gateway.evaluations
            reason=follow['reason']
            assert {'partial_line':'PARTIAL_LINE','visual_dependency':'VISUAL_DEPENDENCY','private_suffix':'PRIVATE','overlap':'OVERLAP'}[case] in reason.upper(),reason
            return
        assert follow['status']=='feedback_required',{'status':follow['status'],'reason':follow.get('reason')}
        if case in {'retract_before','retract_inflight'}:
            binding=reading['action']['writing_ocr_confirmation']
            def retract():
                from home_study_agent.writing_ocr_authorization import WritingOcrAuthorizationService
                WritingOcrAuthorizationService(db).retract(sid,**binding,client_action_id='retract-region-original')
            if case=='retract_before':retract()
            else:
                original_complete=gateway.complete
                async def complete(request):
                    output=await original_complete(request)
                    if request.request_type=='learning_outcome_evaluation':retract()
                    return output
                gateway.complete=complete
        evaluated=client.post(f"/v1/agent-turns/{follow['feedback_turn_id']}/execute")
        if case in {'retract_before','retract_inflight'}:
            with db.connect() as c:
                assert c.execute('SELECT count(*) FROM rubric_assessment').fetchone()[0]==0
            assert len(gateway.evaluations)==(0 if case=='retract_before' else 1)
            return
        assert evaluated.status_code==200,evaluated.text
        result=evaluated.json()['execution']['result']
        assert result['formative_only'] and not result['automatic_certification']
        assert len(gateway.evaluations)==1
        payload=json.loads(gateway.evaluations[0].messages[1]['content'])
        assert set(payload)=={'question','response','rubric'} and payload['response']==selected
        if case=='unrelated_caption':assert 'See the diagram below.' not in json.dumps(gateway.evaluations[0].messages)
        serialized=json.dumps(gateway.evaluations[0].messages)
        for value in ('visual_materials','visual_capture','region_xyxy','facts','text_geometry',artifact['artifact_id'],base64.b64encode(raw).decode()):
            assert value not in serialized
        with db.connect() as c:
            original=c.execute('SELECT details_json FROM capture_artifact WHERE artifact_id=?',(artifact['artifact_id'],)).fetchone()[0]
            assert json.loads(original)['visual_capture']['cloud_transmission_allowed'] is False
            assert json.loads(original)['visual_materials']
    with TestClient(create_app(settings,model_gateway=gateway)) as client:
        replay=client.post(f"/v1/agent-turns/{follow['feedback_turn_id']}/execute")
        assert replay.status_code==200,replay.text
        assert len(gateway.evaluations)==1
        with db.connect() as c:
            assert c.execute('SELECT details_json FROM capture_artifact WHERE artifact_id=?',(artifact['artifact_id'],)).fetchone()[0]==original
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0]==1
