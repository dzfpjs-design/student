"""Isolated next-owner reproduction; real HTTP and synthetic semantic input."""
import pytest
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.mistake_dialogue import MistakeDialogueService
from test_mistake_pending_answer_http import waiting
from test_mistake_voice_journey import settings,focus
from test_pet_vocabulary_prompt_integration import Planner
from test_document_voice_capture import begin,complete
from test_foreground_independent_dialogue import foreground_models


@pytest.fixture
def mistake_context(tmp_path):
    config=settings(tmp_path);gateway=Planner();gateway.configured=False
    with TestClient(create_app(config,model_gateway=gateway)) as client:
        sid,db=waiting(client);original=focus(client,sid)
        yield {'settings':config,'db':db,'sid':sid,'client':client,'gateway':gateway,'original':original}


def ask(j,monkeypatch,quote='我们先不看这道错题，你有PET学习资料可以介绍吗？'):
    foreground_models(j,monkeypatch,quote)
    assert begin(j,reference={'kind':'mistake','binding':j['original']['answer_binding']}).status_code==200
    voice=complete(j,text=quote)
    result=j['client'].post(f"/v1/sessions/{j['sid']}/agent-turn",json={
        'transcript':quote,'voice_input_id':voice['voice_input_id'],
        'client_action_id':'independent-mistake-resource','cloud_text_allowed':True})
    assert result.status_code==200,result.text
    return result.json()


def test_mistake_waiting_answer_does_not_swallow_independent_resource_question(mistake_context,monkeypatch):
    j=mistake_context
    with j['db'].connect() as c:
        before=[tuple(row) for row in c.execute('SELECT * FROM mastery_state ORDER BY rowid')]
    result=ask(j,monkeypatch)
    assert focus(j['client'],j['sid'])['answer_binding']==j['original']['answer_binding']
    assert result['action']['type']=='inspect_learning_system',result['action']
    execution=j['client'].post(f"/v1/agent-turns/{result['agent_turn_id']}/execute")
    assert execution.status_code==200,execution.text
    j['client'].__exit__(None, None, None)
    with TestClient(create_app(j['settings'],model_gateway=j['gateway'])) as restarted:
        replay=restarted.post(f"/v1/agent-turns/{result['agent_turn_id']}/execute")
        assert replay.status_code==200 and replay.json()['execution']==execution.json()['execution']
    with j['db'].connect() as c:
        assert c.execute('SELECT count(*) FROM mistake_attempt').fetchone()[0]==0
        assert [tuple(row) for row in c.execute('SELECT * FROM mastery_state ORDER BY rowid')]==before
    assert len(j['gateway'].requests)==2
    import json
    binding=j['original']['answer_binding']
    for request in j['gateway'].requests:
        exported=json.dumps({'messages':request.messages,'manifest':request.context_manifest,'refs':request.evidence_refs})
        for private in (binding['mistake_id'],binding['item_id'],binding['source_version'],
                        j['original']['state']['question_text']):
            assert private not in exported


@pytest.mark.parametrize('change',['pause','hint','source'])
def test_changed_mistake_binding_cannot_execute_an_independent_reply(mistake_context,monkeypatch,change):
    j=mistake_context;result=ask(j,monkeypatch)
    assert result['action']['type']=='inspect_learning_system',result['action']
    if change in {'pause','hint'}:
        MistakeDialogueService(j['db']).respond(j['sid'],'跳过' if change=='pause' else '给我一点提示','external-'+change)
    else:
        with j['db'].connect() as c:
            c.execute("UPDATE diagnostic_item SET prompt=prompt || ' revised' WHERE diagnostic_item_id=?",
                      (j['original']['answer_binding']['item_id'],));c.commit()
    executed=j['client'].post(f"/v1/agent-turns/{result['agent_turn_id']}/execute")
    assert executed.status_code in {409,422},executed.text
    with j['db'].connect() as c:
        assert c.execute('SELECT count(*) FROM mistake_attempt').fetchone()[0]==0


def test_current_mistake_item_quotation_never_enters_independent_cloud_lane(mistake_context,monkeypatch):
    j=mistake_context;quote=j['original']['state']['next_activity']['item']['prompt']+' 这是什么？'
    before=len(j['gateway'].requests)
    result=ask(j,monkeypatch,quote)
    assert result['action']['type']=='none',result['action']
    assert len(j['gateway'].requests)==before
    assert focus(j['client'],j['sid'])['answer_binding']==j['original']['answer_binding']
