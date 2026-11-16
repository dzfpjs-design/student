"""Literal-span semantic correction, without prescribed child wording or cloud history."""
import json
import re
import pytest
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.model_gateway import ModelResult
from test_writing_ocr_warning_voice import warning_read, finish_read
from test_writing_voice_runtime_http import environment, ControlGateway, capture, interpret, TEXT


class ReviewGateway(ControlGateway):
    indices = (0,1)
    literal_pair = None

    async def complete(self, request):
        if request.prompt_version != 'writing_review_literal_intent_v1':
            return await super().complete(request)
        self.requests.append(request)
        payload = json.loads(request.messages[-1]['content'])
        text = payload['transcript']
        literals = list(re.finditer(r'\[片段\d+\]', text))
        old, new = ((re.search(re.escape(value), text) for value in self.literal_pair)
                    if self.literal_pair else (literals[i] for i in self.indices))
        return ModelResult(content=json.dumps(dict(role='correction',quote=text,confidence=.96,
            old_span=[old.start(),old.end()],new_span=[new.start(),new.end()])),
            provider='test',model='controlled-intent',latency_ms=1)


@pytest.mark.parametrize('quote,indices', [('不是Saturday，是Sunday',(0,1)),
    ('你把Sunday读成Saturday了，应该是Sunday',(1,2)),
    ('应该是Sunday，不是Saturday',(1,0)),('识别更正，Saturday要改成Sunday',(0,1))])
def test_spoken_correction_needs_no_quotes_or_keyword_recipe(environment, monkeypatch, quote, indices):
    settings, db = environment
    gateway = ReviewGateway('')
    with TestClient(create_app(settings,model_gateway=gateway)) as client:
        sid, prompt, turn, _ = warning_read(client,db,settings,monkeypatch,gateway)
        finish_read(client,sid,turn,'natural-before-correction')
        gateway.quote, gateway.control, gateway.role, gateway.indices = quote, None, 'unknown', indices
        voice = capture(client,settings,monkeypatch,sid,quote,prompt,'natural-correction-voice')
        corrected = interpret(client,sid,quote,'natural-correction',voice)
        assert corrected['action']['type'] == 'read_writing_original', corrected['response_text']
        binding = corrected['action']['writing_ocr_confirmation']
        packet = client.get(f'/v1/sessions/{sid}/writing-ocr',params=binding).json()
        assert packet['response_text'] == TEXT.replace('Saturday','Sunday')
        request = next(r for r in gateway.requests if r.prompt_version=='writing_review_literal_intent_v1')
        serialized = json.dumps(request.messages)
        for private in (TEXT, 'Saturday', 'Sunday', sid, prompt['prompt_id'], voice):
            assert private not in serialized
        assert set(json.loads(request.messages[-1]['content'])) == {'transcript'}
        assert request.evidence_refs == ()


@pytest.mark.parametrize('quote', ['对，刚读出来的就是我的作答，一个字也没错', 'yes'])
def test_semantic_confirmation_does_not_require_canonical_accuracy_words(environment,monkeypatch,quote):
    settings, db = environment
    gateway = ControlGateway('')
    with TestClient(create_app(settings,model_gateway=gateway)) as client:
        sid,prompt,turn,_=warning_read(client,db,settings,monkeypatch,gateway)
        finish_read(client,sid,turn,'natural-body-read')
        gateway.quote,gateway.control=quote,'writing.confirm_original'
        voice=capture(client,settings,monkeypatch,sid,quote,prompt,'natural-body-voice')
        confirmed=interpret(client,sid,quote,'natural-body',voice)
        assert confirmed['action']['type']=='confirm_writing_original',confirmed['response_text']
        result=client.post(f"/v1/agent-turns/{confirmed['agent_turn_id']}/execute")
        assert result.status_code==200,result.text


@pytest.mark.parametrize('fault', ['invented_span','inside_placeholder','same_literal','not_unique','conditional','low_confidence'])
def test_semantic_literal_parser_rejects_inference_or_ambiguity(fault):
    from home_study_agent.writing_review_intent import parse,project
    quote='不是Saturday，是Sunday'
    if fault=='conditional':quote='如果不是Saturday，是Sunday'
    projection=project(quote,[],[TEXT])
    text=projection[0]
    literals=list(re.finditer(r'\[片段\d+\]',text))
    value=dict(role='correction',quote=text,confidence=.96,
        old_span=[literals[0].start(),literals[0].end()],new_span=[literals[1].start(),literals[1].end()])
    source=TEXT
    if fault=='invented_span':value['new_span']=[0,99999]
    elif fault=='inside_placeholder':value['old_span'][0]+=1
    elif fault=='same_literal':value['new_span']=value['old_span']
    elif fault=='not_unique':source=TEXT+' Saturday'
    elif fault=='low_confidence':value['confidence']=.4
    if fault=='low_confidence':
        assert parse(json.dumps(value),quote,projection,source)['status']=='clarify'
    else:
        with pytest.raises(ValueError,match='WRITING_OCR_CORRECTION_'):
            parse(json.dumps(value),quote,projection,source)


@pytest.mark.parametrize('quote', ['如果刚才读得没错，那就是我的答案', '这页只有我写的答案', '可能是对的，我还不确定'])
def test_semantic_positive_cannot_override_conditional_or_scope_only(environment,monkeypatch,quote):
    settings,db=environment
    gateway=ControlGateway('')
    with TestClient(create_app(settings,model_gateway=gateway)) as client:
        sid,prompt,turn,_=warning_read(client,db,settings,monkeypatch,gateway)
        finish_read(client,sid,turn,'guarded-body-read')
        gateway.quote,gateway.control=quote,'writing.confirm_original'
        voice=capture(client,settings,monkeypatch,sid,quote,prompt,'guarded-body-voice')
        result=interpret(client,sid,quote,'guarded-body',voice)
        assert result['action']['type']=='none'
        with db.connect() as c:
            assert c.execute("SELECT count(*) FROM writing_ocr_event WHERE kind='authorize'").fetchone()[0]==0
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0]==0


@pytest.mark.parametrize('fault', ['page_changed','consent','retract','cancel','new_cancel_arrival'])
def test_model_return_rechecks_current_review_and_consent(environment,monkeypatch,fault):
    settings,db=environment
    class RevokingGateway(ReviewGateway):
        async def complete(self,request):
            result=await super().complete(request)
            if request.prompt_version=='writing_review_literal_intent_v1':
                with db.connect() as c:
                    if fault=='page_changed':c.execute("UPDATE capture_artifact SET extracted_text='new page' WHERE artifact_id='page'")
                    elif fault=='consent':c.execute('UPDATE privacy_preference SET cloud_model_text_allowed=0')
                    elif fault=='cancel':
                        c.execute("UPDATE daily_session SET status='completed' WHERE session_id=?",(request.session_id,))
                    elif fault=='new_cancel_arrival':
                        c.execute("""INSERT INTO learning_input_arrival SELECT 'cancel-arrival',child_id,session_id,
                            'cancel-current-correction','取消刚才的更正',snapshot_json,pending_json,created_at
                            FROM learning_input_arrival WHERE session_id=? ORDER BY rowid DESC LIMIT 1""",(request.session_id,))
                    else:
                        from home_study_agent.repository import utc_now
                        row=c.execute('SELECT * FROM writing_ocr_confirmation').fetchone()
                        c.execute("INSERT INTO writing_ocr_event VALUES('revoked-during-model',?,?,'retract','{}','{}',?,'revoked-during-model')",
                            (row['confirmation_id'],row['child_id'],utc_now()))
                    c.commit()
            return result
    gateway=RevokingGateway('')
    with TestClient(create_app(settings,model_gateway=gateway)) as client:
        sid,prompt,turn,_=warning_read(client,db,settings,monkeypatch,gateway)
        finish_read(client,sid,turn,'race-before-read')
        quote='不是Saturday，是Sunday'
        gateway.quote,gateway.control,gateway.role=quote,None,'unknown'
        voice=capture(client,settings,monkeypatch,sid,quote,prompt,'race-correction-voice')
        response=client.post(f'/v1/sessions/{sid}/agent-turn',json={'transcript':quote,
            'client_action_id':'race-correction','cloud_text_allowed':True,'voice_input_id':voice})
        assert response.status_code in (200,409),response.text
        if response.status_code==200:assert response.json()['action']['type']=='none'
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM writing_ocr_confirmation').fetchone()[0]==1
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0]==0


@pytest.mark.parametrize('quote', ['不是开心，是快乐', '应该是快乐，不是开心', '你把快乐读成开心了，应该是快乐'])
def test_unquoted_chinese_literal_roles_and_exact_action_replay(environment, monkeypatch, quote):
    settings,db=environment
    gateway=ReviewGateway('')
    body='今天我和同学一起去公园。我们很开心，回家前收好了所有东西。'
    with TestClient(create_app(settings,model_gateway=gateway)) as client:
        sid,prompt,turn,_=warning_read(client,db,settings,monkeypatch,gateway,text=body)
        finish_read(client,sid,turn,'chinese-read')
        gateway.quote,gateway.control,gateway.role=quote,None,'unknown'
        gateway.literal_pair=('开心','快乐')
        voice=capture(client,settings,monkeypatch,sid,quote,prompt,'chinese-correct-voice')
        result=interpret(client,sid,quote,'chinese-correct',voice)
        assert result['action']['type']=='read_writing_original',result['response_text']
        binding=result['action']['writing_ocr_confirmation']
        packet=client.get(f'/v1/sessions/{sid}/writing-ocr',params=binding).json()
        assert packet['response_text']==body.replace('开心','快乐')
        count=len(gateway.requests)
        replay=interpret(client,sid,quote,'chinese-correct',voice)
        assert replay['agent_turn_id']==result['agent_turn_id'] and len(gateway.requests)==count
        request=next(r for r in gateway.requests if r.prompt_version=='writing_review_literal_intent_v1')
        assert set(json.loads(request.messages[-1]['content']))=={'transcript'}
        assert request.context_manifest.get('all_literal_text_masked') is False
        assert body not in json.dumps(request.messages,ensure_ascii=False)
        assert request.evidence_refs==()
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM writing_ocr_confirmation').fetchone()[0]==2
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0]==0


@pytest.mark.parametrize('quote', ['别改，不是开心，是快乐', '不是让你改，只是说不是开心，是快乐',
    '只是举例，不是开心，是快乐', '不要把开心改成快乐', '如果不是开心，是快乐', '不是开心，是快乐'])
def test_chinese_negation_and_repeated_anchor_never_create_review(environment,monkeypatch,quote):
    settings,db=environment
    gateway=ReviewGateway('')
    body='今天我很开心。' + ('大家都很开心。' if quote=='不是开心，是快乐' else '我们去了公园。')
    with TestClient(create_app(settings,model_gateway=gateway)) as client:
        sid,prompt,turn,_=warning_read(client,db,settings,monkeypatch,gateway,text=body)
        finish_read(client,sid,turn,'negative-chinese-read')
        gateway.quote,gateway.control,gateway.role=quote,None,'unknown'
        gateway.literal_pair=('开心','快乐')
        voice=capture(client,settings,monkeypatch,sid,quote,prompt,'negative-chinese-voice')
        result=interpret(client,sid,quote,'negative-chinese',voice)
        assert result['action']['type']=='none',result['response_text']
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM writing_ocr_confirmation').fetchone()[0]==1
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0]==0
