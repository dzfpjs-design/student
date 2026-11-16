"""Cross-lane regressions through real isolated HTTP and persisted runtime state."""
import json
import hashlib
from dataclasses import replace
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.model_gateway import ModelResult
from test_learning_input_replan import schedule, inputs, flow
from test_learning_input_actionable_clarification import MISSING
from test_paused_homework_revision_runtime_http import SemanticGateway, say, post
from test_diagnostic_prompt_http import prepare
from test_voice_runtime_binding import capture, interpret
from test_native_writing_shell_http import scheduled, draft, headers, KEY


class MixedGateway(SemanticGateway):
    control = None
    field_meaning = None
    field_hook = None

    async def complete(self, request):
        payload = json.loads(request.messages[-1]['content'])
        if payload.get('pending_input_field') and self.field_hook:
            self.field_hook()
        if payload.get('pending_input_field') and (self.control or self.field_meaning):
            self.requests.append(request)
            return ModelResult(content=json.dumps(dict(field=payload['pending_input_field'],meaning=self.field_meaning or 'other_request',
                quote=payload['transcript'],literal_quote=None,minutes=None,confidence=.99)),provider='test',model='test',latency_ms=1)
        if payload.get('pending_input_field') or payload.get('transcript', '').startswith('还漏了'):
            return await super().complete(request)
        self.requests.append(request)
        assert request.request_type == 'voice_interaction'
        return ModelResult(content=json.dumps(dict(role='control' if self.control else 'unknown',
            control=self.control,quote=payload['transcript'],confidence=.99)),provider='test',model='test',latency_ms=1)


@pytest.mark.parametrize('question', ['writing', 'numeric'])
@pytest.mark.parametrize('reply', ['estimate','negation','quoted','cancel','late_source','unknown_control','original_answer'])
def test_latest_duration_question_is_not_stolen_by_retained_question(schedule,tmp_path,monkeypatch,question,reply):
    (db,sid,_),_,_ = schedule
    settings=Settings(data_dir=tmp_path/'app',database_path=db.path,maintenance_enabled=False)
    gateway=MixedGateway(revision=MISSING)
    with TestClient(create_app(settings,model_gateway=gateway)) as client:
        post(client,'/v1/plan-items/old/start','composition-start')
        post(client,'/v1/plan-items/old/pause','composition-pause')
        prompt=prepare(client,sid,track='pet' if question=='writing' else 'school',
            preferred_item_id='diag-pet-writing-1' if question=='writing' else 'diag-p4-arithmetic-closed-1')['prompt']
        _,proposed=say(client,sid,'还漏了语文作业，背诵第二段','composition-propose')
        assert proposed['execution']['result']['missing_field']=='expected_minutes'
        quote={'estimate':'大概十分钟','negation':'不是十分钟','quoted':'“大概十分钟”',
               'cancel':'取消','late_source':'大概十分钟','unknown_control':'请把旁边的椅子挪开',
               'original_answer':'我的答案是90'}[reply]
        if reply=='unknown_control':gateway.field_meaning='other_request'
        if reply=='late_source':
            def change_source():
                with db.connect() as c:
                    c.execute("UPDATE daily_plan SET version=version+1 WHERE plan_id='original'")
                    c.commit()
            gateway.field_hook=change_source
        voice=capture(client,settings,monkeypatch,sid,quote,prompt,'composition-duration-voice')
        heard=interpret(client,sid,quote,'composition-duration',voice)
        if reply=='original_answer':
            assert heard['action']['type']=='submit_diagnostic_answer',heard
            assert heard['action']['answer']=='90'
            with db.connect() as c:
                assert c.execute('SELECT state FROM learning_input_proposal').fetchone()[0]=='clarification_required'
                assert c.execute('SELECT count(*) FROM learning_input_revision_event').fetchone()[0]==0
                assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0]==0
            if question=='numeric':
                answer=client.post('/v1/sessions/'+sid+'/diagnostic-prompt/answer',json={
                    'prompt_id':prompt['prompt_id'],'item_version':prompt['item_version'],'answer':'90',
                    'client_action_id':'composition-original-answer','authorized_turn_id':heard['agent_turn_id']})
                assert answer.status_code==200,answer.text
                assert answer.json()['attempt']['submitted_answer']=='90'
            return
        assert heard['runtime_capability'] in {'learning_inputs.select_target','learning_inputs.cancel_revision'},heard
        result=client.post('/v1/agent-turns/'+heard['agent_turn_id']+'/execute')
        assert result.status_code==200,result.text
        with db.connect() as c:
            row=c.execute('SELECT state,proposal_json FROM learning_input_proposal').fetchone()
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0]==0
            assert c.execute('SELECT count(*) FROM learning_input_revision_event').fetchone()[0]==0
            assert c.execute('SELECT status FROM diagnostic_prompt WHERE prompt_id=?',(prompt['prompt_id'],)).fetchone()[0]=='waiting_answer'
        assert row['state']=={'estimate':'awaiting_confirmation','late_source':'conflict','cancel':'cancelled'}.get(reply,'clarification_required'), {
            'action':heard['action'],'result':result.json(),'draft':dict(row)}
        assert json.loads(row['proposal_json'])['changes'].get('expected_minutes')==(10 if reply=='estimate' else None)
        if reply=='estimate':
            payload=json.loads(gateway.requests[-1].messages[-1]['content'])
            assert set(payload)=={'transcript','pending_input_field'}


@pytest.mark.parametrize('control,quote,action', [
    ('native_writing.submission_status','刚才作文交上了吗','check_native_writing_submission'),
    ('native_writing.submit_retained','继续提交本题','submit_retained_native_writing'),
])
@pytest.mark.parametrize('withdrawn', [False,True])
def test_native_recovery_survives_new_duration_question(tmp_path,monkeypatch,control,quote,action,withdrawn):
    monkeypatch.setattr('test_scheduled_writing_natural_runtime_http._http_client',TestClient)
    with scheduled(tmp_path) as case:
        settings=replace(case['settings'],native_launch_attestation_key=KEY)
        gateway=MixedGateway(revision=MISSING)
        case['client'].__exit__(None, None, None)
        with TestClient(create_app(settings,model_gateway=gateway)) as client:
            with case['db'].connect() as c:
                c.execute("INSERT INTO session_planning_context(session_id,energy_level,fixed_commitment_minutes,tomorrow_event_minutes,updated_at) VALUES (?,'medium',0,0,datetime('now'))",(case['sid'],))
                c.commit()
            body=draft(case)
            response=client.post('/v1/sessions/'+case['sid']+'/native-writing-original',json=body,headers=headers(body))
            assert response.status_code==200,response.text
            original=response.json()
            _,proposed=say(client,case['sid'],'还漏了语文作业，背诵第二段','native-composition-propose')
            assert proposed['execution']['result']['missing_field']=='expected_minutes'
            gateway.control=control
            if withdrawn:
                def withdraw_source():
                    with case['db'].connect() as c:
                        c.execute("UPDATE capture_artifact SET status='withdrawn' WHERE artifact_id=?",(original['artifact_id'],))
                        c.commit()
                gateway.field_hook=withdraw_source
            voice=capture(client,settings,monkeypatch,case['sid'],quote,case['prompt'],'native-composition-voice')
            heard=interpret(client,case['sid'],quote,'native-composition-control',voice)
            if withdrawn:
                assert heard['runtime_capability']=='learning_inputs.select_target',heard
                result=client.post('/v1/agent-turns/'+heard['agent_turn_id']+'/execute')
                assert result.status_code==200,result.text
                with case['db'].connect() as c:
                    assert c.execute('SELECT answer_action_id FROM diagnostic_prompt WHERE prompt_id=?',(case['prompt']['prompt_id'],)).fetchone()[0] is None
                    assert c.execute('SELECT state FROM learning_input_proposal').fetchone()[0]=='clarification_required'
                return
            assert heard['action']['type']==action,heard
            with case['db'].connect() as c:
                assert c.execute('SELECT answer_action_id FROM diagnostic_prompt WHERE prompt_id=?',(case['prompt']['prompt_id'],)).fetchone()[0] is None
                assert c.execute('SELECT state FROM learning_input_proposal').fetchone()[0]=='clarification_required'
            assert json.loads(gateway.requests[-1].messages[-1]['content'])['state']['has_native_writing_submission'] is True
            admission=client.post('/v1/sessions/'+case['sid']+'/agent-turns/'+heard['agent_turn_id']+'/control-admission')
            assert admission.status_code==200,admission.text
            assert admission.json()['status']=='admitted'
            params={k:body[k] for k in ('child_id','prompt_id','item_version','diagnostic_item_id')}
            params.update(text_sha256=hashlib.sha256(body['text'].encode()).hexdigest(),capture_action_id=body['client_action_id'])
            receipt=client.get('/v1/sessions/'+case['sid']+'/native-writing-receipt',params=params)
            assert receipt.status_code==200,receipt.text
            assert receipt.json()['original']['artifact_id']==original['artifact_id']
            if control=='native_writing.submit_retained':
                answer={k:body[k] for k in ('prompt_id','item_version')}
                answer.update(answer=body['text'],authorized_turn_id=heard['agent_turn_id'],
                    writing_original_artifact_id=original['artifact_id'],writing_original_revision=original['original_revision'],
                    client_action_id='composition-retained-answer')
                saved=client.post('/v1/sessions/'+case['sid']+'/diagnostic-prompt/answer',json=answer)
                assert saved.status_code==200,saved.text
                assert saved.json()['attempt']['submitted_answer']==body['text']
                replay=client.post('/v1/sessions/'+case['sid']+'/diagnostic-prompt/answer',json=answer)
                assert replay.status_code==200,replay.text
                assert replay.json()['attempt']['attempt_id']==saved.json()['attempt']['attempt_id']


@pytest.mark.parametrize('mode', ['duration','natural_correction','quoted_correction','cancel','ambiguous_confirmation'])
def test_paper_readback_and_new_field_keep_their_own_evidence(schedule,tmp_path,monkeypatch,mode):
    from test_writing_voice_runtime_http import page
    from test_writing_ocr_warning_voice import finish_read
    from test_writing_review_natural_voice import ReviewGateway
    # The schedule fixture freezes repository time, but real OCR events use UTC.
    monkeypatch.setattr('test_writing_voice_runtime_http.utc_now',lambda:datetime.now(timezone.utc).isoformat())

    class MixedReviewGateway(MixedGateway):
        indices=(0,1)
        literal_pair=None

        async def complete(self,request):
            if request.prompt_version=='writing_review_literal_intent_v1':
                return await ReviewGateway.complete(self,request)
            return await super().complete(request)

    (db,sid,_),_,_=schedule
    settings=Settings(data_dir=tmp_path/'app',database_path=db.path,maintenance_enabled=False)
    gateway=MixedReviewGateway(revision=MISSING)
    with TestClient(create_app(settings,model_gateway=gateway)) as client:
        post(client,'/v1/plan-items/old/start','paper-start')
        post(client,'/v1/plan-items/old/pause','paper-pause')
        prompt=prepare(client,sid,track='pet',preferred_item_id='diag-pet-writing-1')['prompt']
        page(db,sid)
        gateway.control='writing.read_original'
        quote='作文写完了，你帮我看看吧'
        voice=capture(client,settings,monkeypatch,sid,quote,prompt,'paper-read-voice')
        interpret(client,sid,quote,'paper-read',voice)
        quote='这页只有我写的答案'
        voice=capture(client,settings,monkeypatch,sid,quote,prompt,'paper-scope-voice')
        readback=interpret(client,sid,quote,'paper-scope',voice)
        assert readback['action']['type']=='read_writing_original',readback
        finish_read(client,sid,readback,'composition-paper-readback')
        gateway.control=None
        _,proposed=say(client,sid,'还漏了语文作业，背诵第二段','paper-omission')
        assert proposed['execution']['result']['missing_field']=='expected_minutes'
        quote={'duration':'大概十分钟','natural_correction':'不是Saturday，是Sunday',
               'quoted_correction':'识别更正，把“Saturday”改成“Sunday”','cancel':'取消',
               'ambiguous_confirmation':'是'}[mode]
        gateway.field_meaning='other_request' if mode=='natural_correction' else None
        voice=capture(client,settings,monkeypatch,sid,quote,prompt,'paper-followup-voice')
        heard=interpret(client,sid,quote,'paper-followup',voice)
        if mode in {'natural_correction','quoted_correction'}:
            assert heard['action']['type']=='read_writing_original',heard
            assert heard['action']['writing_ocr_confirmation']!=readback['action']['writing_ocr_confirmation']
            packet=client.get('/v1/sessions/'+sid+'/writing-ocr',params=heard['action']['writing_ocr_confirmation']).json()
            assert 'Sunday' in packet['response_text'] and 'Saturday' not in packet['response_text']
        else:
            result=client.post('/v1/agent-turns/'+heard['agent_turn_id']+'/execute')
            assert result.status_code==200,result.text
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0]==0
            assert c.execute("SELECT count(*) FROM writing_ocr_event WHERE kind='authorize'").fetchone()[0]==0
            row=c.execute('SELECT state,proposal_json FROM learning_input_proposal').fetchone()
            assert row['state']=={'duration':'awaiting_confirmation','cancel':'cancelled'}.get(mode,'clarification_required')
            assert json.loads(row['proposal_json'])['changes'].get('expected_minutes')==(10 if mode=='duration' else None)


@pytest.mark.parametrize('quote', ['是','确认','yes','OK','“大概十分钟”','如果你能帮我提交原稿'])
def test_ambiguous_or_quoted_reply_cannot_gain_control_handoff_from_wrong_model_label(quote):
    from home_study_agent.learning_input_field_semantics import parse
    decision=parse(json.dumps(dict(field='expected_minutes',meaning='other_request',quote=quote,
        literal_quote=None,minutes=None,confidence=.99)),quote,'expected_minutes')
    assert decision['meaning']=='unclear'
