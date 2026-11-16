"""Corrections change a derived local explanation, not the original evidence."""
import json
import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.document_revisit import parse, parse_journal
from home_study_agent.document_spoken_revision import resolve
from test_document_open_check import opened, evaluation
from test_document_open_followup import evaluated
from test_document_revisit import say
from test_document_spoken_conversation import supplement
from test_voice_runtime_binding import capture, interpret


def corrected(j, monkeypatch, parent, text, revision, key, *, execute=True):
    j['answer']=text
    j['responses'].append(json.dumps(dict(meaning='answer',quote=text,time=None,confidence=.99,revision=revision)))
    voice=capture(j['client'],j['settings'],monkeypatch,j['sid'],text,parent,key+'-voice')
    turn=interpret(j['client'],j['sid'],text,key,voice)
    assert turn['runtime_capability']=='learning.review_revisit',turn
    if not execute:
        return turn
    j['responses'].append(evaluation(j,parent))
    response=j['client'].post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
    assert response.status_code==200,response.text
    assert response.json()['execution']['result'].get('supplement_evaluation_status')=='completed',response.text
    return response.json()


def test_explicit_correction_replaces_selected_segment_and_survives_restart(tmp_path,monkeypatch):
    with opened(tmp_path,monkeypatch,'spoken_explanation') as j:
        parent,_,_=evaluated(j,monkeypatch,'revision-first')
        first=j['answer']
        old='我的另一段说法是先把人数相加，然后再处理总数。'
        middle=supplement(j,monkeypatch,parent,old,'revision-second')
        new='平均每人的数量应该用总数除以人数。'
        text='第二段回答改成：'+new
        result=corrected(j,monkeypatch,middle,text,
            dict(target=2,target_quote='第二段回答',replacement_quote=new),'revision-third')
        prompt=result['execution']['result']['diagnostic_prompt']['prompt']
        assert result['execution']['result']['supplement_conversation']['effective_segments'][1]['text']==new
        assert old not in result['execution']['response_text']
        status=say(j,monkeypatch,'把我改过后的解释读回来','status','revision-status')
        chain=status['execution']['result']['spoken_conversation']
        assert [s['text'] for s in chain['effective_segments']]==[first,new]
        assert chain['effective_segments'][1]['slot_id']==middle['prompt_id']
        assert chain['effective_segments'][1]['source_prompt_id']==prompt['prompt_id']
        assert [s['transcript'] for s in chain['segments']]==[first,old,text]
        assert not chain['pending_revisions']
        assert old not in status['execution']['response_text']
        assert chain['combined_assessment_status']=='not_evaluated'
        assert chain['mastery_update_allowed'] is False
        payload=json.loads(j['calls'][-2]['messages'][1]['content'])
        assert set(payload)=={'question','response','rubric'} and payload['response']==text
        assert old not in json.dumps(payload,ensure_ascii=False)
        j['client'].__exit__(None, None, None)
        with TestClient(create_app(j['settings'],model_gateway=j['gateway'])) as restarted:
            replay=restarted.post('/v1/agent-turns/'+status['turn']['agent_turn_id']+'/execute')
            assert replay.status_code==200,replay.text
            assert replay.json()['execution']['result']['spoken_conversation']==chain
            assert replay.json()['execution']['replay_audio_allowed'] is False
        with j['db'].connect() as c:
            assert [r[0] for r in c.execute('SELECT submitted_answer FROM diagnostic_attempt ORDER BY rowid')]==[first,old,text]
            assert not c.execute('SELECT 1 FROM diagnostic_attempt WHERE correctness IS NOT NULL').fetchone()


def test_unclear_correction_keeps_original_and_asks_for_location(tmp_path,monkeypatch):
    with opened(tmp_path,monkeypatch,'spoken_explanation') as j:
        parent,_,_=evaluated(j,monkeypatch,'unclear-revision-first')
        original=j['answer']
        text='刚才那步不对，应该先求总数。'
        result=corrected(j,monkeypatch,parent,text,dict(target=None,target_quote='刚才那步',replacement_quote='先求总数。'),'unclear-revision-second')
        assert '你要改第几段' in result['execution']['response_text']
        status=say(j,monkeypatch,'改的那部分确认了吗','status','unclear-revision-status')
        chain=status['execution']['result']['spoken_conversation']
        assert [s['text'] for s in chain['effective_segments']]==[original]
        assert [s['transcript'] for s in chain['pending_revisions']]==[text]
        assert '你要改第几段' in status['execution']['response_text']
        next_prompt=result['execution']['result']['diagnostic_prompt']['prompt']
        clarified=corrected(j,monkeypatch,next_prompt,'第一段改成：先求总数。',
            dict(target=1,target_quote='第一段',replacement_quote='先求总数。'),'unclear-revision-resolved')
        resolved=clarified['execution']['result']['supplement_conversation']
        assert not resolved['pending_revisions']
        assert [s['text'] for s in resolved['effective_segments']]==['先求总数。']
        assert [s['transcript'] for s in resolved['segments']][1]==text
        assert '你要改第几段' not in clarified['execution']['response_text']


def test_revision_role_and_verbatim_replacement_required():
    text='第二段改成：先相加。'
    raw=dict(meaning='answer',quote=text,time=None,confidence=.99,
        revision=dict(target=2,target_quote='第二段',replacement_quote='先相加。'))
    parsed=parse(json.dumps(raw),text,'2026-09-13T00:00:00+00:00')
    assert parse_journal(parsed,text,'2026-09-13T00:00:00+00:00')==parsed
    for bad in ({**raw,'meaning':'help'}, {**raw,'revision':{**raw['revision'],'replacement_quote':'model invented answer'}},
                {**raw,'revision':{**raw['revision'],'target':True}}):
        with pytest.raises(ValueError):
            parse(json.dumps(bad),text,'2026-09-13T00:00:00+00:00')


@pytest.mark.parametrize('target,quote', [(2,'第一段'),(99,'第九十九段'),('last','刚才那句'),(None,'那个')])
def test_numeric_reference_cannot_guess_a_target(target,quote):
    chain=dict(fingerprint='fixed',effective_segments=[dict(slot_id='one',source_prompt_id='one',text='old')])
    value=resolve(chain,dict(target=target,target_quote=quote,replacement_quote='new'))
    assert value['status']=='needs_clarification'


def test_semantic_revision_change_after_capture_cannot_write(tmp_path,monkeypatch):
    with opened(tmp_path,monkeypatch,'spoken_explanation') as j:
        parent,_,_=evaluated(j,monkeypatch,'tampered-revision-first')
        turn=corrected(j,monkeypatch,parent,'上一段改成：用总数除以人数。',
            dict(target='last',target_quote='上一段',replacement_quote='用总数除以人数。'),'tampered-revision-second',execute=False)
        with j['db'].connect() as c:
            c.execute("UPDATE voice_control_intent_request SET decision_json=json_set(decision_json,'$.revision.target',null) WHERE request_key=?",
                (turn['action']['document_revisit_request'],))
            c.commit()
        calls=len(j['calls'])
        response=j['client'].post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
        assert response.status_code in {409,422},response.text
        assert len(j['calls'])==calls
        with j['db'].connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0]==1


def test_original_v1_readback_receipt_remains_valid_after_upgrade(tmp_path,monkeypatch):
    from home_study_agent.document_goal_handoff import digest
    with opened(tmp_path,monkeypatch,'spoken_explanation') as j:
        parent,_,_=evaluated(j,monkeypatch,'legacy-chain-first')
        supplement(j,monkeypatch,parent,'我再用乘法核对数量。','legacy-chain-second')
        status=say(j,monkeypatch,'前后的回答读回来','status','legacy-chain-status')
        current=status['execution']['result']['spoken_conversation']
        legacy={k:v for k,v in current.items() if k not in {'effective_segments','pending_revisions','fingerprint'}}
        legacy['version']='document_spoken_conversation_v1'
        legacy['fingerprint']=digest(legacy)
        with j['db'].connect() as c:
            c.execute("UPDATE agent_step SET result_json=json_set(result_json,'$.result.spoken_conversation',json(?)) WHERE plan_id=(SELECT plan_id FROM agent_plan WHERE agent_turn_id=?)",
                (json.dumps(legacy),status['turn']['agent_turn_id']))
            c.execute("UPDATE voice_control_intent_request SET execution_receipt_json=json_set(execution_receipt_json,'$.result.spoken_conversation',json(?)) WHERE request_key=?",
                (json.dumps(legacy),status['turn']['action']['document_revisit_request']))
            c.commit()
        replay=j['client'].post('/v1/agent-turns/'+status['turn']['agent_turn_id']+'/execute')
        assert replay.status_code==200,replay.text
        assert replay.json()['execution']['result']['spoken_conversation']==legacy
        assert replay.json()['execution']['replay_audio_allowed'] is False
