"""Scheduled exact-answer reviews are owned by the runtime, not a page effect."""
import json
from importlib import import_module

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from test_due_review_plan_completion import flow, freeze_clock, CHILD
from test_natural_task_navigation_http import Offline, hear, execute
from test_voice_runtime_binding import submit


def settings_for(flow):
    return Settings(data_dir=flow['db'].path.parent,database_path=flow['db'].path,maintenance_enabled=False)


def review_clock(flow,monkeypatch):
    freeze_clock(monkeypatch,flow['clock'])
    from home_study_agent import repository
    for name in ('diagnostic_prompt','school_open_reviews','voice_control_intent','agent_runtime',
                 'supply_gap_resume','agent_orchestrator','agent_capability_executor'):
        module=import_module('home_study_agent.'+name)
        if hasattr(module,'datetime'):
            monkeypatch.setattr(module,'datetime',repository.datetime)
        if hasattr(module,'utc_now'):
            monkeypatch.setattr(module,'utc_now',lambda:flow['clock'][0])


def test_spoken_start_returns_the_bound_review_before_timer_ack(flow,monkeypatch):
    review_clock(flow,monkeypatch)
    settings=settings_for(flow)
    with TestClient(create_app(settings,model_gateway=Offline())) as client:
        heard,_=hear(client,settings,monkeypatch,flow['session'],'开始下一项')
        assert heard['runtime_capability']=='task.start',heard
        result=execute(client,heard)['execution']['result']
        assert result['plan_item_id']==flow['item']['plan_item_id']
        prompt=result['diagnostic_prompt']['prompt']
        assert prompt['review_task_id']==flow['review']['source_ref']
        assert prompt['diagnostic_item_id']==flow['review']['diagnostic_item_id']
        assert result['status']=='active'
        with flow['db'].connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_prompt WHERE review_task_id=?',(prompt['review_task_id'],)).fetchone()[0]==1
            assert c.execute('SELECT count(*) FROM diagnostic_attempt WHERE session_id=?',(flow['session'],)).fetchone()[0]==0


@pytest.mark.parametrize('answer,correctness', [('42',1),('43',0)])
def test_spoken_review_answer_is_durable_and_only_scores_the_bound_review(flow,monkeypatch,answer,correctness):
    review_clock(flow,monkeypatch)
    settings=settings_for(flow)
    with TestClient(create_app(settings,model_gateway=Offline())) as client:
        started,_=hear(client,settings,monkeypatch,flow['session'],'开始下一项')
        prompt=execute(client,started)['execution']['result']['diagnostic_prompt']['prompt']
        heard,_=hear(client,settings,monkeypatch,flow['session'],'我的答案是'+answer,'review-answer',prompt=prompt)
        response=submit(client,flow['session'],prompt,heard,answer)
        assert response.status_code==200,response.text
        receipt=response.json()
        assert receipt['status']=='answered',receipt
        assert receipt['attempt']['correctness']==correctness
        assert receipt['attempt']['review_task_id']==prompt['review_task_id']
        done,_=hear(client,settings,monkeypatch,flow['session'],'做完了并且检查过了','review-finish')
        assert done['runtime_capability']=='task.complete',done
        finished=execute(client,done)
        if finished['execution']['result']['status']=='awaiting_completion_check':
            done,_=hear(client,settings,monkeypatch,flow['session'],'刚才问的每一项我都完成并检查了','review-confirm')
            finished=execute(client,done)
        assert finished['execution']['result']['status']=='completed',finished
    with TestClient(create_app(settings,model_gateway=Offline())) as client:
        replay=submit(client,flow['session'],prompt,heard,answer)
        assert replay.status_code==200,replay.text
        assert replay.json()['attempt']['attempt_id']==receipt['attempt']['attempt_id']
        with flow['db'].connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_attempt WHERE session_id=?',(flow['session'],)).fetchone()[0]==1
            assert c.execute('SELECT status FROM review_task WHERE review_task_id=?',(prompt['review_task_id'],)).fetchone()[0]=='completed'
            assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?',(flow['item']['plan_item_id'],)).fetchone()[0]=='completed'


@pytest.mark.parametrize('fault', ['source_changed','budget_changed'])
def test_start_preflight_failure_never_leaves_question_or_timer(flow,monkeypatch,fault):
    review_clock(flow,monkeypatch)
    settings=settings_for(flow)
    with TestClient(create_app(settings,model_gateway=Offline())) as client:
        heard,_=hear(client,settings,monkeypatch,flow['session'],'开始下一项')
        with flow['db'].connect() as c:
            if fault=='source_changed':
                c.execute("UPDATE diagnostic_item SET status='inactive' WHERE diagnostic_item_id=?",(flow['review']['diagnostic_item_id'],))
            else:
                c.execute('UPDATE family_rule SET max_learning_minutes=0 WHERE child_id=?',(CHILD,))
            c.commit()
        response=client.post(f"/v1/agent-turns/{heard['agent_turn_id']}/execute")
        assert response.status_code in {200,409},response.text
        if fault=='budget_changed':
            assert response.status_code==200
            assert response.json()['execution']['result']['status']=='blocked'
            assert '还没有出题或开始计时' in response.json()['execution']['response_text']
        with flow['db'].connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_prompt WHERE origin_session_id=?',(flow['session'],)).fetchone()[0]==0
            assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?',(flow['item']['plan_item_id'],)).fetchone()[0]=='pending'


def test_source_revision_after_start_cannot_score_old_prompt(flow,monkeypatch):
    review_clock(flow,monkeypatch)
    settings=settings_for(flow)
    with TestClient(create_app(settings,model_gateway=Offline())) as client:
        started,_=hear(client,settings,monkeypatch,flow['session'],'开始下一项')
        prompt=execute(client,started)['execution']['result']['diagnostic_prompt']['prompt']
        heard,_=hear(client,settings,monkeypatch,flow['session'],'我的答案是42','review-answer',prompt=prompt)
        with flow['db'].connect() as c:
            c.execute("UPDATE diagnostic_item SET prompt=prompt||' changed' WHERE diagnostic_item_id=?",(prompt['diagnostic_item_id'],))
            c.commit()
        response=submit(client,flow['session'],prompt,heard,'42')
        assert response.status_code==200,response.text
        assert response.json()['status']=='invalidated'
        assert response.json()['attempt'] is None
        with flow['db'].connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_attempt WHERE session_id=?',(flow['session'],)).fetchone()[0]==0
            assert c.execute('SELECT status FROM review_task WHERE review_task_id=?',(prompt['review_task_id'],)).fetchone()[0]=='pending'


def test_restart_resume_preserves_original_prompt_and_replay_does_not_restart_timer(flow,monkeypatch):
    review_clock(flow,monkeypatch)
    settings=settings_for(flow)
    with TestClient(create_app(settings,model_gateway=Offline())) as client:
        heard,_=hear(client,settings,monkeypatch,flow['session'],'开始下一项')
        prompt=execute(client,heard)['execution']['result']['diagnostic_prompt']['prompt']
    with TestClient(create_app(settings,model_gateway=Offline())) as client:
        resume,_=hear(client,settings,monkeypatch,flow['session'],'那我们继续吧','review-resume')
        assert resume['runtime_capability']=='task.resume',resume
        result=execute(client,resume)
        assert result['execution']['result']['diagnostic_prompt']['prompt']['prompt_id']==prompt['prompt_id']
        execute(client,resume)
        with flow['db'].connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_prompt WHERE review_task_id=?',(prompt['review_task_id'],)).fetchone()[0]==1
            events=dict(c.execute('SELECT event_type,count(*) FROM plan_item_event WHERE plan_item_id=? GROUP BY event_type',(flow['item']['plan_item_id'],)).fetchall())
            assert events['started']==1
            assert events['resumed']==1


def test_timer_failure_rolls_back_prompt_and_manifest(flow,monkeypatch):
    from home_study_agent.repository import StudyRepository
    review_clock(flow,monkeypatch)
    settings=settings_for(flow)
    original=StudyRepository.start_plan_item
    def fail_after_write(self,*args,**kwargs):
        original(self,*args,**kwargs)
        raise RuntimeError('synthetic failure after task write')
    with TestClient(create_app(settings,model_gateway=Offline()),raise_server_exceptions=False) as client:
        heard,_=hear(client,settings,monkeypatch,flow['session'],'开始下一项')
        with monkeypatch.context() as patch:
            patch.setattr(StudyRepository,'start_plan_item',fail_after_write)
            client.post(f"/v1/agent-turns/{heard['agent_turn_id']}/execute")
        with flow['db'].connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_prompt WHERE origin_session_id=?',(flow['session'],)).fetchone()[0]==0
            assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?',(flow['item']['plan_item_id'],)).fetchone()[0]=='pending'
            assert c.execute("SELECT count(*) FROM plan_item_event WHERE plan_item_id=? AND event_type='started'",(flow['item']['plan_item_id'],)).fetchone()[0]==0
            manifest=json.loads(c.execute('SELECT context_manifest_json FROM agent_plan WHERE agent_turn_id=?',(heard['agent_turn_id'],)).fetchone()[0])
            assert 'goal_prompt_binding' not in manifest
        retry,_=hear(client,settings,monkeypatch,flow['session'],'开始下一项','review-retry')
        assert execute(client,retry)['execution']['result']['status']=='active'
