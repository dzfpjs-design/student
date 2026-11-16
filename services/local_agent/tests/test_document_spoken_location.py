"""Short spoken location replies are controls, not a second copy of an answer."""
import json
import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.document_revisit import parse,parse_journal
from test_document_open_check import opened
from test_document_open_followup import evaluated
from test_document_revisit import say
from test_document_spoken_revision import corrected
from test_document_spoken_conversation import supplement
from test_voice_runtime_binding import capture,interpret


def locate(j,monkeypatch,text,key,target=1,quote='第一段',execute=True):
    j['responses'].append(json.dumps(dict(meaning='locate_revision',quote=text,time=None,confidence=.99,
        location=dict(target=target,target_quote=quote))))
    voice=capture(j['client'],j['settings'],monkeypatch,j['sid'],text,key=key+'-voice')
    turn=interpret(j['client'],j['sid'],text,key,voice)
    assert turn['runtime_capability']=='learning.review_revisit',turn
    if not execute:
        return turn
    reply=j['client'].post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
    assert reply.status_code==200,reply.text
    return reply.json()


def pending(j,monkeypatch,key):
    parent,_,_=evaluated(j,monkeypatch,key+'-first')
    result=corrected(j,monkeypatch,parent,'那一步改成：先求总数。',
        dict(target=None,target_quote='那一步',replacement_quote='先求总数。'),key+'-change')
    return result['execution']['result']['diagnostic_prompt']['prompt']


@pytest.mark.parametrize('raw_voice', [True, False])
def test_short_location_persists_without_new_answer_or_grade_and_old_replay_survives(tmp_path,monkeypatch,raw_voice):
    with opened(tmp_path,monkeypatch,'spoken_explanation',raw_voice=raw_voice) as j:
        parent=pending(j,monkeypatch,'short')
        before=say(j,monkeypatch,'更正保存了吗','status','short-before')
        original=before['execution']['result']['spoken_conversation']['segments'][0]['transcript']
        calls=len(j['calls'])
        result=locate(j,monkeypatch,'就是第一段','short-location')
        chain=result['execution']['result']['spoken_conversation']
        assert [s['text'] for s in chain['effective_segments']]==['先求总数。']
        assert not chain['pending_revisions']
        assert chain['effective_segments'][0]['source_prompt_id']==parent['prompt_id']
        assert len(chain['segments'])==2 and len(chain['location_events'])==1
        assert chain['combined_assessment_status']=='not_evaluated' and not chain['mastery_update_allowed']
        assert len(j['calls'])==calls+1
        assert original not in json.dumps(j['calls'][-1],ensure_ascii=False)
        outbound=json.loads(j['calls'][-1]['messages'][1]['content'])
        assert set(outbound)=={'transcript','phase','current_time','timezone'}
        assert outbound['transcript']=='就是第一段'
        with j['db'].connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0]==2
            assert not c.execute("SELECT 1 FROM diagnostic_attempt WHERE submitted_answer='就是第一段'").fetchone()
        j['client'].__exit__(None, None, None)
        with TestClient(create_app(j['settings'],model_gateway=j['gateway'])) as restarted:
            j['client'] = restarted
            for reply in (before,result):
                replay=restarted.post('/v1/agent-turns/'+reply['turn']['agent_turn_id']+'/execute')
                assert replay.status_code==200,replay.text
                assert replay.json()['execution']['result']['spoken_conversation']==reply['execution']['result']['spoken_conversation']
                assert replay.json()['execution']['replay_audio_allowed'] is False
            # A later answer and explicit change must read their parents as of their own turn.
            next_prompt=supplement(j,monkeypatch,parent,'接着把总数平均分。','short-more')
            changed=corrected(j,monkeypatch,next_prompt,'第二段改成：总数除以人数。',
                dict(target=2,target_quote='第二段',replacement_quote='总数除以人数。'),'short-next-change')
            assert [s['text'] for s in changed['execution']['result']['supplement_conversation']['effective_segments']]==[
                '先求总数。','总数除以人数。']
            status=say(j,monkeypatch,'现在的解释读回来','status','short-status')
            assert not status['execution']['result']['spoken_conversation']['pending_revisions']


@pytest.mark.parametrize('damage',['voice','event','semantics'])
def test_location_source_revocation_rejects_saved_readback(tmp_path,monkeypatch,damage):
    with opened(tmp_path,monkeypatch,'spoken_explanation') as j:
        pending(j,monkeypatch,'revoked')
        result=locate(j,monkeypatch,'第一段','revoked-locate')
        turn=result['turn']
        with j['db'].connect() as c:
            if damage=='voice':
                c.execute("UPDATE voice_input SET transcript='另外的话' WHERE voice_input_id=?",(turn['action']['voice_input_id'],))
            elif damage=='event':
                c.execute("UPDATE agent_commitment SET payload_json=json_remove(payload_json,'$.practice.spoken_location_events') WHERE commitment_id=?",
                    (result['execution']['result']['commitment_id'],))
            else:
                c.execute("UPDATE voice_control_intent_request SET decision_json=json_set(decision_json,'$.location.target',2) WHERE request_key=?",
                    (turn['action']['document_revisit_request'],))
            c.commit()
        calls=len(j['calls'])
        replay=j['client'].post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
        assert replay.status_code in {409,422},replay.text
        assert len(j['calls'])==calls


def test_absent_or_out_of_range_location_does_not_grade_or_modify(tmp_path,monkeypatch):
    with opened(tmp_path,monkeypatch,'spoken_explanation') as j:
        parent,_,_=evaluated(j,monkeypatch,'empty-first')
        absent=locate(j,monkeypatch,'第一段','empty-locate')
        assert '没有等待定位' in absent['execution']['response_text']
        corrected(j,monkeypatch,parent,'那一步改成：先求总数。',
            dict(target=None,target_quote='那一步',replacement_quote='先求总数。'),'empty-change')
        invalid=locate(j,monkeypatch,'第九段','empty-invalid',9,'第九段')
        assert '不能对应' in invalid['execution']['response_text']
        status=say(j,monkeypatch,'先保留了吗','status','empty-status')
        assert len(status['execution']['result']['spoken_conversation']['pending_revisions'])==1
        with j['db'].connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0]==2


def test_location_schema_has_no_replacement_or_fabricated_quote():
    raw=dict(meaning='locate_revision',quote='第一段',time=None,confidence=.99,location=dict(target=1,target_quote='第一段'))
    anchor='2026-09-13T00:00:00+00:00'
    parsed=parse(json.dumps(raw),'第一段',anchor)
    assert parse_journal(parsed,'第一段',anchor)==parsed
    for bad in ({**raw,'meaning':'answer'},{**raw,'location':dict(target=2,target_quote='第二段')},
            {**raw,'location':dict(target=1,target_quote='第一段',replacement_quote='invented')}):
        with pytest.raises(ValueError):
            parse(json.dumps(bad),'第一段',anchor)


def test_location_on_revoked_question_has_recoverable_no_write_response(tmp_path,monkeypatch):
    with opened(tmp_path,monkeypatch,'spoken_explanation') as j:
        parent=pending(j,monkeypatch,'unavailable')
        with j['db'].connect() as c:
            c.execute("UPDATE diagnostic_item SET status='inactive' WHERE diagnostic_item_id=?",(parent['diagnostic_item_id'],))
            c.commit()
        result=locate(j,monkeypatch,'第一段','unavailable-location')
        assert '无法核对' in result['execution']['response_text']
        assert 'spoken_revision_event' not in result['execution']['result']
        with j['db'].connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0]==2


@pytest.mark.parametrize('quote,target,status',[
    ('我上一段回答','last','resolved'),('我的第一段',1,'resolved'),
    ('不是上一段','last','needs_clarification'),('他第一段',1,'needs_clarification'),
])
def test_literal_self_reference_does_not_accept_negation_or_other_speaker(quote,target,status):
    from home_study_agent.document_spoken_location import proposal
    chain=dict(prompt_id='head',fingerprint='synthetic',effective_segments=[dict(slot_id='one',source_prompt_id='one',text='first')],
        pending_revisions=[dict(prompt_id='pending',replacement_quote='replacement')])
    assert proposal(chain,dict(target=target,target_quote=quote))['status']==status


def test_multiple_pending_corrections_never_choose_the_latest_by_accident(tmp_path,monkeypatch):
    with opened(tmp_path,monkeypatch,'spoken_explanation') as j:
        parent=pending(j,monkeypatch,'many')
        corrected(j,monkeypatch,parent,'那句话改成：再核对单位。',
            dict(target=None,target_quote='那句话',replacement_quote='再核对单位。'),'many-another')
        reply=locate(j,monkeypatch,'第一段','many-location')
        assert '不止一条' in reply['execution']['response_text']
        status=say(j,monkeypatch,'目前保存哪些','status','many-status')
        assert len(status['execution']['result']['spoken_conversation']['pending_revisions'])==2
        with j['db'].connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0]==3


def test_location_failure_before_receipt_rolls_back_then_retries_once(tmp_path,monkeypatch):
    from home_study_agent import document_spoken_location
    with opened(tmp_path,monkeypatch,'spoken_explanation') as j:
        pending(j,monkeypatch,'rollback')
        turn=locate(j,monkeypatch,'第一段','rollback-location',execute=False)
        original=document_spoken_location.apply
        def broken(*args,**kwargs):
            raise ValueError('synthetic-location-interruption')
        with monkeypatch.context() as scoped:
            scoped.setattr(document_spoken_location,'apply',broken)
            response=j['client'].post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
        assert response.status_code in {409,422},response.text
        with j['db'].connect() as c:
            saved=c.execute('SELECT payload_json FROM agent_commitment WHERE commitment_id=?',
                (turn['action']['document_revisit_commitment'],)).fetchone()
            assert not json.loads(saved[0])['practice'].get('spoken_location_events')
        assert document_spoken_location.apply is original
        calls=len(j['calls'])
        response=j['client'].post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
        assert response.status_code==200,response.text
        assert len(response.json()['execution']['result']['spoken_conversation']['location_events'])==1
        assert len(j['calls'])==calls


def test_committed_location_recovers_after_outer_dispatch_interruption(tmp_path,monkeypatch):
    from home_study_agent import document_revisit
    with opened(tmp_path,monkeypatch,'spoken_explanation') as j:
        pending(j,monkeypatch,'committed')
        turn=locate(j,monkeypatch,'第一段','committed-location',execute=False)
        original=document_revisit.execute
        def interrupted(database,turn_id):
            result=original(database,turn_id)
            assert result['result']['spoken_revision_event']['status']=='resolved'
            raise ValueError('synthetic-after-commit')
        with monkeypatch.context() as scoped:
            scoped.setattr(document_revisit,'execute',interrupted)
            failed=j['client'].post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
        assert failed.status_code in {409,422},failed.text
        with j['db'].connect() as c:
            work=json.loads(c.execute('SELECT payload_json FROM agent_commitment WHERE commitment_id=?',
                (turn['action']['document_revisit_commitment'],)).fetchone()[0])['practice']
            assert len(work['spoken_location_events'])==1
        calls=len(j['calls'])
        j['client'].__exit__(None, None, None)
        with TestClient(create_app(j['settings'],model_gateway=j['gateway'])) as restarted:
            j['client'] = restarted
            recovered=restarted.post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
            assert recovered.status_code==200,recovered.text
            assert recovered.json()['execution']['replay_audio_allowed'] is False
            assert len(recovered.json()['execution']['result']['spoken_conversation']['location_events'])==1
            assert len(j['calls'])==calls
            status=say(j,monkeypatch,'位置确认好了吗','status','committed-status')
            assert not status['execution']['result']['spoken_conversation']['pending_revisions']


def test_new_question_does_not_inherit_unrelated_correction_events(tmp_path,monkeypatch):
    with opened(tmp_path,monkeypatch,'spoken_explanation') as j:
        first=pending(j,monkeypatch,'separate')
        located=locate(j,monkeypatch,'第一段','separate-location')
        second,_,_=evaluated(j,monkeypatch,'separate-next-question')
        assert second['diagnostic_item_id']!=first['diagnostic_item_id']
        with j['db'].connect() as c:
            c.execute("UPDATE voice_input SET transcript='revoked old control' WHERE voice_input_id=?",
                (located['turn']['action']['voice_input_id'],))
            c.commit()
        status=say(j,monkeypatch,'这道新题回答保存了吗','status','separate-new-status')
        chain=status['execution']['result']['spoken_conversation']
        assert [s['prompt_id'] for s in chain['segments']]==[second['prompt_id']]
        assert not chain.get('location_events') and not chain['pending_revisions']
