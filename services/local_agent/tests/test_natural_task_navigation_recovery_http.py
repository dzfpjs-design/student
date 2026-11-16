"""Frozen starts and original unstarted questions across actual host restarts."""
import json

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.repository import StudyRepository
from home_study_agent.agent_runtime import AgentRuntimeStore
from test_diagnostic_prompt_http import environment, OTHER
from test_natural_task_navigation_http import Offline, setup_case, hear, execute
from test_pet_task_resume_http import awaiting_case


@pytest.mark.parametrize('change', ['source','position','cancel','other_child','new_arrival','write_race'])
def test_new_start_never_reselects_after_arrival_or_revocation(environment, monkeypatch, change):
    settings,db=environment
    with TestClient(create_app(settings,model_gateway=Offline()),raise_server_exceptions=False) as client:
        sid,repo=setup_case(client,db,'completed')
        heard,_=hear(client,settings,monkeypatch,sid,'开始下一项')
        assert heard['runtime_capability']=='task.start',heard
        def revoke():
            with db.connect() as c:
                c.execute("UPDATE plan_item SET source_ref='changed-source' WHERE plan_item_id='control-task-2'")
                c.commit()
        if change=='write_race':
            original=StudyRepository.start_plan_item
            def raced(self,*args,**kwargs):
                revoke()
                return original(self,*args,**kwargs)
            monkeypatch.setattr(StudyRepository,'start_plan_item',raced)
        elif change=='new_arrival':
            hear(client,settings,monkeypatch,sid,'别开始',key='newer-hold')
        else:
            with db.connect() as c:
                if change=='source':
                    c.execute("UPDATE plan_item SET source_ref='changed-source' WHERE plan_item_id='control-task-2'")
                elif change=='position':
                    c.execute("UPDATE plan_item SET position=99 WHERE plan_item_id='control-task-2'")
                elif change=='cancel':
                    c.execute("UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id=?",(heard['agent_turn_id'],))
                else:
                    c.execute("UPDATE daily_plan SET child_id=? WHERE plan_id='control-plan'",(OTHER,))
                c.commit()
        response=client.post(f"/v1/agent-turns/{heard['agent_turn_id']}/execute")
        assert response.status_code==409,response.text
        with db.connect() as c:
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id='control-task-2'").fetchone()[0]==0
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==0


@pytest.mark.parametrize('lost',[False,True])
def test_same_start_replay_after_shutdown_does_not_start_again(environment, monkeypatch,lost):
    settings,db=environment
    with TestClient(create_app(settings,model_gateway=Offline()),raise_server_exceptions=False) as client:
        sid,repo=setup_case(client,db,'completed')
        heard,voice=hear(client,settings,monkeypatch,sid,'开始下一项')
        if lost:
            original=StudyRepository.start_plan_item
            def lose(self,*args,**kwargs):
                original(self,*args,**kwargs)
                raise OSError('isolated response lost after actual start')
            with monkeypatch.context() as local:
                local.setattr(StudyRepository,'start_plan_item',lose)
                assert client.post(f"/v1/agent-turns/{heard['agent_turn_id']}/execute").status_code==500
        else:
            execute(client,heard)
    with TestClient(create_app(settings,model_gateway=Offline())) as client:
        replay=client.post(f'/v1/sessions/{sid}/agent-turn',json={
            'transcript':'开始下一项','client_action_id':'navigation','voice_input_id':voice})
        assert replay.status_code==200,replay.text
        assert replay.json()['agent_turn_id']==heard['agent_turn_id']
        execute(client,replay.json())
        assert repo._get_plan_item_by_id('control-task-2')['status']=='paused'
        with db.connect() as c:
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='started' AND plan_item_id='control-task-2'").fetchone()[0]==1


def test_failed_interpretation_keeps_original_arrival_and_does_not_reselect(environment,monkeypatch):
    settings,db=environment
    with TestClient(create_app(settings,model_gateway=Offline()),raise_server_exceptions=False) as client:
        sid,repo=setup_case(client,db,'completed')
        from test_voice_runtime_binding import capture
        voice=capture(client,settings,monkeypatch,sid,'开始下一项',key='first-capture')
        request={'transcript':'开始下一项','client_action_id':'before-turn-save','voice_input_id':voice}
        def failed(*args,**kwargs):
            raise OSError('isolated plan storage failure')
        with monkeypatch.context() as local:
            local.setattr(AgentRuntimeStore,'persist_plan',failed)
            result=client.post(f'/v1/sessions/{sid}/agent-turn',json=request)
            assert result.status_code==500,result.text
        with db.connect() as c:
            saved=c.execute("SELECT snapshot_json FROM learning_input_arrival WHERE client_action_id='before-turn-save'").fetchone()[0]
            assert 'task_navigation_arrival' in saved
            c.execute("UPDATE plan_item SET source_ref='changed-after-failure' WHERE plan_item_id='control-task-2'")
            c.commit()
        response=client.post(f'/v1/sessions/{sid}/agent-turn',json=request)
        assert response.status_code==409,response.text
        with db.connect() as c:
            assert c.execute("SELECT snapshot_json FROM learning_input_arrival WHERE client_action_id='before-turn-save'").fetchone()[0]==saved
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id='control-task-2'").fetchone()[0]==0


@pytest.mark.parametrize('change', ['cancel', 'journal', 'quote', 'source'])
def test_lost_start_receipt_does_not_revive_revoked_request(environment,monkeypatch,change):
    settings,db=environment
    with TestClient(create_app(settings,model_gateway=Offline()),raise_server_exceptions=False) as client:
        sid,repo=setup_case(client,db,'completed')
        heard,_=hear(client,settings,monkeypatch,sid,'开始下一项')
        original=StudyRepository.start_plan_item
        def lose(self,*args,**kwargs):
            original(self,*args,**kwargs)
            raise OSError('isolated lost receipt')
        with monkeypatch.context() as local:
            local.setattr(StudyRepository,'start_plan_item',lose)
            assert client.post(f"/v1/agent-turns/{heard['agent_turn_id']}/execute").status_code==500
        with db.connect() as c:
            if change=='cancel':
                c.execute("UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id=?",(heard['agent_turn_id'],))
                c.execute("UPDATE agent_step SET status='cancelled' WHERE plan_id=(SELECT plan_id FROM agent_plan WHERE agent_turn_id=?)",(heard['agent_turn_id'],))
            elif change=='journal':
                c.execute("UPDATE voice_control_intent_request SET status='cancelled' WHERE session_id=?",(sid,))
            elif change=='quote':
                c.execute("UPDATE agent_turn SET transcript='不同原话' WHERE agent_turn_id=?",(heard['agent_turn_id'],))
            else:
                c.execute("UPDATE plan_item SET source_ref='replaced-source' WHERE plan_item_id='control-task-2'")
            c.commit()
            before=dict(c.execute("SELECT * FROM plan_item WHERE plan_item_id='control-task-2'").fetchone())
        response=client.post(f"/v1/agent-turns/{heard['agent_turn_id']}/execute")
        assert response.status_code==409,response.text
        with db.connect() as c:
            assert dict(c.execute("SELECT * FROM plan_item WHERE plan_item_id='control-task-2'").fetchone())==before
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='started' AND plan_item_id='control-task-2'").fetchone()[0]==1


def test_clarification_does_not_complete_or_replace_unfinished_learning_goal(environment,monkeypatch):
    settings,db=environment
    with TestClient(create_app(settings,model_gateway=Offline())) as client:
        sid,repo=setup_case(client,db,'active')
        with db.connect() as c:
            c.execute('''INSERT INTO agent_objective(objective_id,child_id,session_id,objective_type,
                summary,success_criteria_json,phase,status,created_at,updated_at)
                VALUES('unfinished','child-primary-4',?,'learning','Synthetic unfinished goal',
                '["Explain independently"]','explain','active',datetime('now'),datetime('now'))''',(sid,))
            c.commit()
            original=dict(c.execute("SELECT * FROM agent_objective WHERE objective_id='unfinished'").fetchone())
        heard,_=hear(client,settings,monkeypatch,sid,'开始下一项')
        execute(client,heard)
        with db.connect() as c:
            assert dict(c.execute("SELECT * FROM agent_objective WHERE objective_id='unfinished'").fetchone())==original
            plan=c.execute('SELECT objective_id FROM agent_plan WHERE agent_turn_id=?',(heard['agent_turn_id'],)).fetchone()
            assert plan['objective_id'] is None


@pytest.mark.parametrize('quote', ['开始','那我们继续吧'])
def test_retained_pet_original_start_does_not_prepare_a_replacement(tmp_path,monkeypatch,quote):
    case,settings,prompt=awaiting_case(tmp_path,monkeypatch)
    db,sid,pid,_,_=case
    with TestClient(create_app(settings,model_gateway=Offline())) as client:
        heard,_=hear(client,settings,monkeypatch,sid,quote,prompt=prompt)
        assert heard['runtime_capability']=='task.resume',heard
        result=execute(client,heard)
        actual=result['execution']['result']['diagnostic_prompt']
        assert actual['status']=='waiting_answer'
        assert actual['prompt']['prompt_id']==prompt['prompt_id']
        assert actual['prompt']['item_version']==prompt['item_version']
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==0
            assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt').fetchone()[0]==1
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='started' AND plan_item_id=?",(pid,)).fetchone()[0]==1


def test_retained_pet_original_next_request_cannot_skip_unstarted_question(tmp_path,monkeypatch):
    case,settings,prompt=awaiting_case(tmp_path,monkeypatch)
    db,sid,pid,_,_=case
    with TestClient(create_app(settings,model_gateway=Offline())) as client:
        heard,_=hear(client,settings,monkeypatch,sid,'开始下一项',prompt=prompt)
        assert heard['runtime_capability']=='conversation.respond',heard
        execute(client,heard)
        current=client.get(f'/v1/sessions/{sid}/diagnostic-prompt').json()
        assert current['status']=='awaiting_task_start'
        assert current['prompt']['prompt_id']==prompt['prompt_id']
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==0
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='started' AND plan_item_id=?",(pid,)).fetchone()[0]==0
