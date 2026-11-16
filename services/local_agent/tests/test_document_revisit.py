"""A due original question crosses sessions without borrowing yesterday's authority."""
from datetime import datetime, timedelta, timezone
import json

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent import agent_runtime, document_revisit, agent_orchestrator, voice_control_intent
from test_diagnostic_prompt_http import environment, start
from test_document_goal_http import journey, teach, BODY
from test_document_review_schedule import spoken, commitments
from test_voice_runtime_binding import capture, interpret


def next_day(j, monkeypatch):
    teach(j)
    spoken(j, monkeypatch, '过一分钟再试这题', 'propose', {'kind':'relative','minutes':1}, key='due-proposal')
    spoken(j, monkeypatch, '确认这个时间', 'confirm', key='due-confirm')
    cid = commitments(j)[0]['commitment_id']
    with j['db'].connect() as c:
        c.execute("UPDATE daily_session SET status='completed',state='completed' WHERE session_id=?", (j['sid'],))
        c.commit()
    j['old_sid'], j['sid'] = j['sid'], start(j['client'], action='next-day-session')
    with j['db'].connect() as c:
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (j['sid'],))
        c.commit()
    future = datetime.now(timezone.utc)+timedelta(days=2)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return future.astimezone(tz) if tz else future.replace(tzinfo=None)
    monkeypatch.setattr(agent_runtime, 'datetime', Clock)
    monkeypatch.setattr(document_revisit, 'utc_now', lambda: future.isoformat())
    monkeypatch.setattr(agent_orchestrator, 'utc_now', lambda: future.isoformat())
    monkeypatch.setattr(voice_control_intent, 'utc_now', lambda: future.isoformat())
    return cid


def say(j, monkeypatch, text, meaning, key, execute=True, when=None):
    j['responses'].append(json.dumps(dict(meaning=meaning,quote=text,time=when,confidence=.99)))
    voice = capture(j['client'],j['settings'],monkeypatch,j['sid'],text,key=key+'-voice')
    turn = interpret(j['client'],j['sid'],text,key,voice)
    assert turn['runtime_capability'] == 'learning.review_revisit', turn
    if not execute:
        return turn
    reply = j['client'].post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
    assert reply.status_code == 200, reply.text
    return reply.json()


def feedback(text):
    return json.dumps({'assessment':{'response_quote':text,'hypothesis':'需要继续核对是否理解两类数量合并。'},
        'blackboard':{'title':'合并两种数量','segments':[{'narration':'你把两种颜色合起来的想法值得保留。再说说十二和八分别代表什么？',
            'blocks':[{'kind':'question','text':'十二和八分别代表什么？'}]}]}})


def offer(j):
    reply = j['client'].get(f'/v1/sessions/{j["sid"]}/agent/proactive-prompt')
    assert reply.status_code == 200, reply.text
    value = reply.json()
    assert value['prompt'] and value['prompt']['source_type'] == 'document_revisit', value
    assert BODY not in value['prompt']['message']
    return value['prompt']


def test_due_original_voice_start_answer_and_restart(journey, monkeypatch):
    j = journey
    cid = next_day(j,monkeypatch)
    offer(j)
    with j['db'].connect() as c:
        sources_before = [tuple(r) for r in c.execute('SELECT * FROM tutor_ocr_confirmation')]
    started = say(j,monkeypatch,'现在有空，我自己试着做','start','start-original')
    assert BODY in started['execution']['response_text']
    assert '相加' not in started['execution']['response_text']
    assert BODY not in json.dumps(j['calls'][-1],ensure_ascii=False)
    answered = say(j,monkeypatch,'应该一共有二十支，因为把两种颜色合起来','answer','answer-original')
    assert answered['execution']['feedback_error']=='JSONDecodeError'
    assert answered['execution']['result']['status'] == 'answer_saved'
    with j['db'].connect() as c:
        row = c.execute('SELECT * FROM agent_commitment WHERE commitment_id=?',(cid,)).fetchone()
        work = json.loads(row['payload_json'])['practice']
        assert row['status']=='pending' and work['responses'][0]['kind']=='answer'
        assert work['responses'][0]['text']=='应该一共有二十支，因为把两种颜色合起来'
        assert work['session_id']==j['sid'] and work['session_id']!=j['old_sid']
        assert [tuple(r) for r in c.execute('SELECT * FROM tutor_ocr_confirmation')]==sources_before
        assert c.execute('SELECT count(*) FROM learning_evidence').fetchone()[0]==0
    call_count=len(j['calls'])
    j['client'].__exit__(None, None, None)
    with TestClient(create_app(j['settings'],model_gateway=j['gateway'])) as restarted:
        again = restarted.post('/v1/agent-turns/'+answered['turn']['agent_turn_id']+'/execute')
        assert again.status_code==200,again.text
        assert again.json()['execution']['result']==answered['execution']['result']
    assert len(j['calls'])==call_count


def test_feedback_uses_only_original_question_and_new_attempt_and_replays_once(journey,monkeypatch):
    j=journey
    cid=next_day(j,monkeypatch)
    offer(j)
    say(j,monkeypatch,'现在试试看','start','try-original')
    text='我把十二和八合并起来，应该是二十支'
    turn=say(j,monkeypatch,text,'answer','practice-answer',execute=False)
    j['responses'].append(feedback(text))
    reply=j['client'].post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
    assert reply.status_code==200,reply.text
    value=reply.json()['execution']
    assert 'feedback_error' not in value,value
    assert value['result']['status']=='feedback_ready'
    assert value['tutor_turn']['question']==BODY
    assert value['tutor_turn']['output_envelope']['verification_status']=='unverified'
    request=json.loads(j['calls'][-1]['messages'][-1]['content'])
    assert j['calls'][-1]['max_tokens']>=1600
    assert j['calls'][-1]['response_format']=={'type':'json_object'}
    assert set(request)=={'subject','question','response','response_kind','after_feedback'}
    assert request['question']==BODY and request['response']==text and request['response_kind']=='answer'
    count=len(j['calls'])
    j['client'].__exit__(None, None, None)
    with TestClient(create_app(j['settings'],model_gateway=j['gateway'])) as restarted:
        j['client'] = restarted
        repeated=restarted.post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
        assert repeated.status_code==200,repeated.text
        assert repeated.json()['execution']['tutor_turn']['turn_id']==value['tutor_turn']['turn_id']
        assert len(j['calls'])==count
        with j['db'].connect() as c:
            packet=document_revisit.retained(c,cid,'child-primary-4')
            work=packet[1]['practice']
            assert work['phase']=='feedback_ready' and len(work['feedbacks'])==1
            assert c.execute('SELECT count(*) FROM learning_evidence').fetchone()[0]==0
        path=f'/v1/sessions/{j["sid"]}/tutor-playback'
        begun=j['client'].post(path+'/begin',json={'turn_id':value['tutor_turn']['turn_id'],'client_action_id':'review-board-begin'})
        assert begun.status_code==200,begun.text
        board=begun.json()['playback']
        assert board['presentation']['segments'][0]['narration']==value['tutor_turn']['answer']
        with j['db'].connect() as c:
            c.execute("UPDATE capture_artifact SET extracted_text='withdrawn-question' WHERE artifact_id=?",(j['source']['artifact_id'],))
            c.commit()
        invalidated=j['client'].get(path)
        assert invalidated.status_code==200,invalidated.text
        assert invalidated.json()['playback']['status']=='invalidated'


@pytest.mark.parametrize('tamper',[False,True])
def test_next_day_reschedule_needs_exact_readback_before_replacing_due(journey,monkeypatch,tamper):
    j=journey
    cid=next_day(j,monkeypatch)
    offer(j)
    original=commitments(j)[0]['due_at']
    proposed=say(j,monkeypatch,'今天半小时之后再看','propose','reschedule-original',when={'kind':'relative','minutes':30})
    assert commitments(j)[0]['due_at']==original
    assert '对吗' in proposed['execution']['response_text']
    if tamper:
        with j['db'].connect() as c:
            c.execute("UPDATE agent_commitment SET payload_json=json_set(payload_json,'$.practice.proposal.due_at','2027-01-01T00:00:00+00:00') WHERE commitment_id=?",(cid,))
            c.commit()
    turn=say(j,monkeypatch,'好的，按这个新时间安排','confirm','confirm-reschedule',execute=False)
    reply=j['client'].post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
    assert reply.status_code==(409 if tamper else 200),reply.text
    assert (commitments(j)[0]['due_at']==original)==tamper


def test_unknown_feedback_is_not_billed_again_and_explicit_retry_preserves_original_answer(journey,monkeypatch):
    j=journey
    next_day(j,monkeypatch)
    offer(j)
    say(j,monkeypatch,'好的我试着回答','start','unknown-start')
    text='我还不明白这两种颜色怎么合并'
    turn=say(j,monkeypatch,text,'help','unknown-help',execute=False)
    async def lost(*args):
        raise TimeoutError('synthetic unknown transport')
    with monkeypatch.context() as patch:
        patch.setattr(j['gateway'],'complete',lost)
        reply=j['client'].post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
        assert reply.status_code==200,reply.text
        assert reply.json()['execution']['feedback_error']=='TimeoutError'
    count=len(j['calls'])
    retry=say(j,monkeypatch,'继续核对刚才保存的思路','help','unknown-retry')
    assert retry['execution']['result']['status']=='retry_proposal'
    assert '要重新生成' in retry['execution']['response_text']
    assert len(j['calls'])==count+1  # Only new intent, no repeated feedback generation.
    with j['db'].connect() as c:
        work=json.loads(c.execute('SELECT payload_json FROM agent_commitment').fetchone()[0])['practice']
        assert len(work['responses'])==1 and work['responses'][0]['kind']=='help'
        assert work['responses'][0]['text']==text


def test_rescheduling_invalidates_old_offer_and_allows_new_due_offer_same_session(journey,monkeypatch):
    j=journey
    next_day(j,monkeypatch)
    original_offer=offer(j)
    say(j,monkeypatch,'稍后半小时再看','propose','new-offer-time',when={'kind':'relative','minutes':30})
    say(j,monkeypatch,'确认改到这个时间','confirm','new-offer-confirm')
    current=j['client'].get(f'/v1/sessions/{j["sid"]}/agent/proactive-prompts/{original_offer["prompt_id"]}/current')
    assert current.status_code==200 and not current.json()['current']
    future=agent_runtime.datetime.now(timezone.utc)+timedelta(hours=1)
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):
            return future.astimezone(tz) if tz else future.replace(tzinfo=None)
    monkeypatch.setattr(agent_runtime,'datetime',Clock)
    # Expire the old delivery first; a changed due time owns a different offer.
    j['client'].get(f'/v1/sessions/{j["sid"]}/agent/proactive-prompt')
    replacement=offer(j)
    assert replacement['prompt_id']!=original_offer['prompt_id']


def test_help_then_retry_keeps_attempts_separate_without_mastery(journey,monkeypatch):
    j=journey
    next_day(j,monkeypatch)
    offer(j)
    say(j,monkeypatch,'我愿意现在试试看','start','two-attempt-start')
    for key,text,meaning in [('first-help','我不懂应该从哪里开始','help'),
                             ('second-answer','现在我知道要合并成二十支','answer')]:
        turn=say(j,monkeypatch,text,meaning,key,execute=False)
        j['responses'].append(feedback(text))
        reply=j['client'].post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
        assert reply.status_code==200,reply.text
        assert 'tutor_turn' in reply.json()['execution'],reply.text
    with j['db'].connect() as c:
        work=json.loads(c.execute('SELECT payload_json FROM agent_commitment').fetchone()[0])['practice']
        assert [r['kind'] for r in work['responses']]==['help','answer']
        assert [r['after_feedback'] for r in work['responses']]==[False,True]
        assert len(work['feedbacks'])==2
        assert c.execute('SELECT count(*) FROM learning_evidence').fetchone()[0]==0


def test_gateway_transport_failure_is_one_physical_request_not_a_hidden_retry(journey,monkeypatch):
    import httpx
    j=journey
    next_day(j,monkeypatch)
    offer(j)
    say(j,monkeypatch,'我现在自己试一试','start','transport-start')
    turn=say(j,monkeypatch,'十二加八应该是二十','answer','transport-answer',execute=False)
    calls=[]
    async def lost(*args,**kwargs):
        calls.append(1)
        raise httpx.ReadError('synthetic lost transport')
    monkeypatch.setattr(j['gateway']._client,'post',lost)
    reply=j['client'].post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
    assert reply.status_code==200,reply.text
    assert reply.json()['execution'].get('feedback_error')
    assert calls==[1]
    with j['db'].connect() as c:
        assert c.execute("SELECT status FROM tutor_request_reservation WHERE action_id LIKE 'document_revisit_formative_v1:%'").fetchone()[0]=='interrupted'


@pytest.mark.parametrize('change',['quote','new_voice','source'])
def test_feedback_late_or_ungrounded_cannot_publish_or_mark_mastery(journey,monkeypatch,change):
    j=journey
    next_day(j,monkeypatch)
    offer(j)
    say(j,monkeypatch,'现在可以尝试','start','guard-start')
    text='把十二加上八，得到二十'
    turn=say(j,monkeypatch,text,'answer','guard-answer',execute=False)
    j['responses'].append(feedback('fabricated quote' if change=='quote' else text))
    original=j['gateway'].complete
    async def changing(request):
        result=await original(request)
        with j['db'].connect() as c:
            if change=='source':
                c.execute("UPDATE capture_artifact SET extracted_text='changed-after-generation' WHERE artifact_id=?",(j['source']['artifact_id'],))
            elif change=='new_voice':
                c.execute("UPDATE voice_input SET transcript='changed voice' WHERE voice_input_id=?",(turn['action']['voice_input_id'],))
            c.commit()
        return result
    monkeypatch.setattr(j['gateway'],'complete',changing)
    reply=j['client'].post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
    assert reply.status_code==200,reply.text
    assert reply.json()['execution'].get('feedback_error')
    assert 'tutor_turn' not in reply.json()['execution']
    with j['db'].connect() as c:
        assert c.execute('SELECT count(*) FROM tutor_turn WHERE session_id=?',(j['sid'],)).fetchone()[0]==0
        assert c.execute('SELECT count(*) FROM learning_evidence').fetchone()[0]==0


@pytest.mark.parametrize('change',['voice','page','teacher','session'])
def test_new_voice_or_changed_original_blocks_old_start(journey,monkeypatch,change):
    j=journey
    cid=next_day(j,monkeypatch)
    offer(j)
    turn=say(j,monkeypatch,'可以，我现在试试','start','late-start',execute=False)
    if change=='voice':
        capture(j['client'],j['settings'],monkeypatch,j['sid'],'等一下先不要开始',key='new-interruption')
    else:
        with j['db'].connect() as c:
            if change=='page':
                c.execute("UPDATE capture_artifact SET extracted_text='changed' WHERE artifact_id=?",(j['source']['artifact_id'],))
            elif change=='teacher':
                c.execute("UPDATE tutor_turn SET answer='changed' WHERE session_id=?",(j['old_sid'],))
            else:
                c.execute("UPDATE daily_session SET status='completed',state='completed' WHERE session_id=?",(j['sid'],))
            c.commit()
    reply=j['client'].post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
    assert reply.status_code==409,reply.text
    assert 'practice' not in json.loads(commitments(j)[0]['payload_json'])
