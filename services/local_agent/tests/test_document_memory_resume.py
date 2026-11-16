"""Recall-to-practice runs through voice, HTTP, durable sources and real runtime."""
import json
import base64
from datetime import datetime, timedelta

import pytest
import cv2
import numpy as np
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.companion_memory import CompanionMemory
from home_study_agent import document_memory_resume, document_revisit
from test_diagnostic_prompt_http import environment, start
from test_document_goal_http import journey, BODY
from test_document_revisit_memory import practice, finish, CHILD, TEXT
from test_document_revisit import say, feedback
from test_voice_runtime_binding import capture, interpret

SECOND = 'A shelf has five books. Three books are borrowed. How many books remain?'


def test_both_document_prompts_satisfy_official_json_mode_contract():
    from types import SimpleNamespace
    from home_study_agent import document_review_schedule
    from home_study_agent.model_gateway import ModelGateway
    endpoint=SimpleNamespace(provider='deepseek',base_url='https://api.deepseek.com',model='deepseek-flash')
    for prompt in (document_revisit.PROMPT,document_review_schedule.PROMPT):
        payload=ModelGateway.request_payload(endpoint,[{'role':'system','content':prompt}], 'voice_interaction')
        assert payload['response_format']=={'type':'json_object'}
        assert any('json' in message['content'].lower() for message in payload['messages'])
        assert payload['thinking']=={'type':'disabled'}


def second_practice(j, monkeypatch, *, expired_first=True):
    from home_study_agent import agent_runtime, agent_orchestrator, voice_control_intent
    from home_study_agent.local_ocr.service import RapidOcrEngine
    from test_cloud_first_agent import Planner
    from test_document_goal_http import teach
    from test_document_review_schedule import spoken
    from test_document_revisit import offer
    text = '看看另一道新题，讲我不会的，再提醒我复习'
    j['responses'].append(json.dumps(dict(meaning='other_request', quote=text, time=None, confidence=.99)))
    plan = Planner('learning.capture_question', {}, text, objective_type='learning').payload
    plan.update(after_execution='replan', continuation_scope=[
        {'capability':'learning.explain', 'arguments':{'subject':'数学'}, 'request_quote':'讲我不会的'},
        {'capability':'conversation.respond', 'arguments':{}, 'request_quote':'再提醒我复习'}])
    j['responses'].append(json.dumps(plan, ensure_ascii=False))
    voice = capture(j['client'], j['settings'], monkeypatch, j['sid'], text, key='second-root-voice')
    root = interpret(j['client'], j['sid'], text, 'second-root', voice)
    assert root['runtime_capability'] == 'learning.capture_question', root
    lines = [{'text':SECOND,'confidence':.99,'box':[[30,70],[900,70],[900,150],[30,150]]}]
    monkeypatch.setattr(RapidOcrEngine, 'recognize', lambda *a, **k: lines)
    monkeypatch.setattr(RapidOcrEngine, 'recognize_page_orientation', lambda *a, **k: lines)
    pic = np.full((300,1000,3),230,dtype=np.uint8)
    cv2.putText(pic,'Five books, three borrowed',(30,140),cv2.FONT_HERSHEY_SIMPLEX,1.1,(0,0,0),2)
    ok, encoded = cv2.imencode('.png',pic)
    assert ok
    source = j['client'].post(f'/v1/sessions/{j["sid"]}/ocr',json={
        'image_base64':base64.b64encode(encoded.tobytes()).decode(),'client_action_id':'second-page'}).json()
    receipt = dict(capability='learning.capture_question',execution_location='client_device_bridge',
        action_type='request_capture',status='completed',completed_at=document_revisit.utc_now(),
        output=dict(command_id=root['agent_turn_id'],device_status='recognized',capture_artifact_id=source['artifact_id'],
            ocr=dict(status='completed',artifact_id=source['artifact_id'])))
    saved = j['client'].put('/v1/agent-turns/'+root['agent_turn_id']+'/outcome',json={
        'final_response':'新题拍到了，请核对。','tool_status':'completed',
        'result':{'execution_receipt':receipt,'client_bridge_receipt':receipt}})
    assert saved.status_code == 200, saved.text
    confirmed = j['client'].post(f'/v1/sessions/{j["sid"]}/tutor-ocr-confirmations',json={
        'artifact_id':source['artifact_id'],'confirmed_text':SECOND,'confirmation_kind':'ocr_verified',
        'client_action_id':'second-confirm'})
    assert confirmed.status_code == 200, confirmed.text
    j.update(root=root,source=source,confirmation=confirmed.json())
    teach(j, expected_question=SECOND)
    spoken(j,monkeypatch,'过一分钟再复习这道新题','propose',{'kind':'relative','minutes':1},key='second-time')
    spoken(j,monkeypatch,'确认新题的复习时间','confirm',key='second-time-confirm')
    future = datetime.fromisoformat(document_revisit.utc_now())+timedelta(minutes=2)
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):
            return future.astimezone(tz) if tz else future.replace(tzinfo=None)
    monkeypatch.setattr(agent_runtime,'datetime',Clock)
    for module in (document_revisit,agent_orchestrator,voice_control_intent):
        monkeypatch.setattr(module,'utc_now',lambda:future.isoformat())
    # The first poll acknowledges the previous prompt's expiry; the next poll
    # asks for the next due original, as the running client does.
    if expired_first:
        expired = j['client'].get(f'/v1/sessions/{j["sid"]}/agent/proactive-prompt')
        assert expired.status_code == 200 and expired.json()['status'] == 'expired', expired.text
    offer(j)
    say(j,monkeypatch,'现在试试新题','start','second-start')
    response = '五本借走三本，剩下两本'
    turn = say(j,monkeypatch,response,'answer','second-answer',execute=False)
    j['responses'].append(feedback(response))
    done = j['client'].post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
    assert done.status_code == 200 and 'tutor_turn' in done.json()['execution'],done.text


def recall(j, monkeypatch):
    finish(j)
    j['sid'] = start(j['client'], action='resume-memory-session')
    with j['db'].connect() as c:
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (j['sid'],))
        c.commit()
    before = len(j['calls'])
    voice = capture(j['client'], j['settings'], monkeypatch, j['sid'], '上次学了什么', key='resume-recall-voice')
    turn = interpret(j['client'], j['sid'], '上次学了什么', 'resume-recall', voice)
    assert turn['intent'] == 'memory_command' and TEXT in turn['response_text']
    assert turn['action']['document_memory_resume']['refs']
    assert len(j['calls']) == before
    done = j['client'].put('/v1/agent-turns/'+turn['agent_turn_id']+'/outcome',
        json={'final_response': turn['response_text'], 'tool_status': 'completed'})
    assert done.status_code == 200, done.text
    return done.json()


def test_spoken_recall_resumes_same_question_and_feedback_after_restart(journey, monkeypatch):
    j = journey
    practice(j, monkeypatch)
    recall(j, monkeypatch)
    before = len(j['calls'])
    resumed = say(j, monkeypatch, '那就接着做刚才回顾的题吧', 'start', 'resume-original')
    assert resumed['execution']['result']['status'] == 'waiting_answer'
    assert BODY in resumed['execution']['response_text'] and TEXT not in resumed['execution']['response_text']
    assert BODY not in json.dumps(j['calls'][before:], ensure_ascii=False)
    cid = resumed['execution']['result']['commitment_id']
    j['client'].__exit__(None, None, None)
    with TestClient(create_app(j['settings'], model_gateway=j['gateway'])) as client:
        copy = {**j, 'client': client}
        turn = say(copy, monkeypatch, '我这次先说十二代表红色的数量，八是蓝色的数量', 'answer', 'resume-new-answer', execute=False)
        j['responses'].append(feedback(turn['transcript']))
        result = client.post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
        assert result.status_code == 200 and 'tutor_turn' in result.json()['execution'], result.text
        count = len(j['calls'])
        replay = client.post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
        assert replay.status_code == 200 and len(j['calls']) == count
    with j['db'].connect() as c:
        item = document_revisit.retained(c, cid, CHILD)
        responses = item[1]['practice']['responses']
        assert len(responses) == 2 and responses[0]['text'] == TEXT
        assert responses[1]['after_feedback'] is False
        assert responses[1]['session_id'] == j['sid']
        assert c.execute('SELECT count(*) FROM learning_evidence').fetchone()[0] == 0
    remembered = CompanionMemory(j['db']).handle_command(CHILD,j['sid'],'上次学了什么','resumed-memory-review')
    assert '我这次先说十二' in remembered
    from home_study_agent.repository import StudyRepository
    with j['db'].connect() as c:
        c.execute("UPDATE daily_session SET state='reflection' WHERE session_id=?",(j['sid'],))
        c.commit()
    repo=StudyRepository(j['db'])
    repo.complete_with_reflection(j['sid'],'','','','',True,'resumed-finish')
    assert repo.report_evidence(j['sid'])['document_revisit_practice'][0]['response'] == responses[1]['text']


@pytest.mark.parametrize('change', ['forget', 'cancel', 'source', 'recall_voice', 'new_voice'])
def test_recall_authority_revoked_before_execution(journey, monkeypatch, change):
    j = journey
    practice(j, monkeypatch)
    recalled = recall(j, monkeypatch)
    pending = say(j, monkeypatch, '继续刚才那道原题', 'start', 'stale-resume', execute=False)
    with j['db'].connect() as c:
        before = [tuple(r) for r in c.execute('SELECT commitment_id,payload_json FROM agent_commitment')]
        if change == 'cancel':
            c.execute("UPDATE agent_commitment SET status='cancelled'")
        elif change == 'source':
            c.execute("UPDATE tutor_ocr_confirmation SET confirmed_text='changed source'")
        elif change == 'recall_voice':
            c.execute("UPDATE voice_input SET transcript='changed recall' WHERE voice_input_id=?", (recalled['action']['voice_input_id'],))
        c.commit()
    if change == 'forget':
        memory = CompanionMemory(j['db'])
        mid = memory.recall(CHILD, '原题回顾')['memories'][0]['memory_id']
        memory.forget(CHILD, mid, 'forget-before-resume')
    if change == 'new_voice':
        capture(j['client'], j['settings'], monkeypatch, j['sid'], '先别做了', key='newer-resume-voice')
    reply = j['client'].post('/v1/agent-turns/'+pending['agent_turn_id']+'/execute')
    assert reply.status_code != 200 or reply.json()['execution'].get('result', {}).get('status') != 'waiting_answer', reply.text
    with j['db'].connect() as c:
        assert [tuple(r) for r in c.execute('SELECT commitment_id,payload_json FROM agent_commitment')] == before


def test_decline_does_not_cancel_original_schedule(journey, monkeypatch):
    j = journey
    practice(j, monkeypatch)
    recall(j, monkeypatch)
    with j['db'].connect() as c:
        before = [tuple(r) for r in c.execute('SELECT * FROM agent_commitment')]
    done = say(j, monkeypatch, '我现在不想继续了，先留着', 'resume_decline', 'decline-memory')
    assert done['execution']['result']['status'] == 'memory_declined'
    with j['db'].connect() as c:
        assert [tuple(r) for r in c.execute('SELECT * FROM agent_commitment')] == before
        assert document_memory_resume.focus(c, j['sid'], CHILD, (done['turn']['agent_turn_id'],)) is None


def test_multiple_originals_require_spoken_selection_not_latest_guess(journey, monkeypatch):
    j = journey
    practice(j,monkeypatch)
    second_practice(j,monkeypatch)
    recall(j,monkeypatch)
    before = len(j['calls'])
    with j['db'].connect() as c:
        originals = [tuple(row) for row in c.execute('SELECT * FROM agent_commitment')]
    # A model must not select an unseen ordinal before the product names options.
    options = say(j,monkeypatch,'接着第二题吧','select_2','choose-original')
    assert options['execution']['result']['status'] == 'memory_choice'
    text = options['execution']['response_text']
    assert BODY in text and SECOND in text and '第1题' in text and '第2题' in text
    with j['db'].connect() as c:
        assert [tuple(row) for row in c.execute('SELECT * FROM agent_commitment')] == originals
    still_choosing = say(j,monkeypatch,'继续吧','start','ambiguous-choice')
    assert still_choosing['execution']['result']['status'] == 'memory_choice'
    with j['db'].connect() as c:
        assert [tuple(row) for row in c.execute('SELECT * FROM agent_commitment')] == originals
    j['client'].__exit__(None, None, None)
    with TestClient(create_app(j['settings'],model_gateway=j['gateway'])) as restarted:
        resumed = say({**j,'client':restarted},monkeypatch,'我选第二题','select_2','choose-second')
    assert resumed['execution']['result']['status'] == 'waiting_answer'
    assert resumed['execution']['result']['original_question'] == BODY
    cloud = json.dumps(j['calls'][before:],ensure_ascii=False)
    assert BODY not in cloud and SECOND not in cloud and TEXT not in cloud


def test_multiple_attempts_on_one_question_do_not_create_fake_choices(journey,monkeypatch):
    j=journey
    practice(j,monkeypatch)
    attempt=say(j,monkeypatch,'我再解释一次，十二和八是两种颜色的数量','answer','repeat-before-recall',execute=False)
    j['responses'].append(feedback(attempt['transcript']))
    assert j['client'].post('/v1/agent-turns/'+attempt['agent_turn_id']+'/execute').status_code==200
    recall(j,monkeypatch)
    resumed=say(j,monkeypatch,'现在自己试试原题','start','single-deduped')
    assert resumed['execution']['result']['status']=='waiting_answer'


@pytest.mark.parametrize('change',['uncompleted','owner','nonvoice'])
def test_recall_without_completed_owned_voice_cannot_supply_focus(journey,monkeypatch,change):
    j=journey
    practice(j,monkeypatch)
    turn=recall(j,monkeypatch)
    with j['db'].connect() as c:
        if change=='uncompleted':
            c.execute("UPDATE agent_turn SET tool_status='pending' WHERE agent_turn_id=?",(turn['agent_turn_id'],))
        elif change=='owner':
            c.execute("UPDATE companion_memory_source SET child_id='child-junior-3' WHERE action_id='resume-recall'")
        else:
            c.execute("UPDATE agent_turn SET action_json=json_remove(action_json,'$.voice_input_id') WHERE agent_turn_id=?",(turn['agent_turn_id'],))
        focus = document_revisit.focus(c,j['sid'],CHILD,'unsafe-resume')
        assert focus is None or (focus['phase']=='memory_stale' and focus['choices']==[])
