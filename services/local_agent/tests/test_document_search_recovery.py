"""A failed local target search remains a recoverable spoken conversation."""
import json

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.retrieval.local_semantic import LocalSemanticMatcher
from home_study_agent import document_target_hypothesis as targets
from test_diagnostic_prompt_http import environment, CHILD
import test_document_goal_http as original
from test_document_revisit import next_day, offer, say, feedback


@pytest.mark.parametrize('failure,phrase', [('time_budget','及时查完'),
    ('missing_assets','暂时不能使用'), ('no_match','足够相近')])
def test_failure_status_restart_retry_and_decline_keep_the_original(environment, monkeypatch, failure, phrase):
    monkeypatch.setattr(original, 'BODY', '先求24与6的和，然后将所得结果乘3。')
    calls = []
    ready = [False]
    def scores(self, query, documents):
        calls.append(query)
        if ready[0]:
            return {'status':'ready','scores':[.9]*len(documents)}
        return {'status':'ready','scores':[.1]*len(documents)} if failure=='no_match' else {'status':failure}
    monkeypatch.setattr(LocalSemanticMatcher, 'scores', scores)
    generator = original.journey.__wrapped__(environment, monkeypatch)
    j = next(generator)
    try:
        next_day(j, monkeypatch)
        offer(j)
        say(j,monkeypatch,'现在看看原题','start','search-original')
        failed=say(j,monkeypatch,'换道类似的新题检查一下','independent_check','search-first')
        assert failed['execution']['result']['status']=='target_search_pending'
        assert phrase in failed['execution']['response_text']
        cid=failed['execution']['result']['commitment_id']
        assert len(calls)==1
        old=j['client']
        j['client'].__exit__(None, None, None)
        with TestClient(create_app(j['settings'],model_gateway=j['gateway'])) as restarted:
            j['client']=restarted
            status=say(j,monkeypatch,'刚才怎么没有找到','status','search-status')
            assert phrase in status['execution']['response_text']
            assert status['execution']['result']['status']=='target_search_pending'
            replay=restarted.post('/v1/agent-turns/'+failed['turn']['agent_turn_id']+'/execute')
            assert replay.status_code==200,replay.text
            assert len(calls)==1
            if failure!='time_budget':
                declined=say(j,monkeypatch,'先别找了，回到原题','resume_decline','search-decline')
                assert declined['execution']['result']['status']=='waiting_answer'
            ready[0]=True
            retried=say(j,monkeypatch,'再找一道相似的题试试','independent_check','search-retry')
            assert retried['execution']['result']['status']=='target_choice'
            assert len(calls)==2
        j['client']=old
        with j['db'].connect() as c:
            row=c.execute('SELECT status,payload_json FROM agent_commitment WHERE commitment_id=?',(cid,)).fetchone()
            assert row['status']=='pending'
            saved=json.loads(row['payload_json'])['independent_target_hypothesis']
            assert saved['status']=='proposed' and saved['alignment_verified'] is False
            assert c.execute('SELECT count(*) FROM learning_evidence').fetchone()[0]==0
            assert c.execute('SELECT count(*) FROM diagnostic_prompt').fetchone()[0]==0
        assert original.BODY not in json.dumps(j['calls'][-4:],ensure_ascii=False)
    finally:
        generator.close()


@pytest.mark.parametrize('metadata', [None, 'corrupt', {'status':[]}, {'status':{'bad':True}}])
def test_damaged_search_status_does_not_leak_payload_or_claim_mastery(monkeypatch, metadata):
    from home_study_agent import document_independent_check
    monkeypatch.setattr(document_independent_check,'original_version',lambda state:'original')
    packet=({}, {targets.KEY:{'status':'unavailable','version':targets.VERSION,
        'original_version':'original','retrieval':metadata}}, {}, {}, {}, {})
    message=targets.search_message(packet)
    assert '原题和换题请求都已保留' in message
    assert '没有据此判断你的薄弱点' in message
    assert 'corrupt' not in message and 'bad' not in message


def test_failed_search_does_not_erase_previous_feedback_exposure(environment, monkeypatch):
    monkeypatch.setattr(original,'BODY','先求24与6的和，然后将所得结果乘3。')
    monkeypatch.setattr(LocalSemanticMatcher,'scores',lambda *args:{'status':'time_budget'})
    generator=original.journey.__wrapped__(environment,monkeypatch)
    j=next(generator)
    try:
        next_day(j,monkeypatch)
        offer(j)
        say(j,monkeypatch,'现在看看原题','start','exposure-original')
        text='先给我一点提示'
        turn=say(j,monkeypatch,text,'help','exposure-first',execute=False)
        j['responses'].append(feedback(text))
        first=j['client'].post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
        assert first.status_code==200,first.text
        assert first.json()['execution']['result']['status']=='feedback_ready'
        failed=say(j,monkeypatch,'找一道相似题考考我','independent_check','exposure-search')
        assert failed['execution']['result']['status']=='target_search_pending'
        text='现在我再说说原题的思路'
        turn=say(j,monkeypatch,text,'answer','exposure-second',execute=False)
        j['responses'].append(feedback(text))
        second=j['client'].post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
        assert second.status_code==200,second.text
        with j['db'].connect() as c:
            payload=json.loads(c.execute('SELECT payload_json FROM agent_commitment WHERE commitment_id=?',
                (failed['execution']['result']['commitment_id'],)).fetchone()[0])
            assert [r['after_feedback'] for r in payload['practice']['responses']]==[False,True]
            assert c.execute('SELECT count(*) FROM learning_evidence').fetchone()[0]==0
    finally:
        generator.close()
