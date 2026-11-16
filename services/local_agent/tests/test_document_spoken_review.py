"""Whole reasoning is a versioned reference, not a synthetic original answer."""
import json
import pytest
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.document_spoken_review import prepare,parse,KEY,VERSION
from home_study_agent.document_revisit import retained
from test_document_open_check import opened
from test_document_open_followup import evaluated
from test_document_revisit import say
from test_document_spoken_conversation import supplement
from test_document_spoken_revision import corrected
from test_document_spoken_undo import undo
from test_document_spoken_location import pending


def review(j,monkeypatch,key,mutate=None,verdict='supported'):
    turn=say(j,monkeypatch,'我讲完了，请把整段思路一起评价','review_explanation',key,execute=False)
    projection=turn['action'].get(KEY)
    assert projection and projection['status']=='ready',turn
    data=projection['payload']
    value=dict(observations=[dict(kind='supported',quote=data['segments'][0],criterion=data['criteria'][0],
        explanation='你说明了分组的依据，还需要用原题条件检查计算。')],next_prompt='你怎样用原题的总量来检验每组的数量？')
    if mutate:
        mutate(value)
    j['responses'].append(json.dumps(value,ensure_ascii=False))
    j['responses'].append(json.dumps(dict(verdict=verdict)))
    response=j['client'].post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
    assert response.status_code==200,response.text
    return response.json(),data


def test_whole_explanation_authorized_payload_readback_restart_and_followup(tmp_path,monkeypatch):
    with opened(tmp_path,monkeypatch,'spoken_explanation') as j:
        parent,_,_=evaluated(j,monkeypatch,'whole-first')
        first=j['answer']
        second=supplement(j,monkeypatch,parent,'接着用总量除以组数，再乘回去检验。','whole-second')
        with j['db'].connect() as c:
            before=c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0]
            assessments=c.execute('SELECT count(*) FROM rubric_assessment').fetchone()[0]
        result,data=review(j,monkeypatch,'whole-review')
        value=result['execution']['result'].get('whole_review')
        assert value and value['status']=='reference_feedback',result
        assert value['mastery_update_allowed'] is False and value['followup_kind']=='same_question_clarification'
        assert data['segments']==[first,'接着用总量除以组数，再乘回去检验。']
        sent=json.loads(j['calls'][-2]['messages'][1]['content'])
        assert sent==data
        assert set(sent)=={'question','segments','criteria','after_feedback'}
        verification=json.loads(j['calls'][-1]['messages'][1]['content'])
        assert {k:v for k,v in verification.items() if k!='candidate'}==data
        assert '你怎样用原题' in result['execution']['response_text']
        calls=len(j['calls'])
        j['client'].__exit__(None, None, None)
        with TestClient(create_app(j['settings'],model_gateway=j['gateway'])) as client:
            j['client']=client
            replay=client.post('/v1/agent-turns/'+result['turn']['agent_turn_id']+'/execute')
            assert replay.status_code==200,replay.text
            assert replay.json()['execution']['replay_audio_allowed'] is False
            assert len(j['calls'])==calls
            readback=say(j,monkeypatch,'把刚才点评再说一下','status','whole-readback')
            assert readback['execution']['response_text']==result['execution']['response_text']
            with j['db'].connect() as c:
                assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0]==before
                assert c.execute('SELECT count(*) FROM rubric_assessment').fetchone()[0]==assessments
            # The next actual explanation remains source-bound and assisted;
            # its addition invalidates the earlier combined feedback.
            supplement(j,monkeypatch,second,'我把每组的数量乘以组数，核对题中给定的总量。','whole-followup')
            current=say(j,monkeypatch,'现在整体的说法呢','status','whole-new-status')
            assert current['execution']['response_text']!=result['execution']['response_text']
            stale=client.post('/v1/agent-turns/'+result['turn']['agent_turn_id']+'/execute')
            assert stale.status_code in {409,422},stale.text


def test_whole_review_uses_replacement_not_canceled_original_and_undo_invalidates(tmp_path,monkeypatch):
    with opened(tmp_path,monkeypatch,'spoken_explanation') as j:
        parent,_,_=evaluated(j,monkeypatch,'whole-revision-first')
        first=j['answer']
        corrected(j,monkeypatch,parent,'第一段改成：先确定总量，再确定组数。',
            dict(target=1,target_quote='第一段',replacement_quote='先确定总量，再确定组数。'),'whole-revised')
        result,data=review(j,monkeypatch,'whole-revised-review')
        assert data['segments']==['先确定总量，再确定组数。']
        assert first not in json.dumps(data,ensure_ascii=False)
        assert result['execution']['result'].get('whole_review'),result
        undo(j,monkeypatch,'whole-revised-undo')
        current=say(j,monkeypatch,'现在的结果读回来','status','whole-undone-status')
        assert current['execution']['response_text']!=result['execution']['response_text']
        stale=j['client'].post('/v1/agent-turns/'+result['turn']['agent_turn_id']+'/execute')
        assert stale.status_code in {409,422},stale.text


def test_pending_correction_clarifies_before_any_combined_model_call(tmp_path,monkeypatch):
    with opened(tmp_path,monkeypatch,'spoken_explanation') as j:
        pending(j,monkeypatch,'whole-pending')
        calls=len(j['calls'])
        result=say(j,monkeypatch,'请整体评价我的解释','review_explanation','whole-pending-review')
        assert '改第几段' in result['execution']['response_text']
        assert len(j['calls'])==calls+1
        assert not result['execution']['result'].get('whole_review_requested')


def test_hallucinated_quote_rejected_and_http_replay_cannot_buy_retry(tmp_path,monkeypatch):
    with opened(tmp_path,monkeypatch,'spoken_explanation') as j:
        evaluated(j,monkeypatch,'whole-bad-first')
        result,_=review(j,monkeypatch,'whole-bad-review',lambda v:v['observations'][0].update(quote='从来没有说过的话'))
        assert not result['execution']['result'].get('whole_review'),result
        assert '尚未确认' in result['execution']['response_text']
        calls=len(j['calls'])
        for _ in range(2):
            replay=j['client'].post('/v1/agent-turns/'+result['turn']['agent_turn_id']+'/execute')
            assert replay.status_code==200,replay.text
        assert len(j['calls'])==calls
        with j['db'].connect() as c:
            assert c.execute('SELECT attempts FROM tutor_request_reservation WHERE action_id=?',
                (VERSION+':'+result['turn']['agent_turn_id'],)).fetchone()[0]==1


def test_contradictory_teacher_feedback_is_not_spoken(tmp_path,monkeypatch):
    with opened(tmp_path,monkeypatch,'spoken_explanation') as j:
        evaluated(j,monkeypatch,'whole-conflict-first')
        result,_=review(j,monkeypatch,'whole-conflict-review',verdict='conflict')
        assert not result['execution']['result'].get('whole_review')
        assert '尚未确认' in result['execution']['response_text']
        assert '你说明了分组' not in result['execution']['response_text']
        action=result['execution']['result']['whole_review_followup']
        assert action['kind']=='published_criterion_clarification' and action['mastery_update_allowed'] is False
        assert action['criterion'] in result['execution']['response_text']
        readback=say(j,monkeypatch,'刚才要核对什么','status','whole-conflict-readback')
        assert readback['execution']['result']['whole_review_followup']==action
        assert readback['execution']['response_text']==result['execution']['response_text']


@pytest.mark.parametrize('fault',['unknown','withdraw'])
def test_verification_interruption_or_consent_change_never_publishes_candidate(tmp_path,monkeypatch,fault):
    with opened(tmp_path,monkeypatch,'spoken_explanation') as j:
        evaluated(j,monkeypatch,'whole-race-first')
        original=j['gateway'].complete
        async def interrupted(request):
            if request.context_manifest.get('stage')=='candidate_check':
                if fault=='unknown':
                    request.pre_dispatch.authorize_physical({})
                    raise TimeoutError('synthetic verifier outcome unknown')
                with j['db'].connect() as c:
                    c.execute('UPDATE privacy_preference SET cloud_model_text_allowed=0')
                    c.commit()
            return await original(request)
        with monkeypatch.context() as scoped:
            scoped.setattr(j['gateway'],'complete',interrupted)
            result,_=review(j,monkeypatch,'whole-race-review')
        assert not result['execution']['result'].get('whole_review'),result
        assert '尚未确认' in result['execution']['response_text']
        with j['db'].connect() as c:
            key=VERSION+':'+result['turn']['agent_turn_id']
            assert not c.execute('SELECT 1 FROM tutor_turn WHERE client_action_id=?',(key,)).fetchone()
            row=c.execute('SELECT status,attempts FROM tutor_request_reservation WHERE action_id=?',(key,)).fetchone()
            assert row['status']==('interrupted' if fault=='unknown' else 'failed')
            assert row['attempts']==1
        calls=len(j['calls'])
        replay=j['client'].post('/v1/agent-turns/'+result['turn']['agent_turn_id']+'/execute')
        assert replay.status_code in {200,409,422},replay.text
        assert len(j['calls'])==calls


def test_saved_model_review_survives_lost_outer_delivery_without_another_model_call(tmp_path,monkeypatch):
    from home_study_agent.document_spoken_review import SpokenReview
    with opened(tmp_path,monkeypatch,'spoken_explanation') as j:
        evaluated(j,monkeypatch,'whole-delivery-first')
        original=SpokenReview.generate
        async def interrupted(self,turn_id):
            result=await original(self,turn_id)
            assert result['status']=='reference_feedback'
            raise ValueError('synthetic interruption after saved review')
        with monkeypatch.context() as scoped:
            scoped.setattr(SpokenReview,'generate',interrupted)
            result,_=review(j,monkeypatch,'whole-delivery-review')
        calls=len(j['calls'])
        j['client'].__exit__(None, None, None)
        with TestClient(create_app(j['settings'],model_gateway=j['gateway'])) as client:
            j['client']=client
            replay=client.post('/v1/agent-turns/'+result['turn']['agent_turn_id']+'/execute')
            assert replay.status_code==200,replay.text
            assert replay.json()['execution']['result']['whole_review']['status']=='reference_feedback'
            assert replay.json()['execution']['replay_audio_allowed'] is False
            assert len(j['calls'])==calls
            again=client.post('/v1/agent-turns/'+result['turn']['agent_turn_id']+'/execute')
            assert again.status_code==200,again.text
            assert again.json()['execution']['response_text']==replay.json()['execution']['response_text']
            assert again.json()['execution']['replay_audio_allowed'] is False
            assert len(j['calls'])==calls
            readback=say(j,monkeypatch,'再说一次整体点评','status','whole-delivery-readback')
            assert '你怎样用原题' in readback['execution']['response_text']


@pytest.mark.parametrize('fault',['source','consent','voice','cancel','model'])
def test_whole_feedback_replay_rechecks_authority(tmp_path,monkeypatch,fault):
    with opened(tmp_path,monkeypatch,'spoken_explanation') as j:
        parent,_,_=evaluated(j,monkeypatch,'whole-fault-first')
        result,_=review(j,monkeypatch,'whole-fault-review')
        assert result['execution']['result'].get('whole_review'),result
        with j['db'].connect() as c:
            if fault=='source':
                c.execute("UPDATE diagnostic_item SET status='inactive' WHERE diagnostic_item_id=?",(parent['diagnostic_item_id'],))
            elif fault=='consent':
                c.execute('UPDATE privacy_preference SET cloud_model_text_allowed=0')
            elif fault=='cancel':
                c.execute("UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id=?",(result['turn']['agent_turn_id'],))
            elif fault=='model':
                c.execute("UPDATE model_call SET safety_status='blocked' WHERE prompt_version=?",(VERSION,))
            else:
                c.execute("UPDATE voice_input SET transcript='changed' WHERE voice_input_id=(SELECT json_extract(action_json,'$.voice_input_id') FROM agent_turn WHERE agent_turn_id=?)",(result['turn']['agent_turn_id'],))
            c.commit()
        replay=j['client'].post('/v1/agent-turns/'+result['turn']['agent_turn_id']+'/execute')
        assert replay.status_code in {409,422},replay.text


def test_reference_parser_rejects_fabricated_criteria_and_score():
    payload=dict(segments=['先求总数。'],criteria=['解释除法的意义'])
    value=dict(observations=[dict(kind='missing',quote='',criterion=payload['criteria'][0],explanation='还需要说明为什么除。')],next_prompt='除数表示什么？')
    assert parse(json.dumps(value),payload)==value
    with pytest.raises(ValueError):
        parse(json.dumps(dict(value,score=100)),payload)
    value['observations'][0]['criterion']='不存在的标准'
    with pytest.raises(ValueError):
        parse(json.dumps(value),payload)


@pytest.mark.parametrize('text',[
    '虽然6×6=24能凑出总数，但和前段矛盾。',
    '验算6*6=24。', '因为24÷6=6，答案正确。',
    '计算得 Δ=4-12=-7。', '先展开平方，Δ=2²-4×1×3=4-12=-7。',
    '于是 8÷2=4=3。', 'The result is -4+1=-4.', r'计算 2\times 3=5。',
])
def test_numeric_guard_rejects_actual_model_arithmetic_error(text):
    from home_study_agent.formative_arithmetic_guard import assert_numeric_equalities
    with pytest.raises(ValueError,match='FORMATIVE_FALSE_NUMERIC_EQUALITY'):
        assert_numeric_equalities(text)


@pytest.mark.parametrize('text',[
    '用4×6=24检验。', '24÷6=4，每人4支。', '6×6不等于24。', '每人4支。',
    'Δ=(-2)²-4×1×3=4-12=-8。', 'Δ=2 ^ 2 - 4×1×3=-8。',
    'Δ=4²-4×1×4=0。', '令 x+2=3。', '若x + 2 = 3，则x=1。',
    'x=(-3+√25)/2=1。', 'Δ=16-16=0（重根）。', '结果是 1.5+1.5=3。',
    r'计算 2\times 3=6。', '验算 2 x 3=6。', '令 n 2 = 6。',
])
def test_numeric_guard_accepts_correct_checks_and_nonclaims(text):
    from home_study_agent.formative_arithmetic_guard import assert_numeric_equalities
    assert_numeric_equalities(text)


@pytest.mark.parametrize('stage',['generation','verification'])
def test_truncated_review_keeps_usage_known_failure_and_restart_no_retry(tmp_path,monkeypatch,stage):
    from home_study_agent.model_gateway import ModelResponseError
    from home_study_agent.document_spoken_review import REQUEST_TYPE,VERIFY_REQUEST_TYPE
    with opened(tmp_path,monkeypatch,'spoken_explanation') as j:
        evaluated(j,monkeypatch,'length-first')
        original=j['gateway'].complete
        physical_calls=[]
        async def truncated(request):
            target=REQUEST_TYPE if stage=='generation' else VERIFY_REQUEST_TYPE
            if request.request_type==target:
                request.pre_dispatch.authorize_physical({})
                physical_calls.append(request.request_id)
                raise ModelResponseError('MODEL_OUTPUT_TRUNCATED',response_received=True,
                    prompt_tokens=123,completion_tokens=4096,latency_ms=234)
            return await original(request)
        with monkeypatch.context() as scoped:
            scoped.setattr(j['gateway'],'complete',truncated)
            result,_=review(j,monkeypatch,'length-review')
        assert not result['execution']['result'].get('whole_review')
        assert '尚未确认' in result['execution']['response_text']
        key=VERSION+':'+result['turn']['agent_turn_id']
        with j['db'].connect() as c:
            reservation=c.execute('SELECT status,attempts FROM tutor_request_reservation WHERE action_id=?',(key,)).fetchone()
            assert reservation['status']=='failed' and reservation['attempts']==1
            call=c.execute('SELECT * FROM model_call WHERE call_id=?',(physical_calls[0],)).fetchone()
            assert call['error_code']=='MODEL_OUTPUT_TRUNCATED'
            assert call['prompt_tokens']==123 and call['completion_tokens']==4096
            assert not c.execute('SELECT 1 FROM tutor_turn WHERE client_action_id=?',(key,)).fetchone()
        calls=len(j['calls'])
        j['client'].__exit__(None, None, None)
        with TestClient(create_app(j['settings'],model_gateway=j['gateway'])) as client:
            replay=client.post('/v1/agent-turns/'+result['turn']['agent_turn_id']+'/execute')
            assert replay.status_code==200,replay.text
            assert replay.json()['execution']['replay_audio_allowed'] is False
            assert len(j['calls'])==calls and len(physical_calls)==1
