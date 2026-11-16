"""COPY-only real routers, synthetic paper/ASR and no physical model network."""
import base64
import json
from synthetic_unclean_app_process import unclean_app

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.document_monitoring import DocumentMonitoringService
from home_study_agent.document_vision import DocumentVisionService, DocumentVisionError
from test_supply_gap_resume_http import supplied, start, facts, publish_next
from test_paper_writing_runtime_http import synthetic_page
from test_writing_followup_paper_http import revised_page
from test_writing_voice_runtime_http import readback_receipt
from test_writing_question_authorization import LICENSE


def grant(case, item_id):
    path = f"/v1/parent/children/child-primary-4/writing-question-authorizations/{item_id}"
    current = case['client'].get(path)
    assert current.status_code==200, current.text
    value = current.json()
    result = case['post'](case['client'], path+'/grant', {
        **{key:value[key] for key in ('item_version','source_version','curriculum_version')},
        'expected_revision':0,'client_action_id':'purpose-'+item_id,'license_basis':LICENSE})
    assert result['current']['status']=='authorized'


def paper_answer(case, monkeypatch, prompt, key, *, text=None):
    client, sid = case['client'], case['sid']
    png, text = revised_page(monkeypatch,'independent_check',text=text) if text else synthetic_page(monkeypatch)
    def no_upload(*args, **kwargs):
        raise DocumentVisionError('DOCUMENT_VISION_NOT_CONFIGURED')
    monkeypatch.setattr(DocumentVisionService,'analyze_page',no_upload)
    case['post'](client, f'/v1/sessions/{sid}/document-monitor/start', {'client_action_id':key+'-monitor'})
    artifact = case['post'](client, f'/v1/sessions/{sid}/document-monitor/ocr', {
        'image_base64':base64.b64encode(png).decode(), 'client_action_id':key+'-page'})
    assert artifact['extracted_text']==text and artifact['details']['quality']['passed']
    revision = DocumentMonitoringService(case['db']).record_revision(sid,artifact['artifact_id'],key+'-page-signature',key+'-page-revision')
    case['speech']['control']='writing.read_original'
    case['say'](key+'-look','写好了，帮我看看这一页',prompt=prompt)
    reading = case['say'](key+'-scope','这页只有我写的答案',prompt=prompt)
    assert reading['action']['type']=='read_writing_original', reading
    receipt = readback_receipt(client,sid,reading,playback_id=key+'-readback')
    done = client.put(f"/v1/agent-turns/{reading['agent_turn_id']}/outcome",json={
        'final_response':'原稿已读回','tool_status':'completed','result':receipt})
    assert done.status_code==200, done.text
    case['speech']['control']='writing.confirm_original'
    confirmed = case['say'](key+'-confirm','对，是我写的，识别没有错',prompt=prompt)
    submitted = case['post'](client,f"/v1/agent-turns/{confirmed['agent_turn_id']}/execute",{})
    follow = submitted['turn']['local_evaluation']
    assert follow['status']=='feedback_required', (follow.get('reason'),follow)
    evaluated = case['post'](client,f"/v1/agent-turns/{follow['feedback_turn_id']}/execute",{})
    value = evaluated['execution']['result']
    assert value['formative_only'] and not value['automatic_certification']
    assert value['outcome_level'] not in {'independent','transferred','retained'}
    with case['db'].connect() as c:
        saved = c.execute('''SELECT da.*,q.writing_original_artifact_id FROM diagnostic_prompt q
            JOIN diagnostic_attempt da USING(attempt_id) WHERE q.prompt_id=?''',(prompt['prompt_id'],)).fetchone()
        assert saved['submitted_answer']==text and saved['submitted_answer']!='对，是我写的，识别没有错'
        assert saved['writing_original_artifact_id']
    for payload in case['requests']:
        if set(payload)=={'transcript','state'}:
            assert text not in json.dumps(payload)
        assert all(x not in json.dumps(payload) for x in (artifact['artifact_id'],revision['revision_id'],prompt['prompt_id']))
    return follow, dict(saved), artifact


@pytest.mark.parametrize('supplied',['public'],indirect=True)
def test_resume_paper_formative_independent_original_and_restart(supplied, monkeypatch):
    case = supplied
    turn, task, prompt = start(case)
    grant(case,prompt['diagnostic_item_id'])
    first, attempt, page = paper_answer(case,monkeypatch,prompt,'first')
    assert attempt['created_at'] >= task['started_at']
    completed = case['post'](case['client'], f"/v1/plan-items/{task['plan_item_id']}/complete", {
        'actual_minutes':1,'completion_evidence_type':'needs_correction',
        'completion_note':'Original manuscript and reference feedback saved, not a mastery claim',
        'client_action_id':'complete-original-practice'})
    finished = facts(case)
    assert next(p for p in finished['plan_item'] if p['plan_item_id']==task['plan_item_id'])['status']=='completed'
    workspace=case['client'].get(f"/v1/sessions/{case['sid']}")
    assert workspace.status_code==200,workspace.text
    projected=next(row for row in workspace.json()['supply_obligations'] if row['plan_item_id']==task['plan_item_id'])
    assert projected['status']=='practice_completed' and projected['mastery_claim_allowed'] is False
    assert '不等于' in projected['message']
    other = publish_next(case)
    from home_study_agent.supply_gap_resume import candidates
    with case['db'].connect() as c:
        assert not candidates(c,case['sid'],'child-primary-4'), 'Completed original gap must not reopen when another source arrives'
    assert next(p for p in facts(case)['plan_item'] if p['plan_item_id']==task['plan_item_id']) == next(
        p for p in finished['plan_item'] if p['plan_item_id']==task['plan_item_id'])
    grant(case,other['diagnostic_item_id'])
    case['speech']['control']='writing.independent_check'
    request = case['say']('request-independent','给我一道没做过的题，我自己试试',prompt=prompt)
    assert request.get('runtime_capability')=='writing.independent_check', request
    response = case['post'](case['client'],f"/v1/agent-turns/{request['agent_turn_id']}/execute",{})
    next_prompt = response['execution']['result']['prompt']
    assert next_prompt['diagnostic_item_id']==other['diagnostic_item_id']
    assert next_prompt['prompt_id']!=prompt['prompt_id']
    second, second_attempt, second_page = paper_answer(case,monkeypatch,next_prompt,'second',text=
        'Dear Ben, our science club meets in the library every Friday. I want to build a small bridge because I enjoy testing designs. '
        'Would you like to join us next Friday at four? Bring a notebook so we can record our ideas. Best wishes, Mia.')
    assert second_attempt['attempt_id']!=attempt['attempt_id'] and second_page['artifact_id']!=page['artifact_id']
    with case['db'].connect() as c:
        link = c.execute('SELECT * FROM writing_followup WHERE prompt_id=?',(next_prompt['prompt_id'],)).fetchone()
        assert link['parent_prompt_id']==prompt['prompt_id'] and link['kind']=='independent_check'
        assert c.execute("SELECT COUNT(*) FROM agent_outcome WHERE outcome_level IN ('independent','transferred','retained')").fetchone()[0]==0
    before = facts(case)
    requests = len(case['requests'])
    with unclean_app(case, lane='supply_gap', headers=dict(case['client'].headers)) as reopened:
        for tid in (turn['agent_turn_id'],first['feedback_turn_id'],request['agent_turn_id'],second['feedback_turn_id']):
            result = reopened.post(f'/v1/agent-turns/{tid}/execute')
            assert result.status_code==200, result.text
        for p in (prompt,next_prompt):
            current = reopened.get(f"/v1/sessions/{case['sid']}/diagnostic-prompt",params={'prompt_id':p['prompt_id']})
            assert current.status_code==200 and current.json()['status']=='answered', current.text
        assert len(case['requests'])==requests and facts(case)==before
