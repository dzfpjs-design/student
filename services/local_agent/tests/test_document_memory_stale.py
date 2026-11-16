"""Fresh voice after an invalidated recall must not silently select another task."""
import json

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.companion_memory import CompanionMemory
from test_diagnostic_prompt_http import environment
from test_document_goal_http import journey, BODY
from test_document_memory_resume import recall, second_practice, SECOND
from test_document_revisit_memory import practice, CHILD, TEXT
from test_document_revisit import say
from test_voice_runtime_binding import capture, interpret


def invalidate(j, change):
    if change == 'forget':
        store = CompanionMemory(j['db'])
        mid = store.recall(CHILD, '原题回顾')['memories'][0]['memory_id']
        store.forget(CHILD, mid, 'stale-memory-forget')
        return
    with j['db'].connect() as c:
        if change == 'source':
            c.execute("UPDATE tutor_ocr_confirmation SET confirmed_text='changed original'")
        elif change == 'cancel':
            c.execute("UPDATE agent_commitment SET status='cancelled'")
        elif change == 'expiry':
            c.execute("UPDATE companion_memory SET expires_at='2020-01-01T00:00:00+00:00'")
        c.commit()


@pytest.mark.parametrize('change', ['forget', 'source', 'cancel', 'expiry'])
def test_new_spoken_continue_explains_stale_recall_without_guessing(journey, monkeypatch, change):
    j = journey
    practice(j, monkeypatch)
    recall(j, monkeypatch)
    invalidate(j, change)
    with j['db'].connect() as c:
        before = [tuple(row) for row in c.execute('SELECT * FROM agent_commitment')]
    calls = len(j['calls'])
    reply = say(j, monkeypatch, '接着刚才那道题做吧', 'start', 'new-stale-request')
    assert reply['execution']['result']['status'] == 'memory_stale'
    assert '重新核对' in reply['execution']['response_text']
    assert BODY not in reply['execution']['response_text'] and TEXT not in reply['execution']['response_text']
    assert BODY not in json.dumps(j['calls'][calls:], ensure_ascii=False)
    assert TEXT not in json.dumps(j['calls'][calls:], ensure_ascii=False)
    with j['db'].connect() as c:
        assert [tuple(row) for row in c.execute('SELECT * FROM agent_commitment')] == before
        assert c.execute('SELECT count(*) FROM learning_evidence').fetchone()[0] == 0


def test_stale_recall_does_not_swallow_new_camera_control(journey, monkeypatch):
    from test_cloud_first_agent import Planner
    j = journey
    practice(j, monkeypatch)
    recall(j, monkeypatch)
    invalidate(j, 'forget')
    text = '现在请把摄像头关掉，不做题了'
    call_start = len(j['calls'])
    j['responses'].append(json.dumps(dict(meaning='other_request', quote=text, time=None, confidence=.99)))
    j['responses'].append(json.dumps(Planner('observation.pause',{},text).payload))
    voice = capture(j['client'], j['settings'], monkeypatch, j['sid'], text, key='stale-camera-voice')
    turn = interpret(j['client'], j['sid'], text, 'stale-camera-stop', voice)
    assert turn['action']['type'] == 'pause_observation', (turn['action']['type'], [
        json.loads(call['messages'][-1]['content']).get('phase','planner') for call in j['calls'][call_start:]])
    assert turn['runtime_capability'] != 'learning.review_revisit'


def test_repeated_stale_request_survives_restart_and_replay(journey,monkeypatch):
    j=journey
    practice(j,monkeypatch)
    recall(j,monkeypatch)
    invalidate(j,'forget')
    first=say(j,monkeypatch,'继续原题吧','start','stale-first')
    j['client'].__exit__(None, None, None)
    with TestClient(create_app(j['settings'],model_gateway=j['gateway'])) as restarted:
        second=say({**j,'client':restarted},monkeypatch,'那还是接着做吧','start','stale-again')
        assert second['execution']['result']['status']=='memory_stale'
        assert second['execution']['response_text']==first['execution']['response_text']
        count=len(j['calls'])
        replay=restarted.post('/v1/agent-turns/'+second['turn']['agent_turn_id']+'/execute')
        assert replay.status_code==200 and len(j['calls'])==count
        assert replay.json()['execution']['result']==second['execution']['result']


def test_unavailable_semantic_model_still_explains_invalid_source(journey,monkeypatch):
    j=journey
    practice(j,monkeypatch)
    recall(j,monkeypatch)
    invalidate(j,'source')
    j['responses'].append('invalid semantic response')
    text='继续刚才那一道'
    voice=capture(j['client'],j['settings'],monkeypatch,j['sid'],text,key='stale-unavailable-voice')
    turn=interpret(j['client'],j['sid'],text,'stale-unavailable',voice)
    assert turn['runtime_capability']=='learning.review_revisit'
    done=j['client'].post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
    assert done.status_code==200 and done.json()['execution']['result']['status']=='memory_stale'
    assert '重新核对' in done.json()['execution']['response_text']


def test_declining_stale_recall_does_not_claim_cancelled_plan_is_kept(journey,monkeypatch):
    j=journey
    practice(j,monkeypatch)
    recall(j,monkeypatch)
    invalidate(j,'cancel')
    done=say(j,monkeypatch,'那先不继续了','resume_decline','stale-decline')
    assert done['execution']['result']['status']=='memory_declined'
    assert '没有修改复习安排' in done['execution']['response_text']
    with j['db'].connect() as c:
        assert {r[0] for r in c.execute('SELECT status FROM agent_commitment')}=={'cancelled'}


def test_stale_recall_can_recapture_and_learn_an_explicit_new_question(journey,monkeypatch):
    from home_study_agent.document_revisit_memory import report_memory
    j=journey
    practice(j,monkeypatch)
    recall(j,monkeypatch)
    invalidate(j,'source')
    done=say(j,monkeypatch,'继续那道题','start','stale-before-recapture')
    assert done['execution']['result']['status']=='memory_stale'
    second_practice(j,monkeypatch,expired_first=False)
    with j['db'].connect() as c:
        items=report_memory(c,CHILD,j['sid'])['items']
        assert len(items)==1 and items[0]['question']==SECOND
        assert items[0]['response']=='五本借走三本，剩下两本'
        assert c.execute('SELECT count(*) FROM learning_evidence').fetchone()[0]==0
    said=CompanionMemory(j['db']).handle_command(CHILD,j['sid'],'上次学了什么','new-source-recall')
    assert SECOND in said and BODY not in said and '五本借走三本' in said
