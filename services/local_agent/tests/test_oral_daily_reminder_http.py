"""Synthetic voice/source, real HTTP reminder, publication, execution and restart."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from test_oral_daily_supply_http import test_daily_supply_publication_voice_answer_feedback_and_restart as journey
from test_writing_stage_review_consumers import advance_clock


def test_daily_offer_waits_for_source_then_heard_yes_executes_original_check(tmp_path, monkeypatch):
    from home_study_agent import oral_daily_reminder
    clocks=[]
    ids=[]
    outgoing=[]

    def quiet(case):
        from home_study_agent import agent_runtime
        with case['db'].connect() as c:
            latest=c.execute('SELECT MAX(created_at) FROM agent_turn').fetchone()[0]
        clock=advance_clock(monkeypatch,max(datetime.fromisoformat(latest),agent_runtime.datetime.now(timezone.utc))+timedelta(seconds=75))
        clocks.append(clock)
        return clock

    def missing(case):
        clock=quiet(case)
        base=f"/v1/sessions/{case['sid']}/agent"
        result=case['client'].get(base+'/proactive-prompt')
        assert result.status_code==200,result.text
        assert (result.json().get('prompt') or {}).get('source_type')!='due_commitment',result.json()
        with case['db'].connect() as c:
            c.execute("UPDATE agent_commitment SET payload_json=json_set(payload_json,'$.source_step_id','missing-source-step') WHERE commitment_id=?",(case['root'],))
            assert not oral_daily_reminder.due_current(c,case['child'],case['sid'],case['root'],clock.current)
            c.rollback()
        with case['db'].connect() as c:
            c.execute('''INSERT INTO agent_proactive_prompt(prompt_id,child_id,session_id,source_type,source_id,
                message_text,status,created_at,expires_at,delivery_attempts,last_attempt_at)
                VALUES('old-daily-missing',?,?,'due_commitment',?,'old generic reminder','delivered',?,?,1,?)''',
                (case['child'],case['sid'],case['root'],clock.current.isoformat(),
                    (clock.current+timedelta(minutes=10)).isoformat(),clock.current.isoformat()))
            c.commit()
        path=base+'/proactive-prompts/old-daily-missing'
        assert not case['client'].get(path+'/current').json()['current']
        assert case['client'].post(path+'/acknowledge').json()['status']=='expired'
        clocks.append(advance_clock(monkeypatch,clock.current+timedelta(minutes=6)))

    def available(case):
        client,db=case['client'],case['db']
        base=f"/v1/sessions/{case['sid']}/agent"
        # Clear only an unrelated synthetic wrap-up delivery, if one was offered.
        with db.connect() as c:
            c.execute("UPDATE agent_proactive_prompt SET status='expired' WHERE source_type='study_wrap_up'")
            c.commit()
        delivery=client.get(base+'/proactive-prompt').json()
        offer=delivery['prompt']
        assert offer and offer['source_type']==oral_daily_reminder.KIND,delivery
        assert '数学' in offer['message'] and '自己试一道' in offer['message']
        ids.append(offer['prompt_id'])
        path=base+'/proactive-prompts/'+offer['prompt_id']
        assert client.get(path+'/current').json()['current']
        with db.connect() as c:
            c.execute('BEGIN')
            assert oral_daily_reminder.acknowledged(c,case['child'],case['sid'],clocks[-1].current) is None
            assert not c.execute('SELECT 1 FROM diagnostic_prompt WHERE origin_session_id=?',(case['sid'],)).fetchone()
            assert not c.execute('SELECT 1 FROM plan_item WHERE source_ref=?',(case['root'],)).fetchone()
        assert client.get(base+'/proactive-prompt').json()['status']=='awaiting_ack'
        with db.connect() as c:
            c.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id=?",(case['source']['diagnostic_item_id'],))
            c.commit()
        assert not client.get(path+'/current').json()['current']
        assert client.get(base+'/proactive-prompt').json()['status']=='expired'
        with db.connect() as c:
            c.execute("UPDATE diagnostic_item SET status='active' WHERE diagnostic_item_id=?",(case['source']['diagnostic_item_id'],))
            c.execute("UPDATE daily_plan SET version=version+1 WHERE session_id=? AND status='active'",(case['sid'],))
            c.commit()
        newer=client.get(base+'/proactive-prompt').json()['prompt']
        assert newer and newer['source_type']==oral_daily_reminder.KIND and newer['prompt_id']!=offer['prompt_id'],newer
        assert client.post(path+'/acknowledge').json()['status']=='expired'
        with db.connect() as c:
            c.execute('BEGIN')
            assert oral_daily_reminder.acknowledged(c,case['child'],case['sid'],clocks[-1].current) is None
        offer=newer
        ids.append(offer['prompt_id'])
        path=base+'/proactive-prompts/'+offer['prompt_id']
        assert client.post(path+'/acknowledge').json()['status']=='acknowledged'
        assert not client.get(path+'/current').json()['current']
        with db.connect() as c:
            c.execute('BEGIN')
            proof=oral_daily_reminder.acknowledged(c,case['child'],case['sid'],clocks[-1].current)
            assert proof and proof['prompt_id']==offer['prompt_id']
            ids.append(proof['source_id'])
            assert oral_daily_reminder.acknowledged(c,'other-child',case['sid'],clocks[-1].current) is None
            assert oral_daily_reminder.acknowledged(c,case['child'],case['sid'],clocks[-1].current+timedelta(minutes=6)) is None
            # An actual later dialogue invalidates the interpretation of a bare
            # yes; this synthetic interference is rolled back before continuing.
            prior=c.execute('SELECT * FROM agent_turn WHERE child_id=? LIMIT 1',(case['child'],)).fetchone()
            c.execute('''INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,
                action_json,orchestrator_version,created_at,client_action_id) VALUES ('intervening-daily',?,?,'先等等',
                ?,?,'{}',?,?, 'intervening-daily')''',
                (case['child'],case['sid'],prior['intent'],prior['response_text'],prior['orchestrator_version'],clocks[-1].current.isoformat()))
            assert oral_daily_reminder.acknowledged(c,case['child'],case['sid'],clocks[-1].current) is None
            c.rollback()
        outgoing.append(case['posts'])
        return '好'

    journey(tmp_path,monkeypatch,before_publication=missing,before_resume=available)
    contexts=[json.loads(b['messages'][1]['content']) for b in outgoing[0]
        if len(b.get('messages',[]))>1 and b['messages'][1]['content'].startswith('{')]
    assert any(c.get('transcript')=='好' and c.get('state',{}).get('has_acknowledged_daily_supply_offer') for c in contexts)
    assert all(v not in json.dumps(outgoing,ensure_ascii=False) for v in ids)


@pytest.mark.parametrize('fault',['source_changed','cancelled','write_failure'])
def test_daily_heard_yes_rechecks_source_and_transaction_before_start(tmp_path,monkeypatch,fault):
    def available(case):
        from home_study_agent import oral_daily_reminder, agent_runtime
        with case['db'].connect() as c:
            latest=c.execute('SELECT MAX(created_at) FROM agent_turn').fetchone()[0]
        advance_clock(monkeypatch,max(datetime.fromisoformat(latest),agent_runtime.datetime.now(timezone.utc))+timedelta(seconds=75))
        base=f"/v1/sessions/{case['sid']}/agent"
        offer=case['client'].get(base+'/proactive-prompt').json()['prompt']
        assert offer and offer['source_type']==oral_daily_reminder.KIND,offer
        assert case['client'].post(base+'/proactive-prompts/'+offer['prompt_id']+'/acknowledge').json()['status']=='acknowledged'
        return '好的'
    journey(tmp_path,monkeypatch,fault,before_resume=available)
