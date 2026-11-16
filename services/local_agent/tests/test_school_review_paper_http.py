"""Scheduled independent school review through real OCR/voice/Runtime routes."""
import base64
import json

import pytest

from home_study_agent.document_vision import DocumentVisionError, DocumentVisionService
from test_school_open_reviews import school_case, answer_and_review, next_day, CHILD
from test_paper_writing_runtime_http import PaperGateway, synthetic_page
from test_voice_runtime_binding import capture, interpret
from test_writing_voice_runtime_http import readback_receipt
from test_writing_question_authorization import LICENSE


@pytest.mark.parametrize('school_case',['public'],indirect=True)
def test_due_review_confirmed_document_formative_parent_review_and_restart(school_case,monkeypatch):
    # Load the deferred projection before advancing the synthetic family day.
    import home_study_agent.paper_writing_projection
    answer_and_review(school_case,score=0)
    case,slot,p,start_turn,_=next_day(school_case,monkeypatch,key='paper-review')
    db,client,headers,sid,_,_,_,_,settings=case
    path=f'/v1/parent/children/{CHILD}/writing-question-authorizations/'+p['diagnostic_item_id']
    view=client.get(path,headers=headers)
    assert view.status_code==200,view.text
    granted=client.post(path+'/grant',headers=headers,json={
        **{k:view.json()[k] for k in ('item_version','source_version','curriculum_version')},
        'expected_revision':0,'client_action_id':'paper-review-purpose','license_basis':LICENSE})
    assert granted.status_code==200,granted.text
    def no_image_cloud(*a,**k):
        raise DocumentVisionError('DOCUMENT_VISION_NOT_CONFIGURED')
    monkeypatch.setattr(DocumentVisionService,'analyze_page',no_image_cloud)
    png,text=synthetic_page(monkeypatch)
    assert client.post(f'/v1/sessions/{sid}/document-monitor/start',json={'client_action_id':'paper-review-monitor'}).status_code==200
    page=client.post(f'/v1/sessions/{sid}/document-monitor/ocr',json={
        'image_base64':base64.b64encode(png).decode(),'client_action_id':'paper-review-ocr'})
    assert page.status_code==200,page.text
    revision=client.post(f'/v1/sessions/{sid}/document-monitor/revisions',json={
        'artifact_id':page.json()['artifact_id'],'content_signature':'isolated-review-sheet','client_action_id':'paper-review-revision'})
    assert revision.status_code==200,revision.text
    gateway=PaperGateway()
    monkeypatch.setattr(client.school_gateway,'complete',gateway.complete)
    def say(quote,key,control):
        gateway.quote,gateway.control=quote,control
        voice=capture(client,settings,monkeypatch,sid,quote,p,key+'-voice',child=CHILD)
        return interpret(client,sid,quote,key,voice)
    say('写完了，帮我看看过程','paper-look','writing.read_original')
    reading=say('这页只有我写的答案','paper-scope','writing.read_original')
    assert reading['action']['type']=='read_writing_original',reading
    playback=readback_receipt(client,sid,reading)
    assert client.put(f"/v1/agent-turns/{reading['agent_turn_id']}/outcome",json={
        'final_response':'原稿已读回','tool_status':'completed','result':playback}).status_code==200
    confirm=say('对，是我写的，识别没有错','paper-confirm','writing.confirm_original')
    submitted=client.post(f"/v1/agent-turns/{confirm['agent_turn_id']}/execute")
    assert submitted.status_code==200,submitted.text
    follow=submitted.json()['turn']['local_evaluation']
    assert follow['status']=='feedback_required',json.dumps(follow,ensure_ascii=False)
    evaluated=client.post(f"/v1/agent-turns/{follow['feedback_turn_id']}/execute")
    assert evaluated.status_code==200,evaluated.text
    assessment=evaluated.json()['execution']['result']['rubric_assessment']
    early=client.post(f"/v1/plan-items/{slot['plan_item_id']}/complete",json={
        'actual_minutes':1,'client_action_id':'paper-reference-complete'})
    assert early.status_code!=200,early.text
    detail_path=f'/v1/parent/children/{CHILD}/learning-rubrics/'+assessment['assessment_id']
    detail=client.get(detail_path,headers=headers)
    assert detail.status_code==200,detail.text
    binding=detail.json()['writing_review']['binding']
    # Intentionally unrelated synthetic prose cannot be represented as mastery.
    body=dict(operation='approve',expected_revision=0,client_action_id='paper-low-review',
        reason='The preserved original is unrelated to the mathematical task; no mastery claim.',
        independent=True,hint_level=0,review_profile='school_open_written_v1',
        writing_evidence={'binding_version':binding['binding_version'],
            'task_point_evidence':[{'task_point':v,'response_quote':text} for v in binding['task_points']]},
        dimensions=[dict(dimension_id=d['dimension_id'],score=0,response_quotes=[text],
            rationale='Original manuscript does not answer this task') for d in assessment['dimensions']],
        criterion_evidence=[dict(criterion=v,response_quote=text) for v in assessment['criteria']])
    reviewed=client.post(detail_path+'/review',headers=headers,json=body)
    assert reviewed.status_code==200,reviewed.text
    done=client.post(f"/v1/plan-items/{slot['plan_item_id']}/complete",json={
        'actual_minutes':1,'client_action_id':'paper-reviewed-complete'})
    assert done.status_code==200 and done.json()['status']=='completed',done.text
    with db.connect() as c:
        attempts=[dict(r) for r in c.execute('SELECT * FROM diagnostic_attempt WHERE review_task_id=?',(p['review_task_id'],))]
        assert len(attempts)==1 and attempts[0]['correctness'] is None
        assert c.execute("SELECT COUNT(*) FROM agent_outcome WHERE outcome_level IN ('independent','transferred','retained') AND child_id=?",(CHILD,)).fetchone()[0]==0
    voice_payloads='\n'.join(json.dumps(r.messages,ensure_ascii=False) for r in gateway.requests if r.request_type=='voice_interaction')
    assert text not in voice_payloads and p['item']['prompt'] not in voice_payloads
    payload=json.loads(gateway.evaluations[0].messages[1]['content'])
    assert set(payload)=={'question','response','rubric'} and payload['response']==text
    reopened=client.school_reopen()
    restored=reopened.post(f"/v1/plan-items/{slot['plan_item_id']}/complete",json={
        'actual_minutes':1,'client_action_id':'paper-reviewed-complete'})
    assert restored.status_code==200,restored.text
    with db.connect() as c:
        assert [dict(r) for r in c.execute('SELECT * FROM diagnostic_attempt WHERE review_task_id=?',(p['review_task_id'],))]==attempts
