"""Candidate choice must remain possible without looking at a screen."""
import json
import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent import document_target_hypothesis as targets
from home_study_agent.retrieval.local_semantic import LocalSemanticMatcher
from test_diagnostic_prompt_http import environment
from test_document_target_hypothesis import proposed
from test_document_revisit import say,feedback
from test_voice_runtime_binding import capture,interpret,submit


def payload(j):
    with j['db'].connect() as c:
        return json.loads(c.execute('SELECT payload_json FROM agent_commitment WHERE commitment_id=?',(j['cid'],)).fetchone()[0])


def test_restart_readback_repeats_the_same_numbered_choices_without_search_or_selection(environment,monkeypatch):
    with proposed(environment,monkeypatch) as j:
        before=payload(j)[targets.KEY]
        actual_scores=LocalSemanticMatcher.scores
        def forbidden(*args,**kwargs):
            pytest.fail('A status/readback request must not restart semantic search')
        monkeypatch.setattr(LocalSemanticMatcher,'scores',forbidden)
        old=j['client']
        j['client'].__exit__(None, None, None)
        with TestClient(create_app(j['settings'],model_gateway=j['gateway'])) as client:
            j['client']=client
            for i,(text,meaning) in enumerate([('刚才有哪些目标，再说一下','status'),
                    ('我没记住，不知道选哪个','unclear'),('能先说明有哪些选项吗','help')]):
                result=say(j,monkeypatch,text,meaning,'target-readback-'+str(i))
                message=result['execution']['response_text']
                for n,option in enumerate(before['options'],1):
                    assert f'{n}，{option["title"]}' in message
                assert '不是确定的知识点判断' in message
                assert result['execution']['result']['status']=='target_choice'
                assert not result['execution']['result'].get('diagnostic_prompt')
            assert payload(j)[targets.KEY]==before
            with j['db'].connect() as c:
                assert c.execute('SELECT count(*) FROM diagnostic_prompt').fetchone()[0]==0
                assert c.execute('SELECT count(*) FROM learning_evidence').fetchone()[0]==0
            cloud=json.dumps(j['calls'][-3:],ensure_ascii=False)
            assert all(option['title'] not in cloud for option in before['options'])
            monkeypatch.setattr(LocalSemanticMatcher,'scores',actual_scores)
            selected=say(j,monkeypatch,'第一项吧','select_1','target-readback-select')
            prompt=selected['execution']['result']['diagnostic_prompt']['prompt']
            with j['db'].connect() as c:
                answer=json.loads(c.execute('SELECT expected_answer_json FROM diagnostic_item WHERE diagnostic_item_id=?',
                    (prompt['diagnostic_item_id'],)).fetchone()[0])['exact_answers'][0]
            voice=capture(j['client'],j['settings'],monkeypatch,j['sid'],'答案是'+answer,prompt,'target-readback-answer-voice')
            turn=interpret(j['client'],j['sid'],'答案是'+answer,'target-readback-answer',voice)
            response=submit(j['client'],j['sid'],prompt,turn,answer)
            assert response.status_code==200,response.text
            assert response.json()['attempt']['correctness']==1
            assert payload(j)['practice']['phase']=='check_feedback'
            assert payload(j)[targets.KEY]['alignment_verified'] is False

        j['client']=old

@pytest.mark.parametrize('damage',['withdraw_source','alter_title','alter_version','bad_options'])
def test_readback_refuses_stale_or_forged_candidate_labels_without_discarding_original(environment,monkeypatch,damage):
    with proposed(environment,monkeypatch) as j:
        before=payload(j)
        with j['db'].connect() as c:
            if damage=='withdraw_source':
                c.execute("UPDATE diagnostic_item SET status='inactive' WHERE diagnostic_item_id=?",(before[targets.KEY]['options'][0]['item_id'],))
            else:
                value=json.loads(json.dumps(before))
                if damage=='alter_title':
                    value[targets.KEY]['options'][0]['title']='FORGED_CANDIDATE_LABEL'
                elif damage=='alter_version':
                    value[targets.KEY]['options'][0]['item_version']='changed'
                else:
                    value[targets.KEY]['options']='CORRUPT_OPTIONS'
                c.execute('UPDATE agent_commitment SET payload_json=? WHERE commitment_id=?',(json.dumps(value),j['cid']))
            c.commit()
        result=say(j,monkeypatch,'请再说一下候选目标','status','target-stale-readback')
        assert result['execution']['result']['status']=='target_search_pending'
        assert '变化' in result['execution']['response_text']
        assert 'FORGED' not in result['execution']['response_text'] and 'CORRUPT' not in result['execution']['response_text']
        assert payload(j)['practice']['responses']==before['practice']['responses']
        assert payload(j)['practice'].get('search_return_phase')=='waiting_answer'
        declined=say(j,monkeypatch,'先回到原来的题','resume_decline','target-stale-return')
        assert declined['execution']['result']['status']=='waiting_answer'
        with j['db'].connect() as c:
            assert c.execute('SELECT count(*) FROM learning_evidence').fetchone()[0]==0


@pytest.mark.parametrize('withdraw',[False,True])
def test_returning_from_choices_preserves_the_existing_feedback_phase(environment,monkeypatch,withdraw):
    with proposed(environment,monkeypatch) as j:
        say(j,monkeypatch,'先回原题','resume_decline','choice-return-initial')
        text='给一点原题的提示'
        turn=say(j,monkeypatch,text,'help','choice-return-help',execute=False)
        j['responses'].append(feedback(text))
        response=j['client'].post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
        assert response.status_code==200,response.text
        assert response.json()['execution']['result']['status']=='feedback_ready'
        before=payload(j)['practice']['responses']
        choices=say(j,monkeypatch,'找一道同类新题检查','independent_check','choice-return-search')
        assert choices['execution']['result']['status']=='target_choice'
        if withdraw:
            candidate=payload(j)[targets.KEY]['options'][0]
            with j['db'].connect() as c:
                c.execute("UPDATE diagnostic_item SET status='inactive' WHERE diagnostic_item_id=?",(candidate['item_id'],))
                c.commit()
            invalid=say(j,monkeypatch,'刚才有哪些候选','status','choice-return-stale')
            assert invalid['execution']['result']['status']=='target_search_pending'
        returned=say(j,monkeypatch,'先不选了回原题','resume_decline','choice-return-feedback')
        assert returned['execution']['result']['status']=='feedback_ready'
        assert payload(j)['practice']['responses']==before
        assert payload(j)['practice']['feedbacks']
