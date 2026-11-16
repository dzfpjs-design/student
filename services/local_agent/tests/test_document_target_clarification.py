"""Spoken learning-goal clarification is evidence, not an original-question rewrite."""
import json
from contextlib import contextmanager
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent import document_revisit, document_target_hypothesis as targets
from home_study_agent.retrieval.local_semantic import LocalSemanticMatcher
from test_diagnostic_prompt_http import environment
import test_document_goal_http as original
from test_document_revisit import next_day, offer, say
from test_voice_runtime_binding import capture, interpret, submit
from test_local_semantic import installed_models


def describe(j, monkeypatch, text, query, key, meaning='describe_target', execute=True):
    j['responses'].append(json.dumps(dict(meaning=meaning,quote=text,time=None,confidence=.99,query=query)))
    voice=capture(j['client'],j['settings'],monkeypatch,j['sid'],text,key=key+'-voice')
    turn=interpret(j['client'],j['sid'],text,key,voice)
    assert turn['runtime_capability']=='learning.review_revisit',turn
    with j['db'].connect() as c:
        decision=json.loads(c.execute('SELECT decision_json FROM voice_control_intent_request WHERE request_key=?',
            (turn['action']['document_revisit_request'],)).fetchone()[0])
        assert decision.get('meaning')==meaning and decision.get('query')==query,decision
    if not execute:
        return turn
    reply=j['client'].post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
    assert reply.status_code==200,reply.text
    return reply.json()


@contextmanager
def searching(environment,monkeypatch):
    monkeypatch.setattr(original,'BODY','先求24与6的和，然后将所得结果乘3。')
    queries=[]
    def scores(self,query,documents):
        queries.append(query)
        good='先算括号' in query or '整数混合运算' in query
        return {'status':'ready','scores':[.95 if good and '四则运算' in d else .1 for d in documents]}
    monkeypatch.setattr(LocalSemanticMatcher,'scores',scores)
    generator=original.journey.__wrapped__(environment,monkeypatch)
    j=next(generator)
    try:
        next_day(j,monkeypatch)
        offer(j)
        say(j,monkeypatch,'先看看原题','start','clarify-original')
        failed=say(j,monkeypatch,'另外找题考考我','independent_check','clarify-search')
        assert failed['execution']['result']['status']=='target_search_pending'
        j['cid']=failed['execution']['result']['commitment_id']
        j['queries']=queries
        yield j
    finally:
        generator.close()


def saved(j):
    with j['db'].connect() as c:
        return json.loads(c.execute('SELECT payload_json FROM agent_commitment WHERE commitment_id=?',(j['cid'],)).fetchone()[0])


@pytest.mark.parametrize('meaning',['describe_target','revise_target'])
def test_clarification_parse_retains_query_only_for_explicit_description(meaning):
    raw=dict(meaning=meaning,quote='题目问的是先算什么',time=None,confidence=.99,query='整数混合运算')
    value=document_revisit.parse(json.dumps(raw),raw['quote'],'2026-09-13T12:00:00+00:00')
    assert value['meaning']==meaning and value['query']=='整数混合运算'
    raw['confidence']=.5
    value=document_revisit.parse(json.dumps(raw),raw['quote'],'2026-09-13T12:00:00+00:00')
    assert value['meaning']=='unclear' and value.get('query') is None


@pytest.mark.parametrize('query',[None,'', 'x'*81, {'invented':'query'}])
def test_malformed_concept_is_rejected(query):
    raw=dict(meaning='describe_target',quote='我补充一个条件',time=None,confidence=.99,query=query)
    with pytest.raises(ValueError):
        document_revisit.parse(json.dumps(raw),raw['quote'],'2026-09-13T12:00:00+00:00')


def test_accumulate_correct_restart_choose_score_without_rewriting_original(environment,monkeypatch):
    with searching(environment,monkeypatch) as j:
        first=describe(j,monkeypatch,'我补充一下，它要求比较两种运算','运算顺序','clarify-first')
        assert first['execution']['result']['status']=='target_search_pending'
        assert saved(j)['practice']['responses']==[]
        old=j['client']
        j['client'].__exit__(None, None, None)
        with TestClient(create_app(j['settings'],model_gateway=j['gateway'])) as client:
            j['client']=client
            second=describe(j,monkeypatch,'另外还有小括号，需要先算里面','先算括号','clarify-second')
            assert second['execution']['result']['status']=='target_choice'
            assert j['queries'][-1]=='运算顺序\n先算括号'
            corrected=describe(j,monkeypatch,'前面说得不准确，只需要整数混合运算','整数混合运算','clarify-correction','revise_target')
            assert corrected['execution']['result']['status']=='target_choice'
            assert j['queries'][-1]=='整数混合运算'
            count=len(j['queries'])
            replay=client.post('/v1/agent-turns/'+corrected['turn']['agent_turn_id']+'/execute')
            assert replay.status_code==200,replay.text
            assert len(j['queries'])==count
            state=saved(j)
            entries=state[targets.KEY]['clarifications']
            assert len(entries)==3 and [e['mode'] for e in entries]==['describe_target','describe_target','revise_target']
            assert original.BODY==corrected['execution']['result']['original_question']
            assert not state.get('independent_mapping') and state['practice']['responses']==[]
            selected=say(j,monkeypatch,'选第一项','select_1','clarify-select')
            check=selected['execution']['result']['diagnostic_prompt']['prompt']
            with j['db'].connect() as c:
                assert c.execute('SELECT count(*) FROM learning_evidence').fetchone()[0]==0
                answer=json.loads(c.execute('SELECT expected_answer_json FROM diagnostic_item WHERE diagnostic_item_id=?',
                    (check['diagnostic_item_id'],)).fetchone()[0])['exact_answers'][0]
            voice=capture(j['client'],j['settings'],monkeypatch,j['sid'],'答案是'+answer,check,'clarify-answer-voice')
            turn=interpret(j['client'],j['sid'],'答案是'+answer,'clarify-answer',voice)
            result=submit(j['client'],j['sid'],check,turn,answer)
            assert result.status_code==200,result.text
            assert result.json()['attempt']['correctness']==1
        j['client']=old
        with TestClient(create_app(j['settings'],model_gateway=j['gateway'])) as client:
            j['client']=client
            status=say(j,monkeypatch,'刚才检查结果如何','status','clarify-result')
            assert '这道新题回答正确' in status['execution']['response_text']
        j['client']=old
        cloud=json.dumps(j['calls'][-5:],ensure_ascii=False)
        assert original.BODY not in cloud
        assert '运算顺序' not in cloud and '先算括号' not in cloud
        assert saved(j)[targets.KEY]['alignment_verified'] is False


def test_changed_clarification_voice_blocks_selection(environment,monkeypatch):
    with searching(environment,monkeypatch) as j:
        proposed=describe(j,monkeypatch,'我说的是整数的运算次序','整数混合运算','clarify-voice-proof')
        assert proposed['execution']['result']['status']=='target_choice'
        with j['db'].connect() as c:
            c.execute("UPDATE voice_input SET transcript='不是刚才的意思' WHERE voice_input_id=?",
                (proposed['turn']['action']['voice_input_id'],))
            c.commit()
        response=say(j,monkeypatch,'选第一项','select_1','clarify-invalid-select')
        assert not response['execution']['result'].get('diagnostic_prompt')
        with j['db'].connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_prompt').fetchone()[0]==0


def test_changed_description_while_choosing_returns_to_clarification(environment,monkeypatch):
    with searching(environment,monkeypatch) as j:
        previous=describe(j,monkeypatch,'我说的是整数的运算次序','整数混合运算','clarify-change-in-choice')
        with j['db'].connect() as c:
            c.execute("UPDATE voice_input SET transcript='前面说错了' WHERE voice_input_id=?",
                (previous['turn']['action']['voice_input_id'],))
            c.commit()
        followup=describe(j,monkeypatch,'还需要先处理括号部分','先算括号','clarify-invalid-followup')
        assert followup['execution']['result']['status']=='target_search_pending'
        assert saved(j)[targets.KEY]['status']=='unavailable'


def test_description_outside_target_search_cannot_change_goal(environment,monkeypatch):
    with searching(environment,monkeypatch) as j:
        say(j,monkeypatch,'回到原题先不找了','resume_decline','clarify-leave')
        before=saved(j)
        response=describe(j,monkeypatch,'我想补充一个目标','整数混合运算','clarify-wrong-phase')
        assert response['execution']['result']['status']=='waiting_answer'
        assert saved(j)[targets.KEY]==before[targets.KEY]


def test_withdrawn_clarification_can_be_replaced_explicitly_without_reusing_old_words(environment,monkeypatch):
    with searching(environment,monkeypatch) as j:
        old=describe(j,monkeypatch,'它是先乘除后加减的顺序','整数混合运算','clarify-withdrawn')
        with j['db'].connect() as c:
            c.execute("UPDATE voice_input SET transcript='撤回旧识别文本' WHERE voice_input_id=?",
                (old['turn']['action']['voice_input_id'],))
            c.commit()
        invalid=say(j,monkeypatch,'第一项','select_1','clarify-withdrawn-select')
        assert not invalid['execution']['result'].get('diagnostic_prompt')
        retry=say(j,monkeypatch,'再找一次目标','independent_check','clarify-withdrawn-retry')
        assert retry['execution']['result']['status']=='target_search_pending'
        addition=describe(j,monkeypatch,'还要看括号','先算括号','clarify-withdrawn-addition')
        assert not addition['execution']['result'].get('diagnostic_prompt')
        assert '重新完整说明' in addition['execution']['response_text']
        correction=describe(j,monkeypatch,'我重新完整说一遍，考的是整数的混合运算','整数混合运算','clarify-withdrawn-replacement','revise_target')
        assert correction['execution']['result']['status']=='target_choice'
        current=saved(j)[targets.KEY]
        assert len(current['clarifications'])==1
        assert current['superseded_clarifications'][0][0]['turn_id']==old['turn']['agent_turn_id']
        assert j['queries'][-1]=='整数混合运算'


def test_real_cpu_model_uses_the_captured_concept_to_select_a_qualified_probe(environment,installed_models,monkeypatch):
    settings,db=environment
    actual=LocalSemanticMatcher.scores
    with searching((replace(settings,local_model_dir=installed_models),db),monkeypatch) as j:
        # The first search failure is controlled; this clarification uses real inference.
        monkeypatch.setattr(LocalSemanticMatcher,'scores',actual)
        proposal=describe(j,monkeypatch,'我要检查四则运算的先后顺序','整数四则运算顺序','clarify-real-model')
        assert proposal['execution']['result']['status']=='target_choice'
        value=saved(j)[targets.KEY]
        assert value['retrieval']['status']=='ready'
        assert value['options'][0]['node_id']=='node-p4-math-arithmetic'
        selected=say(j,monkeypatch,'选第一项','select_1','clarify-real-select')
        prompt=selected['execution']['result']['diagnostic_prompt']['prompt']
        assert prompt['item']['knowledge_node_id']=='node-p4-math-arithmetic'


@pytest.mark.parametrize('tamper',['remove_prefix','change_query','change_mode'])
def test_clarification_chain_cannot_be_edited_or_shortened(environment,monkeypatch,tamper):
    with searching(environment,monkeypatch) as j:
        describe(j,monkeypatch,'先补充，它在比较运算次序','运算顺序','clarify-chain-first')
        describe(j,monkeypatch,'还需要先计算括号内的部分','先算括号','clarify-chain-second')
        with j['db'].connect() as c:
            value=json.loads(c.execute('SELECT payload_json FROM agent_commitment WHERE commitment_id=?',(j['cid'],)).fetchone()[0])
            entries=value[targets.KEY]['clarifications']
            if tamper=='remove_prefix':
                entries.pop(0)
            elif tamper=='change_query':
                entries[0]['query']='几何面积'
            else:
                entries[1]['mode']='revise_target'
            c.execute('UPDATE agent_commitment SET payload_json=? WHERE commitment_id=?',(json.dumps(value),j['cid']))
            c.commit()
        result=say(j,monkeypatch,'第一项','select_1','clarify-chain-select')
        assert not result['execution']['result'].get('diagnostic_prompt')
        with j['db'].connect() as c:
            assert c.execute('SELECT count(*) FROM learning_evidence').fetchone()[0]==0
