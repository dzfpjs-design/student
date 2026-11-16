"""Unknown feedback needs a fresh, source-bound, single-use spoken permission."""
import json

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.document_revisit_retry import MESSAGE
from test_diagnostic_prompt_http import environment
from test_document_goal_http import journey
from test_document_revisit import next_day,offer,say,feedback
from test_voice_runtime_binding import capture

TEXT='十二加八合起来一共有二十支'


def uncertain(j,monkeypatch):
    next_day(j,monkeypatch)
    offer(j)
    say(j,monkeypatch,'现在我自己试试','start','retry-start')
    turn=say(j,monkeypatch,TEXT,'answer','retry-answer',execute=False)
    async def lost(*args):
        raise TimeoutError('synthetic uncertain response')
    with monkeypatch.context() as patch:
        patch.setattr(j['gateway'],'complete',lost)
        result=j['client'].post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
    assert result.status_code==200,result.text
    assert result.json()['execution']['result']['status']=='retry_proposal'
    assert result.json()['turn']['response_text']==MESSAGE
    return turn


def saved(j):
    with j['db'].connect() as c:
        work=json.loads(c.execute('SELECT payload_json FROM agent_commitment').fetchone()[0])['practice']
        attempt=dict(c.execute("SELECT * FROM tutor_request_reservation WHERE action_id LIKE 'document_revisit_formative_v1:%'").fetchone())
        assert c.execute('SELECT count(*) FROM learning_evidence').fetchone()[0]==0
        return work,attempt


def test_restart_spoken_permission_retries_original_once_and_preserves_both_attempts(journey,monkeypatch):
    j=journey
    uncertain(j,monkeypatch)
    original,old=saved(j)
    j['client'].__exit__(None, None, None)
    with TestClient(create_app(j['settings'],model_gateway=j['gateway'])) as restarted:
        j['client']=restarted
        turn=say(j,monkeypatch,'知道了，就把原来的点评重新生成一次','retry_confirm','retry-confirm',execute=False)
        j['responses'].append(feedback(TEXT))
        reply=restarted.post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
        assert reply.status_code==200,reply.text
        assert 'tutor_turn' in reply.json()['execution'],reply.text
        count=len(j['calls'])
        replay=restarted.post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
        assert replay.status_code==200,replay.text
        assert replay.json()['execution']['tutor_turn']['turn_id']==reply.json()['execution']['tutor_turn']['turn_id']
        assert len(j['calls'])==count
    work,attempt=saved(j)
    assert attempt['attempts']==2 and attempt['action_id']==old['action_id'] and attempt['status']=='completed'
    assert work['responses']==original['responses']
    with j['db'].connect() as c:
        history=[r[0] for r in c.execute('SELECT status FROM tutor_request_attempt WHERE action_id=? ORDER BY attempt',(old['action_id'],))]
        assert history==['interrupted','completed']
        journal=c.execute('SELECT execution_receipt_json FROM voice_control_intent_request WHERE request_key=?',
            (turn['action']['document_revisit_request'],)).fetchone()
        assert json.loads(journal[0])['result']['retry_consumed']['attempt']==2


@pytest.mark.parametrize('choice',['retry_decline','status','help'])
def test_decline_or_status_never_authorizes_a_new_generation(journey,monkeypatch,choice):
    j=journey
    uncertain(j,monkeypatch)
    before,attempt=saved(j)
    count=len(j['calls'])
    reply=say(j,monkeypatch,{'retry_decline':'先别重新生成了，保留吧','status':'刚才的点评是什么状态','help':'继续核对刚才的记录'}[choice],choice,'no-retry')
    assert 'tutor_turn' not in reply['execution']
    assert len(j['calls'])==count+1
    after,new=saved(j)
    assert new==attempt and after['responses']==before['responses']
    if choice=='retry_decline':
        assert after['phase']=='answer_saved' and 'retry_proposal' not in after


@pytest.mark.parametrize('change',['voice','readback','attempt','source'])
def test_changed_authority_prevents_retry_and_preserves_old_attempt(journey,monkeypatch,change):
    j=journey
    original=uncertain(j,monkeypatch)
    turn=say(j,monkeypatch,'确认重新生成这次点评','retry_confirm','late-confirm',execute=False)
    count=len(j['calls'])
    if change=='voice':
        capture(j['client'],j['settings'],monkeypatch,j['sid'],'等一下不要重新生成',key='newest-stop')
    else:
        with j['db'].connect() as c:
            if change=='readback':
                c.execute("UPDATE agent_turn SET response_text='changed readback' WHERE agent_turn_id=?",(original['agent_turn_id'],))
            elif change=='attempt':
                c.execute("UPDATE tutor_request_reservation SET attempts=99 WHERE action_id LIKE 'document_revisit_formative_v1:%'")
            else:
                c.execute("UPDATE capture_artifact SET extracted_text='different question' WHERE artifact_id=?",(j['source']['artifact_id'],))
            c.commit()
    reply=j['client'].post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
    assert reply.status_code==409,reply.text
    assert len(j['calls'])==count
    with j['db'].connect() as c:
        assert c.execute("SELECT count(*) FROM tutor_request_attempt WHERE action_id LIKE 'document_revisit_formative_v1:%'").fetchone()[0]==1


def test_unoffered_confirmation_after_decline_does_not_trigger_generation(journey,monkeypatch):
    j=journey
    uncertain(j,monkeypatch)
    say(j,monkeypatch,'先不重新生成了','retry_decline','decline-first')
    count=len(j['calls'])
    reply=say(j,monkeypatch,'确认','retry_confirm','unoffered-confirm')
    assert 'feedback_response_turn_id' not in reply['execution']['result']
    assert len(j['calls'])==count+1 and saved(j)[1]['attempts']==1


def test_second_unknown_cannot_reuse_old_permission(journey,monkeypatch):
    j=journey
    uncertain(j,monkeypatch)
    turn=say(j,monkeypatch,'请重新生成一次','retry_confirm','single-permission',execute=False)
    calls=[]
    async def lost(*args):
        calls.append(1)
        raise TimeoutError('synthetic second uncertainty')
    with monkeypatch.context() as patch:
        patch.setattr(j['gateway'],'complete',lost)
        first=j['client'].post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
        second=j['client'].post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
    assert first.status_code==second.status_code==200
    assert calls==[1] and saved(j)[1]['attempts']==2


def test_known_failure_new_help_is_single_use_but_another_voice_can_retry(journey,monkeypatch):
    j=journey
    next_day(j,monkeypatch)
    offer(j)
    say(j,monkeypatch,'现在我自己试试','start','known-start')
    original=say(j,monkeypatch,TEXT,'answer','known-answer',execute=False)
    j['responses'].append('{}')
    assert j['client'].post('/v1/agent-turns/'+original['agent_turn_id']+'/execute').status_code==200
    turn=say(j,monkeypatch,'请重新核对刚才的原回答','help','known-help',execute=False)
    count=len(j['calls'])
    j['responses'].extend(['{}','{}'])
    path='/v1/agent-turns/'+turn['agent_turn_id']+'/execute'
    assert j['client'].post(path).status_code==200
    assert j['client'].post(path).status_code==200
    assert len(j['calls'])==count+1
    assert saved(j)[1]['attempts']==2
    j['responses'].clear()
    fresh=say(j,monkeypatch,'再试着核对我刚才的思路','help','known-fresh',execute=False)
    j['responses'].append(feedback(TEXT))
    reply=j['client'].post('/v1/agent-turns/'+fresh['agent_turn_id']+'/execute')
    assert 'tutor_turn' in reply.json()['execution'],reply.text
    assert saved(j)[1]['attempts']==3


def test_failed_claim_rolls_back_permission_consumption(journey,monkeypatch):
    j=journey
    uncertain(j,monkeypatch)
    turn=say(j,monkeypatch,'确认重新生成一次','retry_confirm','rollback-confirm',execute=False)
    with j['db'].connect() as c:
        c.execute("""CREATE TRIGGER reject_retry BEFORE INSERT ON tutor_request_attempt
            WHEN NEW.action_id LIKE 'document_revisit_formative_v1:%' AND NEW.attempt=2
            BEGIN SELECT RAISE(ABORT,'synthetic failed claim'); END""")
        c.commit()
    count=len(j['calls'])
    path='/v1/agent-turns/'+turn['agent_turn_id']+'/execute'
    reply=j['client'].post(path)
    assert reply.status_code==200,reply.text
    assert len(j['calls'])==count and saved(j)[1]['attempts']==1
    with j['db'].connect() as c:
        receipt=json.loads(c.execute('SELECT execution_receipt_json FROM voice_control_intent_request WHERE request_key=?',
            (turn['action']['document_revisit_request'],)).fetchone()[0])
        assert 'retry_consumed' not in receipt['result']
        c.execute('DROP TRIGGER reject_retry')
        c.commit()
    j['responses'].append(feedback(TEXT))
    retry=j['client'].post(path)
    assert 'tutor_turn' in retry.json()['execution'],retry.text
    assert saved(j)[1]['attempts']==2
