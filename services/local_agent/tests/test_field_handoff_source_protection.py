"""Sagan's unique-manuscript regression; actual HTTP, no network transport."""
import json
import pytest
from datetime import datetime, timezone
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.model_gateway import ModelResult
from test_lane_composition_v6 import MixedGateway
from test_learning_input_replan import schedule, inputs, flow
from test_learning_input_actionable_clarification import MISSING
from test_paused_homework_revision_runtime_http import say
from test_diagnostic_prompt_http import prepare
from test_voice_runtime_binding import capture, interpret
from test_writing_voice_runtime_http import page
from test_writing_ocr_warning_voice import finish_read
from test_writing_response_origin import TEXT


@pytest.mark.parametrize('quote', ['刚才 Silver lanterns 那部分已经存好了没有', '帮我核对 Silver lanterns 是否保存'])
def test_rejected_paper_text_must_not_fall_into_unfiltered_control(schedule,tmp_path,monkeypatch,quote):
    monkeypatch.setattr('test_writing_voice_runtime_http.utc_now',lambda:datetime.now(timezone.utc).isoformat())
    class Gateway(MixedGateway):
        async def complete(self,request):
            if request.prompt_version=='writing_review_literal_intent_v1':
                self.requests.append(request)
                text=json.loads(request.messages[-1]['content'])['transcript']
                return ModelResult(content=json.dumps(dict(role='other',quote=text,confidence=.99,
                    old_span=None,new_span=None)),provider='test',model='test',latency_ms=1)
            return await super().complete(request)
    (db,sid,_),_,_=schedule
    settings=Settings(data_dir=tmp_path/'app',database_path=db.path,maintenance_enabled=False)
    gateway=Gateway(revision=MISSING)
    with TestClient(create_app(settings,model_gateway=gateway)) as client:
        prompt=prepare(client,sid,track='pet',preferred_item_id='diag-pet-writing-1')['prompt']
        assert 'Silver lanterns' not in json.dumps(prompt,ensure_ascii=False)
        page(db,sid,text=TEXT+' Silver lanterns illuminate quiet corridors.')
        gateway.control='writing.read_original'
        for index,read_quote in enumerate(['作文写完了，你帮我看看吧','这页只有我写的答案']):
            voice=capture(client,settings,monkeypatch,sid,read_quote,prompt,'source-read-voice-'+str(index))
            heard=interpret(client,sid,read_quote,'source-read-'+str(index),voice)
        assert heard['action']['type']=='read_writing_original',heard
        finish_read(client,sid,heard,'source-readback')
        gateway.control=None
        _,proposed=say(client,sid,'还漏了语文作业，背诵第二段','source-omission')
        assert proposed['execution']['result']['missing_field']=='expected_minutes'
        first=len(gateway.requests)
        voice=capture(client,settings,monkeypatch,sid,quote,prompt,'source-check-voice')
        result=interpret(client,sid,quote,'source-check',voice)
        payloads=[{'version':r.prompt_version,'payload':json.loads(r.messages[-1]['content'])}
                  for r in gateway.requests[first:]]
        print(json.dumps({'action':result['action']['type'],'requests':payloads},ensure_ascii=False))
        assert not any('Silver lanterns' in json.dumps(p['payload'],ensure_ascii=False)
                       for p in payloads),payloads
        assert any(p['version']=='writing_review_literal_intent_v1' for p in payloads)
        executed=client.post('/v1/agent-turns/'+result['agent_turn_id']+'/execute')
        assert executed.status_code==200,executed.text
        with db.connect() as c:
            assert c.execute('SELECT status FROM diagnostic_prompt WHERE prompt_id=?',(prompt['prompt_id'],)).fetchone()[0]=='waiting_answer'
            proposal=c.execute('SELECT state,proposal_json FROM learning_input_proposal').fetchone()
            assert proposal['state']=='clarification_required'
            assert 'expected_minutes' not in json.loads(proposal['proposal_json'])['changes']
            assert c.execute('SELECT count(*) FROM learning_input_revision_event').fetchone()[0]==0
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0]==0
            assert c.execute("SELECT count(*) FROM writing_ocr_event WHERE kind='authorize'").fetchone()[0]==0
